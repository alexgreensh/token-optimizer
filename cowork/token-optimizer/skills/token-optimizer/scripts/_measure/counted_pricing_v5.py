"""Counted-ledger pricing, version 5.

Three corrections to how already-counted context savings are priced. None of
them changes a token count; each one prices tokens the ledger already holds at
the rate the provider actually billed (or would have billed).

1. Write-class premium. An initial removal is logged at the input rate. When
   the next call wrote its context to the cache, the removed tokens would have
   been billed at that write class. The counted pass already measures the
   difference per event (``oneshot_class_premium_usd``); it now joins the action
   total as its own measured part instead of a footnote.
2. Cache-miss rereads. A later turn normally re-reads removed context from the
   cache. When a turn read nothing from the cache and wrote at least the removed
   amount, that turn rewrote its whole context, so the removed tokens would have
   been billed at the turn's write class, not the cache-read rate.
3. Lean resumes. Older ``resume_lean`` rows were priced on a family card (the
   generic ``opus`` rate). They are repriced on the exact card of the model the
   resumed session recorded.

Pure functions only: callers pass rate cards and lookups in, so this module has
no import-time state and no facade dependency.
"""

import json
import math
from . import counted_position


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --------------------------------------------------------------------------
# 1. Write-class premium as an action-savings part
# --------------------------------------------------------------------------

def class_premium_usd(counted):
    """Measured write-class premium for the removal cohort, or 0.0.

    Only a usable counted ledger contributes; an unavailable one adds nothing.
    """
    counted = counted or {}
    if not counted.get("available"):
        return 0.0
    value = counted.get("oneshot_class_premium_usd", 0.0)
    try:
        value = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


# --------------------------------------------------------------------------
# 2. Cache-miss reread turns
# --------------------------------------------------------------------------

def is_cache_miss_turn(cache_class, tokens, context=None, removal_context=None):
    """True when a reread turn rewrote its whole context.

    A partial cache hit counts when its read ends before the context at the
    removal and the later context has not shrunk. Without position evidence,
    only a full miss counts. A missing usage record never counts as a miss.
    """
    return counted_position.is_rebuild(cache_class, tokens, context, removal_context)


def reread_turn_rate(card, cache_class, tokens, oneshot_rate, context=None, removal_context=None):
    """(rate per MTok or None, miss flag) for one reread turn.

    ``oneshot_rate`` is the counted pass's own write-class resolver
    (``_counted_oneshot_rate``), so a miss turn is priced exactly like the
    first call after a removal: its 1h/5m write class, or the input rate when
    the card has no write premium.
    """
    base = (card or {}).get("cache_read")
    if not _finite(base) or base < 0:
        return base, False
    if not is_cache_miss_turn(cache_class, tokens, context, removal_context):
        return base, False
    rate, basis = oneshot_rate(card, cache_class)
    if basis not in ("class", "exact") or not _finite(rate) or rate < base:
        return base, False
    return rate, True


# --------------------------------------------------------------------------
# 3. Lean resumes on exact model cards
# --------------------------------------------------------------------------

_RESUME_BASIS = (
    ("cold 1h cache-write", "cache_write_1h"),
    ("warm cache-read", "cache_read"),
    ("working-set fallback", "cache_read"),
)


def resume_rate_key(detail):
    """Card rate a resume_lean row was priced at, from its own detail text."""
    text = str(detail or "")
    for marker, key in _RESUME_BASIS:
        if marker in text:
            return key
    return None


def resume_exact_model(breakdown_json, family=None):
    """The exact model id a session ran, from session_log.model_usage_breakdown_json.

    When the row names a family (``opus``), only that family's models are
    candidates. Among candidates the one with the most recorded tokens wins.
    Returns None when nothing usable is recorded.
    """
    try:
        breakdown = json.loads(breakdown_json) if isinstance(breakdown_json, str) else breakdown_json
    except (TypeError, ValueError):
        return None
    if not isinstance(breakdown, dict):
        return None
    sized = []
    for model, parts in breakdown.items():
        if not isinstance(model, str) or not model or model.startswith("<"):
            continue
        total = 0
        if isinstance(parts, dict):
            total = sum(v for v in parts.values() if isinstance(v, int) and not isinstance(v, bool) and v > 0)
        if total > 0:
            sized.append((total, model))
    if family:
        fam = str(family).lower()
        same = [item for item in sized if fam in item[1].lower()]
        if same:
            sized = same
        elif fam in ("opus", "sonnet", "haiku", "fable", "mythos"):
            return None
    if not sized:
        return None
    return max(sized)[1]


def resume_exact_rate(breakdown_json, family, rate_key, exact_card):
    """(per-MTok rate, exact model id) on the session's exact card, or (None, None)."""
    if not rate_key:
        return None, None
    model = resume_exact_model(breakdown_json, family)
    if not model:
        return None, None
    card = exact_card(model) or {}
    rate = card.get(rate_key)
    if rate_key == "cache_write_1h" and not _finite(rate):
        rate = card.get("cache_write")
    if not _finite(rate) or rate < 0:
        return None, None
    return float(rate), model


_RESUME_ROWS_SQL = (
    "SELECT s.tokens_saved, s.cost_saved_usd, s.model, s.detail, "
    "  (SELECT l.model_usage_breakdown_json FROM session_log l "
    "   WHERE l.session_uuid = s.session_uuid ORDER BY l.id DESC LIMIT 1) "
    "FROM {table} s WHERE s.event_type = 'resume_lean' AND s.timestamp >= ?")


def resume_lean_reprice(conn, table, cutoff, exact_card):
    """Exact-card correction for the resume_lean rows summed in this window.

    Returns {"delta_usd", "repriced_events", "unresolved_events", "tokens"}.
    The stored rows are never changed: the delta is applied to the estimated
    resume_lean entry at read time. A row whose session or model card cannot be
    resolved keeps its stored price and is counted as unresolved.
    """
    out = {"delta_usd": 0.0, "repriced_events": 0, "unresolved_events": 0, "tokens": 0}
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        if "model_usage_breakdown_json" not in cols:
            return out
        rows = conn.execute(_RESUME_ROWS_SQL.format(table=table), (cutoff,)).fetchall()
    except Exception:
        return out
    for tokens, cost, family, detail, breakdown in rows:
        try:
            tokens = int(tokens or 0)
            cost = float(cost or 0.0)
        except (TypeError, ValueError):
            continue
        if tokens <= 0:
            continue
        rate, _model = resume_exact_rate(breakdown, family, resume_rate_key(detail), exact_card)
        if rate is None:
            out["unresolved_events"] += 1
            continue
        out["delta_usd"] += tokens * rate / 1e6 - cost
        out["repriced_events"] += 1
        out["tokens"] += tokens
    out["delta_usd"] = round(out["delta_usd"], 6)
    return out


def resume_lean_log_rate(conn, session_uuid, family, rate_key, exact_card):
    """Exact-card rate for a new resume_lean credit, or None to keep the family card."""
    try:
        row = conn.execute(
            "SELECT model_usage_breakdown_json FROM session_log WHERE session_uuid = ? "
            "ORDER BY id DESC LIMIT 1", (session_uuid,)).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    rate, _model = resume_exact_rate(row[0], family, rate_key, exact_card)
    return rate


# --------------------------------------------------------------------------
# Session-mix pricing for rows without per-turn evidence (moved verbatim from
# savings._counted_session_cache_mix / _counted_rollup_rows, behaviour unchanged)
# --------------------------------------------------------------------------

def session_cache_mix_terms(breakdown, exact_card, rate_key):
    """[(cache_read weight, rate or None)] from a parsed per-model breakdown, or None."""
    terms = []
    for model, parts in breakdown.items():
        weight = parts.get("cache_read") if isinstance(parts, dict) else None
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            continue
        rate = (exact_card(model) or {}).get(rate_key)
        if not (isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0):
            rate = None
        terms.append((weight, rate))
    return terms if any(rate is not None for _, rate in terms) else None


def session_mix_price(amount, terms):
    """(usd, priced tokens, unpriced signed remainder) for a signed token amount."""
    total_weight = sum(weight for weight, _ in terms)
    rated = [(weight, rate) for weight, rate in terms if rate is not None]
    rated_weight = sum(weight for weight, _ in rated)
    priced = round(abs(amount) * rated_weight / total_weight)
    usd = (math.copysign(1, amount) * abs(amount)
           * sum(weight * rate for weight, rate in rated) / total_weight / 1e6)
    return usd, priced, int(math.copysign(abs(amount) - priced, amount))


# --------------------------------------------------------------------------
# Orphaned derived rows
# --------------------------------------------------------------------------

def orphaned_keys(stored_rows, candidate_keys, conn):
    """Stored ledger-event rows whose source event still exists but is no longer
    a counted removal.

    counted_reread rows are derived from savings/compression events. When an
    event type leaves the counted set (checkpoint_restore moved to the estimated
    tier on 2026-11-05), its old derived rows are never re-scored, so they keep
    their old price forever and, after any pricing-version bump, hold the card in
    "re-measuring" permanently. They are excluded from the counted rollup at read
    time and never deleted. A row whose source event is gone keeps counting (it
    is the durable history). Marker (mk) and media (md) rows have their own
    lifecycles and are left alone.
    """
    present = set()
    try:
        present.update("se:%d" % r[0] for r in conn.execute("SELECT id FROM main.savings_events"))
        present.update("ce:%d" % r[0] for r in conn.execute("SELECT id FROM main.compression_events"))
    except Exception:
        return set()
    return {row["event_key"] for row in stored_rows
            if row.get("source") in ("se", "ce") and row["event_key"] in present
            and row["event_key"] not in candidate_keys}
