import os
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
sys.path.insert(0, str(SCRIPTS))


def test_collect_sweeps_only_old_root_candidates(monkeypatch, tmp_path):
    import measure

    root = tmp_path / "data"
    cache = tmp_path / "cache"
    root.mkdir()
    cache.mkdir()
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", root)
    monkeypatch.setattr(measure, "QUALITY_CACHE_DIR", cache)
    monkeypatch.setattr(measure, "_QLEASE_SWEEP_MARKER", cache / "sweep")

    code = (
        "from hook_runtime import LeaseLock; import sys,time; "
        "lock=LeaseLock(sys.argv[1], acquire_timeout=0); "
        "assert lock.acquire(); print('locked', flush=True); time.sleep(60)"
    )
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    for name in (".archive-cleanup.lease", ".cohort_tripwire.lock.lease"):
        proc = subprocess.Popen([sys.executable, "-c", code, str(root / name)],
                                env=env, stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "locked"
        finally:
            proc.kill()
            proc.wait(timeout=5)
    candidates = list(root.glob("..*.candidate-*"))
    assert len(candidates) == 2
    old = time.time() - 90000
    for candidate in candidates:
        os.utime(candidate, (old, old))
    fresh = root / ("..archive-cleanup.lease.candidate-" + "a" * 32)
    fresh.write_text("fresh")
    unrelated = root / ("..unrelated.lease.candidate-" + "b" * 32)
    unrelated.write_text("unrelated")
    os.utime(unrelated, (old, old))

    measure.maybe_sweep_stale_leases()
    assert all(not candidate.exists() for candidate in candidates)
    assert fresh.exists()
    assert unrelated.exists()


def test_old_candidate_from_live_process_is_preserved(tmp_path):
    from _measure.lease_candidate_cleanup import sweep

    candidate = tmp_path / ("..archive-cleanup.lease.candidate-" + "c" * 32)
    candidate.write_text(json.dumps({"pid": os.getpid()}))
    old = time.time() - 90000
    os.utime(candidate, (old, old))
    assert sweep(tmp_path) == 0
    assert candidate.exists()


def test_windows_cleanup_does_not_send_process_signal(tmp_path, monkeypatch):
    from _measure import lease_candidate_cleanup as cleanup

    candidate = tmp_path / ("..archive-cleanup.lease.candidate-" + "d" * 32)
    candidate.write_text(json.dumps({"pid": os.getpid()}))
    old = time.time() - 90000
    os.utime(candidate, (old, old))

    def forbidden_signal(*args):
        raise AssertionError("Windows must not use os.kill for liveness")

    with monkeypatch.context() as m:
        m.setattr(cleanup.os, "name", "nt")
        m.setattr(cleanup.os, "kill", forbidden_signal)
        cleanup.sweep(tmp_path)
    assert candidate.exists()


def test_windows_unknown_pid_is_preserved_but_missing_pid_is_reaped(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock
    from _measure import lease_candidate_cleanup as cleanup

    kernel = SimpleNamespace(OpenProcess=Mock(return_value=0),
                             GetExitCodeProcess=Mock(), CloseHandle=Mock())
    with monkeypatch.context() as m:
        m.setattr(cleanup.os, "name", "nt")
        m.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
        m.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        assert not cleanup.owner_is_gone(123)
        m.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
        assert cleanup.owner_is_gone(123)
