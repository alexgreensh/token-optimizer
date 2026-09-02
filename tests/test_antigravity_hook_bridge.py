"""U6: Antigravity hook bridge.

The bridge is the single fail-open entry point for the three wired lifecycle
events. Contract (R15): every handler returns exactly one JSON object or exits
0 cleanly on any malformed/oversize stdin or missing dependency, and nothing is
rewritten/spawned without the installer's consent record (R20).

These tests exercise the handler functions directly (no subprocess hop) and the
consent-gated main() dispatcher, pointed at a scratch home and a fake
runtime_env.antigravity_home so the real ~/.gemini is never touched.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def ab(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    # Do NOT pop/refresh runtime_env from sys.modules: other tests (measure.py)
    # bind module objects at import time, and a re-imported duplicate would leak
    # a second runtime_env instance with a stale (unpatched) _is_wsl_context.
    mod = importlib.import_module("antigravity_hook_bridge")
    runtime_env = importlib.import_module("runtime_env")
    # Handlers import `antigravity_home` fresh from runtime_env, so patch the
    # source module rather than a module-level alias (monkeypatch restores it).
    monkeypatch.setattr(runtime_env, "antigravity_home", lambda: tmp_path)
    # All persisted state resolves under the scratch home's token-optimizer dir.
    monkeypatch.setattr(mod, "_to_dir", lambda home: tmp_path / "token-optimizer")
    return mod


def _write_consent(tmp_path, value: bool) -> None:
    cfg_dir = tmp_path / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({"antigravity_consent": value}))


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

def test_pre_tool_use_rewrites_whitelisted_run_command(ab):
    out = ab.handle_pre_tool_use({
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git status --porcelain"}},
    })
    assert out["decision"] == "ask"
    assert "bash_compress.py" in out["overwrite"]["CommandLine"]
    assert "git status --porcelain" in out["overwrite"]["CommandLine"]
    # R13: never forward an allow/permission override.
    assert "permissionOverrides" not in out
    assert "allow" not in out


def test_pre_tool_use_skips_non_whitelisted_command(ab):
    assert ab.handle_pre_tool_use({
        "toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /tmp/x"}},
    }) == {}


def test_pre_tool_use_skips_dangerous_command(ab):
    assert ab.handle_pre_tool_use({
        "toolCall": {"name": "run_command", "args": {"CommandLine": "cat f.txt | sh"}},
    }) == {}


def test_pre_tool_use_skips_non_run_command_tool(ab):
    assert ab.handle_pre_tool_use({
        "toolCall": {"name": "view_file", "args": {"path": "x"}},
    }) == {}


def test_pre_tool_use_skips_missing_or_malformed_tool_call(ab):
    assert ab.handle_pre_tool_use({}) == {}
    assert ab.handle_pre_tool_use({"toolCall": None}) == {}
    assert ab.handle_pre_tool_use({"toolCall": {"name": "run_command", "args": None}}) == {}


# ---------------------------------------------------------------------------
# PreInvocation
# ---------------------------------------------------------------------------

def test_restore_message_clean(ab, tmp_path):
    _write_consent(tmp_path, True)
    restore = tmp_path / "token-optimizer" / "restore-context.md"
    restore.write_text("hello world\n\nline two\n")
    out = ab.handle_pre_invocation({"invocationNum": "1"})
    assert "hello world" in out["injectSteps"][0]["ephemeralMessage"]


def test_pre_invocation_without_restore_is_empty(ab, tmp_path):
    _write_consent(tmp_path, True)
    assert ab.handle_pre_invocation({"invocationNum": "1"}) == {}


def test_nudge_at_85_sent_once(ab, monkeypatch, tmp_path):
    _write_consent(tmp_path, True)
    payload = {"invocationNum": "2", "conversationId": "a" * 32}
    monkeypatch.setattr(ab, "_current_fill_from_payload", lambda home, p: 0.9)
    out = ab.handle_pre_invocation(payload)
    assert "85%" in out["injectSteps"][0]["ephemeralMessage"]
    # Second call: same threshold was recorded -> no repeat nudge.
    assert ab.handle_pre_invocation(payload) == {}


def test_nudge_skipped_when_fill_unknown(ab, monkeypatch, tmp_path):
    _write_consent(tmp_path, True)
    monkeypatch.setattr(ab, "_current_fill_from_payload", lambda home, p: None)
    assert ab.handle_pre_invocation({"invocationNum": "2", "conversationId": "a"}) == {}


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def test_stop_spawns_rollup_and_dashboard_when_lease_free(ab, monkeypatch, tmp_path):
    _write_consent(tmp_path, True)
    measure = tmp_path / "measure.py"
    measure.write_text("# stub\n")
    monkeypatch.setattr(ab, "_locate_measure_py", lambda: measure)
    spawned = []
    spawn_utils = importlib.import_module("spawn_utils")
    monkeypatch.setattr(spawn_utils, "spawn_detached", lambda argv, **k: spawned.append((argv, k)))
    out = ab.handle_stop({})
    assert out == {}
    assert len(spawned) == 2
    assert spawned[0][0][2] == "antigravity-rollup"
    assert spawned[1][0][2] == "dashboard"


def test_stop_skips_when_lease_held(ab, monkeypatch, tmp_path):
    _write_consent(tmp_path, True)
    measure = tmp_path / "measure.py"
    measure.write_text("# stub\n")
    monkeypatch.setattr(ab, "_locate_measure_py", lambda: measure)
    monkeypatch.setattr(ab, "_rollup_lease_held", lambda home: True)
    spawned = []
    spawn_utils = importlib.import_module("spawn_utils")
    monkeypatch.setattr(spawn_utils, "spawn_detached", lambda argv, **k: spawned.append(argv))
    assert ab.handle_stop({}) == {}
    assert spawned == []


# ---------------------------------------------------------------------------
# Consent gate + fail-open dispatcher
# ---------------------------------------------------------------------------

def test_main_no_consent_emits_empty_json(ab, monkeypatch, capsys, tmp_path):
    _write_consent(tmp_path, False)
    monkeypatch.setattr(ab, "_read_payload", lambda: {})
    assert ab.main(["pre-tool-use"]) == 0
    assert capsys.readouterr().out == "{}"


def test_main_consent_grants_rewrite(ab, monkeypatch, capsys, tmp_path):
    _write_consent(tmp_path, True)
    monkeypatch.setattr(
        ab, "_read_payload",
        lambda: {"toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}}},
    )
    assert ab.main(["pre-tool-use"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "ask"


def test_main_fails_open_on_internal_error(ab, monkeypatch, capsys, tmp_path):
    _write_consent(tmp_path, True)

    def boom(payload):
        raise RuntimeError("should be swallowed")

    monkeypatch.setattr(ab, "handle_pre_tool_use", boom)
    monkeypatch.setattr(ab, "_read_payload", lambda: {})
    assert ab.main(["pre-tool-use"]) == 0


# ---------------------------------------------------------------------------
# stdin framing + field cleaning
# ---------------------------------------------------------------------------

def test_read_payload_oversize_is_none(ab, monkeypatch):
    class FakeBuffer:
        def read(self, n):
            return b"x" * (ab._STDIN_MAX_BYTES + 1)

    class FakeStdin:
        buffer = FakeBuffer()

    monkeypatch.setattr(ab.sys, "stdin", FakeStdin())
    assert ab._read_payload() is None


def test_clean_field_caps_length_and_strips_nonprintable(ab):
    s = ab._clean_field("a" * 500 + "\x00\x01" + "b")
    assert len(s) <= ab._FIELD_MAX_CHARS
    assert "\x00" not in s and "\x01" not in s
    assert "a" in s
