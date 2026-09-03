#!/usr/bin/env python3
"""Daemon uninstall sweeps all token-optimizer-* identities (issue #106).

Root cause: daemon paths derive from one module-level ``SNAPSHOT_DIR`` (the
resolved identity), but multiple installs create multiple
``token-optimizer-*`` identities. An identity-scoped uninstall leaves the
sibling's ``dashboard-server.py`` + ``0600 daemon-token`` (a live local HTTP
daemon plus its CSRF secret) on disk, and a scheduler registration whose data
dir already vanished stays registered.

The fix makes daemon uninstall identity-sweeping: it removes the per-identity
daemon files from EVERY ``token-optimizer-*`` identity, unregisters every
runtime's scheduler artifact by name, and reports per-identity what was
removed. ``--this-install-only`` opts out (cleans only the resolved identity)
for a user intentionally running side-by-side installs.

These tests mock subprocess and the plugin-env enumeration so no real
launchctl/schtasks/systemctl runs and no real plugin-data dir is touched.

Run directly:  python3 tests/test_daemon_uninstall_identity_sweep.py
Exits non-zero on first failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import plugin_env  # noqa: E402

# POSIX-only: every test drives _uninstall_launchd_daemon (launchctl +
# os.getuid) and 0600 daemon-token modes -- Unix semantics Windows cannot
# honor. Skip the whole file on Windows; macOS/Linux run it unchanged.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX daemon semantics (launchctl/os.kill/file modes)",
)


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _no_op_run(*a, **k):
    """Fake subprocess.run that never registers/deletes a real task."""
    return _Completed(returncode=1, stdout="", stderr="not found")


def _setup_two_identities(tmp_path, monkeypatch):
    """Create two token-optimizer-* identities, each with daemon artifacts.

    Returns (data_base, ident_a, ident_b, snap_a, snap_b). Monkeypatches
    measure.py + plugin_env so the sweeping uninstall sees both identities
    and writes into the temp tree only.
    """
    data_base = tmp_path / "plugins" / "data"
    ident_a = data_base / "token-optimizer-marketplace"
    ident_b = data_base / "token-optimizer-inline"
    snap_a = ident_a / "data"
    snap_b = ident_b / "data"
    for snap in (snap_a, snap_b):
        snap.mkdir(parents=True)
        (snap / "dashboard-server.py").write_text("# daemon\n", encoding="utf-8")
        (snap / "daemon-token").write_text("csrf-secret-0600", encoding="utf-8")
        (snap / "dashboard-host").write_text("127.0.0.1", encoding="utf-8")
        (snap / ".daemon-thrash").write_text("", encoding="utf-8")

    # Point plugin_env enumeration at the temp base.
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_BASE", data_base)
    monkeypatch.setattr(plugin_env, "_INSTALLED_PLUGINS", data_base.parent / "installed_plugins.json")
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_ENV_VARS", ("CLAUDE_PLUGIN_DATA",))
    plugin_env.resolve_plugin_data_dir.cache_clear()

    # measure.py imported _all_plugin_data_dirs by value at import time, so
    # repoint the measure module's binding too.
    monkeypatch.setattr(measure, "_all_plugin_data_dirs", plugin_env._all_plugin_data_dirs)

    # Resolved identity = ident_a (marketplace). Pin SNAPSHOT_DIR + the
    # derived module-level paths so tombstone + per-identity resolution land
    # in the temp tree.
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", snap_a)
    monkeypatch.setattr(measure, "DAEMON_TOKEN_PATH", snap_a / "daemon-token")
    monkeypatch.setattr(measure, "DAEMON_HOST_PATH", snap_a / "dashboard-host")
    monkeypatch.setattr(measure, "DAEMON_THRASH_BREADCRUMB", snap_a / ".daemon-thrash")
    monkeypatch.setattr(measure, "DAEMON_LOG_DIR", snap_a / "logs")
    # LaunchAgents dir -> temp so no real plist is touched.
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setattr(measure, "LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(measure, "PLIST_PATH", launch_agents / "com.token-optimizer.dashboard.plist")
    # Write a plist so the scheduler-removal path is exercised.
    measure.PLIST_PATH.write_text("<plist/>", encoding="utf-8")

    # No real launchctl/schtasks/systemctl.
    monkeypatch.setattr(measure.subprocess, "run", _no_op_run)
    # Tombstone write must not escape the temp tree (it mkdirs SNAPSHOT_DIR).
    return data_base, ident_a, ident_b, snap_a, snap_b


def _sibling_snap():
    """The non-resolved identity's snapshot dir (the one that should survive
    under --this-install-only and be cleaned under sweep-all)."""
    for d in measure._daemon_identity_snapshot_dirs(False):
        if d != measure.SNAPSHOT_DIR:
            return d
    raise AssertionError("no sibling identity found")


def test_sweep_all_cleans_both_identities(tmp_path, monkeypatch, capsys):
    _setup_two_identities(tmp_path, monkeypatch)
    measure._uninstall_launchd_daemon(this_install_only=False)
    out = capsys.readouterr().out
    # Both identities' per-identity files are gone.
    assert not (measure.SNAPSHOT_DIR / "dashboard-server.py").exists()
    # The sibling identity (resolved from plugin_env enumeration) is cleaned.
    sibling_snap = _sibling_snap()
    assert not (sibling_snap / "dashboard-server.py").exists()
    assert not (sibling_snap / "daemon-token").exists()
    assert not (sibling_snap / "dashboard-host").exists()
    # The shared per-runtime plist is removed.
    assert not measure.PLIST_PATH.exists()
    # Per-identity report is printed.
    assert "Swept all token-optimizer-* identities" in out


def test_this_install_only_leaves_sibling_intact(tmp_path, monkeypatch, capsys):
    _setup_two_identities(tmp_path, monkeypatch)
    sibling_snap = _sibling_snap()  # capture before uninstall
    measure._uninstall_launchd_daemon(this_install_only=True)
    # Resolved identity cleaned.
    assert not (measure.SNAPSHOT_DIR / "dashboard-server.py").exists()
    assert not (measure.SNAPSHOT_DIR / "daemon-token").exists()
    # Sibling identity untouched.
    assert (sibling_snap / "dashboard-server.py").exists(), "sibling was cleaned under --this-install-only"
    assert (sibling_snap / "daemon-token").exists()
    out = capsys.readouterr().out
    assert "Swept all" not in out


def test_sweep_never_touches_paths_outside_plugin_data_base(tmp_path, monkeypatch):
    data_base, ident_a, ident_b, snap_a, snap_b = _setup_two_identities(tmp_path, monkeypatch)
    # A sentinel file OUTSIDE the plugin-data base must survive.
    outside = tmp_path / "outside-sentinel.txt"
    outside.write_text("keep me", encoding="utf-8")
    measure._uninstall_launchd_daemon(this_install_only=False)
    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "keep me"
    # The data base parent still contains only our two identities.
    siblings = sorted(p.name for p in data_base.iterdir())
    assert siblings == ["token-optimizer-inline", "token-optimizer-marketplace"]


def test_nothing_to_remove_honesty_preserved(tmp_path, monkeypatch, capsys):
    """When no artifacts exist anywhere, the honesty header stands."""
    data_base = tmp_path / "plugins" / "data"
    ident_a = data_base / "token-optimizer-marketplace"
    snap_a = ident_a / "data"
    snap_a.mkdir(parents=True)
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_BASE", data_base)
    monkeypatch.setattr(plugin_env, "_INSTALLED_PLUGINS", data_base.parent / "installed_plugins.json")
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_ENV_VARS", ("CLAUDE_PLUGIN_DATA",))
    plugin_env.resolve_plugin_data_dir.cache_clear()
    monkeypatch.setattr(measure, "_all_plugin_data_dirs", plugin_env._all_plugin_data_dirs)
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", snap_a)
    monkeypatch.setattr(measure, "DAEMON_TOKEN_PATH", snap_a / "daemon-token")
    monkeypatch.setattr(measure, "DAEMON_HOST_PATH", snap_a / "dashboard-host")
    monkeypatch.setattr(measure, "DAEMON_THRASH_BREADCRUMB", snap_a / ".daemon-thrash")
    monkeypatch.setattr(measure, "DAEMON_LOG_DIR", snap_a / "logs")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setattr(measure, "LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(measure, "PLIST_PATH", launch_agents / "com.token-optimizer.dashboard.plist")
    monkeypatch.setattr(measure.subprocess, "run", _no_op_run)
    measure._uninstall_launchd_daemon(this_install_only=False)
    out = capsys.readouterr().out
    assert "Nothing to remove" in out
    assert "Deleted:" not in out


def test_ports_to_reclaim_scope():
    """Sweep-all must reclaim EVERY runtime's port, not just the
    resolved runtime's, or a sibling daemon + live CSRF token survives."""
    all_ports = set(measure._daemon_ports_to_reclaim(this_install_only=False))
    assert all_ports == {24842, 24843, 24844, 24845, 24846, 24847, 24848}
    scoped = measure._daemon_ports_to_reclaim(this_install_only=True)
    assert scoped == (measure.DAEMON_PORT,)


def test_sweep_all_reclaims_every_runtime_port(tmp_path, monkeypatch):
    """The uninstall must SIGTERM-reclaim all five runtime ports on sweep-all so
    a sibling runtime's running daemon (e.g. Codex on 24843) does not keep
    serving its token-authed API after a reported uninstall."""
    _setup_two_identities(tmp_path, monkeypatch)
    reclaimed = []
    monkeypatch.setattr(
        measure, "_reclaim_posix_daemon_port",
        lambda port=measure.DAEMON_PORT, **k: reclaimed.append(port),
    )
    measure._uninstall_launchd_daemon(this_install_only=False)
    assert set(reclaimed) == {24842, 24843, 24844, 24845, 24846, 24847, 24848}


def test_this_install_only_reclaims_resolved_port_only(tmp_path, monkeypatch):
    _setup_two_identities(tmp_path, monkeypatch)
    reclaimed = []
    monkeypatch.setattr(
        measure, "_reclaim_posix_daemon_port",
        lambda port=measure.DAEMON_PORT, **k: reclaimed.append(port),
    )
    measure._uninstall_launchd_daemon(this_install_only=True)
    assert reclaimed == [measure.DAEMON_PORT]


def test_reclaim_of_one_port_never_aborts_the_others(tmp_path, monkeypatch):
    """A raise while reclaiming one port must not skip the remaining ports."""
    _setup_two_identities(tmp_path, monkeypatch)
    seen = []

    def flaky(port=measure.DAEMON_PORT, **k):
        seen.append(port)
        if port == 24843:
            raise RuntimeError("boom")

    monkeypatch.setattr(measure, "_reclaim_posix_daemon_port", flaky)
    measure._uninstall_launchd_daemon(this_install_only=False)
    assert set(seen) == {24842, 24843, 24844, 24845, 24846, 24847, 24848}


if __name__ == "__main__":
    import tempfile

    class _Monkey:
        def __init__(self):
            self._saved = {}

        def setattr(self, target, name, value):
            if target not in self._saved:
                self._saved[target] = {}
            self._saved[target][name] = getattr(target, name)
            setattr(target, name, value)

        def setenv(self, k, v):
            import os
            self._saved.setdefault(os, {})
            self._saved[os][k] = os.environ.get(k)
            os.environ[k] = v

        def undo(self):
            import os
            for target, attrs in self._saved.items():
                if target is os:
                    for k, v in attrs.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                else:
                    for name, val in attrs.items():
                        setattr(target, name, val)

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        mp = _Monkey()
        try:
            with tempfile.TemporaryDirectory() as td:
                if "capsys" in t.__code__.co_varnames:
                    import io
                    import contextlib
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        t(Path(td), mp)
                    t._last_out = buf.getvalue()
                else:
                    t(Path(td), mp)
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
