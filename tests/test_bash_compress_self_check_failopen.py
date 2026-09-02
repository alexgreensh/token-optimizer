"""bash_compress self-check availability contract.

The wrapper's argv re-validation gate must be dependency-free at import time
(every whitelisted Bash command pays its startup cost), and when the gate
module cannot be imported at all the wrapper must fail OPEN: the original
command runs uncompressed with its output and exit code relayed intact.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


class _BlockImport:
    """meta_path finder that makes one module unimportable."""

    def __init__(self, name):
        self._name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self._name:
            raise ImportError(f"{self._name} blocked for test")
        return None


def _run_wrapper(args, env_extra=None):
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "bash_compress.py"), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


def test_gate_module_has_no_heavy_imports():
    """The gate module must import only stdlib modules that are already loaded
    by bare interpreter startup, so the wrapper's self-check stays cheap."""
    probe = (
        "import sys; import bash_whitelist; "
        "heavy = [m for m in ('plugin_env', 'runtime_env', 'bash_hook') if m in sys.modules]; "
        "print(','.join(sorted(set(sys.modules) & set(heavy))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60,
        env={"PYTHONPATH": str(SCRIPTS), "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"bash_whitelist pulled in heavy modules: {result.stdout.strip()}"
    )


_BLOCKER = """
import sys
sys.path.insert(0, {scripts!r})
class _B:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'bash_whitelist':
            raise ImportError('blocked')
        return None
sys.meta_path.insert(0, _B())
"""


def test_fail_open_runs_original_when_gate_unavailable():
    """With the gate module unimportable, the wrapper must execute the original
    command (exit code and output relayed), never drop it."""
    probe = _BLOCKER.format(scripts=str(SCRIPTS)) + (
        "sys.argv = ['bash_compress.py', 'echo', 'failopen-ok']\n"
        "import bash_compress as bc\n"
        "bc.main()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={"PATH": __import__("os").environ.get("PATH", "")},
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "failopen-ok" in result.stdout
    assert "not eligible for compression" not in result.stderr


def test_fail_open_relays_nonzero_exit_code():
    """A failing original command keeps its exit code on the fail-open path."""
    probe = _BLOCKER.format(scripts=str(SCRIPTS)) + (
        "sys.argv = ['bash_compress.py', 'python3', '-c', 'import sys; sys.exit(3)']\n"
        "import bash_compress as bc\n"
        "bc.main()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={"PATH": __import__("os").environ.get("PATH", "")},
    )
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)


@pytest.mark.slow
def test_wrapper_startup_budget_relative():
    """Startup budget: importing the wrapper must stay within a small factor of
    importing the bare gate module. Interleaved to cancel machine-load drift;
    the bound is relative so loaded CI machines stay stable. A regression here
    means a heavy import chain crept back into the wrapper's startup path."""
    light = [sys.executable, "-c", "import bash_whitelist"]
    heavy = [sys.executable, "-c", "import bash_compress"]
    env = {"PYTHONPATH": str(SCRIPTS), "PATH": __import__("os").environ.get("PATH", "")}

    def _timed(cmd):
        import time
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, timeout=60, env=env)
        assert r.returncode == 0, r.stderr
        return time.perf_counter() - t0

    # Warm-up (pyc caching, fs cache)
    for _ in range(2):
        _timed(light)
        _timed(heavy)

    light_t, heavy_t = [], []
    for _ in range(7):
        light_t.append(_timed(light))
        heavy_t.append(_timed(heavy))

    light_med = statistics.median(light_t)
    heavy_med = statistics.median(heavy_t)
    ratio = heavy_med / max(light_med, 1e-6)
    # bash_compress is stdlib-only after the gate extraction; a heavy import
    # chain (plugin env resolution) measures 3x+ worse than this bound.
    assert ratio < 4.0, (
        f"bash_compress startup {heavy_med:.3f}s vs bash_whitelist {light_med:.3f}s "
        f"(ratio {ratio:.2f}x): heavy import chain in wrapper startup"
    )
