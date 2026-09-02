#!/usr/bin/env python3
"""Admin-enabled org telemetry emitter (Token Optimizer Teams edition).

OFF BY DEFAULT, and file-only: nothing is emitted unless an org admin has
placed a ``fleet.json`` (mode 0600) in Token Optimizer's config dir holding
``endpoint``, ``hash_key`` and either ``token`` or ``token_env``. Environment
variables alone never enable emission (a cloned repo's settings must not be
able to switch telemetry on). ``TO_FLEET_DISABLE=1`` overrides everything.

What it sends when enabled: per-session aggregates Token Optimizer already
stores locally (platform, pseudonymous ids, token counts, model split, cost,
counted savings, one account-level limit-meter reading, timestamps). Never
prompts, never responses, never file contents, never file paths, never command
text, never tool output. The allowlist below is enforced on keys AND values
before serialisation.

Delivery: events queue in a local outbox (JSONL, 0600, atomic writes) first,
then the outbox is drained with bounded POSTs (3s timeout, at most two batches
per flush, redirects refused). A failed POST leaves the outbox intact; the id
cursor advances only after a 2xx. Every entry point is fail-open: nothing here
can raise into a hook.

Inspect exactly what would be sent: ``python3 fleet_emitter.py --dry-run``
(output is sensitive: it contains this org's pseudonymous aggregates).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = "to.fleet.v1"

ENV_DISABLE = "TO_FLEET_DISABLE"
ENV_CONFIG = "TO_FLEET_CONFIG"
ENV_USER = "TO_FLEET_USER"
ENV_HASH_KEY = "TO_FLEET_HASH_KEY"

CONFIG_FILENAME = "fleet.json"
OUTBOX_NAME = "fleet-outbox.jsonl"
CURSOR_NAME = "fleet-cursor.json"
LOG_NAME = "fleet.log"

MAX_BATCH = 200            # events per POST
MAX_POSTS_PER_FLUSH = 2    # bounded network time per session-end flush
MAX_OUTBOX = 500           # events kept locally while the collector is down
MAX_OUTBOX_AGE_DAYS = 7
RECENT_DAYS = 2            # re-send rows this recent so backfilled columns land
MIN_NETWORK_SECONDS = 7.0  # skip the network step when less budget remains
TIMEOUT_S = 3.0
LOG_MAX_LINES = 200
TMP_MAX_AGE_S = 600

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,80}$")
_HASHISH_RE = re.compile(r"^(?:[0-9a-f]{16,32}|p-[0-9a-f]{30})$")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Every key a wire event may carry. Anything else is stripped by sanitize_event().
EVENT_KEYS = frozenset({
    "type", "session_uuid", "platform", "project_hash", "date",
    "started_at", "ended_at", "duration_minutes",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_create_tokens",
    "reported_input_tokens", "reported_output_tokens", "api_calls", "message_count",
    "models", "cost_usd", "cost_source", "savings", "savings_coverage",
    "quality_score", "quality_grade",
})
ENVELOPE_KEYS = frozenset({
    "schema", "sent_at", "to_version", "user_hash", "host_hash",
    "limit", "billing_mode", "savings_method", "models_redacted",
    "user_label", "events",
})
_MODEL_PART_KEYS = ("fresh_input", "cache_read", "cache_create", "output")
_STR_KEYS = frozenset({"type", "session_uuid", "platform", "project_hash", "date",
                       "started_at", "ended_at", "cost_source", "savings_coverage",
                       "quality_grade"})


def clamp(text, length=120):
    """Clamp to printable ASCII (control chars and non-ASCII dropped)."""
    return "".join(c for c in str(text or "") if 32 <= ord(c) < 127)[:length]


# --------------------------------------------------------------------------- #
# Configuration (file-only enablement, R2/R2a/R2b)
# --------------------------------------------------------------------------- #

def load_config(env=None, config_dir=None):
    """Return a config dict, or None when telemetry is not configured.

    A fleet.json is REQUIRED. Env vars alone never enable. The file must be
    owner-readable only; a group/world-readable file is ignored with a logged
    reason. TO_FLEET_CONFIG must resolve inside config_dir when config_dir is
    given. Endpoints must be https:// except loopback http.
    """
    env = os.environ if env is None else env
    if (env.get(ENV_DISABLE) or "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    base = Path(config_dir).expanduser() if config_dir else _default_config_dir(env)
    override = (env.get(ENV_CONFIG) or "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        try:
            p.relative_to(base.resolve())
        except ValueError:
            return None
    else:
        p = base / CONFIG_FILENAME
    try:
        if not p.is_file():
            return None
        mode = p.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            log_line(None, f"config ignored: {p.name} is group/world-readable")
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("enabled") is False:
        return None
    endpoint = str(raw.get("endpoint") or "").strip()
    token = str(raw.get("token") or "").strip()
    if not token:
        token = (env.get(str(raw.get("token_env") or "") or "") or "").strip()
    hash_key = str(raw.get("hash_key") or "").strip()
    if not hash_key:
        hk_env = str(raw.get("hash_key_env") or "").strip()
        hash_key = (env.get(hk_env) or "").strip() if hk_env else \
            (env.get(ENV_HASH_KEY) or "").strip()
    if not endpoint or not token or not hash_key:
        return None
    endpoint = clamp(endpoint, 300)
    scheme, _, rest = endpoint.partition("://")
    host = rest.split("/")[0].split(":")[0].lower()
    if scheme == "http" and host not in _LOOPBACK:
        return None
    if scheme not in ("http", "https"):
        return None
    return {
        "endpoint": endpoint.rstrip("/"),
        "token": token,
        "hash_key": hash_key,
        "user": clamp(raw.get("user") or "", 120),
        "send_user_label": bool(raw.get("send_user_label", False)),
        "source": str(p),
    }


def _default_config_dir(env):
    """Mirror measure.CONFIG_DIR cheaply (no measure import when disabled)."""
    base = (env.get("TOKEN_OPTIMIZER_CONFIG_DIR") or "").strip()
    if base:
        return Path(base).expanduser()
    runtime = (env.get("TOKEN_OPTIMIZER_RUNTIME") or "").strip().lower()
    home = Path(env.get("HOME") or env.get("USERPROFILE") or str(Path.home()))
    if runtime == "codex":
        return home / ".codex" / "token-optimizer"
    if runtime == "opencode":
        return home / ".local" / "share" / "opencode" / "token-optimizer"
    if runtime == "hermes":
        return home / ".hermes" / "token-optimizer"
    if runtime == "copilot":
        return home / ".copilot" / "token-optimizer"
    return home / ".claude" / "token-optimizer"


def is_enabled(env=None, config_dir=None):
    return load_config(env=env, config_dir=config_dir) is not None


def enable(endpoint, token, hash_key, config_dir=None, env=None):
    """Write fleet.json at mode 0600. Returns (ok, path_or_reason)."""
    env = os.environ if env is None else env
    base = Path(config_dir).expanduser() if config_dir else _default_config_dir(env)
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / CONFIG_FILENAME
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"endpoint": endpoint, "token": token, "hash_key": hash_key}, fh)
            fh.write("\n")
        os.chmod(str(path), 0o600)
        return True, str(path)
    except OSError as exc:
        return False, exc.__class__.__name__


# --------------------------------------------------------------------------- #
# Pseudonymous identity (R3/R3a, KTD7: HMAC keyed by the org hash_key)
# --------------------------------------------------------------------------- #

def canonical_identity(cfg, env=None, home=None):
    """fleet.json user -> TO_FLEET_USER -> gitconfig user.email -> OS user."""
    env = os.environ if env is None else env
    if cfg.get("user"):
        return cfg["user"]
    v = (env.get(ENV_USER) or "").strip()
    if v:
        return v
    gitconfig = Path(home or Path.home()) / ".gitconfig"
    try:
        in_user = False
        for line in gitconfig.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                in_user = s.lower().startswith("[user")
                continue
            if in_user and "=" in s:
                k, _, val = s.partition("=")
                if k.strip().lower() == "email" and val.strip():
                    return val.strip()
    except OSError:
        pass
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


def _hmac(hash_key, value, length):
    return hmac.new(hash_key.encode("utf-8"), str(value).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:length]


def user_hash(cfg, env=None, home=None):
    return _hmac(cfg["hash_key"], canonical_identity(cfg, env, home), 32)


def host_hash(cfg):
    try:
        import socket
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return _hmac(cfg["hash_key"], host, 16)


def project_hash(cfg, project):
    return _hmac(cfg["hash_key"], project or "", 16)


def session_uuid_for(cfg, row_uuid, jsonl_path):
    """R4a: stored uuid verbatim; NULL falls back to the filename UUID, then an
    HMAC of the path (the path itself never leaves the machine)."""
    if row_uuid:
        s = str(row_uuid).strip()
        if _UUID_RE.fullmatch(s):
            return s.lower()
        return _hmac(cfg["hash_key"], "sid:" + s, 32)
    m = _UUID_RE.search(Path(str(jsonl_path or "")).name)
    if m:
        return m.group(0).lower()
    if jsonl_path:
        return "p-" + _hmac(cfg["hash_key"], "path:" + str(jsonl_path), 30)
    return None


# --------------------------------------------------------------------------- #
# Value sanitisation (R4: allowlist on keys AND values)
# --------------------------------------------------------------------------- #

def _num(value, default=0):
    try:
        if value is None:
            return default
        n = float(value)
        if n != n or n in (float("inf"), float("-inf")):
            return default
        return int(n) if float(n).is_integer() else round(n, 6)
    except (TypeError, ValueError, OverflowError):
        return default


def sanitize_model_name(cfg, name):
    """Strip a provider prefix; enforce the R4 pattern; redact otherwise."""
    raw = str(name or "")
    stripped = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    if _MODEL_RE.fullmatch(stripped):
        return stripped, False
    return "custom-" + _hmac(cfg["hash_key"], "model:" + raw, 16), True


def sanitize_event(cfg, event):
    """Allowlist filter. Returns a clean dict or None when the event is unusable."""
    if not isinstance(event, dict):
        return None
    out = {}
    redacted = 0
    for key, value in event.items():
        if key not in EVENT_KEYS:
            continue
        if key == "models":
            models = {}
            if isinstance(value, dict):
                for model, parts in value.items():
                    if not isinstance(parts, dict):
                        continue
                    clean, was_redacted = sanitize_model_name(cfg, model)
                    redacted += 1 if was_redacted else 0
                    models[clean] = {k: _num(parts.get(k)) for k in _MODEL_PART_KEYS}
            out[key] = models
        elif key == "savings":
            sv = value if isinstance(value, dict) else {}
            out[key] = {"tokens": _num(sv.get("tokens")),
                        "cost_usd": round(_num(sv.get("cost_usd"), 0.0), 6)}
        elif key == "limit":
            out[key] = None
            if isinstance(value, dict):
                out[key] = {
                    "five_hour_pct": _num(value.get("five_hour_pct"), None),
                    "seven_day_pct": _num(value.get("seven_day_pct"), None),
                    "ts": _num(value.get("ts"), None),
                }
        elif key in _STR_KEYS:
            out[key] = None if value is None else clamp(value, 64)
            if key == "project_hash" and out[key] and not _HASHISH_RE.fullmatch(out[key]):
                out[key] = _hmac(cfg.get("hash_key", ""), "proj:" + out[key], 16)
        else:
            out[key] = _num(value, None)
    if out.get("type") != "session" or not out.get("session_uuid"):
        return None
    out["models_redacted"] = redacted
    return out


def build_payload(cfg, events, version, env=None, home=None, meters=None,
                  billing_mode="unknown", savings_method="none"):
    """Envelope + sanitised events. Envelope keys are the frozen ENVELOPE_KEYS."""
    clean, redacted = [], 0
    for ev in events:
        c = sanitize_event(cfg, ev)
        if c is None:
            continue
        redacted += c.pop("models_redacted", 0)
        clean.append(c)
    payload = {
        "schema": SCHEMA,
        "sent_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_version": clamp(version or "unknown", 40),
        "user_hash": user_hash(cfg, env, home),
        "host_hash": host_hash(cfg),
        "billing_mode": clamp(billing_mode or "unknown", 20),
        "savings_method": clamp(savings_method or "none", 40),
        "models_redacted": redacted,
        "events": clean,
    }
    if isinstance(meters, dict) and meters.get("available"):
        payload["limit"] = {
            "five_hour_pct": _num(meters.get("five_hour_pct"), None),
            "seven_day_pct": _num(meters.get("seven_day_pct"), None),
            "ts": _num(meters.get("ts"), None),
        }
    if cfg.get("send_user_label"):
        payload["user_label"] = clamp(canonical_identity(cfg, env, home), 120)
    assert set(payload) <= ENVELOPE_KEYS
    return payload


# --------------------------------------------------------------------------- #
# Event building from trends.db (R4a-R4c, R6a, R6b)
# --------------------------------------------------------------------------- #

def build_session_events(conn, cfg, cursor_id=0, watermark="", runtime="claude",
                         cost_fn=None, model_cost_fn=None, max_events=MAX_OUTBOX,
                         now=None):
    """Union of three bounded queries (R6a/R6b), ascending id, capped so a
    backlog drains over flushes. Returns (events, max_id_seen, max_computed_at)."""
    now = now or datetime.now()
    recent_cutoff = (now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    budget = max(0, int(max_events))
    ids = set()
    for (rid,) in conn.execute(
            "SELECT id FROM session_log WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(cursor_id or 0), budget)):
        ids.add(int(rid))
    for (rid,) in conn.execute(
            "SELECT id FROM session_log WHERE date >= ? ORDER BY id ASC LIMIT ?",
            (recent_cutoff, budget)):
        ids.add(int(rid))
    if watermark:
        for (rid,) in conn.execute(
                "SELECT s.id FROM session_log s WHERE s.session_uuid IN "
                "(SELECT DISTINCT session_uuid FROM counted_reread "
                " WHERE computed_at > ?) AND s.id > 0 ORDER BY s.id ASC LIMIT ?",
                (str(watermark), budget)):
            ids.add(int(rid))
    if not ids:
        return [], int(cursor_id or 0), str(watermark or "")
    rows = conn.execute(
        "SELECT id, jsonl_path, date, project, duration_minutes, input_tokens, "
        "output_tokens, message_count, api_calls, cache_create_1h_tokens, "
        "cache_create_5m_tokens, model_usage_json, model_usage_breakdown_json, "
        "collected_at, quality_score, quality_grade, session_uuid, is_sidechain, "
        "reported_input_tokens, reported_output_tokens, platform, "
        "cost_usd, cost_source "
        "FROM session_log WHERE id IN (%s) ORDER BY id ASC" % ",".join("?" * len(ids)),
        tuple(sorted(ids))).fetchall()
    events, max_id = [], int(cursor_id or 0)
    for (rid, jsonl_path, date, project, duration, in_tok, out_tok, msgs, calls,
         cc1h, cc5m, model_usage_json, breakdown_json, collected_at, qscore, qgrade,
         row_uuid, sidechain, rep_in, rep_out, platform, cost_usd, cost_source) in rows:
        max_id = max(max_id, int(rid or 0))
        if sidechain:
            continue
        suuid = session_uuid_for(cfg, row_uuid, jsonl_path)
        if not suuid:
            continue
        try:
            breakdown = json.loads(breakdown_json) if breakdown_json else {}
        except (TypeError, ValueError):
            breakdown = {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        models, cache_read, cache_create = {}, 0, 0
        for model, parts in breakdown.items():
            if not isinstance(parts, dict):
                continue
            clean, _ = sanitize_model_name(cfg, model)
            models[clean] = {k: _num(parts.get(k)) for k in _MODEL_PART_KEYS}
            cache_read += _num(parts.get("cache_read"))
            cache_create += _num(parts.get("cache_create"))
        # R4b cost precedence: stored -> breakdown -> dominant-model fallback.
        cost, used_source = 0.0, None
        if cost_source:
            cost, used_source = _num(cost_usd, 0.0), clamp(cost_source, 40)
        elif breakdown and cost_fn is not None:
            try:
                cost = float(cost_fn(breakdown, cache_create_1h=cc1h,
                                     cache_create_5m=cc5m) or 0.0)
            except Exception:
                cost = 0.0
        elif model_cost_fn is not None:
            try:
                usage = json.loads(model_usage_json) if model_usage_json else {}
            except (TypeError, ValueError):
                usage = {}
            if isinstance(usage, dict) and usage:
                dominant = max(usage, key=lambda m: _num(usage[m]
                               if isinstance(usage[m], dict) else usage[m]))
                parts = breakdown.get(dominant, {}) if isinstance(breakdown, dict) else {}
                cost = float(model_cost_fn(dominant, _num(parts.get("fresh_input")),
                                           _num(parts.get("output")),
                                           _num(parts.get("cache_read")),
                                           _num(parts.get("cache_create"))) or 0.0)
        counted = runtime == "claude"
        tokens, sav_cost = _counted_savings(conn, suuid) if counted else (0, 0.0)
        events.append({
            "type": "session",
            "session_uuid": suuid,
            "platform": clamp(platform or "unknown", 32),
            "project_hash": project_hash(cfg, project),
            "date": clamp(date, 10),
            "started_at": None,
            "ended_at": clamp(collected_at, 32),
            "duration_minutes": _num(duration, None),
            "input_tokens": _num(in_tok, None),
            "output_tokens": _num(out_tok, None),
            "cache_read_tokens": cache_read,
            "cache_create_tokens": cache_create,
            "reported_input_tokens": _num(rep_in, None),
            "reported_output_tokens": _num(rep_out, None),
            "api_calls": _num(calls, None),
            "message_count": _num(msgs, None),
            "models": models,
            "cost_usd": round(cost, 6),
            "cost_source": used_source,
            "savings": {"tokens": tokens, "cost_usd": round(sav_cost, 6)},
            "savings_coverage": "counted" if counted else "unsupported_platform",
            "quality_score": _num(qscore, None),
            "quality_grade": clamp(qgrade, 8) if qgrade else None,
            "_src_id": int(rid),
        })
    try:
        wm = conn.execute("SELECT MAX(computed_at) FROM counted_reread").fetchone()
        max_wm = str(wm[0]) if wm and wm[0] else str(watermark or "")
    except Exception:
        max_wm = str(watermark or "")
    return events, max_id, max_wm


def _counted_savings(conn, session_uuid):
    """R4c: the counted_reread ledger sums for one session."""
    tokens, cost = 0, 0.0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens),0), COALESCE(SUM(oneshot_usd + reread_usd),0) "
            "FROM counted_reread WHERE session_uuid = ?", (session_uuid,)).fetchone()
        if row:
            tokens, cost = int(row[0] or 0), float(row[1] or 0.0)
    except Exception:
        pass
    return tokens, cost


# --------------------------------------------------------------------------- #
# Outbox, cursor, log (atomic 0600 writes, R6)
# --------------------------------------------------------------------------- #

def _write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def snapshot_dir_ok(snapshot_dir):
    """R6: a group/world-writable snapshot dir disables emission."""
    if snapshot_dir is None:
        return False
    try:
        st = os.stat(str(snapshot_dir))
        return not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        return False


def reap_stale_tmp(snapshot_dir, now=None):
    now = time.time() if now is None else now
    try:
        for p in Path(snapshot_dir).glob("*.tmp"):
            try:
                if now - p.stat().st_mtime > TMP_MAX_AGE_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def log_line(snapshot_dir, message):
    try:
        path = Path(snapshot_dir) / LOG_NAME if snapshot_dir else None
        if path is None:
            return
        stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(f"{stamp} {clamp(message, 300)}")
        _write_private(path, "\n".join(lines[-LOG_MAX_LINES:]) + "\n")
    except Exception:
        pass


def read_state(snapshot_dir):
    """Cursor + savings watermark from one file."""
    try:
        data = json.loads((Path(snapshot_dir) / CURSOR_NAME).read_text(encoding="utf-8"))
        return int(data.get("last_id") or 0), str(data.get("watermark") or "")
    except Exception:
        return 0, ""


def write_state(snapshot_dir, last_id, watermark):
    try:
        _write_private(Path(snapshot_dir) / CURSOR_NAME, json.dumps({
            "last_id": int(last_id), "watermark": str(watermark or ""),
            "updated_at": datetime.utcnow().isoformat() + "Z"}))
    except Exception:
        pass


def read_outbox(snapshot_dir):
    items = []
    try:
        path = Path(snapshot_dir) / OUTBOX_NAME
        if not path.exists():
            return items
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("event"), dict):
                items.append(rec)
    except OSError:
        pass
    return items


def write_outbox(snapshot_dir, items):
    try:
        path = Path(snapshot_dir) / OUTBOX_NAME
        if not items:
            if path.exists():
                path.unlink()
            return
        _write_private(path, "".join(
            json.dumps(i, separators=(",", ":")) + "\n" for i in items))
    except Exception:
        pass


def enqueue(snapshot_dir, events, source_ids=None, now=None):
    """Append sanitised events to the outbox, dedup by session_uuid (newest
    wins), enforce the size and age caps. Returns the number now queued."""
    now = now or datetime.utcnow()
    reap_stale_tmp(snapshot_dir, now.timestamp())
    items = read_outbox(snapshot_dir)
    by_uuid = {r["event"].get("session_uuid"): r for r in items}
    source_ids = source_ids or {}
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "session":
            continue
        by_uuid[ev["session_uuid"]] = {
            "queued_at": now.isoformat() + "Z",
            "id": int(source_ids.get(ev["session_uuid"], 0)),
            "event": ev,
        }
    cutoff = (now - timedelta(days=MAX_OUTBOX_AGE_DAYS)).isoformat()
    merged = [r for r in by_uuid.values() if str(r.get("queued_at", "")) >= cutoff]
    merged.sort(key=lambda r: (str(r.get("queued_at", "")), int(r.get("id") or 0)))
    merged = merged[-MAX_OUTBOX:]
    write_outbox(snapshot_dir, merged)
    return len(merged)


def drain_outbox(snapshot_dir, cfg, version, post=None, max_posts=MAX_POSTS_PER_FLUSH,
                 env=None, home=None, billing_mode="unknown", savings_method="none",
                 meters=None):
    """Send queued events in bounded batches. The cursor advances only over
    acked ids (R6a). Returns stats. Never raises."""
    post = post or post_payload
    stats = {"sent": 0, "posts": 0, "failed": 0, "remaining": 0, "status": None}
    try:
        items = read_outbox(snapshot_dir)
        last_id, watermark = read_state(snapshot_dir)
        posts = 0
        while items and posts < max_posts:
            batch, ids = items[:MAX_BATCH], []
            for rec in batch:
                rid = int(rec.get("id") or 0)
                if rid:
                    ids.append(rid)
            ok, status, _detail = post(cfg, batch, version, env=env, home=home,
                                       billing_mode=billing_mode,
                                       savings_method=savings_method, meters=meters)
            stats["posts"] += 1
            posts += 1
            stats["status"] = status
            if not ok:
                stats["failed"] += 1
                break
            stats["sent"] += len(batch)
            items = items[len(batch):]
            if ids:
                last_id = max(last_id, max(ids))
            write_state(snapshot_dir, last_id, watermark)
        write_outbox(snapshot_dir, items)
        stats["remaining"] = len(items)
    except Exception as exc:
        stats["failed"] += 1
        stats["status"] = exc.__class__.__name__
    return stats


# --------------------------------------------------------------------------- #
# Network (lazy import; redirects refused, R6c)
# --------------------------------------------------------------------------- #

class _NoRedirect(Exception):
    pass


def post_payload(cfg, records, version, timeout=TIMEOUT_S, env=None, home=None,
                 billing_mode="unknown", savings_method="none", meters=None):
    """POST one batch of outbox records as a to.fleet.v1 envelope.
    Returns (ok, status, detail). Never raises."""
    try:
        import urllib.error
        import urllib.request

        class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise _NoRedirect(f"redirect {code} refused")

        events = [r.get("event") for r in records if isinstance(r, dict)]
        payload = build_payload(cfg, events, version, env=env, home=home,
                                meters=meters, billing_mode=billing_mode,
                                savings_method=savings_method)
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            cfg["endpoint"].rstrip("/") + "/v1/ingest", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['token']}",
                "User-Agent": f"token-optimizer-fleet/{clamp(version or 'unknown', 40)}",
            })
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return (200 <= status < 300), status, ""
    except _NoRedirect as exc:
        return False, 0, str(exc)
    except Exception as exc:  # HTTPError, URLError, socket.timeout, ValueError
        status = int(getattr(exc, "code", 0) or 0)
        return False, status, exc.__class__.__name__


# --------------------------------------------------------------------------- #
# Flush entry point (called from the session-end flush worker tail, R5)
# --------------------------------------------------------------------------- #

def emit_after_flush(trends_db=None, snapshot_dir=None, config_dir=None, runtime="claude",
                     version="unknown", cost_fn=None, model_cost_fn=None, meters_fn=None,
                     billing_mode="unknown", time_left_fn=None, env=None, home=None):
    """Build events, enqueue, drain. Fail-open; returns a stats dict or None."""
    env = os.environ if env is None else env
    try:
        cfg = load_config(env=env, config_dir=config_dir)
        if cfg is None:
            return None
        if not snapshot_dir_ok(snapshot_dir):
            log_line(snapshot_dir, "emission skipped: snapshot dir group/world-writable")
            return None
        reap_stale_tmp(snapshot_dir)
        conn = sqlite_connect(trends_db)
        if conn is None:
            return None
        try:
            last_id, watermark = read_state(snapshot_dir)
            outbox_len = len(read_outbox(snapshot_dir))
            events, max_id, max_wm = build_session_events(
                conn, cfg, cursor_id=last_id, watermark=watermark, runtime=runtime,
                cost_fn=cost_fn, model_cost_fn=model_cost_fn,
                max_events=max(0, MAX_OUTBOX - outbox_len))
            meters = None
            if meters_fn is not None:
                try:
                    meters = meters_fn()
                except Exception:
                    meters = None
            if events:
                ids = {e["session_uuid"]: e.pop("_src_id", 0) for e in events}
                enqueue(snapshot_dir, events, source_ids=ids)
            log_line(snapshot_dir, f"flush: {len(events)} new events, "
                                   f"outbox={len(read_outbox(snapshot_dir))}, "
                                   f"endpoint={cfg['endpoint']}")
            if time_left_fn is not None:
                try:
                    if float(time_left_fn()) < MIN_NETWORK_SECONDS:
                        return {"skipped_network": True}
                except Exception:
                    pass
            return drain_outbox(snapshot_dir, cfg, version, env=env, home=home,
                                billing_mode=billing_mode,
                                savings_method="counted_reread" if runtime == "claude"
                                else "none", meters=meters)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        log_line(snapshot_dir, f"emit failed: {exc.__class__.__name__}")
        return None


def sqlite_connect(trends_db):
    try:
        import sqlite3
        conn = sqlite3.connect(str(trends_db), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# CLI (R7). Paths come from measure (KTD9); no path resolver of its own.
# --------------------------------------------------------------------------- #

def _measure():
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if "measure" in sys.modules and not hasattr(sys.modules["measure"], "__file__"):
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    return mod


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--enable" in argv:
        i = argv.index("--enable")
        vals = argv[i + 1:i + 4]
        if len(vals) < 3:
            print("usage: fleet_emitter.py --enable <endpoint> <ingest-token> <hash-key>")
            return 2
        ok, where = enable(*vals)
        print(("wrote " if ok else "failed: ") + str(where))
        return 0 if ok else 1
    m = _measure()
    cfg = load_config(config_dir=str(m.CONFIG_DIR))
    if "--status" in argv:
        if cfg is None:
            print("FLEET TELEMETRY: DISABLED (no valid fleet.json)")
        else:
            outbox = len(read_outbox(str(m.SNAPSHOT_DIR)))
            last_id, _wm = read_state(str(m.SNAPSHOT_DIR))
            host = cfg["endpoint"].split("://", 1)[-1].split("/")[0]
            print(f"FLEET TELEMETRY: ENABLED endpoint={host} source={cfg['source']} "
                  f"outbox={outbox} cursor={last_id}")
        return 0
    if cfg is None:
        return 0
    if "--dry-run" in argv:
        conn = sqlite_connect(m.TRENDS_DB)
        if conn is None:
            print("no trends.db")
            return 1
        last_id, wm = read_state(str(m.SNAPSHOT_DIR))
        events, _mid, _w = build_session_events(conn, cfg, cursor_id=last_id,
                                                watermark=wm, runtime="claude",
                                                cost_fn=m._cost_from_model_breakdown,
                                                model_cost_fn=m._get_model_cost)
        conn.close()
        meters = None
        try:
            meters = m._keepwarm_read_meters()
        except Exception:
            pass
        payload = build_payload(cfg, events, m.TOKEN_OPTIMIZER_VERSION, meters=meters,
                                billing_mode="unknown", savings_method="counted_reread")
        print(json.dumps(payload, indent=2))
        print("SENSITIVE: pseudonymous org aggregates; share deliberately.", file=sys.stderr)
        return 0
    # --flush [--all]
    meters = None
    try:
        meters = m._keepwarm_read_meters()
    except Exception:
        pass
    conn = sqlite_connect(m.TRENDS_DB)
    if conn is None:
        return 1
    last_id, wm = read_state(str(m.SNAPSHOT_DIR))
    if "--all" in argv:
        last_id, wm = 0, ""
    events, max_id, max_wm = build_session_events(
        conn, cfg, cursor_id=last_id, watermark=wm, runtime="claude",
        cost_fn=m._cost_from_model_breakdown, model_cost_fn=m._get_model_cost)
    conn.close()
    if events:
        ids = {e["session_uuid"]: e.pop("_src_id", 0) for e in events}
        enqueue(str(m.SNAPSHOT_DIR), events, source_ids=ids)
    stats = drain_outbox(str(m.SNAPSHOT_DIR), cfg, m.TOKEN_OPTIMIZER_VERSION,
                         savings_method="counted_reread", meters=meters)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
