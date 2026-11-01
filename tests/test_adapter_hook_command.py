"""Hook command text shared by the Antigravity, Cursor and Grok installers."""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"))
import adapter_hook_command as ahc  # noqa: E402

EVENTS = ("stop", "pre-tool-use")
WIN_PY = r"C:\Python314\python.exe"
WIN_BRIDGE = r"C:\Users\Tom Smith\.gemini\config\plugins\token-optimizer\antigravity_hook_bridge.py"


def test_posix_form_is_runtime_pinned_and_quoted():
    cmd = ahc.build("/usr/bin/python3", "/home/a b/bridge.py", "stop", runtime="cursor", windows=False)
    assert shlex.split(cmd) == ["TOKEN_OPTIMIZER_RUNTIME=cursor", "/usr/bin/python3", "/home/a b/bridge.py", "stop"]


def test_windows_form_runs_in_powershell_and_cmd():
    cmd = ahc.build(WIN_PY, WIN_BRIDGE, "stop", runtime="antigravity",
                    python_flags=("-E", "-s"), windows=True)
    assert cmd == f'{WIN_PY} -E -s "{WIN_BRIDGE}" stop'


@pytest.mark.parametrize("windows,py,bridge", [
    (False, "/usr/bin/python3", "/home/a b/bridge.py"),
    (True, WIN_PY, WIN_BRIDGE),
])
def test_parse_round_trips(windows, py, bridge):
    for flags in ((), ("-E", "-s")):
        cmd = ahc.build(py, bridge, "stop", runtime="grok", python_flags=flags, windows=windows)
        assert ahc.parse(cmd, runtime="grok", events=EVENTS, python_flags=flags,
                         windows=windows) == [py, bridge, "stop"]


@pytest.mark.parametrize("cmd", [
    f'{WIN_PY} "{WIN_BRIDGE}" stop & calc',
    f'{WIN_PY} "{WIN_BRIDGE}" not-an-event',
    f'python.exe "{WIN_BRIDGE}" stop',
    f'{WIN_PY} "bridge.py" stop',
])
def test_parse_rejects_foreign_windows_commands(cmd):
    assert ahc.parse(cmd, runtime="grok", events=EVENTS, windows=True) is None


def test_parse_rejects_posix_command_without_runtime_pin():
    assert ahc.parse("/usr/bin/python3 /b.py stop", runtime="grok", events=EVENTS, windows=False) is None


@pytest.mark.parametrize("bad", [r"C:\Users\$money\bridge.py", r"C:\Users\100%\bridge.py"])
def test_windows_path_shells_would_expand_is_refused_with_guidance(bad):
    # No 8.3 short name exists off Windows, so the path cannot be made safe.
    with pytest.raises(RuntimeError, match="plain folder"):
        ahc.build(WIN_PY, bad, "stop", runtime="cursor", windows=True)


def test_path_spellings_include_the_path():
    assert ahc.path_spellings(WIN_BRIDGE, windows=True) >= {WIN_BRIDGE}
    assert ahc.path_spellings("/a/b.py", windows=False) == {"/a/b.py"}
