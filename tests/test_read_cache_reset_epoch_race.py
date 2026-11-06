#!/usr/bin/env python3
"""Regression tests for the U2 post-reset writer race (read cache).

Reachable invalid state (confirmed at the storage layer): a writer process
that judged a Read BEFORE a context reset (/clear or compaction
``reset_context_state`` / subagent ``clear_file_entries``) but commits its
``file_reads`` upsert AFTER the reset resurrects a pre-clear "already in
context" row. The next Read of that file is then judged redundant and denied
or outline-served even though the model lost the content - silent context
loss, the same harm class as the --clear-compacted fix. SQLite only
serializes individual statements, so the judge->write window is real for two
hook processes and for any harness that does not await PostToolUse before
delivering SessionStart.

Fix: a reset_epoch counter bumped inside the reset transaction; writers
snapshot the epoch at judgment time and their upsert is dropped if the epoch
moved (check + insert in one IMMEDIATE transaction).

Run: python3 -m pytest tests/test_read_cache_reset_epoch_race.py -v
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    sys.path.insert(0, str(SCRIPTS))
    try:
        for mod in ("session_store",):
            sys.modules.pop(mod, None)
        import session_store
        yield session_store.SessionStore(SESSION)
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))


def _entry(tokens=100):
    return {
        "mtime_ns": 123,
        "size_bytes": 400,
        "ranges_seen": [[0, 0]],
        "tokens_est": tokens,
        "read_count": 1,
        "last_access": time.time(),
    }


def test_reset_bumps_epoch(store):
    assert store.get_reset_epoch() == 0
    store.reset_context_state()
    assert store.get_reset_epoch() == 1
    store.reset_context_state()
    assert store.get_reset_epoch() == 2


def test_clear_file_entries_bumps_epoch(store):
    store.clear_file_entries()
    assert store.get_reset_epoch() == 1


def test_guarded_upsert_drops_after_reset(store):
    """Writer judged at epoch 0; reset landed before the write -> dropped."""
    epoch_at_judgment = store.get_reset_epoch()
    store.reset_context_state()  # reset commits in the judge->write window
    ok = store.upsert_file_entry(
        "/repo/a.py", _entry(), expected_reset_epoch=epoch_at_judgment
    )
    assert ok is False
    assert store.get_file_entry("/repo/a.py") is None


def test_guarded_upsert_succeeds_when_epoch_unchanged(store):
    epoch_at_judgment = store.get_reset_epoch()
    ok = store.upsert_file_entry(
        "/repo/a.py", _entry(), expected_reset_epoch=epoch_at_judgment
    )
    assert ok is True
    assert store.get_file_entry("/repo/a.py") is not None


def test_unguarded_upsert_still_works(store):
    """Legacy callers (escape hatch, invalidate) keep unconditional writes."""
    assert store.upsert_file_entry("/repo/b.py", _entry()) is True
    store.reset_context_state()
    assert store.upsert_file_entry("/repo/b.py", _entry()) is True


def test_two_process_interleaving(tmp_path):
    """Executable repro: separate writer/resetter processes over one db.

    The writer snapshots the epoch and judges, sleeps inside the window, then
    issues the guarded write; the resetter resets during the sleep. Pre-fix
    this resurrected the row; post-fix the row must be absent.
    """
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from session_store import SessionStore\n"
        "role = sys.argv[1]\n"
        f"store = SessionStore({SESSION!r})\n"
        "if role == 'writer':\n"
        "    epoch = store.get_reset_epoch()\n"
        "    entry = {'mtime_ns': 1, 'size_bytes': 4, 'ranges_seen': [[0, 0]],\n"
        "             'tokens_est': 1, 'read_count': 1, 'last_access': time.time()}\n"
        "    time.sleep(1.0)\n"
        "    ok = store.upsert_file_entry('/repo/a.py', entry, expected_reset_epoch=epoch)\n"
        "    print('writer_ok', ok)\n"
        "else:\n"
        "    time.sleep(0.3)\n"
        "    store.reset_context_state()\n"
        "    print('reset_done')\n",
        encoding="utf-8",
    )
    env = dict(os.environ, TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path))
    writer = subprocess.Popen(
        [sys.executable, str(runner), "writer"],
        env=env, stdout=subprocess.PIPE, text=True,
    )
    resetter = subprocess.run(
        [sys.executable, str(runner), "resetter"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    out, _ = writer.communicate(timeout=30)
    assert "reset_done" in resetter.stdout
    assert "writer_ok False" in out, out

    sys.path.insert(0, str(SCRIPTS))
    try:
        sys.modules.pop("session_store", None)
        os.environ["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
        from session_store import SessionStore
        assert SessionStore(SESSION).get_file_entry("/repo/a.py") is None
    finally:
        sys.path.remove(str(SCRIPTS))


# ---- F4: corrupt epoch value handling ----


# ---- F4: corrupt stored epoch -> DROP (never normalize, never raise) ----


def test_guarded_upsert_accepts_matching_zero_epoch(store):
    """Control: fresh db (no epoch row reads as 0), writer at epoch 0 -> write."""
    ok = store.upsert_file_entry("/repo/z0.py", _entry(), expected_reset_epoch=0)
    assert ok is True
    assert store.get_file_entry("/repo/z0.py") is not None


def test_guarded_upsert_accepts_matching_positive_epoch(store):
    """Control: real epoch 3, writer holding 3 -> write."""
    store.set_meta("reset_epoch", "3")
    ok = store.upsert_file_entry("/repo/z3.py", _entry(), expected_reset_epoch=3)
    assert ok is True
    assert store.get_file_entry("/repo/z3.py") is not None


def test_corrupt_current_epoch_drops_and_next_reset_heals(store):
    """Corrupt stored epoch must DROP, never normalize to 0: 'no reset since
    corruption' and 'reset landed, THEN corruption' are indistinguishable from
    the value alone, and normalizing would accept a stale epoch-0 writer in the
    second case -- resurrecting the pre-reset row this guard exists to drop.
    Dropping is fail-open (the read/edit proceeds; only the ledger write is
    skipped) and the next reset bump CASTs the value to a clean integer,
    re-engaging the guard."""
    store.set_meta("reset_epoch", "not-a-number")
    assert store.get_reset_epoch() == 0  # tolerant read, unchanged behavior
    # A writer holding epoch 0 (the tolerant-read value) is STILL dropped:
    # it may be a pre-reset writer whose reset bumped the epoch before the
    # value was corrupted.
    ok = store.upsert_file_entry("/repo/c.py", _entry(), expected_reset_epoch=0)
    assert ok is False
    assert store.get_file_entry("/repo/c.py") is None
    # The next reset heals the value to a clean integer and the guard re-engages.
    store.reset_context_state()
    assert store.get_reset_epoch() == 1
    ok = store.upsert_file_entry("/repo/c.py", _entry(), expected_reset_epoch=1)
    assert ok is True
    assert store.get_file_entry("/repo/c.py") is not None


def test_stale_real_epoch_still_drops_against_corrupt_current(store):
    """A writer holding a real stale epoch mismatches a corrupt current value:
    drop (the safe direction)."""
    store.set_meta("reset_epoch", "not-a-number")
    ok = store.upsert_file_entry("/repo/d.py", _entry(), expected_reset_epoch=5)
    assert ok is False
    assert store.get_file_entry("/repo/d.py") is None


@pytest.mark.parametrize("bad", ["5.7", "", "1e3", "NaN", "inf", "0x10", " "])
def test_bad_stored_epoch_types_drop(store, bad):
    """Non-integer-string stored values are corrupt: drop, never raise, never
    accept -- regardless of the expected epoch."""
    store.set_meta("reset_epoch", bad)
    ok = store.upsert_file_entry("/repo/t.py", _entry(), expected_reset_epoch=0)
    assert ok is False
    assert store.get_file_entry("/repo/t.py") is None
    ok = store.upsert_file_entry("/repo/t.py", _entry(), expected_reset_epoch=5)
    assert ok is False


def test_unparseable_expected_epoch_drops(store):
    """A writer that cannot state its epoch has no business writing
    conditionally: drop, do not raise (no hook-error regression)."""
    ok = store.upsert_file_entry("/repo/u.py", _entry(), expected_reset_epoch="junk")
    assert ok is False
    assert store.get_file_entry("/repo/u.py") is None


def test_whitespace_padded_integer_epoch_is_not_corrupt(store):
    """int() semantics: a padded integer string is a clean value, matches."""
    store.set_meta("reset_epoch", " 5")
    ok = store.upsert_file_entry("/repo/w.py", _entry(), expected_reset_epoch=5)
    assert ok is True
