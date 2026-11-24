"""The detached dashboard self-heal is bounded in time and memory, then backs off."""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _measure import selfheal_guard  # noqa: E402


def test_time_ceiling(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SELFHEAL_MAX_SECONDS", "600")
    assert selfheal_guard.check(Path("."), started=0, now=599, rss_mb=10) is None
    assert selfheal_guard.check(Path("."), started=0, now=601, rss_mb=10).startswith("time")


def test_memory_ceiling(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SELFHEAL_MAX_MB", "1536")
    assert selfheal_guard.check(Path("."), started=0, now=1, rss_mb=1500) is None
    assert selfheal_guard.check(Path("."), started=0, now=1, rss_mb=1600).startswith("memory")


def test_bad_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SELFHEAL_MAX_SECONDS", "nope")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SELFHEAL_MAX_MB", "-5")
    assert selfheal_guard.max_seconds() == 600
    assert selfheal_guard.max_mb() == 1536


def test_peak_rss_is_plausible():
    rss = selfheal_guard.peak_rss_mb()
    if rss is not None:
        assert 1 < rss < 100000


def test_backoff_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SELFHEAL_BACKOFF_SECONDS", "100")
    assert not selfheal_guard.backoff_active(tmp_path)
    (tmp_path / selfheal_guard.BACKOFF_NAME).write_text("time\n")
    assert selfheal_guard.backoff_active(tmp_path)
    assert not selfheal_guard.backoff_active(tmp_path, now=time.time() + 101)
    selfheal_guard.clear_backoff(tmp_path)
    assert not selfheal_guard.backoff_active(tmp_path)


def test_claim_refuses_while_backoff_is_active(tmp_path):
    from types import SimpleNamespace
    from _measure import dashboard_heal_claim
    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path, os=os, time=time,
                             _dashboard_selfheal_inflight=lambda: False,
                             _DASHBOARD_HEAL_LOCK_NAME="dashboard.heal.lock",
                             _DASHBOARD_HEAL_LOCK_STALE_SECONDS=600)
    (tmp_path / selfheal_guard.BACKOFF_NAME).write_text("memory\n")
    assert dashboard_heal_claim.claim(facade) is False
    assert not (tmp_path / "dashboard.heal.lock").exists()
    selfheal_guard.clear_backoff(tmp_path)
    assert dashboard_heal_claim.claim(facade) is True


@pytest.mark.parametrize("cap_env, reason", [
    ({"TOKEN_OPTIMIZER_SELFHEAL_MAX_SECONDS": "1"}, "time"),
    ({"TOKEN_OPTIMIZER_SELFHEAL_MAX_MB": "200"}, "memory"),
])
def test_runaway_child_is_stopped_and_backs_off(tmp_path, cap_env, reason):
    if reason == "memory" and selfheal_guard.peak_rss_mb() is None:
        pytest.skip("no peak-memory reading on this platform")
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(SCRIPTS)!r})
        from pathlib import Path
        from _measure import selfheal_guard
        selfheal_guard.start(Path({str(tmp_path)!r}))
        hog = []
        for _ in range(300):
            if {reason!r} == "memory":
                hog.append(bytearray(20 * 1024 * 1024))
                hog[-1][::4096] = b"x" * len(hog[-1][::4096])
            time.sleep(0.1)
        print("finished")
    """)
    env = dict(os.environ, **cap_env)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3, proc.stderr
    assert "finished" not in proc.stdout
    assert time.monotonic() - started < 20
    marker = tmp_path / selfheal_guard.BACKOFF_NAME
    assert marker.read_text().startswith(reason)
