"""The settings.json self-heal backs up everything EXCEPT secrets.

A privacy audit found the heal copying the whole settings.json (env with API
keys, MCP headers, token-bearing arguments) to a second place and restoring it
silently. These tests pin the fix: the backup is the file minus credentials,
a dropped value stays absent on restore, every restore says so (stderr, log,
Health) and names what it could not restore, older full copies are scrubbed on
upgrade, and TOKEN_OPTIMIZER_SETTINGS_HEAL=0 turns it all off.

Fixtures come from tests/test_settings_self_heal.py.

Run: python3 -m pytest tests/test_settings_backup.py -q
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"))

from test_settings_self_heal import WIPE_RESIDUE, _read, _write, measure  # noqa: E402,F401

ANTHROPIC = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIj"
GITHUB = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
AWS = "AKIAIOSFODNN7EXAMPLE"
PLAIN = "plainsecret99"  # no detector shape: only its flag name gives it away
TOKEN_URL = "https://mcp.example.com/sse?token=abcd1234efgh5678"
SECRETS = (ANTHROPIC, GITHUB, AWS, PLAIN, "abcd1234efgh5678", "keep-me", "hunter2", "tvly-live-value",
           "get-key-helper", "creds-export", "otel-helper")

HOOK_OK = {"type": "command", "command": "python3 /opt/mytool/notify.py --done"}
HOOK_SECRET = {"type": "command", "command": "curl -H 'Authorization: Bearer %s' https://x.example.com" % GITHUB}

SECRET_SETTINGS = {
    "cleanupPeriodDays": 99999,
    "statusLine": {"type": "command", "command": "node '/u/.claude/token-optimizer/statusline.js'"},
    "env": {"ANTHROPIC_API_KEY": ANTHROPIC, "MY_KEY": "keep-me"},
    "apiKeyHelper": "get-key-helper",
    "awsAuthRefresh": "aws sso login",
    "awsCredentialExport": "creds-export",
    "otelHeadersHelper": "otel-helper",
    "myServiceToken": "tvly-live-value",
    "oauthAccount": {"email": "x@y.z"},
    "permissions": {"allow": ["Bash(git:*)", "Bash(curl -H 'Authorization: Bearer %s' x)" % GITHUB],
                    "deny": ["Read(./.env)"]},
    "model": "opus",
    "enabledPlugins": {"token-optimizer@alexgreensh-token-optimizer": True},
    "extraKnownMarketplaces": {"alexgreensh-token-optimizer": {"source": {"source": "github", "repo": "a/b"}}},
    "hooks": {"Stop": [{"hooks": [HOOK_OK, HOOK_SECRET]}], "SessionEnd": [{"hooks": [HOOK_SECRET]}]},
    "mcpServers": {
        "tavily": {"type": "stdio", "command": "npx",
                   "args": ["-y", "tavily-mcp", "--api-key", PLAIN],
                   "env": {"TAVILY_API_KEY": "tvly-live-value"}, "headers": {"Authorization": "Bearer hunter2"}},
        "remote": {"type": "sse", "url": TOKEN_URL},
        "docs": {"type": "http", "url": "https://docs.example.com/mcp"},
    },
    "_disabledMcpServers": {"gh": {"command": "gh-mcp", "args": ["--token=%s" % GITHUB], "env": {"GH": GITHUB}}},
}


def _heal_text(heal_dir: Path, skip=("heal_log.jsonl",)) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(heal_dir.rglob("*"))
                     if p.is_file() and p.name not in skip)


def _assert_no_secrets(text: str):
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked"
    assert "[CREDENTIAL REDACTED" not in text, "a placeholder could be restored as a real value"


def _plant_full_copies(heal_dir: Path) -> None:
    """What the previous version left behind: whole-file copies, secrets included."""
    (heal_dir / "backups").mkdir(parents=True, exist_ok=True)
    (heal_dir / "highwater.json").write_text(json.dumps(SECRET_SETTINGS, indent=2), encoding="utf-8")
    (heal_dir / "highwater.meta.json").write_text(json.dumps(
        {"key_count": len(SECRET_SETTINGS), "key_set": sorted(SECRET_SETTINGS), "timestamp": "2026-01-01T00:00:00"}),
        encoding="utf-8")
    (heal_dir / "backups" / "settings.json").write_text(json.dumps(SECRET_SETTINGS), encoding="utf-8")
    (heal_dir / "backups" / "settings.json-1").write_text(json.dumps(dict(WIPE_RESIDUE, env=SECRET_SETTINGS["env"])),
                                                          encoding="utf-8")
    for p in (heal_dir / "highwater.json", heal_dir / "backups" / "settings.json"):
        os.chmod(p, 0o644)


@pytest.fixture()
def secret_live(measure):
    mod, settings, heal_dir = measure
    _write(settings, SECRET_SETTINGS)
    return mod, settings, heal_dir


# --- the scrub ---------------------------------------------------------------------

def test_scrub_drops_credential_keys_and_values_keeps_the_rest():
    import settings_backup as sb
    before = copy.deepcopy(SECRET_SETTINGS)
    clean, stats = sb.scrub(SECRET_SETTINGS)

    assert SECRET_SETTINGS == before, "scrub must not mutate its input"
    for key in ("env", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper",
                "myServiceToken", "oauthAccount"):
        assert key not in clean, f"{key} must not be backed up"
    assert sorted(stats.withheld) == sorted(["env", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport",
                                             "otelHeadersHelper", "myServiceToken", "oauthAccount"])
    for key in ("cleanupPeriodDays", "statusLine", "permissions", "model", "enabledPlugins",
                "extraKnownMarketplaces", "hooks", "mcpServers", "_disabledMcpServers"):
        assert key in clean, f"{key} is not a secret and must be backed up"
    _assert_no_secrets(json.dumps(clean))
    assert clean["permissions"] == {"allow": ["Bash(git:*)"], "deny": ["Read(./.env)"]}
    assert clean["hooks"] == {"Stop": [{"hooks": [HOOK_OK]}]}, "a flagged command drops its entry, not its neighbours"
    assert sorted(stats.partial) == ["_disabledMcpServers", "hooks", "mcpServers", "permissions"]


def test_mcp_servers_keep_structure_but_lose_env_headers_and_token_args():
    import settings_backup as sb
    servers = sb.scrub(SECRET_SETTINGS)[0]["mcpServers"]
    assert servers["tavily"] == {"type": "stdio", "command": "npx", "args": ["-y", "tavily-mcp"]}
    assert servers["remote"] == {"type": "sse"}, "a url carrying a credential is dropped"
    assert servers["docs"] == {"type": "http", "url": "https://docs.example.com/mcp"}
    assert sb.scrub(SECRET_SETTINGS)[0]["_disabledMcpServers"] == {"gh": {"command": "gh-mcp", "args": []}}


def test_a_broken_custom_pattern_file_does_not_switch_the_check_off(monkeypatch):
    import credential_patterns as cp
    import settings_backup as sb

    def boom(*_a, **_k):
        raise cp.RedactionConfigError("bad custom patterns")
    monkeypatch.setattr(cp, "scan_for_credentials", boom)
    monkeypatch.setattr(cp, "redact_credentials", boom)
    assert sb._flagged(ANTHROPIC) is True
    assert sb._flagged("Bash(git:*)") is False


# --- snapshot -------------------------------------------------------------------------

def test_snapshot_holds_everything_except_secrets(secret_live):
    mod, settings, heal_dir = secret_live

    mod._maybe_self_heal_settings()

    highwater = _read(heal_dir / "highwater.json")
    import settings_backup as sb
    assert highwater == sb.scrub(SECRET_SETTINGS)[0]
    assert highwater["permissions"] and highwater["model"] == "opus" and highwater["hooks"] and highwater["mcpServers"]
    meta = _read(heal_dir / "highwater.meta.json")
    assert "env" in meta["withheld_keys"] and "mcpServers" in meta["scrubbed_in"]
    _assert_no_secrets(_heal_text(heal_dir))


def test_no_secret_reaches_any_heal_file_after_a_restore(secret_live):
    mod, settings, heal_dir = secret_live
    mod._maybe_self_heal_settings()
    _write(settings, dict(WIPE_RESIDUE, env=SECRET_SETTINGS["env"]))
    mod._maybe_self_heal_settings()

    _assert_no_secrets(_heal_text(heal_dir))
    log = (heal_dir / "heal_log.jsonl").read_text(encoding="utf-8")
    _assert_no_secrets(log)


def test_snapshot_refreshes_stale_hook_commands_but_never_shrinks(secret_live):
    mod, settings, heal_dir = secret_live
    mod._maybe_self_heal_settings()
    newer = dict(SECRET_SETTINGS, hooks={"Stop": [{"hooks": [dict(HOOK_OK, command="python3 /opt/mytool/v2.py")]}]})
    _write(settings, newer)
    mod._maybe_self_heal_settings()
    assert _read(heal_dir / "highwater.json")["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("v2.py")

    _write(settings, {"model": "sonnet", "cleanupPeriodDays": 99999})  # a sighting that lost keys
    mod._maybe_self_heal_settings()
    assert "permissions" in _read(heal_dir / "highwater.json")


# --- restore ----------------------------------------------------------------------------

def test_wipe_restores_the_non_secret_keys_and_never_a_scrubbed_value(secret_live):
    mod, settings, heal_dir = secret_live
    mod._maybe_self_heal_settings()
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert healed["model"] == "opus"
    assert healed["permissions"] == {"allow": ["Bash(git:*)"], "deny": ["Read(./.env)"]}
    assert healed["hooks"] == {"Stop": [{"hooks": [HOOK_OK]}]}
    assert healed["mcpServers"]["docs"] == {"type": "http", "url": "https://docs.example.com/mcp"}
    assert healed["mcpServers"]["tavily"] == {"type": "stdio", "command": "npx", "args": ["-y", "tavily-mcp"]}
    assert "enabledPlugins" in healed and "extraKnownMarketplaces" in healed
    for key in ("env", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper", "myServiceToken"):
        assert key not in healed, f"{key} must stay absent"
    _assert_no_secrets(json.dumps(healed))


def test_restore_notice_names_what_could_not_be_restored(secret_live, capsys):
    mod, settings, heal_dir = secret_live
    mod._maybe_self_heal_settings()
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 1, "exactly one stderr line"
    assert "env" in lines[0] and "not restored: Token Optimizer never stores secrets" in lines[0]
    assert "Secret values inside" in lines[0] and "mcpServers" in lines[0]
    assert "TOKEN_OPTIMIZER_SETTINGS_HEAL=0" in lines[0]
    entry = [json.loads(ln) for ln in (heal_dir / "heal_log.jsonl").read_text().splitlines()
             if json.loads(ln).get("action") == "restore"][-1]
    assert entry["notice"] in lines[0]


def test_every_restore_shows_in_health_and_doctor(secret_live):
    mod, settings, heal_dir = secret_live
    import heal_report
    mod._maybe_self_heal_settings()
    assert not any("restored" in ln for ln in heal_report.health_lines(mod))
    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()

    health = "\n".join(heal_report.health_lines(mod))
    assert "Token Optimizer restored its backup" in health and "never stores secrets" in health
    rows = [r for r in heal_report.doctor_rows(mod) if r[1] == "Settings self-heal"]
    assert "never stores secrets" in rows[0][2]


def test_pre_restore_backup_is_secret_free_and_private(secret_live):
    mod, settings, heal_dir = secret_live
    mod._maybe_self_heal_settings()
    _write(settings, dict(WIPE_RESIDUE, env=SECRET_SETTINGS["env"]))

    mod._maybe_self_heal_settings()

    backups = list((heal_dir / "backups").glob("settings*.json"))
    assert backups
    assert _read(backups[0]) == WIPE_RESIDUE
    _assert_no_secrets(backups[0].read_text(encoding="utf-8"))
    if os.name != "nt":
        assert (backups[0].stat().st_mode & 0o777) == 0o600


def test_restore_from_an_old_full_snapshot_is_scrubbed_even_if_migration_failed(secret_live, monkeypatch):
    mod, settings, heal_dir = secret_live
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    import settings_backup
    monkeypatch.setattr(settings_backup, "migrate_to_secret_free", lambda facade: {})
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert "env" not in healed and "apiKeyHelper" not in healed
    _assert_no_secrets(json.dumps(healed))
    assert healed["model"] == "opus"


# --- migration -----------------------------------------------------------------------------

def test_old_full_copies_are_scrubbed_on_upgrade(measure):
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    live_before = settings.read_bytes()
    import settings_backup as sb

    mod._maybe_self_heal_settings()

    assert _read(heal_dir / "highwater.json") == sb.scrub(SECRET_SETTINGS)[0]
    assert _read(heal_dir / "backups" / "settings.json") == sb.scrub(SECRET_SETTINGS)[0]
    assert _read(heal_dir / "backups" / "settings.json-1") == WIPE_RESIDUE
    meta = _read(heal_dir / "highwater.meta.json")
    assert "env" in meta["withheld_keys"] and meta["timestamp"] == "2026-01-01T00:00:00"
    _assert_no_secrets(_heal_text(heal_dir))
    if os.name != "nt":
        for p in (heal_dir / "highwater.json", heal_dir / "backups" / "settings.json"):
            assert (p.stat().st_mode & 0o777) == 0o600
    assert settings.read_bytes() == live_before, "the live settings.json is never touched by migration"
    log = [json.loads(ln) for ln in (heal_dir / "heal_log.jsonl").read_text().splitlines()]
    assert any(e["action"] == "migrate_secret_free" for e in log)


def test_migration_is_idempotent(measure):
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    import settings_backup as sb
    assert sb.migrate_to_secret_free(mod)
    assert sb.migrate_to_secret_free(mod) == {}


def test_migration_leaves_unreadable_copies_and_files_outside_heal_dir_alone(measure):
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    broken = heal_dir / "backups" / "settings.json-2"
    broken.write_text("{ not json", encoding="utf-8")
    outside = heal_dir.parent / "somebody-elses.json"
    outside.write_text(json.dumps(SECRET_SETTINGS), encoding="utf-8")

    mod._maybe_self_heal_settings()

    assert broken.read_text(encoding="utf-8") == "{ not json"
    assert json.loads(outside.read_text(encoding="utf-8")) == SECRET_SETTINGS


# --- kill switch ----------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "off", "no", " 0 "])
def test_kill_switch_skips_snapshot_and_restore(secret_live, monkeypatch, value):
    mod, settings, heal_dir = secret_live
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", value)

    mod._maybe_self_heal_settings()
    assert not (heal_dir / "highwater.json").exists(), "no backup when the heal is off"
    assert not (heal_dir / "last_healthy.json").exists()

    monkeypatch.delenv("TOKEN_OPTIMIZER_SETTINGS_HEAL")
    mod._maybe_self_heal_settings()  # a snapshot exists now
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", value)
    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()
    assert _read(settings) == WIPE_RESIDUE, "no restore when the heal is off"


def test_kill_switch_1_keeps_the_heal_on(secret_live, monkeypatch):
    mod, settings, heal_dir = secret_live
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", "1")
    mod._maybe_self_heal_settings()
    assert (heal_dir / "highwater.json").is_file()


def test_kill_switch_still_scrubs_old_full_copies(measure, monkeypatch):
    """Scrubbing our own copies only removes data, so it runs even when the heal is off."""
    mod, settings, heal_dir = measure
    heal_dir.mkdir(parents=True, exist_ok=True)
    _plant_full_copies(heal_dir)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SETTINGS_HEAL", "0")

    mod._maybe_self_heal_settings()

    _assert_no_secrets(_heal_text(heal_dir))
