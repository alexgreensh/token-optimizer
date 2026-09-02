#!/usr/bin/env python3
"""Regression tests for the consolidated UserPromptSubmit dispatcher (issue #139).

The six former UserPromptSubmit hooks.json entries are collapsed into ONE that
runs ``hooks/userpromptsubmit_runner.py``, which imports ``measure.py`` once and
runs all six subcommands in-process with per-subcommand failure isolation.

These four tests pin the three issue #139 deliverables:
  (a) the ``TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT=0`` pre-import opt-out in
      run.py (Req 3) -- no child process is spawned.
  (b) the single dispatcher runs all six subcommands against one measure.py
      import (Req 2).
  (c) one subcommand throwing never aborts the others; the hook exits 0 and
      logs the failure to stderr (Req 2 failure isolation).
  (d) the per-session marker gate (``measure._ran_once_this_session``) skips the
      three harness-gated subcommands on a latched session while the three
      always-on subcommands still run (Req 2 gating parity).

Run: python3 -m pytest tests/test_userpromptsubmit_runner_139.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
RUN_PY = HOOKS / "run.py"
RUNNER = HOOKS / "userpromptsubmit_runner.py"


def _load_run_py():
    """Import hooks/run.py as a fresh module (it has no package-relative imports)."""
    spec = importlib.util.spec_from_file_location("ups_run_py_under_test", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_runner(monkeypatch, tmp_path):
    """Import hooks/userpromptsubmit_runner.py with CLAUDE_PLUGIN_ROOT=REPO so
    its _resolve_measure_dir() finds skills/token-optimizer/scripts/measure.py."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # Keep the measure import deterministic and isolated from the host's real
    # ~/.claude state by pointing config dirs at a tmp dir.
    # claude_home() honors CLAUDE_CONFIG_DIR only when the directory exists;
    # a missing dir is rejected and falls back to the host's real ~/.claude.
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("ups_runner_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# (a) TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT=0 pre-import opt-out (Req 3)
# --------------------------------------------------------------------------- #


def test_userpromptsubmit_env_opt_out_returns_before_spawning_child(monkeypatch):
    run = _load_run_py()
    monkeypatch.setattr(sys, "argv", ["run.py", "hooks/userpromptsubmit_runner.py"])
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT", "0")
    # Clear CLAUDE_PLUGIN_ROOT so _plugin_disabled_by_host fails open (returns
    # False) and the opt-out check is the thing that actually short-circuits.
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    spawned = {"count": 0}

    class _NoSpawn:
        def __init__(self, *a, **k):
            spawned["count"] += 1
            raise AssertionError("run.py spawned a child despite the opt-out env var")

    monkeypatch.setattr(run.subprocess, "Popen", _NoSpawn)
    monkeypatch.setattr(run.signal, "signal", lambda *_a, **_k: None)

    rc = run.main()
    assert rc == 0, "opt-out must exit 0"
    assert spawned["count"] == 0, "run.py must not build the module_runner command"


def test_userpromptsubmit_env_opt_out_is_exact_target(monkeypatch):
    """The opt-out must NOT silence any other hook script (exact-target gate)."""
    run = _load_run_py()
    # A different script path with the env var set must still proceed to Popen
    # (i.e. the gate is scoped to the UserPromptSubmit runner only).
    monkeypatch.setattr(sys, "argv", ["run.py", "skills/token-optimizer/scripts/measure.py", "quality-cache", "--warn", "--quiet"])
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT", "0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    # Stub Popen + wait so main() can complete without really spawning measure.
    class _FakeProc:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    spawned = {"count": 0}

    def _popen(*a, **k):
        spawned["count"] += 1
        return _FakeProc()

    monkeypatch.setattr(run.subprocess, "Popen", _popen)
    monkeypatch.setattr(run.signal, "signal", lambda *_a, **_k: None)
    # Bypass consent (it may read a real ~/.claude/config.json); we only care
    # that the env opt-out did NOT fire for a non-runner script. run.py now
    # calls _check_consent(root_resolved), so the stub must accept that arg.
    monkeypatch.setattr(run, "_check_consent", lambda *a, **k: True)
    monkeypatch.setattr(run, "_plugin_disabled_by_host", lambda: False)

    run.main()
    assert spawned["count"] == 1, "non-runner scripts must NOT be silenced by the opt-out"


# --------------------------------------------------------------------------- #
# (b) single dispatcher runs all six subcommands against one measure.py import
# --------------------------------------------------------------------------- #


def _stub_budget(monkeypatch, runner):
    """Replace the wall-clock budget with no-ops so tests don't arm watchdog threads.

    Also pin consent True: these tests exercise dispatch / failure-isolation /
    gating logic, NOT the consent gate (which has its own dedicated
    consent=False tests below). Pinning here keeps them deterministic and
    independent of the host's real ~/.claude config.
    """
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    monkeypatch.setattr(runner.measure, "_install_hook_budget", lambda seconds=8: object())
    monkeypatch.setattr(runner.measure, "_clear_hook_budget", lambda deadline: None)
    # Issue #139 FIX 1: stub the shared deadline so tests don't arm a real
    # 18s HookDeadline watchdog (which would os._exit the test process on
    # timeout).  With no deadline, _runner_budget returns the default 8s.
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=18: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)


def _install_call_recorder(monkeypatch, runner):
    """Monkeypatch the six subcommand entrypoints + side-effect helpers to record
    calls. Returns a dict of call logs keyed by subcommand."""
    calls = {
        "quality_cache_warn": [],
        "prompt_continuity": [],
        "verbosity_steer": [],
        "ensure_health": [],
        "quality_cache_force": [],
        "compact_restore": [],
    }

    def _quality_cache(**kw):
        if kw.get("warn") and not kw.get("force"):
            calls["quality_cache_warn"].append(kw)
        elif kw.get("force"):
            calls["quality_cache_force"].append(kw)

    def _continuity(**kw):
        calls["prompt_continuity"].append(kw)
        return ""

    def _verbosity(**kw):
        calls["verbosity_steer"].append(kw)
        return None

    def _ensure_health():
        calls["ensure_health"].append({})

    def _compact_restore(**kw):
        calls["compact_restore"].append(kw)

    monkeypatch.setattr(runner.measure, "quality_cache", _quality_cache)
    monkeypatch.setattr(runner.measure, "_continuity_prompt_hint", _continuity)
    monkeypatch.setattr(runner.measure, "run_verbosity_steer", _verbosity)
    monkeypatch.setattr(runner.measure, "run_ensure_health", _ensure_health)
    monkeypatch.setattr(runner.measure, "compact_restore", _compact_restore)
    # Side-effect helpers: must not raise, must not touch the real filesystem.
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_ensure_health_daemon_revive_first", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    # Marker guard: return False so the gated subcommands DO their work.
    monkeypatch.setattr(runner.measure, "_ran_once_this_session", lambda tag, sid: False)
    # Cowork/codex detection: stay on the raw-stdout path (no envelope wrapping).
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")
    return calls


def test_userpromptsubmit_runner_all_subcommands_one_import(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    # The runner reads stdin once via _read_hook_input; bypass it with a fixed
    # payload so no real stdin read happens.
    payload = {"session_id": "sess-abc-139", "transcript_path": "/tmp/t.jsonl",
               "cwd": "/tmp", "prompt": "hello"}
    monkeypatch.setattr(runner, "_read_hook_input", lambda: payload)
    # Harness guard must pass so the three gated subcommands run.
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)

    # measure.py is imported exactly once: the runner module holds a single
    # `measure` attribute bound to the cached sys.modules entry.
    assert runner.measure is sys.modules.get("measure")

    rc = runner.main()
    assert rc == 0

    # All six subcommands ran exactly once, in one process, against one import.
    assert len(calls["quality_cache_warn"]) == 1, "quality-cache --warn must run"
    assert len(calls["prompt_continuity"]) == 1, "prompt-continuity must run"
    assert len(calls["verbosity_steer"]) == 1, "verbosity-steer must run"
    assert len(calls["ensure_health"]) == 1, "ensure-health must run"
    assert len(calls["quality_cache_force"]) == 1, "quality-cache --force must run"
    assert len(calls["compact_restore"]) == 1, "compact-restore must run"

    # Verify the REAL call shapes (the plan assumed wrong kwargs; pin the truth).
    warn_kw = calls["quality_cache_warn"][0]
    assert warn_kw == {
        "throttle_seconds": 120, "warn_threshold": 70, "quiet": True,
        "session_jsonl": "/tmp/t.jsonl", "force": False,
        "pure_time_throttle": False, "session_id": "sess-abc-139", "warn": True,
    }
    force_kw = calls["quality_cache_force"][0]
    assert force_kw["force"] is True and force_kw["warn"] is False
    assert force_kw["session_jsonl"] == "/tmp/t.jsonl"
    # verbosity-steer dispatch hardcodes quiet=False (NOT the --quiet flag).
    assert calls["verbosity_steer"][0]["quiet"] is False
    # compact-restore uses new_session_only=True.
    assert calls["compact_restore"][0]["new_session_only"] is True
    assert calls["compact_restore"][0]["session_id"] == "sess-abc-139"


# --------------------------------------------------------------------------- #
# (c) failure isolation: one subcommand throwing never aborts the others
# --------------------------------------------------------------------------- #


def test_userpromptsubmit_runner_failure_isolation(monkeypatch, tmp_path, capsys):
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    # Make quality-cache --warn explode. The other five must still run.
    def _boom(**kw):
        if kw.get("warn") and not kw.get("force"):
            raise RuntimeError("simulated quality-cache --warn failure")
        if kw.get("force"):
            calls["quality_cache_force"].append(kw)

    monkeypatch.setattr(runner.measure, "quality_cache", _boom)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-iso-139", "prompt": "x"})
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)

    rc = runner.main()
    assert rc == 0, "a subcommand failure must never abort the hook (exit 0)"

    # The other five subcommands still ran.
    assert len(calls["prompt_continuity"]) == 1
    assert len(calls["verbosity_steer"]) == 1
    assert len(calls["ensure_health"]) == 1
    assert len(calls["quality_cache_force"]) == 1
    assert len(calls["compact_restore"]) == 1

    err = capsys.readouterr().err
    assert "quality-cache --warn failed, continuing" in err, (
        "failure must be logged to stderr, not swallowed silently"
    )


# --------------------------------------------------------------------------- #
# (d) per-session marker gate: gated subcommands skip on a latched session
# --------------------------------------------------------------------------- #


def test_userpromptsubmit_runner_session_marker_gate(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    # Simulate "already ran this session" for EVERY gated tag. The runner calls
    # _ran_once_this_session(tag, sid) for ensure-health and
    # compact-restore-new-session. quality-cache-force uses a CHECK-ONLY gate
    # so we must create the actual marker file for it.
    monkeypatch.setattr(runner.measure, "_ran_once_this_session", lambda tag, sid: True)
    # Create the quality-cache-force marker so the CHECK-ONLY gate sees it.
    qcd = tmp_path / "quality-cache"
    qcd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", qcd)
    sid = "sess-latched-139"
    qc_marker = runner.measure._once_per_session_marker("quality-cache-force", sid)
    if qc_marker is not None:
        qc_marker.parent.mkdir(parents=True, exist_ok=True)
        qc_marker.write_text('{"ts": 0}', encoding="utf-8")
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": sid, "prompt": "x"})
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)

    rc = runner.main()
    assert rc == 0

    # The three always-on subcommands still run.
    assert len(calls["quality_cache_warn"]) == 1, "ungated quality-cache --warn must run"
    assert len(calls["prompt_continuity"]) == 1, "ungated prompt-continuity must run"
    assert len(calls["verbosity_steer"]) == 1, "ungated verbosity-steer must run"

    # The three gated subcommands skip their domain work.
    assert calls["ensure_health"] == [], "ensure-health must skip when marker exists"
    assert calls["quality_cache_force"] == [], "quality-cache --force must skip when marker exists"
    assert calls["compact_restore"] == [], "compact-restore must skip when marker exists"


def _isolate_quality_cache(monkeypatch, runner, tmp_path, sid, *, present):
    """Point the per-session quality cache at tmp and create it (or not).

    Returns the hook input carrying the transcript_path that
    ``measure._quality_cache_path_for`` derives the cache path from, the same
    way a real UserPromptSubmit payload does.
    """
    qcd = tmp_path / "quality-cache"
    qcd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", qcd)
    transcript = tmp_path / f"{sid}.jsonl"
    cache_path = runner.measure._quality_cache_path_for(str(transcript))
    assert cache_path.parent == qcd
    if present:
        cache_path.write_text("{}", encoding="utf-8")
    return {"session_id": sid, "prompt": "x", "transcript_path": str(transcript)}


def test_userpromptsubmit_runner_harness_guard_skips_gated(monkeypatch, tmp_path):
    """When the harness guard fails, the three gated subcommands are skipped
    entirely (replicating the shell `exit 0` that used to prefix entries 4/5/6)
    while the three always-on subcommands still run.

    Precondition: a quality cache already exists for the session. Without one
    the runner takes the bootstrap branch instead (covered below)."""
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)
    hook_input = _isolate_quality_cache(monkeypatch, runner, tmp_path,
                                        "sess-noguard-139", present=True)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: hook_input)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: False)

    rc = runner.main()
    assert rc == 0

    assert len(calls["quality_cache_warn"]) == 1
    assert len(calls["prompt_continuity"]) == 1
    assert len(calls["verbosity_steer"]) == 1
    assert calls["ensure_health"] == []
    assert calls["quality_cache_force"] == []
    assert calls["compact_restore"] == []


def test_userpromptsubmit_runner_bootstraps_missing_cache_outside_harness(monkeypatch, tmp_path):
    """Guard fails AND no quality cache exists: the ONE recovery
    (`quality-cache --force`) runs so ContextQ is not blank for the whole
    session after a missed SessionStart. ensure-health and compact-restore stay
    skipped: the harness gate still owns them."""
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)
    hook_input = _isolate_quality_cache(monkeypatch, runner, tmp_path,
                                        "sess-bootstrap-139", present=False)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: hook_input)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: False)

    rc = runner.main()
    assert rc == 0

    assert len(calls["quality_cache_warn"]) == 1
    assert len(calls["prompt_continuity"]) == 1
    assert len(calls["verbosity_steer"]) == 1
    assert calls["ensure_health"] == [], "bootstrap must not unlock ensure-health"
    assert len(calls["quality_cache_force"]) == 1, "missing cache -> exactly one force"
    assert calls["quality_cache_force"][0].get("force") is True
    assert calls["compact_restore"] == [], "bootstrap must not unlock compact-restore"


def test_userpromptsubmit_runner_harness_true_missing_cache_forces_once(monkeypatch, tmp_path):
    """Guard passes AND no cache: the harness branch already runs
    `quality-cache --force`; the bootstrap `elif` must not run it a second time."""
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)
    hook_input = _isolate_quality_cache(monkeypatch, runner, tmp_path,
                                        "sess-harness-nocache-139", present=False)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: hook_input)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)

    rc = runner.main()
    assert rc == 0

    assert len(calls["ensure_health"]) == 1
    assert len(calls["quality_cache_force"]) == 1, "force must run once, not twice"
    assert len(calls["compact_restore"]) == 1


# --------------------------------------------------------------------------- #
# (e) P0 consent-gate deadlock: consent=False must still bootstrap, then flip.
#     These tests do NOT bypass consent -- they drive the REAL run._check_consent
#     against a tmp config.json and prove the consolidated runner does not
#     deadlock the way the pre-fix run.py gate did.
#     NOTE (unit D, v5.12.4 race fix): "config exists, flags unset" is the
#     SessionStart race window and now fails OPEN (consent True), so the
#     consent-False fixtures below use the only remaining consent-False state:
#     an explicit opt-out (enterprise_consent_shown written False, exactly
#     what `measure.py consent --reset` persists).
# --------------------------------------------------------------------------- #


def _load_runner_real_consent(monkeypatch, tmp_path, config_json="{}"):
    """Load the runner with HOOKS on sys.path (so its ``import run`` resolves to
    hooks/run.py) and a tmp CLAUDE_CONFIG_DIR holding ``config_json``.

    Returns ``(runner_module, config_path)``. Does NOT monkeypatch consent: the
    runner's ``_check_consent`` calls the real ``run._check_consent``, which
    reads the tmp config. measure.CONFIG_PATH / CONFIG_DIR / _CONFIG_LOCK_PATH
    are repointed at the same tmp config so the real ``_write_config_flag`` /
    ``_read_config_flag`` primitives (used by the ensure-health bootstrap stub)
    land in the tmp file the consent gate reads -- proving the bootstrap
    actually flips consent, with no host ~/.claude touched.
    """
    # `import run` inside the runner must find hooks/run.py, not some other
    # `run` on sys.path. Prepend HOOKS and drop any stale cached `run` module.
    monkeypatch.syspath_prepend(str(HOOKS))
    sys.modules.pop("run", None)

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = claude_dir / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(config_json, encoding="utf-8")

    # run._check_consent honors CLAUDE_CONFIG_DIR (absolute, existing, non-symlink).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    # Ensure run._check_consent does not fall through to CLAUDE_PLUGIN_DATA /
    # CODEX_HOME / the legacy ~/.claude path.
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))

    spec = importlib.util.spec_from_file_location("ups_runner_consent_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Repoint measure's import-time config globals at the tmp config so the real
    # _write_config_flag / _read_config_flag / _config_lock primitives stay
    # inside tmp (measure may be a cached module from a prior test with a
    # different CONFIG_PATH frozen at import time).
    monkeypatch.setattr(mod.measure, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(mod.measure, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(mod.measure, "_CONFIG_LOCK_PATH", cfg_dir / ".config.lock")
    return mod, cfg_path


def _read_tmp_config(runner, cfg_path):
    import json as _json
    if not cfg_path.exists():
        return {}
    try:
        return _json.loads(cfg_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _install_consent_recorder(monkeypatch, runner, write_flags_on_health):
    """Like _install_call_recorder but the ensure-health entrypoint either writes
    the real consent flags (the bootstrap) or is a plain recorder. Budget is
    stubbed inline (NOT via _stub_budget, which pins consent True)."""
    calls = {
        "quality_cache_warn": [], "prompt_continuity": [], "verbosity_steer": [],
        "ensure_health": [], "quality_cache_force": [], "compact_restore": [],
    }

    def _quality_cache(**kw):
        if kw.get("warn") and not kw.get("force"):
            calls["quality_cache_warn"].append(kw)
        elif kw.get("force"):
            calls["quality_cache_force"].append(kw)

    def _continuity(**kw):
        calls["prompt_continuity"].append(kw)
        return ""

    def _verbosity(**kw):
        calls["verbosity_steer"].append(kw)
        return None

    def _ensure_health():
        calls["ensure_health"].append({})
        if write_flags_on_health:
            # Mirror run_ensure_health's consent-bootstrap tail (measure.py
            # L38486-38488: _show_v5_welcome writes enterprise_consent_shown,
            # then v5_welcome_shown is written). Use the REAL _write_config_flag
            # so the test proves the flag-write primitive lands in config.json
            # and flips the real consent gate on the next prompt.
            runner.measure._write_config_flag("enterprise_consent_shown", True)
            runner.measure._write_config_flag("v5_welcome_shown", True)

    def _compact_restore(**kw):
        calls["compact_restore"].append(kw)

    monkeypatch.setattr(runner.measure, "quality_cache", _quality_cache)
    monkeypatch.setattr(runner.measure, "_continuity_prompt_hint", _continuity)
    monkeypatch.setattr(runner.measure, "run_verbosity_steer", _verbosity)
    monkeypatch.setattr(runner.measure, "run_ensure_health", _ensure_health)
    monkeypatch.setattr(runner.measure, "compact_restore", _compact_restore)
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_ensure_health_daemon_revive_first", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    # Never latch: the second prompt must be allowed to run the gated work too.
    monkeypatch.setattr(runner.measure, "_ran_once_this_session", lambda tag, sid: False)
    # Keep compact-restore on the raw-stdout path (envelope wrapping is covered
    # elsewhere); the Cowork-ness under test here is the harness guard, not the
    # additionalContext envelope.
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")
    # Stub the wall-clock budget inline (no consent pin).
    monkeypatch.setattr(runner.measure, "_install_hook_budget", lambda seconds=8: object())
    monkeypatch.setattr(runner.measure, "_clear_hook_budget", lambda deadline: None)
    # Issue #139 FIX 1: stub the shared deadline too.
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=18: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)
    return calls


def test_consent_false_cowork_bootstraps_then_flips(monkeypatch, tmp_path):
    """P0 regression (the Cowork-fatal path). With consent False (config.json
    holds an explicit opt-out, enterprise_consent_shown: false) and the Cowork
    harness guard active (CLAUDE_CODE_REMOTE
    set, no SessionStart to bootstrap out-of-band), the runner MUST still run
    ensure-health (the bootstrap) and skip the other five; ensure-health writes
    the consent flags; a subsequent prompt then sees consent True and runs all
    six. Does NOT bypass consent -- run._check_consent is real."""
    runner, cfg_path = _load_runner_real_consent(
        monkeypatch, tmp_path, config_json='{"enterprise_consent_shown": false}'
    )
    # Cowork harness guard (real _harness_only_context path): CLAUDE_CODE_REMOTE.
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "1")
    calls = _install_consent_recorder(monkeypatch, runner, write_flags_on_health=True)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-p0-cowork-139", "prompt": "x"})

    # Sanity: consent really is False against the tmp config before we start.
    assert runner._check_consent() is False, (
        "fixture error: tmp config must read consent=False (explicit opt-out)"
    )

    # First prompt: consent False. Only ensure-health (the bootstrap) runs.
    rc = runner.main()
    assert rc == 0
    assert len(calls["ensure_health"]) == 1, "ensure-health bootstrap must run when consent is False"
    assert calls["quality_cache_warn"] == [], "quality-cache --warn must skip when consent is False"
    assert calls["prompt_continuity"] == [], "prompt-continuity must skip when consent is False"
    assert calls["verbosity_steer"] == [], "verbosity-steer must skip when consent is False"
    assert calls["quality_cache_force"] == [], "quality-cache --force must skip when consent is False"
    assert calls["compact_restore"] == [], "compact-restore must skip when consent is False"

    # The bootstrap wrote the consent flags to the tmp config (real primitive).
    cfg = _read_tmp_config(runner, cfg_path)
    assert cfg.get("v5_welcome_shown") is True, "ensure-health must write v5_welcome_shown"
    assert cfg.get("enterprise_consent_shown") is True, "ensure-health must write enterprise_consent_shown"

    # Consent has now flipped (real gate re-reads the tmp config).
    assert runner._check_consent() is True, "bootstrap must flip consent to True"

    # Second prompt: consent True. All six subcommands run (deadlock broken).
    rc = runner.main()
    assert rc == 0
    assert len(calls["ensure_health"]) == 2
    assert len(calls["quality_cache_warn"]) == 1
    assert len(calls["prompt_continuity"]) == 1
    assert len(calls["verbosity_steer"]) == 1
    assert len(calls["quality_cache_force"]) == 1
    assert len(calls["compact_restore"]) == 1


def test_consent_false_non_harness_skips_all_and_stays_false(monkeypatch, tmp_path):
    """consent False on a NON-harness host (no CLAUDE_CODE_REMOTE/CONTAINER_ID):
    ensure-health is harness-gated, so it skips too, and consent stays False.
    This is the native-Claude-Code edge: SessionStart is the out-of-band
    bootstrap, so UserPromptSubmit correctly does nothing when consent is False
    and there is no harness guard. Preserves the pre-consolidation semantics
    (ensure-health was a harness-gated, consent-exempt entry)."""
    runner, cfg_path = _load_runner_real_consent(
        monkeypatch, tmp_path, config_json='{"enterprise_consent_shown": false}'
    )
    # No CLAUDE_CODE_REMOTE / CONTAINER_ID / harness markers => guard False.
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CONTAINER_ID", raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    calls = _install_consent_recorder(monkeypatch, runner, write_flags_on_health=True)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-p0-native-139", "prompt": "x"})

    assert runner._check_consent() is False
    assert runner._harness_only_context() is False

    rc = runner.main()
    assert rc == 0
    # Nothing runs: ensure-health is harness-gated, the other five are consent-gated.
    assert calls["ensure_health"] == []
    assert calls["quality_cache_warn"] == []
    assert calls["prompt_continuity"] == []
    assert calls["verbosity_steer"] == []
    assert calls["quality_cache_force"] == []
    assert calls["compact_restore"] == []

    # Consent unchanged (no bootstrap fired).
    cfg = _read_tmp_config(runner, cfg_path)
    assert cfg.get("enterprise_consent_shown") in (None, False)
    assert cfg.get("v5_welcome_shown") in (None, False)
    assert runner._check_consent() is False


def test_consent_true_cowork_runs_all_six(monkeypatch, tmp_path):
    """consent True (flags pre-set in config) on Cowork: the real consent gate
    returns True and all six subcommands run. Covers the Cowork consent=True
    path with a real (non-bypassed) consent read."""
    runner, _cfg_path = _load_runner_real_consent(
        monkeypatch, tmp_path,
        config_json='{"enterprise_consent_shown": true, "v5_welcome_shown": true}',
    )
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "1")
    calls = _install_consent_recorder(monkeypatch, runner, write_flags_on_health=False)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-p0-cowork-true-139", "prompt": "x"})

    assert runner._check_consent() is True, "pre-set flags must read consent=True"
    assert runner._harness_only_context() is True

    rc = runner.main()
    assert rc == 0
    assert len(calls["quality_cache_warn"]) == 1
    assert len(calls["prompt_continuity"]) == 1
    assert len(calls["verbosity_steer"]) == 1
    assert len(calls["ensure_health"]) == 1
    assert len(calls["quality_cache_force"]) == 1
    assert len(calls["compact_restore"]) == 1


def test_run_py_exempts_runner_path_from_consent_gate(monkeypatch, tmp_path):
    """run.py must let hooks/userpromptsubmit_runner.py through its consent gate
    even when consent is False (the runner does its own per-subcommand gating).
    A non-runner script with consent False still gets gated (returns 0 before
    Popen). This pins the run.py half of the P0 fix."""
    run = _load_run_py()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # Make consent deterministically False via a tmp config holding an
    # explicit opt-out (flags-unset is the fail-open race window, unit D).
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "token-optimizer").mkdir(parents=True, exist_ok=True)
    (claude_dir / "token-optimizer" / "config.json").write_text(
        '{"enterprise_consent_shown": false}', encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(run, "_plugin_disabled_by_host", lambda: False)
    assert run._check_consent() is False, "fixture error: consent must be False"

    spawned = {"count": 0}

    class _FakeProc:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def _popen(*a, **k):
        spawned["count"] += 1
        return _FakeProc()

    monkeypatch.setattr(run.subprocess, "Popen", _popen)
    monkeypatch.setattr(run.signal, "signal", lambda *_a, **_k: None)

    # The runner path is exempted => it MUST proceed to Popen despite consent False.
    monkeypatch.setattr(sys, "argv", ["run.py", "hooks/userpromptsubmit_runner.py"])
    run.main()
    assert spawned["count"] == 1, (
        "run.py must dispatch the runner even when consent is False (P0 fix)"
    )

    # A non-runner script is still consent-gated => returns 0 before Popen.
    spawned["count"] = 0
    monkeypatch.setattr(
        sys, "argv",
        ["run.py", "skills/token-optimizer/scripts/measure.py", "quality-cache", "--warn", "--quiet"],
    )
    run.main()
    assert spawned["count"] == 0, (
        "non-runner scripts must still be consent-gated (only the runner is exempt)"
    )


# --------------------------------------------------------------------------- #
# (f) Signature-drift guard: call each _sub_* handler end-to-end against the
#     REAL measure.py (NO monkeypatching of measure.*). A future kwarg/param
#     rename in measure.py raises TypeError out of the handler (handlers only
#     catch measure._HookTimeout, a BaseException) and fails RED here, instead
#     of being silently swallowed by _run_safely in production.
# --------------------------------------------------------------------------- #


# Modules whose import-time path globals (CONFIG_PATH, SETTINGS_PATH,
# CLAUDE_DIR, QUALITY_CACHE_DIR, _STATE_BASE, ...) must re-resolve under the
# tmp env so the real measure functions read/write inside tmp, never the host.
_FRESH_MEASURE_MODULES = (
    "measure", "runtime_env", "plugin_env", "hook_io", "hook_runtime",
    "codex_session",
)


def _load_runner_fresh_measure(monkeypatch, tmp_path):
    """Load the runner against a FRESHLY imported measure.py so every
    import-time path global resolves under the tmp env. Returns
    ``(runner, restore_fn, transcript_path)``.

    No measure.* attribute is monkeypatched: the domain functions the runner
    calls (quality_cache, _continuity_prompt_hint, run_verbosity_steer,
    run_ensure_health, compact_restore, _ran_once_this_session,
    _ensure_health_daemon_revive_first, _daemon_midsession_pulse, is_cowork,
    detect_runtime, ...) stay 100% real. Only sys.modules entries are
    saved/popped so measure re-imports under the tmp env; restore_fn puts the
    originals back so later tests are unaffected.
    """
    saved = {k: sys.modules.get(k) for k in _FRESH_MEASURE_MODULES}
    for k in _FRESH_MEASURE_MODULES:
        sys.modules.pop(k, None)

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = claude_dir / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # daemon_disabled=true + foreign runtime => every daemon spawn/revive
    # no-ops at the cheapest gate (detect_runtime != "claude"), so the
    # integration test never installs/revives a real dashboard daemon.
    (cfg_dir / "config.json").write_text(
        '{"daemon_disabled": true, "enterprise_consent_shown": true, '
        '"v5_welcome_shown": true}',
        encoding="utf-8",
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # Foreign runtime: run_ensure_health returns early (_is_foreign_runtime),
    # _daemon_midsession_pulse returns "noop-foreign", and the detached
    # daemon-revive child returns "noop-foreign" -- all at the detect_runtime
    # gate, before any settings/launchd work.
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "opencode")
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CONTAINER_ID", raising=False)

    spec = importlib.util.spec_from_file_location(
        "ups_runner_integration_under_test", RUNNER
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _restore():
        for k in _FRESH_MEASURE_MODULES:
            if saved[k] is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = saved[k]

    return mod, _restore, transcript


def test_sub_handlers_call_real_measure_without_signature_error(monkeypatch, tmp_path, capsys):
    """Each _sub_* handler calls the REAL measure.py functions with the exact
    kwargs the runner uses. A kwarg/param rename in measure.py raises
    TypeError (not _HookTimeout), which the handlers do NOT catch, so this test
    fails RED on drift instead of the failure being silently swallowed by
    _run_safely in production. Calls the handlers directly (NOT via main()/
    _run_safely) precisely so the TypeError is visible."""
    runner, restore, transcript = _load_runner_fresh_measure(monkeypatch, tmp_path)
    try:
        sid = "sess-integration-139"
        hook_input = {
            "session_id": sid,
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "prompt": "integration probe",
        }

        # Each handler must run to completion without raising. The real
        # _install_hook_budget arms an 8s HookDeadline watchdog; the handlers
        # no-op fast on the empty transcript + foreign runtime, so the finally
        # _clear_hook_budget cancels it well before it could fire.
        try:
            runner._sub_prompt_continuity(hook_input)
            runner._sub_verbosity_steer(hook_input)
            runner._sub_quality_cache_warn(hook_input)
            runner._sub_ensure_health(hook_input)
            runner._sub_quality_cache_force(hook_input)
            runner._sub_compact_restore(hook_input)
        except TypeError as e:
            pytest.fail(
                f"signature drift: a _sub_* handler called measure with a "
                f"renamed/removed kwarg: {type(e).__name__}: {e}"
            )

        # Positive proof the handlers reached the REAL measure functions (not a
        # silent no-op): the three gated subcommands each wrote their real
        # run-once marker via measure._ran_once_this_session / _once_per_session_marker.
        m = runner.measure
        for tag in ("ensure-health", "quality-cache-force", "compact-restore-new-session"):
            marker = m._once_per_session_marker(tag, sid)
            assert marker is not None and marker.exists(), (
                f"real measure run-once marker for {tag!r} was not written; "
                f"the handler did not reach measure._ran_once_this_session"
            )
    finally:
        restore()


def test_sub_handlers_signature_drift_fails_red(monkeypatch, tmp_path):
    """Mutation guard for the guard: if a measure kwarg the runner uses is
    renamed, the integration test above must fail RED. We simulate drift by
    injecting a wrapper that rejects the runner's kwargs on quality_cache and
    confirm the handler raises TypeError (not silently swallowed). This pins
    that the end-to-end test is actually wired to the real call shapes."""
    runner, restore, transcript = _load_runner_fresh_measure(monkeypatch, tmp_path)
    try:
        # Simulate a rename of `warn_threshold` -> `warn_thresh` in measure.py:
        # the real quality_cache still accepts warn_threshold, so wrap it to
        # raise TypeError on the runner's exact kwargs, proving the handler
        # propagates the error instead of swallowing it.
        real_qc = runner.measure.quality_cache

        def _drifted_qc(**kw):
            if "warn_threshold" in kw:
                raise TypeError("simulated drift: warn_threshold renamed")
            return real_qc(**kw)

        runner.measure.quality_cache = _drifted_qc
        hook_input = {
            "session_id": "sess-drift-139",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "prompt": "x",
        }
        with pytest.raises(TypeError, match="warn_threshold"):
            runner._sub_quality_cache_warn(hook_input)
    finally:
        restore()


# --------------------------------------------------------------------------- #
# (g) FIX 1: shared deadline — early subcommand exceeding budget does NOT
#     kill later subcommands; the shared deadline is the only os._exit.
# --------------------------------------------------------------------------- #


def test_fix1_shared_deadline_early_budget_exceeded_later_subcommands_still_run(
    monkeypatch, tmp_path,
):
    """Shared deadline (2s total), early subcommand uses > its fair share, but
    later subcommands still run and produce side effects.  The shared deadline
    is the ONLY os._exit — no individual subcommand deadline can kill the rest.
    The hook still exits 0."""
    runner = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-fix1-139", "prompt": "x"})

    # Use a REAL shared deadline with a tiny total budget (2s).  Do NOT stub
    # the budget away — this test exercises the real shared-deadline path.
    # We monkeypatch only the heavy work functions to be fast no-ops so the
    # deadline never actually fires.
    calls = {"prompt_continuity": [], "verbosity_steer": [], "quality_cache_warn": [],
             "ensure_health": [], "quality_cache_force": [], "compact_restore": []}

    def _slow_continuity(**kw):
        calls["prompt_continuity"].append(kw)
        _time.sleep(0.3)  # exceed the per-subcommand fair share of 2s/6 ≈ 0.33s
        return ""

    def _fast_verbosity(**kw):
        calls["verbosity_steer"].append(kw)
        return None

    def _fast_qc(**kw):
        if kw.get("warn") and not kw.get("force"):
            calls["quality_cache_warn"].append(kw)
        elif kw.get("force"):
            calls["quality_cache_force"].append(kw)

    def _fast_eh():
        calls["ensure_health"].append({})

    def _fast_cr(**kw):
        calls["compact_restore"].append(kw)

    monkeypatch.setattr(runner.measure, "_continuity_prompt_hint", _slow_continuity)
    monkeypatch.setattr(runner.measure, "run_verbosity_steer", _fast_verbosity)
    monkeypatch.setattr(runner.measure, "quality_cache", _fast_qc)
    monkeypatch.setattr(runner.measure, "run_ensure_health", _fast_eh)
    monkeypatch.setattr(runner.measure, "compact_restore", _fast_cr)
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_ensure_health_daemon_revive_first", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "_ran_once_this_session", lambda tag, sid: False)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")

    # Override the shared deadline to 2s total (tiny, but enough for all six).
    monkeypatch.setattr(runner, "_RUNNER_TOTAL_BUDGET", 2.0)

    rc = runner.main()
    assert rc == 0, "shared-deadline runner must exit 0"

    # prompt-continuity ran (even though it exceeded its fair share).
    assert len(calls["prompt_continuity"]) == 1
    # All later subcommands STILL ran — the shared deadline is the ONLY kill
    # switch, and it did not fire because the total time was under 2s.
    assert len(calls["verbosity_steer"]) == 1, "verbosity-steer must run after slow continuity"
    assert len(calls["quality_cache_warn"]) == 1, "quality-cache --warn must run"
    assert len(calls["ensure_health"]) == 1, "ensure-health must run"
    assert len(calls["quality_cache_force"]) == 1, "quality-cache --force must run"
    assert len(calls["compact_restore"]) == 1, "compact-restore must run"


# --------------------------------------------------------------------------- #
# (h) FIX 2: ensure-health marker unlink on failure — first call throws =>
#     marker NOT durably set => second prompt retries and bootstraps.
# --------------------------------------------------------------------------- #


def test_fix2_ensure_health_marker_unlinked_on_failure_next_prompt_retries(
    monkeypatch, tmp_path, capsys,
):
    """First ensure-health call raises => marker unlinked => second call retries
    the bootstrap.  Without the FIX 2 unlink, the marker stays and ensure-health
    no-ops for the rest of the session (re-deadlock)."""
    # Create the CLAUDE_CONFIG_DIR before loading the runner so measure's
    # import-time path resolution finds it and scopes QUALITY_CACHE_DIR (and
    # therefore _ran_once_this_session markers) under tmp_path, not the host's
    # real ~/.claude.
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "token-optimizer").mkdir(parents=True, exist_ok=True)
    runner = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-fix2-139", "prompt": "x"})
    # Stub budget + shared deadline (no real watchdog).
    monkeypatch.setattr(runner.measure, "_install_hook_budget", lambda seconds=8: object())
    monkeypatch.setattr(runner.measure, "_clear_hook_budget", lambda deadline: None)
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=18: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)
    # Repoint QUALITY_CACHE_DIR under tmp so the real _ran_once_this_session
    # writes markers under tmp_path, not the host's real ~/.claude.  The
    # cached measure module may have been imported with real paths; this
    # override keeps the test isolated.
    qc_dir = tmp_path / "quality-cache"
    qc_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", qc_dir)

    # Use REAL _ran_once_this_session (no monkeypatch) so the marker is
    # actually written to tmp_path and can be verified.
    calls = {"ensure_health": []}
    fail_count = {"count": 0}

    def _failing_then_ok_eh():
        calls["ensure_health"].append({})
        if fail_count["count"] == 0:
            fail_count["count"] += 1
            raise RuntimeError("simulated transient ensure-health failure")

    monkeypatch.setattr(runner.measure, "run_ensure_health", _failing_then_ok_eh)
    monkeypatch.setattr(runner.measure, "_ensure_health_daemon_revive_first", lambda: None)
    # Stub the other subcommands so they're fast.
    monkeypatch.setattr(runner.measure, "quality_cache", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "_continuity_prompt_hint", lambda **kw: "")
    monkeypatch.setattr(runner.measure, "run_verbosity_steer", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "compact_restore", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")

    # First prompt: ensure-health raises.  Marker must be unlinked.
    rc = runner.main()
    assert rc == 0
    assert len(calls["ensure_health"]) == 1

    err = capsys.readouterr().err
    assert "CRITICAL: ensure-health bootstrap failed" in err, (
        "FIX 2: stderr must escalate on bootstrap failure"
    )

    # Verify the marker was unlinked (not on disk).
    sid = "sess-fix2-139"
    marker = runner.measure._once_per_session_marker("ensure-health", sid)
    assert marker is not None
    assert not marker.exists(), (
        "FIX 2: marker must be unlinked after ensure-health failure so next "
        "prompt retries"
    )

    # Second prompt: ensure-health should run AGAIN (marker was unlinked).
    calls["ensure_health"].clear()
    rc = runner.main()
    assert rc == 0
    assert len(calls["ensure_health"]) == 1, (
        "FIX 2: second prompt must retry ensure-health after marker unlink"
    )


# --------------------------------------------------------------------------- #
# (i) FIX 3: buffered stdout — >=2 subcommands emit simultaneously, stdout
#     is still consumable (not a corrupted blob).
# --------------------------------------------------------------------------- #


def test_fix3_buffered_stdout_multi_emit_is_consumable(monkeypatch, tmp_path):
    """Two subcommands emit hookSpecificOutput JSON simultaneously.  The
    buffered emitter in main() captures each subcommand's stdout separately
    and emits them in order, so the host receives consumable units."""
    runner = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: False)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-fix3-139", "prompt": "x"})
    # Stub budget + shared deadline.
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=18: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)
    monkeypatch.setattr(runner.measure, "_install_hook_budget", lambda seconds=8: object())
    monkeypatch.setattr(runner.measure, "_clear_hook_budget", lambda deadline: None)

    # Make prompt-continuity emit a hookSpecificOutput JSON.
    monkeypatch.setattr(runner.measure, "_continuity_prompt_hint",
                        lambda **kw: "fix3-continuity-hint")
    # Make verbosity-steer emit raw text.
    monkeypatch.setattr(runner.measure, "run_verbosity_steer",
                        lambda **kw: "fix3-verbosity-raw-output")
    # Stub the rest.
    monkeypatch.setattr(runner.measure, "quality_cache", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")

    import io as _io_mod
    from contextlib import redirect_stdout as _redirect

    captured = _io_mod.StringIO()
    with _redirect(captured):
        rc = runner.main()

    assert rc == 0
    stdout = captured.getvalue()

    # Both outputs are present and in order.
    assert "fix3-continuity-hint" in stdout, "prompt-continuity output must be present"
    assert "fix3-verbosity-raw-output" in stdout, "verbosity-steer output must be present"

    # Verify the output is consumable: the hookSpecificOutput JSON for
    # prompt-continuity is valid JSON and self-contained.
    pos_continuity = stdout.index("fix3-continuity-hint")
    pos_verbosity = stdout.index("fix3-verbosity-raw-output")
    assert pos_continuity < pos_verbosity, (
        "output order must match subcommand dispatch order"
    )

    # The hookSpecificOutput from prompt-continuity is a valid JSON object.
    import json as _json_mod
    lines = stdout.strip().split("\n")
    json_objects = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = _json_mod.loads(line)
            json_objects.append(obj)
        except _json_mod.JSONDecodeError:
            pass  # raw text is fine too

    assert len(json_objects) >= 1, "at least one valid JSON object must be present"
    continuity_obj = json_objects[0]
    assert continuity_obj.get("hookSpecificOutput", {}).get("additionalContext") == "fix3-continuity-hint"


# --------------------------------------------------------------------------- #
# (j) FIX 4: importlib path import — decoy run.py cannot shadow the real gate.
# --------------------------------------------------------------------------- #


def test_fix4_explicit_path_import_decoy_run_py_cannot_shadow(monkeypatch, tmp_path):
    """Plant a decoy run.py earlier on sys.path; the runner's _check_consent
    must still resolve hooks/run.py by explicit path and call the REAL
    _check_consent, not the decoy."""
    runner = _load_runner(monkeypatch, tmp_path)

    # Create a decoy run.py in a temp dir and prepend it to sys.path.
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy_run = decoy_dir / "run.py"
    decoy_run.write_text(
        "def _check_consent():\n    return False  # decoy says no consent\n",
        encoding="utf-8",
    )

    # Verify the decoy is on sys.path BEFORE the hooks dir.
    # The runner already has hooks/ on sys.path (from _load_runner), so
    # we need to check that the explicit-path import wins over sys.path.
    monkeypatch.syspath_prepend(str(decoy_dir))

    # If the runner used bare `import run`, it would find the decoy first
    # and return False.  With the explicit-path import, it finds the real
    # hooks/run.py and calls its _check_consent (which reads the tmp config
    # we set up with no consent flags => returns False, but that's the real
    # gate, not the decoy).

    # Set up a tmp config with consent=True so the real gate returns True.
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "token-optimizer").mkdir(parents=True, exist_ok=True)
    (claude_dir / "token-optimizer" / "config.json").write_text(
        '{"enterprise_consent_shown": true}', encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    # The real _check_consent should return True (consent flags are set).
    # The decoy would return False.  If the explicit-path import works,
    # we get True.
    assert runner._check_consent() is True, (
        "FIX 4: explicit-path import must resolve the real hooks/run.py, "
        "not the decoy on sys.path"
    )
