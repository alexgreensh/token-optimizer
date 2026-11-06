#!/usr/bin/env python3
"""Regression guard for the consent-gate silent no-op race (v5.11.93, unit D).

The race: a SessionStart hook (ensure-health's ``last_hook_heal_check``
write, measure.py) creates ``<config>/token-optimizer/config.json`` BEFORE the
consent bootstrap writes ``enterprise_consent_shown`` / ``v5_welcome_shown``.
In that window run.py's ``_check_consent()`` must FAIL OPEN: every non-exempt
hook -- including the PreToolUse Bash compression rewrite
(``bash_hook.py --quiet``) -- must still be dispatched. Fail-closed-and-silent
there made compression silently do nothing for the whole window.

What must NOT change: a genuine explicit opt-out (``measure.py consent
--reset`` writes ``enterprise_consent_shown: false`` -- key PRESENT, value
False) stays gated, and the ensure-health/consent/v5 bootstrap exemption
keeps working under that opt-out so the user can re-grant.

Run: python3 -m pytest tests/test_consent_gate_race_window.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
RUN_PY = HOOKS / "run.py"
MEASURE_PY = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

# The exact argv the hooks.json PreToolUse "Bash" entry dispatches through
# run.py (see hooks/hooks.json PreToolUse matcher "Bash").
BASH_HOOK_ARGV = [
    "run.py",
    "skills/token-optimizer/scripts/bash_hook.py",
    "--quiet",
]
# A bootstrap-exempt command (SessionStart ensure-health entry).
ENSURE_HEALTH_ARGV = [
    "run.py",
    "skills/token-optimizer/scripts/measure.py",
    "ensure-health",
    "--once-mark",
]


def _load_run_py():
    """Import hooks/run.py as a fresh module (it has no package-relative imports)."""
    spec = importlib.util.spec_from_file_location("race_window_run_py", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    _split_exec_module(mod)
    return mod


@pytest.fixture
def race_env(monkeypatch, tmp_path):
    """Real run.py against a tmp CLAUDE_CONFIG_DIR. Returns a helper object.

    Does NOT stub _check_consent: the whole point is driving the REAL consent
    read against a tmp config.json. Popen and signal.signal are stubbed so no
    child process spawns and pytest's handlers are untouched.
    """
    run = _load_run_py()

    claude_dir = tmp_path / "claude"
    cfg_dir = claude_dir / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.json"

    # run._check_consent honors CLAUDE_CONFIG_DIR (absolute, existing,
    # non-symlink). Keep it from falling through to CLAUDE_PLUGIN_DATA /
    # CODEX_HOME / the host ~/.claude.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    monkeypatch.setattr(run, "_plugin_disabled_by_host", lambda: False)

    spawned = {"argv": []}

    class _FakeProc:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def _popen(cmd, *a, **k):
        spawned["argv"].append(cmd)
        return _FakeProc()

    monkeypatch.setattr(run.subprocess, "Popen", _popen)
    monkeypatch.setattr(run.signal, "signal", lambda *_a, **_k: None)

    def write_config(obj: dict) -> None:
        cfg_path.write_text(json.dumps(obj), encoding="utf-8")

    def dispatch(argv) -> int:
        spawned["argv"].clear()
        monkeypatch.setattr(sys, "argv", argv)
        return run.main()

    class _Env:
        pass

    env = _Env()
    env.run = run
    env.cfg_path = cfg_path
    env.write_config = write_config
    env.dispatch = dispatch
    env.spawned = spawned
    return env


def test_race_window_config_exists_flags_absent_consent_fails_open(race_env):
    """THE race state: config.json EXISTS (written by ensure-health's
    last_hook_heal_check) with NO enterprise_consent_shown and NO
    v5_welcome_shown. Consent must read True (fail-open), not False."""
    race_env.write_config({"last_hook_heal_check": 1756300000})
    assert race_env.run._check_consent(REPO) is True, (
        "flags-absent race window must fail OPEN; fail-closed here is the "
        "v5.11.93 silent no-op"
    )


def test_race_window_pretooluse_bash_hook_still_fires(race_env):
    """End-to-end through run.main(): with config.json present but consent
    flags absent, the PreToolUse Bash compression hook (bash_hook.py) MUST
    reach Popen -- it must NOT return 0 silently at the consent gate."""
    race_env.write_config({"last_hook_heal_check": 1756300000})
    rc = race_env.dispatch(BASH_HOOK_ARGV)
    assert len(race_env.spawned["argv"]) == 1, (
        "PreToolUse Bash hook was silently gated off in the flags-absent "
        "window (v5.11.93 race): run.py returned before Popen"
    )
    # The dispatch must target bash_hook.py via module_runner.
    assert any("bash_hook" in part for part in race_env.spawned["argv"][0])
    assert rc == 0


def test_explicit_opt_out_still_gates_pretooluse_bash(race_env):
    """A genuine opt-out is NOT the race window: `consent --reset` writes
    enterprise_consent_shown: false (key PRESENT). That state must stay
    fail-CLOSED for non-exempt hooks -- never weakened by the race fix."""
    race_env.write_config({"enterprise_consent_shown": False})
    assert race_env.run._check_consent(REPO) is False, (
        "explicit opt-out (consent --reset) must remain gated"
    )
    rc = race_env.dispatch(BASH_HOOK_ARGV)
    assert race_env.spawned["argv"] == [], (
        "explicit opt-out must still gate the PreToolUse Bash hook"
    )
    assert rc == 0  # gate is a silent 0 by design; the point is no Popen


def test_explicit_opt_out_still_allows_bootstrap_exemption(race_env):
    """Under an explicit opt-out the ensure-health bootstrap must STILL
    dispatch (it is the only path that can re-grant consent); the fix must
    not break the ensure-health/consent/v5 exemption."""
    race_env.write_config({"enterprise_consent_shown": False})
    rc = race_env.dispatch(ENSURE_HEALTH_ARGV)
    assert len(race_env.spawned["argv"]) == 1, (
        "ensure-health bootstrap must stay consent-exempt even under an "
        "explicit opt-out, or the user can never re-grant"
    )
    assert rc == 0


def test_missing_config_still_fails_open(race_env):
    """Pre-existing invariant, kept as a guard: no config.json at all must
    fail open (fresh install before any writer runs)."""
    assert not race_env.cfg_path.exists()
    assert race_env.run._check_consent(REPO) is True
    race_env.dispatch(BASH_HOOK_ARGV)
    assert len(race_env.spawned["argv"]) == 1


def test_v5_welcome_backfill_grants_when_enterprise_key_absent(race_env):
    """Legacy path, kept: v5_welcome_shown true with NO enterprise_consent_shown
    key => consent True and the enterprise flag is backfilled (pre-enterprise-
    consent users who saw the v5 welcome implicitly consented)."""
    race_env.write_config({"v5_welcome_shown": True})
    assert race_env.run._check_consent(REPO) is True
    cfg = json.loads(race_env.cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("enterprise_consent_shown") is True, (
        "v5 welcome backfill must persist enterprise_consent_shown: true"
    )


def test_explicit_opt_out_wins_over_v5_backfill(race_env):
    """D1: enterprise_consent_shown PRESENT and False is an explicit
    opt-out (`consent --reset`). The v5_welcome_shown backfill must NOT grant
    over it: consent stays False across repeated hook invocations and the
    backfill must not rewrite the flag to true."""
    race_env.write_config({"enterprise_consent_shown": False, "v5_welcome_shown": True})
    assert race_env.run._check_consent(REPO) is False, (
        "explicit opt-out must survive the v5 welcome backfill (D1): "
        "backfill may only fire when the enterprise key is ABSENT"
    )
    # Second hook invocation: still gated (the opt-out is stable).
    assert race_env.run._check_consent(REPO) is False
    cfg = json.loads(race_env.cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("enterprise_consent_shown") is False, (
        "backfill must not rewrite an explicit opt-out to true"
    )


def test_consent_reset_stays_opted_out_end_to_end(monkeypatch, tmp_path):
    """D1, full flow through the REAL measure.py CLI: from the
    post-bootstrap state (both flags true, exactly what the SessionStart
    ensure-health bootstrap persists), run `consent --reset`. The pre-fix
    --reset left v5_welcome_shown true, so the run.py backfill silently
    re-granted consent on the next hook. After the fix: --reset clears BOTH
    flags, and _check_consent returns False and STAYS False across repeated
    hook invocations.

    The granted state is seeded by direct file write (not a `consent --grant`
    subprocess) on purpose: the config lease's post-release reuse window
    (~10s) silently drops a second process's write that soon after another
    writer, which would make a grant->reset subprocess pair flaky for reasons
    unrelated to D1 (pre-existing lease anti-churn, reported separately)."""
    claude_dir = tmp_path / "claude-e2e"
    # CLAUDE_CONFIG_DIR is only honored when absolute, EXISTING, non-symlink.
    claude_dir.mkdir(parents=True)
    cfg_dir = claude_dir / "token-optimizer"
    cfg_dir.mkdir(parents=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(
        json.dumps({"enterprise_consent_shown": True, "v5_welcome_shown": True}),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(claude_dir),
    }
    for var in ("CLAUDE_PLUGIN_DATA", "CODEX_HOME", "TOKEN_OPTIMIZER_SNAPSHOT_DIR"):
        env.pop(var, None)

    reset = subprocess.run(
        [sys.executable, str(MEASURE_PY), "consent", "--reset"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert reset.returncode == 0, reset.stderr

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("enterprise_consent_shown") is False, (
        "consent --reset must persist enterprise_consent_shown: false"
    )
    assert cfg.get("v5_welcome_shown") is False, (
        "consent --reset must ALSO clear v5_welcome_shown, or the run.py "
        "backfill re-grants consent on the next hook (D1)"
    )

    # Drive the REAL in-process consent gate against the post-reset config,
    # twice (two hook invocations). It must not flip back to True.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    run = _load_run_py()
    assert run._check_consent(REPO) is False, (
        "after consent --reset the gate must read False"
    )
    assert run._check_consent(REPO) is False, (
        "opt-out must survive repeated hook invocations (no silent re-grant)"
    )
    cfg_after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg_after.get("enterprise_consent_shown") is False
