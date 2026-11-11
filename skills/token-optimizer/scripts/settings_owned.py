"""What the settings.json self-heal may copy and restore: Token Optimizer's own
entries and nothing else.

The self-heal used to snapshot the WHOLE settings.json (``env`` with API keys
and tokens, ``permissions``, ``apiKeyHelper``, other plugins' hooks, MCP server
definitions) into ``settings_heal/highwater.json`` and silently restore any
missing key from it. A privacy audit rated that Medium. The snapshot, the
pre-restore backup and the restore now go through this module, which keeps a
key only when Token Optimizer itself wrote it:

* ``cleanupPeriodDays``     run_ensure_health sets it (lifecycle.py).
* ``statusLine``            only when its command runs our statusline.js.
* ``hooks``                 only the entries Token Optimizer can prove it wrote
                            (owned_hooks.cleanup_owner, the same predicate the
                            uninstall sweep uses). Other plugins' and the
                            user's hooks never enter a copy.
* ``compactInstructions``   only the block setup-smart-compact generated.

Everything else (``env``, ``apiKeyHelper``, ``permissions``, ``mcpServers``,
``enabledPlugins``, ...) is never copied and never restored. ``mcpServers`` /
``_disabledMcpServers`` are moved between each other by the dashboard's MCP
toggle but their content is the user's server configuration (often tokens), so
they are not ours to copy.

Kill switch: ``TOKEN_OPTIMIZER_SETTINGS_HEAL=0`` skips snapshot and restore.

Lives beside measure.py; standard library only, Python 3.9 grammar.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

OWNED_KEYS = ("cleanupPeriodDays", "statusLine", "hooks", "compactInstructions")
KILL_SWITCH_ENV = "TOKEN_OPTIMIZER_SETTINGS_HEAL"
_OFF = ("0", "false", "off", "no")
_BACKUP_GLOB = "settings.json*"


def heal_disabled():
    """True when the user turned the snapshot and restore off."""
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in _OFF


# --- what we own -----------------------------------------------------------------

def _owned_hooks(facade, hooks):
    if not isinstance(hooks, dict):
        return {}
    try:
        import owned_hooks
        owns = owned_hooks.cleanup_owner(facade)
    except Exception:
        return {}  # cannot prove ownership: copy nothing
    out = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            kept = []
            for entry in entries:
                try:
                    if isinstance(entry, dict) and owns(event, group, entry):
                        kept.append(entry)
                except Exception:
                    continue
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                kept_groups.append(new_group)
        if kept_groups:
            out[event] = kept_groups
    return out


def _owned_compact(facade, text):
    if not isinstance(text, str):
        return None
    marker = facade._COMPACT_INSTR_MARKER
    ours = text.split(marker, 1)[1] if marker in text else text
    return ours.strip() if facade._compact_instructions_generated(ours) else None


def owned_subset(facade, data):
    """The part of a settings dict Token Optimizer owns. Fails closed: anything
    it cannot prove is ours is left out."""
    if not isinstance(data, dict):
        return {}
    out = {}
    days = data.get("cleanupPeriodDays")
    if isinstance(days, int) and not isinstance(days, bool) and days > 0:
        out["cleanupPeriodDays"] = days
    line = data.get("statusLine")
    if isinstance(line, dict):
        try:
            if facade._statusline_cmd_is_ours(line.get("command")):
                out["statusLine"] = {k: line[k] for k in ("type", "command") if k in line}
        except Exception:
            pass
    hooks = _owned_hooks(facade, data.get("hooks"))
    if hooks:
        out["hooks"] = hooks
    try:
        compact = _owned_compact(facade, data.get("compactInstructions"))
    except Exception:
        compact = None
    if compact:
        out["compactInstructions"] = compact
    return out


# --- snapshot --------------------------------------------------------------------

def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def read_highwater(facade):
    """The snapshot, reduced to owned keys on the way in so a copy written by an
    older version can never feed a restore. None when there is no file."""
    raw = facade._settings_read_highwater()
    return None if raw is None else owned_subset(facade, raw)


def update_highwater(facade, data):
    """Snapshot the owned part of ``data``. Returns the snapshot written, or None.

    Replaced when it knows more owned keys, or the same keys with fresher values
    (hook commands change on upgrade; a stale path restored is a broken hook). A
    sighting that lost an owned key never replaces it."""
    owned = owned_subset(facade, data)
    if not owned:
        return None
    heal_dir = facade._settings_heal_ensure_dir()
    if heal_dir is None:
        return None
    existing = read_highwater(facade)
    if existing is not None:
        grows = len(owned) > len(existing)
        refreshes = set(owned) >= set(existing) and owned != existing
        if not (grows or refreshes):
            return None
    if not facade._settings_write_file_secure(heal_dir / "highwater.json", _dump(owned)):
        return None
    meta = {"key_count": len(owned), "key_set": sorted(owned),
            "timestamp": datetime.now().isoformat()}
    facade._settings_write_file_secure(heal_dir / "highwater.meta.json", _dump(meta))
    return owned


def backup_owned(facade, dest_dir):
    """Pre-restore backup: the owned part of the live file, mode 0600. Returns
    its path, or None (the heal then aborts, as before)."""
    try:
        path = Path(facade.SETTINGS_PATH)
        if not path.is_file():
            return None
        live = json.loads(path.read_text(encoding="utf-8"))
        dest = Path(dest_dir) / "settings.json"
        i = 1
        while dest.exists():
            dest = Path(dest_dir) / ("settings.json-%d" % i)
            i += 1
        return dest if facade._settings_write_file_secure(dest, _dump(owned_subset(facade, live))) else None
    except (OSError, ValueError):
        return None


# --- restore ---------------------------------------------------------------------

def restore_value(facade, key, highwater):
    """(value, "snapshot") for an absent owned key, else (None, None). Re-filtered
    here so a restore can only ever write owned content."""
    if key not in OWNED_KEYS or key not in highwater:
        return None, None
    value = owned_subset(facade, {key: highwater[key]}).get(key)
    if value is None or not facade._settings_validate_value(key, value):
        return None, None
    return value, "snapshot"


def restore_notice(restored_keys):
    """The one line shown after a restore."""
    keys = sorted(restored_keys)
    what = "its own hooks" if keys == ["hooks"] else "its own settings (%s)" % ", ".join(keys)
    return "Token Optimizer restored %s after settings.json was reset" % what


def announce_restore(restored_keys):
    """One short stderr notice (the heal log entry carries the same line)."""
    try:
        print("  [Token Optimizer] %s. Turn this off with %s=0." % (restore_notice(restored_keys), KILL_SWITCH_ENV),
              file=sys.stderr)
    except Exception:
        pass


def last_restore_line(facade, max_age_days=14):
    """The most recent restore as one line, or None when none within ``max_age_days``."""
    try:
        log = Path(facade._settings_heal_dir()) / "heal_log.jsonl"
        if not log.is_file():
            return None
        for raw in reversed(log.read_text(encoding="utf-8").strip().split("\n")):
            if not raw.strip():
                continue
            entry = json.loads(raw)
            if entry.get("action") != "restore":
                continue
            when = datetime.fromisoformat(str(entry.get("timestamp")))
            if max_age_days is not None and datetime.now() - when > timedelta(days=max_age_days):
                return None
            line = entry.get("notice") or restore_notice(entry.get("restored_keys") or ["hooks"])
            return "%s (%s)" % (line, when.strftime("%Y-%m-%d %H:%M"))
    except Exception:
        pass
    return None


# --- migration ---------------------------------------------------------------------

def _reduce_file(facade, path):
    """Rewrite one full-settings copy as its owned part, atomically, mode 0600.
    Returns the number of keys dropped, or None when left alone (unreadable or
    not a settings object)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    owned = owned_subset(facade, data)
    if owned == data:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return 0
    if not facade._settings_write_file_secure(path, _dump(owned)):
        return None
    return len(data) - len(owned)


def migrate_to_owned_only(facade):
    """Reduce every full-settings copy under settings_heal to owned keys only:
    highwater.json (+ its meta) and backups/settings.json*. Files outside
    settings_heal are never touched. Returns {file: keys_dropped} for files that
    changed; idempotent, so a second run returns {}."""
    heal_dir = Path(facade._settings_heal_dir())
    changed = {}
    if not heal_dir.is_dir():
        return changed
    targets = [heal_dir / "highwater.json"]
    backups = heal_dir / "backups"
    if backups.is_dir():
        targets += sorted(p for p in backups.glob(_BACKUP_GLOB) if p.is_file())
    for path in targets:
        if path.is_file():
            dropped = _reduce_file(facade, path)
            if dropped:
                changed[str(path.relative_to(heal_dir))] = dropped
    mark = heal_dir / "highwater.json"
    meta = heal_dir / "highwater.meta.json"
    if "highwater.json" in changed and meta.is_file():
        try:
            old = json.loads(meta.read_text(encoding="utf-8"))
            keys = sorted(json.loads(mark.read_text(encoding="utf-8")))
            old.update({"key_count": len(keys), "key_set": keys})
            facade._settings_write_file_secure(meta, _dump(old))
        except (OSError, ValueError, AttributeError):
            pass
    if changed:
        facade._settings_heal_log("migrate_owned_only", files=sorted(changed),
                                  keys_dropped=sum(changed.values()))
    return changed
