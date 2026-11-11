"""The secret-free backup behind the settings.json self-heal.

When settings.json is wiped, Token Optimizer can put back what was lost. The
backup it restores from is a copy of settings.json with every secret taken out,
so a second copy of the file never holds a credential:

* Whole keys that carry credentials by design are not copied: ``env``,
  ``apiKeyHelper``, ``awsAuthRefresh``, ``awsCredentialExport``,
  ``otelHeadersHelper``, and any top-level key whose name looks like a secret
  (token, secret, key, password, credential, auth) except a short safe list.
* ``mcpServers`` / ``_disabledMcpServers`` keep their structure (name, type,
  command, args, url) but lose each server's ``env`` and ``headers`` and any
  argument or URL the credential detector flags.
* Every other string in the tree that ``credential_patterns`` flags is dropped.
  A dropped value is simply absent: no placeholder is stored, so a restore can
  never write back a stand-in that looks real.

A restore only ever adds keys that are missing, and names what it could not
restore. ``TOKEN_OPTIMIZER_SETTINGS_HEAL=0`` turns the backup and the restore off.

Lives beside measure.py; standard library only, Python 3.9 grammar.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

KILL_SWITCH_ENV = "TOKEN_OPTIMIZER_SETTINGS_HEAL"
_OFF = ("0", "false", "off", "no")
_BACKUP_GLOB = "settings.json*"

# Credential carriers by design: never copied, whatever they hold.
DROP_KEYS = frozenset({"env", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper"})
# Names that match the secret pattern below but are not secrets.
SAFE_KEYS = frozenset({"hooks", "keybindings", "authors", "author"})
_SECRET_KEY = re.compile(r"token|secret|key|password|credential|auth", re.IGNORECASE)
# Per-server MCP maps that carry credentials.
_MCP_DROP = frozenset({"env", "headers", "headersHelper", "apiKeyHelper"})
_MCP_KEYS = ("mcpServers", "_disabledMcpServers")
_DROP = object()


def heal_disabled():
    """True when the user turned the backup and restore off."""
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in _OFF


# --- the scrub -----------------------------------------------------------------

def _flagged(text):
    """True when ``text`` holds something credential_patterns recognises. Uses
    the shipped detectors only; with a broken custom-pattern file it falls back
    to the built-in set rather than skipping the check."""
    if not isinstance(text, str) or not text:
        return False
    import credential_patterns as cp
    try:
        # redact_credentials skips its regexes on text with no credential prefix;
        # scan_for_credentials has no such shortcut, so gate it the same way.
        maybe = getattr(cp, "_text_may_contain_credentials", lambda _t: True)(text)
        return cp.redact_credentials(text) != text or (maybe and bool(cp.scan_for_credentials(text)))
    except cp.RedactionConfigError:
        return any(pat.search(text) for _, pat in cp.CREDENTIAL_PATTERNS)


def _secret_name(name):
    return isinstance(name, str) and name not in SAFE_KEYS and bool(_SECRET_KEY.search(name))


class _Stats:
    def __init__(self):
        self.withheld = []   # top-level keys left out whole
        self.partial = []    # top-level keys that lost some inner values
        self.values = 0      # inner values dropped


def _tree(value, stats):
    """Copy of ``value`` without flagged strings (and secret-named string fields)."""
    if isinstance(value, str):
        if _flagged(value):
            stats.values += 1
            return _DROP
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _flagged(k) or (_secret_name(k) and isinstance(v, str)):
                stats.values += 1
                continue
            kept = _tree(v, stats)
            if kept is not _DROP:
                out[k] = kept
        return out
    if isinstance(value, list):
        return [kept for kept in (_tree(v, stats) for v in value) if kept is not _DROP]
    return value


def _args(args, stats):
    """MCP args without credentials. A dropped value also takes the flag that
    introduced it, and a secret-named flag takes its value, so no dangling
    ``--token`` is left behind."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            if not (isinstance(a, str) and a.startswith("-")):
                stats.values += 1
                continue
        if isinstance(a, str) and a.startswith("-") and "=" not in a and _secret_name(a.lstrip("-")):
            stats.values += 1
            skip = True
            continue
        kept = _tree(a, stats)
        if kept is _DROP:
            if out and isinstance(out[-1], str) and out[-1].startswith("-") and "=" not in out[-1]:
                out.pop()
            continue
        out.append(kept)
    return out


def _mcp(value, stats):
    if not isinstance(value, dict):
        return _tree(value, stats)
    out = {}
    for name, cfg in value.items():
        if not isinstance(cfg, dict):
            kept = _tree(cfg, stats)
            if kept is not _DROP:
                out[name] = kept
            continue
        server = {}
        for k, v in cfg.items():
            if k in _MCP_DROP:
                stats.values += 1
            elif k == "args" and isinstance(v, list):
                server[k] = _args(v, stats)
            else:
                kept = _tree(v, stats) if not (_secret_name(k) and isinstance(v, str)) else _DROP
                if kept is _DROP:
                    stats.values += 1
                else:
                    server[k] = kept
        out[name] = server
    return out


def _hooks(value, stats):
    """Hooks without flagged commands; an entry whose command was dropped goes
    too (a hook with no command restores as noise)."""
    clean = _tree(value, stats)
    if not isinstance(clean, dict):
        return clean
    out = {}
    for event, groups in clean.items():
        kept_groups = []
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            good = [e for e in entries if not (isinstance(e, dict) and e.get("type") == "command"
                                               and not isinstance(e.get("command"), str))]
            if good:
                kept_groups.append(dict(group, hooks=good))
        if kept_groups:
            out[event] = kept_groups
    return out


def scrub(data):
    """(clean copy of a settings dict without secrets, stats). Never mutates ``data``."""
    stats = _Stats()
    clean = {}
    if not isinstance(data, dict):
        return clean, stats
    for key, value in data.items():
        if key in DROP_KEYS or _secret_name(key) or _flagged(key):
            stats.withheld.append(str(key))
            continue
        before = stats.values
        if key in _MCP_KEYS:
            kept = _mcp(value, stats)
        elif key == "hooks":
            kept = _hooks(value, stats)
        else:
            kept = _tree(value, stats)
            if key == "statusLine" and not (isinstance(kept, dict) and isinstance(kept.get("command"), str)):
                kept = _DROP
        if kept is _DROP:
            stats.withheld.append(str(key))
            continue
        if stats.values > before:
            stats.partial.append(str(key))
        clean[key] = kept
    return clean, stats


# --- snapshot ------------------------------------------------------------------

def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _meta(clean, stats, timestamp=None):
    return {"key_count": len(clean), "key_set": sorted(clean),
            "withheld_keys": sorted(stats.withheld), "scrubbed_in": sorted(stats.partial),
            "timestamp": timestamp or datetime.now().isoformat()}


def read_highwater(facade):
    """The snapshot, scrubbed again on the way in so a copy an older version
    wrote can never feed a restore. None when there is no file."""
    raw = facade._settings_read_highwater()
    return None if raw is None else scrub(raw)[0]


def read_meta(facade):
    try:
        meta = json.loads((Path(facade._settings_heal_dir()) / "highwater.meta.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def update_highwater(facade, data):
    """Snapshot the secret-free form of ``data``. Returns it, or None if not written.

    Replaced when it holds more keys, or the same keys (or more) with fresher
    values: hook commands change on upgrade, and restoring a stale path would
    restore a broken hook. A sighting that lost a key never shrinks it."""
    clean, stats = scrub(data)
    if not clean:
        return None
    heal_dir = facade._settings_heal_ensure_dir()
    if heal_dir is None:
        return None
    existing = read_highwater(facade)
    if existing is not None:
        grows = len(clean) > len(existing)
        refreshes = set(clean) >= set(existing) and clean != existing
        if not (grows or refreshes):
            return None
    if not facade._settings_write_file_secure(heal_dir / "highwater.json", _dump(clean)):
        return None
    facade._settings_write_file_secure(heal_dir / "highwater.meta.json", _dump(_meta(clean, stats)))
    return clean


def backup_scrubbed(facade, dest_dir):
    """Pre-restore backup of the live file, secret-free, mode 0600. Returns its
    path, or None (the heal then aborts)."""
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
        return dest if facade._settings_write_file_secure(dest, _dump(scrub(live)[0])) else None
    except (OSError, ValueError):
        return None


# --- restore notice --------------------------------------------------------------

def restore_notice(restored_keys, meta=None):
    """The one line shown after a restore, naming what could not be restored."""
    keys = sorted(restored_keys)
    shown = ", ".join(keys[:6]) + (" +%d more" % (len(keys) - 6) if len(keys) > 6 else "")
    line = "Token Optimizer restored %s after settings.json was reset" % (
        "its backup (%s)" % shown if keys else "nothing")
    meta = meta or {}
    withheld = [k for k in meta.get("withheld_keys") or [] if k not in keys]
    if withheld:
        line += ". %s %s not restored: Token Optimizer never stores secrets" % (
            ", ".join(withheld), "was" if len(withheld) == 1 else "were")
    inner = [k for k in meta.get("scrubbed_in") or [] if k in keys]
    if inner:
        line += ". Secret values inside %s were left out" % ", ".join(inner)
    return line


def announce_restore(notice):
    """One short stderr line (the heal log entry carries the same text)."""
    try:
        print("  %s. Turn this off with %s=0." % (notice, KILL_SWITCH_ENV), file=sys.stderr)
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
            line = entry.get("notice") or restore_notice(entry.get("restored_keys") or [])
            return "%s (%s)" % (line, when.strftime("%Y-%m-%d %H:%M"))
    except Exception:
        pass
    return None


# --- migration -------------------------------------------------------------------

def _scrub_file(facade, path):
    """Rewrite one settings copy without secrets, atomically, mode 0600. Returns
    (items dropped, stats), or None when left alone (unreadable / not settings)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    clean, stats = scrub(data)
    if clean == data:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return 0, stats
    if not facade._settings_write_file_secure(path, _dump(clean)):
        return None
    return len(stats.withheld) + stats.values, stats


def migrate_to_secret_free(facade):
    """Scrub every settings copy under settings_heal: highwater.json (+ its meta)
    and backups/settings.json*. Files outside settings_heal are never touched.
    Returns {file: items dropped} for files that changed; idempotent."""
    heal_dir = Path(facade._settings_heal_dir())
    changed = {}
    if not heal_dir.is_dir():
        return changed
    targets = [heal_dir / "highwater.json"]
    backups = heal_dir / "backups"
    if backups.is_dir():
        targets += sorted(p for p in backups.glob(_BACKUP_GLOB) if p.is_file())
    high_stats = None
    for path in targets:
        if not path.is_file():
            continue
        done = _scrub_file(facade, path)
        if done is None:
            continue
        if path.name == "highwater.json":
            high_stats = done[1]
        if done[0]:
            changed[str(path.relative_to(heal_dir))] = done[0]
    meta = heal_dir / "highwater.meta.json"
    if "highwater.json" in changed and high_stats is not None:
        try:
            clean = json.loads((heal_dir / "highwater.json").read_text(encoding="utf-8"))
            old = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
            facade._settings_write_file_secure(meta, _dump(_meta(clean, high_stats, old.get("timestamp"))))
        except (OSError, ValueError, AttributeError):
            pass
    if changed:
        facade._settings_heal_log("migrate_secret_free", files=sorted(changed),
                                  items_dropped=sum(changed.values()))
    return changed
