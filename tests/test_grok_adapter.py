#!/usr/bin/env python3
"""Grok Build adapter unit tests (state reader, session normalizer, bridge, installer).

Contract-only beta: every fixture mirrors the documented Grok Build shape from
the cloned ``xai-org/grok-build`` source (10-hooks.md, session persistence/
signals/notification structs) — the only source of truth available in
NO-INSTALL mode. The remaining live-verification gaps are tracked in
G-STATUS.md "Needs live verification".

Run: python3 -m pytest tests/test_grok_adapter.py -v
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


# The fixtures below pop and re-import script modules (grok_hook_bridge,
# grok_install, grok_doctor). Without this, a re-imported runtime_env stays in
# sys.modules while later test FILES (e.g. test_runtime_env_wsl_mnt) still hold
# the instance bound at collection time, so the later test patches one instance
# while measure.py dynamically imports the replacement — the same duplicate
# module leak #107's test files already defend against.
@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = sys.modules.copy()
    yield
    for k in list(sys.modules):
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


# ---------------------------------------------------------------------------
# grok_state: bounded, read-only session-store reader
# ---------------------------------------------------------------------------


def _turn_completed(*, input_tokens=0, output_tokens=0, cached_read=0,
                    cache_create=0, reasoning=0, model_calls=1, cost_ticks=0,
                    incomplete=False, partial=False):
    return {
        "update": {
            "sessionUpdate": "turn_completed",
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cachedReadTokens": cached_read,
                "cacheCreationTokens": cache_create,
                "reasoningTokens": reasoning,
                "modelCalls": model_calls,
                "costUsdTicks": cost_ticks,
                "usageIsIncomplete": incomplete,
                "costIsPartial": partial,
                "modelUsage": {
                    "grok-4": {
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cacheReadInputTokens": cached_read,
                        "cacheCreationInputTokens": cache_create,
                        "modelCalls": model_calls,
                    }
                },
            },
        }
    }


def _write_session(tmp_path, sid="sess-1", *, cwd="/work/example",
                   turns=None, signals=None):
    home = tmp_path / "grok-home"
    session_dir = home / "sessions" / "group-a" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text(json.dumps({
        "info": {"id": sid, "cwd": cwd},
        "sessionSummary": "A Grok session",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:10:00Z",
        "numMessages": 12,
        "numChatMessages": 11,
        "currentModelId": "grok-4",
        "agentName": "grok",
    }), encoding="utf-8")
    if turns is not None:
        (session_dir / "updates.jsonl").write_text(
            "\n".join(json.dumps(t) for t in turns) + "\n",
            encoding="utf-8",
        )
    if signals is not None:
        (session_dir / "signals.json").write_text(json.dumps(signals), encoding="utf-8")
    # A stray file inside the group dir must be ignored (only dirs are sessions).
    (session_dir.parent / "not-a-session.txt").write_text("x", encoding="utf-8")
    return session_dir


def test_find_session_dirs_discovers_uuid_dirs(tmp_path):
    import grok_state as gs

    session_dir = _write_session(tmp_path)
    # second session in a second group proves breadth, not just depth
    _write_session(tmp_path, sid="sess-2", cwd="/work/other")
    dirs = gs.find_session_dirs(home=tmp_path / "grok-home")
    assert sorted(d.name for d in dirs) == ["sess-1", "sess-2"]
    assert all(d.parent.parent == tmp_path / "grok-home" / "sessions" for d in dirs)


def test_read_usage_totals_sums_turn_completed_and_scrubs_cost(tmp_path):
    import grok_state as gs

    turns = [
        # Trustworthy cost: the only record whose ticks count.
        _turn_completed(input_tokens=10, output_tokens=5, cached_read=3,
                        cache_create=1, reasoning=2, model_calls=2, cost_ticks=100),
        # costIsPartial -> cost scrubbed (still tokens count).
        _turn_completed(input_tokens=20, output_tokens=6, cached_read=4,
                        cache_create=1, reasoning=1, model_calls=1,
                        cost_ticks=200, partial=True),
        # usageIsIncomplete -> cost scrubbed AND usage_incomplete flag set.
        _turn_completed(input_tokens=30, output_tokens=7, cached_read=5,
                        cache_create=1, reasoning=1, model_calls=1,
                        cost_ticks=300, incomplete=True),
        # A non-turn_completed update must contribute NOTHING.
        {"update": {"sessionUpdate": "compaction_done",
                    "usage": {"inputTokens": 999, "costUsdTicks": 9}},
        },
    ]
    session_dir = _write_session(tmp_path, turns=turns)
    totals = gs.read_usage_totals(session_dir)

    assert totals["turns"] == 3
    assert totals["input_tokens"] == 60
    assert totals["output_tokens"] == 18
    assert totals["cache_read_tokens"] == 12
    assert totals["cache_create_tokens"] == 3
    assert totals["reasoning_tokens"] == 4
    assert totals["model_calls"] == 4
    assert totals["cost_usd_ticks"] == 100, "partial + incomplete costs must be scrubbed"
    assert totals["usage_incomplete"] is True
    assert totals["model_usage"]["grok-4"] == {
        "input_tokens": 60,
        "output_tokens": 18,
        "cache_read_tokens": 12,
        "cache_create_tokens": 3,
        "model_calls": 4,
    }


def test_read_usage_totals_missing_updates_yields_zeros(tmp_path):
    import grok_state as gs

    session_dir = _write_session(tmp_path)
    totals = gs.read_usage_totals(session_dir)
    assert totals["turns"] == 0
    assert totals["input_tokens"] == 0
    assert totals["cost_usd_ticks"] == 0
    assert totals["model_usage"] == {}


def test_read_session_combines_identity_signals_usage(tmp_path):
    import grok_state as gs

    turns = [_turn_completed(input_tokens=10, output_tokens=5, cached_read=3,
                             cache_create=1, cost_ticks=100)]
    signals = {
        "turnCount": 3,
        "toolCallCount": 4,
        "contextTokensUsed": 500,
        "contextWindowTokens": 256000,
        "primaryModelId": "grok-4",
        "sessionDurationSeconds": 600,
    }
    session_dir = _write_session(tmp_path, cwd="/work/example",
                                 turns=turns, signals=signals)
    raw = gs.read_session(session_dir)

    assert raw["session_id"] == "sess-1"
    assert raw["cwd"] == "/work/example"
    assert raw["title"] == "A Grok session"
    assert raw["num_messages"] == 12
    assert raw["model_id"] == "grok-4"
    assert raw["signals"]["toolCallCount"] == 4
    assert raw["usage"]["input_tokens"] == 10
    assert raw["data_source"] == "grok_session_store"


# ---------------------------------------------------------------------------
# grok_session: canonical normalizer
# ---------------------------------------------------------------------------


def _normalized(tmp_path, *, turns=None, signals=None):
    import grok_session
    import grok_state as gs

    session_dir = _write_session(tmp_path, turns=turns, signals=signals)
    return grok_session.normalize_session(gs.read_session(session_dir))


def test_normalize_session_uses_authoritative_updates(tmp_path):
    s = _normalized(tmp_path, turns=[
        _turn_completed(input_tokens=10, output_tokens=5, cached_read=3,
                        cache_create=1, reasoning=2, model_calls=2, cost_ticks=100),
    ])
    assert s["token_source"] == "grok_updates_jsonl"
    assert s["total_input_tokens"] == 10
    assert s["total_output_tokens"] == 5
    assert s["total_cache_read"] == 3
    assert s["total_cache_create"] == 1
    assert s["cost_usd"] == round(100 / 10_000_000_000, 6)
    assert s["cost_source"] == "grok_cost_usd_ticks"
    assert s["runtime"] == "grok"
    assert s["model"] == "grok-4"
    assert s["estimated"] is False
    assert s["incomplete"] is False


def test_normalize_session_falls_back_to_signals_estimate(tmp_path):
    s = _normalized(tmp_path, signals={
        "contextTokensUsed": 500,
        "toolCallCount": 2,
        "sessionDurationSeconds": 120,
    })
    assert s["token_source"] == "grok_signals_only"
    assert s["estimated"] is True
    assert s["total_input_tokens"] == 500
    assert s["total_output_tokens"] == 0
    assert s["cost_source"] == "grok_no_cost_data"
    assert s["cost_usd"] == 0.0


def test_normalize_session_scrubs_incomplete_cost(tmp_path):
    s = _normalized(tmp_path, turns=[
        _turn_completed(input_tokens=30, output_tokens=7, cost_ticks=300,
                        incomplete=True),
    ])
    assert s["incomplete"] is True
    assert s["estimated"] is True  # an incomplete bill under-counts
    assert s["end_reason"] == "usage_incomplete"
    assert s["cost_source"] == "grok_no_cost_data"
    assert s["total_input_tokens"] == 30  # tokens still recorded


# ---------------------------------------------------------------------------
# grok_hook_bridge: hook output contract + fail-open
# ---------------------------------------------------------------------------


@pytest.fixture()
def gb(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    for _mod in ("grok_hook_bridge", "runtime_env", "spawn_utils"):
        sys.modules.pop(_mod, None)
    import grok_hook_bridge as g

    # Never write to the real ~/.grok during unit tests.
    monkeypatch.setattr(g, "_to_dir", lambda: None)
    return g


def test_decode_payload_prefers_pascal_case_event(gb):
    p = gb.decode_payload({
        "hook_event_name": "PreToolUse",
        "hookEventName": "pre_tool_use",
        "sessionId": "session-abc-123",
        "cwd": "/work/example",
        "toolName": "Bash",
        "toolInput": '{"command": "ls -la"}',
        "promptId": "p-1",
        "permissionMode": "default",
    })
    assert p["event_name"] == "PreToolUse"
    assert p["session_id"] == "session-abc-123"
    assert p["tool_name"] == "Bash"
    assert p["tool_args"] == {"command": "ls -la"}
    assert p["cwd"] == "/work/example"
    assert p["prompt_id"] == "p-1"


def test_sanitize_session_id(gb):
    assert gb._sanitize_session_id("session-abc_123") == "session-abc_123"
    assert gb._sanitize_session_id("../../../etc/passwd") == "etcpasswd"
    assert gb._sanitize_session_id("a/b c:") == "unknown"


def test_pre_tool_use_emits_rewrite_contract(gb, monkeypatch, capsys):
    monkeypatch.setattr(gb, "_compression_enabled", lambda: True)
    monkeypatch.setattr(
        gb, "_bash_hook",
        types.SimpleNamespace(_has_dangerous_chars=lambda c: False,
                              _is_whitelisted=lambda c: True),
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "hookEventName": "pre_tool_use",
        "sessionId": "session-abc-123",
        "cwd": "/work/example",
        "toolName": "run_terminal_command",
        "toolInput": {"command": "git status --short", "workdir": "/work/example"},
    }
    gb.handle_pre_tool_use(payload)
    out = capsys.readouterr().out.strip()
    assert out
    obj = json.loads(out)

    block = obj["hookSpecificOutput"]
    assert block["hookEventName"] == "PreToolUse"
    # Omitting `decision` = allow + apply the rewrite (10-hooks.md).
    assert "decision" not in block
    assert "permissionDecision" not in block
    updated = block["updatedInput"]
    assert updated["workdir"] == "/work/example"  # non-command fields survive
    # The bare command is wrapped, not left verbatim.
    assert updated["command"] != "git status --short"
    assert updated["command"].startswith(
        shlex.quote(sys.executable) + " "
        + shlex.quote(str(gb._COMPRESS_PATH)) + " "
    )
    assert "deny" not in out and "block" not in out


def test_handlers_fail_open_on_malformed_payload(gb, monkeypatch, capsys):
    # None / empty dict must never raise, and must never emit a deny/block.
    for handler in (gb.handle_session_start, gb.handle_user_prompt_submit,
                    gb.handle_pre_tool_use, gb.handle_post_tool_use):
        handler(None)
    monkeypatch.setattr(gb, "_stop_rollup_due", lambda: False)
    gb.handle_stop(None)
    out = capsys.readouterr().out
    assert "deny" not in out and "block" not in out


def test_main_fails_open_when_handler_raises(gb, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "grok")
    monkeypatch.setattr(gb, "_read_stdin_payload", lambda: {})
    monkeypatch.setattr(gb.logger, "exception", lambda *a, **k: None)

    def boom(payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(gb._HANDLERS, "PreToolUse", boom)
    assert gb.main(["PreToolUse"]) == 0


def test_main_unknown_event_returns_zero(gb):
    assert gb.main(["NotAWiredEvent"]) == 0
    assert gb.main([]) == 0


# ---------------------------------------------------------------------------
# grok_install: Cursor-hardened trust gate + hooks file ownership
# ---------------------------------------------------------------------------


@pytest.fixture()
def gi(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("grok_install", None)
    monkeypatch.delenv("TOKEN_OPTIMIZER_PYTHON", raising=False)
    import grok_install as g
    return g


def test_resolver_returns_absolute_existing_file(gi):
    r = gi._resolve_safe_python()
    assert os.path.isabs(r), f"not absolute: {r}"
    assert os.path.isfile(r), f"not a real file: {r}"


def test_hook_payload_bakes_absolute_python(gi, tmp_path):
    resolved = gi._resolve_safe_python()
    payload = gi._hooks_payload(tmp_path / "grok_hook_bridge.py")
    cmd = payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert resolved in cmd, f"resolved path not embedded in hook command: {cmd}"
    assert " python3 " not in f" {cmd} " and " python " not in f" {cmd} ", cmd


def test_override_env_is_honored_when_trusted(gi, monkeypatch):
    resolved = gi._resolve_safe_python()
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", resolved)
    assert gi._resolve_safe_python() == os.path.abspath(resolved)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "/nonexistent/python3")
    assert os.path.isfile(gi._resolve_safe_python())


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_rejects_hijackable_paths(gi):
    # world-writable DIR -> anyone can swap the file
    d = tempfile.mkdtemp()
    os.chmod(d, 0o777)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert gi._py_path_is_trusted(f) is False

    # world-writable FILE in an owned dir -> anyone can rewrite its bytes
    d2 = tempfile.mkdtemp()
    os.chmod(d2, 0o755)
    f2 = os.path.join(d2, "python3")
    open(f2, "w").close()
    os.chmod(f2, 0o777)
    assert gi._py_path_is_trusted(f2) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_accepts_owned_unwritable_file(gi):
    d = tempfile.mkdtemp()
    os.chmod(d, 0o755)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert gi._py_path_is_trusted(f) is True


def test_install_writes_our_five_event_hooks_file(gi, tmp_path):
    home = tmp_path / "grok-home"
    result = gi.install(home=home)

    hook_file = Path(result["hook_file"])
    assert hook_file.is_file()
    data = json.loads(hook_file.read_text(encoding="utf-8"))
    assert set(data["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"
    }
    pretool = data["hooks"]["PreToolUse"][0]
    assert pretool.get("matcher") == "Bash"
    cmd = pretool["hooks"][0]["command"]
    assert "grok_hook_bridge.py" in cmd and " PreToolUse" in cmd
    # Idempotency contract: our payload dir has the bridge + measure locator.
    assert (home / "token-optimizer" / "plugin" / "grok_hook_bridge.py").is_file()
    assert (home / "token-optimizer" / "plugin" / "measure-path").is_file()


def test_uninstall_removes_only_our_files(gi, tmp_path):
    home = tmp_path / "grok-home"
    gi.install(home=home)
    foreign = home / "hooks" / "figma.json"
    foreign.write_text("{}", encoding="utf-8")

    gi.uninstall(home=home)

    assert not (home / "hooks" / "token-optimizer.json").exists()
    assert foreign.exists()
    assert not (home / "token-optimizer" / "plugin").exists()


def test_install_refuses_hooks_file_symlink(gi, tmp_path):
    home = tmp_path / "grok-home"
    (home / "hooks").mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    (home / "hooks" / "token-optimizer.json").symlink_to(target)

    with pytest.raises(RuntimeError):
        gi.install(home=home)

    assert target.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# grok_doctor: --probe never runs a corrupted/injected command
# ---------------------------------------------------------------------------


@pytest.fixture()
def gd(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("grok_doctor", None)
    import grok_doctor as g
    return g


def test_parse_hook_command_accepts_expected_shape(gd):
    py = "/usr/bin/python3"
    bridge = "/Users/x/.grok/token-optimizer/plugin/grok_hook_bridge.py"
    argv = gd._parse_hook_command(
        f"TOKEN_OPTIMIZER_RUNTIME=grok {py} {bridge} Stop"
    )
    assert argv == [py, bridge, "Stop"]


@pytest.mark.parametrize("cmd", [
    "TOKEN_OPTIMIZER_RUNTIME=grok python3 /abs/bridge.py Stop",  # bare python
    "echo hi; TOKEN_OPTIMIZER_RUNTIME=grok /p /b Stop",
    "TOKEN_OPTIMIZER_RUNTIME=grok /p /b Stop extra",
    "TOKEN_OPTIMIZER_RUNTIME=hermes /p /b Stop",
])
def test_parse_hook_command_rejects_malformed(gd, cmd):
    assert gd._parse_hook_command(cmd) is None


# ---------------------------------------------------------------------------
# grok_doctor: --probe trust gate (P0-2 — refuse untrusted interpreter)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_probe_refuses_untrusted_interpreter(gd, monkeypatch, tmp_path):
    """A tampered hooks file pointing at a world-writable interpreter must NOT
    be executed by --probe (mirrors cursor_doctor P0-2)."""
    # Create a world-writable fake interpreter (fails the trust gate).
    fake_py = tmp_path / "evil-python"
    fake_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_py.chmod(0o777)
    bridge = tmp_path / "bridge.py"
    bridge.write_text("pass", encoding="utf-8")
    cmd = f"TOKEN_OPTIMIZER_RUNTIME=grok {fake_py} {bridge} Stop"
    result = gd._run_probe_command(cmd, {"hookEventName": "stop"}, tmp_path)
    assert result["status"] == "fail"
    assert "not trusted" in result["detail"]


def test_resolver_persists_realpath_not_abspath(gi, monkeypatch, tmp_path):
    """The installer must persist the resolved realpath, not abspath, so a
    symlinked interpreter can't be swapped after install (P1-1/54a3456d)."""
    # Create a symlink to the real python3 and set it as the override.
    real = gi._resolve_safe_python()
    link = tmp_path / "python-link"
    link.symlink_to(real)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(link))
    resolved = gi._resolve_safe_python()
    # Must be the realpath (target), not the symlink path itself.
    assert resolved == os.path.realpath(str(link))
    assert resolved != str(link)
