import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_concurrency_throttle_claims import _load_measure


def test_live_flush_is_not_reclaimed_due_to_age(tmp_path, monkeypatch):
    measure = _load_measure(tmp_path, monkeypatch)
    first = measure._acquire_session_end_flush_lock()
    assert first is not None
    old = time.time() - 3600
    marker = tmp_path / ".session-end-flush.lock"
    if marker.exists():
        os.utime(marker, (old, old))
        with pytest.raises(OSError):
            marker.rmdir()
    assert measure._acquire_session_end_flush_lock() is None
    measure._release_session_end_flush_lock(first)


@pytest.mark.skipif(os.name == "nt", reason="requires SIGKILL")
def test_eight_flushes_have_one_owner_and_kill_is_reclaimed(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
    sys.path.insert(0, str(scripts))
    code = """
import sys, time
from pathlib import Path
from _measure import session_flush_lock
root = Path(sys.argv[1])
class F: SNAPSHOT_DIR = root
deadline = time.monotonic() + 10
while not (root / 'start').exists() and time.monotonic() < deadline: time.sleep(.01)
lock = session_flush_lock.acquire(F, 120)
(root / ('result-' + sys.argv[2])).write_text('owned' if lock else '')
while lock and not (root / 'finish').exists() and time.monotonic() < deadline: time.sleep(.01)
if lock: session_flush_lock.release(lock)
"""
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)],
                                 env=dict(os.environ, PYTHONPATH=str(scripts))) for i in range(8)]
    try:
        (tmp_path / "start").touch()
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("result-*"))) < 8 and time.monotonic() < deadline:
            time.sleep(.01)
        results = [(tmp_path / ("result-%d" % i)).read_text() for i in range(8)]
        winners = [i for i, value in enumerate(results) if value]
        assert len(winners) == 1
        children[winners[0]].kill()
        children[winners[0]].wait(timeout=5)
        from _measure import session_flush_lock
        class Facade:
            SNAPSHOT_DIR = tmp_path
        lock = session_flush_lock.acquire(Facade, 120)
        assert lock is not None
        session_flush_lock.release(lock)
    finally:
        (tmp_path / "finish").touch()
        for child in children:
            child.wait(timeout=10)
