"""Regression tests for long dashboard self-heals started by hooks."""

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


def _child_env(measure):
    """Keep test children inside the fixture home without host credentials."""
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "PATHEXT", "COMSPEC")
           if key in os.environ}
    env.update({
        "PYTHONPATH": str(SCRIPTS),
        "HOME": str(measure.SNAPSHOT_DIR),
        "USERPROFILE": str(measure.SNAPSHOT_DIR),
        "CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"],
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(measure.SNAPSHOT_DIR),
        "TOKEN_OPTIMIZER_RUNTIME": "claude",
    })
    return env


@pytest.fixture()
def measure(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    config = tmp_path / "claude-config"
    (config / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    module = importlib.import_module("measure")
    yield module
    sys.modules.pop("measure", None)
    sys.path.remove(str(SCRIPTS))


def _try_lock_in_child(measure):
    code = (
        "import measure; "
        "lock = measure._dashboard_selfheal_build_lock(); "
        "print(lock.__enter__()); lock.__exit__(None, None, None)"
    )
    return subprocess.run(
        [sys.executable, "-c", code], env=_child_env(measure), text=True,
        capture_output=True, timeout=30, check=True,
    ).stdout.strip()


def test_rebuild_lock_stays_held_past_marker_window(measure, monkeypatch):
    """A second process must not rebuild even when the 60s launch marker expires."""
    with measure._dashboard_selfheal_build_lock() as acquired:
        assert acquired
        assert measure._dashboard_selfheal_inflight()
        marker = measure.SNAPSHOT_DIR / measure._DASHBOARD_HEAL_LOCK_NAME
        marker.write_text("expired")
        old = time.time() - measure._DASHBOARD_HEAL_LOCK_STALE_SECONDS - 10
        os.utime(marker, (old, old))
        assert not measure._dashboard_heal_spawn_due()
        assert _try_lock_in_child(measure) == "False"
    assert _try_lock_in_child(measure) == "True"
    assert measure._dashboard_heal_spawn_due()


def test_detached_dashboard_cli_skips_when_another_child_is_building(measure, tmp_path):
    flag = tmp_path / "rebuilt.txt"
    code = (
        "import measure; "
        f"measure.generate_standalone_dashboard = lambda **kw: open({str(flag)!r}, 'w').write('ran'); "
        "measure._dispatch_dashboard(['dashboard', '--quiet'])"
    )
    env = _child_env(measure)
    env["TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL"] = "1"
    env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
    with measure._dashboard_selfheal_build_lock() as acquired:
        assert acquired
        subprocess.run([sys.executable, "-c", code], env=env, timeout=30, check=True)
        assert not flag.exists()
    subprocess.run([sys.executable, "-c", code], env=env, timeout=30, check=True)
    assert flag.read_text() == "ran"


def test_rebuild_lock_released_when_child_crashes(measure):
    code = (
        "import os, measure; "
        "lock = measure._dashboard_selfheal_build_lock(); "
        "assert lock.__enter__(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], env=_child_env(measure), timeout=30, check=True)
    with measure._dashboard_selfheal_build_lock() as acquired:
        assert acquired


def test_two_stale_marker_reclaimers_cannot_both_win(measure, monkeypatch):
    marker = measure.SNAPSHOT_DIR / measure._DASHBOARD_HEAL_LOCK_NAME
    marker.write_text('old')
    old = time.time() - measure._DASHBOARD_HEAL_LOCK_STALE_SECONDS - 10
    os.utime(marker, (old, old))
    original = measure.os.unlink
    nested = []

    def race(path):
        if str(path) == str(marker) and not nested:
            nested.append(False)
            nested[0] = measure._dashboard_heal_spawn_due()
        return original(path)

    monkeypatch.setattr(measure.os, 'unlink', race)
    assert measure._dashboard_heal_spawn_due()
    assert not any(nested)


def test_eight_stale_marker_reclaimers_have_one_winner(measure):
    marker = measure.SNAPSHOT_DIR / measure._DASHBOARD_HEAL_LOCK_NAME
    marker.write_text('old')
    old = time.time() - measure._DASHBOARD_HEAL_LOCK_STALE_SECONDS - 10
    os.utime(marker, (old, old))
    code = "import measure; print(measure._dashboard_heal_spawn_due())"
    children = [subprocess.Popen([sys.executable, '-c', code], env=_child_env(measure),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(8)]
    results = [child.communicate(timeout=15) for child in children]
    assert all(child.returncode == 0 for child in children), results
    assert [out.strip() for out, _ in results].count('True') == 1


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires no-follow open")
def test_symlinked_lock_and_backoff_files_are_rejected(measure, tmp_path):
    target = tmp_path / "sentinel"
    target.write_text("unchanged")
    (measure.SNAPSHOT_DIR / "dashboard.selfheal.build.lock").symlink_to(target)
    with measure._dashboard_selfheal_build_lock() as acquired:
        assert not acquired
    assert not measure._dashboard_selfheal_inflight()
    (measure.SNAPSHOT_DIR / "dashboard.hook.backoff").symlink_to(target)
    measure._record_dashboard_hook_timeout()
    assert target.read_text() == "unchanged"


def test_all_spawn_sites_share_guard_and_record_child_pid(measure, monkeypatch):
    children = []

    class FakePopen:
        pid = 43210

        def __init__(self, argv, **kwargs):
            children.append((argv, kwargs))

    monkeypatch.setattr(measure.subprocess, "Popen", FakePopen)
    measure._spawn_detached_dashboard_selfheal(force=True)
    measure._spawn_detached_dashboard_selfheal(force=True)
    assert len(children) == 1
    assert children[0][1]["env"]["TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL"] == "1"
    assert (measure.SNAPSHOT_DIR / measure._DASHBOARD_HEAL_LOCK_NAME).read_text() == "43210"


def test_failed_spawn_releases_launch_marker_for_retry(measure, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("process table full")

    monkeypatch.setattr(measure.subprocess, "Popen", fail)
    measure._spawn_detached_dashboard_selfheal()
    assert not (measure.SNAPSHOT_DIR / measure._DASHBOARD_HEAL_LOCK_NAME).exists()
    assert not measure._dashboard_hook_backoff_active()


def test_hook_timeout_backoff_expires_and_resets_on_version_change(measure, monkeypatch):
    dashboard = measure.SNAPSHOT_DIR / "dashboard.html"
    monkeypatch.setattr(measure, "DASHBOARD_PATH", dashboard)
    measure._record_dashboard_hook_timeout()
    assert not measure._dashboard_hook_backoff_active(), "a failed child must not suppress retries"
    marker = measure.SNAPSHOT_DIR / "dashboard.hook.backoff"
    old = time.time() - 5
    os.utime(marker, (old, old))
    dashboard.write_text("updated")
    assert measure._dashboard_hook_backoff_active()
    future = time.time() + 1
    os.utime(dashboard, (future, future))
    assert measure._dashboard_hook_backoff_active(), "small filesystem clock skew must not rerun hooks"
    future = time.time() + 10
    os.utime(dashboard, (future, future))
    assert not measure._dashboard_hook_backoff_active(), "a far-future timestamp must not pin backoff"
    old = time.time() - measure._DASHBOARD_HOOK_BACKOFF_SECONDS - 1
    os.utime(dashboard, (old, old))
    assert not measure._dashboard_hook_backoff_active()
    measure._record_dashboard_hook_timeout()
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "next-version")
    assert not measure._dashboard_hook_backoff_active()


def test_flush_skips_expensive_dashboard_during_backoff(measure, monkeypatch):
    calls = []
    dashboard = measure.SNAPSHOT_DIR / "dashboard.html"
    monkeypatch.setattr(measure, "DASHBOARD_PATH", dashboard)
    measure._record_dashboard_hook_timeout()
    marker = measure.SNAPSHOT_DIR / "dashboard.hook.backoff"
    old = time.time() - 5
    os.utime(marker, (old, old))
    dashboard.write_text("updated")
    monkeypatch.setattr(measure, "_acquire_session_end_flush_lock", lambda: object())
    monkeypatch.setattr(measure, "_release_session_end_flush_lock", lambda _: None)
    monkeypatch.setattr(measure, "_install_hook_budget", lambda _: type("Budget", (), {"remaining": lambda self: 20})())
    monkeypatch.setattr(measure, "_clear_hook_budget", lambda _: None)
    monkeypatch.setattr(measure, "_session_refresh_due", lambda: True)
    monkeypatch.setattr(measure, "collect_sessions", lambda **kwargs: calls.append("collect"))
    monkeypatch.setattr(measure, "generate_standalone_dashboard", lambda **kwargs: calls.append("dashboard"))
    monkeypatch.setattr(measure, "compact_capture", lambda **kwargs: None)
    monkeypatch.setattr(measure, "_cleanup_quality_cache", lambda: None)
    monkeypatch.setattr(measure, "reclaim_orphaned_plugin_data_dirs", lambda: None)
    monkeypatch.setattr(measure, "_rotate_checkpoint_events", lambda: None)
    monkeypatch.setattr(measure, "_prune_trends_db", lambda: None)
    monkeypatch.setattr(measure, "_run_post_flush_extensions", lambda **kwargs: None)
    measure._run_session_end_flush_worker([])
    assert calls == ["collect"]
