#!/usr/bin/env python3
"""Regression: duplicate keep-warm arm records must not multiply pings.

Reachable state (evidence below): a double-registered Stop hook -- the
settings-fossil scenario ``install_reconcile`` / ``_reconcile_sessionend_fossils``
exist to clean up -- fires ``keepwarm-arm`` twice for one stop, and any two Stop
events without transcript growth between them do the same. Both calls append an
arm record with an IDENTICAL (session_id, transcript_mtime, byte_size) snapshot,
because ``write_keepwarm_arm_record`` never dedups.

The tick loop then fires one probe ping PER RECORD for the same paused session
in a single tick, because:

  * the per-pause probe cap (``pings_fired``, max 2 probes) is tracked per
    record, not per session;
  * the ping command uses ``--no-session-persistence``, so a fired ping never
    bumps the transcript mtime that the idle re-check and the classifier watch
    -- the second record's ``recently-active`` guard never trips.

That violates the documented economics ("max 2 pings unless the predictor
promotes sustain", keepwarm_enable copy): with the LLM ping escape hatch on,
one pause is probed 2 x (record count) times per cadence window.

These tests pin the fix: arming is idempotent for an unchanged transcript
snapshot, so a duplicate arm is dropped and one tick fires at most one probe
per paused session.

Run: python3 -m pytest tests/test_keepwarm_arm_dedup.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def m(monkeypatch, tmp_path):
    """Fresh measure module against a fully sandboxed home/snapshot dir."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    # The LLM ping is dogfood-gated; the duplicate-fire path only exists when
    # the escape hatch is explicitly on.
    monkeypatch.setenv("TOKEN_OPTIMIZER_KEEPWARM_ALLOW_LLM_PING", "1")
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _write_transcript(tmp_path: Path, age_seconds: float) -> Path:
    """A paused session's transcript: one usage-bearing assistant turn, old mtime."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "session-abc123.jsonl"
    ts = datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).isoformat()
    record = {
        "type": "assistant",
        "timestamp": ts,
        "cwd": str(project),
        "message": {
            "model": "claude-sonnet-4-5",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 50,
                "cache_read_input_tokens": 20000,
                "cache_creation": {"ephemeral_5m_input_tokens": 1000},
            },
        },
    }
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(transcript, (old, old))
    return transcript


def _arm(m, tmp_path, age_seconds=7200.0):
    mod, _ = m
    transcript = _write_transcript(tmp_path, age_seconds)
    rec = mod.write_keepwarm_arm_record("abc123", str(transcript))
    assert rec is not None, "arm setup failed (transcript usage not recognized)"
    return transcript


def _sidecar_records(mod):
    return mod.load_keepwarm_records()


def test_arm_is_idempotent_for_an_unchanged_snapshot(m, tmp_path):
    """Two arms of the same unchanged transcript keep ONE record.

    Reachability: hooks.json double-registration (fossil) fires the Stop path
    twice for one stop; both arms see the same transcript mtime/size.
    """
    mod, _ = m
    transcript = _arm(m, tmp_path)
    rec2 = mod.write_keepwarm_arm_record("abc123", str(transcript))
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert len(records) == 1, (
        f"duplicate arm records for one unchanged transcript snapshot: {len(records)}"
    )


def test_arm_after_real_turn_appends(m, tmp_path):
    """Dedup must not eat genuine re-arms: a grown transcript arms again."""
    mod, _ = m
    transcript = _arm(m, tmp_path)
    # A new turn: transcript grows and mtime advances.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": "x"}) + "\n")
    newer = time.time() - 3600
    os.utime(transcript, (newer, newer))
    rec2 = mod.write_keepwarm_arm_record("abc123", str(transcript))
    assert rec2 is not None
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert len(records) == 2


def test_one_tick_fires_one_probe_per_paused_session(m, tmp_path):
    """End-to-end: with duplicate records present, one tick fires ONE ping."""
    mod, _ = m
    transcript = _write_transcript(tmp_path, 7200.0)
    # Simulate the fossil double-fire directly at the sidecar level (two
    # identical-snapshot records, as two Stop-hook registrations produce).
    for _ in range(2):
        rec = mod.write_keepwarm_arm_record("abc123", str(transcript))
        assert rec is not None

    fires = []

    class _Proc:
        returncode = 0
        stdout = json.dumps({"cost_usd": 0.001, "cache_read": 20000, "cache_write": 0})
        stderr = ""

    def _fake_runner(cmd, cwd, env, timeout):
        fires.append(list(cmd))
        return _Proc()

    summary = mod.keepwarm_tick(
        now=time.time(),
        env=dict(os.environ),
        runner=_fake_runner,
        gate=lambda **kw: (True, "ok"),
    )
    sid_fires = [c for c in fires if "abc123" in [str(a) for a in c]]
    assert len(sid_fires) <= 1, (
        f"one tick fired {len(sid_fires)} pings for one paused session "
        f"(duplicate arm records multiply the per-pause probe cap); "
        f"decisions={summary.get('decisions')}"
    )


def test_two_sessions_arm_independently(m, tmp_path):
    """Dedup is keyed per session: two legit sessions each keep their record."""
    mod, _ = m
    t1 = _write_transcript(tmp_path, 7200.0)
    t2 = tmp_path / "session-def456.jsonl"
    t2.write_text(t1.read_text(encoding="utf-8"), encoding="utf-8")
    old = time.time() - 7200.0
    os.utime(t2, (old, old))
    assert mod.write_keepwarm_arm_record("abc123", str(t1)) is not None
    assert mod.write_keepwarm_arm_record("def456", str(t2)) is not None
    sids = sorted(r.get("session_id") for r in _sidecar_records(mod))
    assert sids == ["abc123", "def456"]


def _race_two_arms(mod, transcript):
    import threading
    barrier = threading.Barrier(2)
    results, errors = [], []

    def _arm_again():
        try:
            barrier.wait()
            results.append(mod.write_keepwarm_arm_record("abc123", str(transcript)))
        except Exception as exc:  # the writer must never raise into the hook chain
            errors.append(exc)

    threads = [threading.Thread(target=_arm_again) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _assert_race_invariants(mod, results, errors):
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert not errors, errors
    assert len(results) == 2
    assert len(records) == 1, f"racing arms produced {len(records)} records"
    # The lock is a bounded lease (75ms): a contender that cannot get it skips the
    # mutation and returns None BY DESIGN, so a loaded runner may legitimately hand
    # one racer None. What must hold: never an exception, exactly one stored record,
    # at least one racer saw it, and every non-None result is that one record.
    seen = [r for r in results if r is not None]
    assert seen, "neither racer saw the armed record"
    assert all(r.get("transcript_mtime") == records[0]["transcript_mtime"]
               and r.get("byte_size") == records[0]["byte_size"] for r in seen)


def test_concurrent_arms_collapse_to_one_record(m, tmp_path):
    """Two racing Stop-hook processes (fossil double-registration) arm once."""
    mod, _ = m
    transcript = _arm(m, tmp_path)
    results, errors = _race_two_arms(mod, transcript)
    _assert_race_invariants(mod, results, errors)


def test_concurrent_arms_when_the_lock_holder_outlasts_the_lease(m, tmp_path):
    """Forced contention: the holder spends longer than the 75ms lease timeout scanning the
    sidecar, so the other racer is skipped (None). Still one record, no exception."""
    mod, _ = m
    transcript = _arm(m, tmp_path)
    original = mod.load_keepwarm_records

    def slow_load(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    mod.load_keepwarm_records = slow_load
    try:
        results, errors = _race_two_arms(mod, transcript)
    finally:
        mod.load_keepwarm_records = original
    _assert_race_invariants(mod, results, errors)
    assert any(r is None for r in results), "precondition: the lease timeout skipped one racer"


def test_truncated_rotated_transcript_rearms(m, tmp_path):
    """Rotation/truncation is a NEW snapshot: the dedup must not swallow it.

    A rotated transcript shares the session id but not the snapshot; refusing
    to re-arm would leave the fresh pause unwatched.
    """
    mod, _ = m
    transcript = _arm(m, tmp_path)
    # Rotate: replace with a shorter, newer transcript (different size).
    ts = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": ts,
            "cwd": str(tmp_path / "project"),
            "message": {
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 18000,
                    "cache_creation": {"ephemeral_5m_input_tokens": 500},
                },
            },
        }) + "\n",
        encoding="utf-8",
    )
    newer = time.time() - 3600
    os.utime(transcript, (newer, newer))
    assert mod.write_keepwarm_arm_record("abc123", str(transcript)) is not None
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert len(records) == 2
