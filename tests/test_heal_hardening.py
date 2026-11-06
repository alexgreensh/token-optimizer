"""No-flash-fix regressions: the sticky marker, the Windows heal, and the
interpreter-resolution hardening.

The first no-flash fix introduced a self-heal whose failure modes were worse than
the bug. This file
pins the hardened behavior:

The sticky ``.daemon-install-failed`` marker:
  * armed ONLY by definitive/permanent classes (MS-Store alias, schtasks
    missing, policy-denied task creation), classified INSIDE the installer;
  * never armed by transient classes (timeouts, one-off nonzero, spawn
    failures, lock races) -- those rely on the existing throttles;
  * cleared by any later VERIFIED success, not only manual setup-daemon;
  * visible: ensure-health prints the wedge + remedy, doctor has a check.

The heal must not make the no-flash concern worse:
  * a failed heal never compensate-/Runs an action still classified as a
    console flasher (that /Run IS an extra flash per session);
  * the restore runs from a ``finally`` (a RAISING installer must not strand
    the daemon) when the registered action is safe to fire;
  * heal attempts are throttled (24h) so a persistently-failing heal cannot
    /End + sleep(2) + spam every SessionStart;
  * ``_fail`` respects soft_fail: hook paths print to stderr, never stdout.

Sticky-opt-out / gate unification:
  * a user-DISABLED task (Settings-level ``<Enabled>false</Enabled>``) is
    treated exactly like a tombstone;
  * every heal/revive path routes through ``_daemon_resurrection_blocked()``,
    including the restart-path call site and the auto-update block.

T5 -- interpreter resolution:
  * a DEAD baked pythonw path (Python upgrade/uninstall/deleted venv) is
    detected and healed by re-registration;
  * a failed ``schtasks /Run`` with nothing serving is reported as
    'restart-failed', never a false 'restarted';
  * the generated .cmd guards its bare-PATH pythonw rung against Microsoft
    Store aliases (silent exit-0 under Task Scheduler).

T7 -- heal write-surface hardening:
  * .cmd rewrite is atomic (mkstemp + os.replace) and refuses symlinks /
    hardlinked names;
  * the marker's state dir is created 0o700;
  * ``%`` is escaped in paths interpolated into the generated .cmd.

Run: python3 -m pytest tests/test_heal_hardening.py -q
"""

from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source

import ast
import contextlib
import importlib
import os
import re
import shutil
import stat as stat_mod
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE_PY = SCRIPTS / "measure.py"

sys.path.insert(0, str(SCRIPTS))


def _measure_source() -> str:
    return _split_measure_source(MEASURE_PY)


@pytest.fixture()
def m(monkeypatch):
    """measure.py imported with its state dir pinned into a temp dir."""
    tmp = tempfile.mkdtemp(prefix="to-107h-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    # OS runners/installers are stubbed below; keep their pure orchestration testable.
    monkeypatch.setattr(mod, "_daemon_snapshot_sandboxed", lambda: False)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    # The legacy daemon dir derives from RUNTIME_DIR (the REAL ~/.claude, not
    # covered by TOKEN_OPTIMIZER_SNAPSHOT_DIR); pin it into the sandbox so no
    # test can ever read or heal a real machine's legacy shim. Built EAGERLY:
    # under the fake-nt monkeypatch, a lazy Path() would flavour as
    # WindowsPath and blow up on POSIX.
    _legacy_sandbox = Path(tmp) / "legacy"
    monkeypatch.setattr(mod, "_legacy_daemon_dir",
                        lambda: _legacy_sandbox, raising=False)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _fake_nt(m, monkeypatch):
    monkeypatch.setattr(m.os, "name", "nt", raising=False)


def _result(rc=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _flags_env(m, monkeypatch, initial=None):
    """Dict-backed config flags so throttle stamps never touch the REAL
    config.json (CONFIG_PATH is not sandboxed by TOKEN_OPTIMIZER_SNAPSHOT_DIR)."""
    flags = dict(initial or {})
    monkeypatch.setattr(m, "_read_config_flag",
                        lambda k, d=None: flags.get(k, d if d is not None else False))
    monkeypatch.setattr(m, "_write_config_flag",
                        lambda k, v: flags.__setitem__(k, v))
    return flags


class _Recorder:
    """Stand-in for the subprocess runner with a MUTABLE task action and an
    optional Settings-level <Enabled> state. NOTE for the host-safety guard:
    this file spawns NO real subprocesses -- every runner is stubbed, so no
    host-mutating verb can ever reach a developer's machine."""

    def __init__(self, action, enabled=None, query_rc=0):
        self.action = action
        self.enabled = enabled
        self.query_rc = query_rc
        self.verbs = []

    def __call__(self, argv, *a, **k):
        self.verbs.append(list(argv))
        if "/Query" in argv:
            settings = ""
            if self.enabled is not None:
                settings = (
                    "<Settings><Enabled>"
                    + ("true" if self.enabled else "false")
                    + "</Enabled></Settings>"
                )
            return _result(
                rc=self.query_rc,
                stdout=(
                    "<Task><Triggers><LogonTrigger><Enabled>true</Enabled>"
                    "</LogonTrigger></Triggers>"
                    f"<Actions><Exec><Command>{self.action}</Command></Exec>"
                    f"</Actions>{settings}</Task>"
                ),
            )
        return _result()


def _heal_env(m, monkeypatch, action, enabled=None, pythonw=r"C:\Py\pythonw.exe",
              install_ok=True, path_exists=True):
    _fake_nt(m, monkeypatch)
    flags = _flags_env(m, monkeypatch)
    monkeypatch.setattr(m, "_windows_gui_python", lambda: pythonw)
    monkeypatch.setattr(m, "_persist_dashboard_host",
                        lambda persist=True: "127.0.0.1")
    monkeypatch.setattr(m, "_windows_action_path_exists",
                        lambda p: path_exists)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    rec = _Recorder(action, enabled=enabled)
    monkeypatch.setattr(m.subprocess, "run", rec)
    installs = []
    monkeypatch.setattr(
        m, "_install_task_scheduler_daemon",
        lambda **k: (installs.append(k), install_ok)[1])
    return rec, installs, flags


def _installer_env(m, monkeypatch, create_rc=0, create_stderr="",
                   end_raises=None, port_owner=None, ms_store=False):
    """Drive the REAL _install_task_scheduler_daemon with everything around it
    stubbed. File writes land in the sandboxed SNAPSHOT_DIR."""
    _fake_nt(m, monkeypatch)
    monkeypatch.setattr(m, "_ensure_dashboard_file", lambda **kw: True)
    monkeypatch.setattr(m, "_is_ms_store_python_alias", lambda p: ms_store)
    monkeypatch.setattr(m, "_compose_windows_user_id", lambda: "WORKGROUP\\bob")
    monkeypatch.setattr(m, "_probe_windows_port_owner", lambda port: port_owner)
    monkeypatch.setattr(m, "_get_or_create_daemon_token", lambda: "tok")
    monkeypatch.setattr(m, "_generate_daemon_script", lambda: "# daemon\n")
    monkeypatch.setattr(m, "_windows_gui_python", lambda: r"C:\Py\pythonw.exe")
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    @contextlib.contextmanager
    def _lock(soft_fail=False):
        yield True

    monkeypatch.setattr(m, "_daemon_install_lock", _lock)
    calls = []

    def _run(argv, *a, **k):
        calls.append(list(argv))
        if "/End" in argv and end_raises is not None:
            raise end_raises
        if "/Create" in argv:
            return _result(rc=create_rc, stderr=create_stderr)
        return _result()

    monkeypatch.setattr(m.subprocess, "run", _run)
    return calls


# ---------------------------------------------------------------------------
# Definitive classes arm; transient classes never do
# ---------------------------------------------------------------------------
def test_ms_store_alias_arms_the_permanent_marker(m, monkeypatch):
    """A Store App Execution Alias is STRUCTURALLY impossible to register as a
    Task Scheduler action -- the one failure shape the sticky marker is for."""
    _installer_env(m, monkeypatch, ms_store=True)
    assert m._install_task_scheduler_daemon(soft_fail=True) is False
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    assert "ms-store-python-alias" in (m._daemon_install_failed_reason() or "")


def test_schtasks_access_denied_arms_the_permanent_marker(m, monkeypatch):
    """Policy-blocked task creation fails identically next prompt -- arm."""
    _installer_env(m, monkeypatch, create_rc=1,
                   create_stderr="ERROR: Access is denied.")
    assert m._install_task_scheduler_daemon(soft_fail=True) is False
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    assert "schtasks-create-access-denied" in (
        m._daemon_install_failed_reason() or "")


def test_schtasks_missing_arms_the_permanent_marker(m, monkeypatch):
    """No schtasks.exe on the machine = no scheduler to register with."""
    _installer_env(m, monkeypatch, end_raises=FileNotFoundError("schtasks"))
    assert m._install_task_scheduler_daemon(soft_fail=True) is False
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    assert "schtasks-missing" in (m._daemon_install_failed_reason() or "")


def test_transient_create_failure_does_not_arm(m, monkeypatch):
    """A nonzero /Create that is NOT positively classified as permanent stays
    retryable under the throttles (never arm on a one-off
    nonzero)."""
    _installer_env(m, monkeypatch, create_rc=1,
                   create_stderr="ERROR: The network path was not found.")
    assert m._install_task_scheduler_daemon(soft_fail=True) is False
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_port_bound_failure_does_not_arm_and_prints_to_stderr(
        m, monkeypatch, capsys):
    """A foreign port owner is transient (never arms), and
    under soft_fail the '[Error] Port ...' line must go to stderr -- stdout is
    hook output injected into every session's context."""
    _installer_env(m, monkeypatch, port_owner="pid=999 (netstat: ...)")
    assert m._install_task_scheduler_daemon(soft_fail=True) is False
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    out, err = capsys.readouterr()
    assert "[Error] Port" not in out, (
        "soft_fail error text leaked into hook stdout"
    )
    assert "[Error] Port" in err


def test_live_daemon_clears_stale_marker(m, monkeypatch):
    """Clear-on-verified-success: a live, identity-checked daemon
    disproves the 'structurally broken' record (the install-lock race wedge:
    loser armed the marker, winner installed fine)."""
    _flags_env(m, monkeypatch)
    m._write_daemon_install_failed_marker("installer-returned-false")
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: True)
    assert m._ensure_dashboard_daemon() == "noop-healthy"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists(), (
        "a verified-healthy daemon must clear the stale marker"
    )


def test_install_success_supersedes_marker_race(m, monkeypatch):
    """A successful self-heal install clears a marker a concurrent session
    raced in mid-flight."""
    _flags_env(m, monkeypatch)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Darwin")
    monkeypatch.setattr(m, "_daemon_service_installed", lambda s=None: False)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_ensure_dashboard_file", lambda **kw: True)
    monkeypatch.setattr(m, "_get_or_create_daemon_token", lambda: "t")

    def _install(**k):
        # A concurrent loser arms the marker while we are installing.
        m._write_daemon_install_failed_marker("installer-returned-false")
        return True

    monkeypatch.setattr(m, "_install_launchd_daemon", _install)
    assert m._ensure_dashboard_daemon(force=True) == "installed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_lock_race_loser_resolves_from_reality_without_arming(m, monkeypatch):
    """T3-H1: the loser's instantaneous recheck races the winner's
    multi-second install window. Ambiguity must NOT arm the marker."""
    _flags_env(m, monkeypatch)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Darwin")
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_ensure_dashboard_file", lambda **kw: True)
    monkeypatch.setattr(m, "_get_or_create_daemon_token", lambda: "t")
    monkeypatch.setattr(m, "_install_launchd_daemon", lambda **k: False)

    # Winner still mid-install: nothing registered YET -> retryable, no marker.
    monkeypatch.setattr(m, "_daemon_service_installed", lambda s=None: False)
    assert m._ensure_dashboard_daemon(force=True) == "install-failed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()

    # Winner lands the install BETWEEN our top-of-ensure check and the
    # post-failure recheck -> resolve from reality as success.
    seen = {"n": 0}

    def _installed(s=None):
        seen["n"] += 1
        return seen["n"] > 1  # False at the top of ensure, True on recheck

    monkeypatch.setattr(m, "_daemon_service_installed", _installed)
    assert m._ensure_dashboard_daemon(force=True) == "installed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_marker_reason_helper(m):
    m._write_daemon_install_failed_marker("some-definitive-reason")
    reason = m._daemon_install_failed_reason()
    assert reason is not None and "some-definitive-reason" in reason


# ---------------------------------------------------------------------------
# Visibility: the wedge must never be silent
# ---------------------------------------------------------------------------
def test_ensure_health_surfaces_the_wedge():
    """run_ensure_health must print a one-liner when the marker is suppressing
    the daemon, naming the exact command that clears it. The message is emitted
    as a ``{"systemMessage": ...}`` JSON object on stdout so the CC UI shows it
    to the user (rendered as "<hook> says: ...") WITHOUT sending it to the
    model. stderr would be invisible in the CC UI on exit 0 (only in the
    Ctrl+O transcript), which is the exact silence the persistent-disable
    wedge fix set out to kill."""
    src = _measure_source()
    idx = src.index("def run_ensure_health")
    body = src[idx:]
    branch = body.index('== "noop-install-failed"')
    # Scope to JUST this branch (up to the enclosing try's `except`), so the
    # trailing `except ... file=sys.stderr` handler can't leak into the window.
    branch_body = body[branch:]
    end = branch_body.index("\n    except Exception")
    window = branch_body[:end]
    assert "setup-daemon" in window, (
        "the suppression one-liner must name the clearing command"
    )
    assert "systemMessage" in window, (
        "the suppression one-liner must be emitted as a systemMessage JSON "
        "object so the CC UI shows it to the user without sending it to the "
        "model -- stderr is invisible in the CC UI on exit 0"
    )
    assert "file=sys.stderr" not in window, (
        "the suppression one-liner must NOT route to stderr -- stderr is "
        "invisible in the CC UI on exit 0, which makes the wedge silent"
    )
    assert "_daemon_install_failed_reason" in window, (
        "the one-liner should include the recorded failure reason"
    )


def test_doctor_has_a_daemon_check():
    """doctor had ZERO daemon checks -- the wedge was invisible to the one
    command named for the job."""
    src = _measure_source()
    start = src.index("def doctor(")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "Dashboard daemon" in body, "doctor must include a daemon check"
    assert "_daemon_install_failed_marker_present" in body, (
        "doctor's daemon check must look at the sticky marker"
    )
    assert "setup-daemon" in body, (
        "doctor's daemon check must name the remediation command"
    )


# ---------------------------------------------------------------------------
# The heal must not make the no-flash concern worse
# ---------------------------------------------------------------------------
def test_heal_restore_runs_in_finally_even_when_installer_raises(
        m, monkeypatch):
    """T2-H3: an OSError INSIDE the installer used to skip the compensating
    /Run entirely, leaving the user's daemon stopped. With the action already
    migrated to pythonw (safe to fire), the finally-restore must /Run it."""
    rec, _, _ = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")

    def _install(**k):
        # The re-registration landed the new pythonw action...
        rec.action = r"C:\Py\pythonw.exe"
        # ...then the installer raised (disk full on a later write).
        raise OSError("disk full")

    monkeypatch.setattr(m, "_install_task_scheduler_daemon", _install)
    assert m._heal_windows_console_flash() is False
    assert any("/Run" in v for v in rec.verbs), (
        "a raising installer must still restore the (now-safe) task via the "
        "finally-based /Run"
    )


def test_heal_never_restores_by_running_a_flasher_even_on_raise(
        m, monkeypatch):
    """The other half of the same finally: when the action is STILL the .cmd
    flasher after the raise, the restore must refuse -- /Run-ing it is one
    extra console flash per session."""
    rec, _, _ = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")

    def _install(**k):
        raise OSError("boom")

    monkeypatch.setattr(m, "_install_task_scheduler_daemon", _install)
    assert m._heal_windows_console_flash() is False
    assert not any("/Run" in v for v in rec.verbs)


def test_heal_task_attempts_are_throttled(m, monkeypatch):
    """A persistently-failing heal must not /End the live daemon + sleep(2) +
    reattempt on EVERY SessionStart. Mirrors the ensure throttle: one attempt
    per window, stamped BEFORE the attempt."""
    rec, installs, flags = _heal_env(
        m, monkeypatch, r"C:\s\dashboard-launcher.cmd", install_ok=False)
    assert m._heal_windows_console_flash() is False
    assert len(installs) == 1, "first attempt must actually run"
    assert flags.get("last_daemon_flash_heal_attempt"), (
        "the attempt must stamp the throttle BEFORE running"
    )
    rec.verbs.clear()
    assert m._heal_windows_console_flash() is False
    assert len(installs) == 1, (
        "a second SessionStart inside the throttle window must not re-attempt"
    )
    assert not any("/End" in v for v in rec.verbs), (
        "a throttled heal must not stop the live daemon"
    )


def test_successful_heal_is_not_delayed_by_the_throttle(m, monkeypatch):
    """The stamp is written before the attempt, so a SUCCESSFUL heal leaves
    the next session's classify-no-op untouched -- and a fresh install (no
    stamp) heals immediately."""
    _, installs, flags = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")
    assert m._heal_windows_console_flash() is True
    assert len(installs) == 1


# ---------------------------------------------------------------------------
# user-disabled task + the unified gate
# ---------------------------------------------------------------------------
def test_heal_leaves_a_user_disabled_task_alone(m, monkeypatch):
    """T2-H1: disabling the task (Task Scheduler UI / schtasks /Change
    /DISABLE) is the natural Windows off-switch and was durable pre-fix. The
    heal must treat Settings-level <Enabled>false</Enabled> exactly like a
    tombstone: no /End, no re-registration, no /Run."""
    rec, installs, _ = _heal_env(
        m, monkeypatch, r"C:\s\dashboard-launcher.cmd", enabled=False)
    assert m._heal_windows_console_flash() is False
    assert installs == []
    assert not any("/End" in v for v in rec.verbs)
    assert not any("/Run" in v for v in rec.verbs)


def test_enabled_task_still_heals(m, monkeypatch):
    """Guard against over-blocking: an explicitly-enabled flasher task must
    still be migrated."""
    _, installs, _ = _heal_env(
        m, monkeypatch, r"C:\s\dashboard-launcher.cmd", enabled=True)
    assert m._heal_windows_console_flash() is True
    assert len(installs) == 1


def test_task_action_info_parses_settings_enabled(m, monkeypatch):
    """The Settings-level <Enabled> must be read from the XML already in hand;
    trigger-level <Enabled> elements must not be confused for it."""
    for enabled, expect in ((False, False), (True, True), (None, None)):
        rec = _Recorder(r"C:\s\dashboard-launcher.cmd", enabled=enabled)
        monkeypatch.setattr(m.subprocess, "run", rec)
        command, parsed = m._windows_task_action_info()
        assert command == r"C:\s\dashboard-launcher.cmd"
        assert parsed is expect, f"enabled={enabled} parsed as {parsed}"


def test_shared_gate_blocks_on_each_condition(m, monkeypatch):
    """_daemon_resurrection_blocked() is THE one gate (tombstone, disabled,
    marker) every heal/revive path must route through."""
    _flags_env(m, monkeypatch)
    assert m._daemon_resurrection_blocked() is None
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    m.DAEMON_THRASH_BREADCRUMB.write_text("uninstalled", encoding="utf-8")
    assert m._daemon_resurrection_blocked() == "tombstoned"
    m.DAEMON_THRASH_BREADCRUMB.unlink()
    m._write_daemon_install_failed_marker("x")
    assert m._daemon_resurrection_blocked() == "install-failed"
    m._clear_daemon_install_failed_marker()
    _flags_env(m, monkeypatch, {"daemon_disabled": True})
    assert m._daemon_resurrection_blocked() == "disabled"


def test_restart_path_task_heal_respects_shared_gate(m, monkeypatch):
    """`_heal_windows_task_action` is ALSO called (ungated, pre-fix)
    from _restart_dashboard_daemon's /End->/Run window. The gate now lives
    inside the primitive, so a disabled/tombstoned/condemned daemon can never
    be resurrected through that second door."""
    _fake_nt(m, monkeypatch)
    _flags_env(m, monkeypatch, {"daemon_disabled": True})
    called = []
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: called.append(a) or _result())
    assert m._heal_windows_task_action() is False
    assert called == [], (
        "the gated heal must not even query schtasks for a disabled daemon"
    )


def test_auto_update_restart_is_gated():
    """The ensure-health auto-update block used to bypass every
    gate -- a version bump could /Run a daemon the user turned off. The
    restart must now sit behind the shared gate."""
    src = _measure_source()
    idx = src.index("def run_ensure_health")
    body = src[idx:]
    apply_idx = body.index("_apply_daemon_restart_outcome(")
    window = body[max(0, apply_idx - 800):apply_idx]
    assert "_daemon_resurrection_blocked() is None" in window, (
        "the auto-update restart must be gated on _daemon_resurrection_blocked"
    )


# ---------------------------------------------------------------------------
# T5-H1: dead baked interpreter path -- detectable and repairable
# ---------------------------------------------------------------------------
def test_dead_pythonw_action_is_healed(m, monkeypatch):
    """A baked absolute pythonw path that no longer exists (Python upgrade,
    deleted venv) is invisible to the flasher classifier; the heal must treat
    it as heal-worthy and re-register (re-baking the CURRENT twin)."""
    _, installs, _ = _heal_env(
        m, monkeypatch,
        r"C:\Users\bob\AppData\Local\Programs\Python\Python312\pythonw.exe",
        path_exists=False)
    assert m._heal_windows_console_flash() is True
    assert len(installs) == 1, "a dead action must trigger re-registration"


def test_dead_action_healed_even_without_gui_twin(m, monkeypatch):
    """No usable pythonw twin normally means 'nothing better to migrate to' --
    but for a DEAD action, the .cmd fallback beats a daemon that can never
    start again."""
    _, installs, _ = _heal_env(
        m, monkeypatch, r"C:\gone\pythonw.exe",
        pythonw=None, path_exists=False)
    assert m._heal_windows_console_flash() is True
    assert len(installs) == 1


def test_live_pythonw_action_is_left_alone(m, monkeypatch):
    """Idempotency guard: an alive pythonw action is neither a flasher nor
    dead -- the heal must no-op."""
    _, installs, _ = _heal_env(
        m, monkeypatch, r"C:\Py\pythonw.exe", path_exists=True)
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_windows_action_is_dead_path_classifier(m, monkeypatch):
    monkeypatch.setattr(m, "_windows_action_path_exists", lambda p: False)
    # Absolute, missing -> dead (plain, quoted, XML-escaped, UNC).
    assert m._windows_action_is_dead_path(r"C:\Python312\pythonw.exe") is True
    assert m._windows_action_is_dead_path('"C:\\Python312\\pythonw.exe"') is True
    assert m._windows_action_is_dead_path(r"C:\a&amp;b\pythonw.exe") is True
    assert m._windows_action_is_dead_path(r"\\srv\share\pythonw.exe") is True
    # Bare PATH-resolved names cannot be judged dead.
    assert m._windows_action_is_dead_path("pythonw.exe") is False
    assert m._windows_action_is_dead_path("") is False
    assert m._windows_action_is_dead_path(None) is False
    # Alive absolute path -> not dead.
    monkeypatch.setattr(m, "_windows_action_path_exists", lambda p: True)
    assert m._windows_action_is_dead_path(r"C:\Python312\pythonw.exe") is False


def test_restart_reports_failure_when_run_fails_and_nothing_serves(
        m, monkeypatch):
    """`schtasks /Run` exiting nonzero with nothing serving the
    port is a DEMONSTRABLY failed restart. The None-version safe-degrade must
    not convert it into a false 'restarted' (that is how a permanently-dead
    daemon stayed green)."""
    monkeypatch.setattr(m, "_daemon_served_version", lambda *a, **k: None)
    monkeypatch.setattr(m, "_reclaim_posix_daemon_port", lambda: None)
    monkeypatch.setattr(m, "_heal_windows_task_action", lambda *a, **k: False)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    def _run_fails(argv, *a, **k):
        return _result(rc=1 if "/Run" in argv else 0)

    monkeypatch.setattr(m.subprocess, "run", _run_fails)
    assert m._restart_dashboard_daemon("Windows") == "restart-failed"

    # rc==0 keeps the documented safe-degrade (slow cold start != failure).
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _result())
    assert m._restart_dashboard_daemon("Windows") == "restarted"


# ---------------------------------------------------------------------------
# T7: heal write-surface hardening
# ---------------------------------------------------------------------------
STALE_SHIM = "@echo off\r\nwhere py.exe >nul 2>&1\r\npy.exe -3 x\r\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics")
def test_shim_heal_refuses_symlink(m, monkeypatch, tmp_path):
    _fake_nt(m, monkeypatch)
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "victim.cmd"
    target.write_bytes(STALE_SHIM.encode("utf-8"))
    launcher = m.SNAPSHOT_DIR / m.WINDOWS_LAUNCHER_NAME
    launcher.symlink_to(target)
    assert m._heal_windows_launcher_shim() is False
    assert target.read_bytes() == STALE_SHIM.encode("utf-8"), (
        "the heal must never write through a symlink"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics")
def test_shim_heal_refuses_hardlinked_launcher(m, monkeypatch, tmp_path):
    _fake_nt(m, monkeypatch)
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    launcher = m.SNAPSHOT_DIR / m.WINDOWS_LAUNCHER_NAME
    launcher.write_bytes(STALE_SHIM.encode("utf-8"))
    os.link(str(launcher), str(tmp_path / "alias.cmd"))
    assert m._heal_windows_launcher_shim() is False
    assert launcher.read_bytes() == STALE_SHIM.encode("utf-8"), (
        "the heal must refuse a hardlinked name (st_nlink > 1)"
    )


def test_shim_heal_rewrites_atomically(m, monkeypatch):
    """The rewrite must go through mkstemp + os.replace (the keepwarm-plist
    pattern), never a truncate-in-place write over a file cmd.exe may be
    executing."""
    _fake_nt(m, monkeypatch)
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    launcher = m.SNAPSHOT_DIR / m.WINDOWS_LAUNCHER_NAME
    launcher.write_text(STALE_SHIM, encoding="utf-8")
    replaces = []
    real_replace = os.replace

    def _tracking_replace(src, dst):
        replaces.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(m.os, "replace", _tracking_replace)
    assert m._heal_windows_launcher_shim() is True
    assert any(str(launcher) == str(dst) for _src, dst in replaces), (
        "the shim rewrite must land via os.replace, not an in-place write"
    )
    assert b"where " not in launcher.read_bytes().lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_marker_state_dir_created_0o700(m):
    """T7-M: _write_daemon_install_failed_marker mkdirs SNAPSHOT_DIR; when it
    is the first creator, the dir must carry the same 0o700 every other
    creation site applies."""
    shutil.rmtree(m.SNAPSHOT_DIR, ignore_errors=True)
    assert not m.SNAPSHOT_DIR.exists()
    m._write_daemon_install_failed_marker("x")
    mode = stat_mod.S_IMODE(os.stat(m.SNAPSHOT_DIR).st_mode)
    assert mode == 0o700, f"SNAPSHOT_DIR created with 0o{mode:o}, not 0o700"


def test_shim_generator_escapes_percent(m):
    """T7-M: `%` is legal in Windows paths but expands at cmd parse time,
    silently mis-targeting DAEMON_SCRIPT and both log redirects."""
    src = m._generate_windows_launcher_cmd(
        r"C:\Users\a%PATH%b\dashboard-server.py", r"C:\logs%x")
    assert 'set "DAEMON_SCRIPT=C:\\Users\\a%%PATH%%b\\dashboard-server.py"' in src
    assert 'set "STDOUT_LOG=C:\\logs%%x\\stdout.log"' in src
    assert 'set "STDERR_LOG=C:\\logs%%x\\stderr.log"' in src
    # And a %-free path stays byte-for-byte unescaped.
    clean = m._generate_windows_launcher_cmd(r"C:\s\d.py", r"C:\l")
    assert 'set "DAEMON_SCRIPT=C:\\s\\d.py"' in clean


# ---------------------------------------------------------------------------
# T5-M: the .cmd ladder's Store-alias guard
# ---------------------------------------------------------------------------
def test_shim_pythonw_rung_guards_windows_store_alias(m):
    """A WindowsApps App Execution Alias exits 0 SILENTLY under Task
    Scheduler, so an unguarded bare-PATH pythonw.exe first rung would end the
    ladder with no daemon -- on hosts where the pre-fix py-first ladder
    worked. The rung must resolve the PATH hit (pure cmd, no where.exe) and
    skip WindowsApps."""
    shim = m._generate_windows_launcher_cmd(r"C:\s\d.py", r"C:\l")
    assert 'for %%I in (pythonw.exe) do set "PYW_EXE=%%~$PATH:I"' in shim
    assert ('if defined PYW_EXE if /i not "%PYW_EXE:windowsapps=%"'
            '=="%PYW_EXE%" set "PYW_EXE="') in shim
    assert 'if defined PYW_EXE "%PYW_EXE%" "%DAEMON_SCRIPT%"' in shim
    # No unguarded bare pythonw rung may remain.
    for line in shim.splitlines():
        assert not line.strip().lower().startswith("pythonw.exe "), (
            f"unguarded bare pythonw rung: {line!r}"
        )
    # The launcher rungs (never Store aliases) stay bare, GUI-first.
    assert "pyw.exe -3" in shim and "py.exe -3" in shim
    assert "where " not in shim.lower()
    # Guarded pythonw block still runs FIRST.
    assert shim.index('if defined PYW_EXE "%PYW_EXE%"') < shim.index("pyw.exe -3")


# ---------------------------------------------------------------------------
# Structural guard: the transient arming sites stay gone
# ---------------------------------------------------------------------------
def test_marker_arming_sites_are_definitive_only():
    """AST guard: `_write_daemon_install_failed_marker` may be called from the
    marker helper itself, `_fail` inside the Windows installer (the one place
    failures can be CLASSIFIED), and nowhere else. Reintroducing an arming
    call in ensure/pulse/restart re-creates the wedge."""
    src = _measure_source()
    tree = ast.parse(src)
    spans = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    owner = {}
    for start, end, name in spans:
        for line in range(start, end + 1):
            owner[line] = name
    call_owners = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and ast.unparse(node.func) == "_write_daemon_install_failed_marker"):
            call_owners.add(owner.get(node.lineno, "<module>"))
    assert call_owners == {"_fail"}, (
        f"unexpected marker-arming call sites: {sorted(call_owners)} -- "
        "transient paths must never arm the permanent marker"
    )


# ---------------------------------------------------------------------------
# A clear that did not clear must not report success
# ---------------------------------------------------------------------------
def test_clear_marker_success_is_verified(m):
    m._write_daemon_install_failed_marker("wedge")
    assert m._clear_daemon_install_failed_marker() is True
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    # Clearing an already-absent marker is also a verified success.
    assert m._clear_daemon_install_failed_marker() is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_clear_marker_failure_is_loud_and_reports_false(m, capsys):
    """Pre-fix, the OSError was swallowed: setup-daemon printed its full
    'installed and running' success story while the marker survived and every
    self-heal path stayed permanently disabled -- undiagnosably."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    m._write_daemon_install_failed_marker("wedge")
    os.chmod(m.SNAPSHOT_DIR, 0o500)  # unlink now fails (dir not writable)
    try:
        assert m._clear_daemon_install_failed_marker() is False, (
            "an unlink that did not unlink must not report success"
        )
    finally:
        os.chmod(m.SNAPSHOT_DIR, 0o700)
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    err = capsys.readouterr().err
    assert "could not remove" in err and "setup-daemon" in err, (
        "the surviving wedge must be reported, with the remedy"
    )


# ---------------------------------------------------------------------------
# Marker presence is an explicit stat policy, not an exists() accident
# ---------------------------------------------------------------------------
def test_marker_state_is_tristate(m):
    assert m._daemon_install_failed_marker_state() == "absent"
    m._write_daemon_install_failed_marker("x")
    assert m._daemon_install_failed_marker_state() == "present"
    assert m._daemon_install_failed_marker_present() is True


def test_marker_present_fails_closed_on_unreadable_stat(m, monkeypatch):
    """Pre-fix, os.path.exists mapped an AV/permission denial to False -- a
    PRESENT marker read as ABSENT and the retry loop silently resumed,
    intermittently. present-but-unreadable must be distinguishable from
    absent, and the gate must fail CLOSED so it cannot flap."""
    m._write_daemon_install_failed_marker("x")
    real_stat = os.stat

    def _denied_stat(path, *a, **k):
        if str(path) == str(m.DAEMON_INSTALL_FAILED_BREADCRUMB):
            raise PermissionError(13, "denied by AV")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(m.os, "stat", _denied_stat)
    assert m._daemon_install_failed_marker_state() == "unknown"
    assert m._daemon_install_failed_marker_present() is True, (
        "an unreadable marker must gate as PRESENT (fail-closed), not flap open"
    )


# ---------------------------------------------------------------------------
# A marker write that could not be persisted must say so
# ---------------------------------------------------------------------------
def test_marker_write_failure_is_loud(m, monkeypatch, capsys):
    monkeypatch.setattr(
        m.SNAPSHOT_DIR.__class__, "mkdir",
        lambda self, **k: (_ for _ in ()).throw(OSError("read-only")))
    m._write_daemon_install_failed_marker("boom")  # must not raise
    err = capsys.readouterr().err
    assert "could not persist" in err, (
        "silently-degraded stickiness leaves the per-window retry unexplained"
    )


# ---------------------------------------------------------------------------
# Batch-file classification is scoped to OUR launcher
# ---------------------------------------------------------------------------
def test_classifier_scopes_batch_files_to_our_launcher(m):
    assert m._windows_action_is_console_flasher(
        r"C:\s\dashboard-launcher.cmd") is True
    assert m._windows_action_is_console_flasher("DASHBOARD-LAUNCHER.CMD") is True
    for foreign in (r"C:\x\my-daemon-wrapper.cmd", "launcher.BAT",
                    "custom.cmd", "venv-shim.bat", "cmd.exe", "conhost.exe"):
        assert m._windows_action_is_console_flasher(foreign) is False, foreign


def test_heal_leaves_a_user_wrapper_cmd_alone(m, monkeypatch):
    """A user who edited OUR task's action to their own wrapper .cmd (env
    setup, venv pinning, VPN gating) must not have it wiped by the heal --
    'positively identify as ours' has to mean the launcher we generate."""
    rec, installs, _ = _heal_env(
        m, monkeypatch, r"C:\x\my-daemon-wrapper.cmd")
    assert m._heal_windows_console_flash() is False
    assert installs == []
    assert not any("/End" in v for v in rec.verbs)


# ---------------------------------------------------------------------------
# XML-escaped commands + the legacy shim location
# ---------------------------------------------------------------------------
def test_classifier_unescapes_xml_quoted_commands(m):
    """The <Command> value arrives XML-escaped from schtasks /Query; a quoted
    flasher (&quot;...&quot;) must classify identically to its raw form --
    otherwise the flash is never healed for that population, which is the
    whole point of the feature. Consistent with the dead-path classifier."""
    assert m._windows_action_is_console_flasher(
        "&quot;C:\\s\\dashboard-launcher.cmd&quot;") is True
    assert m._windows_action_is_console_flasher(
        "&quot;C:\\Python\\python.exe&quot;") is True
    assert m._windows_action_is_console_flasher(
        "&quot;C:\\Py\\pythonw.exe&quot;") is False
    assert m._windows_action_is_console_flasher(
        r"C:\a&amp;b\dashboard-launcher.cmd") is True


def test_legacy_shim_is_healed(m, monkeypatch, tmp_path):
    """Pre-plugin-data-migration installs keep their shim (and their task
    action) at ~/.claude/_backups/token-optimizer. The auto-update block
    refreshes THAT dir's dashboard-server.py, but pre-fix nothing ever
    rewrote the legacy shim's console-first ladder."""
    _fake_nt(m, monkeypatch)
    legacy = tmp_path / "legacy-backups"
    legacy.mkdir()
    monkeypatch.setattr(m, "_legacy_daemon_dir", lambda: legacy)
    launcher = legacy / m.WINDOWS_LAUNCHER_NAME
    launcher.write_bytes(STALE_SHIM.encode("utf-8"))
    assert m._heal_windows_launcher_shim() is True
    healed = launcher.read_bytes().decode("utf-8")
    assert "where " not in healed.lower()
    # The legacy shim must keep launching the LEGACY dir's daemon script (the
    # one the auto-update block refreshes there), not the plugin-data one.
    assert str(legacy / "dashboard-server.py") in healed
    # Idempotent on the second run.
    assert m._heal_windows_launcher_shim() is False
    # And the snapshot-side launcher was NOT created (never half-install).
    assert not (m.SNAPSHOT_DIR / m.WINDOWS_LAUNCHER_NAME).exists()


def test_both_shim_locations_heal_in_one_pass(m, monkeypatch, tmp_path):
    _fake_nt(m, monkeypatch)
    legacy = tmp_path / "legacy-backups"
    legacy.mkdir()
    monkeypatch.setattr(m, "_legacy_daemon_dir", lambda: legacy)
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for base in (m.SNAPSHOT_DIR, legacy):
        (base / m.WINDOWS_LAUNCHER_NAME).write_bytes(STALE_SHIM.encode("utf-8"))
    assert m._heal_windows_launcher_shim() is True
    for base in (m.SNAPSHOT_DIR, legacy):
        healed = (base / m.WINDOWS_LAUNCHER_NAME).read_bytes().decode("utf-8")
        assert "where " not in healed.lower(), str(base)
        assert str(base / "dashboard-server.py") in healed, str(base)
    assert m._heal_windows_launcher_shim() is False


def test_healthy_task_is_read_once_per_window_not_every_session_start(m, monkeypatch):
    """5.13.50 removed the scheduler read from healthy SessionStarts, but the
    console-flash heal still ran one `schtasks /Query` on every start. A healthy
    task is read once, then not again inside the throttle window."""
    rec, installs, flags = _heal_env(m, monkeypatch, r"C:\Py\pythonw.exe")
    assert m._heal_windows_console_flash() is False
    assert any("/Query" in v for v in rec.verbs), "the first start must look at the task"
    assert flags.get("last_windows_task_action_check")
    rec.verbs.clear()
    assert m._heal_windows_console_flash() is False
    assert not any("/Query" in v for v in rec.verbs), rec.verbs
    assert installs == []
