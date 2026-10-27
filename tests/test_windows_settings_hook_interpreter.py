"""settings.json hooks written by setup-smart-compact and setup-quality-bar
named bare ``python3``. Claude Code runs them through Git Bash on Windows,
where ``python3`` is often missing or the Microsoft Store stub, so every event
failed. On Windows they now name the running interpreter (forward slashes,
bash-quoted, so an apostrophe in the path is safe), like HOOK_COMMAND does.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))

PY = r"C:\Users\O'Brien\AppData\Local\Programs\Python\Python313\python.exe"
MP = r"C:\Users\O'Brien\.claude\skills\token-optimizer\scripts\measure.py"


@pytest.fixture
def m(monkeypatch, tmp_path):
    (tmp_path / "claude").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    import measure
    return measure


def _argv_after_guard(command):
    guard, _, rest = command.partition(" || exit 0; ")
    assert guard.startswith("[ -r ")
    return shlex.split(guard[len("[ -r "):-2]), shlex.split(rest)


def test_windows_smart_compact_hooks_name_the_real_interpreter(m, monkeypatch):
    monkeypatch.setattr(m.sys, "platform", "win32")
    monkeypatch.setattr(m.sys, "executable", PY)
    monkeypatch.setattr(m, "_get_measure_py_path", lambda: MP)
    commands = m._smart_compact_hook_commands()
    for event, command in commands.items():
        guard_argv, argv = _argv_after_guard(command)
        assert guard_argv == [MP.replace("\\", "/")]
        assert argv[0] == PY.replace("\\", "/"), (event, command)
        assert argv[1] == MP.replace("\\", "/")
        assert "python3" not in command
    settings = {"hooks": {e: [{"hooks": [{"type": "command", "command": c}]}]
                          for e, c in commands.items()}}
    assert all(m._is_smart_compact_installed(settings).values())


def test_posix_smart_compact_hooks_are_unchanged(m, monkeypatch):
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m, "_get_measure_py_path", lambda: "/x/measure.py")
    assert m._smart_compact_hook_commands()["Stop"] == (
        "[ -r '/x/measure.py' ] || exit 0; python3 '/x/measure.py' compact-capture --trigger stop")


def test_windows_quality_cache_hook_names_the_real_interpreter(m, monkeypatch):
    monkeypatch.setattr(m.sys, "platform", "win32")
    monkeypatch.setattr(m.sys, "executable", PY)
    py, mp = m._settings_hook_python_and_script(MP)
    command = f"{py} {mp} quality-cache --quiet"
    assert shlex.split(command)[:2] == [PY.replace("\\", "/"), MP.replace("\\", "/")]
    assert m._command_drives_quality_cache(command) is True


def test_setup_quality_bar_on_windows_writes_the_real_interpreter(m, monkeypatch, tmp_path):
    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(m, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(m, "_is_running_from_plugin_cache", lambda: False)
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_get_measure_py_path", lambda: MP)
    monkeypatch.setattr(m.sys, "platform", "win32")
    monkeypatch.setattr(m.sys, "executable", PY)
    m.setup_quality_bar(quiet=False)
    import json
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    ours = [c for c in commands if "quality-cache" in c]
    assert ours, commands
    for c in ours:
        assert shlex.split(c)[0] == PY.replace("\\", "/"), c
