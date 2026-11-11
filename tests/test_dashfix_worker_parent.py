import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))


def _delayed_write(folder):
    folder = Path(folder)
    (folder / 'ready').write_text('ready')
    time.sleep(3)
    (folder / 'finished').write_text('finished')


def _parent(folder):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from _measure.evidence_parallel import watch_parent
    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'), initializer=watch_parent)
    pool.submit(_delayed_write, folder)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (Path(folder) / 'ready').exists():
        time.sleep(.05)
    os._exit(0 if (Path(folder) / 'ready').exists() else 1)


def test_worker_exits_if_parent_is_killed(tmp_path):
    result = subprocess.run([sys.executable, __file__, str(tmp_path)], timeout=15)
    assert result.returncode == 0
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        assert not (tmp_path / 'finished').exists()
        time.sleep(.05)


if __name__ == '__main__':
    _parent(sys.argv[1])
