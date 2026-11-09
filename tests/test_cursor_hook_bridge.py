"""Cursor hook bridge: payload decoding, tally lifecycle, and output contracts.

Covers the bridge behaviours that the Copilot adapter does NOT share with
Cursor, so they can't be inherited by copy:

  - snake_case payload decoding (object or JSON-string ``tool_input``) with a
    traversal-safe conversation-id sanitizer;
  - the durable tally read-modify-write under ``sessions/<id>.json``, including
    the reopen rule that flips a final/idle row back to active on new activity;
  - the observed-events ledger (KTD7/R13) and the ``rewrite_honoured`` /
    ``rewrite_ignored`` distinction the doctor and docs consume;
  - top-level Cursor output contracts (``permission``/``updated_input``,
    ``additional_context``), NOT the Copilot ``hookSpecificOutput`` envelope;
  - per-workspace restore injection keyed by ``sha1(workspace_root)``;
  - stop throttling (one rollup+dashboard per 120s) vs sessionEnd unthrottled.

All fixtures are built under tmp_path; no network and no writes outside
tmp_path (``cursor_home`` is monkeypatched to a tmp path).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_hook_bridge as bridge  # noqa: E402


def simple():
    return SimpleNamespace(pid=4242)


@pytest.fixture()
def cursor_dir(monkeypatch, tmp_path):
    cur = tmp_path / ".cursor"
    cur.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    return cur


def _to_dir(cursor_dir):
    return cursor_dir / "token-optimizer"


def _fake_bash(monkeypatch, *, whitelisted=True, dangerous=False):
    monkeypatch.setattr(
        bridge,
        "_bash_hook",
        SimpleNamespace(
            _is_whitelisted=lambda c: whitelisted,
            _has_dangerous_chars=lambda c: dangerous,
            _is_auto_approvable=lambda c: whitelisted and not dangerous,
        ),
    )


def _read_last_observed(cursor_dir):
    path = _to_dir(cursor_dir) / "observed-events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def _shell_payload(command, sid="composer-abc123"):
    return {
        "hook_event_name": "postToolUse",
        "tool_name": "Shell",
        "tool_input": {"command": command, "working_directory": "/Users/alex/repos/demo"},
        "conversation_id": sid,
        "cursor_version": "3.18.9",
        "workspace_roots": ["/Users/alex/repos/demo"],
    }


# ---------------------------------------------------------------------------
# decode_payload
# ---------------------------------------------------------------------------


def test_decode_snake_case_object_and_string_tool_input(cursor_dir):
    obj = _shell_payload("echo hi")
    fields = bridge.decode_payload(obj)
    assert fields["tool_name"] == "Shell"
    assert fields["tool_args"]["command"] == "echo hi"
    assert fields["conversation_id"] == "composer-abc123"

    string_input = dict(obj)
    string_input["tool_input"] = json.dumps(obj["tool_input"])
    fields = bridge.decode_payload(string_input)
    assert fields["tool_args"]["command"] == "echo hi"
    assert fields["tool_args"]["working_directory"] == "/Users/alex/repos/demo"


def test_decode_rejects_traversal_and_short_ids(cursor_dir):
    # Dots/slashes are stripped, so a traversal id cannot escape the data dir.
    out = bridge.decode_payload({"conversation_id": "../../../etc/passwd"})
    assert out["conversation_id"] == "etcpasswd"
    assert "/" not in out["conversation_id"] and ".." not in out["conversation_id"]

    # Shorter than 6 chars after sanitize -> unknown.
    out = bridge.decode_payload({"conversation_id": "ab"})
    assert out["conversation_id"] == "unknown"


# ---------------------------------------------------------------------------
# Tally lifecycle
# ---------------------------------------------------------------------------


def test_post_tool_use_writes_tally_and_reopens_idle(cursor_dir, monkeypatch):
    sdir = _to_dir(cursor_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "composer-abc123.json").write_text(
        json.dumps({
            "conversation_id": "composer-abc123",
            "first_ts": time.time() - 900,
            "updated_at": time.time() - 60,
            "turns": 1,
            "tool_calls": 4,
            "final": True,
            "end_reason": "idle",
        }),
        encoding="utf-8",
    )
    _fake_bash(monkeypatch)

    bridge.handle_post_tool_use(_shell_payload("echo hi"))

    tally = json.loads((sdir / "composer-abc123.json").read_text(encoding="utf-8"))
    assert tally["tool_calls"] == 5
    assert tally["final"] is False
    assert tally["end_reason"] == ""


def test_session_end_marks_tally_final(cursor_dir, monkeypatch):
    monkeypatch.setattr(bridge, "spawn_detached", lambda argv, **k: simple())
    bridge.handle_session_end({
        "hook_event_name": "sessionEnd",
        "conversation_id": "composer-abc123",
        "reason": "user_exit",
        "cursor_version": "3.18.9",
    })
    tally = json.loads(
        (_to_dir(cursor_dir) / "sessions" / "composer-abc123.json").read_text(encoding="utf-8")
    )
    assert tally["final"] is True
    assert tally["end_reason"] == "user_exit"


# ---------------------------------------------------------------------------
# G0: hostile payload envelope + hot-path bounds
# ---------------------------------------------------------------------------


def test_deeply_nested_stdin_payload_still_exits_zero():
    """G0 P1: '['*2000 raises RecursionError inside json.loads; the bridge
    must treat it as undecodable and still exit 0 (always-exit-0 invariant)."""
    import subprocess as sp

    proc = sp.run(
        [sys.executable, str(SCRIPTS / "cursor_hook_bridge.py"), "preToolUse"],
        input="[" * 2000 + "]" * 2000,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr[-300:]


def test_nested_tool_input_string_never_raises(cursor_dir, monkeypatch):
    payload = _shell_payload("git status")
    payload["tool_input"] = "[" * 1500 + "]" * 1500
    bridge.handle_pre_tool_use(payload)  # must not raise


def test_oversized_command_skips_rewrite_fast(cursor_dir, monkeypatch, capsys):
    """G0 P1: shlex is quadratic on hostile lengths; commands past the cap
    pass through uncompressed without touching the whitelist/parser."""
    calls = []
    monkeypatch.setattr(bridge, "_bash_hook", SimpleNamespace(
        _is_auto_approvable=lambda c: calls.append(c) or True,
    ))
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "_COMPRESS_PATH", SCRIPTS / "bash_compress.py")

    bridge.handle_pre_tool_use(_shell_payload("git status #" + "x" * 9000))

    assert capsys.readouterr().out.strip() == ""
    assert calls == []  # whitelist never invoked on an oversized command


# ---------------------------------------------------------------------------
# preToolUse output contract + rewrite
# ---------------------------------------------------------------------------


def test_pre_tool_use_no_rewrite_on_native_windows(cursor_dir, monkeypatch, capsys):
    """G4: shlex single-quote quoting is POSIX-only; cmd.exe would break, and
    the actual Windows Shell host is unverified - record-only there."""
    _fake_bash(monkeypatch, whitelisted=True, dangerous=False)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "_COMPRESS_PATH", SCRIPTS / "bash_compress.py")
    monkeypatch.setattr(bridge.os, "name", "nt")

    bridge.handle_pre_tool_use(_shell_payload("git status"))

    assert capsys.readouterr().out.strip() == ""  # no rewrite emitted


@pytest.mark.skipif(os.name == "nt", reason="shell rewriting is disabled on native Windows")
def test_pre_tool_use_rewrites_and_echoes_fields(cursor_dir, monkeypatch, capsys):
    _fake_bash(monkeypatch, whitelisted=True, dangerous=False)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "_COMPRESS_PATH", SCRIPTS / "bash_compress.py")

    bridge.handle_pre_tool_use(_shell_payload("git status"))

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["permission"] == "allow"
    updated = emitted["updated_input"]
    assert "bash_compress.py" in updated["command"]
    # The whole-tool_input replacement must preserve the untouched fields.
    assert updated["working_directory"] == "/Users/alex/repos/demo"


def test_pre_tool_use_non_shell_emits_nothing_records_event(cursor_dir, monkeypatch, capsys):
    _fake_bash(monkeypatch)
    bridge.handle_pre_tool_use({
        "hook_event_name": "preToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/x"},
        "conversation_id": "composer-abc123",
        "cursor_version": "3.18.9",
    })
    assert capsys.readouterr().out == ""
    assert _read_last_observed(cursor_dir)["tool_name"] == "Read"


def test_pre_tool_use_dangerous_command_emits_nothing(cursor_dir, monkeypatch, capsys):
    # Fail closed: a whitelisted command with dangerous chars must emit NOTHING
    # (no permission/updated_input), and must not record a rewrite attempt.
    _fake_bash(monkeypatch, whitelisted=True, dangerous=True)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)

    bridge.handle_pre_tool_use(_shell_payload("git status; rm -rf /"))

    assert capsys.readouterr().out == ""
    assert "rewrite" not in _read_last_observed(cursor_dir)


# ---------------------------------------------------------------------------
# postToolUse rewrite ledger
# ---------------------------------------------------------------------------


def _run_rewrite_pair(cursor_dir, monkeypatch, capsys, executed_command):
    """Drive preToolUse (stages the rewrite), then postToolUse as Cursor
    would: same tool_use_id, the command actually executed."""
    _fake_bash(monkeypatch, whitelisted=True)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "_COMPRESS_PATH", SCRIPTS / "bash_compress.py")
    pre = _shell_payload("git status")
    pre["hook_event_name"] = "preToolUse"
    pre["tool_use_id"] = "toolu-pair-1"
    bridge.handle_pre_tool_use(pre)
    emitted = json.loads(capsys.readouterr().out)
    rewritten = emitted["updated_input"]["command"]
    post = _shell_payload(rewritten if executed_command is None else executed_command)
    post["tool_use_id"] = "toolu-pair-1"
    bridge.handle_post_tool_use(post)
    return rewritten


@pytest.mark.skipif(os.name == "nt", reason="shell rewriting is disabled on native Windows")
def test_post_tool_use_records_rewrite_honoured(cursor_dir, monkeypatch, capsys):
    rewritten = _run_rewrite_pair(cursor_dir, monkeypatch, capsys, None)
    # honoured: the executed command is byte-identical to the staged rewrite.
    assert "bash_compress.py" in rewritten
    assert _read_last_observed(cursor_dir)["rewrite"] == "honoured"


@pytest.mark.skipif(os.name == "nt", reason="shell rewriting is disabled on native Windows")
def test_post_tool_use_records_rewrite_ignored(cursor_dir, monkeypatch, capsys):
    _run_rewrite_pair(cursor_dir, monkeypatch, capsys, "git status")
    # ignored: a rewrite was staged but the bare command actually ran.
    assert _read_last_observed(cursor_dir)["rewrite"] == "ignored"


def test_post_tool_use_no_attempt_is_unknown_not_proof(cursor_dir, monkeypatch):
    # G0: no staged rewrite for this tool_use_id -> NO classification; a bare
    # whitelisted command or an 'ls bash_compress.py' proves nothing.
    _fake_bash(monkeypatch, whitelisted=True)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    bridge.handle_post_tool_use(_shell_payload("git status"))
    assert "rewrite" not in _read_last_observed(cursor_dir)
    bridge.handle_post_tool_use(_shell_payload("ls bash_compress.py"))
    assert "rewrite" not in _read_last_observed(cursor_dir)


def test_post_tool_use_skips_non_whitelisted_rewrite_ledger(cursor_dir, monkeypatch):
    _fake_bash(monkeypatch, whitelisted=False)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    bridge.handle_post_tool_use(_shell_payload("rm -rf /"))
    assert "rewrite" not in _read_last_observed(cursor_dir)


def test_post_tool_use_nudges_on_crossing_threshold(cursor_dir, monkeypatch, capsys):
    sdir = _to_dir(cursor_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "composer-abc123.json").write_text(
        json.dumps({
            "conversation_id": "composer-abc123",
            "first_ts": time.time() - 900,
            "updated_at": time.time() - 5,
            "tool_calls": 30,
            "nudge_level": 0,
        }),
        encoding="utf-8",
    )
    _fake_bash(monkeypatch)
    bridge.handle_post_tool_use(_shell_payload("echo hi"))
    assert "additional_context" in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Restore context (per workspace)
# ---------------------------------------------------------------------------


def test_session_start_injects_matching_workspace_restore(cursor_dir, capsys):
    root = "/Users/alex/repos/demo"
    digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
    restore_dir = _to_dir(cursor_dir) / "restore-context"
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / f"{digest}.md").write_text(
        f"<!-- source-end: {time.time()} -->\n[Token Optimizer] Continuity from your previous Cursor session", encoding="utf-8"
    )

    bridge.handle_session_start({
        "hook_event_name": "sessionStart",
        "session_id": "composer-new",
        "workspace_roots": [root],
        "cursor_version": "3.18.9",
    })

    assert "additional_context" in json.loads(capsys.readouterr().out)


def test_session_start_skips_restore_when_sibling_active(cursor_dir, capsys):
    root = "/Users/alex/repos/demo"
    digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
    restore_dir = _to_dir(cursor_dir) / "restore-context"
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / f"{digest}.md").write_text(f"<!-- source-end: {time.time()} -->\nshould be skipped", encoding="utf-8")

    sdir = _to_dir(cursor_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "sibling.json").write_text(
        json.dumps({
            "conversation_id": "sibling",
            "updated_at": time.time(),
            "final": False,
            "workspace_roots": [root],
        }),
        encoding="utf-8",
    )

    bridge.handle_session_start({
        "hook_event_name": "sessionStart",
        "session_id": "composer-new",
        "workspace_roots": [root],
    })

    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# stop / sessionEnd spawns + throttle
# ---------------------------------------------------------------------------


def test_stop_throttles_rollup_and_dashboard(cursor_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached",
                        lambda argv, **k: calls.append(argv) or simple())
    payload = {"hook_event_name": "stop", "conversation_id": "composer-abc123"}

    bridge.handle_stop(payload)
    assert len(calls) == 2
    kind = [("cursor-rollup" in a or "dashboard" in a) for a in calls]
    assert kind == [True, True]

    # A second stop inside the 120s window must not spawn again.
    bridge.handle_stop(payload)
    assert len(calls) == 2


def test_stop_rollup_due_fails_closed_when_write_fails(cursor_dir, monkeypatch):
    # If the throttle-state write fails (disk full / read-only data dir), the
    # throttle must NOT open the floodgate: _stop_rollup_due returns False so
    # handle_stop spawns nothing, instead of spawning on every stop.
    monkeypatch.setattr(bridge, "_atomic_write_json", lambda path, obj: False)
    assert bridge._stop_rollup_due() is False


def test_locate_measure_py_resolves_via_locator(cursor_dir, monkeypatch, tmp_path):
    # After install, measure.py is NOT copied next to the bridge; a one-line
    # measure-path locator names the canonical measure.py in the checkout.
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    real_measure = tmp_path / "measure.py"
    real_measure.write_text("", encoding="utf-8")
    (plugin / "measure-path").write_text(f"{real_measure}\n", encoding="utf-8")

    monkeypatch.setattr(bridge, "_SCRIPT_DIR", plugin)
    monkeypatch.setattr(bridge, "_MEASURE_LOCATOR", plugin / "measure-path")

    assert bridge._locate_measure_py() == real_measure


def test_locate_measure_py_returns_none_without_measure(cursor_dir, monkeypatch, tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    monkeypatch.setattr(bridge, "_SCRIPT_DIR", plugin)
    monkeypatch.setattr(bridge, "_MEASURE_LOCATOR", plugin / "measure-path")

    assert bridge._locate_measure_py() is None


def test_session_end_spawns_unthrottled(cursor_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached",
                        lambda argv, **k: calls.append(argv) or simple())
    monkeypatch.setattr(bridge, "_stop_rollup_due", lambda: False)  # irrelevant to sessionEnd
    payload = {"hook_event_name": "sessionEnd", "conversation_id": "composer-abc123"}

    bridge.handle_session_end(payload)
    bridge.handle_session_end(payload)
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# observed-events ledger
# ---------------------------------------------------------------------------


def test_ledger_records_event_and_cursor_version(cursor_dir, monkeypatch):
    monkeypatch.setenv("CURSOR_VERSION", "3.18.9")
    bridge.handle_stop({"hook_event_name": "stop", "conversation_id": "composer-abc123"})

    entry = _read_last_observed(cursor_dir)
    assert entry["event"] == "stop"
    assert entry["cursor_version"] == "3.18.9"


@pytest.mark.parametrize("bad", [None, [1, 2, 3], "x", 42])
def test_pre_compact_survives_non_dict_payloads(bad, monkeypatch, tmp_path):
    """P1-2: handle_pre_compact was the one handler reading payload.get()
    directly; None/list/str/int payloads crashed with AttributeError and the
    compaction record was silently lost."""
    monkeypatch.setattr(bridge, "cursor_home", lambda: tmp_path)
    bridge.handle_pre_compact(bad)  # must not raise


def test_stop_rollup_throttle_skips_when_lease_held(monkeypatch, tmp_path):
    """P1-3: with the throttle lease held by a concurrent stop hook,
    _stop_rollup_due must return False (deterministic contention branch --
    the old unlocked code would have returned True and spawned a second
    rollup+dashboard pair)."""
    from hook_runtime import lease_lock
    monkeypatch.setattr(bridge, "cursor_home", lambda: tmp_path)
    lock_dir = tmp_path / "token-optimizer" / ".locks"
    lock_dir.mkdir(parents=True)
    with lease_lock(lock_dir / ".stop-rollup.lock", acquire_timeout=0.0,
                    lease_seconds=60.0) as acquired:
        assert acquired
        assert bridge._stop_rollup_due() is False
    # once the lease is released, the throttle proceeds
    assert bridge._stop_rollup_due() is True


def test_stop_rollup_throttle_is_atomic_under_concurrency(monkeypatch, tmp_path):
    """P1-3 (smoke): two concurrent stop hooks must yield at most one 'due'
    verdict. Scheduler-dependent, so the deterministic pin lives in
    test_stop_rollup_throttle_skips_when_lease_held."""
    import threading
    monkeypatch.setattr(bridge, "cursor_home", lambda: tmp_path)
    results = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(bridge._stop_rollup_due())

    t1, t2 = threading.Thread(target=run), threading.Thread(target=run)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results.count(True) <= 1, results


def test_post_tool_use_nudge_in_single_locked_write(monkeypatch, tmp_path, capsys):
    """P1-4: the nudge_level update is folded into the tally's one locked RMW;
    a single postToolUse that crosses a threshold bumps tool_calls AND
    nudge_level in the same file write and emits the nudge once."""
    monkeypatch.setattr(bridge, "cursor_home", lambda: tmp_path)
    payload = {"hook_event_name": "postToolUse", "tool_name": "Shell",
               "tool_input": {"command": "ls"},
               "conversation_id": "nudge-conv", "cursor_version": "3.18.9"}
    for _ in range(bridge._NUDGE_TOOL_CALLS[0]):
        bridge.handle_post_tool_use(payload)
    tally = bridge._load_tally(bridge._tally_path({"conversation_id": "nudge-conv"}))
    assert tally["tool_calls"] == bridge._NUDGE_TOOL_CALLS[0]
    assert tally["nudge_level"] >= 1
    assert "_nudge_emitted" not in tally  # consumed by the handler, never persisted
    out = capsys.readouterr().out
    assert out.count("additional_context") == 1


def test_main_logs_handler_failure_but_exits_zero(monkeypatch, caplog):
    """P1-10: a handler crash must still exit 0 (never break Cursor) but can
    no longer vanish silently -- it logs at ERROR with the event name."""
    import logging
    monkeypatch.setattr(bridge, "_read_stdin_payload", lambda: {})
    def boom(_payload):
        raise RuntimeError("disk exploded")
    monkeypatch.setitem(bridge._HANDLERS, "stop", boom)
    with caplog.at_level(logging.ERROR, logger="cursor_hook_bridge"):
        rc = bridge.main(["stop"])
    assert rc == 0
    assert any("stop" in r.message and r.levelno == logging.ERROR for r in caplog.records)


def test_observed_append_failure_warns_once(monkeypatch, tmp_path, caplog):
    """P1-10 follow-up: a persistently unwritable data dir logs ONE warning
    (first failure) instead of a traceback per hook event."""
    import logging

    ro = tmp_path / "ro-dir"
    ro.mkdir()
    monkeypatch.setattr(bridge, "_observed_warned", False)
    monkeypatch.setattr(bridge, "_to_dir", lambda: ro)
    def denied(*args, **kwargs):
        raise PermissionError("ledger unavailable")
    monkeypatch.setattr(bridge.os, "open", denied)
    with caplog.at_level(logging.WARNING, logger="cursor_hook_bridge"):
        bridge._record_observed("stop")
        bridge._record_observed("stop")
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1


def test_pre_compact_persists_real_numbers_from_dict_payload(monkeypatch, tmp_path):
    """P1-2: a well-formed preCompact payload's real context numbers land in
    the compaction record; garbage values ('abc') degrade to 0, not a crash."""
    monkeypatch.setattr(bridge, "cursor_home", lambda: tmp_path)
    bridge.handle_pre_compact({
        "hook_event_name": "preCompact", "trigger": "manual",
        "context_tokens": 1234, "context_window_size": 128000,
        "context_usage_percent": 0.42, "conversation_id": "pc-conv",
    })
    tally = bridge._load_tally(bridge._tally_path({"conversation_id": "pc-conv"}))
    comp = tally["compactions"][-1]
    assert comp["context_tokens"] == 1234
    assert comp["context_window_size"] == 128000
    assert abs(comp["context_usage_percent"] - 0.42) < 1e-9
    assert comp["trigger"] == "manual"

    bridge.handle_pre_compact({
        "hook_event_name": "preCompact", "trigger": "auto",
        "context_tokens": "abc", "conversation_id": "pc-conv",
    })
    tally = bridge._load_tally(bridge._tally_path({"conversation_id": "pc-conv"}))
    comp = tally["compactions"][-1]
    assert comp["context_tokens"] == 0
    assert comp["context_window_size"] == 0


def test_after_shell_filename_does_not_claim_rewrite(cursor_dir):
    bridge.handle_after_shell_execution({"conversation_id": "composer-123", "command": "cat bash_compress.py"})
    assert "rewrite" not in _read_last_observed(cursor_dir)


def test_restore_rejects_stale_source_and_avoids_old_tally_reads(cursor_dir, monkeypatch):
    root = "/workspace/example"
    digest = hashlib.sha1(root.encode()).hexdigest()
    folder = _to_dir(cursor_dir) / "restore-context"
    folder.mkdir(parents=True)
    restore = folder / f"{digest}.md"
    restore.write_text(f"<!-- source-end: {time.time() - 8 * 86400} -->\nstale")
    fields = {"workspace_roots": [root], "conversation_id": "current"}
    assert bridge._restore_for_workspace(fields) is None
    restore.write_text(f"<!-- source-end: {time.time()} -->\nrecent")
    sessions = _to_dir(cursor_dir) / "sessions"
    sessions.mkdir(exist_ok=True)
    old = sessions / "old.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    monkeypatch.setattr(bridge, "_load_tally", lambda path: pytest.fail("old tally read"))
    assert bridge._restore_for_workspace(fields) == "recent"
