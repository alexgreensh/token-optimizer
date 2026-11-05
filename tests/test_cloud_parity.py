"""Claude Code cloud parity (2026-10-19 live probes).

A Claude Code cloud session is an ephemeral Ubuntu VM: root, HOME=/root, the
repo at /home/user/<repo>, CLAUDE_CODE_REMOTE=true and
CLAUDE_CODE_REMOTE_SESSION_ID=cse_..., no status line, no browser, thrown
away when the session is reclaimed. Real TO hooks ran there, and these bugs
showed up. Each test pins one fix:

  1. detection: cloud was labelled Cowork (CLAUDE_CODE_REMOTE read as Cowork).
  2. lean profile: daemon / keep-warm / status line / settings writes /
     welcome are skipped in cloud; the session-end flush runs inline.
  3. bytecode: no __pycache__ lands in a repo (the VM's own Stop hook then
     nags the model to commit it).
  4. the cwd -> project mismatch warning goes to stderr, never model context.
  5. read cache: a compaction boundary in the transcript clears earlier reads
     before a re-Read can be soft-blocked as "already in context".
  6. repo-scoped config: $CLAUDE_PROJECT_DIR/.claude/token-optimizer.json.
  7. trends rows carry an origin (local / cloud / cowork); legacy DBs migrate.

Run: python3 -m pytest tests/test_cloud_parity.py -q
"""
from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import runtime_env  # noqa: E402

_HOST_ENV = (
    "CLAUDE_CODE_REMOTE",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "CLAUDE_CODE_CONTAINER_ID",
    "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE",
    "CLAUDE_CODE_ENTRYPOINT",
    "TOKEN_OPTIMIZER_REMOTE_HOST",
    "TOKEN_OPTIMIZER_REMOTE_LEAN",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "AI_AGENT",
    "CLAUDE_PROJECT_DIR",
    "PYTHONPYCACHEPREFIX",
    "TOKEN_OPTIMIZER_CLOUD_KIT_VERSION",
    "TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER",
)


def _clear_caches():
    for fn in (runtime_env.remote_host, runtime_env.is_cowork,
               runtime_env.is_claude_code_cloud, runtime_env.project_config,
               runtime_env._project_config_file,
               runtime_env.detect_runtime):
        clear = getattr(fn, "cache_clear", None)
        if callable(clear):
            clear()


@pytest.fixture
def host(monkeypatch):
    """Clean host env; returns a setter that also clears the detection caches."""
    for var in _HOST_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    _clear_caches()

    def _set(**env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        _clear_caches()

    yield _set
    _clear_caches()


def _cloud(host):
    host(CLAUDE_CODE_REMOTE="true", CLAUDE_CODE_REMOTE_SESSION_ID="cse_01TESTCLOUD")


# --------------------------------------------------------------------------- #
# 1. Detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, ""),
        ({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"}, "cloud"),
        ({"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"}, "cloud"),
        # A bare CLAUDE_CODE_REMOTE says "hosted VM", not which one: no cloud
        # lean profile, no Cowork caveats (review #4).
        ({"CLAUDE_CODE_REMOTE": "true"}, "remote"),
        # CLAUDE_CODE_CONTAINER_ID (every Cowork VM) means Cowork when no
        # cloud evidence is present; the cloud VM always carries some (review 2 MED-3).
        ({"CLAUDE_CODE_CONTAINER_ID": "abc"}, "cowork"),
        ({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_CONTAINER_ID": "abc"}, "cowork"),
        ({"CLAUDE_CODE_CONTAINER_ID": "abc", "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default"}, "cloud"),
        ({"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X", "CLAUDE_CODE_CONTAINER_ID": "abc"}, "cloud"),
        # Synced plugin root: Cowork only without cloud evidence (the cloud VM
        # has plugins/synced/ too; account plugin sync will deliver there).
        ({"CLAUDE_PLUGIN_ROOT": "/root/.claude/plugins/synced/acct/token-optimizer"}, "cowork"),
        ({"CLAUDE_PLUGIN_ROOT": "/root/.claude/plugins/synced/acct/token-optimizer",
          "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"}, "cloud"),
        ({"CLAUDE_CODE_REMOTE": "false"}, ""),
        # The environment type is the most specific host marker.
        ({"CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default"}, "cloud"),
        ({"CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cowork_local",
          "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"}, "cowork"),
        ({"CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "something_new", "CLAUDE_CODE_REMOTE": "true"}, "remote"),
        ({"CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default",
          "CLAUDE_PLUGIN_ROOT": "/root/.claude/plugins/synced/acct/token-optimizer"}, "cloud"),
        # The cloud kit's runner marks its own VM.
        ({"TOKEN_OPTIMIZER_CLOUD_KIT_VERSION": "0.1.0"}, "cloud"),
        ({"TOKEN_OPTIMIZER_CLOUD_KIT_VERSION": "0.1.0", "CLAUDE_CODE_CONTAINER_ID": "abc"}, "cloud"),
        ({"AI_AGENT": "claude-code_2.1.258_harness"}, ""),
        # Explicit pin beats everything.
        ({"TOKEN_OPTIMIZER_REMOTE_HOST": "cowork", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"}, "cowork"),
        ({"TOKEN_OPTIMIZER_REMOTE_HOST": "cowork", "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default"}, "cowork"),
        ({"TOKEN_OPTIMIZER_REMOTE_HOST": "local", "CLAUDE_CODE_REMOTE": "true"}, ""),
        ({"TOKEN_OPTIMIZER_REMOTE_HOST": "bogus", "CLAUDE_CODE_REMOTE": "true"}, "remote"),
    ],
)
def test_remote_host_resolution(host, env, expected):
    host(**env)
    assert runtime_env.remote_host() == expected
    assert runtime_env.is_claude_code_cloud() is (expected == "cloud")
    assert runtime_env.is_cowork() is (expected == "cowork")
    assert runtime_env.run_origin() == (expected or "local")


# The exact host env of an Anthropic-hosted cloud VM started by a routine
# (live probes cse_01Au8fmq3iUyLMULzXTMuy5o and cse_01DWjCoVWnRSY5yNapeGTV7z,
# 2026-10-19), plus a plugin root under the VM's own plugins/synced/ dir, which
# is where account plugin sync will deliver Token Optimizer.
LIVE_CLOUD_ENV = {
    "CLAUDE_CODE_REMOTE": "true",
    "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01Au8fmq3iUyLMULzXTMuy5o",
    "CLAUDE_CODE_CONTAINER_ID": "container_01ABCDEF",
    "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default",
    "CLAUDE_CODE_ENTRYPOINT": "remote_trigger",
    "CLAUDE_PLUGIN_ROOT": "/root/.claude/plugins/synced/org-uuid_account-uuid/token-optimizer",
}


def test_live_cloud_vm_env_is_cloud_not_cowork(host, monkeypatch):
    host(**LIVE_CLOUD_ENV)
    assert runtime_env.remote_host() == "cloud"
    assert runtime_env.is_claude_code_cloud() is True
    assert runtime_env.is_cowork() is False
    assert runtime_env.run_origin() == "cloud"
    monkeypatch.setattr(measure, "is_claude_code_cloud", runtime_env.is_claude_code_cloud)
    assert measure._remote_lean_profile() is True
    # Each cloud marker alone (with the synced root and container id that the
    # cloud VM also carries) still says cloud.
    for only in ("CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE"):
        env = {k: (v if k in (only, "CLAUDE_PLUGIN_ROOT", "CLAUDE_CODE_CONTAINER_ID", "CLAUDE_CODE_REMOTE") else None)
               for k, v in LIVE_CLOUD_ENV.items()}
        host(**env)
        assert runtime_env.remote_host() == "cloud", only


def test_bare_remote_flag_gets_no_lean_profile(host, monkeypatch):
    """Review #4: CLAUDE_CODE_REMOTE=true alone (a self-hosted runner, a
    desktop with the variable exported, remote control) must not switch off
    the daemon, status line and settings self-heal."""
    monkeypatch.setattr(measure, "is_claude_code_cloud", runtime_env.is_claude_code_cloud)
    monkeypatch.delenv("TOKEN_OPTIMIZER_REMOTE_LEAN", raising=False)
    host(CLAUDE_CODE_REMOTE="true")
    assert measure._remote_lean_profile() is False
    host(CLAUDE_CODE_REMOTE="true", CLAUDE_CODE_CONTAINER_ID="abc")  # Cowork, not cloud
    assert measure._remote_lean_profile() is False
    for positive in ({"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"},
                     {"TOKEN_OPTIMIZER_CLOUD_KIT_VERSION": "5.13.50"},
                     {"TOKEN_OPTIMIZER_REMOTE_HOST": "cloud"},
                     {"CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default"}):
        host(CLAUDE_CODE_REMOTE="true", CLAUDE_CODE_CONTAINER_ID=None, **positive)
        assert measure._remote_lean_profile() is True, positive
        for key in positive:
            host(**{key: None})


def test_cloud_label_is_not_cowork(host, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    _cloud(host)
    assert runtime_env.runtime_name_for_humans() == "Claude Code (cloud)"
    host(CLAUDE_CODE_REMOTE=None, CLAUDE_CODE_REMOTE_SESSION_ID=None,
         CLAUDE_PLUGIN_ROOT="/home/u/.claude/plugins/synced/acct/token-optimizer-cowork")
    assert runtime_env.runtime_name_for_humans() == "Claude Code (Cowork)"
    host(CLAUDE_PLUGIN_ROOT=None, CLAUDE_CODE_REMOTE="true")
    assert runtime_env.runtime_name_for_humans() == "Claude Code"


def test_cloud_keeps_claude_only_features(host, monkeypatch):
    """Subagent-cache advice and usage recs are Claude Code features; Cowork
    excluded them, and cloud inherited that exclusion through is_cowork()."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    _cloud(host)
    monkeypatch.setattr(measure, "is_cowork", runtime_env.is_cowork)
    assert measure._subagent_cache_claude_only() is True
    host(CLAUDE_CODE_REMOTE=None, CLAUDE_CODE_REMOTE_SESSION_ID=None,
         CLAUDE_PLUGIN_ROOT="/home/u/.claude/plugins/synced/acct/token-optimizer-cowork")
    assert measure._subagent_cache_claude_only() is False


# --------------------------------------------------------------------------- #
# 2. Lean profile
# --------------------------------------------------------------------------- #

@pytest.fixture
def lean(monkeypatch):
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: True)
    monkeypatch.delenv("TOKEN_OPTIMIZER_REMOTE_LEAN", raising=False)


def test_lean_profile_follows_cloud_and_opt_out(monkeypatch):
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: False)
    assert measure._remote_lean_profile() is False
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: True)
    assert measure._remote_lean_profile() is True
    monkeypatch.setenv("TOKEN_OPTIMIZER_REMOTE_LEAN", "0")
    assert measure._remote_lean_profile() is False


def test_lean_skips_daemon_and_keepwarm(lean, monkeypatch):
    monkeypatch.setattr(measure, "_daemon_snapshot_sandboxed", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("spawned a detached process in a cloud VM")

    monkeypatch.setattr(measure, "spawn_detached", _boom)
    assert measure._ensure_dashboard_daemon(force=True) == "noop-remote"
    assert measure._daemon_midsession_pulse() == "noop-remote"
    assert measure._ensure_health_daemon_revive_first() == "noop-remote"
    assert measure.keepwarm_scheduler_repair() == "noop-remote"
    assert measure.write_keepwarm_arm_record("sid", "/nonexistent.jsonl") is None


def test_lean_quiet_quality_bar_never_touches_settings(lean, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("auto quality-bar install read/wrote settings.json in a cloud VM")

    monkeypatch.setattr(measure, "_read_settings_for_write", _boom)
    assert measure.setup_quality_bar(quiet=True) is None


def test_lean_off_in_cloud_still_never_adds_quality_bar_hooks(monkeypatch, tmp_path):
    """Interaction gap from review 1: TOKEN_OPTIMIZER_REMOTE_LEAN=0 in a cloud
    VM (or on any machine whose hooks the cloud kit owns) let the quiet
    quality-bar self-heal add statusLine + the cache hook to the same
    ~/.claude/settings.json the kit wired, so the hook ran twice."""
    def _boom(*a, **k):
        raise AssertionError("auto quality-bar install touched settings.json under the kit")

    monkeypatch.setenv("TOKEN_OPTIMIZER_REMOTE_LEAN", "0")
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: True)
    monkeypatch.setattr(measure, "_read_settings_for_write", _boom)
    assert measure._remote_lean_profile() is False
    assert measure.setup_quality_bar(quiet=True) is None
    # Desktop where the kit's runner is in settings.json (=force install).
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: False)
    monkeypatch.setattr(measure, "_cloud_kit_owns_hooks", lambda: True)
    assert measure.setup_quality_bar(quiet=True) is None


def _record_ensure_health(monkeypatch):
    calls = []

    def rec(name, ret=None):
        def _f(*a, **k):
            calls.append(name)
            return ret
        return _f

    monkeypatch.setattr(measure, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_maybe_self_heal_settings", rec("self_heal_settings"))
    monkeypatch.setattr(measure, "_read_settings_for_write", lambda *a, **k: ({"model": "x"}, True))

    def _write(data, *a, **k):
        if isinstance(data, dict) and "cleanupPeriodDays" in data:
            calls.append("cleanup_period_write")
        return True

    monkeypatch.setattr(measure, "_write_settings_atomic", _write)
    monkeypatch.setattr(measure, "setup_all_hooks", rec("setup_all_hooks", {"added": 0}))
    monkeypatch.setattr(measure, "_reconcile_sessionend_fossils", rec("fossils", {}))
    monkeypatch.setattr(measure, "_should_skip_self_heal_hooks", rec("skip_heal", (False, True)))
    monkeypatch.setattr(measure, "_show_v5_welcome", rec("welcome"))
    monkeypatch.setattr(measure, "setup_quality_bar", rec("quality_bar"))
    monkeypatch.setattr(measure, "_ensure_dashboard_daemon", rec("daemon", "noop-remote"))
    monkeypatch.setattr(measure, "keepwarm_scheduler_repair", rec("keepwarm", "noop-remote"))
    monkeypatch.setattr(measure, "_spawn_detached_dashboard_selfheal", rec("dashboard_regen"))
    monkeypatch.setattr(measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(measure, "_read_config_flag", lambda key, default=False: default)
    monkeypatch.setattr(measure, "_write_config_flag", lambda *a, **k: None)
    return calls


def test_lean_ensure_health_skips_desktop_only_work(lean, monkeypatch, capsys):
    calls = _record_ensure_health(monkeypatch)
    measure.run_ensure_health()
    out = capsys.readouterr().out
    for skipped in ("cleanup_period_write", "setup_all_hooks", "welcome", "quality_bar"):
        assert skipped not in calls, (skipped, calls)
    assert "First-run tip" not in out  # marketplace auto-update nudge


def test_desktop_ensure_health_still_runs_that_work(monkeypatch, capsys):
    """Sensitivity check for the test above: off-cloud the same work runs."""
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: False)
    calls = _record_ensure_health(monkeypatch)
    measure.run_ensure_health()
    out = capsys.readouterr().out
    assert "cleanup_period_write" in calls
    assert "welcome" in calls
    assert "First-run tip" in out


def test_cloud_session_end_flush_runs_inline_within_deadline(lean, monkeypatch):
    seen = {}
    monkeypatch.setattr(measure, "_run_session_end_flush_worker",
                        lambda args, budget_seconds=20: seen.update(args=args, budget=budget_seconds))
    monkeypatch.setattr(measure, "_defer_session_end_flush",
                        lambda args: pytest.fail("deferred to a detached worker in a cloud VM"))
    deadline = measure.HookDeadline(10).start()
    try:
        mode = measure._dispatch_session_end_flush(
            ["session-end-flush", "--trigger", "stop", "--quiet", "--defer"])
    finally:
        deadline.cancel()
    assert mode == "inline-remote"
    assert seen["args"][2] == "stop"
    assert 0.5 <= seen["budget"] < 10


def test_desktop_session_end_flush_still_deferred(monkeypatch):
    monkeypatch.setattr(measure, "is_claude_code_cloud", lambda: False)
    seen = []
    monkeypatch.setattr(measure, "_defer_session_end_flush", lambda args: seen.append(args))
    assert measure._dispatch_session_end_flush(["session-end-flush", "--defer"]) == "deferred"
    assert seen


def test_inline_flush_budget_without_deadline_is_default():
    assert measure._inline_flush_budget_seconds(default=7.0) == 7.0


def test_inline_flush_respects_the_stop_runners_fair_share(lean, monkeypatch, tmp_path):
    """Review #9: the Stop runner splits ONE 13 s deadline across
    compact-capture, session-end-flush and keepwarm-arm. The inline cloud flush
    took deadline.remaining() - 0.5 instead of its share, could overrun, and the
    shared HookDeadline then os._exit(0)'d the runner with its buffered stdout.
    The flush now gets at most the share the runner hands it."""
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("to_stop_runner_cloud", HOOKS / "stop_runner.py")
    runner = _ilu.module_from_spec(spec)
    _split_exec_module(runner)
    seen = {}
    monkeypatch.setattr(runner.measure, "_remote_lean_profile", lambda: True)
    monkeypatch.setattr(runner.measure, "_run_session_end_flush_worker",
                        lambda args, budget_seconds=20: seen.setdefault("budget", budget_seconds))
    deadline = runner._install_runner_deadline(13.0)
    try:
        runner._runner_budget(8, subcommand_count_hint=3)
        runner._sub_session_end_flush_stop({})
    finally:
        runner._clear_runner_deadline()
    # Two subcommands left of ~13 s: share ~6.5 s, capped at 8, minus margin.
    assert 0.1 <= seen["budget"] <= 6.5 - 0.5 + 0.05, seen
    assert deadline is not None
    # Direct: an explicit cap below the deadline's remainder wins.
    d = measure.HookDeadline(10).start()
    try:
        assert measure._inline_flush_budget_seconds(cap=2.0) == pytest.approx(1.5, abs=0.01)
        assert measure._inline_flush_budget_seconds(cap=0.2) == pytest.approx(0.1)
        assert 9.0 <= measure._inline_flush_budget_seconds() <= 9.5
    finally:
        d.cancel()


# --------------------------------------------------------------------------- #
# 3. Bytecode never lands in a repo
# --------------------------------------------------------------------------- #

def _load_run_py():
    spec = importlib.util.spec_from_file_location("to_run_py_cloud", HOOKS / "run.py")
    mod = importlib.util.module_from_spec(spec)
    _split_exec_module(mod)
    return mod


def test_bytecode_prefix_rules(tmp_path, host, monkeypatch):
    run = _load_run_py()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    plain = tmp_path / "plain" / "plugin"
    plain.mkdir(parents=True)
    repo_plugin = tmp_path / "repo" / ".claude" / "to-cloud" / "token-optimizer"
    repo_plugin.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()

    if os.name != "nt":
        expected = str(tmp_path / "cache" / "token-optimizer" / "pycache")
    else:
        expected = None  # LOCALAPPDATA-based on Windows; only assert non-None
    assert run._bytecode_prefix_for(plain) is None  # desktop default unchanged
    got = run._bytecode_prefix_for(repo_plugin)
    assert got is not None and (expected is None or got == expected)
    _cloud(host)
    assert run._bytecode_prefix_for(plain) is not None
    # A user's own PYTHONPYCACHEPREFIX is left alone.
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "mine"))
    assert run._bytecode_prefix_for(repo_plugin) is None


def _fake_plugin(root: Path) -> Path:
    (root / "hooks").mkdir(parents=True)
    shutil.copy2(HOOKS / "run.py", root / "hooks" / "run.py")
    shutil.copy2(HOOKS / "module_runner.py", root / "hooks" / "module_runner.py")
    scripts = root / "skills" / "token-optimizer" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "cloud_probe_helper.py").write_text("VALUE = 41\n", encoding="utf-8")
    (scripts / "cloud_probe.py").write_text(
        "import cloud_probe_helper\nprint(cloud_probe_helper.VALUE + 1)\n", encoding="utf-8")
    return root


def _run_fake_hook(plugin: Path, tmp_path: Path, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in _HOST_ENV and k != "PYTHONDONTWRITEBYTECODE"}
    env.update({
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "LOCALAPPDATA": str(tmp_path / "cache"),
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    })
    env.update(extra_env or {})
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(plugin / "hooks" / "run.py"),
         "skills/token-optimizer/scripts/cloud_probe.py"],
        input="{}", capture_output=True, text=True, env=env, timeout=60,
        cwd=str(tmp_path),
    )


def _pycache_dirs(root: Path):
    return [p for p in root.rglob("__pycache__") if p.is_dir()]


def test_hook_in_git_repo_writes_no_pycache_into_it(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    plugin = _fake_plugin(repo / ".claude" / "to-cloud" / "token-optimizer")
    res = _run_fake_hook(plugin, tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "42", (res.stdout, res.stderr)
    assert _pycache_dirs(repo) == [], "bytecode was written into the user's repo"
    cached = list((tmp_path / "cache" / "token-optimizer" / "pycache").rglob("*.pyc"))
    assert cached, "bytecode should go to the per-user cache prefix (keeps warm starts)"


def test_cloud_hook_writes_no_pycache_beside_scripts(tmp_path):
    plugin = _fake_plugin(tmp_path / "vm" / "token-optimizer")
    res = _run_fake_hook(plugin, tmp_path, {
        "CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"})
    assert res.returncode == 0, res.stderr
    assert _pycache_dirs(plugin) == []


def test_desktop_hook_outside_git_keeps_bytecode_beside_scripts(tmp_path):
    """The default desktop install is untouched (no new cache location)."""
    plugin = _fake_plugin(tmp_path / "plain" / "token-optimizer")
    res = _run_fake_hook(plugin, tmp_path)
    assert res.returncode == 0, res.stderr
    assert _pycache_dirs(plugin), "desktop bytecode location changed unexpectedly"


def test_host_dontwritebytecode_in_git_repo_still_keeps_repo_clean(tmp_path):
    """PR #259 composes with the repo redirect: a host-set
    PYTHONDONTWRITEBYTECODE=1 no longer disables the hook's bytecode cache, and
    inside a git work tree that cache still lands in the per-user prefix."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    plugin = _fake_plugin(repo / ".claude" / "to-cloud" / "token-optimizer")
    res = _run_fake_hook(plugin, tmp_path, {"PYTHONDONTWRITEBYTECODE": "1"})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "42", (res.stdout, res.stderr)
    assert _pycache_dirs(repo) == [], "bytecode was written into the user's repo"
    cached = [p.name for p in
              (tmp_path / "cache" / "token-optimizer" / "pycache").rglob("*.pyc")]
    assert any(n.startswith("cloud_probe.") for n in cached), cached


def test_host_dontwritebytecode_on_desktop_keeps_bytecode_beside_scripts(tmp_path):
    """The Windows desktop app case from PR #259, through the real run.py ->
    module_runner chain: the hook module's .pyc is still written and reused."""
    plugin = _fake_plugin(tmp_path / "plain" / "token-optimizer")
    res = _run_fake_hook(plugin, tmp_path, {"PYTHONDONTWRITEBYTECODE": "1"})
    assert res.returncode == 0, res.stderr
    scripts = plugin / "skills" / "token-optimizer" / "scripts"
    assert list((scripts / "__pycache__").glob("cloud_probe.*.pyc"))


def test_measure_cli_bytecode_redirect(tmp_path, host, monkeypatch):
    monkeypatch.setattr(sys, "pycache_prefix", None)
    # Record the (absent) original so the os.environ write below is undone;
    # monkeypatch.delenv on an absent key records nothing.
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "placeholder")
    monkeypatch.delenv("PYTHONPYCACHEPREFIX")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    _cloud(host)
    measure._redirect_bytecode_out_of_repo()
    assert sys.pycache_prefix
    assert sys.pycache_prefix.startswith(str(tmp_path / "cache"))
    assert os.environ["PYTHONPYCACHEPREFIX"] == sys.pycache_prefix


# --------------------------------------------------------------------------- #
# 4. Stray stdout in hook paths
# --------------------------------------------------------------------------- #

def test_cwd_mismatch_warning_goes_to_stderr(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "-some-other-project").mkdir(parents=True)
    work = tmp_path / "unrelated" / "cwd"
    work.mkdir(parents=True)
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude_dir)
    monkeypatch.chdir(work)
    result = measure.find_projects_dir()
    captured = capsys.readouterr()
    assert result is not None and result.name == "-some-other-project"
    assert captured.out == "", "warning leaked to stdout (model context in hooks)"
    assert "Could not match cwd" in captured.err


# --------------------------------------------------------------------------- #
# 5. Read cache vs compaction
# --------------------------------------------------------------------------- #

from test_double_fire_read_cache import (  # noqa: E402
    _make_payload, _make_real_python_file, _parse_stdout, _run_read_cache,
)


def _decision(proc):
    parsed = _parse_stdout(proc.stdout)
    if parsed is None:
        return None
    return parsed.get("hookSpecificOutput", {}).get("permissionDecision")


def _append(path: Path, row: dict):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_reread_after_compaction_is_not_soft_blocked(tmp_path):
    target = _make_real_python_file(tmp_path / "target.py")
    transcript = tmp_path / "session.jsonl"
    _append(transcript, {"type": "user", "message": {"content": "hi"}})

    def read(tool_use_id):
        payload = _make_payload(target, tool_use_id=tool_use_id)
        payload["transcript_path"] = str(transcript)
        res = _run_read_cache(tmp_path / "snap", payload)
        assert res.returncode == 0, res.stderr
        return _decision(res)

    assert read("toolu_a") != "deny"
    _append(transcript, {"type": "assistant", "message": {"content": "read it"}})
    assert read("toolu_b") == "deny", "control: an unchanged reread is still deduped"

    # A tool result QUOTING the marker is not a compaction.
    _append(transcript, {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": '{"type":"system","subtype":"compact_boundary"}'}]}})
    assert read("toolu_c") == "deny"

    # A real compaction: the earlier read is gone from the model's context.
    _append(transcript, {"type": "system", "subtype": "compact_boundary",
                         "compactMetadata": {"trigger": "auto"}})
    _append(transcript, {"type": "user", "isCompactSummary": True,
                         "message": {"content": "summary"}})
    assert read("toolu_d") != "deny", "re-Read after compaction was soft-blocked"
    # And the cache rebuilds from there: the next reread dedupes again.
    assert read("toolu_e") == "deny"


def test_compaction_scan_cursor_is_incremental(tmp_path):
    import read_cache

    class _Store:
        def __init__(self):
            self.meta = {}

        def get_meta(self, key):
            return self.meta.get(key)

        def set_meta(self, key, value):
            self.meta[key] = value

    store = _Store()
    transcript = tmp_path / "t.jsonl"
    _append(transcript, {"type": "user", "message": {"content": "a"}})
    assert read_cache._scan_for_compaction(store, transcript) is False
    _append(transcript, {"type": "system", "subtype": "compact_boundary"})
    assert read_cache._scan_for_compaction(store, transcript) is True
    # Already scanned: the same boundary does not fire twice.
    assert read_cache._scan_for_compaction(store, transcript) is False
    # A partial line (being written) is left for the next scan.
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write('{"type": "system", "subtype": "compact_bou')
    assert read_cache._scan_for_compaction(store, transcript) is False
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write('ndary"}\n')
    assert read_cache._scan_for_compaction(store, transcript) is True
    # Missing transcript / no store: fail open.
    assert read_cache._scan_for_compaction(store, tmp_path / "missing.jsonl") is False
    assert read_cache._scan_for_compaction(None, transcript) is False


def test_compaction_cursor_survives_alternating_transcripts(tmp_path):
    """Review #7: subagent hooks carry a different transcript_path. With one
    cursor slot each alternation reset the offset, rescanned from 0, re-found
    the OLD boundary and cleared file_reads again: dedup was off for any
    session using subagents. Each path keeps its offset, and a boundary acted
    on once never counts again, even after a rescan from 0."""
    import read_cache

    class _Store:
        def __init__(self):
            self.meta = {}

        def get_meta(self, key):
            return self.meta.get(key)

        def set_meta(self, key, value):
            self.meta[key] = value

    store = _Store()
    main = tmp_path / "main.jsonl"
    sub = tmp_path / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir()
    _append(main, {"type": "user", "message": {"content": "a"}})
    _append(main, {"type": "system", "subtype": "compact_boundary", "uuid": "b-1"})
    _append(sub, {"type": "user", "message": {"content": "sub"}})
    assert read_cache._scan_for_compaction(store, main) is True
    for _ in range(3):
        _append(sub, {"type": "assistant", "message": {"content": "x"}})
        assert read_cache._scan_for_compaction(store, sub) is False
        _append(main, {"type": "assistant", "message": {"content": "y"}})
        assert read_cache._scan_for_compaction(store, main) is False, "old boundary re-fired"
    # A rewrite that forces a rescan from 0 still remembers boundary b-1.
    text = main.read_text(encoding="utf-8")
    main.write_text(text.splitlines(True)[0] + text.splitlines(True)[1], encoding="utf-8")
    assert read_cache._scan_for_compaction(store, main) is False
    # A genuinely new boundary still fires.
    _append(main, {"type": "system", "subtype": "compact_boundary", "uuid": "b-2"})
    assert read_cache._scan_for_compaction(store, main) is True
    # Legacy single-slot cursor is read, not discarded.
    legacy = _Store()
    legacy.meta[read_cache._COMPACT_SCAN_META_KEY] = json.dumps({"path": str(main), "offset": main.stat().st_size})
    assert read_cache._scan_for_compaction(legacy, main) is False


def test_compaction_cursor_map_is_bounded(tmp_path):
    import read_cache

    class _Store(dict):
        get_meta = dict.get

        def set_meta(self, key, value):
            self[key] = value

    store = _Store()
    for i in range(read_cache._COMPACT_SCAN_MAX_PATHS + 10):
        t = tmp_path / ("t%d.jsonl" % i)
        _append(t, {"type": "user", "message": {"content": i}})
        read_cache._scan_for_compaction(store, t)
    cur = json.loads(store[read_cache._COMPACT_SCAN_META_KEY])
    assert len(cur["paths"]) == read_cache._COMPACT_SCAN_MAX_PATHS
    assert str(tmp_path / "t0.jsonl") not in cur["paths"]


# --------------------------------------------------------------------------- #
# 6. Repo-scoped config layer
# --------------------------------------------------------------------------- #

def _write_project_config(tmp_path, data) -> Path:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    path = project / ".claude" / "token-optimizer.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return project


def test_project_config_allowlist(tmp_path, host):
    project = _write_project_config(tmp_path, {
        "v5_bash_compress": False,
        "daemon_disabled": True,
        "pricing_tier": "vertex_global",
        "v5_welcome_shown": True,          # onboarding: personal, not a repo's
        "enterprise_consent_shown": True,  # consent: personal, not a repo's
        "daemon_token": "secret",          # never an allowed key
        "v5_delta_mode": {"nested": 1},    # wrong type
        "pricing_tier_long": "x" * 100,
    })
    host(CLAUDE_PROJECT_DIR=str(project), CLAUDE_CODE_REMOTE="true",
         CLAUDE_CODE_REMOTE_SESSION_ID="cse_01TESTCLOUD")
    assert runtime_env.project_config() == {
        "v5_bash_compress": False, "daemon_disabled": True, "pricing_tier": "vertex_global"}


@pytest.mark.parametrize("content", [
    "not json",
    "[1, 2]",
    '{"daemon_disabled": true, "pad": "' + "x" * (70 * 1024) + '"}',
], ids=["malformed", "not-object", "oversize"])
def test_project_config_rejects_bad_files(tmp_path, host, content):
    project = _write_project_config(tmp_path, content)
    host(CLAUDE_PROJECT_DIR=str(project), CLAUDE_CODE_REMOTE="true",
         CLAUDE_CODE_REMOTE_SESSION_ID="cse_01TESTCLOUD")
    assert runtime_env.project_config() == {}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_project_config_rejects_symlink(tmp_path, host):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"daemon_disabled": True}), encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "token-optimizer.json").symlink_to(real)
    host(CLAUDE_PROJECT_DIR=str(project), CLAUDE_CODE_REMOTE="true",
         CLAUDE_CODE_REMOTE_SESSION_ID="cse_01TESTCLOUD")
    assert runtime_env.project_config() == {}


def test_project_config_unset_project_dir(host):
    assert runtime_env.project_config() == {}


def test_project_layer_sits_under_user_config(tmp_path, host, monkeypatch):
    import plugin_env

    project = _write_project_config(tmp_path, {"v5_bash_compress": False, "daemon_disabled": True})
    host(CLAUDE_PROJECT_DIR=str(project), CLAUDE_CODE_REMOTE="true",
         CLAUDE_CODE_REMOTE_SESSION_ID="cse_01TESTCLOUD")
    user_cfg_dir = tmp_path / "user"
    user_cfg_dir.mkdir()
    monkeypatch.setattr(plugin_env, "_USER_CONFIG_DIR", user_cfg_dir)
    monkeypatch.setattr(plugin_env, "resolve_plugin_data_dir", lambda: None)
    monkeypatch.delenv("TOKEN_OPTIMIZER_BASH_COMPRESS", raising=False)

    # No user value: the repo's value applies.
    assert plugin_env.is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=True) is False
    # The user's own value wins.
    (user_cfg_dir / "config.json").write_text(json.dumps({"v5_bash_compress": True}), encoding="utf-8")
    assert plugin_env.is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=True) is True
    # And the env beats both.
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASH_COMPRESS", "0")
    assert plugin_env.is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=True) is False

    # measure's reader: same order.
    cfg = tmp_path / "measure-config.json"
    monkeypatch.setattr(measure, "CONFIG_PATH", cfg)
    assert measure._read_config_flag("daemon_disabled", False) is True
    cfg.write_text(json.dumps({"daemon_disabled": False}), encoding="utf-8")
    assert measure._read_config_flag("daemon_disabled", True) is False
    # Non-allowlisted keys never come from the repo.
    assert measure._read_config_flag("last_hook_heal_check", 0) == 0


def test_project_layer_is_ignored_on_desktop(tmp_path, host, monkeypatch):
    """Review #5: a cloned repo's .claude/token-optimizer.json steered
    machine-wide desktop state (daemon_disabled stopped the global daemon
    revive, pricing_tier changed the cost_saved_usd persisted in the shared
    trends.db, v5 flags). Outside a cloud VM the file is now ignored."""
    import plugin_env

    project = _write_project_config(tmp_path, {
        "daemon_disabled": True, "pricing_tier": "vertex_global",
        "v5_bash_compress": False, "read_cache_enabled": False})
    for desktop_env in ({}, {"CLAUDE_CODE_REMOTE": "true"},
                        {"CLAUDE_PLUGIN_ROOT": "/u/.claude/plugins/synced/a/token-optimizer"}):
        host(**{"CLAUDE_PROJECT_DIR": str(project), "CLAUDE_CODE_REMOTE": None, "CLAUDE_PLUGIN_ROOT": None,
                 "CLAUDE_CODE_REMOTE_SESSION_ID": None, **desktop_env})
        assert runtime_env.project_config() == {}, desktop_env
        assert runtime_env.project_config_value("daemon_disabled") is None
    cfg = tmp_path / "measure-config.json"
    monkeypatch.setattr(measure, "CONFIG_PATH", cfg)
    assert measure._read_config_flag("daemon_disabled", False) is False
    user_cfg_dir = tmp_path / "user"
    user_cfg_dir.mkdir()
    monkeypatch.setattr(plugin_env, "_USER_CONFIG_DIR", user_cfg_dir)
    monkeypatch.setattr(plugin_env, "resolve_plugin_data_dir", lambda: None)
    monkeypatch.delenv("TOKEN_OPTIMIZER_BASH_COMPRESS", raising=False)
    assert plugin_env.is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=True) is True
    # The same file applies once the session is a cloud VM.
    host(CLAUDE_CODE_REMOTE="true", CLAUDE_CODE_REMOTE_SESSION_ID="cse_01X")
    assert runtime_env.project_config_value("daemon_disabled") is True


def test_project_layer_read_cache_opt_out(tmp_path, host, monkeypatch):
    import read_cache

    project = _write_project_config(tmp_path, {"read_cache_enabled": False})
    host(CLAUDE_PROJECT_DIR=str(project), CLAUDE_CODE_REMOTE="true",
         CLAUDE_CODE_REMOTE_SESSION_ID="cse_01TESTCLOUD")
    monkeypatch.delenv("TOKEN_OPTIMIZER_READ_CACHE", raising=False)
    monkeypatch.setattr(read_cache, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(read_cache, "CACHE_DIR", tmp_path / "cache")
    assert read_cache._is_read_cache_disabled() is True
    (tmp_path / "snap").mkdir()
    (tmp_path / "snap" / "config.json").write_text(json.dumps({"read_cache_enabled": True}), encoding="utf-8")
    assert read_cache._is_read_cache_disabled() is False


def test_to_never_writes_the_project_file():
    """TO reads the repo file; nothing in the product writes it. The path is
    built in exactly one place (runtime_env.PROJECT_CONFIG_RELPATH) and that
    module only reads it."""
    assert runtime_env.PROJECT_CONFIG_RELPATH == (".claude", "token-optimizer.json")
    for path in list(SCRIPTS.rglob("*.py")) + list(HOOKS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name != "runtime_env.py":
            assert "PROJECT_CONFIG_RELPATH" not in text and "_project_config_path" not in text, path.name
    src = (SCRIPTS / "runtime_env.py").read_text(encoding="utf-8")
    body = src[src.index("def _project_config_path"):src.index("def project_config_value")]
    for writer in ("write_text", "write_bytes", '"w"', "'w'", "os.replace", "mkdir"):
        assert writer not in body, writer


# --------------------------------------------------------------------------- #
# 7. origin column + migration
# --------------------------------------------------------------------------- #

_LEGACY_SCHEMA = """
CREATE TABLE session_log (id INTEGER PRIMARY KEY AUTOINCREMENT, jsonl_path TEXT UNIQUE,
    date TEXT NOT NULL, project TEXT, input_tokens INTEGER, output_tokens INTEGER,
    message_count INTEGER, collected_at TEXT);
CREATE TABLE savings_events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL, tokens_saved INTEGER DEFAULT 0, cost_saved_usd REAL DEFAULT 0.0,
    session_id TEXT, detail TEXT);
CREATE TABLE compression_events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    session_id TEXT, session_uuid TEXT, feature TEXT NOT NULL, command_pattern TEXT,
    original_tokens INTEGER DEFAULT 0, compressed_tokens INTEGER DEFAULT 0,
    compression_ratio REAL DEFAULT 0.0, quality_preserved INTEGER DEFAULT 1,
    verified INTEGER DEFAULT 0, detail TEXT, model TEXT, tier TEXT);
INSERT INTO session_log (jsonl_path, date, project, input_tokens, output_tokens, message_count, collected_at)
    VALUES ('/x/old.jsonl', '2026-01-01', 'p', 1, 2, 3, '2026-01-01T00:00:00');
INSERT INTO savings_events (timestamp, event_type, tokens_saved, session_id, detail)
    VALUES ('2026-01-01T00:00:00', 'legacy', 5, 's', 'd');
"""


def _cols(db, table):
    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_legacy_trends_db_migrates_origin(tmp_path, monkeypatch):
    db = tmp_path / "trends.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    measure._init_trends_db().close()
    for table in ("session_log", "savings_events", "compression_events"):
        assert "origin" in _cols(db, table), table
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT origin, tokens_saved FROM savings_events").fetchall() == [(None, 5)]
        assert conn.execute("SELECT origin FROM session_log").fetchall() == [(None,)]
    finally:
        conn.close()
    # Idempotent: a second init on the migrated DB is clean.
    measure._init_trends_db().close()


def test_hot_path_writers_record_origin(tmp_path, host, monkeypatch):
    import compression_log
    import read_cache

    db = tmp_path / "trends.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    conn.close()
    _cloud(host)
    monkeypatch.setattr(read_cache, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(read_cache, "TRENDS_DB", db)
    monkeypatch.setattr(read_cache, "_savings_columns_verified", False)
    monkeypatch.setattr(compression_log, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(compression_log, "TRENDS_DB", db)

    read_cache._log_savings_event("read_dedup", 100, "sess-1", "x.py")
    compression_log.log_compression_event(
        feature="bash", original_tokens=100, compressed_tokens=10, session_id="sess-1")

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute(
            "SELECT origin FROM savings_events WHERE event_type='read_dedup'").fetchall() == [("cloud",)]
        assert conn.execute("SELECT origin FROM compression_events").fetchall() == [("cloud",)]
    finally:
        conn.close()


def _transcript(path: Path, turns: int):
    rows = []
    for i in range(turns):
        rows.append({"type": "user", "timestamp": f"2026-01-01T00:0{i}:00Z",
                     "message": {"content": f"q{i}"}})
        rows.append({"type": "assistant", "timestamp": f"2026-01-01T00:0{i}:05Z",
                     "requestId": f"req-{i}",
                     "message": {"model": "claude-sonnet-4-20250514",
                                 "content": [{"type": "text", "text": "a"}],
                                 "usage": {"input_tokens": 10, "output_tokens": 5,
                                           "cache_read_input_tokens": 0,
                                           "cache_creation_input_tokens": 0}}})
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_session_log_origin_survives_teleported_copy(tmp_path, monkeypatch):
    """A session collected in the cloud VM, then seen again as a fuller local
    copy (a teleported transcript), stays ONE row keyed by session id and keeps
    origin='cloud'."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    for var in _HOST_ENV:
        monkeypatch.delenv(var, raising=False)
    fake_claude = tmp_path / "claude_home"
    sid = "3f1c9a2e-7b4d-4e8a-9c1f-0a2b3c4d5e6f"
    cloud_copy = fake_claude / "projects" / "-home-user-repo" / f"{sid}.jsonl"
    cloud_copy.parent.mkdir(parents=True)
    _transcript(cloud_copy, 1)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_claude))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01TELEPORT")
    for mod_name in ("measure", "runtime_env", "plugin_env"):
        monkeypatch.delitem(sys.modules, mod_name, raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    m = importlib.import_module("measure")
    m.collect_sessions(days=3650, quiet=True)

    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        assert conn.execute("SELECT session_uuid, origin FROM session_log").fetchall() == [(sid, "cloud")]
    finally:
        conn.close()

    # Back on the laptop: the teleported, longer copy under the local project.
    monkeypatch.delenv("CLAUDE_CODE_REMOTE")
    monkeypatch.delenv("CLAUDE_CODE_REMOTE_SESSION_ID")
    local_copy = fake_claude / "projects" / "-Users-me-repo" / f"{sid}.jsonl"
    local_copy.parent.mkdir(parents=True)
    _transcript(local_copy, 3)
    m.collect_sessions(days=3650, quiet=True)

    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        rows = conn.execute("SELECT session_uuid, origin, jsonl_path FROM session_log").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, rows
    assert rows[0][0] == sid and rows[0][1] == "cloud"
    assert rows[0][2] == str(local_copy)


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35, 0), reason="needs ALTER TABLE DROP COLUMN")
def test_collect_still_stores_sessions_when_origin_migration_was_lost(tmp_path, monkeypatch):
    """Review #10: collect_sessions always named the origin column. If the
    session_log ALTER lost its busy wait (another writer held the DB), every
    insert failed with "no such column" and the collect stored nothing. It now
    adds origin only when the column exists, like the event writers."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    for var in _HOST_ENV:
        monkeypatch.delenv(var, raising=False)
    fake_claude = tmp_path / "claude_home"
    sid = "8a1c9a2e-7b4d-4e8a-9c1f-0a2b3c4d5e70"
    path = fake_claude / "projects" / "-Users-me-repo" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    _transcript(path, 2)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_claude))
    for mod_name in ("measure", "runtime_env", "plugin_env"):
        monkeypatch.delitem(sys.modules, mod_name, raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    m = importlib.import_module("measure")

    real_init = m._init_trends_db

    def _init_without_origin():
        conn = real_init()
        if "origin" in {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}:
            conn.execute("ALTER TABLE session_log DROP COLUMN origin")
            conn.commit()
        return conn

    monkeypatch.setattr(m, "_init_trends_db", _init_without_origin)
    m.collect_sessions(days=3650, quiet=True)
    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        assert "origin" not in _cols(m.TRENDS_DB, "session_log")
        assert conn.execute("SELECT session_uuid FROM session_log").fetchall() == [(sid,)]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 8. Per-account schedulers are never touched from a redirected HOME
# --------------------------------------------------------------------------- #

def test_home_redirect_detection(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / ".claude").mkdir(parents=True)
    other = tmp_path / "scratch-home"
    (other / ".claude").mkdir(parents=True)
    monkeypatch.delenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", raising=False)
    monkeypatch.setattr(measure, "_real_account_home", lambda: real.resolve())

    monkeypatch.setattr(measure, "HOME", real)
    monkeypatch.setattr(measure, "CLAUDE_DIR", real / ".claude")
    assert measure._compute_home_redirected() is False

    monkeypatch.setattr(measure, "HOME", other)
    monkeypatch.setattr(measure, "CLAUDE_DIR", other / ".claude")
    assert measure._compute_home_redirected() is True

    # Real HOME with the Claude home outside it is a supported setup
    # (CLAUDE_CONFIG_DIR anywhere), not a redirect (review #2).
    monkeypatch.setattr(measure, "HOME", real)
    monkeypatch.setattr(measure, "CLAUDE_DIR", other / ".claude")
    assert measure._compute_home_redirected() is False

    monkeypatch.setattr(measure, "HOME", other)
    monkeypatch.setenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", "1")
    assert measure._compute_home_redirected() is False

    # Unknown account home: no refusal from this check.
    monkeypatch.delenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER")
    monkeypatch.setattr(measure, "_real_account_home", lambda: None)
    assert measure._compute_home_redirected() is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_home_redirect_tolerates_symlinked_and_automounted_homes(tmp_path, monkeypatch):
    """HPC/NFS: the passwd entry and HOME often name one directory two ways
    (an automount symlink and the real mount). Same directory, no redirect."""
    real = tmp_path / "nfs" / "u"
    real.mkdir(parents=True)
    link = tmp_path / "home-u"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.delenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", raising=False)
    for pw_dir, home in ((link, real), (real, link), (link, link)):
        monkeypatch.setattr(measure, "_real_account_home", lambda pw=pw_dir: pw)
        monkeypatch.setattr(measure, "HOME", home)
        assert measure._compute_home_redirected() is False, (pw_dir, home)


def test_windows_home_guard_compares_profile_dir(tmp_path, monkeypatch):
    """Review #2: Windows has no pwd, so the schtasks hijack class was open.
    The account home there is the profile folder the OS records
    (SHGetKnownFolderPath), compared against the home TO derives its paths
    from (USERPROFILE via Path.home()). Runs on every OS via monkeypatch."""
    profile = tmp_path / "Users" / "alex"
    profile.mkdir(parents=True)
    monkeypatch.delenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", raising=False)
    monkeypatch.setattr(measure, "_ACCOUNT_HOME_IS_WINDOWS", True)
    monkeypatch.setattr(measure, "_windows_profile_dir", lambda: profile)
    assert measure._real_account_home() == profile
    monkeypatch.setattr(measure, "HOME", profile)
    assert measure._compute_home_redirected() is False
    monkeypatch.setattr(measure, "HOME", tmp_path / "e2e-scratch")
    assert measure._compute_home_redirected() is True
    # Case-insensitive on Windows is handled by normcase; an API failure
    # (None) never refuses.
    monkeypatch.setattr(measure, "_windows_profile_dir", lambda: None)
    assert measure._compute_home_redirected() is False


@pytest.mark.skipif(os.name != "nt", reason="real SHGetKnownFolderPath call")
def test_windows_profile_dir_ignores_userprofile_env(tmp_path, monkeypatch):
    before = measure._windows_profile_dir()
    assert before is not None and before.is_dir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert measure._windows_profile_dir() == before


def test_windows_profile_dir_is_none_off_windows():
    if os.name != "nt":
        assert measure._windows_profile_dir() is None


@pytest.fixture
def account_home(tmp_path, monkeypatch):
    """This process at the account's real home (HOME == account home), the
    Claude home relocated to /opt-style dir, no snapshot override."""
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "opt" / "claude-config"
    cfg.mkdir(parents=True)
    monkeypatch.delenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", raising=False)
    monkeypatch.setattr(measure, "_real_account_home", lambda: home)
    monkeypatch.setattr(measure, "HOME", home)
    monkeypatch.setattr(measure, "CLAUDE_DIR", cfg)
    monkeypatch.setattr(measure, "RUNTIME_DIR", cfg)
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", cfg / "plugins" / "data" / "to" / "data")
    monkeypatch.setattr(measure, "_RESOLVED_PLUGIN_DATA", None)
    monkeypatch.setattr(measure, "_SNAPSHOT_DIR_OVERRIDE", "")
    monkeypatch.setattr(measure, "_HOME_REDIRECTED", measure._compute_home_redirected())
    plist = home / "Library" / "LaunchAgents" / "com.token-optimizer.dashboard.plist"
    kw = home / "Library" / "LaunchAgents" / "com.token-optimizer.keepwarm.plist"
    monkeypatch.setattr(measure, "PLIST_PATH", plist)
    monkeypatch.setattr(measure, "_keepwarm_scheduler_plist_path", lambda: kw)
    monkeypatch.setattr(measure, "_systemd_user_unit_path", lambda: home / ".config" / "systemd" / "user" / "to.service")
    measure._reset_job_guard_cache()
    return {"home": home, "cfg": cfg, "plist": plist, "tmp": tmp_path}


def _write_plist(path, script, log):
    import plistlib
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump({"Label": "com.token-optimizer.dashboard",
                       "ProgramArguments": ["/usr/bin/python3", str(script)],
                       "StandardOutPath": str(log), "StandardErrorPath": str(log)}, fh)


def test_config_dir_outside_home_keeps_scheduler_and_uninstall(account_home, monkeypatch):
    """Review #2 repro: HOME=real, CLAUDE_DIR=/opt/claude-config. Before: every
    scheduler action, uninstall included, was refused ("noop-sandbox")."""
    assert measure._HOME_REDIRECTED is False
    assert measure._daemon_snapshot_sandboxed() is False
    assert measure._scheduler_install_refused() is False  # no job yet
    assert measure.setup_daemon(uninstall=True, dry_run=True, latch_optout=False) != "noop-sandbox"
    # A job that this install wrote (paths under the relocated Claude home).
    monkeypatch.setattr(measure, "_normalized_platform", lambda: "Darwin")
    data = account_home["cfg"] / "plugins" / "data" / "to" / "data"
    _write_plist(account_home["plist"], data / "dashboard-server.py", data / "logs" / "stdout.log")
    assert measure._scheduler_job_points_elsewhere() == ""
    assert measure._scheduler_install_refused() is False


def _foreign_job(account_home, monkeypatch, platform, exists=True):
    """Register a job for this account that belongs to another install."""
    monkeypatch.setattr(measure, "_normalized_platform", lambda: platform)
    foreign = account_home["tmp"] / "someone-else" / ".claude" / "token-optimizer"
    if exists:
        foreign.mkdir(parents=True)
        for name in ("dashboard-server.py", "dashboard-launcher.sh", "dashboard-launcher.cmd"):
            (foreign / name).write_text("# other install\n", encoding="utf-8")
    if platform == "Darwin":
        _write_plist(account_home["plist"], foreign / "dashboard-server.py", foreign / "out.log")
    elif platform == "Linux":
        unit = measure._systemd_user_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text('[Service]\nExecStart="%s"\nStandardOutput=append:%s/stdout.log\n'
                        % (foreign / "dashboard-launcher.sh", foreign), encoding="utf-8")
    else:
        monkeypatch.setattr(measure, "_windows_task_job_paths",
                            lambda: [str(foreign / "dashboard-launcher.cmd")])
    return foreign


@pytest.mark.parametrize("platform", ["Darwin", "Linux", "Windows"])
def test_job_of_another_install_blocks_writes_not_uninstall(account_home, monkeypatch, platform):
    """Real HOME, but the account's job was installed by a different install
    that still exists (a scratch CLAUDE_CONFIG_DIR e2e run would otherwise
    re-point it): the writers refuse; uninstall and status stay allowed."""
    foreign = _foreign_job(account_home, monkeypatch, platform)
    assert str(foreign) in measure._scheduler_job_points_elsewhere()
    assert measure._scheduler_install_refused() is True
    assert measure._daemon_snapshot_sandboxed() is False  # uninstall/status allowed
    # A dead daemon, so a repair is due (a live one on the dev box would make
    # this "noop-healthy", which writes nothing and never asks the guard).
    monkeypatch.setattr(measure, "_verify_daemon_port", lambda *a, **k: False)
    monkeypatch.setattr(measure, "_daemon_served_version", lambda *a, **k: None)
    assert measure._ensure_dashboard_daemon(force=True) == "noop-sandbox"
    assert measure._restart_dashboard_daemon(platform) == "noop-sandbox"
    monkeypatch.setenv("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", "1")
    assert measure._scheduler_install_refused() is False


@pytest.mark.parametrize("platform", ["Darwin", "Linux", "Windows"])
def test_stale_job_whose_paths_are_gone_is_repaired_not_protected(account_home, monkeypatch, platform):
    """Review 2 MED-2: a job pointing only at paths that no longer exist
    (account renamed by Migration Assistant, CLAUDE_CONFIG_DIR moved) used to
    count as "another install" and blocked every repair forever. A job is
    another install's only while at least one of its paths exists."""
    _foreign_job(account_home, monkeypatch, platform, exists=False)
    assert measure._scheduler_job_points_elsewhere() == ""
    assert measure._scheduler_install_refused() is False


def test_hot_hook_paths_never_query_the_scheduler(account_home, monkeypatch):
    """Review 2 MED-1: the per-prompt pulse, the shared resurrection gate and
    the SessionStart revive used the full guard, which on Windows runs
    `schtasks /Query` (up to 10 s) on every UserPromptSubmit. They now read
    only the frozen sandbox flag; the writers keep the full check."""
    calls = []

    def _recording(*a, **k):
        calls.append("scheduler-query")
        return ["C:\\elsewhere\\dashboard-launcher.cmd"]

    monkeypatch.setattr(measure, "_normalized_platform", lambda: "Windows")
    monkeypatch.setattr(measure, "_windows_task_job_paths", _recording)
    monkeypatch.setattr(measure, "_scheduler_job_points_elsewhere",
                        lambda: calls.append("guard") or "")
    monkeypatch.setattr(measure, "spawn_detached", lambda *a, **k: None)
    monkeypatch.setattr(measure, "_remote_lean_profile", lambda: False)
    measure._daemon_midsession_pulse()
    measure._daemon_resurrection_blocked()
    measure._ensure_health_daemon_revive_first()
    assert calls == [], calls
    # Lean (cloud) answers before any guard at all.
    monkeypatch.setattr(measure, "_remote_lean_profile", lambda: True)
    monkeypatch.setattr(measure, "_daemon_snapshot_sandboxed",
                        lambda: pytest.fail("guard consulted before the lean check"))
    assert measure._daemon_midsession_pulse() == "noop-remote"
    assert measure._ensure_health_daemon_revive_first() == "noop-remote"


def test_scheduler_writers_run_cheap_gates_before_the_job_query(account_home, monkeypatch):
    """Windows CI 5.13.50: the restart and task-heal writers ran the full guard
    (a `schtasks /Query` on Windows) before their own cheap gates, so an
    already-current daemon or a disabled one still spawned schtasks, and
    _ensure_dashboard_daemon did it on every SessionStart. The job check now
    runs only once a write is actually due. Host-independent: Windows is
    simulated, the query is recorded instead of run."""
    queries = []
    monkeypatch.setattr(measure, "_normalized_platform", lambda: "Windows")
    monkeypatch.setattr(measure, "_windows_task_job_paths",
                        lambda: queries.append("schtasks /Query") or [])
    monkeypatch.setattr(measure, "_reclaim_posix_daemon_port", lambda *a, **k: None)
    measure._reset_job_guard_cache()

    # Already current: nothing to write, nothing to ask.
    monkeypatch.setattr(measure, "_daemon_served_version", lambda *a, **k: measure.TOKEN_OPTIMIZER_VERSION)
    assert measure._restart_dashboard_daemon("Windows") == "restarted"
    # Healthy SessionStart ensure: no job query either.
    monkeypatch.setattr(measure, "_remote_lean_profile", lambda: False)
    monkeypatch.setattr(measure, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_daemon_install_failed_marker_present", lambda: False)
    monkeypatch.setattr(measure, "_daemon_service_installed", lambda system: True)
    monkeypatch.setattr(measure, "_verify_daemon_port", lambda *a, **k: True)
    assert measure._ensure_dashboard_daemon() == "noop-healthy"
    # Disabled daemon: the task heal's resurrection gate answers first. (Last,
    # because os.name is spoofed until teardown.)
    monkeypatch.setattr(measure.os, "name", "nt", raising=False)
    monkeypatch.setattr(measure, "_daemon_resurrection_blocked", lambda: "disabled")
    # With os.name spoofed on a POSIX host, the real guard builds a
    # WindowsPath, raises, and fails open BEFORE reaching the task reader, so
    # recording only the reader could never see a misplaced guard here (the
    # planted bug passed on macOS/Linux). Record the guard itself as well.
    monkeypatch.setattr(measure, "_scheduler_job_points_elsewhere",
                        lambda: queries.append("job guard") or "")
    assert measure._heal_windows_task_action() is False
    assert queries == [], queries


def test_doctor_and_cli_name_the_other_install(account_home, monkeypatch, capsys):
    """Review 2 MED-2: with another install's job in place, doctor said
    "disabled by user (opt-out honored)" and scored it, and `setup-daemon`
    printed nothing. Both now name the job and the way out."""
    foreign = _foreign_job(account_home, monkeypatch, "Darwin")
    monkeypatch.setattr(measure, "_daemon_install_failed_marker_present", lambda: False)
    monkeypatch.setattr(measure, "_daemon_resurrection_blocked", lambda: None)
    report = measure.doctor(as_json=True)
    daemon = next(c for c in report["checks"] if c["name"] == "Dashboard daemon")
    assert daemon["status"] == "!!"
    assert "owned by another install" in daemon["detail"] and str(foreign) in daemon["detail"]
    assert "disabled by user" not in daemon["detail"]

    capsys.readouterr()
    assert measure._setup_daemon_cli(["setup-daemon"]) == 1
    out = capsys.readouterr().out
    assert "refused" in out and str(foreign) in out


def test_windows_task_reader_parses_command_and_arguments(monkeypatch):
    xml = ("<Task><Actions><Exec><Command>C:\\Python312\\pythonw.exe</Command>"
           "<Arguments>\"C:\\Users\\a\\.claude\\token-optimizer\\dashboard-server.py\"</Arguments>"
           "</Exec></Actions></Task>")

    class _R:
        returncode = 0
        stdout = "\x00".join(xml)  # schtasks emits UTF-16

    monkeypatch.setattr(measure.subprocess, "run", lambda *a, **k: _R())
    assert measure._windows_task_job_paths() == [
        "C:\\Users\\a\\.claude\\token-optimizer\\dashboard-server.py"]


@pytest.mark.skipif(os.name == "nt", reason="pwd-based account home is POSIX only")
def test_redirected_home_refuses_every_scheduler_write(tmp_path):
    """Import measure the way an e2e/scratch run does (HOME = a temp dir, no
    snapshot override) and prove the daemon and keep-warm schedulers refuse
    before any launchctl/systemctl/schtasks call. Shims on PATH record any
    scheduler command that does get run, so a regression is caught without
    ever touching the real per-account jobs."""
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "scheduler-calls.log"
    for tool in ("launchctl", "systemctl", "schtasks", "crontab", "loginctl", "lsof"):
        shim = shims / tool
        shim.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n', encoding="utf-8")
        shim.chmod(0o755)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    probe = (
        "import json, measure\n"
        "out = {'redirected': measure._HOME_REDIRECTED,\n"
        "       'sandboxed': measure._daemon_snapshot_sandboxed(),\n"
        "       'ensure': measure._ensure_dashboard_daemon(force=True),\n"
        "       'pulse': measure._daemon_midsession_pulse(),\n"
        "       'revive': measure._ensure_health_daemon_revive_first(),\n"
        "       'kw_install': measure._keepwarm_scheduler_install_macos(quiet=True)[0],\n"
        "       'kw_uninstall': measure.keepwarm_scheduler_uninstall(quiet=True)[0],\n"
        "       'kw_heal': measure._heal_keepwarm_plist_path(),\n"
        "       'win_heal': measure._heal_windows_task_action()}\n"
        "print(json.dumps(out))\n"
    )
    env = {
        "PATH": f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "PYTHONPATH": str(SCRIPTS),
        "CLAUDECODE": "1",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
        "TOKEN_OPTIMIZER_RUNTIME": "claude",
    }
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         env=env, timeout=120)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["redirected"] is True and out["sandboxed"] is True
    assert out["ensure"] == "noop-sandbox"
    assert out["pulse"] == "noop-sandbox"
    assert out["revive"] == "noop-sandbox"
    assert out["kw_install"] is False and out["kw_uninstall"] is False
    assert out["kw_heal"] is False and out["win_heal"] is False
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    assert calls == "", f"scheduler commands ran from a redirected HOME:\n{calls}"


def test_deadline_remaining_never_exceeds_its_length(monkeypatch):
    """Windows CI (5.13.51): remaining() read in the same clock tick as start
    returned 10.000000000000028 for a 10 s deadline (float rounding of
    (t0 + s) - t0 at a large monotonic t0), and the inline flush budget then
    overran its share by that much."""
    import hook_runtime
    t0 = 1234567.891  # a large monotonic value, as on a long-running host
    monkeypatch.setattr(hook_runtime.time, "monotonic", lambda: t0)
    for seconds in (10, 10.0, 13.0, 4.5, 0.1):
        d = hook_runtime.HookDeadline(seconds)
        assert d.remaining() <= seconds, (seconds, d.remaining())
