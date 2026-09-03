#!/usr/bin/env python3
"""The uninstall tombstone must OUTLIVE the uninstall.

Root cause of the regression these tests pin:
``_uninstall_*_daemon`` wrote the adv-006 ``.daemon-thrash`` tombstone first
(so a racing respawn exits cleanly), then UNLINKED it again at the end of the
per-identity sweep. Each generated ``dashboard-server.py`` bakes in its own
identity's breadcrumb path and checks it on start::

    if os.path.exists(<breadcrumb>): return "noop-tombstoned"

so deleting the breadcrumb re-armed daemon self-revive: an orphaned
LaunchAgent/unit could resurrect the daemon after a "successful" uninstall and
mint a fresh 0600 CSRF token.

Contract pinned here:
  1. After uninstall, EVERY swept identity has a tombstone (not just the
     active one) -- a sibling's orphaned daemon must stay dead too.
  2. ``--this-install-only`` still tombstones the identity it did sweep.
  3. A legitimate reinstall is the ONLY thing that clears the UNINSTALL
     tombstone (``setup_daemon`` unlinks it before starting), so uninstall
     never re-arms revive.

The ``.daemon-thrash`` breadcrumb has two kinds
with opposite lifetimes. The UNINSTALL tombstone (content ==
``_UNINSTALL_TOMBSTONE_MARKER``) is permanent -- the generated daemon honors it
forever, so a surviving daemon script cannot self-clear it after 60s and mint a
fresh CSRF token post-uninstall. The 3-strikes THRASH tombstone (an EMPTY file)
still self-heals after 60s once the dashboard is back. The runtime tests below
pin both behaviors against the ACTUAL generated ``_thrash_check_and_update``.

Run: python3 -m pytest tests/test_daemon_uninstall_tombstone_persists.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
TOMBSTONE = ".daemon-thrash"

# POSIX-only: the uninstall path (_uninstall_launchd_daemon -> launchctl +
# os.getuid) and 0600 token modes are Unix semantics Windows cannot honor.
# Skip the whole file on Windows; macOS/Linux run it unchanged.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX daemon semantics (launchctl/os.kill/file modes)",
)


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    """Import measure.py with SNAPSHOT_DIR pinned into tmp_path."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "active" / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def _seed_identity(snap_dir: Path) -> None:
    """A daemon-bearing identity: script + 0600 token + host, no tombstone."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "dashboard-server.py").write_text("# daemon\n", encoding="utf-8")
    token = snap_dir / "daemon-token"
    token.write_text("secret\n", encoding="utf-8")
    token.chmod(0o600)
    (snap_dir / "dashboard-host").write_text("127.0.0.1\n", encoding="utf-8")


def _run_uninstall(measure, monkeypatch, snap_dirs, this_install_only=False):
    """Drive the launchd uninstaller with the identity sweep stubbed to
    `snap_dirs` and every OS call neutralized."""
    monkeypatch.setattr(
        measure, "_daemon_identity_snapshot_dirs",
        lambda only: [snap_dirs[0]] if only else list(snap_dirs),
    )
    monkeypatch.setattr(measure, "_ALL_LAUNCH_AGENT_LABELS", ())
    monkeypatch.setattr(measure, "_reclaim_posix_daemon_port", lambda *a, **k: None)
    measure._uninstall_launchd_daemon(this_install_only=this_install_only)


def test_tombstone_persists_in_every_swept_identity(measure, tmp_path, monkeypatch):
    """THE regression: post-uninstall, both identities keep a tombstone."""
    active = tmp_path / "active" / "data"
    sibling = tmp_path / "sibling" / "data"
    _seed_identity(active)
    _seed_identity(sibling)

    _run_uninstall(measure, monkeypatch, [active, sibling])

    for snap in (active, sibling):
        assert (snap / TOMBSTONE).exists(), (
            f"{snap} has no tombstone after uninstall -- daemon self-revive is "
            "re-armed and an orphaned job can mint a fresh 0600 token"
        )
        # The real daemon artifacts ARE gone.
        assert not (snap / "dashboard-server.py").exists()
        assert not (snap / "daemon-token").exists()


def test_tombstone_survives_when_it_already_existed(measure, tmp_path, monkeypatch):
    """A pre-existing tombstone (prior uninstall / thrash back-off) is kept."""
    active = tmp_path / "active" / "data"
    _seed_identity(active)
    (active / TOMBSTONE).write_text("", encoding="utf-8")

    _run_uninstall(measure, monkeypatch, [active])

    assert (active / TOMBSTONE).exists()


def test_this_install_only_still_tombstones_the_swept_identity(measure, tmp_path, monkeypatch):
    """Scoped uninstall must not leave its own identity revivable."""
    active = tmp_path / "active" / "data"
    sibling = tmp_path / "sibling" / "data"
    _seed_identity(active)
    _seed_identity(sibling)

    _run_uninstall(measure, monkeypatch, [active, sibling], this_install_only=True)

    assert (active / TOMBSTONE).exists()
    # Sibling untouched by a scoped uninstall (its daemon files remain).
    assert (sibling / "dashboard-server.py").exists()
    assert (sibling / "daemon-token").exists()


def test_reinstall_is_the_only_path_that_clears_the_tombstone(measure, tmp_path):
    """setup_daemon's start path unlinks it; nothing in uninstall may.

    Guards the lifecycle contract: uninstall writes, install clears. A future
    refactor that re-adds an unlink to the uninstall sweep fails the first test
    above; this one pins the other half (the clear still exists on the start
    path) so the tombstone can never become permanently sticky either.
    """
    source = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    assert 'if DAEMON_THRASH_BREADCRUMB.exists():' in source, (
        "setup_daemon's stale-tombstone clear disappeared -- a reinstall would "
        "start a daemon that immediately exits noop-tombstoned"
    )
    assert '_unlink_if_exists(files["thrash_breadcrumb"])' not in source, (
        "an uninstall path unlinks the tombstone again -- #106 F2 P1 regression"
    )


def _extract_thrash_fn(measure, thrash_path: Path, dashboard_path: Path):
    """Exec the ACTUAL generated _thrash_check_and_update in an isolated namespace
    with paths pointed at the temp tree, so we test the code the daemon runs."""
    src = measure._generate_daemon_script()
    start = src.index("def _thrash_check_and_update():")
    end = src.index("\nif not _thrash_check_and_update():")
    func_src = src[start:end]
    ns = {
        "os": os, "time": time, "sys": sys,
        "THRASH_PATH": str(thrash_path),
        "DASHBOARD": str(dashboard_path),
        "UNINSTALL_MARKER": measure._UNINSTALL_TOMBSTONE_MARKER,
        "THRASH_LIMIT": 3,
    }
    exec(compile(func_src, "<daemon-thrash>", "exec"), ns)
    return ns["_thrash_check_and_update"]


def _age_file(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_generated_daemon_never_resurrects_after_uninstall(measure, tmp_path):
    """An aged UNINSTALL tombstone next to a live dashboard must
    NOT self-clear. The surviving daemon must stay dead, not mint a fresh token."""
    snap = tmp_path / "active" / "data"
    snap.mkdir(parents=True)
    thrash = snap / TOMBSTONE
    dashboard = snap / "dashboard.html"
    dashboard.write_text("<html></html>", encoding="utf-8")  # preserved user data
    # Write a real uninstall tombstone, then age it well past the 60s window.
    measure._write_uninstall_tombstone(snap)
    assert thrash.read_text(encoding="utf-8").strip() == measure._UNINSTALL_TOMBSTONE_MARKER
    _age_file(thrash, 120)

    fn = _extract_thrash_fn(measure, thrash, dashboard)
    assert fn() is False, "daemon resurrected itself after a reported uninstall"
    assert thrash.exists(), "uninstall tombstone was self-cleared (must be permanent)"


def test_generated_daemon_fresh_uninstall_tombstone_stays_dead(measure, tmp_path):
    snap = tmp_path / "active" / "data"
    snap.mkdir(parents=True)
    thrash = snap / TOMBSTONE
    dashboard = snap / "dashboard.html"
    dashboard.write_text("<html></html>", encoding="utf-8")
    measure._write_uninstall_tombstone(snap)  # fresh (age ~0)

    fn = _extract_thrash_fn(measure, thrash, dashboard)
    assert fn() is False
    assert thrash.exists()


def test_generated_daemon_empty_thrash_tombstone_still_self_heals(measure, tmp_path):
    """The 3-strikes thrash tombstone (EMPTY file) must still self-heal after 60s
    when the dashboard is back -- the reconciliation must not break that path."""
    snap = tmp_path / "active" / "data"
    snap.mkdir(parents=True)
    thrash = snap / TOMBSTONE
    dashboard = snap / "dashboard.html"
    dashboard.write_text("<html></html>", encoding="utf-8")
    thrash.write_text("", encoding="utf-8")  # empty = transient thrash tombstone
    _age_file(thrash, 120)

    fn = _extract_thrash_fn(measure, thrash, dashboard)
    assert fn() is True, "an aged empty thrash tombstone no longer self-heals"
    assert not thrash.exists(), "self-heal should have cleared the empty thrash tombstone"


def test_uninstall_reclaims_the_daemon_port(measure, tmp_path, monkeypatch):
    """Unregistering must be followed by an actual process kill.

    A sweep-all uninstall reclaims EVERY runtime's port, not
    just the resolved runtime's, or a sibling daemon + live CSRF token survives.
    """
    active = tmp_path / "active" / "data"
    _seed_identity(active)
    ports = []
    monkeypatch.setattr(
        measure, "_daemon_identity_snapshot_dirs", lambda only: [active]
    )
    monkeypatch.setattr(measure, "_ALL_LAUNCH_AGENT_LABELS", ())
    monkeypatch.setattr(
        measure, "_reclaim_posix_daemon_port",
        lambda port=measure.DAEMON_PORT, **k: ports.append(port),
    )

    measure._uninstall_launchd_daemon()  # default = sweep all identities

    assert set(ports) == {24842, 24843, 24844, 24845, 24846, 24847, 24848}, (
        "uninstall did not reclaim every runtime's daemon port -- a sibling "
        "runtime's daemon keeps serving its CSRF token (#106 F2 P1b / G3 B-F1)"
    )
