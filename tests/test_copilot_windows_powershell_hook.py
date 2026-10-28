"""Copilot CLI on native Windows runs a hook's ``powershell`` field and skips a
hook that only carries ``bash``. The installer wrote only ``bash``, so no
Token Optimizer hook ever fired on Windows. It now also writes a PowerShell
command that sets the runtime and calls the interpreter by literal path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

_PS_SQ = "'\u2018\u2019\u201a\u201b"


def _load(monkeypatch, platform):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("copilot_install_ps", SCRIPTS / "copilot_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", platform)
    return module


def _parse_ps(command: str):
    """Model of how PowerShell reads the generated statement pair: one
    ``$env:NAME='value'`` assignment, ``;``, then ``& '<exe>' '<arg>' bare``.
    Single-quoted strings are literal; any PowerShell quote char doubled is
    one literal quote. Raises on anything else."""
    m = re.match(r"^\$env:([A-Za-z_]+)='([a-z]+)'; & ", command)
    assert m, command
    env = {m.group(1): m.group(2)}
    rest, argv, i = command[m.end():], [], 0
    while i < len(rest):
        if rest[i] == " ":
            i += 1
            continue
        if rest[i] in _PS_SQ:
            i += 1
            cur = []
            while True:
                assert i < len(rest), "unterminated string"
                if rest[i] in _PS_SQ:
                    if i + 1 < len(rest) and rest[i + 1] in _PS_SQ:
                        cur.append(rest[i])
                        i += 2
                        continue
                    i += 1
                    break
                cur.append(rest[i])
                i += 1
            argv.append("".join(cur))
        else:
            j = rest.find(" ", i)
            j = len(rest) if j < 0 else j
            token = rest[i:j]
            assert re.fullmatch(r"[a-z-]+", token), token
            argv.append(token)
            i = j
    return env, argv


PATHS = [
    r"C:\Python313\python.exe",
    r"C:\Program Files\Python313\python.exe",
    r"C:\Users\O'Brien\AppData\Local\Programs\Python\Python313\python.exe",
    "C:\\Users\\Smart\u2019Quote\\py\\python.exe",
    r"C:\Users\Jo$h `x` 100% (R&D)!\python.exe",
    "C:\\Users\\Jos\u00e9 \u5c71\u7530\\python.exe",
]


@pytest.mark.parametrize("py", PATHS)
def test_powershell_command_passes_paths_literally(monkeypatch, py):
    ci = _load(monkeypatch, "win32")
    bridge = py.replace("python.exe", r".copilot\token-optimizer\copilot_hook_bridge.py")
    env, argv = _parse_ps(ci._powershell_hook_command(py, bridge, "session-start"))
    assert env == {"TOKEN_OPTIMIZER_RUNTIME": "copilot"}
    assert argv == [py, bridge, "session-start"]


def _install(monkeypatch, tmp_path, platform):
    ci = _load(monkeypatch, platform)
    monkeypatch.setattr(ci, "_resolve_safe_python", lambda: r"C:\Users\O'Brien\py\python.exe")
    import copilot_hook_bridge
    monkeypatch.setattr(copilot_hook_bridge, "load_capabilities", lambda refresh=False: {})
    ci.install(home=tmp_path / ".copilot")
    return json.loads((tmp_path / ".copilot" / "hooks" / ci.HOOK_FILE_NAME).read_text(encoding="utf-8"))


def test_windows_install_writes_a_powershell_command_for_every_hook(monkeypatch, tmp_path):
    config = _install(monkeypatch, tmp_path, "win32")
    for event, entries in config["hooks"].items():
        for entry in entries:
            assert "powershell" in entry, (event, entry)
            _, argv = _parse_ps(entry["powershell"])
            assert argv[0] == r"C:\Users\O'Brien\py\python.exe"
            assert argv[1].endswith("copilot_hook_bridge.py")
            assert "bash" in entry  # Git Bash / WSL users keep the POSIX form


def test_posix_install_is_unchanged(monkeypatch, tmp_path):
    config = _install(monkeypatch, tmp_path, "linux")
    for entries in config["hooks"].values():
        for entry in entries:
            assert "powershell" not in entry


def _powershells():
    names = ["powershell", "pwsh"] if sys.platform == "win32" else ["pwsh"]
    return [p for p in (shutil.which(n) for n in names) if p]


@pytest.mark.skipif(not _powershells(), reason="no PowerShell on this machine")
def test_powershell_command_really_runs_with_the_payload_on_stdin(monkeypatch, tmp_path):
    """On a machine with PowerShell (the Windows CI jobs): the hook gets the
    payload on stdin, its arguments, and TOKEN_OPTIMIZER_RUNTIME=copilot."""
    ci = _load(monkeypatch, "win32")
    folder = tmp_path / "Jo$h O'Brien 100%"
    folder.mkdir()
    bridge = folder / "copilot_hook_bridge.py"
    bridge.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(),"
        " 'runtime': os.environ.get('TOKEN_OPTIMIZER_RUNTIME')}))\n", encoding="utf-8")
    command = ci._powershell_hook_command(sys.executable, str(bridge), "post-tool-use")
    for program in _powershells():
        proc = subprocess.run([program, "-NoProfile", "-Command", command],
                              input=b'{"toolName":"bash"}', capture_output=True, timeout=120,
                              env=dict(os.environ))
        assert proc.returncode == 0, (program, proc.stderr)
        out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        assert out == {"argv": ["post-tool-use"], "stdin": '{"toolName":"bash"}', "runtime": "copilot"}
