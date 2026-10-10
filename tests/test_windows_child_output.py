"""Finding 7 (sweep r3d): read-cache-stats, structure-proof and read-cache-clear must print on Windows.

The child was spawned with creationflags=_NO_WINDOW and no std streams, so on Windows its
output went to a hidden console or NULL handles. The fix hands it the parent's streams
through the same guard hooks/run.py uses. Not provable without Windows: these tests pin
the arguments given to subprocess.
"""

import importlib.util
import io
import os
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



class _FileLikeStream(io.StringIO):
    """A stream with a working fileno(), like a real console/pipe handle."""

    def fileno(self):
        return 7



# --- Finding 7 ---------------------------------------------------------------


def test_stdio_kwargs_pass_usable_streams_on_windows(measure, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake_in, fake_out, fake_err = _FileLikeStream(), _FileLikeStream(), _FileLikeStream()
    monkeypatch.setattr(sys, "stdin", fake_in)
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    kwargs = measure._windows_stdio_kwargs()
    assert kwargs == {"stdin": fake_in, "stdout": fake_out, "stderr": fake_err}


def test_stdio_kwargs_skip_missing_or_unusable_streams(measure, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", None)  # pythonw: no stdin at all
    monkeypatch.setattr(sys, "stdout", io.StringIO())  # fileno() raises UnsupportedOperation
    good = _FileLikeStream()
    monkeypatch.setattr(sys, "stderr", good)
    assert measure._windows_stdio_kwargs() == {"stderr": good}


def test_stdio_kwargs_are_empty_off_windows(measure, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", _FileLikeStream())
    assert measure._windows_stdio_kwargs() == {}


def _fake_run_recorder(record):
    def fake_run(argv, **kwargs):
        record.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return fake_run


@pytest.mark.parametrize(
    "script_name",
    ["read_cache.py", "structure_replay.py"],
)
def test_script_child_gets_std_streams_on_windows(measure, monkeypatch, script_name):
    """The shared child launcher hands the std streams to the child on Windows."""
    monkeypatch.setattr(sys, "platform", "win32")
    out, err, inn = _FileLikeStream(), _FileLikeStream(), _FileLikeStream()
    monkeypatch.setattr(sys, "stdin", inn)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    record = []
    monkeypatch.setattr(measure.subprocess, "run", _fake_run_recorder(record))
    script = MEASURE_PATH.parent / script_name
    result = measure._run_script_child(script, ["--stats"], timeout=5)
    assert result.returncode == 0
    (argv, kwargs), = record
    assert argv[:2] == [sys.executable, str(script)]
    assert argv[2:] == ["--stats"]
    assert kwargs["stdin"] is inn and kwargs["stdout"] is out and kwargs["stderr"] is err
    assert kwargs["creationflags"] == measure._NO_WINDOW
    assert kwargs["timeout"] == 5


def test_script_child_adds_no_stream_kwargs_off_windows(measure, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", _FileLikeStream())
    record = []
    monkeypatch.setattr(measure.subprocess, "run", _fake_run_recorder(record))
    measure._run_script_child(MEASURE_PATH.parent / "read_cache.py", ["--stats"], timeout=5)
    (_, kwargs), = record
    assert "stdin" not in kwargs and "stdout" not in kwargs and "stderr" not in kwargs


def _cli_source_block(measure_src, command):
    start = measure_src.index(f'elif args[0] == "{command}":')
    nxt = measure_src.index("\n    elif args[0] ==", start + 10)
    return measure_src[start:nxt] if nxt > start else measure_src[start:start + 1500]


@pytest.mark.parametrize("command", ["read-cache-clear", "read-cache-stats"])
def test_cli_branches_use_the_shared_child_launcher(command):
    src = MEASURE_PATH.read_text(encoding="utf-8")
    block = _cli_source_block(src, command)
    assert "_run_script_child(" in block, block
    assert "subprocess.run(" not in block, block


def test_structure_proof_branch_uses_the_shared_child_launcher():
    src = MEASURE_PATH.read_text(encoding="utf-8")
    start = src.index('elif args[0] == "structure-proof":')
    block = src[start:src.index("    else:\n        print(\"Usage:\")", start)]
    assert "_run_script_child(" in block, block
    assert "subprocess.run(" not in block, block


def test_read_cache_stats_still_prints_on_this_platform(tmp_path):
    """End to end through the real CLI: the child's output reaches our stdout."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path / "snap")
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, str(MEASURE_PATH), "read-cache-stats", "--session", "r3d-test"],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert (proc.stdout + proc.stderr).strip(), "read-cache-stats printed nothing"
