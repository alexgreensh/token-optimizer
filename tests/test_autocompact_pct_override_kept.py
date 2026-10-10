#!/usr/bin/env python3
"""CLAUDE_AUTOCOMPACT_PCT_OVERRIDE must NEVER be auto-deleted from settings.

The variable is now DOCUMENTED (code.claude.com/docs/en/model-config:
"Set the percentage (1-100) of the compact window already used at which
auto-compaction runs; the variable can't raise the threshold"). The old
_auto_remove_bad_env_vars path deleted it from the user's settings.json on
every doctor / ensure-health run, destroying a documented user setting.

Contract pinned here:
  * the destructive helper and its BAD_ENV_VARS list no longer exist;
  * a settings.json that carries the variable survives every command that
    used to remove it BYTE FOR BYTE (doctor), and with the variable intact
    plus zero removed keys (ensure-health's settings-touching path);
  * doctor EXPLAINS the value instead (read-only), including a note when it
    is low enough to compact very early.

Run: python3 -m pytest tests/test_autocompact_pct_override_kept.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

USER_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "opus",
    "cleanupPeriodDays": 99999,
    "env": {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
        "MY_OTHER_KEY": "keep-me",
    },
    "permissions": {"allow": ["Bash(ls:*)"]},
    "statusLine": {"type": "command", "command": "node '/gone/statusline.js'"},
}


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "claude"
    home.mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    settings.write_text(json.dumps(USER_SETTINGS, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    # Keep the ambient environment from leaking a runtime identity in.
    for key in ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "TOKEN_OPTIMIZER_CONTEXT_SIZE"):
        monkeypatch.delenv(key, raising=False)
    yield mod, settings
    sys.modules.pop("measure", None)


def _bytes(settings_path: Path) -> bytes:
    return settings_path.read_bytes()


# ---------------------------------------------------------------------------
# The destructive helper must not exist at all.
# ---------------------------------------------------------------------------

def test_auto_remove_helper_is_gone(measure):
    mod, _ = measure
    assert not hasattr(mod, "_auto_remove_bad_env_vars"), (
        "_auto_remove_bad_env_vars must not exist: it deleted the documented "
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE setting from the user's settings.json"
    )
    assert not hasattr(mod, "BAD_ENV_VARS"), (
        "BAD_ENV_VARS must not exist; the auto-delete machinery is removed"
    )


# ---------------------------------------------------------------------------
# doctor: byte-for-byte survival + explain-only.
# ---------------------------------------------------------------------------

def test_doctor_keeps_settings_byte_for_byte(measure, capsys):
    mod, settings = measure
    before = _bytes(settings)
    mod.doctor(as_json=False)
    capsys.readouterr()
    assert _bytes(settings) == before, (
        "doctor modified settings.json while carrying the documented "
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE setting; it must be read-only there"
    )


def test_doctor_explains_override_instead_of_removing_it(measure, capsys):
    mod, settings = measure
    before = _bytes(settings)
    mod.doctor(as_json=True)
    out = capsys.readouterr().out
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in out, (
        "doctor must EXPLAIN the documented variable, not stay silent about it"
    )
    assert _bytes(settings) == before
    payload = json.loads(out)
    joined = json.dumps(payload)
    assert "70" in joined, "the explained value must include the setting's value"


def test_doctor_flags_a_very_low_override(measure, capsys):
    """A low percentage compacts very early; doctor must say so (read-only)."""
    mod, settings = measure
    settings.write_text(json.dumps(
        dict(USER_SETTINGS, env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "5"}), indent=2
    ) + "\n", encoding="utf-8")
    mod.doctor(as_json=False)
    out = capsys.readouterr().out
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in out
    assert "earlier" in out or "early" in out, (
        "a 5% override compacts at 5% of the window; doctor must warn about it"
    )


def test_doctor_reports_clean_env_when_no_override(measure, capsys):
    mod, settings = measure
    settings.write_text(json.dumps(
        dict(USER_SETTINGS, env={"MY_OTHER_KEY": "keep-me"}), indent=2) + "\n",
        encoding="utf-8")
    mod.doctor(as_json=False)
    out = capsys.readouterr().out
    assert _bytes(settings).decode() == json.dumps(
        dict(USER_SETTINGS, env={"MY_OTHER_KEY": "keep-me"}), indent=2) + "\n"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in out or "not set" in out


# ---------------------------------------------------------------------------
# ensure-health (SessionStart startup): the variable survives, nothing is
# dropped. Unrelated writers (daemon, hooks heal) are stubbed exactly like
# tests/test_sessionstart_context_tax.py does; settings-touching paths run real.
# ---------------------------------------------------------------------------

def _quiet_ensure_health(mod, monkeypatch):
    flags = {
        "v5_welcome_shown": True,
        "enterprise_consent_shown": True,
        "last_hook_heal_check": 0,
    }
    for key, value in flags.items():
        monkeypatch.setattr(mod, "_read_config_flag",
                            lambda k, d=None, _f=flags, _v=value: _f.get(k, d)
                            if False else _f.get(k, d))
    monkeypatch.setattr(mod, "_auto_capture_pristine_baseline", lambda: False)
    monkeypatch.setattr(mod, "DASHBOARD_PATH",
                        Path(str(mod.SNAPSHOT_DIR)) / "nope.html")
    monkeypatch.setattr(mod, "_ensure_dashboard_daemon", lambda *a, **k: "noop-healthy")
    monkeypatch.setattr(mod, "maybe_sweep_stale_leases", lambda: None)
    monkeypatch.setattr(mod, "_ensure_vscode_extension", lambda: None)
    monkeypatch.setattr(mod, "keepwarm_scheduler_repair", lambda: None)
    monkeypatch.setattr(mod, "_reconcile_sessionend_fossils",
                        lambda: {"rewritten": 0, "removed": 0, "stop_removed": 0})
    monkeypatch.setattr(mod, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(mod, "setup_all_hooks",
                        lambda dry_run=False, verbose=False: {"added": 0})
    monkeypatch.setattr(mod, "_fix_stale_settings_paths", lambda: 0)
    monkeypatch.setattr(mod, "_migrate_statusline_to_stable_path", lambda: False)
    monkeypatch.setattr(mod, "_heal_keepwarm_plist_path", lambda: False)
    monkeypatch.setattr(mod, "_heal_windows_console_flash", lambda: False)
    monkeypatch.setattr(mod, "_fix_malformed_hook_commands", lambda: 0)
    monkeypatch.setattr(mod, "_maybe_self_heal_settings", lambda: None)


def test_ensure_health_keeps_the_override(measure, monkeypatch, capsys):
    mod, settings = measure
    before = json.loads(settings.read_text(encoding="utf-8"))
    _quiet_ensure_health(mod, monkeypatch)
    mod.run_ensure_health()
    capsys.readouterr()
    after_raw = settings.read_text(encoding="utf-8")
    after = json.loads(after_raw)
    # The documented variable survives with its exact value.
    assert after.get("env", {}).get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == "70"
    # No user key was dropped: every original key/value still present.
    for key, value in before.items():
        assert after.get(key) == value, f"ensure-health changed key {key!r}"
    # Nothing claims a removal happened.
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "Removed CLAUDE" not in out
