"""Issue #78: ``bash install.sh --copilot`` from a Windows shell runs WSL as
root, so $HOME is /root and the hooks landed in /root/.copilot, which the
native Windows Copilot CLI never reads. install.sh now finds the real Windows
Copilot home (/mnt/c/Users/<you>/.copilot) in that one case, and leaves every
other case alone.

Drives the real install.sh function through its test mode and its mock hooks
(_TO_PROC_VERSION, _TO_WSL_USERS_DIR, _TO_POWERSHELL, _TO_CMD_EXE).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _recover(tmp_path, *, proc_version="Linux version 5.15 microsoft-standard-WSL2",
             users=("alex",), home="/root", uid="0", preset=None):
    proc = tmp_path / "proc_version"
    proc.write_text(proc_version, encoding="utf-8")
    users_dir = tmp_path / "Users"
    users_dir.mkdir(exist_ok=True)
    for u in users:
        (users_dir / u).mkdir(exist_ok=True)
    (users_dir / "Public").mkdir(exist_ok=True)
    script = (
        f'source "{INSTALL_SH.as_posix()}"\n'
        f'id() {{ echo {uid}; }}\n'
        '_recover_home_from_windows_env TOKEN_OPTIMIZER_COPILOT_HOME ".copilot" >/dev/null 2>&1\n'
        'printf "RESULT=%s\\n" "${TOKEN_OPTIMIZER_COPILOT_HOME:-}"\n'
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("WSL_DISTRO_NAME", "TOKEN_OPTIMIZER_COPILOT_HOME", "COPILOT_HOME")}
    env.update({
        "_TO_INSTALL_SH_TEST_MODE": "1",
        "_TO_PROC_VERSION": str(proc),
        "_TO_WSL_USERS_DIR": str(users_dir),
        "_TO_POWERSHELL": str(tmp_path / "no-powershell.exe"),
        "_TO_CMD_EXE": str(tmp_path / "no-cmd.exe"),
        "HOME": home,
    })
    if preset:
        env["TOKEN_OPTIMIZER_COPILOT_HOME"] = preset
    proc_run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, env=env, cwd=str(tmp_path))
    out = [line for line in proc_run.stdout.splitlines() if line.startswith("RESULT=")]
    assert out, (proc_run.stdout, proc_run.stderr)
    return out[-1][len("RESULT="):]


def test_wsl_root_targets_the_single_windows_profile(tmp_path):
    assert _recover(tmp_path) == "/mnt/c/Users/alex/.copilot"


def test_non_root_wsl_user_keeps_their_own_home(tmp_path):
    assert _recover(tmp_path, home="/home/alex", uid="1000") == ""


def test_plain_linux_is_untouched(tmp_path):
    assert _recover(tmp_path, proc_version="Linux version 6.1 generic") == ""


def test_several_windows_profiles_are_not_guessed(tmp_path):
    assert _recover(tmp_path, users=("alex", "sam")) == ""


def test_an_explicit_choice_wins(tmp_path):
    assert _recover(tmp_path, preset="/mnt/d/custom/.copilot") == "/mnt/d/custom/.copilot"
