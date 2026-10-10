"""The launcher runs under ``set -e`` but must never abort before it execs.

HOOK-SAFETY (see the end of hooks/python-launcher.sh): a hook must not exit
non-zero, because Claude Code and Codex treat a failing PreToolUse hook as a
tool-blocking failure. Two paths could still abort the launcher before ``exec``:

* ``rm`` in ``_prune_interpreter_cache`` failing (a record held by antivirus,
  immutable, or otherwise unremovable). The abort came before the cache write,
  so no new record was ever stored and every later hook aborted the same way.
* a ``_exec_*_interpreter`` call returning 1 inside an ``if`` body (where
  ``set -e`` is live), or ``exec`` itself failing (a bad shebang, a file that
  vanished between discovery and exec). Both ended the launcher with a
  non-zero status instead of falling through to the next candidate and the
  documented ``exit 0`` degradation.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="launcher is a bash script"),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="exercises POSIX file semantics (directory-as-record, shebang exec failure)",
    ),
]

PROBE = 'print("PROBE-OK")\n'
# Absolute path: some tests narrow PATH, and the child must still find bash.
BASH = shutil.which("bash") or "bash"


def _env(tmp_path: Path, cache_dir: Path, path: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("OLDPWD", "TOKEN_OPTIMIZER_PYTHON", "CDPATH")}
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    env["TOKEN_OPTIMIZER_PY_CACHE"] = str(cache_dir)
    if path is not None:
        env["PATH"] = path
    return env


def _launch(tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    return subprocess.run([BASH, str(LAUNCHER), str(probe)], cwd=tmp_path, env=env,
                          input="", capture_output=True, text=True, timeout=60)


def _stuck_cache(tmp_path: Path) -> tuple[Path, str]:
    """40 stale records plus a PATH whose ``rm`` always fails.

    A fake ``rm`` is the portable stand-in for a record that is held by
    antivirus, immutable, or on an odd filesystem: whatever the cause, the
    launcher sees ``rm`` return non-zero for the oldest record.
    """
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    for i in range(1, 41):
        rec = cache / f"interpreter-e4-{i}-{i}.cache"
        rec.write_text("x\n")
        os.utime(rec, (1_000_000_000 + i, 1_000_000_000 + i))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_rm = fakebin / "rm"
    fake_rm.write_text("#!/bin/sh\nexit 1\n")
    fake_rm.chmod(0o755)
    return cache, f"{fakebin}{os.pathsep}{os.environ['PATH']}"


def test_unremovable_cache_record_never_aborts_the_launcher(tmp_path):
    """Finding 3: 40 records, ``rm`` fails on the oldest -> still exec."""
    cache, path = _stuck_cache(tmp_path)

    done = _launch(tmp_path, _env(tmp_path, cache, path=path))

    assert done.returncode == 0, done.stderr
    assert "PROBE-OK" in done.stdout, (done.stdout, done.stderr)


def test_unremovable_record_does_not_block_writing_the_new_record(tmp_path):
    """The abort used to land before the cache write, so the cache never healed."""
    cache, path = _stuck_cache(tmp_path)

    _launch(tmp_path, _env(tmp_path, cache, path=path))

    fresh = [p for p in cache.glob("interpreter-e4-*.cache")
             if p.read_text().startswith("INTERP\t")]
    assert fresh, sorted(p.name for p in cache.iterdir())


def _bad_interpreter_dir(tmp_path: Path) -> Path:
    """A PATH dir whose python3 passes every launcher check but cannot exec.

    Owned by us, not group/other-writable (so the POSIX ownership fallback of
    ``_is_safe_prefix`` blesses it), non-empty, executable -- and a shebang that
    names an interpreter that does not exist, so ``exec`` itself fails.
    """
    d = tmp_path / "badbin"
    d.mkdir(mode=0o755)
    fake = d / "python3"
    fake.write_text("#!/nonexistent/interpreter/for/token-optimizer-test\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return d


@pytest.mark.parametrize("warm", [False, True], ids=["cache-miss", "cache-hit"])
def test_failed_exec_of_one_candidate_falls_through_to_the_next(tmp_path, warm):
    """An exec that fails must advance discovery, not end the launcher non-zero."""
    bad = _bad_interpreter_dir(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    env = _env(tmp_path, cache, path=f"{bad}{os.pathsep}{os.environ['PATH']}")
    if warm:
        _launch(tmp_path, env)  # writes the record naming the bad interpreter
    done = _launch(tmp_path, env)

    assert done.returncode == 0, (done.returncode, done.stderr)
    assert "PROBE-OK" in done.stdout, (done.stdout, done.stderr)


def test_failed_exec_with_nothing_else_still_exits_zero(tmp_path):
    """No working interpreter anywhere: the documented degradation is exit 0."""
    bad = _bad_interpreter_dir(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    # Only the bad dir on PATH. The direct-probe fallback may still find a real
    # /usr/bin/python3; either way the launcher must not exit non-zero.
    env = _env(tmp_path, cache, path=str(bad))
    done = _launch(tmp_path, env)
    assert done.returncode == 0, (done.returncode, done.stderr)
