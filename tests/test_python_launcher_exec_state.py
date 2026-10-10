"""The launcher must hand the exec'd interpreter the state the host gave it.

Regression for the #215 builtin-only canonicalisation: ``cd -P`` ran in the
main shell (no subshell any more) and never went back, so every hook and every
launcher-routed command (``/token-optimizer:quick``, ``:health``) ran with the
plugin's ``hooks/`` directory as its working directory. Project-relative reads
(``CLAUDE.md``, ``Path.cwd()`` project-dir matching) then looked at the wrong
project. The suite counts subshells and externals but never looked at what the
interpreter inherits, so it stayed green.

The probe interpreter reports cwd, the environment-visible shell state and the
process-level state that survives ``exec`` (umask, signal dispositions, open
fds). Anything the launcher changes there and does not restore is a leak.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"



def _hook_bash() -> str | None:
    """The bash the host runs hooks under: Git Bash on Windows, bash elsewhere.

    On Windows ``shutil.which("bash")`` usually finds WSL's
    ``C:\\Windows\\System32\\bash.exe`` first. With no distro installed (every
    GitHub runner) it prints a UTF-16 "Windows Subsystem for Linux has no
    installed distributions" banner and exits non-zero, which is what a naive
    ``subprocess.run(["bash", ...])`` would be talking to. Hooks never run there,
    so resolve Git Bash explicitly and skip when only WSL exists.
    """
    if os.name != "nt":
        return shutil.which("bash")
    roots = [os.environ.get("GIT_INSTALL_ROOT")]
    git = shutil.which("git")
    if git:  # <root>\cmd\git.exe -> <root>
        roots.append(str(Path(git).resolve().parent.parent))
    roots += [r"C:\Program Files\Git", r"C:\Program Files (x86)\Git"]
    for root in roots:
        if not root:
            continue
        for rel in ("bin", "usr/bin"):
            cand = Path(root) / rel / "bash.exe"
            if cand.exists():
                return str(cand)
    found = shutil.which("bash")
    if found and "system32" in found.lower():  # WSL launcher, not the hook shell
        return None
    return found


BASH = _hook_bash()

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="launcher is a bash script (on Windows it runs under Git Bash; WSL bash is not the hook shell)",
)


def _for_bash(p) -> str:
    """A path in a form bash accepts on every OS (``C:/x/y`` for Git Bash)."""
    return Path(p).as_posix() if os.name == "nt" else str(p)


def _make_interpreter_shim(tmp_path: Path) -> Path:
    """A PATH dir whose ``python3`` hands over to the interpreter running pytest.

    The launcher refuses interpreters outside known install locations (an
    anti-PATH-hijack allow-list), and on Windows ``sys.executable`` lives in
    ``C:\\hostedtoolcache``/a venv, which is not on it, so discovery would find
    nothing and the hook would be skipped. The shim sits under the one Windows
    pattern the allow-list accepts for any user
    (``/<drive>/Users/*/AppData/Local/Programs/Python/*``) and execs the real
    interpreter, so discovery, the cache, and the cwd handling all run for real.
    The script must keep LF endings, or ``#!/bin/sh\\r`` fails under Git Bash.
    """
    shim = tmp_path / "AppData" / "Local" / "Programs" / "Python" / "shim"
    shim.mkdir(parents=True, exist_ok=True)
    target = shim / "python3"
    target.write_bytes(
        f'#!/bin/sh\nexec {shlex.quote(_for_bash(sys.executable))} "$@"\n'.encode())
    target.chmod(0o755)
    return shim

PROBE = """\
import json, os, signal, sys
dispositions = {}
for name in ("SIGINT", "SIGTERM", "SIGPIPE", "SIGHUP"):
    sig = getattr(signal, name, None)
    if sig is not None:
        h = signal.getsignal(sig)
        dispositions[name] = h if isinstance(h, int) else str(h)
mask = os.umask(0o022)
os.umask(mask)
print(json.dumps({
    "cwd": os.getcwd(),
    "env_pwd": os.environ.get("PWD"),
    "env_oldpwd": os.environ.get("OLDPWD"),
    "env_ifs": os.environ.get("IFS"),
    "env_cdpath": os.environ.get("CDPATH"),
    "env_lc_all": os.environ.get("LC_ALL"),
    "env_extra": sorted(k for k in os.environ
                        if k.startswith(("_CANON", "_PY_", "_CACHE", "_PYW", "_CAND"))),
    "umask": mask,
    "signals": dispositions,
    "stdin": sys.stdin.read(),
}))
"""


def _run(tmp_path: Path, cwd: Path, *, warm: bool, cache: bool = True,
         extra_env: dict | None = None, stdin: str = "payload-from-host",
         umask: int | None = None) -> dict:
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    shim = _make_interpreter_shim(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if k not in ("OLDPWD", "TOKEN_OPTIMIZER_PYTHON", "IFS", "CDPATH", "LC_ALL")}
    env["HOME"] = _for_bash(home)
    env.pop("XDG_CACHE_HOME", None)
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    if cache:
        # Under HOME: Git Bash only trusts a cache dir confined to HOME/XDG_CACHE_HOME.
        cache_dir = home / "cache"
        cache_dir.mkdir(mode=0o700, exist_ok=True)
        env["TOKEN_OPTIMIZER_PY_CACHE"] = _for_bash(cache_dir)
    else:
        env["TOKEN_OPTIMIZER_PY_CACHE"] = ""  # the launcher's off switch
    env.update(extra_env or {})

    def go() -> subprocess.CompletedProcess:
        def prep():
            if umask is not None:
                os.umask(umask)
        return subprocess.run([BASH, _for_bash(LAUNCHER), _for_bash(probe)], cwd=cwd, env=env,
                              input=stdin, capture_output=True, text=True,
                              timeout=60, preexec_fn=prep if umask is not None else None)

    def check(done: subprocess.CompletedProcess) -> None:
        assert done.stdout.lstrip().startswith("{"), (
            f"launcher did not run the probe (rc={done.returncode}): "
            f"stdout={done.stdout!r} stderr={done.stderr!r}")

    if warm:
        first = go()
        check(first)
        if cache:
            assert any((home / "cache").iterdir()), (
                "the first run should have written the interpreter cache record the "
                f"second run hits; stderr={first.stderr!r}")
    done = go()
    check(done)
    return json.loads(done.stdout)


def _same_dir(a: str, b: Path) -> bool:
    if os.name == "nt":
        # Git Bash exports $PWD as /c/Users/...; the native form is C:/Users/...
        a = re.sub(r"^/([A-Za-z])/", r"\1:/", a)
    return os.path.realpath(a) == os.path.realpath(str(b))


def _pwd_is(seen: dict, expected: Path) -> bool:
    """$PWD names the expected directory (always on POSIX; when exported on Windows)."""
    if seen["env_pwd"] is None and os.name == "nt":
        return True  # MSYS decides what native children inherit; cwd is asserted separately
    return seen["env_pwd"] is not None and _same_dir(seen["env_pwd"], expected)


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_interpreter_starts_in_callers_cwd(tmp_path, warm):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm)
    assert _same_dir(seen["cwd"], proj), seen["cwd"]
    assert _pwd_is(seen, proj), seen["env_pwd"]


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_cwd_with_spaces_is_preserved(tmp_path, warm):
    proj = tmp_path / "my project (v2)" / "sub dir"
    proj.mkdir(parents=True)
    seen = _run(tmp_path, proj, warm=warm)
    assert _same_dir(seen["cwd"], proj), seen["cwd"]
    assert _pwd_is(seen, proj), seen["env_pwd"]


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_cache_disabled_run_keeps_cwd(tmp_path, warm):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm, cache=False)
    assert _same_dir(seen["cwd"], proj), seen["cwd"]


def test_symlinked_cwd_keeps_logical_pwd(tmp_path):
    """A host that launches from a symlinked project dir keeps its logical $PWD."""
    real = tmp_path / "real project"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a directory symlink here: {exc}")
    # Bash only honours an inherited $PWD in its own (POSIX) form.
    env = {} if os.name == "nt" else {"PWD": str(link)}
    seen = _run(tmp_path, link, warm=True, extra_env=env)
    assert _same_dir(seen["cwd"], real)
    if os.name == "nt":
        # MSYS decides how a native symlink and $PWD are rendered; the cwd is what
        # the hook acts on, and it must still be the project.
        assert _pwd_is(seen, real), seen["env_pwd"]
    else:
        assert seen["env_pwd"] == str(link), seen["env_pwd"]


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_oldpwd_is_not_invented(tmp_path, warm):
    """An unset OLDPWD stays unset: the child must not see an exported empty one."""
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm)
    assert seen["env_oldpwd"] is None, repr(seen["env_oldpwd"])


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_no_shell_internals_reach_the_interpreter(tmp_path, warm):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm)
    assert seen["env_ifs"] is None
    assert seen["env_cdpath"] is None
    assert seen["env_lc_all"] is None
    assert seen["env_extra"] == [], seen["env_extra"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="a umask for the child (preexec_fn) and SIGHUP/SIGTERM dispositions are POSIX "
           "process state; a native Windows interpreter has neither",
)
@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_umask_and_signal_dispositions_are_untouched(tmp_path, warm):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm, umask=0o027)
    assert seen["umask"] == 0o027, oct(seen["umask"])
    # Python's own defaults: SIGINT -> default_int_handler, others SIG_DFL (0).
    assert seen["signals"]["SIGTERM"] == int(signal.SIG_DFL)
    assert seen["signals"]["SIGHUP"] == int(signal.SIG_DFL)


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_hook_stdin_reaches_the_interpreter(tmp_path, warm):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm, stdin='{"hook": "payload"}')
    assert seen["stdin"] == '{"hook": "payload"}'


def test_override_interpreter_keeps_cwd(tmp_path):
    """The TOKEN_OPTIMIZER_PYTHON branch never canonicalises, but pin it anyway."""
    proj = tmp_path / "proj dir"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=False,
                extra_env={"TOKEN_OPTIMIZER_PYTHON": _for_bash(sys.executable)})
    assert _same_dir(seen["cwd"], proj)
