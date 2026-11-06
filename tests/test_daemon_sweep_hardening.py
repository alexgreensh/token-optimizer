#!/usr/bin/env python3
"""Containment, scope, label sweep, honest reporting.

P2-1 symlinked ``data`` child escaped containment -- a real identity dir whose
      ``data`` was a symlink redirected the unlinks outside the plugin-data
      base (review repro deleted files from $HOME).
P2-2 ``--this-install-only`` scoped the FILE sweep but not the SCHEDULER
      loops, so a scoped uninstall still booted out every runtime's job.
P2-3 launchd was swept by plist PATH, gated on ``plist.exists()``, so a job
      still loaded with no plist file was never unregistered.
P2-4 delete failures were swallowed, printing "Swept all token-optimizer-*
      identities" while a 0600 daemon-token survived on disk.

Run: python3 -m pytest tests/test_daemon_sweep_hardening.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

# POSIX-only: every test drives _uninstall_launchd_daemon (launchctl +
# os.getuid), symlink containment (Path.symlink_to needs privilege on Windows),
# or 0600 token modes -- none of which Windows can represent. Skip the whole
# file on Windows; macOS/Linux run it unchanged.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX daemon semantics (launchctl/os.kill/file modes)",
)


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "base" / "token-optimizer-a" / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    sys.modules.pop("plugin_env", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    base = tmp_path / "base"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod.plugin_env, "_PLUGIN_DATA_BASE", base)
    monkeypatch.setattr(mod, "_all_plugin_data_dirs", mod.plugin_env._all_plugin_data_dirs)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    monkeypatch.setattr(mod, "LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(
        mod,
        "PLIST_PATH",
        launch_agents / "com.token-optimizer.dashboard.plist",
    )
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: mod.subprocess.CompletedProcess(a, 1),
    )
    # The fixture exercises low-level sweep behavior, so bypass the production
    # sandbox guard only after launchd paths and subprocesses are contained.
    monkeypatch.setattr(mod, "_daemon_snapshot_sandboxed", lambda: False)
    yield mod, base
    sys.modules.pop("measure", None)


def _seed(snap: Path) -> None:
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "dashboard-server.py").write_text("# d\n", encoding="utf-8")
    tok = snap / "daemon-token"
    tok.write_text("csrf", encoding="utf-8")
    tok.chmod(0o600)
    (snap / "dashboard-host").write_text("127.0.0.1", encoding="utf-8")


# ---------------------------------------------------------------------------
# P2-1: containment
# ---------------------------------------------------------------------------

def test_symlinked_data_child_is_excluded_from_the_sweep(measure, tmp_path):
    """THE escape: identity dir is real, its `data` child points elsewhere."""
    mod, base = measure
    victim = tmp_path / "victim"
    _seed(victim)
    evil = base / "token-optimizer-evil"
    evil.mkdir(parents=True)
    (evil / "data").symlink_to(victim, target_is_directory=True)

    swept = mod._daemon_identity_snapshot_dirs(False)

    assert (evil / "data") not in swept, "symlinked data child entered the sweep"
    assert not any(str(victim) == str(d) for d in swept)
    # And the guard itself rejects it outright.
    assert mod._daemon_sweep_dir_is_safe(evil / "data") is False
    # Victim files untouched by a real uninstall pass.
    assert (victim / "daemon-token").exists()


def test_real_identity_under_base_is_accepted(measure):
    mod, base = measure
    good = base / "token-optimizer-b" / "data"
    _seed(good)
    assert mod._daemon_sweep_dir_is_safe(good) is True
    assert good in mod._daemon_identity_snapshot_dirs(False)


def test_snapshot_dir_outside_base_is_still_allowed(measure, tmp_path):
    """A sandbox/legacy SNAPSHOT_DIR is an explicit target, not a discovery."""
    mod, _base = measure
    outside = tmp_path / "outside" / "data"
    _seed(outside)
    import unittest.mock as _m
    with _m.patch.object(mod, "SNAPSHOT_DIR", outside):
        assert mod._daemon_sweep_dir_is_safe(outside) is True


def test_unlink_never_follows_a_symlink_to_its_target(measure, tmp_path):
    mod, _base = measure
    target = tmp_path / "precious.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "daemon-token"
    link.symlink_to(target)

    assert mod._unlink_if_exists(link) is True
    assert not link.exists() and not link.is_symlink(), "link should be gone"
    assert target.exists(), "unlink followed the symlink and deleted the target"


# ---------------------------------------------------------------------------
# P2-2 / P2-3: scheduler scope + label sweep
# ---------------------------------------------------------------------------

def test_this_install_only_restricts_the_scheduler_sweep(measure):
    mod, _base = measure
    scoped = mod._scheduler_names_to_sweep(True, mod._ALL_LAUNCH_AGENT_LABELS, mod.DAEMON_LABEL)
    swept_all = mod._scheduler_names_to_sweep(False, mod._ALL_LAUNCH_AGENT_LABELS, mod.DAEMON_LABEL)

    assert scoped == (mod.DAEMON_LABEL,), "scoped uninstall must touch one label"
    assert len(swept_all) == len(mod._ALL_LAUNCH_AGENT_LABELS) > 1


def test_launchd_boots_out_by_label_even_without_a_plist(measure, tmp_path, monkeypatch):
    """P2-3: a loaded job whose plist file is gone must still be unregistered."""
    mod, base = measure
    snap = base / "token-optimizer-a" / "data"
    _seed(snap)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(exist_ok=True)
    monkeypatch.setattr(mod, "LAUNCH_AGENTS_DIR", agents)  # no plist files at all
    monkeypatch.setattr(mod, "_reclaim_posix_daemon_port", lambda *a, **k: None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._uninstall_launchd_daemon()

    booted = [c for c in calls if len(c) > 2 and c[:2] == ["launchctl", "bootout"]]
    assert booted, "no bootout issued when the plist file was absent"
    assert any(mod.DAEMON_LABEL in c[2] for c in booted), (
        f"bootout did not target the label; issued: {booted}"
    )


# ---------------------------------------------------------------------------
# P2-4: honest reporting
# ---------------------------------------------------------------------------

def test_undeletable_token_is_reported_not_hidden(measure, capsys, monkeypatch):
    mod, base = measure
    snap_a = base / "token-optimizer-a" / "data"
    snap_b = base / "token-optimizer-b" / "data"
    _seed(snap_a)
    _seed(snap_b)
    monkeypatch.setattr(mod, "_ALL_LAUNCH_AGENT_LABELS", ())
    monkeypatch.setattr(mod, "_reclaim_posix_daemon_port", lambda *a, **k: None)

    real_unlink = mod._unlink_reporting
    survivor = snap_b / "daemon-token"

    def flaky(path):
        if Path(path) == survivor:
            return (False, True)  # EPERM
        return real_unlink(path)

    monkeypatch.setattr(mod, "_unlink_reporting", flaky)
    mod._uninstall_launchd_daemon()
    out = capsys.readouterr().out

    assert "WARNING" in out and "could NOT be removed" in out
    assert str(survivor) in out, "the surviving token was not named"
    assert "Swept all token-optimizer-* identities" not in out, (
        "claimed a clean all-identity sweep while a 0600 token survived"
    )


def test_clean_sweep_still_prints_the_banner(measure, capsys, monkeypatch):
    mod, base = measure
    _seed(base / "token-optimizer-a" / "data")
    _seed(base / "token-optimizer-b" / "data")
    monkeypatch.setattr(mod, "_ALL_LAUNCH_AGENT_LABELS", ())
    monkeypatch.setattr(mod, "_reclaim_posix_daemon_port", lambda *a, **k: None)

    mod._uninstall_launchd_daemon()
    out = capsys.readouterr().out

    assert "Swept all token-optimizer-* identities" in out
    assert "WARNING" not in out


def test_nothing_to_remove_honesty_preserved(measure, capsys, monkeypatch):
    """L7 invariant: no artifacts -> no contradictory output."""
    mod, base = measure
    (base / "token-optimizer-a" / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_ALL_LAUNCH_AGENT_LABELS", ())
    monkeypatch.setattr(mod, "_reclaim_posix_daemon_port", lambda *a, **k: None)

    mod._uninstall_launchd_daemon()
    out = capsys.readouterr().out

    assert "Nothing to remove" in out
    assert "Deleted:" not in out
