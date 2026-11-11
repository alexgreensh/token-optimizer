"""The settings self-heal copies and restores ONLY what Token Optimizer owns.

A privacy audit found the heal snapshotting the whole settings.json (``env``
with API keys, ``apiKeyHelper``, other plugins' hooks, MCP server configs) and
silently restoring it. These tests pin the fix: the allowlist, the one-time
reduction of older full copies under settings_heal, the restore notice, and the
TOKEN_OPTIMIZER_SETTINGS_HEAL=0 kill switch.

Fixtures come from tests/test_settings_self_heal.py.

Run: python3 -m pytest tests/test_settings_owned_only.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_settings_self_heal import (  # noqa: E402,F401
    FOREIGN_HOOK, FULL_SETTINGS, NOT_OWNED, OWNED_HOOK, OWNED_PART, OWNED_STATUSLINE,
    SECRET_ENV, WIPE_RESIDUE, _read, _write, measure,
)

SECRETS = ("sk-ant-secret-1", "keep-me", "tvly-secret", "sk-live-should-never-be-copied", "get-key")


def _heal_text(heal_dir: Path, skip=("heal_log.jsonl",)) -> str:
    """Everything under settings_heal that could hold a settings copy."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(heal_dir.rglob("*"))
                     if p.is_file() and p.name not in skip)


def _compact_block(mod) -> str:
    bullets = sorted(mod._COMPACT_INSTR_BULLETS)[:2]
    return "\n".join([mod._COMPACT_INSTR_HEAD, *bullets, mod._COMPACT_INSTR_TAIL])


def _plant_full_copies(heal_dir: Path) -> None:
    """What an older version left behind: whole-file copies, env included."""
    (heal_dir / "backups").mkdir(parents=True, exist_ok=True)
    (heal_dir / "highwater.json").write_text(json.dumps(FULL_SETTINGS, indent=2), encoding="utf-8")
    (heal_dir / "highwater.meta.json").write_text(json.dumps(
        {"key_count": len(FULL_SETTINGS), "key_set": sorted(FULL_SETTINGS), "timestamp": "2026-01-01T00:00:00"}),
        encoding="utf-8")
    (heal_dir / "backups" / "settings.json").write_text(json.dumps(FULL_SETTINGS), encoding="utf-8")
    (heal_dir / "backups" / "settings.json-1").write_text(json.dumps(dict(WIPE_RESIDUE, env=SECRET_ENV)),
                                                          encoding="utf-8")
    for p in (heal_dir / "highwater.json", heal_dir / "backups" / "settings.json"):
        os.chmod(p, 0o644)


# --- snapshot -------------------------------------------------------------------

def test_no_secret_or_foreign_key_reaches_any_heal_file(measure):
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    _write(settings, dict(WIPE_RESIDUE, env=SECRET_ENV))
    mod._maybe_self_heal_settings()  # restore path writes a backup too

    text = _heal_text(heal_dir)
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked into settings_heal"
    for key in NOT_OWNED:
        assert f'"{key}"' not in text, f"{key} must not appear in any heal copy"


def test_secret_like_keys_never_enter_the_snapshot(measure):
    """Whatever the key is called, if Token Optimizer did not write it, it is not copied."""
    mod, settings, heal_dir = measure
    live = dict(FULL_SETTINGS, apiToken="t-1", awsSecretAccessKey="s-2",
                headersHelper="cmd --password hunter2", oauthAccount={"email": "x@y.z"})
    _write(settings, live)

    mod._maybe_self_heal_settings()

    assert _read(heal_dir / "highwater.json") == OWNED_PART


def test_foreign_statusline_is_not_snapshotted(measure):
    mod, settings, heal_dir = measure
    live = dict(FULL_SETTINGS, statusLine={"type": "command", "command": "node /home/me/other-tool/statusline.js"})
    _write(settings, live)

    mod._maybe_self_heal_settings()

    assert "statusLine" not in _read(heal_dir / "highwater.json")


def test_compact_instructions_snapshot_holds_only_our_block(measure):
    mod, settings, heal_dir = measure
    ours = _compact_block(mod)
    user_text = "MY PRIVATE RULES: never mention project Orion"
    _write(settings, dict(FULL_SETTINGS, compactInstructions=user_text + mod._COMPACT_INSTR_MARKER + ours))

    mod._maybe_self_heal_settings()

    hw = _read(heal_dir / "highwater.json")
    assert hw["compactInstructions"] == ours
    assert "Orion" not in (heal_dir / "highwater.json").read_text(encoding="utf-8")

    _write(settings, dict(FULL_SETTINGS, compactInstructions=user_text))
    mod._maybe_self_heal_settings()
    assert "Orion" not in _heal_text(heal_dir)


def test_compact_instructions_restored_when_wiped(measure):
    mod, settings, heal_dir = measure
    ours = _compact_block(mod)
    _write(settings, dict(FULL_SETTINGS, compactInstructions=ours))
    mod._maybe_self_heal_settings()
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    assert _read(settings)["compactInstructions"] == ours


def test_snapshot_refreshes_hook_commands_but_never_loses_keys(measure):
    """A hook command changes on upgrade: restoring a stale path would restore a
    broken hook. A sighting that lost an owned key must not shrink the snapshot."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    newer = {"type": "command",
             "command": OWNED_HOOK["command"].replace("collect --quiet", "session-end-flush --trigger end")}
    _write(settings, dict(FULL_SETTINGS, hooks={"SessionEnd": [{"hooks": [newer]}]}))
    mod._maybe_self_heal_settings()
    refreshed = dict(OWNED_PART, hooks={"SessionEnd": [{"hooks": [newer]}]})
    assert _read(heal_dir / "highwater.json") == refreshed

    _write(settings, {"model": "opus", "cleanupPeriodDays": 99999})  # lost statusLine + hooks
    mod._maybe_self_heal_settings()
    assert _read(heal_dir / "highwater.json") == refreshed


# --- restore --------------------------------------------------------------------

def test_restore_leaves_other_plugins_hooks_untouched(measure):
    """Live file has another plugin's hooks and nothing of ours: hooks is present,
    so nothing is merged into it and the foreign entries stay byte for byte."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    foreign = {"Stop": [{"hooks": [FOREIGN_HOOK]}]}
    _write(settings, {"cleanupPeriodDays": 99999, "hooks": foreign})

    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert healed["hooks"] == foreign
    assert healed["statusLine"] == OWNED_STATUSLINE


def test_restore_only_writes_owned_entries_from_an_old_full_snapshot(measure, monkeypatch):
    """Even if reducing the old snapshot failed, the read and the restore filter it."""
    mod, settings, heal_dir = measure
    _plant_full_copies(heal_dir)
    import settings_owned
    monkeypatch.setattr(settings_owned, "migrate_to_owned_only", lambda facade: {})
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert set(healed) <= set(settings_owned.OWNED_KEYS)
    assert healed["hooks"] == OWNED_PART["hooks"]
    assert FOREIGN_HOOK["command"] not in json.dumps(healed)


def test_restore_prints_one_stderr_line_and_records_it(measure, capsys):
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "Token Optimizer restored its own hooks after settings.json was reset" in lines[0]
    assert "TOKEN_OPTIMIZER_SETTINGS_HEAL=0" in lines[0]
    entry = [json.loads(ln) for ln in (heal_dir / "heal_log.jsonl").read_text().splitlines()
             if json.loads(ln).get("action") == "restore"][-1]
    assert entry["notice"].startswith("Token Optimizer restored its own hooks")


def test_restore_is_visible_in_health_and_doctor(measure):
    mod, settings, heal_dir = measure
    import heal_report
    mod._maybe_self_heal_settings()
    assert not any("restored" in ln for ln in heal_report.health_lines(mod))
    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()

    health = "\n".join(heal_report.health_lines(mod))
    assert "Token Optimizer restored its own hooks after settings.json was reset" in health
    rows = [r for r in heal_report.doctor_rows(mod) if r[1] == "Settings self-heal"]
    assert "restored its own hooks" in rows[0][2]


# --- migration ------------------------------------------------------------------

def test_old_full_copies_are_reduced_on_upgrade(measure):
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    live_before = settings.read_bytes()

    mod._maybe_self_heal_settings()

    assert _read(heal_dir / "highwater.json") == OWNED_PART
    meta = _read(heal_dir / "highwater.meta.json")
    assert meta["key_set"] == sorted(OWNED_PART) and meta["key_count"] == len(OWNED_PART)
    assert _read(heal_dir / "backups" / "settings.json") == OWNED_PART
    assert _read(heal_dir / "backups" / "settings.json-1") == WIPE_RESIDUE
    text = _heal_text(heal_dir)
    for secret in SECRETS:
        assert secret not in text
    if os.name != "nt":
        for p in (heal_dir / "highwater.json", heal_dir / "backups" / "settings.json"):
            assert (p.stat().st_mode & 0o777) == 0o600
    assert settings.read_bytes() == live_before, "the live settings.json is never touched by migration"
    log = [json.loads(ln) for ln in (heal_dir / "heal_log.jsonl").read_text().splitlines()]
    assert any(e["action"] == "migrate_owned_only" for e in log)


def test_migration_is_idempotent(measure):
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    import settings_owned
    assert settings_owned.migrate_to_owned_only(mod)
    assert settings_owned.migrate_to_owned_only(mod) == {}


def test_migration_leaves_unreadable_copies_and_files_outside_heal_dir_alone(measure):
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    broken = heal_dir / "backups" / "settings.json-2"
    broken.write_text("{ not json", encoding="utf-8")
    outside = heal_dir.parent / "somebody-elses.json"
    outside.write_text(json.dumps(FULL_SETTINGS), encoding="utf-8")

    mod._maybe_self_heal_settings()

    assert broken.read_text(encoding="utf-8") == "{ not json"
    assert json.loads(outside.read_text(encoding="utf-8")) == FULL_SETTINGS


# --- kill switch ----------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "off", "no", " 0 "])
def test_kill_switch_skips_snapshot_and_restore(measure, monkeypatch, value):
    mod, settings, heal_dir = measure
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", value)

    mod._maybe_self_heal_settings()
    assert not (heal_dir / "highwater.json").exists(), "no snapshot when the heal is off"
    assert not (heal_dir / "last_healthy.json").exists()

    monkeypatch.delenv("TOKEN_OPTIMIZER_SETTINGS_HEAL")
    mod._maybe_self_heal_settings()  # a snapshot exists now
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", value)
    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()
    assert _read(settings) == WIPE_RESIDUE, "no restore when the heal is off"


def test_kill_switch_unset_or_1_keeps_the_heal_on(measure, monkeypatch):
    mod, settings, heal_dir = measure
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", "1")
    mod._maybe_self_heal_settings()
    assert (heal_dir / "highwater.json").is_file()


def test_kill_switch_still_reduces_old_full_copies(measure, monkeypatch):
    """Reducing our own copies only removes data, so it runs even when the heal is off."""
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", "0")

    mod._maybe_self_heal_settings()

    for secret in SECRETS:
        assert secret not in _heal_text(heal_dir)
