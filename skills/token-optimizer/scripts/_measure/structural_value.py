"""Value setup trims per request, per day, at each request's own model card.

For every trim of S tokens and every billed request in a session where the
trimmed component loads (scope), that started after the trim and before the
item came back:

  * each call is valued at S times its own model's cache-read rate.

Claims have a day but no timestamp. Calls on the trim day in a session that
started earlier are excluded because their order cannot be established.

Requests come from usage_request_claims (one row per deduplicated request,
with model, day and cache class), joined to session_log through the claim
owner (the session UUID). Sub-agent requests are claimed under their parent
session and are counted: sub-agent transcripts carry the skill listing and
the CLAUDE.md / MEMORY.md instructions (checked on transcripts, U3 report).

This replaces the snapshot-delta formula, which priced reads on the generic
family card and multiplied the write term by ALL cache writes in the window.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import _compat


def _hash63(text):
    digest = hashlib.blake2b(str(text).encode("utf-8", "replace"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


def local_prefixes(home):
    """(home_key, claude_key): sessions whose project dir encodes a path under
    HOME run on this machine; anything under ~/.claude is synced or tool-run
    (cloud stubs live there) and does not load this machine's setup."""
    enc = "".join(c if c.isalnum() else "-" for c in str(home))
    return enc, enc + "--claude"


def scope_matches(scope, project, home):
    project = project or ""
    if scope == "global":
        home_key, claude_key = local_prefixes(home)
        if project.startswith(claude_key):
            return False
        return project == home_key or project.startswith(home_key + "-")
    if scope.startswith("project:"):
        return project == scope[len("project:"):]
    if scope.startswith("tree:"):
        key = scope[len("tree:"):]
        return project == key or project.startswith(key + "-")
    return False


def _session_start(metrics_json, first_day):
    """Epoch of the session's first activity; else the start of its first day."""
    try:
        metrics = json.loads(metrics_json or "{}")
        firsts = [float(v.get("first")) for v in metrics.values()
                  if isinstance(v, dict) and v.get("first") is not None]
        if firsts:
            return min(firsts)
    except (ValueError, TypeError, AttributeError):
        pass
    if first_day:
        try:
            return datetime.strptime(str(first_day)[:10], "%Y-%m-%d").timestamp()
        except ValueError:
            return None
    return None


def load_sessions(conn):
    """owner_hash -> (project, start_epoch) for every session row."""
    out = {}
    for uuid, path, project, metrics, first_day, date in conn.execute(
            "SELECT session_uuid, jsonl_path, project, daily_metrics_json, first_activity_day, date "
            "FROM session_log"):
        start = _session_start(metrics, first_day or date)
        for key in (uuid, path):
            if key:
                out[_hash63(key)] = (project or "", start)
    return out


def load_requests(conn, start_day, end_day):
    try:
        return conn.execute(
            "SELECT owner_hash, day, model, calls, cr, cc, cc_1h, cc_5m, inp FROM usage_request_claims "
            "WHERE day >= ? AND day <= ?", (start_day, end_day)).fetchall()
    except Exception:
        return []


def _price(card, s, cr, cc, cc_1h, cc_5m):
    """(read_tokens, write_tokens, read_usd, write_1h_usd, write_5m_usd) for
    one request and S trimmed tokens."""
    return s, 0, s * float(card.get("cache_read", 0) or 0) / 1e6, 0.0, 0.0


def value_trims(conn, trims, start_day, end_day, card_fn, home, projected=False):
    """Per-trim and per-day value over [start_day, end_day] (local days).

    projected=True values each still-open trim as if it held for the whole
    window (the forward run-rate); realized values only sessions that started
    inside the trim's life. Returns {"trims": {id: {...}}, "days": {...},
    "requests": n, "unmatched_requests": n, "unpriced_tokens": n}.
    """
    sessions = load_sessions(conn)
    rows = load_requests(conn, start_day, end_day)
    per = {t["id"]: {"read_tokens": 0, "write_tokens": 0, "usd": 0.0, "read_usd": 0.0,
                     "write_1h_usd": 0.0, "write_5m_usd": 0.0, "requests": 0,
                     "unpriced_tokens": 0} for t in trims}
    days = {}
    unmatched = 0
    live = [t for t in trims if t.get("tokens", 0) > 0 and not (projected and t.get("closed_at"))]
    by_scope = {}
    for t in live:
        by_scope.setdefault(t["scope"], []).append(t)
    trim_days = {} if projected else {
        t["id"]: (datetime.fromtimestamp(t["ts_epoch"]).strftime("%Y-%m-%d"),
                  datetime.fromtimestamp(t["closed_epoch"]).strftime("%Y-%m-%d")
                  if t.get("closed_epoch") is not None else None)
        for t in live
    }
    scope_cache = {}
    for owner, day, model, calls, cr, cc, cc_1h, cc_5m, inp in rows:
        sess = sessions.get(owner)
        if sess is None:
            unmatched += 1
            continue
        project, start = sess
        card = card_fn(model, (cr or 0) + (cc or 0) + (inp or 0))
        n = int(calls or 1)
        for scope, group in by_scope.items():
            key = (scope, project)
            if key not in scope_cache:
                scope_cache[key] = scope_matches(scope, project, home)
            if not scope_cache[key]:
                continue
            for t in group:
                if not projected:
                    trim_day, closed_day = trim_days[t["id"]]
                    if day < trim_day or (day == trim_day and (start is None or start < t["ts_epoch"])):
                        continue
                    if closed_day is not None and day >= closed_day:
                        continue
                acc = per[t["id"]]
                if card is None:
                    acc["unpriced_tokens"] += t["tokens"] * n
                    continue
                r, w, ur, u1, u5 = _price(card, t["tokens"], cr or 0, cc or 0, cc_1h or 0, cc_5m or 0)
                if not (r or w):
                    continue
                usd = ur + u1 + u5
                acc["read_tokens"] += r * n
                acc["write_tokens"] += w * n
                acc["usd"] += usd * n
                acc["read_usd"] += ur * n
                acc["write_1h_usd"] += u1 * n
                acc["write_5m_usd"] += u5 * n
                acc["requests"] += n
                d = days.setdefault(day, {"tokens": 0, "usd": 0.0})
                d["tokens"] += (r + w) * n
                d["usd"] += usd * n
    for acc in per.values():
        for k in ("usd", "read_usd", "write_1h_usd", "write_5m_usd"):
            acc[k] = round(acc[k], 4)
    for d in days.values():
        d["usd"] = round(d["usd"], 4)
    return {"trims": per, "days": dict(sorted(days.items())), "requests": len(rows),
            "unmatched_requests": unmatched,
            "unpriced_tokens": sum(a["unpriced_tokens"] for a in per.values())}


def window(days, now=None):
    now = now or datetime.now()
    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=max(1, int(days or 30)) - 1)).strftime("%Y-%m-%d")
    return start, end


def card_resolver(f=None):
    f = f or _compat.facade()
    tier_data = f.PRICING_TIERS.get(f._load_pricing_tier(), f.PRICING_TIERS.get("anthropic", {}))
    limit = int(getattr(f, "ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD", 200_000) or 200_000)
    cache = {}

    def card(model, prompt_tokens):
        """Exact card for the model at this request's prompt size (long-context
        cards apply above the threshold); None for an unpriced model."""
        key = (model, int(prompt_tokens or 0) > limit)
        if key not in cache:
            try:
                cache[key] = f._counted_exact_card(model, tier_data, prompt_tokens=int(prompt_tokens or 0))
            except Exception:
                cache[key] = None
        return cache[key]
    return card


def call_coverage(conn, start_day):
    """Disclosure kept from the old formula: session-days with no dated call
    evidence make every call count a lower bound."""
    try:
        f = _compat.facade()
        f.ensure_activity_view(conn)
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN COALESCE(api_calls_known,0)=0 THEN 1 ELSE 0 END),0) "
            "FROM session_activity WHERE date >= ?", (start_day,)).fetchone()
        unknown = int(row[0] or 0)
    except Exception:
        unknown = 0
    return {"api_calls_complete": unknown == 0, "api_calls_unknown_days": unknown}
