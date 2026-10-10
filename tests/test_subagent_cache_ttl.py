#!/usr/bin/env python3
"""Regression tests for the subagent prompt-cache TTL feature (1h for subagents).

Contract:
  * `measure.py subagent-cache status|enable|disable [--json]` sets
    `subagentPromptCacheTtl: "1h"` in the USER settings.json (CLAUDE_CONFIG_DIR
    aware) -- and never overrides a value the user set, never fights an env
    override, never touches anything when a managed/project/local file already
    sets the key, never sets on Claude Code < 2.1.243, and never writes on
    unknown-state (unreadable / missing) settings.
  * ADVISE-ONLY: the automatic path never writes the key. The cached
    verdict becomes a per-user recommendation in status/doctor/quick/coach
    ("would have saved / is costing about $X net; turn on/off: <cmd>").
    TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 is the ONLY way to get the automatic
    write (explicit opt-in, and only it keeps the 14-day tripwire); =0 stays
    never (and undoes a previous TO-set value).
  * enable records a marker in TO's own data dir (timestamp + previous state);
    disable removes the key ONLY when the marker says TO set it and the value
    is still "1h"; a user who removes/changes the key afterwards is never
    re-set ("user-declined"), and `disable` is final ("removed" is sticky for
    every automatic path).
  * Payoff check: from the user's own sidechain transcripts, count subagent 5m
    cache writes that followed a 5-60 min gap (would have been reads at 1h)
    versus all subagent cache writes (2x instead of 1.25x at 1h). Net estimate
    in tokens and dollars, labelled an estimate.
  * Tripwire (opted-in automation only): 14+ days of post-enable data with a
    negative net estimate -> the next SessionStart reverts (only when TO set
    it), records "auto-reverted", and prints one line saying so.
  * SessionStart wiring: with the force env off, session start never writes
    and never emits a notice; it only keeps the cached verdict fresh and
    records the judgement.
  * Clean no-ops: Cowork (never reads ~/.claude), Codex and other runtimes.

Run: python3 -m pytest tests/test_subagent_cache_ttl.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"

KEY = "subagentPromptCacheTtl"

USER_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "opus",
    "env": {"MY_KEY": "keep-me"},
}

NOTICE_EXPECTED = (
    "Token Optimizer set the subagent cache to 1 hour (was 5 minutes)."
)


def _write_settings(path: Path, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(USER_SETTINGS if data is None else data, indent=2),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def m(tmp_path, monkeypatch):
    """Fresh measure import pinned to a temp home + snapshot dir."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(data_dir))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", MEASURE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "claude"
    home.mkdir(parents=True, exist_ok=True)
    settings = _write_settings(home / "settings.json")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(mod, "is_cowork", lambda: False)
    # Deterministic Claude Code version (2.1.250 >= 2.1.243 floor).
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version",
                        lambda: (2, 1, 250))
    # Billing mode would read the real ~/.claude.json: pin it (API by default).
    monkeypatch.setattr(mod, "keepwarm_billing_mode", lambda *a, **k: "api")
    # The automatic path is evidence-gated. Most tests exercise the guards and
    # the undo machinery around a write, so they run "always on" (the documented
    # force switch); the evidence-gate tests call `_gated(m, monkeypatch)`.
    monkeypatch.setenv("TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H", "1")
    # A background scan is never really spawned by a test: record the argv.
    mod._spawn_log = []

    def _fake_spawn(argv, **kw):
        mod._spawn_log.append((list(argv), kw))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(mod, "spawn_detached", _fake_spawn)
    yield mod, settings, home
    sys.modules.pop("measure", None)


def _read(settings: Path) -> dict:
    return json.loads(settings.read_text(encoding="utf-8"))


def _marker(m):
    mod, _settings, _home = m
    p = mod._subagent_cache_marker_path()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# enable: the happy path
# ---------------------------------------------------------------------------

def test_enable_sets_key_and_marker(m):
    mod, settings, _home = m
    r = mod.subagent_cache_enable(now=1_800_000_000)
    assert r["state"] == "set", r
    assert r["changed"] is True
    assert _read(settings)[KEY] == "1h"
    mk = _marker(m)
    assert mk and mk["state"] == "set"
    assert mk["set_ts"] == 1_800_000_000
    assert mk["previous"] == {"present": False}
    assert mk["set_by"] == "token-optimizer"


def test_enable_notice_carries_undo_command(m):
    mod, settings, _home = m
    r = mod.subagent_cache_enable()
    assert r["notice"] and NOTICE_EXPECTED in r["notice"]
    assert "subagent-cache disable" in r["notice"]
    # The resolved command form: a real path to measure.py, never a bare name.
    assert "measure.py" in r["notice"]


def test_enable_is_idempotent_noop_after_first_success(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    before = settings.read_text(encoding="utf-8")
    r = mod.subagent_cache_enable()
    assert r["state"] == "set"
    assert r["changed"] is False
    assert settings.read_text(encoding="utf-8") == before


def test_enable_never_overrides_user_set_value(m):
    mod, settings, _home = m
    data = dict(USER_SETTINGS)
    data[KEY] = "5m"
    _write_settings(settings, data)
    r = mod.subagent_cache_enable()
    assert r["state"] == "user-set", r
    assert r["changed"] is False
    assert _read(settings)[KEY] == "5m"


def test_enable_never_overrides_existing_1h_user_value(m):
    mod, settings, _home = m
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(settings, data)
    r = mod.subagent_cache_enable()
    assert r["state"] == "user-set", r
    assert r["changed"] is False
    # And no marker claiming TO set it.
    mk = _marker(m)
    assert mk is None or mk["state"] != "set"


# ---------------------------------------------------------------------------
# enable: env outranks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env_var", [
    "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "FORCE_PROMPT_CACHING_5M"])
def test_enable_noop_when_cache_env_set(m, monkeypatch, env_var):
    mod, settings, _home = m
    monkeypatch.setenv(env_var, "5m" if "SUBAGENT" in env_var else "1")
    r = mod.subagent_cache_enable()
    assert r["state"] == "env-override", r
    assert r["changed"] is False
    assert KEY not in _read(settings)
    assert env_var in r["reason"]


# ---------------------------------------------------------------------------
# enable: external settings files win, TO reports which
# ---------------------------------------------------------------------------

def test_enable_noop_when_project_settings_set_key(m, tmp_path, monkeypatch):
    mod, settings, _home = m
    proj = tmp_path / "proj"
    _write_settings(proj / ".claude" / "settings.json", {
        KEY: "5m"})
    monkeypatch.chdir(proj)
    r = mod.subagent_cache_enable()
    assert r["state"] == "external-setting", r
    assert "settings.json" in r["reason"]
    assert r["changed"] is False
    assert KEY not in _read(settings)


def test_enable_noop_when_local_settings_set_key(m, tmp_path, monkeypatch):
    mod, settings, _home = m
    proj = tmp_path / "proj"
    _write_settings(proj / ".claude" / "settings.local.json", {KEY: "5m"})
    monkeypatch.chdir(proj)
    r = mod.subagent_cache_enable()
    assert r["state"] == "external-setting", r
    assert "settings.local.json" in r["reason"]
    assert KEY not in _read(settings)


def test_enable_noop_when_managed_settings_set_key(m, monkeypatch):
    mod, settings, _home = m
    managed = _home / "managed-settings.json"
    _write_settings(managed, {KEY: "5m"})
    monkeypatch.setattr(mod, "_subagent_cache_managed_settings_path",
                        lambda: managed)
    r = mod.subagent_cache_enable()
    assert r["state"] == "external-setting", r
    assert "managed-settings.json" in r["reason"]
    assert KEY not in _read(settings)


def test_managed_path_is_platform_specific(m, monkeypatch):
    mod, _settings, _home = m
    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    p = str(mod._subagent_cache_managed_settings_path())
    assert p.lower().endswith("managed-settings.json")
    assert "ClaudeCode" in p
    monkeypatch.setattr(mod.platform, "system", lambda: "Darwin")
    assert str(mod._subagent_cache_managed_settings_path()).startswith(
        "/Library/Application Support/ClaudeCode/")
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    assert str(mod._subagent_cache_managed_settings_path()) == \
        "/etc/claude-code/managed-settings.json"


# ---------------------------------------------------------------------------
# enable: Claude Code version floor 2.1.243
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version", [(2, 1, 242), (2, 0, 999), (1, 99, 9)])
def test_enable_skips_on_claude_below_floor(m, monkeypatch, version):
    mod, settings, _home = m
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version",
                        lambda: version)
    r = mod.subagent_cache_enable()
    assert r["state"] == "version-unsupported", r
    assert KEY not in _read(settings)
    assert r["changed"] is False


def test_enable_skips_on_unknown_claude_version(m, monkeypatch):
    mod, settings, _home = m
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version",
                        lambda: None)
    r = mod.subagent_cache_enable()
    assert r["state"] == "version-unsupported", r
    assert KEY not in _read(settings)


def test_version_probe_not_run_when_nothing_to_set(m, monkeypatch):
    """The `claude --version` subprocess (up to 3s) must only run when a
    write is actually about to happen: a user-set key, an env override or a
    higher-priority file all answer first, so SessionStart stays cheap."""
    mod, settings, _home = m
    calls = []

    def probe():
        calls.append(1)
        return (2, 1, 250)

    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version", probe)
    _write_settings(settings, dict(USER_SETTINGS, **{KEY: "5m"}))
    assert mod.subagent_cache_enable()["state"] == "user-set"
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "5m")
    assert mod.subagent_cache_enable()["state"] in ("user-set", "env-override")
    assert calls == []


def test_version_probe_parses_claude_version_output(m, monkeypatch):
    """The probe reads `claude --version` and returns a semver tuple."""
    mod, _settings, _home = m

    class R:
        returncode = 0
        stdout = "2.1.250 (Claude Code)"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    assert mod._subagent_cache_claude_code_version() == (2, 1, 250)


# ---------------------------------------------------------------------------
# disable: only undoes what TO set
# ---------------------------------------------------------------------------

def test_disable_removes_key_to_set(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    r = mod.subagent_cache_disable()
    assert r["state"] == "removed", r
    assert r["changed"] is True
    assert KEY not in _read(settings)
    # Every other key intact.
    assert _read(settings)["model"] == "opus"


def test_disable_without_prior_enable_is_a_reported_noop(m):
    mod, settings, _home = m
    r = mod.subagent_cache_disable()
    assert r["state"] in ("nothing-to-undo", "off"), r
    assert r["changed"] is False
    assert KEY not in _read(settings)


def test_disable_leaves_user_set_value_alone(m):
    mod, settings, _home = m
    data = dict(USER_SETTINGS)
    data[KEY] = "5m"
    _write_settings(settings, data)
    r = mod.subagent_cache_disable()
    assert r["changed"] is False
    assert _read(settings)[KEY] == "5m"


def test_disable_leaves_changed_value_alone(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    data = _read(settings)
    data[KEY] = "5m"
    _write_settings(settings, data)
    r = mod.subagent_cache_disable()
    assert r["state"] == "user-changed", r
    assert r["changed"] is False
    assert _read(settings)[KEY] == "5m"


# ---------------------------------------------------------------------------
# user-declined: a user who removes/changes the key is never re-set
# ---------------------------------------------------------------------------

def test_user_removed_key_is_never_set_again(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    data = _read(settings)
    del data[KEY]
    _write_settings(settings, data)
    r = mod.subagent_cache_enable()
    assert r["state"] == "user-declined", r
    assert r["changed"] is False
    assert KEY not in _read(settings)
    assert _marker(m)["state"] == "user-declined"
    # ...and the automatic path keeps honouring it.
    r2 = mod.subagent_cache_enable()
    assert r2["state"] == "user-declined"
    assert KEY not in _read(settings)


def test_user_changed_key_is_never_reset(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    data = _read(settings)
    data[KEY] = "5m"
    _write_settings(settings, data)
    r = mod.subagent_cache_enable()
    assert r["state"] == "user-declined", r
    assert r["changed"] is False
    assert _read(settings)[KEY] == "5m"


def test_explicit_cli_enable_after_decline_is_honoured(m):
    """A user who explicitly runs `subagent-cache enable` reverses the
    decline deliberately; only the AUTOMATIC path stays blocked."""
    mod, settings, _home = m
    mod.subagent_cache_enable()
    data = _read(settings)
    del data[KEY]
    _write_settings(settings, data)
    mod.subagent_cache_enable()  # records user-declined
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "set", r
    assert _read(settings)[KEY] == "1h"


def test_auto_revert_blocks_automatic_reenable(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    mk = _marker(m)
    mk["state"] = "auto-reverted"
    mod._subagent_cache_write_marker(mk)
    before = settings.read_text(encoding="utf-8")
    r = mod.subagent_cache_enable()
    assert r["state"] == "auto-reverted", r
    # The automatic path neither sets nor removes anything here.
    assert r["changed"] is False
    assert settings.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# opt-out env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", ["0", "false", "off", "no"])
def test_optout_env_blocks_enable_and_undoes(m, monkeypatch, val):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    assert _read(settings)[KEY] == "1h"
    monkeypatch.setenv("TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H", val)
    r = mod.subagent_cache_enable()
    assert r["state"] == "opted-out", r
    assert r["changed"] is True
    assert KEY not in _read(settings)
    assert _marker(m)["state"] == "opted-out"


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_truthy_optout_env_does_not_opt_out(m, monkeypatch, val):
    mod, settings, _home = m
    monkeypatch.setenv("TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H", val)
    r = mod.subagent_cache_enable()
    assert r["state"] == "set", r
    assert _read(settings)[KEY] == "1h"


def test_optout_env_with_no_prior_set_does_nothing(m, monkeypatch):
    mod, settings, _home = m
    monkeypatch.setenv("TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H", "0")
    r = mod.subagent_cache_enable()
    assert r["state"] == "opted-out", r
    assert r["changed"] is False
    assert KEY not in _read(settings)


# ---------------------------------------------------------------------------
# unknown-state settings: never write
# ---------------------------------------------------------------------------

def test_invalid_json_settings_never_written(m):
    mod, settings, _home = m
    settings.write_text("{ not json !!!", encoding="utf-8")
    r = mod.subagent_cache_enable()
    assert r["state"] == "unknown-settings", r
    assert r["changed"] is False
    assert settings.read_text(encoding="utf-8") == "{ not json !!!"


def test_missing_settings_file_is_never_created(m):
    mod, settings, _home = m
    settings.unlink()
    r = mod.subagent_cache_enable()
    assert r["state"] == "unknown-settings", r
    assert not settings.exists()


# ---------------------------------------------------------------------------
# Cross-platform / cross-runtime no-ops
# ---------------------------------------------------------------------------

def test_claude_config_dir_respected(m, tmp_path, monkeypatch):
    """enable must write the settings.json INSIDE the CLAUDE_CONFIG_DIR."""
    mod, _settings, _home = m
    cfg = tmp_path / "relocated-config"
    cfg.mkdir()
    relocated = _write_settings(cfg / "settings.json", {"model": "opus"})
    monkeypatch.setattr(mod, "SETTINGS_PATH", relocated)
    r = mod.subagent_cache_enable()
    assert r["state"] == "set", r
    assert json.loads(relocated.read_text(encoding="utf-8"))[KEY] == "1h"
    # The default-home settings file was never touched.
    assert KEY not in json.loads(_settings.read_text(encoding="utf-8"))


def test_cowork_is_clean_noop(m, monkeypatch):
    mod, settings, _home = m
    monkeypatch.setattr(mod, "is_cowork", lambda: True)
    before = settings.read_text(encoding="utf-8")
    assert mod.subagent_cache_enable()["state"] == "platform-gap"
    assert mod.subagent_cache_disable()["state"] == "platform-gap"
    assert mod.subagent_cache_status()["state"] == "platform-gap"
    assert settings.read_text(encoding="utf-8") == before
    assert _marker(m) is None


@pytest.mark.parametrize("runtime", ["codex", "copilot", "cursor", "hermes"])
def test_foreign_runtime_is_clean_noop(m, monkeypatch, runtime):
    mod, settings, _home = m
    monkeypatch.setattr(mod, "detect_runtime", lambda: runtime)
    before = settings.read_text(encoding="utf-8")
    assert mod.subagent_cache_enable()["state"] == "platform-gap"
    assert mod.subagent_cache_disable()["state"] == "platform-gap"
    assert mod.subagent_cache_status()["state"] == "platform-gap"
    assert settings.read_text(encoding="utf-8") == before
    assert _marker(m) is None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_reports_state_and_who(m):
    mod, settings, _home = m
    st = mod.subagent_cache_status()
    assert st["state"] == "off"
    assert st["payoff"] is not None or st["payoff"] == {}
    mod.subagent_cache_enable()
    st = mod.subagent_cache_status()
    assert st["state"] == "set"
    assert st["set_by"] == "token-optimizer"
    assert "payoff" in st


def test_status_reports_user_set_without_marker(m):
    mod, settings, _home = m
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(settings, data)
    st = mod.subagent_cache_status()
    assert st["state"] == "user-set"
    assert st["set_by"] == "user"


def test_status_pays_off_estimate_is_labelled(m):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    st = mod.subagent_cache_status()
    payoff = st["payoff"]
    assert payoff is not None
    assert payoff.get("estimate") is True, "dollar figures must be labelled estimates"


# ---------------------------------------------------------------------------
# payoff accounting (deterministic, from crafted sidechain transcripts)
# ---------------------------------------------------------------------------

def _sidechain_record(ts, req, cc5m, model="claude-sonnet-4-5", cr=0, cc1h=0):
    return {
        "type": "assistant",
        "isSidechain": True,
        "timestamp": ts,
        "requestId": req,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc5m + cc1h,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cc5m,
                    "ephemeral_1h_input_tokens": cc1h,
                },
            },
        },
    }


def _write_sidechain(home: Path, name: str, records, project="proj",
                     agent_type=None):
    f = home / "projects" / project / "subagents" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    if agent_type:
        f.with_suffix(".meta.json").write_text(
            json.dumps({"agentType": agent_type}), encoding="utf-8")
    return f


def _pad(home: Path, n=250, project="pad"):
    """Enough harmless subagent requests (pure small reads, 10 s apart, own
    project + model so they never join another group) to clear the
    tripwire's minimum sample without changing any estimate."""
    recs = []
    base = time.mktime(time.strptime("2026-10-09", "%Y-%m-%d"))
    for i in range(n):
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + i * 10))
        recs.append(_sidechain_record(t, f"pad{i}", 0, model="claude-haiku-4-5",
                                      cr=100))
    return _write_sidechain(home, "pad.jsonl", recs, project=project)


def test_payoff_gap_classification(m):
    mod, _settings, home = m
    t0 = "2026-10-01T10:00:00Z"
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record(t0, "r1", 1000),
        _sidechain_record("2026-10-01T10:10:00Z", "r2", 800),   # +10m: would-be read
        _sidechain_record("2026-10-01T10:11:40Z", "r3", 500),   # +100s: real write
        _sidechain_record("2026-10-01T12:11:40Z", "r4", 400),   # +2h: beyond 1h
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["subagent_5m_cache_writes"] == 4
    assert p["write_tokens_5m"] == 2700
    assert p["would_have_been_reads"] == 1
    assert p["missed_read_tokens"] == 800
    assert p["estimate"] is True


def test_payoff_dollar_math(m):
    mod, _settings, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-01T10:00:00Z", "r1", 1000),
        _sidechain_record("2026-10-01T10:10:00Z", "r2", 800),
        _sidechain_record("2026-10-01T10:11:40Z", "r3", 500),
        _sidechain_record("2026-10-01T12:11:40Z", "r4", 400),
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    # sonnet rate card: 5m write 3.75, 1h write 6.0, read 0.3 (per MTok).
    savings = 800 * (3.75 - 0.3) / 1e6
    extra = 2700 * (6.0 - 3.75) / 1e6
    assert p["savings_usd_est"] == pytest.approx(savings, abs=1e-9)
    assert p["extra_write_cost_usd_est"] == pytest.approx(extra, abs=1e-9)
    assert p["net_usd_est"] == pytest.approx(savings - extra, abs=1e-9)


def test_payoff_ignores_main_transcripts(m):
    """A top-level (non-sidechain) transcript is not subagent spend."""
    mod, _settings, home = m
    f = home / "projects" / "proj" / "main.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    main_rec1 = _sidechain_record("2026-10-01T10:00:00Z", "r1", 5000)
    main_rec2 = _sidechain_record("2026-10-01T11:30:00Z", "r2", 5000)
    del main_rec1["isSidechain"]  # a top-level transcript carries no flag
    del main_rec2["isSidechain"]
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(main_rec1) + "\n")
        fh.write(json.dumps(main_rec2) + "\n")
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["subagent_5m_cache_writes"] == 0
    assert p["would_have_been_reads"] == 0


def test_payoff_respects_window_and_since(m):
    mod, _settings, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-09-01T10:00:00Z", "r1", 1000),
        _sidechain_record("2026-09-01T10:30:00Z", "r2", 1000),
        _sidechain_record("2026-10-01T10:00:00Z", "r3", 1000),
        _sidechain_record("2026-10-01T10:30:00Z", "r4", 1000),
        _sidechain_record("2026-10-08T10:00:00Z", "r5", 1000),
        _sidechain_record("2026-10-08T10:30:00Z", "r6", 1000),
    ])
    now = time.mktime(time.strptime("2026-10-10", "%Y-%m-%d"))
    p = mod.subagent_cache_payoff(days=30, now=now)
    assert p["subagent_5m_cache_writes"] == 4  # September is outside 30d
    since = time.mktime(time.strptime("2026-10-05", "%Y-%m-%d"))
    p2 = mod.subagent_cache_payoff(days=30, now=now, since_ts=since)
    assert p2["subagent_5m_cache_writes"] == 2  # only the Oct 8 pair
    since_early = time.mktime(time.strptime("2026-08-01", "%Y-%m-%d"))
    p3 = mod.subagent_cache_payoff(days=365, now=now, since_ts=since_early)
    assert p3["subagent_5m_cache_writes"] == 6


def test_payoff_empty_machine_is_honest_zero(m):
    mod, _settings, _home = m
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["subagent_5m_cache_writes"] == 0
    assert p["net_usd_est"] == 0.0
    assert p["estimate"] is True


# ---------------------------------------------------------------------------
# payoff v2: across-spawn benefit (flaw 1) and realized 1h accounting (flaw 2)
# Sonnet rate card per MTok: 5m write 3.75, 1h write 6.0, read 0.3
# => premium 2.25/MTok, one avoided rewrite saves 3.45/MTok.
# ---------------------------------------------------------------------------

SAVE = 3.45 / 1e6
PREMIUM = 2.25 / 1e6


def _two_spawns(home, gap_first="10:20:00", agent_a=None, agent_b=None,
                project_a="proj", project_b="proj", cc_b=14000,
                day="2026-10-05"):
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record(f"{day}T10:00:00Z", "a1", 12000),
    ], project=project_a, agent_type=agent_a)
    _write_sidechain(home, "b.jsonl", [
        _sidechain_record(f"{day}T{gap_first}Z", "b1", cc_b),
    ], project=project_b, agent_type=agent_b)


def test_across_spawn_win_two_spawns_20_min_apart(m):
    mod, _s, home = m
    _two_spawns(home)
    p = mod.subagent_cache_payoff(now=time.time())
    # Shared prefix = smallest first request (12000); spawn b rewrote 14000
    # at 5m, 12000 of it would have been a read at 1h.
    assert p["across_spawn_tokens"] == 12000
    assert p["within_agent_tokens"] == 0
    assert p["missed_read_tokens"] == 12000
    assert p["would_have_been_reads"] == 1
    assert p["savings_usd_est"] == pytest.approx(12000 * SAVE, abs=1e-9)
    assert p["extra_write_cost_usd_est"] == pytest.approx(26000 * PREMIUM, abs=1e-9)
    assert p["estimate"] is True


def test_across_spawn_no_win_when_spawns_two_minutes_apart(m):
    mod, _s, home = m
    _two_spawns(home, gap_first="10:02:00")
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["across_spawn_tokens"] == 0, "already shared at the 5m TTL"
    assert p["would_have_been_reads"] == 0


def test_across_spawn_gap_is_from_latest_activity_not_spawn_start(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "a1", 12000),
        _sidechain_record("2026-10-05T10:18:00Z", "a2", 100, cr=12000),
    ])
    _write_sidechain(home, "b.jsonl", [
        _sidechain_record("2026-10-05T10:20:00Z", "b1", 14000),
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    # The prefix was touched at 10:18; spawn b at 10:20 is 2 minutes later.
    assert p["across_spawn_tokens"] == 0


def test_across_spawn_no_win_when_spawns_three_hours_apart(m):
    mod, _s, home = m
    _two_spawns(home, gap_first="13:00:00")
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["across_spawn_tokens"] == 0, "1h cache would have expired too"
    assert p["would_have_been_reads"] == 0


def test_single_spawn_has_no_across_estimate(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "a1", 12000),
        _sidechain_record("2026-10-05T10:20:00Z", "a2", 500),
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["across_spawn_tokens"] == 0, "needs 2+ spawns for a shared prefix"
    # the existing within-transcript rule still applies to the 20 min gap
    assert p["within_agent_tokens"] == 500
    assert p["missed_read_tokens"] == 500


def test_grouping_by_agent_type_when_recorded(m):
    mod, _s, home = m
    _two_spawns(home, agent_a="researcher", agent_b="researcher")
    same = mod.subagent_cache_payoff(now=time.time())
    assert same["across_spawn_tokens"] == 12000
    assert "agent type" in same["grouping"]
    assert "not recorded" not in same["grouping"]
    # a different agent type is a different prefix: no shared-prefix win
    for f in list((home / "projects").rglob("*")):
        if f.is_file():
            f.unlink()
    _two_spawns(home, agent_a="researcher", agent_b="reviewer")
    diff = mod.subagent_cache_payoff(now=time.time())
    assert diff["across_spawn_tokens"] == 0


def test_grouping_falls_back_to_project_and_model_and_says_so(m):
    mod, _s, home = m
    _two_spawns(home)  # no .meta.json: the agent type is not recorded
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["across_spawn_tokens"] == 12000
    assert "not recorded" in p["grouping"]
    # different project: a different prefix
    for f in list((home / "projects").rglob("*")):
        if f.is_file():
            f.unlink()
    _two_spawns(home, project_b="other")
    assert mod.subagent_cache_payoff(now=time.time())["across_spawn_tokens"] == 0


def test_across_spawn_groups_are_per_model(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "a1", 12000)])
    _write_sidechain(home, "b.jsonl", [
        _sidechain_record("2026-10-05T10:20:00Z", "b1", 12000,
                          model="claude-haiku-4-5")])
    assert mod.subagent_cache_payoff(now=time.time())["across_spawn_tokens"] == 0


def test_across_since_uses_pre_window_activity_as_context(m):
    from datetime import datetime
    mod, _s, home = m
    _two_spawns(home)

    def utc(stamp):
        return datetime.fromisoformat(stamp + "+00:00").timestamp()

    # both spawns happened before `since`: nothing to count
    p = mod.subagent_cache_payoff(now=time.time(), since_ts=utc("2026-10-05T12:00:00"))
    assert p["across_spawn_tokens"] == 0
    # spawn a before `since`, spawn b after it: b is counted against a
    p = mod.subagent_cache_payoff(now=time.time(), since_ts=utc("2026-10-05T10:10:00"))
    assert p["across_spawn_tokens"] == 12000
    assert p["subagent_5m_cache_writes"] == 1


def test_no_double_counting_between_within_and_across(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "a1", 12000),
    ])
    _write_sidechain(home, "b.jsonl", [
        _sidechain_record("2026-10-05T10:20:00Z", "b1", 14000),   # across: 12000
        _sidechain_record("2026-10-05T10:40:00Z", "b2", 3000),    # within: 3000
        _sidechain_record("2026-10-05T10:41:00Z", "b3", 800),     # 1 min: write
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["across_spawn_tokens"] == 12000
    assert p["within_agent_tokens"] == 3000
    assert p["missed_read_tokens"] == 15000
    assert p["would_have_been_reads"] == 2, "each request counted at most once"
    assert p["write_tokens_5m"] == 12000 + 14000 + 3000 + 800
    assert p["missed_read_tokens"] <= p["write_tokens_5m"]


def test_5m_write_request_reads_are_not_counted_as_realized(m):
    """A request that wrote at 5m ran under the 5m TTL: its reads are not a
    1h win and its write tokens are not 1h premium."""
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "a1", 1000),
        _sidechain_record("2026-10-05T10:30:00Z", "a2", 500, cr=1000),
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["realized_read_tokens"] == 0
    assert p["subagent_1h_cache_writes"] == 0
    assert p["within_agent_tokens"] == 500


def test_post_enable_realized_reads_within_one_agent(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "r1", 0, cc1h=5000),
        _sidechain_record("2026-10-05T10:30:00Z", "r2", 0, cc1h=100, cr=5000),
        _sidechain_record("2026-10-05T10:31:00Z", "r3", 0, cr=5100),   # 1 min
        _sidechain_record("2026-10-05T11:10:00Z", "r4", 0, cr=5100),   # pure read, 39 min
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["subagent_5m_cache_writes"] == 0
    assert p["subagent_1h_cache_writes"] == 2
    assert p["write_tokens_1h"] == 5100
    assert p["realized_read_tokens"] == 5000 + 5100
    assert p["savings_usd_est"] == pytest.approx(10100 * SAVE, abs=1e-9)
    assert p["extra_write_cost_usd_est"] == pytest.approx(5100 * PREMIUM, abs=1e-9)
    assert p["net_usd_est"] == pytest.approx(10100 * SAVE - 5100 * PREMIUM, abs=1e-9)
    assert p["net_usd_est"] > 0


def test_post_enable_realized_reads_across_spawns(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "a1", 0, cc1h=12000)])
    _write_sidechain(home, "b.jsonl", [
        # first request of a spawn 20 min later: the prefix is a read, and it
        # carries no write at all -- the regime comes from the group's cache
        _sidechain_record("2026-10-05T10:20:00Z", "b1", 0, cr=12000)])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["realized_read_tokens"] == 12000
    assert p["across_spawn_tokens"] == 0, "counterfactual part is 5m-only"
    assert p["savings_usd_est"] == pytest.approx(12000 * SAVE, abs=1e-9)
    assert p["extra_write_cost_usd_est"] == pytest.approx(12000 * PREMIUM, abs=1e-9)


def test_post_enable_loss_is_visible(m):
    mod, _s, home = m
    _write_sidechain(home, "a.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "l1", 0, cc1h=8000),
        _sidechain_record("2026-10-05T12:00:00Z", "l2", 0, cc1h=8000),   # 2h: expired
    ])
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["realized_read_tokens"] == 0
    assert p["extra_write_cost_usd_est"] == pytest.approx(16000 * PREMIUM, abs=1e-9)
    assert p["net_usd_est"] < 0, "the setting is on and nothing is being read"


def test_mixed_5m_and_1h_history_is_one_estimate(m):
    mod, _s, home = m
    # Before enabling (project old): two spawns on 5m writes, 20 min apart.
    _write_sidechain(home, "o1.jsonl", [
        _sidechain_record("2026-09-25T10:00:00Z", "o1", 12000)], project="old")
    _write_sidechain(home, "o2.jsonl", [
        _sidechain_record("2026-09-25T10:20:00Z", "o2", 12000)], project="old")
    # After enabling (project new): the same pattern on 1h writes + reads.
    _write_sidechain(home, "n1.jsonl", [
        _sidechain_record("2026-10-05T10:00:00Z", "n1", 0, cc1h=12000)],
        project="new")
    _write_sidechain(home, "n2.jsonl", [
        _sidechain_record("2026-10-05T10:20:00Z", "n2", 0, cc1h=500, cr=12000)],
        project="new")
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["across_spawn_tokens"] == 12000          # counterfactual, 5m part
    assert p["realized_read_tokens"] == 12000         # realized, 1h part
    assert p["subagent_5m_cache_writes"] == 2
    assert p["subagent_1h_cache_writes"] == 2
    assert p["savings_usd_est"] == pytest.approx(24000 * SAVE, abs=1e-9)
    # premium: counterfactual on 24000 5m tokens + actually paid on 12500 1h
    assert p["extra_write_cost_usd_est"] == pytest.approx(36500 * PREMIUM, abs=1e-9)
    assert p["net_usd_est"] == pytest.approx(
        24000 * SAVE - 36500 * PREMIUM, abs=1e-9)


def test_payoff_counts_subagent_requests_for_the_sample_guard(m):
    mod, _s, home = m
    _pad(home, n=40)
    p = mod.subagent_cache_payoff(now=time.time())
    assert p["subagent_requests"] == 40


# ---------------------------------------------------------------------------
# tripwire: minimum sample + post-enable (1h) data
# ---------------------------------------------------------------------------


def _set_ts(m):
    return float(_marker(m)["set_ts"])


def _judge(m, now=None):
    """The tripwire reads a cached verdict; the background scan writes it.
    Run that scan synchronously (as the detached child would), then judge."""
    mod, _settings, _home = m
    now = time.time() if now is None else now
    mod.subagent_cache_scan_run(now=now, since_ts=_set_ts(m))
    return mod.evaluate_subagent_cache_tripwire(now=now)


def _loss_history(home):
    _write_sidechain(home, "loss.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "x1", 0, cc1h=8000),
        _sidechain_record("2026-10-09T12:00:00Z", "x2", 0, cc1h=8000),
    ])


def test_tripwire_reverts_on_post_enable_loss(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _loss_history(home)
    _pad(home)
    r = _judge(m)
    assert r["reverted"] is True, r
    assert r["net_usd_est"] < 0
    assert KEY not in _read(settings)
    assert _marker(m)["state"] == "auto-reverted"


def test_tripwire_keeps_post_enable_win(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _write_sidechain(home, "win.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "w1", 0, cc1h=5000),
        _sidechain_record("2026-10-09T10:30:00Z", "w2", 0, cc1h=100, cr=5000),
        _sidechain_record("2026-10-09T11:10:00Z", "w3", 0, cr=5100),
    ])
    _pad(home)
    r = _judge(m)
    assert r["reverted"] is False, r
    assert r["net_usd_est"] > 0
    assert _read(settings)[KEY] == "1h"


def test_tripwire_minimum_sample_guard(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _loss_history(home)   # clearly negative, but only 2 subagent requests
    r = _judge(m)
    assert r["reverted"] is False
    assert r["notice"] is None
    assert "not enough data" in (r.get("reason") or "")
    assert str(mod._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS) in r["reason"]
    assert _read(settings)[KEY] == "1h"
    assert _marker(m)["state"] == "set"


def test_tripwire_sample_guard_lets_go_once_enough_data_exists(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _loss_history(home)
    t0 = time.time()
    assert _judge(m, now=t0)["reverted"] is False
    _pad(home, n=mod._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS)
    # next day's re-judgment sees enough data
    r = _judge(m, now=t0 + 90000)
    assert r["reverted"] is True, r


def test_user_with_almost_no_subagents_is_never_reverted(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _write_sidechain(home, "one.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "z1", 0, cc1h=300000)])
    r = _judge(m)
    assert r["reverted"] is False
    assert _read(settings)[KEY] == "1h"


# ---------------------------------------------------------------------------
# status text: two short lines, tokens first on subscription
# ---------------------------------------------------------------------------

def test_status_text_shows_both_parts_and_assumptions(m, capsys):
    mod, _s, home = m
    _two_spawns(home)
    mod._subagent_cache_cli(["subagent-cache", "status"])
    out = capsys.readouterr().out
    assert "ESTIMATE from your own transcripts" in out
    assert "within" in out and "across" in out
    assert "5-60 min" in out
    assert "not recorded" in out
    assert "API-equivalent" not in out, "API billing pays real dollars"


def test_status_text_leads_with_tokens_on_subscription(m, capsys, monkeypatch):
    mod, _s, home = m
    monkeypatch.setattr(mod, "keepwarm_billing_mode", lambda *a, **k: "subscription")
    _two_spawns(home)
    mod._subagent_cache_cli(["subagent-cache", "status"])
    out = capsys.readouterr().out
    assert "API-equivalent" in out
    assert "ESTIMATE from your own transcripts" in out
    assert out.index("tokens") < out.index("$")
    st = mod.subagent_cache_status()
    assert st["billing_mode"] == "subscription"


# ---------------------------------------------------------------------------
# tripwire: 14+ days of negative net after enabling -> auto-revert
# ---------------------------------------------------------------------------

def _enable_15d_ago(m):
    mod, settings, _home = m
    mod.subagent_cache_enable(now=time.time() - 15 * 86400)


def test_tripwire_reverts_on_negative_net(m):
    mod, settings, _home = m
    _enable_15d_ago(m)
    # Since enable: subagent writes on sub-hour gaps? No -- make PURE cost:
    # one write, then a 2h-gap rewrite (no would-be reads), so net < 0.
    _write_sidechain(_home, "neg.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "n1", 5000),
        _sidechain_record("2026-10-09T12:00:00Z", "n2", 5000),
    ])
    _pad(_home)
    r = _judge(m)
    assert r["reverted"] is True, r
    assert r["net_usd_est"] < 0
    assert KEY not in _read(settings)
    mk = _marker(m)
    assert mk["state"] == "auto-reverted"
    assert r["notice"] and "subagent-cache enable" in r["notice"]


def test_tripwire_keeps_setting_on_positive_net(m):
    mod, settings, _home = m
    _enable_15d_ago(m)
    # p2 follows a 30-min gap with a 10000-token rewrite (savings
    # 10000*(3.75-0.3)/1e6 = $0.0345) while the extra 1h write premium is
    # only (10002)*(6.0-3.75)/1e6 = $0.0225 -- a genuinely positive net.
    _write_sidechain(_home, "pos.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "p1", 5000),
        _sidechain_record("2026-10-09T10:30:00Z", "p2", 10000),
        _sidechain_record("2026-10-09T10:30:01Z", "p3", 1),
        _sidechain_record("2026-10-09T12:00:00Z", "p4", 1),
    ])
    _pad(_home)
    r = _judge(m)
    assert r["reverted"] is False, r
    assert r["net_usd_est"] > 0
    assert _read(settings)[KEY] == "1h"
    assert _marker(m)["state"] == "set"


def test_tripwire_needs_14_days(m):
    mod, settings, _home = m
    mod.subagent_cache_enable(now=time.time() - 5 * 86400)
    _write_sidechain(_home, "neg.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "n1", 5000),
        _sidechain_record("2026-10-09T12:00:00Z", "n2", 5000),
    ])
    _pad(_home)
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
    assert r["reverted"] is False
    assert _read(settings)[KEY] == "1h"


def test_tripwire_not_judged_without_post_enable_writes(m):
    mod, settings, _home = m
    _enable_15d_ago(m)
    r = _judge(m)
    assert r["reverted"] is False
    assert _read(settings)[KEY] == "1h"


def test_tripwire_judges_at_most_once_per_day(m, monkeypatch):
    mod, settings, _home = m
    _enable_15d_ago(m)
    _write_sidechain(_home, "pos.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "p1", 5000),
        _sidechain_record("2026-10-09T10:30:00Z", "p2", 10000),
        _sidechain_record("2026-10-09T10:30:01Z", "p3", 1),
        _sidechain_record("2026-10-09T12:00:00Z", "p4", 1),
    ])
    _pad(_home)
    t0 = time.time()
    r1 = _judge(m, now=t0)
    assert r1["reverted"] is False and r1["net_usd_est"] > 0
    # The tripwire never scans transcripts itself, from here on.
    real_payoff = mod.subagent_cache_payoff
    inline_scan_banned = {"on": True}

    def _guarded(**kw):
        assert not inline_scan_banned["on"], (
            "the tripwire must read the cached verdict, not scan")
        return real_payoff(**kw)

    monkeypatch.setattr(mod, "subagent_cache_payoff", _guarded)
    # Same-day session starts: marker says judged, nothing re-read, no spawn.
    r2 = mod.evaluate_subagent_cache_tripwire(now=t0 + 3600)
    assert r2["reverted"] is False and r2["net_usd_est"] is None
    assert mod._spawn_log == []
    assert _marker(m)["state"] == "set"
    # A day later the cached verdict is stale: the scan is spawned detached and
    # the judgment waits for it (marker not stamped as judged).
    r3 = mod.evaluate_subagent_cache_tripwire(now=t0 + 90000)
    assert r3["reverted"] is False and r3["net_usd_est"] is None
    assert len(mod._spawn_log) == 1
    assert _marker(m)["judged_ts"] == pytest.approx(t0)
    # The scan lands; the next session start applies it.
    inline_scan_banned["on"] = False
    r4 = _judge(m, now=t0 + 90100)
    assert r4["reverted"] is False and r4["net_usd_est"] > 0
    assert _marker(m)["judged_ts"] == pytest.approx(t0 + 90100)


def test_tripwire_no_data_stamps_judgment(m, monkeypatch):
    mod, _settings, _home = m
    _enable_15d_ago(m)
    t0 = time.time()
    r1 = _judge(m, now=t0)
    assert "not enough data" in (r1.get("reason") or "")
    judged = _marker(m)["judged_ts"]
    assert judged == pytest.approx(t0)
    r2 = mod.evaluate_subagent_cache_tripwire(now=t0 + 3600)
    assert r2["reverted"] is False and r2["reason"] is None
    assert _marker(m)["judged_ts"] == judged  # judged once, not every session


def test_tripwire_ignores_when_user_set_the_key(m):
    mod, settings, _home = m
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(settings, data)
    _write_sidechain(_home, "neg.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "n1", 5000),
        _sidechain_record("2026-10-09T12:00:00Z", "n2", 5000),
    ])
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
    assert r["reverted"] is False
    assert _read(settings)[KEY] == "1h"


# ---------------------------------------------------------------------------
# SessionStart wiring: one notice, never repeated
# ---------------------------------------------------------------------------

def test_sessionstart_lines_fire_once(m):
    mod, settings, _home = m
    lines = mod._subagent_cache_session_start_lines()
    assert len(lines) == 1
    assert NOTICE_EXPECTED in lines[0]
    assert "Undo:" in lines[0]
    assert _read(settings)[KEY] == "1h"
    # Second session: silent no-op (marker checked BEFORE reading settings).
    lines2 = mod._subagent_cache_session_start_lines()
    assert lines2 == []


def test_sessionstart_lines_empty_when_off(m, monkeypatch):
    mod, settings, _home = m
    monkeypatch.setenv("TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H", "0")
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)


def test_sessionstart_revert_notice_once(m):
    mod, settings, _home = m
    _enable_15d_ago(m)
    _write_sidechain(_home, "neg.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "n1", 5000),
        _sidechain_record("2026-10-09T12:00:00Z", "n2", 5000),
    ])
    _pad(_home)
    mod.subagent_cache_scan_run(now=time.time(), since_ts=_set_ts(m))
    lines = mod._subagent_cache_session_start_lines()
    assert len(lines) == 1
    assert "removed" in lines[0] or "reverted" in lines[0]
    assert KEY not in _read(settings)
    assert mod._subagent_cache_session_start_lines() == []


# ---------------------------------------------------------------------------
# ensure-health integration: the notice reaches stdout as ONE systemMessage
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX-only: fake claude shim on PATH")
def test_ensure_health_emits_notice_once(tmp_path, monkeypatch):
    """Full subprocess: `measure.py ensure-health --once-mark` prints the
    one-time notice via the SessionStart systemMessage channel, and the
    second session start does not repeat it."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    _write_settings(claude_dir / "settings.json")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text(
        "#!/bin/sh\necho '2.1.250 (Claude Code)'\n", encoding="utf-8")
    (fake_bin / "claude").chmod(0o755)

    env = dict(os.environ)
    for var in ("TOKEN_OPTIMIZER_RUNTIME", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_CONTAINER_ID",
                "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
                "FORCE_PROMPT_CACHING_5M"):
        env.pop(var, None)
    # Always-on: this test is about the notice channel, not the evidence gate
    # (test_ensure_health_applies_a_cached_positive_verdict covers that).
    env["TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H"] = "1"
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path / "data")
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    def run_once():
        return subprocess.run(
            [sys.executable, str(MEASURE), "ensure-health", "--once-mark"],
            input=json.dumps({
                "cwd": str(tmp_path),
                "hook_event_name": "SessionStart",
                "session_id": "01subagentcachetest000000000",
                "source": "startup",
            }),
            text=True, capture_output=True, env=env, timeout=120,
        )

    first = run_once()
    assert first.returncode == 0, first.stderr[-2000:]
    assert json.loads((claude_dir / "settings.json").read_text())[KEY] == "1h"
    system_msgs = [
        json.loads(line).get("systemMessage", "")
        for line in first.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    notices = [s for s in system_msgs if NOTICE_EXPECTED in s]
    assert len(notices) == 1, f"expected exactly one notice, got {notices}"
    assert "Undo:" in notices[0]

    second = run_once()
    assert second.returncode == 0, second.stderr[-2000:]
    second_msgs = [
        json.loads(line).get("systemMessage", "")
        for line in second.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    assert not [s for s in second_msgs if NOTICE_EXPECTED in s], (
        "the one-time notice repeated on the second session start")


# ---------------------------------------------------------------------------
# Surfaces: doctor / quick / coach JSON carry the block; cleanup undoes it
# ---------------------------------------------------------------------------

def test_doctor_json_carries_subagent_cache_block(m, monkeypatch):
    mod, _settings, _home = m
    monkeypatch.setattr(mod, "subagent_cache_block",
                        lambda **kw: {"state": "set", "set_by": "token-optimizer"})
    mod.DASHBOARD_PATH = mod.DASHBOARD_PATH  # keep; doctor tolerates absence
    result = mod.doctor(as_json=True)
    assert result["subagent_cache"]["state"] == "set"


def test_quick_json_carries_subagent_cache_block(m, monkeypatch):
    mod, _settings, _home = m
    monkeypatch.setattr(mod, "subagent_cache_block",
                        lambda **kw: {"state": "off"})
    monkeypatch.setattr(mod, "measure_components", lambda: {})
    monkeypatch.setattr(mod, "calculate_totals", lambda c: {"estimated_total": 0})
    monkeypatch.setattr(mod, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(mod, "_estimate_quality_with_curve",
                        lambda *a, **k: (50.0, "generic-fill"))
    monkeypatch.setattr(mod, "_collect_trends_data", lambda **kw: {})
    monkeypatch.setattr(mod, "_auto_snapshot", lambda *a, **k: None)
    result = mod.quick_scan(as_json=True)
    assert result["subagent_cache"]["state"] == "off"


def test_coach_json_carries_subagent_cache_block(m, monkeypatch):
    mod, _settings, _home = m
    monkeypatch.setattr(mod, "generate_coach_data",
                        lambda **kw: {"health_score": 75})
    out, err = _capture(lambda: mod._coach_cli(["coach", "--json"]))
    data = json.loads(out)
    assert "subagent_cache" in data


def _capture(fn):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn()
    except SystemExit:
        pass
    return buf.getvalue(), ""


def test_cleanup_undo_removes_to_set_key(m, monkeypatch, capsys):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    monkeypatch.setattr(mod, "setup_daemon", lambda **kw: None)
    monkeypatch.setattr(mod, "PLIST_PATH", _home / "no.plist")
    monkeypatch.setitem(sys.modules, "install_reconcile",
                        SimpleNamespace(reconcile_uninstall=lambda *a, **kw: {}))
    mod.cleanup(dry_run=False)
    assert KEY not in _read(settings)
    assert _read(settings)["model"] == "opus"


def test_cleanup_dry_run_does_not_touch_settings(m, monkeypatch):
    mod, settings, _home = m
    mod.subagent_cache_enable()
    monkeypatch.setattr(mod, "setup_daemon", lambda **kw: None)
    monkeypatch.setattr(mod, "PLIST_PATH", _home / "no.plist")
    monkeypatch.setitem(sys.modules, "install_reconcile",
                        SimpleNamespace(reconcile_uninstall=lambda *a, **kw: {}))
    mod.cleanup(dry_run=True)
    assert _read(settings)[KEY] == "1h"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def test_cli_subcommand_registered(m):
    """`measure.py subagent-cache status --json` works end to end."""
    import io
    from contextlib import redirect_stdout
    mod, settings, _home = m
    mod.subagent_cache_enable()
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            mod._subagent_cache_cli(["subagent-cache", "status", "--json"])
        except SystemExit:
            pass
    data = json.loads(buf.getvalue())
    assert data["state"] == "set"


def test_subagent_cache_block_shape(m):
    mod, _settings, _home = m
    mod.subagent_cache_enable()
    b = mod.subagent_cache_block()
    for k in ("state", "set_by", "payoff"):
        assert k in b


# ===========================================================================
# Evidence-gated automatic path + cached verdict + detached background scan
# The automatic enable happens ONLY when the user's own last
# 30 days say it pays; the payoff scan never runs inside SessionStart.
# ===========================================================================

FORCE_ENV = "TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H"


def _gated(m, monkeypatch):
    """Drop the fixture's always-on switch so the evidence gate is live."""
    monkeypatch.delenv(FORCE_ENV, raising=False)
    mod, _s, _h = m
    return mod


def _verdict(m, *, requests=500, saved=2.0, premium=1.0, tokens=1_234_567,
             complete=True, age=60.0, since_ts=None, now=None):
    """Write a cached verdict the way the background scan would."""
    mod, _s, _h = m
    now = time.time() if now is None else now
    payoff = mod._subagent_cache_payoff_zero(30)
    payoff.update(
        subagent_requests=requests, savings_usd_est=saved,
        extra_write_cost_usd_est=premium, net_usd_est=saved - premium,
        within_agent_tokens=tokens, missed_read_tokens=tokens)
    rec = {"version": 1, "ts": now - age, "complete": complete,
           "since_ts": since_ts, "window_days": 30,
           "payoff": payoff if complete else None,
           "reason": None if complete else "time budget exceeded"}
    p = mod._subagent_cache_verdict_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _no_inline_scan(mod, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("session start must never run the payoff scan")
    monkeypatch.setattr(mod, "subagent_cache_payoff", _boom)


def test_gate_margin_constant_is_named():
    sys.path.insert(0, str(SCRIPTS))
    src = MEASURE.read_text(encoding="utf-8")
    assert "_SUBAGENT_CACHE_AUTO_ENABLE_MARGIN = 1.15" in src


def test_session_start_without_cache_spawns_one_scan_and_writes_nothing(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _s, settings, _h = m[0], m[1], m[2]
    before = settings.read_text(encoding="utf-8")
    _no_inline_scan(mod, monkeypatch)
    assert mod._subagent_cache_session_start_lines() == []
    assert settings.read_text(encoding="utf-8") == before
    assert len(mod._spawn_log) == 1
    argv, kw = mod._spawn_log[0]
    assert "subagent-cache" in argv and "scan" in argv
    assert Path(argv[1]).name == "measure.py"
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.DEVNULL
    assert kw["env"]["TOKEN_OPTIMIZER_RUNTIME"] == "claude"
    # A second session start while that scan is in flight: lock held, no second.
    assert mod._subagent_cache_session_start_lines() == []
    assert len(mod._spawn_log) == 1
    assert settings.read_text(encoding="utf-8") == before


def test_session_start_with_fresh_positive_verdict_only_advises(m, monkeypatch):
    """ADVISE-ONLY: a positive verdict is recorded and surfaced as a
    recommendation; session start NEVER writes the key itself."""
    mod = _gated(m, monkeypatch)
    settings = m[1]
    before = settings.read_text(encoding="utf-8")
    _no_inline_scan(mod, monkeypatch)
    _verdict(m, requests=500, saved=2.0, premium=1.0, tokens=1_234_567)
    assert mod._subagent_cache_session_start_lines() == []
    assert settings.read_text(encoding="utf-8") == before
    assert mod._spawn_log == []
    ad = _marker(m)["auto_decision"]
    assert ad["decision"] == "recommend"
    assert "pays" in ad["reason"]
    assert ad["ts"] > 0


def test_session_start_with_fresh_negative_verdict_does_not_write(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    before = settings.read_text(encoding="utf-8")
    _no_inline_scan(mod, monkeypatch)
    _verdict(m, requests=500, saved=0.40, premium=1.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert settings.read_text(encoding="utf-8") == before
    assert mod._spawn_log == []
    ad = _marker(m)["auto_decision"]
    assert ad["decision"] == "would-not-pay"
    assert ad["reason"] == "would not pay: saved $0.40 vs premium $1.00"
    assert ad["ts"] > 0


def test_thin_verdict_is_not_enough_data(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    before = settings.read_text(encoding="utf-8")
    n = mod._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS
    _verdict(m, requests=n - 1, saved=50.0, premium=1.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert settings.read_text(encoding="utf-8") == before
    ad = _marker(m)["auto_decision"]
    assert ad["decision"] == "not-enough-data"
    assert "not enough data" in ad["reason"]
    assert str(n) in ad["reason"]


def test_exactly_the_minimum_sample_counts(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _verdict(m, requests=mod._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS,
             saved=2.0, premium=1.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert _marker(m)["auto_decision"]["decision"] == "recommend"


def test_margin_is_one_point_one_five_inclusive(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _verdict(m, saved=1.14, premium=1.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert _marker(m)["auto_decision"]["decision"] == "would-not-pay"
    # A fresh verdict a day later lands exactly on the margin: still advise,
    # still no write.
    _verdict(m, saved=1.15, premium=1.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert _marker(m)["auto_decision"]["decision"] == "recommend"


def test_zero_saving_never_enables_even_with_zero_premium(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _verdict(m, saved=0.0, premium=0.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)


def test_stale_verdict_is_not_applied_and_respawns_scan(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _no_inline_scan(mod, monkeypatch)
    _verdict(m, saved=9.0, premium=1.0, age=2 * 86400)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert len(mod._spawn_log) == 1


def test_incomplete_verdict_is_no_verdict(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _verdict(m, complete=False)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    ad = _marker(m)["auto_decision"]
    assert ad["decision"] == "not-enough-data"
    assert mod._spawn_log == []   # fresh (but partial): retried tomorrow, not now


def test_decision_is_rejudged_at_most_once_a_day(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _verdict(m, saved=0.1, premium=1.0)
    t0 = time.time()
    mod._subagent_cache_session_start_lines(now=t0)
    first = _marker(m)["auto_decision"]
    mod._subagent_cache_session_start_lines(now=t0 + 3600)
    assert _marker(m)["auto_decision"] == first     # untouched, not rewritten
    # A newer verdict (the daily scan finished) is judged again.
    _verdict(m, saved=0.2, premium=1.0, now=t0 + 90000)
    mod._subagent_cache_session_start_lines(now=t0 + 90100)
    second = _marker(m)["auto_decision"]
    assert second["ts"] == pytest.approx(t0 + 90100)
    assert "$0.20" in second["reason"]


def test_force_on_env_enables_without_evidence_and_without_scan(m, monkeypatch):
    mod, settings, _h = m
    monkeypatch.setenv(FORCE_ENV, "1")
    _no_inline_scan(mod, monkeypatch)
    lines = mod._subagent_cache_session_start_lines()
    assert len(lines) == 1 and "subagent-cache disable" in lines[0]
    assert _read(settings)[KEY] == "1h"
    assert mod._spawn_log == []


def test_force_off_env_still_means_never(m, monkeypatch):
    mod, settings, _h = m
    monkeypatch.setenv(FORCE_ENV, "0")
    _verdict(m, saved=9.0, premium=1.0)
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert mod._spawn_log == []


def test_guards_still_win_over_a_positive_verdict(m, monkeypatch):
    """User-set value, env overrides, other settings files, version floor."""
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _verdict(m, saved=9.0, premium=1.0)
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "5m")
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert mod._spawn_log == [], "no scan for a user a guard already excludes"
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL")
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version", lambda: (2, 1, 100))
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    data = dict(USER_SETTINGS)
    data[KEY] = "5m"
    _write_settings(settings, data)
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version", lambda: (2, 1, 250))
    assert mod._subagent_cache_session_start_lines() == []
    assert _read(settings)[KEY] == "5m"


def test_version_probe_never_runs_on_the_advise_only_path(m, monkeypatch):
    """Advise-only: `claude --version` is only probed right before a real
    write, and session start never writes -- so it must never run there,
    whatever the verdict says."""
    mod = _gated(m, monkeypatch)
    calls = []
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version",
                        lambda: calls.append(1) or (2, 1, 250))
    mod._subagent_cache_session_start_lines()          # no verdict yet
    _verdict(m, saved=0.1, premium=1.0)                # verdict says no
    mod._subagent_cache_session_start_lines()
    _verdict(m, saved=9.0, premium=1.0)                # verdict says yes
    mod._subagent_cache_session_start_lines()
    assert calls == [], "claude --version must not run when nothing can be written"


def test_manual_enable_is_unconditional(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    settings = m[1]
    _verdict(m, saved=0.0, premium=5.0)
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "set" and r["changed"] is True
    assert _read(settings)[KEY] == "1h"


def test_manual_enable_cli_prints_estimate_first(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    _two_spawns(m[2])
    mod._subagent_cache_cli(["subagent-cache", "enable"])
    out = capsys.readouterr().out
    assert "ESTIMATE from your own transcripts" in out
    assert out.index("ESTIMATE") < out.index("[Token Optimizer] ")
    assert _read(m[1])[KEY] == "1h"


def test_manual_enable_cli_json_carries_the_estimate(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    mod._subagent_cache_cli(["subagent-cache", "enable", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "set"
    assert data["payoff"]["estimate"] is True


# --- the scan worker ------------------------------------------------------

def test_scan_run_writes_a_complete_verdict(m):
    mod, _s, home = m
    _two_spawns(home)
    rec = mod.subagent_cache_scan_run(now=time.time())
    assert rec["complete"] is True
    on_disk = json.loads(mod._subagent_cache_verdict_path().read_text("utf-8"))
    assert on_disk["complete"] is True and on_disk["since_ts"] is None
    assert on_disk["payoff"]["subagent_requests"] >= 2
    assert on_disk["ts"] > 0


def test_scan_run_partial_on_time_budget_is_no_verdict(m):
    mod, _s, home = m
    _two_spawns(home)
    rec = mod.subagent_cache_scan_run(now=time.time(), time_budget=-1.0)
    assert rec["complete"] is False
    assert rec["payoff"] is None
    assert "time budget" in rec["reason"]


def test_scan_run_partial_on_file_cap_is_no_verdict(m):
    mod, _s, home = m
    _two_spawns(home)
    _pad(home, n=3)
    rec = mod.subagent_cache_scan_run(now=time.time(), max_files=1)
    assert rec["complete"] is False and rec["payoff"] is None
    assert "file cap" in rec["reason"]


def test_scan_run_crash_still_leaves_a_dated_incomplete_verdict(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.setattr(mod, "subagent_cache_payoff",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    rec = mod.subagent_cache_scan_run(now=time.time())
    assert rec["complete"] is False
    assert mod._subagent_cache_verdict_path().exists()
    assert not mod._subagent_cache_scan_lock_path().exists(), "lock leaked"


def test_scan_lock_prevents_two_scans_at_once(m):
    mod, _s, home = m
    _two_spawns(home)
    token = mod._subagent_cache_scan_acquire_lock(time.time())
    assert token
    assert mod._subagent_cache_scan_acquire_lock(time.time()) is None
    # A scan started by hand while the lock is held backs off and writes nothing.
    assert mod.subagent_cache_scan_run(now=time.time()) is None
    assert not mod._subagent_cache_verdict_path().exists()
    # The spawner's child presents the token and runs, releasing the lock after.
    rec = mod.subagent_cache_scan_run(now=time.time(), token=token)
    assert rec["complete"] is True
    assert not mod._subagent_cache_scan_lock_path().exists()


def test_abandoned_scan_lock_is_reclaimed(m):
    mod, _s, _h = m
    now = time.time()
    token = mod._subagent_cache_scan_acquire_lock(now - 3600)
    assert token
    p = mod._subagent_cache_scan_lock_path()
    os.utime(p, (now - 3600, now - 3600))
    assert mod._subagent_cache_scan_acquire_lock(now)


def test_failed_spawn_releases_the_lock(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.setattr(mod, "spawn_detached", lambda argv, **kw: None)
    assert mod._subagent_cache_spawn_scan(now=time.time()) is False
    assert not mod._subagent_cache_scan_lock_path().exists()


def test_spawn_passes_the_tripwire_window(m):
    mod, _s, _h = m
    assert mod._subagent_cache_spawn_scan(now=time.time(), since_ts=1234.5)
    argv, kw = mod._spawn_log[0]
    assert argv[-2:] == ["--since", "1234.5"] or "--since" in argv
    assert kw["env"][mod._SUBAGENT_CACHE_SCAN_TOKEN_ENV]


def test_scan_cli_runs_the_worker(m, capsys):
    mod, _s, home = m
    _two_spawns(home)
    mod._subagent_cache_cli(["subagent-cache", "scan"])
    assert mod._subagent_cache_verdict_path().exists()


def test_scan_spawn_goes_through_spawn_detached_only():
    """No console window on Windows: the spawn helper owns the flags."""
    import ast
    tree = ast.parse(MEASURE.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_subagent_cache_spawn_scan")
    called = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "spawn_detached" in called
    assert not any(c.startswith("subprocess.") for c in called)


# --- the tripwire reads the same cached verdict ---------------------------

def _post_enable_verdict(m, **kw):
    return _verdict(m, since_ts=_set_ts(m), **kw)


def test_tripwire_uses_cached_verdict_not_inline_scan(m, monkeypatch):
    mod, settings, _h = m
    _enable_15d_ago(m)
    _no_inline_scan(mod, monkeypatch)
    _post_enable_verdict(m, requests=500, saved=0.1, premium=2.0)
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
    assert r["reverted"] is True
    assert KEY not in _read(settings)


def test_tripwire_without_cached_verdict_spawns_scan_and_waits(m, monkeypatch):
    mod, settings, _h = m
    _enable_15d_ago(m)
    _no_inline_scan(mod, monkeypatch)
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
    assert r["reverted"] is False
    assert _read(settings)[KEY] == "1h"
    assert len(mod._spawn_log) == 1
    assert "--since" in mod._spawn_log[0][0]
    assert "judged_ts" not in _marker(m), "a pending scan is not a judgment"


def test_tripwire_ignores_a_verdict_for_another_window(m, monkeypatch):
    mod, settings, _h = m
    _enable_15d_ago(m)
    _no_inline_scan(mod, monkeypatch)
    _verdict(m, requests=500, saved=0.0, premium=9.0, since_ts=None)
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
    assert r["reverted"] is False
    assert _read(settings)[KEY] == "1h"
    assert len(mod._spawn_log) == 1


# --- surfaces show the decision -------------------------------------------

def test_status_shows_auto_decision_and_reason(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _two_spawns(m[2])
    st = mod.subagent_cache_status()
    ad = st["auto_decision"]
    assert ad["decision"] in ("would-not-pay", "not-enough-data", "recommend")
    assert ad["reason"]


def test_status_auto_decision_reports_force_on(m, monkeypatch):
    mod, _s, _h = m
    st = mod.subagent_cache_status()
    assert st["auto_decision"]["decision"] == "forced-on"


def test_status_auto_decision_reports_force_off(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.setenv(FORCE_ENV, "0")
    assert mod.subagent_cache_status()["auto_decision"]["decision"] == "opted-out"


def test_status_text_prints_the_decision(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    _two_spawns(m[2])
    mod._subagent_cache_cli(["subagent-cache", "status"])
    out = capsys.readouterr().out
    assert "verdict:" in out


def test_block_reads_the_cache_and_never_scans(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _no_inline_scan(mod, monkeypatch)
    _verdict(m, requests=500, saved=0.4, premium=1.0)
    b = mod.subagent_cache_block()
    assert b["auto_decision"]["decision"] == "would-not-pay"
    assert b["payoff"]["subagent_requests"] == 500
    assert mod._spawn_log == [], "doctor/quick/coach never spawn a scan"


def test_block_without_cache_is_an_honest_pending(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _no_inline_scan(mod, monkeypatch)
    b = mod.subagent_cache_block()
    assert b["auto_decision"]["decision"] == "pending"
    assert b["payoff"]["subagent_requests"] == 0


# --- end to end through ensure-health -------------------------------------

def _advise_only_e2e_env(tmp_path):
    """A full ensure-health sandbox: temp HOME/CLAUDE_CONFIG_DIR/data dir, a
    fake `claude` reporting 2.1.250, and a fresh positive cached verdict."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    _write_settings(claude_dir / "settings.json")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text(
        "#!/bin/sh\necho '2.1.250 (Claude Code)'\n", encoding="utf-8")
    (fake_bin / "claude").chmod(0o755)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    payoff = {"subagent_requests": 900, "savings_usd_est": 3.0,
              "extra_write_cost_usd_est": 1.0, "net_usd_est": 2.0,
              "within_agent_tokens": 777000, "across_spawn_tokens": 0,
              "missed_read_tokens": 777000, "realized_read_tokens": 0}
    (data_dir / "subagent_cache_verdict.json").write_text(json.dumps({
        "version": 1, "ts": time.time() - 30, "complete": True,
        "since_ts": None, "window_days": 30, "payoff": payoff}))

    env = dict(os.environ)
    for var in ("TOKEN_OPTIMIZER_RUNTIME", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_CONTAINER_ID",
                "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
                "FORCE_PROMPT_CACHING_5M", FORCE_ENV):
        env.pop(var, None)
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(data_dir)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    return env, claude_dir


def _run_ensure_health(tmp_path, env):
    return subprocess.run(
        [sys.executable, str(MEASURE), "ensure-health", "--once-mark"],
        input=json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart",
                          "session_id": "01subagentgate0000000000000",
                          "source": "startup"}),
        text=True, capture_output=True, env=env, timeout=120)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX-only: fake claude shim on PATH")
def test_ensure_health_never_writes_the_key_on_the_default_path(tmp_path):
    """ADVISE-ONLY end to end: a fresh positive verdict + unset env means the
    SessionStart hook records the recommendation and writes NOTHING."""
    env, claude_dir = _advise_only_e2e_env(tmp_path)
    res = _run_ensure_health(tmp_path, env)
    assert res.returncode == 0, res.stderr[-2000:]
    assert KEY not in json.loads((claude_dir / "settings.json").read_text())


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX-only: fake claude shim on PATH")
def test_ensure_health_writes_only_with_the_force_env(tmp_path):
    """TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 is the ONLY automatic writer."""
    env, claude_dir = _advise_only_e2e_env(tmp_path)
    env[FORCE_ENV] = "1"
    res = _run_ensure_health(tmp_path, env)
    assert res.returncode == 0, res.stderr[-2000:]
    assert json.loads((claude_dir / "settings.json").read_text())[KEY] == "1h"
    msgs = [json.loads(line).get("systemMessage", "")
            for line in res.stdout.splitlines() if line.strip().startswith("{")]
    notices = [s for s in msgs if NOTICE_EXPECTED in s]
    assert len(notices) == 1, msgs


# ===========================================================================
# Advise-only: `disable` is final, the tripwire only rides the force
# env, an earlier auto-set key is never removed silently, and the verdict is
# a recommendation in status/doctor/quick/coach.
# ===========================================================================

def _set_marker(m, **kw):
    mod, _s, _h = m
    rec = {"state": "set", "set_ts": time.time(), "set_by": "token-optimizer"}
    rec.update(kw)
    mod._subagent_cache_write_marker(rec)
    return rec


def test_disable_is_final_removed_is_sticky_even_for_the_force_env(m):
    """`subagent-cache disable` is final: marker "removed" blocks EVERY
    automatic path, including the force-env opt-in."""
    mod, settings, _h = m          # the fixture forces TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1
    mod.subagent_cache_enable()
    assert _read(settings)[KEY] == "1h"
    r = mod.subagent_cache_disable()
    assert r["state"] == "removed" and r["changed"] is True
    assert _marker(m)["state"] == "removed"
    # The next session start (force env still on) must not re-set it.
    assert mod._subagent_cache_session_start_lines() == []
    assert KEY not in _read(settings)
    assert _marker(m)["state"] == "removed"
    # Only an explicit `enable` can bring it back.
    r2 = mod.subagent_cache_enable(automatic=False)
    assert r2["state"] == "set" and _read(settings)[KEY] == "1h"


def test_no_auto_revert_without_the_force_env(m, monkeypatch):
    """A key an earlier version auto-set is never removed silently: without
    the force env the 14-day tripwire does not run, so a negative post-enable
    window leaves the key alone and shows up as advice instead."""
    mod = _gated(m, monkeypatch)
    settings, home = m[1], m[2]
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(settings, data)
    mk = _set_marker(m, set_ts=time.time() - 15 * 86400)
    _loss_history(home)
    _pad(home)
    mod.subagent_cache_scan_run(now=time.time(), since_ts=mk["set_ts"])
    lines = mod._subagent_cache_session_start_lines()
    assert lines == []
    assert _read(settings)[KEY] == "1h"          # kept, not silently removed
    assert _marker(m)["state"] == "set"


def test_advise_tick_spawns_the_post_enable_scan_for_a_to_set_key(m, monkeypatch):
    """The "is costing / is paying" line for a TO-set key needs the post-enable
    window verdict; session start spawns that scan (with --since) when the
    cache has none."""
    mod = _gated(m, monkeypatch)
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(m[1], data)
    set_ts = time.time() - 1000
    _set_marker(m, set_ts=set_ts)
    assert mod._subagent_cache_session_start_lines() == []
    assert len(mod._spawn_log) == 1
    assert "--since" in mod._spawn_log[0][0]
    assert repr(float(set_ts)) in mod._spawn_log[0][0]


def test_advise_tick_spawns_the_plain_scan_for_a_user_set_1h_key(m, monkeypatch):
    """A user-set "1h" also gets judged (costing/paying direction): session
    start keeps the last-30-days verdict fresh for it."""
    mod = _gated(m, monkeypatch)
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(m[1], data)
    assert mod._subagent_cache_session_start_lines() == []
    assert len(mod._spawn_log) == 1
    assert "--since" not in mod._spawn_log[0][0]


def test_no_scan_for_a_declined_or_a_user_set_5m(m, monkeypatch):
    """Users who made their choice need no verdict work at session start."""
    mod = _gated(m, monkeypatch)
    mod._subagent_cache_write_marker({"state": "user-declined"})
    assert mod._subagent_cache_session_start_lines() == []
    assert mod._spawn_log == []
    data = dict(USER_SETTINGS)
    data[KEY] = "5m"
    _write_settings(m[1], data)
    mod._subagent_cache_write_marker({"state": "user-set", "set_by": "user"})
    assert mod._subagent_cache_session_start_lines() == []
    assert mod._spawn_log == []


# --- the recommendation line ------------------------------------------------

def test_recommendation_enable_when_the_history_pays(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    st = mod.subagent_cache_status(use_cache=True)
    rec = st["recommendation"]
    assert rec["action"] == "enable"
    assert "would have saved" in rec["line"]
    assert "$1.00" in rec["line"]                      # net = saved - premium
    assert "turn on:" in rec["line"] and "subagent-cache enable" in rec["line"]


def test_recommendation_disable_when_the_key_costs(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(m[1], data)
    set_ts = time.time() - 20 * 86400
    _set_marker(m, set_ts=set_ts)
    _verdict(m, requests=500, saved=0.3, premium=1.0, since_ts=set_ts)
    st = mod.subagent_cache_status(use_cache=True)
    rec = st["recommendation"]
    assert rec["action"] == "disable"
    assert "is costing" in rec["line"] and "$0.70" in rec["line"]
    assert "turn off:" in rec["line"] and "subagent-cache disable" in rec["line"]


def test_recommendation_paying_line_for_a_set_key(m, monkeypatch):
    """A "1h" key that pays gets the paying line, in both set_by directions."""
    mod = _gated(m, monkeypatch)
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(m[1], data)
    set_ts = time.time() - 20 * 86400
    _set_marker(m, set_ts=set_ts)
    _verdict(m, requests=500, saved=3.0, premium=1.0, since_ts=set_ts)
    rec = mod.subagent_cache_status(use_cache=True)["recommendation"]
    assert rec["action"] == "keep"
    assert "is paying" in rec["line"] and "turn off" not in rec["line"]


def test_recommendation_neutral_when_thin(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    _verdict(m, requests=50, saved=99.0, premium=1.0)
    rec = mod.subagent_cache_status(use_cache=True)["recommendation"]
    assert rec["action"] == "none"
    assert "not enough data" in rec["line"] and "200" in rec["line"]


def test_recommendation_pending_without_a_verdict(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    rec = mod.subagent_cache_status(use_cache=True)["recommendation"]
    assert rec["action"] == "none"
    assert "scan" in rec["line"].lower()


def test_recommendation_none_for_user_set_5m_and_opted_out(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    data = dict(USER_SETTINGS)
    data[KEY] = "5m"
    _write_settings(m[1], data)
    _verdict(m, requests=500, saved=9.0, premium=1.0)
    assert mod.subagent_cache_status(use_cache=True)["recommendation"] is None
    monkeypatch.setenv(FORCE_ENV, "0")
    assert mod.subagent_cache_status(use_cache=True)["recommendation"] is None


def test_recommendation_says_api_equivalent_on_subscription(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    monkeypatch.setattr(mod, "keepwarm_billing_mode", lambda *a, **k: "subscription")
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    rec = mod.subagent_cache_status(use_cache=True)["recommendation"]
    assert "API-equivalent" in rec["line"]


def test_status_text_prints_the_advice_line(m, monkeypatch, capsys):
    """The explicit `status` command scans live, so the advice comes from the
    live payoff -- stub it rather than the transcript history."""
    mod = _gated(m, monkeypatch)
    paying = dict(mod._subagent_cache_payoff_zero(30))
    paying.update({"subagent_requests": 500, "savings_usd_est": 2.0,
                   "extra_write_cost_usd_est": 1.0, "net_usd_est": 1.0,
                   "missed_read_tokens": 50000})
    monkeypatch.setattr(mod, "subagent_cache_payoff",
                        lambda **kw: paying)
    mod._subagent_cache_cli(["subagent-cache", "status"])
    out = capsys.readouterr().out
    assert "advice:" in out and "subagent-cache enable" in out


def test_quick_text_prints_only_actionable_advice(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    monkeypatch.setattr(mod, "measure_components", lambda: {})
    monkeypatch.setattr(mod, "calculate_totals", lambda c: {"estimated_total": 0})
    monkeypatch.setattr(mod, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(mod, "_estimate_quality_with_curve",
                        lambda *a, **k: (50.0, "generic-fill"))
    monkeypatch.setattr(mod, "_collect_trends_data", lambda **kw: {})
    monkeypatch.setattr(mod, "_auto_snapshot", lambda *a, **k: None)
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    mod.quick_scan()
    out = capsys.readouterr().out
    assert "subagent-cache enable" in out


def test_quick_text_silent_while_the_scan_is_pending(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    monkeypatch.setattr(mod, "measure_components", lambda: {})
    monkeypatch.setattr(mod, "calculate_totals", lambda c: {"estimated_total": 0})
    monkeypatch.setattr(mod, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(mod, "_estimate_quality_with_curve",
                        lambda *a, **k: (50.0, "generic-fill"))
    monkeypatch.setattr(mod, "_collect_trends_data", lambda **kw: {})
    monkeypatch.setattr(mod, "_auto_snapshot", lambda *a, **k: None)
    mod.quick_scan()
    out = capsys.readouterr().out
    assert "subagent-cache enable" not in out and "SUBAGENT CACHE" not in out


def test_doctor_prints_the_neutral_pending_line(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    mod.doctor()
    out = capsys.readouterr().out
    assert "Subagent cache" in out
    assert "no payoff verdict yet" in out or "background" in out


def test_doctor_prints_the_advice_line(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    mod.doctor()
    out = capsys.readouterr().out
    assert "subagent-cache enable" in out


def test_coach_json_carries_the_recommendation(m, monkeypatch):
    mod = _gated(m, monkeypatch)
    monkeypatch.setattr(mod, "generate_coach_data",
                        lambda **kw: {"health_score": 75})
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    out, _ = _capture(lambda: mod._coach_cli(["coach", "--json"]))
    data = json.loads(out)
    rec = data["subagent_cache"]["recommendation"]
    assert rec["action"] == "enable" and "subagent-cache enable" in rec["line"]


def test_coach_text_prints_the_advice_line(m, monkeypatch, capsys):
    mod = _gated(m, monkeypatch)
    monkeypatch.setattr(mod, "generate_coach_data", lambda **kw: {
        "health_score": 75,
        "snapshot": {"total_overhead": 0, "overhead_pct": 0,
                     "context_window": 200000, "usable_tokens": 0,
                     "skill_count": 0, "skill_tokens": 0,
                     "claude_md_tokens": 0, "mcp_server_count": 0,
                     "mcp_tokens": 0},
        "patterns_bad": [], "patterns_good": [], "questions": []})
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    mod._coach_cli(["coach"])
    out = capsys.readouterr().out
    assert "subagent-cache enable" in out


# ===========================================================================
# Settings-write hardening
# ===========================================================================

def test_non_dict_json_settings_is_a_clean_refusal(m):
    """`[1,2,3]` is valid JSON but not an object; enable must refuse
    cleanly (unknown-settings), not die at dict(data)."""
    mod, settings, _h = m
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "unknown-settings", r
    assert r["changed"] is False
    assert settings.read_text(encoding="utf-8") == "[1, 2, 3]"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX chmod semantics (Windows uses the read-only attribute)")
def test_readonly_settings_file_is_never_replaced(m):
    """a settings.json with no write bits (chmod 444 = user froze it)
    must be refused, not silently replaced via rename."""
    mod, settings, _h = m
    os.chmod(settings, 0o444)
    try:
        r = mod.subagent_cache_enable(automatic=False)
        assert r["state"] == "write-refused", r
        assert r["changed"] is False
        assert KEY not in _read(settings)
    finally:
        os.chmod(settings, 0o644)


def test_explicit_command_reclaims_a_released_cohort_lease(m):
    """an explicit `enable` must not lose to a herd writer's released
    lease tombstone (reuse_wall ~10s). The unflagged writer still yields."""
    mod, settings, _h = m
    now = time.time()
    lease = settings.parent / ".settings.lease"
    lease.write_text(json.dumps({
        "pid": os.getpid() + 424242,   # a different, long-gone writer
        "nonce": "ab" * 16,
        "released": 1,
        "reuse_wall": now + 10,
        "created_wall": now - 1,
        "expires_wall": now + 10,
    }), encoding="utf-8")
    payload = dict(_read(settings))
    payload["x-probe"] = 1
    assert mod._write_settings_atomic(payload) is False, (
        "the herd-throttled path must still defer to the tombstone")
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "set" and r["changed"] is True, r
    assert _read(settings)[KEY] == "1h"
    assert "x-probe" not in _read(settings)


def test_explicit_enable_creates_a_missing_settings_file(m):
    """`subagent-cache enable` on a machine with no settings.json
    creates it (allow_missing) and says so."""
    mod, settings, _h = m
    settings.unlink()
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "set" and r["changed"] is True, r
    assert r.get("created_settings") is True
    assert _read(settings)[KEY] == "1h"
    assert "creat" in (r.get("notice") or "").lower()


def test_status_hints_when_the_marker_is_unreadable(m):
    """a "1h" key whose marker is corrupt/missing may be an orphaned
    TO write -- `status` must say so instead of silently reporting user-set."""
    mod, settings, _h = m
    data = dict(USER_SETTINGS)
    data[KEY] = "1h"
    _write_settings(settings, data)
    mp = mod._subagent_cache_marker_path()
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("{ corrupt !!!", encoding="utf-8")
    st = mod.subagent_cache_status()
    assert st["state"] == "user-set"
    assert st.get("hint") and "marker" in st["hint"]
    assert "unreadable" in st["hint"] or "corrupt" in st["hint"]
    # Absent marker: weaker but still flagged (may be an old TO write).
    mp.unlink()
    st = mod.subagent_cache_status()
    assert st.get("hint") and "no token optimizer marker" in st["hint"].lower()


def test_bom_settings_file_reads_and_writes(m):
    """a UTF-8 BOM is tolerated (utf-8-sig); the file is rewritten
    without the BOM and every other key survives."""
    mod, settings, _h = m
    settings.write_bytes(b"\xef\xbb\xbf" + json.dumps(USER_SETTINGS).encode("utf-8"))
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "set", r
    assert _read(settings)[KEY] == "1h"
    assert _read(settings)["model"] == "opus"
    assert not settings.read_bytes().startswith(b"\xef\xbb\xbf")


def test_comment_settings_stay_unknown(m):
    """// comments are NOT documented as tolerated; the file stays
    'unknown' and is never written."""
    mod, settings, _h = m
    settings.write_text('{ "model": "opus", // frozen by hand\n}',
                        encoding="utf-8")
    r = mod.subagent_cache_enable(automatic=False)
    assert r["state"] == "unknown-settings", r
    assert "// frozen by hand" in settings.read_text(encoding="utf-8")
