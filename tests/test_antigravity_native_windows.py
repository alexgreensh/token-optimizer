"""Native Windows (#296): the Antigravity installer writes hook commands that
PowerShell and cmd.exe both run, instead of refusing."""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


def _install_as_windows(tmp_path, monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    ai = importlib.import_module("antigravity_install")
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", sys.executable)
    monkeypatch.setattr(ai, "_resolve_safe_python", lambda: sys.executable)
    # Faking os.name breaks pathlib on Python 3.14, so only the command builder
    # is switched to Windows; install() itself has no Windows-specific branch.
    import adapter_hook_command
    monkeypatch.setattr(adapter_hook_command, "_is_windows", lambda _w: True)
    ai.install(home=tmp_path)
    return ai


def test_windows_install_writes_shell_neutral_commands(tmp_path, monkeypatch):
    ai = _install_as_windows(tmp_path, monkeypatch)
    pdir = ai.plugin_dir(tmp_path)
    config = json.loads((pdir / "hooks.json").read_text(encoding="utf-8"))
    group = config["token-optimizer"]
    commands = [h["command"] for h in group["PreInvocation"] + group["Stop"]]
    commands += [h["command"] for g in group["PreToolUse"] for h in g["hooks"]]
    bridge = str(pdir / "antigravity_hook_bridge.py")
    assert len(commands) == 3
    for cmd in commands:
        assert "TOKEN_OPTIMIZER_RUNTIME=" not in cmd
        assert not cmd.startswith(('"', "'", "&"))
        assert f' -E -s "{bridge}" ' in cmd


def test_bridge_pins_its_own_runtime(monkeypatch):
    """The Windows command has no env prefix, so the bridge sets the runtime."""
    sys.path.insert(0, str(SCRIPTS))
    bridge = importlib.import_module("antigravity_hook_bridge")
    monkeypatch.delenv("TOKEN_OPTIMIZER_RUNTIME", raising=False)
    monkeypatch.setattr(bridge, "_read_payload", lambda: {})
    bridge.main(["unknown-event"])
    assert os.environ.get("TOKEN_OPTIMIZER_RUNTIME") == "antigravity"
