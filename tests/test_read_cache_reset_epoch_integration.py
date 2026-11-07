#!/usr/bin/env python3
"""End-to-end K2 test: handle_read drops the record when a reset lands mid-judgment.

Unit and two-process coverage live in test_read_cache_reset_epoch_race.py; this
drives the REAL handle_read PreToolUse path (compaction scan -> epoch snapshot
-> judgment -> guarded record write) with a simulated concurrent resetter
committing reset_context_state inside the judge->write window. The read itself
must still succeed (fail-open); only the ledger write is dropped, so the next
Read is judged a first read instead of resurrecting a pre-clear row.

Run: python3 -m pytest tests/test_read_cache_reset_epoch_integration.py -v
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
SESSION = "33333333-4444-5555-6666-777777777777"


@pytest.fixture()
def rc(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    cfg = tmp_path / "claude"
    cfg.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    monkeypatch.setenv("TOKEN_OPTIMIZER_READ_CACHE_MODE", "block")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for mod in ("read_cache", "session_store"):
        sys.modules.pop(mod, None)
    session_store = importlib.import_module("session_store")
    read_cache = importlib.import_module("read_cache")
    return read_cache, session_store, tmp_path


def _hook_input(tmp_path, file_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-11-06T10:00:00Z",
            "cwd": str(tmp_path),
            "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 1}},
        }) + "\n",
        encoding="utf-8",
    )
    return {
        "session_id": SESSION,
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(file_path)},
    }


def test_handle_read_drops_record_when_reset_lands_mid_judgment(rc, monkeypatch):
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")  # >16KB

    original = session_store.SessionStore.upsert_file_entry
    drops_armed = {"done": False}

    def racing_upsert(self, file_path, entry, expected_reset_epoch=None):
        if expected_reset_epoch is not None and not drops_armed["done"]:
            drops_armed["done"] = True
            # Concurrent resetter: a sibling process commits the reset inside
            # this writer's judge->write window.
            session_store.SessionStore(SESSION).reset_context_state()
        return original(
            self, file_path, entry, expected_reset_epoch=expected_reset_epoch
        )

    monkeypatch.setattr(
        session_store.SessionStore, "upsert_file_entry", racing_upsert
    )

    # Must not raise: the read decision itself is unaffected (fail-open).
    read_cache.handle_read(_hook_input(tmp_path, target), "block", True)

    assert drops_armed["done"], "guarded write path not exercised"
    store = session_store.SessionStore(SESSION)
    assert store.get_reset_epoch() == 1
    assert store.get_file_entry(str(target)) is None, (
        "reset-mid-judgment write must be dropped, not resurrect a pre-clear row"
    )


def test_handle_read_records_normally_without_reset(rc):
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")
    read_cache.handle_read(_hook_input(tmp_path, target), "block", True)
    store = session_store.SessionStore(SESSION)
    assert store.get_file_entry(str(target)) is not None


def _seed_redundant_entry(store, target):
    """Seed the ledger as if `target` was fully read earlier: matching
    mtime/size, full-range coverage, and a denial counter one below the escape
    hatch threshold, so the next judged-redundant Read MUST route through
    _check_escape_hatch (the production caller wired in F3)."""
    st = target.stat()
    store.upsert_file_entry(
        str(target),
        {
            "mtime_ns": st.st_mtime_ns,
            "size_bytes": st.st_size,
            "ranges_seen": [[0, 0]],
            "tokens_est": 4000,
            "read_count": 3,
            "last_access": 1.0,
            "consecutive_denials": 2,  # MAX_CONSECUTIVE_DENIALS - 1
        },
    )


def test_handle_read_escape_hatch_write_dropped_when_reset_lands_mid_judgment(rc, monkeypatch):
    """Real-path F3, block-mode branch (read_cache.py `mode == 'block'`
    `_check_escape_hatch` call): the hatch's ledger write goes through the
    epoch guard via the production handle_read call chain (no helper
    injection). A reset committing inside the judge->write window drops the
    hatch write, so the denial-counter row is NOT resurrected post-clear."""
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")  # >16KB
    store = session_store.SessionStore(SESSION)
    _seed_redundant_entry(store, target)
    assert store.get_file_entry(str(target)) is not None

    # consecutive_denials is an in-memory counter (no file_reads column), so an
    # unpatched hook process always judges at 0 and the hatch needs 3 denials
    # inside one call. Patching the THRESHOLD to 1 fires the hatch on this
    # read through the exact production call chain
    # (handle_read -> _check_escape_hatch -> guarded upsert); only the tuning
    # constant changes, not the wiring under test.
    monkeypatch.setattr(read_cache, "MAX_CONSECUTIVE_DENIALS", 1)

    original = session_store.SessionStore.upsert_file_entry
    drops_armed = {"done": False}

    def racing_upsert(self, file_path, entry, expected_reset_epoch=None):
        if expected_reset_epoch is not None and not drops_armed["done"]:
            drops_armed["done"] = True
            # Pin WHICH writer raced: the hatch resets the counter to 0 before
            # its write; the deny path would carry 1.
            drops_armed["denials"] = int(entry.get("consecutive_denials", -1))
            # Concurrent resetter commits inside this writer's judge->write window.
            session_store.SessionStore(SESSION).reset_context_state()
        return original(
            self, file_path, entry, expected_reset_epoch=expected_reset_epoch
        )

    monkeypatch.setattr(
        session_store.SessionStore, "upsert_file_entry", racing_upsert
    )

    # Must not raise: the escape hatch ALLOWS the read (fail-open); only the
    # ledger write is dropped. The drop itself proves the wired reset_epoch
    # reached the guarded upsert: without the F3 wiring this write would be
    # legacy-unconditional and the row would be resurrected.
    read_cache.handle_read(_hook_input(tmp_path, target), "block", True)

    assert drops_armed["done"], "guarded write path not exercised"
    assert drops_armed["denials"] == 0, (
        "the raced write must be the escape hatch's (counter reset to 0), "
        "not the deny path's"
    )
    store = session_store.SessionStore(SESSION)
    assert store.get_reset_epoch() == 1
    assert store.get_file_entry(str(target)) is None, (
        "escape-hatch write after a mid-judgment reset must be dropped, not "
        "resurrect a pre-clear denial-counter row"
    )


def test_handle_invalidate_rewrite_dropped_when_reset_lands_mid_hook(rc, monkeypatch):
    """Real-path F3: handle_invalidate's post-edit rewrite goes through the
    epoch guard. A reset committing between the hook's start (epoch snapshot)
    and its rewrite drops the write instead of resurrecting a pre-clear row."""
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")
    store = session_store.SessionStore(SESSION)
    _seed_redundant_entry(store, target)

    original = session_store.SessionStore.upsert_file_entry
    drops_armed = {"done": False}

    def racing_upsert(self, file_path, entry, expected_reset_epoch=None):
        if expected_reset_epoch is not None and not drops_armed["done"]:
            drops_armed["done"] = True
            session_store.SessionStore(SESSION).reset_context_state()
        return original(
            self, file_path, entry, expected_reset_epoch=expected_reset_epoch
        )

    monkeypatch.setattr(
        session_store.SessionStore, "upsert_file_entry", racing_upsert
    )

    hook_input = {
        "session_id": SESSION,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }
    read_cache.handle_invalidate(hook_input, True)

    assert drops_armed["done"], "guarded write path not exercised"
    store = session_store.SessionStore(SESSION)
    assert store.get_reset_epoch() == 1
    assert store.get_file_entry(str(target)) is None, (
        "invalidate rewrite after a mid-hook reset must be dropped, not "
        "resurrect a pre-clear row"
    )


def test_handle_invalidate_rewrites_normally_without_reset(rc):
    """Control: no reset -> the post-edit rewrite lands (guard is a no-op)."""
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")
    store = session_store.SessionStore(SESSION)
    _seed_redundant_entry(store, target)
    hook_input = {
        "session_id": SESSION,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }
    read_cache.handle_invalidate(hook_input, True)
    store = session_store.SessionStore(SESSION)
    entry = store.get_file_entry(str(target))
    assert entry is not None
    assert int(entry["mtime_ns"]) == target.stat().st_mtime_ns


def _invalidate_message(rc, capsys, monkeypatch, race=False, corrupt=False, legacy_none=False):
    """Run handle_invalidate with output enabled; return (stderr, stored entry)."""
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")
    _seed_redundant_entry(session_store.SessionStore(SESSION), target)
    original = session_store.SessionStore.upsert_file_entry
    if race:
        def racing_upsert(self, file_path, entry, expected_reset_epoch=None):
            session_store.SessionStore(SESSION).reset_context_state()
            return original(self, file_path, entry, expected_reset_epoch=expected_reset_epoch)

        monkeypatch.setattr(session_store.SessionStore, "upsert_file_entry", racing_upsert)
    if corrupt:
        # Corrupt AFTER the hook snapshotted a clean epoch, so the guarded write sees an unreadable one.
        def corrupting_upsert(self, file_path, entry, expected_reset_epoch=None):
            conn = session_store.SessionStore(SESSION)._connect()
            conn.execute("INSERT OR REPLACE INTO session_meta(key, value) VALUES ('reset_epoch', 'garbage')")
            conn.commit()
            return original(self, file_path, entry, expected_reset_epoch=expected_reset_epoch)

        monkeypatch.setattr(session_store.SessionStore, "upsert_file_entry", corrupting_upsert)
    if legacy_none:
        def none_upsert(self, *args, **kwargs):
            original(self, *args, **kwargs)  # a legacy store that returns None

        monkeypatch.setattr(session_store.SessionStore, "upsert_file_entry", none_upsert)
    capsys.readouterr()
    read_cache.handle_invalidate(
        {"session_id": SESSION, "tool_name": "Edit", "tool_input": {"file_path": str(target)}}, False)
    return capsys.readouterr().err, session_store.SessionStore(SESSION).get_file_entry(str(target))


def test_invalidate_message_reports_a_dropped_refresh_truthfully(rc, capsys, monkeypatch):
    err, entry = _invalidate_message(rc, capsys, monkeypatch, race=True)
    assert entry is None, "precondition: the reset dropped the write"
    assert "Refresh skipped" in err and "Refreshed after edit" not in err


def test_invalidate_message_reports_a_corrupt_epoch_drop_without_claiming_a_reset_only(rc, capsys, monkeypatch):
    err, entry = _invalidate_message(rc, capsys, monkeypatch, corrupt=True)
    assert entry is not None and entry["last_access"] == 1.0, "precondition: the seeded row was NOT refreshed"
    assert "Refresh skipped" in err and "unreadable epoch" in err and "Refreshed after edit" not in err


def test_invalidate_message_says_refreshed_when_the_write_landed(rc, capsys, monkeypatch):
    err, entry = _invalidate_message(rc, capsys, monkeypatch)
    assert entry is not None
    assert "Refreshed after edit" in err and "Refresh skipped" not in err


def test_invalidate_message_keeps_legacy_none_return_wording(rc, capsys, monkeypatch):
    err, entry = _invalidate_message(rc, capsys, monkeypatch, legacy_none=True)
    assert entry is not None
    assert "Refreshed after edit" in err and "Refresh skipped" not in err


def test_handle_read_escape_hatch_eligible_branch_write_dropped(rc, monkeypatch):
    """Real-path F3, eligible-structure branch (the other production
    `_check_escape_hatch` call site): a structure-eligible redundant read that
    escapes must ALSO route its ledger write through the epoch guard."""
    read_cache, session_store, tmp_path = rc
    target = tmp_path / "many_funcs.py"
    q = chr(34) * 3
    body = []
    for i in range(120):
        body.append(
            f"def func_{i}(a, b):\n"
            f"    {q}Docstring for function {i} describing behavior at some length.{q}\n"
            f"    return a + b + {i}\n"
        )
    target.write_text("\n".join(body), encoding="utf-8")
    store = session_store.SessionStore(SESSION)
    _seed_redundant_entry(store, target)
    assert store.get_file_entry(str(target)) is not None

    monkeypatch.setattr(read_cache, "MAX_CONSECUTIVE_DENIALS", 1)

    original = session_store.SessionStore.upsert_file_entry
    drops_armed = {"done": False}

    def racing_upsert(self, file_path, entry, expected_reset_epoch=None):
        if expected_reset_epoch is not None and not drops_armed["done"]:
            drops_armed["done"] = True
            drops_armed["denials"] = int(entry.get("consecutive_denials", -1))
            session_store.SessionStore(SESSION).reset_context_state()
        return original(
            self, file_path, entry, expected_reset_epoch=expected_reset_epoch
        )

    monkeypatch.setattr(
        session_store.SessionStore, "upsert_file_entry", racing_upsert
    )

    read_cache.handle_read(_hook_input(tmp_path, target), "block", True)

    assert drops_armed["done"], "guarded write path not exercised"
    assert drops_armed["denials"] == 0, (
        "the raced write must be the escape hatch's (counter reset to 0)"
    )
    store = session_store.SessionStore(SESSION)
    assert store.get_reset_epoch() == 1
    assert store.get_file_entry(str(target)) is None, (
        "eligible-branch escape-hatch write after a mid-judgment reset must be "
        "dropped, not resurrect a pre-clear row"
    )
