"""Unit + integration: keep SessionStart hook diagnostics out of model context.

SessionStart hooks inject their stdout AND stderr into the agent's context
window every session. Diagnostic lines printed to either stream land in the
model context on every turn, costing tokens with no benefit to the model.

The fix lives in ``hooks/sessionstart_runner.py``: every subcommand's stdout
and stderr is captured in-process and routed to a diagnostics log file, never
to real stdout or stderr. Only the two compact-restore subcommands produce
context-bound output (restored-state additionalContext) that feeds the
SessionStart envelope.

These tests pin two contracts:

  * **Hook path** (via ``sessionstart_runner.main()``): diagnostics reach
    NEITHER stdout NOR stderr -- they land in the log file. Stdout is only
    the JSON envelope or empty; stderr is empty.
  * **Interactive CLI path** (``soft_fail=False``, a human running
    ``measure.py setup-daemon``): the actionable error must still print to
    stdout so a human sees it. This contract is unchanged.

Run: python3 -m pytest tests/test_sessionstart_context_tax.py -q
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE_PY = SCRIPTS / "measure.py"
RUNNER = REPO / "hooks" / "sessionstart_runner.py"

sys.path.insert(0, str(SCRIPTS))


# --------------------------------------------------------------------------- #
# measure.py fixture (for the interactive CLI path tests, which call measure
# functions directly).
# --------------------------------------------------------------------------- #

@pytest.fixture()
def m(monkeypatch):
    """measure.py imported with its state dir pinned into a temp dir."""
    tmp = tempfile.mkdtemp(prefix="to-sstax-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    _legacy_sandbox = Path(tmp) / "legacy"
    monkeypatch.setattr(mod, "_legacy_daemon_dir",
                        lambda: _legacy_sandbox, raising=False)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _flags_env(m, monkeypatch, initial=None):
    """Dict-backed config flags so throttle stamps never touch the REAL
    config.json (CONFIG_PATH is not sandboxed by SNAPSHOT_DIR)."""
    flags = dict(initial or {})
    monkeypatch.setattr(m, "_read_config_flag",
                        lambda k, d=None: flags.get(k, d if d is not None else False))
    monkeypatch.setattr(m, "_write_config_flag",
                        lambda k, v: flags.__setitem__(k, v))
    return flags


# --------------------------------------------------------------------------- #
# Runner loading helper (for the hook-path tests, which drive the runner).
# --------------------------------------------------------------------------- #

def _load_runner(monkeypatch, tmp_path):
    """Import hooks/sessionstart_runner.py with CLAUDE_PLUGIN_ROOT=REPO so its
    _resolve_measure_dir() finds skills/token-optimizer/scripts/measure.py."""
    assert RUNNER.is_file(), f"runner missing: {RUNNER}"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location("ss_runner_ctx_tax", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_runner_stubs(monkeypatch, runner, tmp_path, log_file):
    """Stub the runner for hook-path tests: point the diagnostics log at a tmp
    file, silence quality-cache/compact-restore, consent True, fake deadline."""
    monkeypatch.setattr(runner, "_diagnostics_log_path", lambda: log_file)

    # Fake deadline that never kills the process.
    class _FakeDeadline:
        def __init__(self, seconds):
            self.seconds = float(seconds)
            self.cancelled = False
        def start(self):
            return self
        def remaining(self):
            return 100.0
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(runner.measure, "HookDeadline", _FakeDeadline)
    monkeypatch.setattr(runner, "_RUNNER_TOTAL_BUDGET", 18.0)
    monkeypatch.setattr(runner, "_RUNNER_DEADLINE", None, raising=False)
    monkeypatch.setattr(runner, "_SUBCOMMANDS_PENDING", 0, raising=False)

    # Consent True: the consent gate has its own tests in test_sessionstart_runner.
    monkeypatch.setattr(runner, "_check_consent", lambda: True)

    # Silence quality-cache and compact-restore (not under test here).
    monkeypatch.setattr(runner.measure, "quality_cache", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "compact_restore", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_ensure_health_daemon_revive_first",
                        lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache",
                        lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)

    # Real markers, written into tmp.
    marker_dir = tmp_path / "quality-cache"
    marker_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", marker_dir)


def _run_runner_capturing(runner, hook_input):
    """Drive runner.main() and capture the REAL stdout/stderr the host would
    see (i.e. what escapes the runner's internal capture)."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = runner.main()
    return rc, out_buf.getvalue(), err_buf.getvalue()


# --------------------------------------------------------------------------- #
# Hook path: the runner captures subcommand diagnostics and routes them to the
# log file. Neither real stdout nor real stderr should carry any diagnostic.
# --------------------------------------------------------------------------- #

def test_systemd_diagnostic_routed_to_log_not_streams(monkeypatch, tmp_path):
    """On a systemd-less box, the ``[Error] systemctl --user is not reachable``
    diagnostic produced inside ensure-health must reach the log file, not
    stdout or stderr. The host captures both streams into the model context."""
    runner = _load_runner(monkeypatch, tmp_path)
    log_file = tmp_path / "diag.log"
    _install_runner_stubs(monkeypatch, runner, tmp_path, log_file)

    # Simulate the systemd-less diagnostic inside ensure-health. The real
    # _install_systemd_user_daemon(soft_fail=True) writes to sys.stderr when
    # _probe_systemd_user_bus returns False.
    monkeypatch.setattr(runner.measure, "_probe_systemd_user_bus", lambda: False)
    monkeypatch.setattr(runner.measure, "_daemon_snapshot_sandboxed", lambda: False)

    def _fake_ensure_health():
        runner.measure._install_systemd_user_daemon(soft_fail=True)

    monkeypatch.setattr(runner.measure, "run_ensure_health", _fake_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ctx-tax-systemd",
                                 "source": "startup"})

    rc, out, err = _run_runner_capturing(runner, {})
    assert rc == 0

    assert "is not reachable" not in out, (
        "systemctl diagnostic leaked into hook stdout (model context)"
    )
    assert "is not reachable" not in err, (
        "systemctl diagnostic leaked into hook stderr (model context)"
    )
    assert "is not reachable" in log_file.read_text(encoding="utf-8"), (
        "the systemctl diagnostic must reach the diagnostics log file"
    )


def test_launchd_diagnostic_routed_to_log_not_streams(monkeypatch, tmp_path):
    """The macOS launchd installer's _fail must also be captured by the runner
    and routed to the log file, not stdout or stderr."""
    runner = _load_runner(monkeypatch, tmp_path)
    log_file = tmp_path / "diag.log"
    _install_runner_stubs(monkeypatch, runner, tmp_path, log_file)

    monkeypatch.setattr(runner.measure, "_ensure_dashboard_file",
                        lambda **kw: False)
    monkeypatch.setattr(runner.measure, "_daemon_snapshot_sandboxed", lambda: False)

    def _fake_ensure_health():
        runner.measure._install_launchd_daemon(soft_fail=True)

    monkeypatch.setattr(runner.measure, "run_ensure_health", _fake_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ctx-tax-launchd",
                                 "source": "startup"})

    rc, out, err = _run_runner_capturing(runner, {})
    assert rc == 0

    assert "Dashboard file missing" not in out, (
        "launchd error leaked into hook stdout (model context)"
    )
    assert "Dashboard file missing" not in err, (
        "launchd error leaked into hook stderr (model context)"
    )
    assert "Dashboard file missing" in log_file.read_text(encoding="utf-8"), (
        "the launchd diagnostic must reach the diagnostics log file"
    )


def test_self_healed_notice_routed_to_log_not_streams(monkeypatch, tmp_path):
    """When ensure-health heals missing hooks, the 'Self-healed N hooks'
    notice must go to the log file via the runner, not stdout or stderr."""
    runner = _load_runner(monkeypatch, tmp_path)
    log_file = tmp_path / "diag.log"
    _install_runner_stubs(monkeypatch, runner, tmp_path, log_file)

    def _fake_ensure_health():
        # Simulate the diagnostic that run_ensure_health prints to stderr
        # under the hook path when it heals hooks.
        print("  [Token Optimizer] Self-healed 1 hook(s) in settings.json",
              file=sys.stderr)

    monkeypatch.setattr(runner.measure, "run_ensure_health", _fake_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ctx-tax-heal",
                                 "source": "startup"})

    rc, out, err = _run_runner_capturing(runner, {})
    assert rc == 0

    assert "Self-healed" not in out, (
        "Self-healed notice leaked into hook stdout (model context)"
    )
    assert "Self-healed" not in err, (
        "Self-healed notice leaked into hook stderr (model context)"
    )
    assert "Self-healed" in log_file.read_text(encoding="utf-8"), (
        "the Self-healed notice must reach the diagnostics log file"
    )


def test_ensure_health_stdout_diagnostic_routed_to_log(monkeypatch, tmp_path):
    """ensure-health diagnostics (e.g. 'Generating initial dashboard') belong
    on stderr and must go to the log file, NOT into the SessionStart envelope.
    Only feature output (systemMessage, compact-restore, bare-text nudges)
    feeds the envelope."""
    runner = _load_runner(monkeypatch, tmp_path)
    log_file = tmp_path / "diag.log"
    _install_runner_stubs(monkeypatch, runner, tmp_path, log_file)

    def _fake_ensure_health():
        # Diagnostics belong on stderr so they reach the log, not the envelope.
        print("  [Token Optimizer] Generating initial dashboard", file=sys.stderr)
        print("  [Token Optimizer] Captured baseline snapshot for structural savings",
              file=sys.stderr)

    monkeypatch.setattr(runner.measure, "run_ensure_health", _fake_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ctx-tax-stdout",
                                 "source": "startup"})

    rc, out, err = _run_runner_capturing(runner, {})
    assert rc == 0

    # stdout must be empty or a valid JSON envelope with NO diagnostic content.
    stripped = out.strip()
    if stripped:
        obj = json.loads(stripped)
        assert isinstance(obj, dict), f"stdout is not a JSON object: {stripped!r}"
        # The envelope must not carry ensure-health diagnostics.
        blob = json.dumps(obj)
        assert "Generating initial dashboard" not in blob, (
            "ensure-health diagnostic leaked into the envelope"
        )
        assert "Captured baseline snapshot" not in blob, (
            "ensure-health stderr diagnostic leaked into the envelope"
        )
    assert "Generating initial dashboard" not in err, (
        "ensure-health diagnostic leaked into stderr"
    )
    assert "Captured baseline snapshot" not in err, (
        "ensure-health stderr diagnostic leaked into stderr"
    )
    log_text = log_file.read_text(encoding="utf-8")
    assert "Generating initial dashboard" in log_text, (
        "ensure-health diagnostic must reach the log file"
    )
    assert "Captured baseline snapshot" in log_text, (
        "ensure-health stderr diagnostic must reach the log file"
    )


def test_systemmessage_reaches_envelope_not_log(monkeypatch, tmp_path):
    """ensure-health and quality-cache ``{"systemMessage": ...}`` JSON lines
    are user-facing (shown to the USER's terminal, NOT injected into the
    model's context) and must keep reaching the SessionStart stdout envelope.
    They collapse to ``payload["systemMessage"]`` only -- no additionalContext,
    so zero model-context tax. Genuine diagnostics (on stderr) still go to
    the log file."""
    runner = _load_runner(monkeypatch, tmp_path)
    log_file = tmp_path / "diag.log"
    _install_runner_stubs(monkeypatch, runner, tmp_path, log_file)

    def _fake_ensure_health():
        # A user-facing systemMessage (dashboard URL) on stdout PLUS a
        # genuine diagnostic on stderr. The systemMessage must reach the
        # envelope; the diagnostic must go to the log.
        print(json.dumps({"systemMessage": "Dashboard daemon installed at http://localhost:7777"}))
        print("  [Token Optimizer] Generating initial dashboard", file=sys.stderr)

    monkeypatch.setattr(runner.measure, "run_ensure_health", _fake_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ctx-tax-sysmsg",
                                 "source": "startup"})

    rc, out, err = _run_runner_capturing(runner, {})
    assert rc == 0

    # stdout must be a JSON envelope carrying the systemMessage.
    stripped = out.strip()
    assert stripped, "stdout must carry the systemMessage envelope, not be empty"
    obj = json.loads(stripped)
    assert isinstance(obj, dict), f"stdout is not a JSON object: {stripped!r}"
    assert obj.get("systemMessage") == "Dashboard daemon installed at http://localhost:7777", (
        "the systemMessage must reach the envelope so the user sees it"
    )
    # The systemMessage must NOT appear in additionalContext (no tax).
    hso = obj.get("hookSpecificOutput", {})
    additional_context = hso.get("additionalContext", "") if isinstance(hso, dict) else ""
    assert "Dashboard daemon installed" not in additional_context, (
        "the systemMessage leaked into additionalContext (model context tax)"
    )
    # The genuine diagnostic must NOT be in the envelope.
    assert "Generating initial dashboard" not in json.dumps(obj), (
        "genuine diagnostic leaked into the envelope"
    )

    # stderr clean.
    assert "Dashboard daemon installed" not in err, (
        "systemMessage leaked into stderr"
    )
    assert "Generating initial dashboard" not in err, (
        "genuine diagnostic leaked into stderr"
    )

    # The genuine diagnostic went to the log; the systemMessage did NOT.
    log_text = log_file.read_text(encoding="utf-8")
    assert "Generating initial dashboard" in log_text, (
        "genuine diagnostic must reach the log file"
    )
    assert "Dashboard daemon installed" not in log_text, (
        "the systemMessage must NOT go to the log (it reached the user)"
    )


# --------------------------------------------------------------------------- #
# Integration test: drive sessionstart_runner.main() on a simulated
# systemd-less box with a missing dashboard, capturing real stdout/stderr.
# --------------------------------------------------------------------------- #

def test_integration_systemd_less_box_routes_diagnostics_to_log(
    monkeypatch, tmp_path,
):
    """Drive ``sessionstart_runner.main()`` end-to-end on a simulated
    systemd-less box (``_probe_systemd_user_bus`` -> False, dashboard missing).

    Asserts the full hook contract:
      * stdout is only the JSON envelope or empty (no diagnostic text)
      * stderr is empty (no diagnostic text)
      * the systemctl and baseline diagnostics landed in the log file
    """
    runner = _load_runner(monkeypatch, tmp_path)
    log_file = tmp_path / "diag.log"
    _install_runner_stubs(monkeypatch, runner, tmp_path, log_file)

    # Simulated systemd-less box.
    monkeypatch.setattr(runner.measure, "_probe_systemd_user_bus", lambda: False)
    monkeypatch.setattr(runner.measure, "_daemon_snapshot_sandboxed", lambda: False)
    # Dashboard missing: point DASHBOARD_PATH at a nonexistent file.
    monkeypatch.setattr(runner.measure, "DASHBOARD_PATH",
                        tmp_path / "nope" / "dashboard.html")

    # Drive the REAL _install_systemd_user_daemon diagnostic path (it writes
    # the systemctl "is not reachable" block to stderr when the bus probe
    # fails), plus the baseline-snapshot diagnostic that ensure-health prints
    # on first run. Both are the ~2900-token block this fix targets.
    def _fake_ensure_health():
        runner.measure._install_systemd_user_daemon(soft_fail=True)
        print("  [Token Optimizer] Captured baseline snapshot for structural savings",
              file=sys.stderr)

    monkeypatch.setattr(runner.measure, "run_ensure_health", _fake_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {
        "session_id": "sess-ctx-tax-integration",
        "source": "startup",
        "hook_event_name": "SessionStart",
    })

    rc, out, err = _run_runner_capturing(runner, {})
    assert rc == 0, f"runner exited {rc}"

    # stdout: only the JSON envelope or empty. No diagnostic text.
    stripped = out.strip()
    if stripped:
        obj = json.loads(stripped)
        assert isinstance(obj, dict), (
            f"stdout must be empty or one JSON object, got: {stripped[:300]!r}"
        )
        blob = json.dumps(obj)
        assert "is not reachable" not in blob, (
            "systemctl diagnostic leaked into stdout envelope"
        )
        assert "Captured baseline snapshot" not in blob, (
            "baseline diagnostic leaked into stdout envelope"
        )

    # stderr: empty of diagnostics.
    assert "is not reachable" not in err, (
        "systemctl diagnostic leaked into stderr"
    )
    assert "Captured baseline snapshot" not in err, (
        "baseline diagnostic leaked into stderr"
    )

    # Log file: both diagnostics present.
    log_text = log_file.read_text(encoding="utf-8")
    assert "is not reachable" in log_text, (
        "systemctl diagnostic must reach the log file"
    )
    assert "Captured baseline snapshot" in log_text, (
        "baseline diagnostic must reach the log file"
    )


# --------------------------------------------------------------------------- #
# Interactive CLI path: a human running ``measure.py setup-daemon`` (soft_fail
# = False) must still see the actionable error on stdout. These tests call
# measure.py functions directly and are UNCHANGED -- the fix is in the runner,
# not measure.py.
# --------------------------------------------------------------------------- #

def test_systemd_error_still_prints_to_stdout_for_interactive_cli(m, monkeypatch, capsys):
    """A human running ``measure.py setup-daemon`` interactively
    (soft_fail=False) must still see the actionable error on stdout -- we only
    suppress the injected-context path, never genuine interactive diagnostics."""
    monkeypatch.setattr(m, "_probe_systemd_user_bus", lambda: False)
    monkeypatch.setattr(m, "_daemon_snapshot_sandboxed", lambda: False)

    with pytest.raises(SystemExit):
        m._install_systemd_user_daemon(soft_fail=False)

    out, err = capsys.readouterr()
    assert "is not reachable" in out, (
        "interactive CLI run must keep the actionable error on stdout"
    )


def test_self_healed_notice_stays_on_stdout_for_interactive_run(m, monkeypatch, capsys):
    """An interactive ``measure.py ensure-health`` (a tty, not a hook) must
    still surface the Self-healed notice so a human sees it. The notice now
    routes to stderr (visible on the terminal), keeping stdout clean for both
    hook and interactive invocations."""
    _stub_ensure_health_to_heal(m, monkeypatch, added=1)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)

    m.run_ensure_health()

    _, err = capsys.readouterr()
    assert "Self-healed" in err, (
        "interactive run must still surface the Self-healed notice on stderr"
    )


# --------------------------------------------------------------------------- #
# Helper for the interactive Self-healed test (unchanged from the original).
# --------------------------------------------------------------------------- #

def _stub_ensure_health_to_heal(m, monkeypatch, added):
    """Stub the I/O-heavy ensure-health surface so run_ensure_health reaches
    the hook-heal block (the 'Self-healed N hooks' print) without touching the
    real filesystem or emitting unrelated stdout."""
    flags = _flags_env(m, monkeypatch, {
        "v5_welcome_shown": True,          # skip first-run welcome
        "enterprise_consent_shown": True,
        "last_hook_heal_check": 0,         # stale -> heal block runs
    })
    # Early ensure-health I/O that could print to stdout.
    monkeypatch.setattr(m, "_read_settings_json",
                        lambda: ({"cleanupPeriodDays": 99999}, None))
    monkeypatch.setattr(m, "_auto_capture_pristine_baseline", lambda: False)
    # Dashboard staleness block: skip by pretending no dashboard file exists.
    monkeypatch.setattr(m, "DASHBOARD_PATH", Path(m.TOKEN_OPTIMIZER_SNAPSHOT_DIR
                                                  if hasattr(m, "TOKEN_OPTIMIZER_SNAPSHOT_DIR")
                                                  else tempfile.gettempdir()) / "nope.html")
    # Daemon self-heal: return a quiet no-op state (no stdout print).
    monkeypatch.setattr(m, "_ensure_dashboard_daemon", lambda *a, **k: "noop-healthy")
    monkeypatch.setattr(m, "maybe_sweep_stale_leases", lambda: None)
    monkeypatch.setattr(m, "_ensure_vscode_extension", lambda: None)
    monkeypatch.setattr(m, "keepwarm_scheduler_repair", lambda: None)
    # Windows-only welcome: force non-Windows.
    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    # The heal block itself.
    monkeypatch.setattr(m, "_reconcile_sessionend_fossils",
                        lambda: {"rewritten": 0, "removed": 0, "stop_removed": 0})
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "setup_all_hooks",
                        lambda dry_run=False, verbose=False: {"added": added})
    # Later one-time/repair notices that could print to stdout.
    monkeypatch.setattr(m, "_fix_stale_settings_paths", lambda: 0)
    monkeypatch.setattr(m, "_migrate_statusline_to_stable_path", lambda: False)
    monkeypatch.setattr(m, "_heal_keepwarm_plist_path", lambda: False)
    monkeypatch.setattr(m, "_heal_windows_console_flash", lambda: False)
    monkeypatch.setattr(m, "_fix_malformed_hook_commands", lambda: 0)
    return flags
