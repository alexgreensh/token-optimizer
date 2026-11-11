from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import session_store


def test_reset_marker_is_not_visible_half_written(tmp_path, monkeypatch):
    marker = session_store._context_reset_marker("same", tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text("1111.0")
    observed = []
    original = Path.write_text

    def interrupted(path, text, *args, **kwargs):
        if path == marker:
            original(path, text[:4], *args, **kwargs)
            observed.append(session_store.context_reset_at("same", tmp_path))
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(session_store.time, "time", lambda: 1234567890.25)
    monkeypatch.setattr(Path, "write_text", interrupted)
    session_store.mark_context_reset("same", tmp_path)
    assert session_store.context_reset_at("same", tmp_path) == 1234567890.25
    assert all(value in (1111.0, 1234567890.25) for value in observed)


def test_reset_marker_does_not_move_back_when_clock_moves_back(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store.time, "time", lambda: 2000.0)
    session_store.mark_context_reset("same", tmp_path)
    monkeypatch.setattr(session_store.time, "time", lambda: 1000.0)
    session_store.mark_context_reset("same", tmp_path)
    assert session_store.context_reset_at("same", tmp_path) == 2000.0


@pytest.mark.skipif(os.name == "nt", reason="requires SIGKILL")
def test_killed_reset_writer_leaves_whole_marker_and_stale_file_is_cleaned(tmp_path):
    marker = session_store._context_reset_marker("same", tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text("1111.0")
    scripts = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
    code = """
import os, sys, time
from pathlib import Path
import session_store
def pause(src, dst):
    print('ready', flush=True)
    time.sleep(60)
os.replace = pause
session_store.mark_context_reset('same', Path(sys.argv[1]))
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                             env=dict(os.environ, PYTHONPATH=str(scripts)),
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert session_store.context_reset_at("same", tmp_path) == 1111.0
    finally:
        child.kill()
        child.wait(timeout=5)
    pending = list(marker.parent.glob(".context-reset-*.tmp"))
    assert len(pending) == 1
    old = time.time() - 90000
    os.utime(pending[0], (old, old))
    session_store.mark_context_reset("same", tmp_path)
    assert not pending[0].exists()
    assert session_store.context_reset_at("same", tmp_path) >= 1111.0


def test_eight_reset_writers_preserve_highest_timestamp(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
    code = """
import sys, time
from pathlib import Path
import session_store
root = Path(sys.argv[1])
deadline = time.monotonic() + 10
while not (root / 'start').exists() and time.monotonic() < deadline: time.sleep(.01)
session_store.time.time = lambda: float(sys.argv[2])
session_store.mark_context_reset('same', root)
"""
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(2000 + i)],
                                 env=dict(os.environ, PYTHONPATH=str(scripts)),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(8)]
    (tmp_path / "start").touch()
    results = [child.communicate(timeout=10) for child in children]
    assert all(child.returncode == 0 for child in children), results
    assert not any(stderr for _, stderr in results), results
    assert session_store.context_reset_at("same", tmp_path) == 2007.0
