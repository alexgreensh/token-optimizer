#!/usr/bin/env python3
"""Defense-in-depth guard tests for hooks/module_runner.py (follow-up to PR #85).

module_runner.py dispatches a hook target via runpy.run_module. PR #85 relied on
run.py already sanitizing the path; this pins module_runner's OWN guards so a
future caller that skips run.py can't turn it into an arbitrary-module-execution
primitive: a non-identifier module_name (path separators, dots, traversal) or a
target that doesn't exist in scripts_dir must be refused, fail-open (exit 0),
without executing anything.

Run: python3 -m pytest tests/test_module_runner_guard.py -v
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE_RUNNER = REPO / "hooks" / "module_runner.py"


def _run(scripts_dir, module_name, *args, cwd=None):
    return subprocess.run(
        [sys.executable, str(MODULE_RUNNER), str(scripts_dir), module_name, *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_valid_module_executes(tmp_path):
    (tmp_path / "good_mod.py").write_text('import sys\nprint("ran:" + " ".join(sys.argv[1:]))\n')
    r = _run(tmp_path, "good_mod", "x", "y")
    assert r.returncode == 0
    assert "ran:x y" in r.stdout


def test_non_identifier_module_name_refused(tmp_path):
    # A traversal-shaped name must be rejected before any import happens.
    (tmp_path / "evil.py").write_text('print("SHOULD_NOT_RUN")\n')
    r = _run(tmp_path, "../evil")
    assert r.returncode == 0
    assert "SHOULD_NOT_RUN" not in r.stdout


def test_dotted_module_name_refused(tmp_path):
    (tmp_path / "pkg.py").write_text('print("SHOULD_NOT_RUN")\n')
    r = _run(tmp_path, "os.path")
    assert r.returncode == 0
    assert "SHOULD_NOT_RUN" not in r.stdout


def test_missing_target_refused(tmp_path):
    r = _run(tmp_path, "does_not_exist")
    assert r.returncode == 0
    assert r.stdout == ""


def test_decoy_in_cwd_cannot_be_run_via_traversal(tmp_path):
    # Even if a decoy exists in cwd, a non-identifier name is refused and the
    # real scripts_dir target is what would run for a valid name.
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "decoy.py").write_text('print("DECOY_RAN")\n')
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    r = _run(scripts, "decoy", cwd=cwd)  # decoy is a valid identifier but NOT in scripts_dir
    assert r.returncode == 0
    assert "DECOY_RAN" not in r.stdout  # refused: not present in scripts_dir
