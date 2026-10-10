#!/usr/bin/env python3
"""Regression tests for the subagent prompt-cache TTL feature (1h for subagents).

Contract (brief ttl.md, FACTS.md):
  * `measure.py subagent-cache status|enable|disable [--json]` sets
    `subagentPromptCacheTtl: "1h"` in the USER settings.json (CLAUDE_CONFIG_DIR
    aware) -- and never overrides a value the user set, never fights an env
    override, never touches anything when a managed/project/local file already
    sets the key, never sets on Claude Code < 2.1.243, and never writes on
    unknown-state (unreadable / missing) settings.
  * enable records a marker in TO's own data dir (timestamp + previous state);
    disable removes the key ONLY when the marker says TO set it and the value
    is still "1h"; a user who removes/changes the key afterwards is never
    re-set ("user-declined").
  * Opt-out env TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=0 (also false/off/no) blocks
    the automatic set and UNDOES a previous TO-set value.
  * Payoff check: from the user's own sidechain transcripts, count subagent 5m
    cache writes that followed a 5-60 min gap (would have been reads at 1h)
    versus all subagent cache writes (2x instead of 1.25x at 1h). Net estimate
    in tokens and dollars, labelled an estimate.
  * Tripwire: 14+ days of post-enable data with a negative net estimate ->
    the next SessionStart reverts (only when TO set it), records
    "auto-reverted", and prints one line saying so.
  * SessionStart wiring: enable runs off the ensure path, prints the one-line
    notice at most ONCE via the systemMessage channel.
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
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
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
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
    assert r["reverted"] is False, r
    assert r["net_usd_est"] > 0
    assert _read(settings)[KEY] == "1h"


def test_tripwire_minimum_sample_guard(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _loss_history(home)   # clearly negative, but only 2 subagent requests
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
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
    assert mod.evaluate_subagent_cache_tripwire(now=t0)["reverted"] is False
    _pad(home, n=mod._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS)
    # next day's re-judgment sees enough data
    r = mod.evaluate_subagent_cache_tripwire(now=t0 + 90000)
    assert r["reverted"] is True, r


def test_user_with_almost_no_subagents_is_never_reverted(m):
    mod, settings, home = m
    _enable_15d_ago(m)
    _write_sidechain(home, "one.jsonl", [
        _sidechain_record("2026-10-09T10:00:00Z", "z1", 0, cc1h=300000)])
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
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
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
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
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
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
    r = mod.evaluate_subagent_cache_tripwire(now=time.time())
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
    calls = []
    real_payoff = mod.subagent_cache_payoff
    monkeypatch.setattr(mod, "subagent_cache_payoff",
                        lambda **kw: calls.append(kw) or real_payoff(**kw))
    r1 = mod.evaluate_subagent_cache_tripwire(now=t0)
    assert r1["reverted"] is False and r1["net_usd_est"] > 0
    assert len(calls) == 1
    # Same-day session starts: marker says judged, no rescan.
    r2 = mod.evaluate_subagent_cache_tripwire(now=t0 + 3600)
    assert r2["reverted"] is False and r2["net_usd_est"] is None
    assert len(calls) == 1
    assert _marker(m)["state"] == "set"
    # A day later the verdict is re-judged (fresh evidence may have landed).
    r3 = mod.evaluate_subagent_cache_tripwire(now=t0 + 90000)
    assert r3["reverted"] is False and r3["net_usd_est"] > 0
    assert len(calls) == 2


def test_tripwire_no_data_stamps_judgment(m, monkeypatch):
    mod, _settings, _home = m
    _enable_15d_ago(m)
    t0 = time.time()
    calls = []
    monkeypatch.setattr(mod, "subagent_cache_payoff",
                        lambda **kw: calls.append(kw) or {
                            "subagent_5m_cache_writes": 0, "net_usd_est": 0.0})
    mod.evaluate_subagent_cache_tripwire(now=t0)
    assert len(calls) == 1
    mod.evaluate_subagent_cache_tripwire(now=t0 + 3600)
    assert len(calls) == 1  # no-data window judged once, not every session


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
                "FORCE_PROMPT_CACHING_5M",
                "TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H"):
        env.pop(var, None)
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
