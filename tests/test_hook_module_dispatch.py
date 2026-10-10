#!/usr/bin/env python3
"""Regression tests for run.py dispatching hook scripts via module_runner.py.

Root cause: CPython never checks or writes __pycache__ bytecode for a script
run as __main__, only for imported modules. run.py used to do
``[sys.executable, str(script_path), *args]`` -- script mode -- so every hook
invocation recompiled the target from source. For measure.py (35k+ lines)
that cost ~0.3s of pure parse/compile on nearly every tool call, on top of
whatever the hook actually does. run.py now dispatches through
module_runner.py, which runs the target as a module so the import system's
bytecode cache applies (measured: script mode ~0.3s per call, module mode
~0.1s after the first call).

Covered:
- Dispatch still runs the target script correctly and forwards args/stdin.
- A second invocation reuses the compiled __pycache__ entry (no recompile).
- A same-named decoy module in the invoking cwd cannot shadow the real one
  (module_runner.py strips '' / '.' from sys.path before inserting scripts_dir).

Run: python3 -m pytest tests/test_hook_module_dispatch.py -v
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"

_DUMMY_HOOK_SRC = '''\
import sys
print("real:" + " ".join(sys.argv[1:]))
'''

_DECOY_HOOK_SRC = '''\
print("decoy")
'''

def _make_plugin_root(tmp_path):
    """Build a minimal plugin root: hooks/{run.py,module_runner.py} + scripts/dummy_hook.py."""
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "hooks" / "run.py").write_text((HOOKS / "run.py").read_text())
    (root / "hooks" / "module_runner.py").write_text((HOOKS / "module_runner.py").read_text())
    (root / "scripts" / "dummy_hook.py").write_text(_DUMMY_HOOK_SRC)
    return root


def _run_hook(root, *args, cwd=None, plugin_data=None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(root)
    # Isolated, config-free data dir so the consent gate always fails open,
    # regardless of the host machine's real ~/.claude/token-optimizer state.
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data or (root / "_data"))
    return subprocess.run(
        [sys.executable, str(root / "hooks" / "run.py"), "scripts/dummy_hook.py", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_dispatch_runs_target_and_forwards_args(tmp_path):
    root = _make_plugin_root(tmp_path)
    result = _run_hook(root, "--quiet", "foo")
    assert result.returncode == 0
    assert result.stdout.strip() == "real:--quiet foo"


def test_second_invocation_reuses_pycache_no_recompile(tmp_path):
    root = _make_plugin_root(tmp_path)

    _run_hook(root, "--quiet")
    pyc_candidates = list((root / "scripts" / "__pycache__").glob("dummy_hook.*.pyc"))
    assert pyc_candidates, "module-mode dispatch should populate __pycache__"
    first_mtime = pyc_candidates[0].stat().st_mtime_ns

    _run_hook(root, "--quiet")
    second_mtime = pyc_candidates[0].stat().st_mtime_ns
    assert second_mtime == first_mtime, (
        "second invocation rewrote the .pyc -- bytecode cache was not reused"
    )


def test_cwd_decoy_module_cannot_shadow_real_one(tmp_path):
    root = _make_plugin_root(tmp_path)

    decoy_cwd = tmp_path / "victim_project"
    decoy_cwd.mkdir()
    (decoy_cwd / "dummy_hook.py").write_text(_DECOY_HOOK_SRC)

    result = _run_hook(root, "--quiet", cwd=str(decoy_cwd))
    assert result.returncode == 0
    assert result.stdout.strip() == "real:--quiet", (
        f"expected the plugin's own dummy_hook.py to run, got: {result.stdout!r}"
    )


def test_no_args_is_a_quiet_noop():
    """run.py with no script arg must exit 0 without invoking a Python subprocess at all."""
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(HOOKS / "run.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout == ""
