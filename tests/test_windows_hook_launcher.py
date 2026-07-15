"""Regression coverage for native Windows hook command generation."""

import importlib.util
import ast
import re
import shlex
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "codex_install.py"
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"


def _load_measure_hook_resolver(platform):
    tree = ast.parse(MEASURE_PATH.read_text(encoding="utf-8"))
    wanted = {
        "_windows_hook_command",
        "_windows_hook_command_is_stale",
        "_resolve_hook_command",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "Path": Path,
        "re": re,
        "shlex": shlex,
        "platform": type("Platform", (), {"system": staticmethod(lambda: platform)}),
        "subprocess": subprocess,
        "sys": sys,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MEASURE_PATH), "exec"), namespace)
    return namespace


def _load_codex_install(monkeypatch, platform):
    scripts = str(MODULE_PATH.parent)
    monkeypatch.syspath_prepend(scripts)
    spec = importlib.util.spec_from_file_location("codex_install_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", platform)
    return module


def test_windows_hook_command_invokes_python_directly(monkeypatch):
    module = _load_codex_install(monkeypatch, "win32")

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert "hooks/run.py" in command or "hooks\\run.py" in command
    assert "skills/token-optimizer/scripts/read_cache.py" in command
    assert "python-launcher.sh" not in command
    assert "for b in bash" not in command


def test_posix_hook_command_keeps_bash_resolver(monkeypatch):
    module = _load_codex_install(monkeypatch, "linux")

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert command.startswith(module._BASH_RESOLVER_PREFIX)
    assert "python-launcher.sh" in command
    assert command.endswith(module._BASH_RESOLVER_SUFFIX)


def test_claude_windows_hook_command_invokes_python_directly():
    module = _load_measure_hook_resolver("Windows")
    template = (
        'for b in bash /bin/bash; do command -v "$b" >/dev/null 2>&1 && '
        'exec "$b" "${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh" '
        '"${CLAUDE_PLUGIN_ROOT}/hooks/run.py" '
        'skills/token-optimizer/scripts/measure.py ensure-health --quiet; done; exit 0'
    )
    root = Path(r"C:\Users\Alex Green\.claude\token-optimizer")

    command = module["_resolve_hook_command"](template, root)

    expected_argv = [
        sys.executable,
        str(root / "hooks" / "run.py"),
        "skills/token-optimizer/scripts/measure.py",
        "ensure-health",
        "--quiet",
    ]
    assert command.startswith(subprocess.list2cmdline(expected_argv))
    assert command.endswith(" >NUL 2>&1")
    assert "python-launcher.sh" not in command
    assert "for b in bash" not in command


def test_claude_posix_hook_command_is_byte_for_byte_unchanged():
    module = _load_measure_hook_resolver("Linux")
    template = (
        'for b in bash /bin/bash; do command -v "$b" >/dev/null 2>&1 && '
        'exec "$b" "${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh" '
        '"${CLAUDE_PLUGIN_ROOT}/hooks/run.py" scripts/example.py --quiet; done; exit 0'
    )
    root = Path("/opt/token optimizer")

    assert module["_resolve_hook_command"](template, root) == template.replace(
        "${CLAUDE_PLUGIN_ROOT}", str(root)
    )


def test_claude_windows_session_start_marks_legacy_launcher_for_self_heal():
    module = _load_measure_hook_resolver("Windows")
    old = 'for b in bash; do exec "$b" "C:/plugin/hooks/python-launcher.sh"; done; exit 0'
    native = 'C:\\Python\\python.exe C:\\plugin\\hooks\\run.py script.py >NUL 2>&1'

    assert module["_windows_hook_command_is_stale"](old, native) is True


def test_claude_posix_does_not_refresh_current_root_launcher():
    module = _load_measure_hook_resolver("Linux")
    old = 'for b in bash; do exec "$b" "/opt/plugin/hooks/python-launcher.sh"; done; exit 0'

    assert module["_windows_hook_command_is_stale"](old, "different") is False
