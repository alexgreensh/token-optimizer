"""U5: thrash-guard parity on Cursor via postToolUse Shell tracking.

Contracts guarded here:
1. Three identical-output runs of one command produce exactly one nudge;
   a fourth run inside the cooldown produces none.
2. Three differently-failing runs of one command produce the burn nudge.
3. Evolving output resets the streak; a stale streak (STALE_SECONDS)
   expires instead of firing.
4. Integration through the bridge: the nudge text rides the emitted
   ``additional_context`` and is named in the observed ledger.
5. Fail-open: missing command/output/session, or a broken store, emits
   nothing and never raises.
6. Env hygiene: the adapter's CLAUDE_SESSION_ID / TOKEN_OPTIMIZER_SNAPSHOT_DIR
   shims are restored after the call, and streak state lands under the
   Cursor home - never a Claude plugin-data dir.
"""
import importlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _purge():
    for m in ("cursor_thrash", "thrash_guard", "session_store", "delta_diff",
              "archive_result", "runtime_env", "plugin_env"):
        sys.modules.pop(m, None)


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # the override is honored only under $HOME
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    (tmp_path / "cursor-home").mkdir()  # the safe-dir guard rejects missing dirs
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(tmp_path / "cursor-home"))
    monkeypatch.delenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    _purge()
    mod = importlib.import_module("cursor_thrash")
    yield mod, tmp_path
    _purge()


def _fields(command, output, sid=None):
    return {
        "tool_name": "Shell",
        "tool_args": {"command": command},
        "tool_output": output,
        "conversation_id": sid or ("conv-" + uuid.uuid4().hex[:10]),
    }


def _run(mod, fields, now):
    return mod.shell_thrash_nudge(fields, now=now)


def test_identical_output_streak_nudges_once(adapter):
    mod, _ = adapter
    f = _fields("ls -la", "file_a\nfile_b\n")
    t0 = 1_000_000.0
    assert _run(mod, f, t0) is None
    assert _run(mod, f, t0 + 1) is None
    nudge = _run(mod, f, t0 + 2)
    assert nudge is not None and "3 times" in nudge
    # Fourth run inside the cooldown (REPEAT_AFTER) stays silent.
    assert _run(mod, f, t0 + 3) is None


def test_burn_streak_nudges(adapter):
    mod, _ = adapter
    cmd = "gcc -o image image.c -lm && ./image"
    t0 = 2_000_000.0
    f1 = _fields(cmd, "Error: undefined symbol foo\nline 1\n")
    f2 = _fields(cmd, "Error: type mismatch bar\nline 2\n")
    f3 = _fields(cmd, "Error: syntax error baz\nline 3\n")
    sid = f1["conversation_id"]
    f2["conversation_id"] = f3["conversation_id"] = sid
    assert _run(mod, f1, t0) is None
    assert _run(mod, f2, t0 + 1) is None
    nudge = _run(mod, f3, t0 + 2)
    assert nudge is not None and "failed" in nudge and "3 times" in nudge


def test_evolving_output_resets_streak(adapter):
    mod, _ = adapter
    t0 = 3_000_000.0
    sid = "conv-" + uuid.uuid4().hex[:10]
    _run(mod, _fields("git status", "on branch main, 1 change", sid), t0)
    _run(mod, _fields("git status", "on branch main, 1 change", sid), t0 + 1)
    # Material change: streak resets, so the next two identical runs must not fire.
    _run(mod, _fields("git status", "on branch main, 2 changes", sid), t0 + 2)
    assert _run(mod, _fields("git status", "on branch main, 2 changes", sid), t0 + 3) is None


def test_stale_streak_expires(adapter):
    mod, _ = adapter
    f = _fields("ls -la", "file_a\nfile_b\n")
    t0 = 4_000_000.0
    _run(mod, f, t0)
    _run(mod, f, t0 + 1)
    # 3rd run arrives after STALE_SECONDS (1800): the streak reset, no nudge.
    assert _run(mod, f, t0 + 1900) is None


def test_fail_open_on_missing_inputs(adapter):
    mod, _ = adapter
    assert mod.shell_thrash_nudge({"tool_name": "Shell", "tool_args": {}, "tool_output": "x", "conversation_id": "conv-abcdef"}) is None
    assert mod.shell_thrash_nudge({"tool_name": "Shell", "tool_args": {"command": "ls"}, "tool_output": "", "conversation_id": "conv-abcdef"}) is None
    assert mod.shell_thrash_nudge({"tool_name": "Shell", "tool_args": {"command": "ls"}, "tool_output": "x", "conversation_id": ""}) is None


def test_env_restored_and_state_under_cursor_home(adapter, monkeypatch):
    mod, tmp_path = adapter
    monkeypatch.setenv("CLAUDE_SESSION_ID", "pre-existing-claude-session")
    f = _fields("ls -la", "file_a\nfile_b\n")
    _run(mod, f, 5_000_000.0)
    assert os.environ.get("CLAUDE_SESSION_ID") == "pre-existing-claude-session"
    assert "TOKEN_OPTIMIZER_SNAPSHOT_DIR" not in os.environ
    store_root = tmp_path / "cursor-home" / "token-optimizer" / "snapshot" / "session-store"
    assert store_root.is_dir(), "streak store must land under the Cursor home"
    assert f["conversation_id"] in next(store_root.glob("*.db")).name


def test_bridge_emits_thrash_nudge_and_ledgers(tmp_path, monkeypatch, capsys):
    """Integration: postToolUse Shell through the real bridge handler."""
    monkeypatch.setenv("HOME", str(tmp_path))  # the override is honored only under $HOME
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    (tmp_path / "cursor-home").mkdir()  # the safe-dir guard rejects missing dirs
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(tmp_path / "cursor-home"))
    monkeypatch.delenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", raising=False)
    _purge()
    for m in ("cursor_hook_bridge", "cursor_state"):
        sys.modules.pop(m, None)
    bridge = importlib.import_module("cursor_hook_bridge")
    importlib.reload(bridge)

    sid = "conv-" + uuid.uuid4().hex[:10]

    def payload(cmd_out):
        return {
            "hook_event_name": "postToolUse",
            "conversation_id": sid,
            "tool_name": "Shell",
            "tool_input": {"command": "ls -la"},
            "tool_output": cmd_out,
        }

    bridge.handle_post_tool_use(payload("file_a\nfile_b\n"))
    bridge.handle_post_tool_use(payload("file_a\nfile_b\n"))
    capsys.readouterr()  # drop earlier emissions
    bridge.handle_post_tool_use(payload("file_a\nfile_b\n"))
    out = capsys.readouterr().out
    emitted = [json.loads(line) for line in out.splitlines() if line.strip()]
    contexts = [e["additional_context"] for e in emitted if "additional_context" in e]
    assert contexts, "bridge must emit additional_context on the third identical run"
    assert any("3 times" in c for c in contexts)

    ledger = tmp_path / "cursor-home" / "token-optimizer" / "observed-events.jsonl"
    assert ledger.exists()
    kinds = [json.loads(l).get("event") for l in ledger.read_text().splitlines() if l.strip()]
    assert "thrashNudge" in kinds
    _purge()
