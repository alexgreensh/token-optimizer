#!/usr/bin/env python3
"""U2: Cursor read-path parity (cursor_read_cache + bridge wiring).

Contracts guarded here:
1. Shadow default: a second identical Read records the decision and emits
   nothing; first reads are never intercepted; no config file means shadow.
2. soft_block (explicit opt-in): a second unchanged Read of a supported
   code file emits deny + user_message with a bounded structure map; a
   changed file emits deny + delta text, not a full map.
3. First-ever read is never denied; non-code files are never denied;
   partial reads (offset/limit) are never intercepted; a file last read in
   a different session is a first read (no cross-session delta base).
4. KTD9 coexistence: Claude TO read hooks in Claude settings + no explicit
   mode -> record-only (shadow) even though the decision would fire.
5. Fail open: malformed payloads, missing files, unknown tool names emit
   nothing and never raise.
6. Installer wires preToolUse with the Shell|Read matcher, preserving
   other tools' entries.
7. Doctor channel state ladder: unknown -> shadow -> armed-unverified ->
   armed-verified, with adaptation evidence required for verified.

Run: python3 -m pytest tests/test_cursor_read_cache.py -v
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_hook_bridge as bridge  # noqa: E402
import cursor_read_cache as rc  # noqa: E402

CODE = '''"""Demo module."""


def alpha(x):
    return x + 1


def beta(y):
    return alpha(y) * 2


class Greeter:
    def hello(self, name):
        return f"hello {name}"
'''

CODE_V2 = CODE + "\n\ndef gamma(z):\n    return beta(z) - 1\n"


@pytest.fixture()
def env(monkeypatch, tmp_path, capsys):
    cur = tmp_path / ".cursor"
    cur.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    monkeypatch.setattr(rc, "_home", lambda: cur)
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "demo.py"
    target.write_text(CODE, encoding="utf-8")
    # No Claude TO hooks anywhere in this sandbox's HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(rc.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    yield cur, repo, target, capsys


def _fields(target: Path, sid: str = "conv-1", extra=None) -> dict:
    args = {"file_path": str(target)}
    if extra:
        args.update(extra)
    return {
        "tool_name": "Read",
        "tool_args": args,
        "conversation_id": sid,
        "workspace_roots": [str(target.parent)],
    }


def _arm(cur: Path, mode: str = "soft_block"):
    d = cur / "token-optimizer"
    d.mkdir(parents=True, exist_ok=True)
    (d / "read-cache.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")


def test_shadow_default_records_and_emits_nothing(env):
    cur, _, target, _ = env
    assert rc.configured_mode() == "shadow"
    first = rc.evaluate(_fields(target))
    assert first["emit"] is None and first["observed"]["decision"] == "first_read"
    second = rc.evaluate(_fields(target))
    assert second["emit"] is None
    assert second["observed"]["decision"] == "shadow_redundant"


def test_soft_block_second_read_denies_with_structure_map(env):
    cur, _, target, _ = env
    _arm(cur)
    rc.evaluate(_fields(target))
    decision = rc.evaluate(_fields(target))
    assert decision["emit"]["permission"] == "deny"
    msg = decision["emit"]["user_message"]
    assert "unchanged" in msg
    assert "alpha" in msg  # structure map content
    assert len(msg) < 4000  # bounded
    assert decision["observed"]["decision"] == "deny_inject"
    assert "agent_message" not in decision["emit"]  # dropped by Cursor (known bug)


def test_soft_block_changed_file_denies_with_delta(env):
    cur, _, target, _ = env
    _arm(cur)
    rc.evaluate(_fields(target))
    target.write_text(CODE_V2, encoding="utf-8")
    decision = rc.evaluate(_fields(target))
    assert decision["emit"]["permission"] == "deny"
    msg = decision["emit"]["user_message"]
    assert "changed since" in msg
    assert "gamma" in msg
    assert decision["observed"]["decision"] == "deny_delta"


def test_first_read_and_partial_reads_never_denied(env):
    cur, _, target, _ = env
    _arm(cur, "block")  # even the harshest mode
    assert rc.evaluate(_fields(target))["emit"] is None
    rc.evaluate(_fields(target))
    partial = rc.evaluate(_fields(target, extra={"offset": 10}))
    assert partial is None
    limited = rc.evaluate(_fields(target, extra={"limit": 50}))
    assert limited is None


def test_non_code_file_never_intercepted(env):
    cur, repo, _, _ = env
    _arm(cur, "block")
    text_file = repo / "notes.txt"
    text_file.write_text("plain prose\n" * 50, encoding="utf-8")
    assert rc.evaluate(_fields(text_file)) is None


def test_cross_session_is_first_read(env):
    cur, _, target, _ = env
    _arm(cur, "block")
    rc.evaluate(_fields(target, sid="conv-A"))
    other = rc.evaluate(_fields(target, sid="conv-B"))
    assert other["emit"] is None
    assert other["observed"]["decision"] == "first_read"


def test_ktd9_claude_hooks_force_record_only(env, monkeypatch, tmp_path):
    cur, _, target, _ = env
    # Claude settings with a TO read-cache hook, no explicit Cursor mode.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Read",
                                  "hooks": [{"command": "python3 /x/token-optimizer/read_cache.py"}]}]}
    }), encoding="utf-8")
    rc.evaluate(_fields(target))
    decision = rc.evaluate(_fields(target))
    assert decision["emit"] is None
    assert decision["observed"]["owner"] == "claude-import"
    assert decision["observed"]["decision"] == "shadow_redundant"
    # explicit mode overrides the defer (manual opt-in wins)
    _arm(cur)
    armed = rc.evaluate(_fields(target))
    assert armed["emit"]["permission"] == "deny"


def test_fail_open(env):
    cur, repo, target, _ = env
    _arm(cur, "block")
    assert rc.evaluate({"tool_name": "Read", "tool_args": "not-a-dict",
                        "conversation_id": "c"}) is None
    assert rc.evaluate({"tool_name": "Edit", "tool_args": {"file_path": str(target)},
                        "conversation_id": "c"}) is None
    missing = repo / "gone.py"
    assert rc.evaluate(_fields(missing)) is None
    assert rc.evaluate({"tool_name": "Read", "tool_args": {"file_path": ""},
                        "conversation_id": "c"}) is None


def _bridge_payload(target: Path, sid: str = "conv-1") -> dict:
    """A real bridge payload: tool_input, not the decoded-fields shape."""
    f = _fields(target, sid)
    return {"hook_event_name": "preToolUse", "tool_name": f["tool_name"],
            "tool_input": f["tool_args"], "conversation_id": sid,
            "workspace_roots": f["workspace_roots"]}


def test_bridge_emits_deny_on_armed_redundant_read(env):
    cur, _, target, capsys = env
    _arm(cur)
    bridge.handle_pre_tool_use(_bridge_payload(target))
    capsys.readouterr()
    bridge.handle_pre_tool_use(_bridge_payload(target))
    out = capsys.readouterr().out
    emitted = [json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    assert any(e.get("permission") == "deny" and "unchanged" in e.get("user_message", "")
               for e in emitted)
    # decision ledgered
    ledger = (cur / "token-optimizer" / "observed-events.jsonl").read_text().splitlines()
    events = [json.loads(l) for l in ledger]
    assert any(e["event"] == "readCache" and e.get("decision") == "deny_inject"
               for e in events)


def test_bridge_shadow_read_emits_nothing_but_records(env):
    cur, _, target, capsys = env
    for _ in range(2):
        bridge.handle_pre_tool_use(_bridge_payload(target))
    assert capsys.readouterr().out.strip() == ""
    ledger = (cur / "token-optimizer" / "observed-events.jsonl").read_text().splitlines()
    decisions = [json.loads(l).get("decision") for l in ledger
                 if json.loads(l)["event"] == "readCache"]
    assert decisions == ["first_read", "shadow_redundant"]


class TestChannelState:
    def test_ladder(self, env):
        cur, _, target, _ = env
        # unknown: no config, no events
        assert rc.channel_state([])["state"] == "unknown"
        # shadow: mode shadow with events
        events = [{"event": "readCache", "decision": "shadow_redundant", "path": "demo.py"}]
        assert rc.channel_state(events)["state"] == "shadow"
        # armed-unverified: deny with no later activity
        _arm(cur)
        events = [{"event": "readCache", "decision": "deny_inject", "path": "demo.py"}]
        assert rc.channel_state(events)["state"] == "armed-unverified"
        # absence is never proof: a denied file missing from later
        # postToolUse events stays unverified even while other tools run
        events.append({"event": "postToolUse", "tool_name": "Shell"})
        assert rc.channel_state(events)["state"] == "armed-unverified"
        # and a reread of the denied file is likewise just unverified
        events.append({"event": "postToolUse", "tool_name": "Read", "path": "demo.py"})
        assert rc.channel_state(events)["state"] == "armed-unverified"


def test_installer_wires_read_matcher():
    sys.path.insert(0, str(SCRIPTS))
    import cursor_install

    entries = cursor_install._hook_entries(Path("/tmp/bridge/cursor_hook_bridge.py"))
    pre = entries["preToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "Shell|Read"


def test_store_permissions(env):
    cur, _, target, _ = env
    rc.evaluate(_fields(target))
    store = cur / "token-optimizer" / "read-fingerprints" / "conv-1.json"
    import stat as stat_module

    assert stat_module.S_IMODE(store.stat().st_mode) == 0o600
