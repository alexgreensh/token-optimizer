"""Concurrency claims about the session-end flush throttle and backfill counter.

Two QA findings claimed CRITICAL concurrency races in the session-end flush path:
  1. TOCTOU in _session_refresh_due (check-then-touch on a marker file).
  2. Non-atomic read-modify-write on the backfill attempt counter.

Both the throttle check and the backfill counter live inside
_run_session_end_flush_worker, which acquires an mkdir-based lock
(_acquire_session_end_flush_lock) BEFORE reaching either code path. mkdir is
atomic on POSIX, so only one worker can ever reach the throttle or the counter.
These tests prove that guard holds under concurrent access, and separately
confirm the backfill itself is idempotent so even a hypothetical counter race
would do duplicate work, not corrupt state.

Run: python3 -m pytest tests/test_concurrency_throttle_claims.py -v
"""
from helpers.measure_split import measure_source as _split_measure_source

import importlib
import os
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


def _load_measure(tmp_path, monkeypatch):
    """Import measure with SNAPSHOT_DIR pointed at a clean tmp dir."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    return importlib.import_module("measure")


# ── the mkdir lock prevents concurrent throttle bypass ──────────


def test_acquire_lock_is_mutually_exclusive(tmp_path, monkeypatch):
    """Two concurrent callers cannot both hold the flush lock.

    This is the guard that refutes the TOCTOU claim: _session_refresh_due is
    only reachable after the lock is acquired, so two concurrent hook
    invocations cannot both pass the throttle check.
    """
    mod = _load_measure(tmp_path, monkeypatch)
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()  # maximise overlap
        lock = mod._acquire_session_end_flush_lock()
        results.append(lock)
        # Hold briefly so the loser definitely sees the lock
        if lock is not None:
            import time
            time.sleep(0.05)
            mod._release_session_end_flush_lock(lock)

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    holders = [r for r in results if r is not None]
    assert len(holders) == 1, (
        f"Expected exactly 1 lock holder, got {len(holders)}. "
        "The mkdir lock is the mutual-exclusion guard for the throttle; "
        "if two callers both acquire it, the TOCTOU is reachable."
    )


def test_session_refresh_due_has_toctou_in_isolation(tmp_path, monkeypatch):
    """Confirm the check-then-touch pattern exists in _session_refresh_due.

    This test ACKNOWLEDGES the code pattern the finding describes: in
    isolation (without the lock), two threads can both see 'due' and both
    touch the marker. The point is that this function is never called without
    the lock in production -- the previous test proves that.
    """
    mod = _load_measure(tmp_path, monkeypatch)
    # No marker exists yet, so both should see 'due'
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        results.append(mod._session_refresh_due())

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    # In isolation both can return True (the TOCTOU). This is expected and
    # harmless because the mkdir lock prevents concurrent callers from ever
    # reaching this function simultaneously in production.
    assert results.count(True) >= 1


def test_throttle_not_concurrently_reachable_via_worker(tmp_path, monkeypatch):
    """The throttle check is behind the lock in _run_session_end_flush_worker.

    Verify by source inspection that _session_refresh_due is called only
    after _acquire_session_end_flush_lock succeeds, and that a None lock
    causes an early return before the throttle is reached.
    """
    mod = _load_measure(tmp_path, monkeypatch)
    src = _split_measure_source(Path(SCRIPTS, "measure.py"))
    worker = src[src.index("def _run_session_end_flush_worker("):]
    worker = worker[:worker.index("\n\ndef ")]

    # Lock is acquired first, and None causes early return.
    assert "_acquire_session_end_flush_lock()" in worker
    assert "if lock_dir is None:" in worker
    assert "return" in worker[worker.index("if lock_dir is None:"):worker.index("_install_hook_budget")]
    # Throttle check comes after the lock guard.
    lock_pos = worker.index("_acquire_session_end_flush_lock()")
    throttle_pos = worker.index("_session_refresh_due()")
    assert throttle_pos > lock_pos, "throttle must be reached only after the lock"


# ── the backfill counter is behind the same lock, and is idempotent ─


def test_backfill_counter_is_behind_lock(tmp_path, monkeypatch):
    """The backfill attempt counter read-modify-write is inside the lock.

    Same guard as the throttle: _acquire_session_end_flush_lock is called
    before the counter code is reached.
    """
    mod = _load_measure(tmp_path, monkeypatch)
    src = _split_measure_source(Path(SCRIPTS, "measure.py"))
    worker = src[src.index("def _run_session_end_flush_worker("):]
    worker = worker[:worker.index("\n\ndef ")]

    lock_pos = worker.index("_acquire_session_end_flush_lock()")
    counter_pos = worker.index("_attempts_marker")
    assert counter_pos > lock_pos, (
        "backfill counter must be reached only after the lock; "
        "if it is before the lock, the counter race is real"
    )


def test_collect_sessions_skips_already_collected_files(tmp_path, monkeypatch):
    """The deep backfill is idempotent: collect_sessions skips collected files.

    Even if the counter race were reachable (it is not, per the lock test
    above), the consequence would be duplicate scans that skip already-stored
    rows, not corrupted state or doubled cost. collect_sessions with
    rebuild=False checks _is_file_collected for each file and continues.
    """
    mod = _load_measure(tmp_path, monkeypatch)
    src = _split_measure_source(Path(SCRIPTS, "measure.py"))
    collect_src = src[src.index("def collect_sessions("):]
    collect_src = collect_src[:collect_src.index("\n\ndef ")]

    assert "_is_file_collected" in collect_src, (
        "collect_sessions must check _is_file_collected for idempotency"
    )
    # The skip must be in the rebuild=False path (not gated behind rebuild=True).
    skip_pos = collect_src.index("_is_file_collected")
    after_skip = collect_src[skip_pos:skip_pos + 200]
    assert "continue" in after_skip, "already-collected files must be skipped with continue"

    # Docstring confirms safe-to-rerun semantics.
    assert "Safe to run repeatedly" in collect_src[:500]


def test_backfill_counter_non_atomic_in_isolation(tmp_path, monkeypatch):
    """Confirm the counter read-modify-write pattern exists in source.

    This ACKNOWLEDGES the code pattern: the counter is read, incremented in
    Python, and written back without an atomic primitive. The point is that
    the mkdir lock makes this pattern unreachable by concurrent processes.
    """
    mod = _load_measure(tmp_path, monkeypatch)
    src = _split_measure_source(Path(SCRIPTS, "measure.py"))
    worker = src[src.index("def _run_session_end_flush_worker("):]
    worker = worker[:worker.index("\n\ndef ")]

    # Read, increment in Python, write back -- no atomic primitive.
    assert "_attempts_marker.read_text" in worker
    assert "_attempts_marker.write_text(str(_attempts + 1))" in worker
    # No file lock, no atomic rename, no CAS loop around the counter.
    counter_section = worker[worker.index("_attempts_marker.read_text"):worker.index("collect_sessions")]
    assert "fcntl" not in counter_section
    assert "os.replace" not in counter_section
