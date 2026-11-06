#!/usr/bin/env python3
"""A cleanup (tidy) must NOT permanently disable the dashboard daemon.

Regression this pins (real incident, 2026-08-03):

`measure.py cleanup` is the full-uninstall tidy. It called
`setup_daemon(uninstall=True)`, which unconditionally wrote the sticky
`daemon_disabled=true` opt-out into the SHARED plugin-data config. That flag is
the FIRST gate in `_ensure_dashboard_daemon` (`return "noop-disabled"`), so the
SessionStart self-heal silently refused to resurrect the dashboard -- forever,
across every version bump -- until the user manually ran `setup-daemon`. A user
who merely tidied up (while keeping Token Optimizer active) was left with a
silently dead dashboard and no path back except a command they should never have
to run. The self-heal was working; cleanup was lying to it.

Contract pinned here:
  1. `setup_daemon(uninstall=True, latch_optout=False)` (the cleanup path) does
     NOT write the sticky opt-out -- so the self-heal is free to reinstall.
  2. `setup_daemon(uninstall=True)` (default: the dedicated `setup-daemon
     --uninstall` "turn my dashboard off" command) STILL writes it -- a
     deliberate opt-out must be honored.
  3. `cleanup()` passes `latch_optout=False` (source-level, so the wiring can't
     silently regress).

The tombstone half of "stay dead" is orthogonal and covered by
test_daemon_uninstall_tombstone_persists.py: an intentional reinstall
(_install_launchd_daemon) clears the tombstone, while orphaned jobs still honor
it. Only the config-flag latch blocked the self-heal, and only for a tidy.

Run: python3 -m pytest tests/test_cleanup_does_not_latch_daemon_optout.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module, measure_source as _split_measure_source

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX daemon uninstall semantics (launchctl)",
)


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "active" / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    # Each test replaces every platform uninstaller before calling setup_daemon.
    monkeypatch.setattr(mod, "_daemon_snapshot_sandboxed", lambda: False)
    yield mod
    sys.modules.pop("measure", None)


def _neutralize_platform_uninstall(measure, monkeypatch, disabled_calls):
    """Stub every OS uninstaller + record _set_daemon_disabled calls."""
    monkeypatch.setattr(measure, "_uninstall_launchd_daemon", lambda **k: None)
    monkeypatch.setattr(measure, "_uninstall_task_scheduler_daemon", lambda **k: None, raising=False)
    monkeypatch.setattr(measure, "_uninstall_systemd_user_daemon", lambda **k: None, raising=False)
    monkeypatch.setattr(measure, "_set_daemon_disabled", lambda v: disabled_calls.append(v))


def test_cleanup_path_does_not_latch_the_optout(measure, monkeypatch):
    """latch_optout=False (how cleanup calls it) must NOT set daemon_disabled."""
    calls = []
    _neutralize_platform_uninstall(measure, monkeypatch, calls)

    measure.setup_daemon(uninstall=True, latch_optout=False)

    assert True not in calls, (
        "cleanup's uninstall wrote the sticky daemon_disabled opt-out -- the "
        "SessionStart self-heal will silently refuse to restore the dashboard"
    )


def test_explicit_uninstall_still_latches_the_optout(measure, monkeypatch):
    """Default latch_optout=True (setup-daemon --uninstall) MUST still opt out."""
    calls = []
    _neutralize_platform_uninstall(measure, monkeypatch, calls)

    measure.setup_daemon(uninstall=True)

    assert calls == [True], (
        "a deliberate `setup-daemon --uninstall` no longer persists the opt-out "
        "-- the self-heal would fight the user who explicitly turned it off"
    )


def test_dry_run_never_latches_regardless_of_flag(measure, monkeypatch):
    """A dry-run is side-effect-free on both paths."""
    calls = []
    _neutralize_platform_uninstall(measure, monkeypatch, calls)

    measure.setup_daemon(uninstall=True, dry_run=True)                 # default latch
    measure.setup_daemon(uninstall=True, dry_run=True, latch_optout=False)

    assert True not in calls, "a dry-run wrote the sticky opt-out (must be side-effect-free)"


def test_cleanup_wires_latch_optout_false(measure):
    """Source-level: cleanup() must pass latch_optout=False into setup_daemon."""
    source = _split_measure_source(SCRIPTS / "measure.py")
    marker = "setup_daemon(dry_run=dry_run, uninstall=True, this_install_only=this_install_only, latch_optout=False)"
    assert marker in source, (
        "cleanup() no longer passes latch_optout=False -- a tidy will again latch "
        "the daemon_disabled opt-out and silently kill the dashboard"
    )
