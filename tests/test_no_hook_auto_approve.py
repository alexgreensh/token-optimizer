#!/usr/bin/env python3
"""No Token Optimizer hook may approve a command that can write or delete.

A PreToolUse hook that answers "allow" approves the call outright: the user's
permission prompt and Claude Code's auto mode classifier are both skipped. The
Bash hook used to return "allow" for everything on the compression whitelist,
which includes `find -delete`, cloud CLIs and `git branch -D`.

Two rules are pinned here:
  1. On Claude Code the PreToolUse Bash hook prints nothing, for any command.
  2. Every host bridge whose rewrite also approves the call is limited to
     commands that cannot write, delete or run other programs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
BASH_HOOK = SCRIPTS / "bash_hook.py"
sys.path.insert(0, str(SCRIPTS))

import bash_whitelist  # noqa: E402

# On the compression whitelist, and able to delete, write or run code.
DESTRUCTIVE_WHITELISTED = [
    "find /srv/data -delete",
    "find . -name x -fprint out.txt",
    "git branch -D main",
    "git diff --output=/etc/hosts",
    "git log --output=notes.txt",
    "aws s3 rm s3://bucket --recursive",
    "aws ec2 terminate-instances --instance-ids i-123",
    "gcloud projects delete my-project --quiet",
    "az group delete --name prod --yes",
    "python3 -m pip install some-package",
    "bun run anything.ts",
    "deno run -A script.ts",
    "rg --pre ./run.sh pattern",
    "tree -o listing.txt",
    "eslint --fix .",
    "mvn deploy",
    "docker pull image",
    "GIT_DIR=/elsewhere git status",
]
INSPECTION_ONLY = ["ls -la", "git status", "git log --oneline -5", "rg pattern src", "wc -l file"]


def _run_bash_hook(command: str, tmp_home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.pop("TOKEN_OPTIMIZER_BASH_COMPRESS", None)
    payload = json.dumps({
        "session_id": "test-session",
        "cwd": str(tmp_home),
        "hook_event_name": "PreToolUse",
        "permission_mode": "auto",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    })
    return subprocess.run([sys.executable, str(BASH_HOOK), "--quiet"], input=payload,
                          capture_output=True, text=True, env=env, timeout=30)


@pytest.mark.parametrize("command", DESTRUCTIVE_WHITELISTED + INSPECTION_ONLY + ["rm -rf /srv/data"])
def test_claude_code_bash_hook_never_answers(command, tmp_path):
    result = _run_bash_hook(command, tmp_path)
    assert result.returncode == 0
    assert result.stdout == "", "PreToolUse Bash hook must stay silent on Claude Code"


def test_claude_code_pretooluse_scripts_never_emit_allow():
    """Nothing wired to Claude Code's PreToolUse may print an allow decision."""
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    wired = set()
    for group in hooks["hooks"].get("PreToolUse", []):
        for hook in group.get("hooks", []):
            for token in hook.get("command", "").split():
                name = token.strip('";').rsplit("/", 1)[-1]
                if name.endswith(".py") and name != "run.py" and (SCRIPTS / name).is_file():
                    wired.add(name)
    assert {"read_cache.py", "refetch_guard.py"} <= wired
    # bash_hook.py must stay UNWIRED: its main() is deliberately silent (a
    # hook "allow" would skip the permission prompt), so registering it only
    # taxed every Bash call with a no-op Python process.
    assert "bash_hook.py" not in wired
    for name in sorted(wired - {"measure.py"}):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert '"permissionDecision": "allow"' not in source, name
        assert "permission_decision=\"allow\"" not in source, name
        assert '_emit_pretool_response("allow"' not in source, name
        assert '_emit_pretool_response(\n        "allow"' not in source, name


@pytest.mark.parametrize("relative", [
    "hooks/hooks.json",
    "plugins/token-optimizer/hooks/hooks.json",
    "cowork/token-optimizer/hooks/hooks.json",
    "skills/token-optimizer/examples/hooks-starter.json",
    "plugins/token-optimizer/skills/token-optimizer/examples/hooks-starter.json",
    "cowork/token-optimizer/skills/token-optimizer/examples/hooks-starter.json",
])
def test_no_manifest_registers_the_silent_bash_hook(relative):
    """Keep the shared gate module, but never spawn its no-op CLI per Bash call."""
    doc = json.loads((REPO / relative).read_text(encoding="utf-8"))
    for event, groups in doc["hooks"].items():
        for group in groups:
            for hook in group.get("hooks", []):
                assert "bash_hook.py" not in hook.get("command", ""), (relative, event)


@pytest.mark.parametrize("command", DESTRUCTIVE_WHITELISTED)
def test_destructive_whitelisted_commands_are_not_auto_approvable(command):
    assert bash_whitelist.is_auto_approvable(command) is False


@pytest.mark.parametrize("command", INSPECTION_ONLY)
def test_inspection_commands_are_auto_approvable(command):
    assert bash_whitelist.is_auto_approvable(command) is True


def _fresh(module_name):
    for mod in (module_name, "bash_hook"):
        sys.modules.pop(mod, None)
    return __import__(module_name)


@pytest.mark.parametrize("command", DESTRUCTIVE_WHITELISTED)
def test_cursor_bridge_does_not_approve_destructive_commands(command, monkeypatch, tmp_path, capsys):
    bridge = _fresh("cursor_hook_bridge")
    cur = tmp_path / ".cursor"
    cur.mkdir()
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    monkeypatch.setattr(bridge, "_compression_enabled", lambda: True)
    bridge.handle_pre_tool_use({
        "hook_event_name": "preToolUse",
        "tool_name": "Shell",
        "tool_input": {"command": command, "working_directory": str(tmp_path)},
        "conversation_id": "composer-test",
        "cursor_version": "3.18.9",
    })
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", DESTRUCTIVE_WHITELISTED)
def test_grok_bridge_does_not_approve_destructive_commands(command, monkeypatch, capsys):
    bridge = _fresh("grok_hook_bridge")
    monkeypatch.setattr(bridge, "_to_dir", lambda: None)
    monkeypatch.setattr(bridge, "_compression_enabled", lambda: True)
    monkeypatch.setattr(bridge, "load_capabilities", lambda: {bridge.CAP_UPDATED_INPUT: True})
    bridge.handle_pre_tool_use({
        "hookEventName": "pre_tool_use",
        "sessionId": "session-test",
        "cwd": "/work/example",
        "toolName": "run_terminal_command",
        "toolInput": {"command": command},
    })
    assert capsys.readouterr().out == ""


def _copilot_pre_tool_use(command, monkeypatch, capsys):
    bridge = _fresh("copilot_hook_bridge")
    monkeypatch.delenv("TOKEN_OPTIMIZER_BASH_COMPRESS", raising=False)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "load_capabilities", lambda refresh=False: {
        bridge.CAP_UPDATED_INPUT: True, bridge.CAP_ALLOW: True})
    bridge.handle_pre_tool_use({"toolName": "bash", "toolArgs": {"command": command},
                                "sessionId": "copilot-test"})
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.mark.parametrize("command", DESTRUCTIVE_WHITELISTED)
def test_copilot_bridge_does_not_approve_destructive_commands(command, monkeypatch, capsys):
    hook_out = _copilot_pre_tool_use(command, monkeypatch, capsys)
    assert hook_out is None or "permissionDecision" not in hook_out


@pytest.mark.parametrize("command", INSPECTION_ONLY)
def test_copilot_bridge_approves_inspection_rewrites(command, monkeypatch, capsys):
    # Control: proves the destructive test above reaches the approval decision.
    hook_out = _copilot_pre_tool_use(command, monkeypatch, capsys)
    assert hook_out["permissionDecision"] == "allow"
    assert hook_out["updatedInput"]["command"].endswith(command.split()[-1])


def test_fake_bash_hook_without_gate_fails_closed(monkeypatch, tmp_path, capsys):
    """A bridge handed a bash_hook without the strict gate must not approve."""
    bridge = _fresh("cursor_hook_bridge")
    cur = tmp_path / ".cursor"
    cur.mkdir()
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    monkeypatch.setattr(bridge, "_compression_enabled", lambda: True)
    monkeypatch.setattr(bridge, "_bash_hook", types.SimpleNamespace(
        _is_whitelisted=lambda c: True, _has_dangerous_chars=lambda c: False))
    with pytest.raises(AttributeError):
        bridge.handle_pre_tool_use({
            "hook_event_name": "preToolUse", "tool_name": "Shell",
            "tool_input": {"command": "git status"}, "conversation_id": "c",
        })
    assert capsys.readouterr().out == ""


CODEX_WRITE_CAPABLE = [
    "tree -o out.txt",
    "tree -no out.txt",
    "tree -R -H http://x -o index.html",
]


@pytest.mark.parametrize("command", DESTRUCTIVE_WHITELISTED + CODEX_WRITE_CAPABLE)
def test_codex_rewrite_never_approves_write_capable_commands(command):
    """Codex's opt-in rewrite answers "allow", so it must pass the shared gate too."""
    import codex_command_compress

    assert not codex_command_compress.eligible(command), command
