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
def gi(monkeypatch, tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("grok_install", None)
    # The host's real interpreter is not guaranteed to pass the trust gate --
    # hosted-CI tool caches extract python world-writable. Point
    # TOKEN_OPTIMIZER_PYTHON at a tmp interpreter with clean modes (0755 file
    # in a 0755 euid-owned dir) so install/_resolve_safe_python works in CI.
    if os.name != "nt" and hasattr(os, "geteuid"):
        d = tmp_path / "trusted-bin"
        d.mkdir(mode=0o755)
        f = d / "python3"
        f.write_bytes(b"#!/bin/sh\n")
        os.chmod(f, 0o755)
        os.chmod(d, 0o755)
        monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(f))
    else:
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
    assert gi._resolve_safe_python() == os.path.realpath(resolved)
    # When the override is invalid, the resolver falls through to other
    # candidates. In CI (hostedtoolcache python is world-writable) no
    # candidate passes the gate, so mock the gate to verify the fallthrough
    # path produces a real file.
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "/nonexistent/python3")
    # Mock the trust gate to accept any existing file (CI hostedtoolcache
    # python is world-writable and fails the real gate).
    monkeypatch.setattr(gi, "py_path_is_trusted",
                        lambda p: os.path.isfile(os.path.realpath(p)))
    assert os.path.isfile(gi._resolve_safe_python())


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_rejects_hijackable_paths(gi):
    import py_trust
    # world-writable DIR -> anyone can swap the file
    d = tempfile.mkdtemp()
    os.chmod(d, 0o777)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert py_trust.py_path_is_trusted(f) is False

    # world-writable FILE in an owned dir -> anyone can rewrite its bytes
    d2 = tempfile.mkdtemp()
    os.chmod(d2, 0o755)
    f2 = os.path.join(d2, "python3")
    open(f2, "w").close()
    os.chmod(f2, 0o777)
    assert py_trust.py_path_is_trusted(f2) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_accepts_owned_unwritable_file(gi):
    import py_trust
    d = tempfile.mkdtemp()
    os.chmod(d, 0o755)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert py_trust.py_path_is_trusted(f) is True


def test_grok_install_uses_shared_trust_gate(gi, monkeypatch, tmp_path):
    """grok_install delegates to the shared py_trust gate, not a private copy.

    Two proofs:
    1. Identity: gi.py_path_is_trusted IS py_trust.py_path_is_trusted (same
       function object), proving the import came from the shared module.
    2. Functional: patching gi.py_path_is_trusted changes _resolve_safe_python
       behavior, proving the resolver calls through that name.
    """
    import py_trust

    # Proof 1: the imported name is the shared function object.
    assert gi.py_path_is_trusted is py_trust.py_path_is_trusted

    # Proof 2: patching the name in grok_install's namespace controls the
    # resolver. A private copy would not be affected by this patch.
    sentinel = str(tmp_path / "sentinel-python")
    Path(sentinel).write_bytes(b"#!/bin/sh\n")
    os.chmod(sentinel, 0o755)

    monkeypatch.setattr(gi, "py_path_is_trusted",
                        lambda p: p == sentinel)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", sentinel)
    resolved = gi._resolve_safe_python()
    assert resolved == os.path.realpath(sentinel)


@pytest.mark.skipif(os.name == "nt", reason="Grok install refuses native Windows (POSIX-shell quoted command)")
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


@pytest.mark.skipif(os.name == "nt", reason="Grok install refuses native Windows (POSIX-shell quoted command)")
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
    # Use platform-appropriate absolute paths so os.path.isabs passes on
    # both POSIX and Windows.
    py = str(Path("/usr/bin/python3").resolve() if os.name != "nt"
             else Path("C:/bin/python3"))
    bridge = str(Path("/Users/x/.grok/token-optimizer/plugin/grok_hook_bridge.py").resolve()
                 if os.name != "nt"
                 else Path("C:/Users/x/.grok/token-optimizer/plugin/grok_hook_bridge.py"))
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


def test_read_session_accepts_snake_case_summary(tmp_path):
    """Upstream writes summary.json with plain serde_json (snake_case); older
    builds wrote camelCase. Both must populate the session record."""
    import json as _json
    import importlib
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS))
    gs = importlib.import_module("grok_state")
    for spelling, payload in (
        ("snake", {"info": {"id": "s1", "cwd": "/w"}, "session_summary": "t", "created_at": "2026-09-03T00:00:00Z",
                   "updated_at": "2026-09-03T00:01:00Z", "num_messages": 3, "num_chat_messages": 2,
                   "current_model_id": "grok-4", "agent_name": "default"}),
        ("camel", {"info": {"id": "s1", "cwd": "/w"}, "sessionSummary": "t", "createdAt": "2026-09-03T00:00:00Z",
                   "updatedAt": "2026-09-03T00:01:00Z", "numMessages": 3, "numChatMessages": 2,
                   "currentModelId": "grok-4", "agentName": "default"}),
    ):
        sd = tmp_path / spelling / "s1"
        sd.mkdir(parents=True)
        (sd / "summary.json").write_text(_json.dumps(payload), encoding="utf-8")
        rec = gs.read_session(sd)
        assert rec["title"] == "t" and rec["num_messages"] == 3 and rec["model_id"] == "grok-4", (spelling, rec)


# ---------------------------------------------------------------------------
# grok_install: bash_whitelist.py in payload (bash compression feature)
# ---------------------------------------------------------------------------


def test_bash_whitelist_in_payload_modules(gi):
    """bash_whitelist.py must be in _PAYLOAD_MODULES so the installed bridge
    can import bash_hook (which does an unguarded top-level import of it).
    Without it, bash compression silently no-ops in every Grok install."""
    assert "bash_whitelist.py" in gi._PAYLOAD_MODULES, (
        "bash_whitelist.py missing from _PAYLOAD_MODULES — bash_hook import "
        "fails in the installed path and PreToolUse compression is dead"
    )


def test_install_copies_bash_whitelist(gi, tmp_path):
    """install() must actually place bash_whitelist.py next to the bridge."""
    home = tmp_path / "grok-home"
    gi.install(home=home)
    whitelist = home / "token-optimizer" / "plugin" / "bash_whitelist.py"
    assert whitelist.is_file(), "bash_whitelist.py not copied to plugin dir"
    # Verify it's the real module, not an empty stub.
    import importlib.util
    spec = importlib.util.spec_from_file_location("bw_check", whitelist)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "is_whitelisted"), (
        "installed bash_whitelist.py is not the real module"
    )


# ---------------------------------------------------------------------------
# grok dashboard collector: toggle lookups match doctor's emitted names
# ---------------------------------------------------------------------------


def test_dashboard_collector_toggle_names_match_doctor(gd, gi, monkeypatch, tmp_path):
    """Every dashboard toggle must look up a check name the doctor actually
    emits, with the correct lowercase status. Before the fix, the collector
    used uppercase statuses and wrong names (copy-pasted from another adapter),
    so every toggle permanently showed installed:False."""
    import measure

    # Install grok so the doctor has a real install to check.
    home = tmp_path / "grok-home"
    gi.install(home=home)
    monkeypatch.setenv("GROK_HOME", str(home))

    # Get the real check names the doctor emits.
    checks = gd.run_checks()
    names = {c["name"] for c in checks}
    statuses = {c["status"] for c in checks}

    # The doctor must emit lowercase statuses (the collector depends on this).
    assert statuses.issubset({"ok", "warn", "fail"}), (
        f"doctor emits non-lowercase statuses: {statuses}"
    )

    # Patch all checks to "ok" so we can verify every toggle CAN turn green.
    monkeypatch.setattr(gd, "run_checks", lambda: [
        {**c, "status": "ok"} for c in checks
    ])

    panel = measure._collect_grok_hook_status_for_dashboard()

    # Every toggle's lookup name must exist in the doctor's emitted names.
    for key, toggle in panel.items():
        # The toggle must reference a real doctor check name. We verify by
        # checking that installed is True (meaning the name was found AND
        # status was "ok"). If the name were wrong, _ok would return False.
        assert toggle["installed"] is True, (
            f"toggle {key} lookup name doesn't match any doctor check — "
            f"toggle can never show installed:True. Doctor names: {names}"
        )


def test_dashboard_collector_no_capability_toggle(gd, gi, monkeypatch, tmp_path):
    """The collector must NOT have a grok_capabilities toggle — grok_doctor
    has no capability matrix check. A toggle looking up a non-existent check
    name permanently shows installed:False."""
    import measure

    home = tmp_path / "grok-home"
    gi.install(home=home)
    monkeypatch.setenv("GROK_HOME", str(home))

    panel = measure._collect_grok_hook_status_for_dashboard()
    assert "grok_capabilities" not in panel, (
        "grok_capabilities toggle exists but grok_doctor has no capability "
        "check — this toggle can never turn green"
    )


def test_dashboard_collector_port_label_matches_grok_port(gd, gi, monkeypatch, tmp_path):
    """The dashboard port toggle must reference grok's own port (24848), not
    antigravity's port (24847) which was copy-pasted."""
    import measure

    home = tmp_path / "grok-home"
    gi.install(home=home)
    monkeypatch.setenv("GROK_HOME", str(home))

    panel = measure._collect_grok_hook_status_for_dashboard()
    port_toggle = panel["grok_dashboard_port"]

    # The label and description must reference 24848, never 24847.
    assert "24848" in port_toggle["label"], (
        f"port label doesn't reference grok's port 24848: {port_toggle['label']}"
    )
    assert "24848" in port_toggle["description"], (
        f"port description doesn't reference grok's port 24848: {port_toggle['description']}"
    )
    assert "24847" not in port_toggle["label"], (
        "port label references antigravity's port 24847"
    )
    assert "24847" not in port_toggle["description"], (
        "port description references antigravity's port 24847"
    )

    # The lookup name must be "dashboard daemon" (what grok_doctor emits).
    checks = gd.run_checks()
    daemon_check = [c for c in checks if c["name"] == "dashboard daemon"]
    assert daemon_check, (
        "grok_doctor doesn't emit a 'dashboard daemon' check — "
        "the port toggle lookup name is wrong"
    )
