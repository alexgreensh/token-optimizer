"""Finding 10a (sweep r3d): git_context._run_git must pass creationflags=_NO_WINDOW.

Under a detached parent a flag-less console spawn opens a visible window on Windows.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_r3d_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def measure():
    return _load_measure()




# --- Finding 10a -------------------------------------------------------------


def test_git_context_run_git_uses_no_window_flag(measure, monkeypatch, tmp_path):
    record = []

    def fake_run(argv, **kwargs):
        record.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    measure.git_context(as_json=True)
    git_calls = [(a, k) for a, k in record if a and a[0] == "git"]
    assert git_calls, "git_context never called git"
    for argv, kwargs in git_calls:
        assert kwargs.get("creationflags") == measure._NO_WINDOW, argv
