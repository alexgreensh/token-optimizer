"""Issue #21: on Windows there is often no ``python3`` at all, only ``python``
or the ``py`` launcher. Every hook goes through hooks/python-launcher.sh, which
must then fall back to ``python``, and to ``py -3`` when that is all there is,
instead of giving up.

The launcher runs for real here, with a PATH that has no python3: one folder
with a stand-in interpreter, plus a toolbox folder holding links to every
system command (/usr/bin, /bin, ...) except Python.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="builds a POSIX toolbox of symlinks")

_PYTHONISH = re.compile(r"^(python|py)([0-9.]*|w)?(\.exe)?$|^python[0-9.]+-config$")


def _toolbox(tmp_path: Path) -> Path:
    box = tmp_path / "toolbox"
    box.mkdir()
    for entry in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        d = Path(entry)
        if not d.is_dir():
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if _PYTHONISH.match(name) or (box / name).exists():
                continue
            src = d / name
            if os.access(src, os.X_OK) and not src.is_dir():
                (box / name).symlink_to(src)
    return box


def _fake(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    path = folder / name
    path.write_text("#!/bin/sh\nprintf 'RAN %s:' \"$(basename \"$0\")\"\n"
                    "for a in \"$@\"; do printf ' [%s]' \"$a\"; done\necho\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_launcher(tmp_path: Path, fake_dir: Path):
    env = {
        "PATH": f"{fake_dir}{os.pathsep}{_toolbox(tmp_path)}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return subprocess.run(["/bin/bash", str(LAUNCHER), "/x/run.py", "hook.py", "--quiet"],
                          input=b"{}", capture_output=True, timeout=60, env=env, cwd=str(tmp_path))


def test_only_python_on_path_is_used(tmp_path):
    fake_dir = tmp_path / "py-only"
    _fake(fake_dir, "python")
    proc = _run_launcher(tmp_path, fake_dir)
    assert b"RAN python: [/x/run.py] [hook.py] [--quiet]" in proc.stdout, proc


def test_only_the_py_launcher_on_path_runs_py_dash_3(tmp_path):
    fake_dir = tmp_path / "launcher-only"
    _fake(fake_dir, "py")
    proc = _run_launcher(tmp_path, fake_dir)
    assert b"RAN py: [-3] [/x/run.py] [hook.py] [--quiet]" in proc.stdout, proc
