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
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="launcher is a bash script"
)

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
    probe.write_text(PROBE)
    cache_dir = tmp_path / "cache"
    env = {k: v for k, v in os.environ.items()
           if k not in ("OLDPWD", "TOKEN_OPTIMIZER_PYTHON", "IFS", "CDPATH", "LC_ALL")}
    if cache:
        cache_dir.mkdir(mode=0o700, exist_ok=True)
        env["TOKEN_OPTIMIZER_PY_CACHE"] = str(cache_dir)
    else:
        env.pop("TOKEN_OPTIMIZER_PY_CACHE", None)
        env["HOME"] = str(tmp_path / "nohome-does-not-exist")
        env.pop("XDG_CACHE_HOME", None)
    env.update(extra_env or {})

    def go() -> subprocess.CompletedProcess:
        def prep():
            if umask is not None:
                os.umask(umask)
        return subprocess.run(["bash", str(LAUNCHER), str(probe)], cwd=cwd, env=env,
                              input=stdin, capture_output=True, text=True,
                              timeout=60, preexec_fn=prep if umask is not None else None)

    if warm:
        first = go()
        assert first.stdout.strip(), first.stderr
    done = go()
    assert done.stdout.strip(), f"launcher printed nothing: {done.stderr!r}"
    return json.loads(done.stdout)


def _same_dir(a: str, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(str(b))


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_interpreter_starts_in_callers_cwd(tmp_path, warm):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = _run(tmp_path, proj, warm=warm)
    assert _same_dir(seen["cwd"], proj), seen["cwd"]
    assert _same_dir(seen["env_pwd"], proj), seen["env_pwd"]


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_cwd_with_spaces_is_preserved(tmp_path, warm):
    proj = tmp_path / "my project (v2)" / "sub dir"
    proj.mkdir(parents=True)
    seen = _run(tmp_path, proj, warm=warm)
    assert _same_dir(seen["cwd"], proj), seen["cwd"]
    assert _same_dir(seen["env_pwd"], proj), seen["env_pwd"]


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
    link.symlink_to(real)
    env = {"PWD": str(link)}
    seen = _run(tmp_path, link, warm=True, extra_env=env)
    assert _same_dir(seen["cwd"], real)
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
    py = shutil.which("python3")
    if not py:
        pytest.skip("no python3 on PATH")
    seen = _run(tmp_path, proj, warm=False, extra_env={"TOKEN_OPTIMIZER_PYTHON": py})
    assert _same_dir(seen["cwd"], proj)
