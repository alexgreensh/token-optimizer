import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_status_bar_command import sb, SID_A


def test_live_refresh_is_not_reclaimed_for_age(sb):
    token = sb._status_bar_acquire_lock(SID_A)
    assert token
    lock = sb._status_bar_lock_path(SID_A)
    old = time.time() - sb._STATUS_BAR_LOCK_STALE_S - 10
    os.utime(lock, (old, old))
    assert sb._status_bar_acquire_lock(SID_A) is None
    assert sb._status_bar_release_lock(SID_A, token)


def test_wrong_owner_release_never_opens_acquisition_gap(sb, monkeypatch):
    token = sb._status_bar_acquire_lock(SID_A)
    assert token
    original = sb.os.rename
    acquired = []

    def race(src, dst):
        original(src, dst)
        acquired.append(sb._status_bar_acquire_lock(SID_A))

    monkeypatch.setattr(sb.os, "rename", race)
    assert not sb._status_bar_release_lock(SID_A, "wrong-token")
    assert not any(acquired)


def test_child_cannot_adopt_a_replaced_marker(sb):
    from _measure import status_bar_lock
    token = sb._status_bar_acquire_lock(SID_A)
    assert token
    assert not status_bar_lock.adopt(sb, SID_A, "replaced-token")
    assert sb._status_bar_release_lock(SID_A, token)


@pytest.mark.skipif(os.name == "nt", reason="requires SIGKILL")
def test_child_adopts_ownership_before_work_and_dead_child_is_reclaimed(sb, tmp_path):
    from _measure import status_bar_lock
    token = sb._status_bar_acquire_lock(SID_A)
    lock = sb._status_bar_lock_path(SID_A)
    code = """
import sys, time
from pathlib import Path
from _measure import status_bar_lock
marker = Path(sys.argv[1])
class F:
    @staticmethod
    def _status_bar_lock_path(sid): return marker
assert status_bar_lock.adopt(F, 'same', sys.argv[2])
Path(sys.argv[3]).touch()
while True: time.sleep(.1)
"""
    scripts = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
    child = subprocess.Popen([sys.executable, "-c", code, str(lock), token, str(tmp_path / "ready")],
                             env=dict(os.environ, PYTHONPATH=str(scripts)))
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path / "ready").exists()
        assert lock.read_text().split() == [token, str(child.pid)]
        old = time.time() - sb._STATUS_BAR_LOCK_STALE_S - 10
        os.utime(lock, (old, old))
        assert status_bar_lock.acquire(sb, SID_A) is None
        child.kill()
        child.wait(timeout=5)
        replacement = status_bar_lock.acquire(sb, SID_A)
        assert replacement
        assert status_bar_lock.release(sb, SID_A, replacement)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="requires SIGKILL")
def test_eight_processes_have_one_owner_and_killed_owner_is_reclaimed(sb, tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
    code = """
import os, sys, time
from pathlib import Path
from _measure import status_bar_lock
root = Path(sys.argv[1])
class F:
    _STATUS_BAR_LOCK_STALE_S = 120
    @staticmethod
    def _status_bar_lock_path(sid): return root / 'refresh.lock'
deadline = time.monotonic() + 10
while not (root / 'start').exists() and time.monotonic() < deadline: time.sleep(.01)
token = status_bar_lock.acquire(F, 'same')
(root / ('result-' + sys.argv[2])).write_text(token or '')
while token and not (root / 'finish').exists() and time.monotonic() < deadline: time.sleep(.01)
if token: status_bar_lock.release(F, 'same', token)
"""
    env = dict(os.environ, PYTHONPATH=str(scripts))
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)], env=env)
                for i in range(8)]
    try:
        (tmp_path / "start").touch()
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("result-*"))) < 8 and time.monotonic() < deadline:
            time.sleep(0.01)
        results = [(tmp_path / ("result-%d" % i)).read_text() for i in range(8)]
        winners = [i for i, token in enumerate(results) if token]
        assert len(winners) == 1
        children[winners[0]].kill()
        children[winners[0]].wait(timeout=5)
        marker = tmp_path / "refresh.lock"
        old = time.time() - 130
        os.utime(marker, (old, old))
        from _measure import status_bar_lock
        class Facade:
            _STATUS_BAR_LOCK_STALE_S = 120
            @staticmethod
            def _status_bar_lock_path(sid): return marker
        token = status_bar_lock.acquire(Facade, "same")
        assert token
        assert status_bar_lock.release(Facade, "same", token)
    finally:
        (tmp_path / "finish").touch()
        for child in children:
            child.wait(timeout=10)
