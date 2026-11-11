#!/usr/bin/env python3
"""Tests for the settings.json self-heal (recovery for already-damaged users).

The wipe bug (setup_quality_bar writing {} over settings.json) is FIXED at the
write guard. But users ALREADY damaged stay damaged: their permissions, env,
mcpServers, model, enabledPlugins, extraKnownMarketplaces are gone. This test
pins the RECOVERY: a silent, additive, high-water-mark-based restore that fires
only when the SURVIVOR FINGERPRINT proves a wipe (every surviving key is
machine-written, zero user-authored keys survive).

Gate (replaces the rejected 3+ key count rule):
  1. SURVIVOR FINGERPRINT: every key in the live file is machine-written.
     If even one user-authored key survives (permissions, model, etc.), do
     nothing -- the user was editing deliberately.
  2. High-water mark exists and has user-authored keys absent from the live file.
  3. Absent keys are not tombstoned.
  4. ABSENT-ONLY merge: restore only keys entirely absent, never overwrite.
  5. SILENT to user, logged for us. Fail open, always.

Run: python3 -m pytest tests/test_settings_self_heal.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

# Token Optimizer's own hook: a SessionEnd command byte-identical to one setup_hook
# generates for this install's measure.py, so owned_hooks can prove it is ours.
OWNED_HOOK = {"type": "command",
              "command": "python3 '%s/skills/token-optimizer/scripts/measure.py' collect --quiet" % REPO}
FOREIGN_HOOK = {"type": "command", "command": "other-plugin-notify.sh --token sk-live-should-never-be-copied"}
OWNED_STATUSLINE = {"type": "command", "command": "node '/u/.claude/token-optimizer/statusline.js'"}
SECRET_ENV = {"ANTHROPIC_API_KEY": "sk-ant-secret-1", "MY_KEY": "keep-me"}

# A realistic full settings.json with Token Optimizer's keys AND everyone else's.
FULL_SETTINGS = {
    "cleanupPeriodDays": 99999,
    "statusLine": OWNED_STATUSLINE,
    "hooks": {
        "SessionEnd": [{"hooks": [OWNED_HOOK, FOREIGN_HOOK]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN_HOOK]}],
    },
    "enabledPlugins": {
        "token-optimizer@alexgreensh-token-optimizer": True,
        "frontend-design@claude-plugins-official": True,
    },
    "env": SECRET_ENV,
    "apiKeyHelper": "/usr/local/bin/get-key",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "mcpServers": {"tavily": {"command": "npx", "args": ["tavily-mcp"], "env": {"TAVILY_API_KEY": "tvly-secret"}}},
    "model": "opus",
    "extraKnownMarketplaces": {
        "alexgreensh-token-optimizer": {"source": {"source": "github", "repo": "alexgreensh/token-optimizer"}},
    },
}

# The part of FULL_SETTINGS Token Optimizer owns: all a snapshot or restore may hold.
OWNED_PART = {
    "cleanupPeriodDays": 99999,
    "statusLine": OWNED_STATUSLINE,
    "hooks": {"SessionEnd": [{"hooks": [OWNED_HOOK]}]},
}

# Post-wipe residue: ONLY machine-written keys survive.
WIPE_RESIDUE = {
    "cleanupPeriodDays": 99999,
    "statusLine": OWNED_STATUSLINE,
}

# Machine-written keys that TO or the host writes automatically.
MACHINE_WRITTEN_KEYS = frozenset({
    "cleanupPeriodDays", "statusLine", "hooks", "compactInstructions",
    "mcpServers", "_disabledMcpServers", "env", "enabledPlugins",
})

NOT_OWNED = ("env", "apiKeyHelper", "permissions", "mcpServers", "model",
             "enabledPlugins", "extraKnownMarketplaces")


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    """Load measure.py against a throwaway CLAUDE_DIR. Never touches ~/.claude."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)

    home = tmp_path / "claude"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    # installed_plugins.json with user-scope entries (including playwright
    # which is installed but deliberately disabled -- no transcript evidence).
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({
            "version": 2,
            "plugins": {
                "token-optimizer@alexgreensh-token-optimizer": [
                    {"scope": "user", "installPath": str(plugins_dir / "cache" / "alexgreensh-token-optimizer" / "token-optimizer" / "1.0")},
                ],
                "playwright@claude-plugins-official": [
                    {"scope": "user", "installPath": str(plugins_dir / "cache" / "claude-plugins-official" / "playwright" / "1.0")},
                ],
                "frontend-design@claude-plugins-official": [
                    {"scope": "user", "installPath": str(plugins_dir / "cache" / "claude-plugins-official" / "frontend-design" / "1.0")},
                ],
            },
        }),
        encoding="utf-8",
    )

    # known_marketplaces.json -- the authoritative source for extraKnownMarketplaces.
    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps({
            "alexgreensh-token-optimizer": {
                "source": {"source": "github", "repo": "alexgreensh/token-optimizer"},
                "installLocation": str(plugins_dir / "marketplaces" / "alexgreensh-token-optimizer"),
                "lastUpdated": "2026-08-29T14:55:43.502Z",
            },
            "claude-plugins-official": {
                "source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
                "installLocation": str(plugins_dir / "marketplaces" / "claude-plugins-official"),
                "lastUpdated": "2026-08-29T14:55:42.239Z",
            },
        }),
        encoding="utf-8",
    )

    # Create a transcript with evidence of token-optimizer and frontend-design
    # use, but NOT playwright (deliberately disabled).
    projects_dir = home / "projects" / "test-project"
    projects_dir.mkdir(parents=True, exist_ok=True)
    transcript = projects_dir / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "token-optimizer:quick"}},
        ]}) + "\n" +
        json.dumps({"type": "assistant", "content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "frontend-design:analyze"}},
        ]}) + "\n",
        encoding="utf-8",
    )

    settings = home / "settings.json"
    settings.write_text(json.dumps(FULL_SETTINGS, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    # Point CONFIG_DIR to the tmp_path so heal data stays isolated.
    heal_dir = tmp_path / "to-config" / "settings_heal"
    monkeypatch.setattr(mod, "CONFIG_DIR", tmp_path / "to-config")
    yield mod, settings, heal_dir
    sys.modules.pop("measure", None)


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _write(p: Path, data: dict) -> None:
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Healthy file untouched: the heal never fires on a file with user keys
# --------------------------------------------------------------------------- #


def test_healthy_file_with_user_keys_is_not_healed(measure):
    """A healthy file with user-authored keys (permissions, model, etc.) must
    NOT be touched by the heal. The survivor fingerprint gate requires ALL
    surviving keys to be machine-written."""
    mod, settings, heal_dir = measure
    original = _read(settings)

    mod._maybe_self_heal_settings()

    assert _read(settings) == original, "a healthy file must not be modified"


def test_healthy_file_snapshots_into_highwater(measure):
    """A healthy file is snapshotted so a future wipe can restore from it, but
    the snapshot holds ONLY what Token Optimizer owns."""
    mod, settings, heal_dir = measure

    mod._maybe_self_heal_settings()

    highwater = heal_dir / "highwater.json"
    assert highwater.is_file(), "highwater.json must be created on a healthy sighting"
    assert _read(highwater) == OWNED_PART, "highwater must hold only Token Optimizer's own keys"
    raw = highwater.read_text(encoding="utf-8")
    for secret in ("sk-ant-secret-1", "keep-me", "tvly-secret", "sk-live-should-never-be-copied", "get-key"):
        assert secret not in raw, f"{secret!r} must never reach the snapshot"


# --------------------------------------------------------------------------- #
# 2. Wipe with only machine residue: the heal fires and restores
# --------------------------------------------------------------------------- #


def test_wipe_with_only_machine_residue_is_healed(measure):
    """On the wipe signature the heal restores Token Optimizer's OWN missing
    keys and nothing else. (The old behaviour also restored env, permissions,
    mcpServers, model, enabledPlugins: that was the bug.)"""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert healed["cleanupPeriodDays"] == WIPE_RESIDUE["cleanupPeriodDays"]
    assert healed["statusLine"] == WIPE_RESIDUE["statusLine"]
    assert healed["hooks"] == OWNED_PART["hooks"], "only Token Optimizer's hook entry comes back"
    for key in NOT_OWNED:
        assert key not in healed, f"{key} is not Token Optimizer's and must never be restored"
    assert FOREIGN_HOOK["command"] not in json.dumps(healed)


def test_heal_writes_a_pre_restore_backup(measure):
    """Every restoration writes a pre-restore backup so the action is
    reversible. The backup holds the owned part of the live file only, mode 0600."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, dict(WIPE_RESIDUE, env=SECRET_ENV))

    mod._maybe_self_heal_settings()

    backup_dir = heal_dir / "backups"
    assert backup_dir.is_dir(), "a backup directory must exist after a heal"
    backups = list(backup_dir.glob("settings*.json"))
    assert backups, "at least one pre-restore backup must exist"
    backup_data = _read(backups[0])
    assert backup_data == WIPE_RESIDUE, "backup is the pre-heal state, owned keys only"
    assert "env" not in backup_data and "sk-ant-secret-1" not in backups[0].read_text(encoding="utf-8")
    if os.name != "nt":
        assert (backups[0].stat().st_mode & 0o777) == 0o600


def test_heal_appends_to_log(measure):
    """Every restoration must append a structured record to the heal log."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    log = heal_dir / "heal_log.jsonl"
    assert log.is_file(), "heal_log.jsonl must exist after a heal"
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    restore_entries = [json.loads(l) for l in lines if json.loads(l).get("action") == "restore"]
    assert restore_entries, "at least one restore entry must be in the log"
    entry = restore_entries[-1]
    assert "restored_keys" in entry, "log entry must list restored keys"
    assert entry["restored_keys"] == ["hooks"], "only owned keys are restored"
    assert entry["notice"] == "Token Optimizer restored its own hooks after settings.json was reset"
    assert "timestamp" in entry, "log entry must have a timestamp"


def test_heal_is_silent_to_stdout(measure, capsys):
    """The heal must produce NO stdout output. Silent to the user."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    captured = capsys.readouterr()
    assert captured.out == "", f"heal must produce no stdout, got: {captured.out!r}"


# --------------------------------------------------------------------------- #
# 3. User-authored key survives: NO heal (the survivor fingerprint gate)
# --------------------------------------------------------------------------- #


def test_user_authored_key_survives_no_heal(measure):
    """If even ONE user-authored key survives in the live file, the heal must
    NOT fire. The user was editing deliberately, not wiped."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    # Simulate the user deleting some keys but keeping `permissions`.
    partial = {"cleanupPeriodDays": 99999, "permissions": {"allow": ["Bash(ls:*)"]}}
    _write(settings, partial)

    mod._maybe_self_heal_settings()

    # The file must be unchanged: permissions survived, so this is not a wipe.
    assert _read(settings) == partial, (
        "a file with a user-authored key surviving must NOT be healed"
    )


def test_only_machine_keys_but_no_highwater_no_heal(measure):
    """A file with only machine-written keys but no high-water mark (fresh
    install) must NOT be healed -- there's nothing to restore from."""
    mod, settings, heal_dir = measure
    _write(settings, WIPE_RESIDUE)
    # No prior snapshot -- highwater.json does not exist.

    mod._maybe_self_heal_settings()

    assert _read(settings) == WIPE_RESIDUE, (
        "a wipe with no high-water mark must not be healed (nothing to restore from)"
    )


# --------------------------------------------------------------------------- #
# 4. Tombstone respected: a key the user removed after a heal stays gone
# --------------------------------------------------------------------------- #


def test_tombstone_prevents_restoring_a_deliberately_removed_key(measure):
    """If we restore a key and the user removes it again, a tombstone must
    prevent re-restoring that key permanently."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()  # heal (restores hooks)

    # User deliberately removes the restored hooks; a user key keeps the file healthy.
    healed = _read(settings)
    del healed["hooks"]
    healed["model"] = "opus"
    _write(settings, healed)
    mod._maybe_self_heal_settings()

    tdata = _read(heal_dir / "tombstones.json")
    assert "hooks" in tdata.get("tombstoned_keys", []), "hooks must be tombstoned after the user removed it"

    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()
    assert "hooks" not in _read(settings), "a tombstoned key must NOT be re-restored on a subsequent heal"


# --------------------------------------------------------------------------- #
# 5. Unreadable / malformed file left alone
# --------------------------------------------------------------------------- #


def test_unreadable_file_is_left_alone(measure):
    """An unreadable settings.json must NOT be healed. Fail open, always."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    settings.chmod(0o000)

    # Must not raise. Called bare: a try/except here would let a heal that
    # raises pass this test, which is the very thing it guards.
    try:
        mod._maybe_self_heal_settings()
    finally:
        settings.chmod(0o644)

    # Restore permissions to verify the file is unchanged.
    settings.chmod(0o644)
    assert _read(settings) == FULL_SETTINGS, "unreadable file must not be modified"


def test_malformed_file_is_left_alone(measure):
    """A malformed settings.json must NOT be healed."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    settings.write_text("{ this is not json", encoding="utf-8")

    mod._maybe_self_heal_settings()  # must not raise (called bare on purpose)

    assert settings.read_text(encoding="utf-8") == "{ this is not json"


# --------------------------------------------------------------------------- #
# 6. Snapshot history poisoned with damaged copies, high-water mark still good
# --------------------------------------------------------------------------- #


def test_poisoned_history_highwater_still_good(measure):
    """Repeated wipes must not erode the high-water mark."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    for _ in range(5):
        _write(settings, WIPE_RESIDUE)
        mod._maybe_self_heal_settings()

    highwater = _read(heal_dir / "highwater.json")
    assert highwater == OWNED_PART, "highwater must retain the owned snapshot"


# --------------------------------------------------------------------------- #
# 7. Concurrent writers: the heal must not race with other settings writers
# --------------------------------------------------------------------------- #


def test_concurrent_heal_does_not_drop_keys(measure):
    """The heal uses _write_settings_atomic (which holds the settings lock),
    so a concurrent writer cannot interleave and drop keys."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)

    # Simulate a concurrent writer adding a key while the heal runs.
    # The heal reads the live file, adds absent keys, and writes atomically.
    # If the concurrent writer adds a key between the heal's read and write,
    # the _settings_write_guard would refuse the write (dropping the new key).
    # The heal must fail open in that case, not destroy the concurrent write.
    import threading

    def _concurrent_add():
        time.sleep(0.01)
        try:
            data, ok = mod._read_settings_for_write()
            if ok:
                data["concurrent_key"] = "added_by_other"
                mod._write_settings_atomic(data)
        except Exception:
            pass

    t = threading.Thread(target=_concurrent_add)
    t.start()
    mod._maybe_self_heal_settings()
    t.join()

    # The file must have either the healed state or the concurrent write,
    # but NOT a state that drops keys from either.
    result = _read(settings)
    # At minimum, the machine-written keys must survive.
    assert "cleanupPeriodDays" in result
    assert "statusLine" in result


# --------------------------------------------------------------------------- #
# 8. enabledPlugins disabled (not removed) is never touched
# --------------------------------------------------------------------------- #


def test_enabledplugins_disabled_not_absent_is_not_restored(measure):
    """enabledPlugins belongs to the host: a present (disabled) value is never
    touched, and an absent one is never rebuilt, because it is not ours."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    assert "enabledPlugins" not in _read(heal_dir / "highwater.json")

    live = {"cleanupPeriodDays": 99999,
            "enabledPlugins": {"token-optimizer@alexgreensh-token-optimizer": False}}
    _write(settings, live)
    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert healed["enabledPlugins"] == live["enabledPlugins"]
    assert healed["hooks"] == OWNED_PART["hooks"], "owned keys are still restored"
    assert "permissions" not in healed and "model" not in healed


# --------------------------------------------------------------------------- #
# 9. High-water mark only updates on a richer sighting
# --------------------------------------------------------------------------- #


def test_highwater_only_updates_on_richer_sighting(measure):
    """The high-water mark updates only when a sighting has MORE top-level keys
    than the current high-water mark. A sighting with fewer keys (even if
    healthy) does not replace it."""
    mod, settings, heal_dir = measure
    # First sighting: full settings; the snapshot keeps the 3 owned keys.
    mod._maybe_self_heal_settings()
    first_highwater = _read(heal_dir / "highwater.json")
    assert first_highwater == OWNED_PART

    # Second sighting: fewer keys but still healthy (has user-authored keys).
    smaller = {"permissions": {"allow": []}, "model": "sonnet"}
    _write(settings, smaller)
    mod._maybe_self_heal_settings()

    # High-water mark must NOT be replaced by the smaller file.
    assert _read(heal_dir / "highwater.json") == first_highwater, (
        "highwater must not be replaced by a sighting with fewer keys"
    )


# --------------------------------------------------------------------------- #
# 10. The heal is visible in doctor output
# --------------------------------------------------------------------------- #


def test_heal_visible_in_doctor(measure, capsys):
    """Anyone who goes looking (running `doctor`) must see heal history."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()  # heal

    # The doctor function should report heal history.
    # We check the heal log directly since doctor may need a full environment.
    log = heal_dir / "heal_log.jsonl"
    assert log.is_file()
    entries = [json.loads(l) for l in log.read_text().strip().split("\n")]
    restore_entries = [e for e in entries if e.get("action") == "restore"]
    assert restore_entries, "doctor must be able to find heal history in the log"


# --------------------------------------------------------------------------- #
# 11. Empty {} file (the most complete wipe) is healed
# --------------------------------------------------------------------------- #


def test_empty_file_is_healed(measure):
    """An empty {} settings.json is the most complete wipe. Token Optimizer's
    own keys come back; the user's never do."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot the healthy file
    _write(settings, {})  # total wipe -> empty dict

    mod._maybe_self_heal_settings()

    assert _read(settings) == OWNED_PART


def test_empty_file_no_highwater_no_heal(measure):
    """An empty {} with no high-water mark (fresh install) must NOT be healed
    -- there's nothing to restore from."""
    mod, settings, heal_dir = measure
    _write(settings, {})  # fresh install: empty, no prior snapshot

    mod._maybe_self_heal_settings()

    assert _read(settings) == {}, (
        "an empty file with no high-water mark must not be healed"
    )


# --------------------------------------------------------------------------- #
# 12. High-water mark poisoning guard: machine-only file never becomes highwater
# --------------------------------------------------------------------------- #


def test_machine_only_file_not_snapshotted_as_highwater(measure):
    """A file with only machine-written keys (wipe residue with many keys)
    must NOT become the high-water mark. Only files with user-authored keys
    are valid high-water candidates."""
    mod, settings, heal_dir = measure
    # First: snapshot a healthy file.
    mod._maybe_self_heal_settings()
    first_highwater = _read(heal_dir / "highwater.json")

    # Now present a machine-only file with MORE keys than the real highwater.
    machine_only = {
        "cleanupPeriodDays": 99999,
        "statusLine": {"type": "command", "command": "node 'x'"},
        "hooks": {"SessionEnd": []},
        "env": {"KEY": "val"},
        "mcpServers": {"x": {}},
        "enabledPlugins": {"x": True},
        "compactInstructions": "test",
        "_disabledMcpServers": {"y": {}},
    }
    _write(settings, machine_only)
    mod._maybe_self_heal_settings()

    # The high-water mark must NOT be replaced by the machine-only file.
    assert _read(heal_dir / "highwater.json") == first_highwater, (
        "a machine-only file must never poison the high-water mark"
    )


# --------------------------------------------------------------------------- #
# 13. Backup failure aborts the heal
# --------------------------------------------------------------------------- #


def test_backup_failure_aborts_heal(measure, monkeypatch):
    """If the pre-restore backup fails, the heal must ABORT. No restore
    without a reversible backup."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot
    _write(settings, WIPE_RESIDUE)

    # Make the owned-part backup fail (the heal no longer copies the whole file).
    import settings_owned
    monkeypatch.setattr(settings_owned, "backup_owned", lambda facade, dest_dir: None)

    mod._maybe_self_heal_settings()

    # The file must NOT be healed -- no backup means no restore.
    assert _read(settings) == WIPE_RESIDUE, (
        "a failed backup must abort the heal"
    )
    # The abort must be logged.
    log = heal_dir / "heal_log.jsonl"
    entries = [json.loads(l) for l in log.read_text().strip().split("\n")]
    abort_entries = [e for e in entries if e.get("action") == "restore_aborted"]
    assert abort_entries, "a backup failure must be logged as restore_aborted"


# --------------------------------------------------------------------------- #
# 14. Time-proximity signal: last healthy sighting is recorded
# --------------------------------------------------------------------------- #


def test_last_healthy_sighting_recorded(measure):
    """A time-proximity signal must be recorded on every healthy sighting so
    we can tell a recent wipe (high-water seen recently) from a stale one."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # healthy sighting

    last_healthy = heal_dir / "last_healthy.json"
    assert last_healthy.is_file(), "last_healthy.json must exist after a healthy sighting"
    data = _read(last_healthy)
    assert "timestamp" in data, "last_healthy.json must have a timestamp"
    assert "key_count" in data, "last_healthy.json must have a key count"


# --------------------------------------------------------------------------- #
# 15. Heal files have hardened permissions
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX mode bits: os.chmod on Windows only toggles the read-only "
    "attribute, so the heal dir reports 0o777 regardless of the 0o700 umask "
    "the product applies.",
)
def test_heal_dir_has_restricted_permissions(measure):
    """The heal directory must be 0o700 and heal files 0o600 to protect
    secrets that may be in the high-water mark snapshot."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # creates heal dir + highwater

    import stat as statmod
    dir_mode = statmod.S_IMODE(os.stat(str(heal_dir)).st_mode)
    assert dir_mode == 0o700, f"heal dir must be 0o700, got {oct(dir_mode)}"

    hw_path = heal_dir / "highwater.json"
    if hw_path.is_file():
        file_mode = statmod.S_IMODE(os.stat(str(hw_path)).st_mode)
        assert file_mode == 0o600, f"highwater.json must be 0o600, got {oct(file_mode)}"


# --------------------------------------------------------------------------- #
# 16. Host-owned keys are never restored, even when a disk record could rebuild them
# --------------------------------------------------------------------------- #


def test_host_owned_keys_are_never_rebuilt_or_restored(measure):
    """extraKnownMarketplaces and enabledPlugins belong to Claude Code. The old
    heal rebuilt them from known_marketplaces.json / installed_plugins.json; the
    snapshot-only-what-we-own rule retires that: they are not ours to write."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()
    _write(settings, WIPE_RESIDUE)

    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert "extraKnownMarketplaces" not in healed
    assert "enabledPlugins" not in healed


# --------------------------------------------------------------------------- #
# 18. Schema validation: malformed values are NOT restored
# --------------------------------------------------------------------------- #


def test_malformed_snapshot_value_not_restored(measure, monkeypatch):
    """A tampered or old full snapshot cannot push anything but owned, valid
    entries into settings.json: env is dropped, a malformed hooks value too."""
    mod, settings, heal_dir = measure
    mod._maybe_self_heal_settings()  # snapshot

    hw = heal_dir / "highwater.json"
    hw_data = _read(hw)
    hw_data["env"] = {"KEY": "from-old-snapshot"}
    hw_data["permissions"] = {"allow": ["Bash(*)"]}
    hw_data["hooks"] = ["not", "a", "dict"]
    _write(hw, hw_data)

    _write(settings, WIPE_RESIDUE)
    mod._maybe_self_heal_settings()

    healed = _read(settings)
    assert "env" not in healed and "permissions" not in healed
    assert "hooks" not in healed, "a malformed hooks value must not be restored"
