#!/usr/bin/env python3
"""Hermes session normalizer for Token Optimizer.

Converts a Hermes ``sessions`` row (as returned by ``hermes_state.py``) into
the same canonical dict shape that ``measure.py`` / the dashboard consume for
Codex and Claude Code sessions.  The goal is that all downstream code can be
runtime-agnostic.

## Key design decisions

**Cost:** Read from ``estimated_cost_usd`` in the sessions row.  Hermes
pre-computes this value; if the column is NULL or ``cost_status`` is
``'unknown'`` we fall back to TO's own ``_get_model_cost`` pricing on the
stored token counts.  This mirrors the Codex session
normalizer's approach.

**Quality scoring:** Hermes does not provide per-message token granularity
(``messages.token_count`` is rarely populated).  We therefore score from the
session-level fields only:

  Active signals:
    - Context fill (input_tokens vs model context window)  — weight 0.40
    - Message count risk                                   — weight 0.35
    - Output / input ratio                                 — weight 0.25

  Omitted signals (unavailable from session row):
    - Cache hit rate (cache_read is present but unreliable on Hermes; included
      as informational only; not wired into the score to avoid noise)
    - Compaction events (Hermes does not persist compaction counts)
    - API calls / turn ratio (api_call_count available; included as optional
      compaction proxy in the grade dict but not in the weighted score because
      Hermes call counts are not directly comparable to CC turns)

The grade thresholds reuse ``score_to_grade`` and ``score_to_band`` imported
from ``measure.py`` (same S/A/B/C/D/F scale).

**Model mapping:** ``model`` in Hermes sessions can be any model ID the user
configured (Claude, OpenAI, Gemini, etc.).  We normalise it to a TO model
family for pricing and context-window lookup using the same helpers measure.py
exposes.  Unknown models get a conservative 200 K context window and
``cost_status='unknown'``.

**No Hermes imports:** This module is pure stdlib + measure.py helpers.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of measure.py helpers (avoids circular import at module load;
# measure.py is the shared core and this adapter runs inside it or alongside).
# ---------------------------------------------------------------------------

_MEASURE_HELPERS_SENTINEL = object()  # distinct from None: "not yet resolved"
_measure_helpers_result: Any = _MEASURE_HELPERS_SENTINEL


def _get_measure_helpers():
    """Return (score_to_grade, score_to_band, _get_model_cost, _normalize_model_name).

    Deferred so tests can import hermes_session without a full measure.py
    bootstrap (e.g. when CLAUDE_PLUGIN_ROOT is unset).  Result is cached ONLY
    on success; a transient failure (measure.py not yet on sys.path) is retried
    on the next call rather than permanently locked as None by lru_cache.
    """
    global _measure_helpers_result
    if _measure_helpers_result is not _MEASURE_HELPERS_SENTINEL:
        # Already resolved (either a tuple on success, or None stayed uncached
        # on failure, so we always reach the try block again on failure).
        return _measure_helpers_result
    try:
        # measure.py lives in the same scripts/ directory.
        scripts_dir = Path(__file__).parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import measure as _m  # noqa: PLC0415

        result = (
            _m.score_to_grade,
            _m.score_to_band,
            _m._get_model_cost,  # noqa: SLF001
            _m._normalize_model_name,  # noqa: SLF001
        )
        _measure_helpers_result = result  # cache only on success
        return result
    except Exception as exc:
        logger.debug("[hermes_session] measure.py helpers unavailable: %s", exc)
        # Do NOT update the sentinel — allow retry on next call.
        return None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UNKNOWN_MODEL = "unknown"

# Conservative context window used when model is unrecognised.
_DEFAULT_CONTEXT_WINDOW = 200_000

# Fail-safe window for an unrecognised *modern* Claude (non-haiku) model. Every
# non-haiku Claude family since the 4.x GA line ships at 1M, so this is the
# right value AND the safe direction: too large only understates fill, whereas
# the 200K default cries "100% CRITICAL" wolf on a 1M model (bug #97).
_LARGE_CONTEXT_WINDOW = 1_000_000

# Known Hermes model → approximate context window (tokens).
# Claude context windows; OpenAI/Gemini models handled via measure.py pricing.
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Claude 4.x/5 non-haiku is 1M GA (since March 2026). Prefix-match below
    # propagates these to versioned ids (e.g. claude-opus-4-8, sonnet-4-6).
    # Claude 3.x and all haiku genuinely stay 200K -- do NOT promote them.
    # The Claude 5 family shares no prefix with any 4.x key, so it needs
    # explicit entries; the _claude family fallback in _context_window_for_model
    # is the durable backstop for whatever ships next (opus-6, sonnet-6, ...).
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-5": 1_000_000,
    "claude-sonnet-4-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-3-5": 200_000,
    "claude-opus-4": 1_000_000,
    "claude-sonnet-4": 1_000_000,
    "claude-haiku-3": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-haiku-20240307": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "gpt-5": 1_000_000,
    "gpt-5.1": 1_000_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-3-pro": 1_000_000,
    "gemini-3-flash": 1_000_000,
}

# Signals active for Hermes (documented for operators / tests).
ACTIVE_QUALITY_SIGNALS = (
    "context_fill",    # input_tokens / model_context_window
    "message_count",   # risk from long session
    "output_input_ratio",  # productivity signal
)

OMITTED_QUALITY_SIGNALS = (
    "cache_hit_rate",      # cache_read present but unreliable in Hermes
    "compaction_events",   # not persisted in sessions row
    "api_per_message",     # api_call_count not directly comparable to CC turns
)


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

def _context_window_for_model(model: str) -> int:
    """Return the context window (tokens) for a Hermes model string."""
    if not model or model == _UNKNOWN_MODEL:
        return _DEFAULT_CONTEXT_WINDOW
    low = model.lower().strip()
    # Strip a provider prefix (e.g. "anthropic/claude-fable-5",
    # "openrouter/anthropic/claude-sonnet-5") so vendor-qualified ids resolve
    # like their bare form instead of falling through to the default.
    if "/" in low:
        low = low.rsplit("/", 1)[-1]
    # Direct lookup first.
    if low in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[low]
    # Prefix match for versioned variants (e.g. claude-sonnet-4-5-20250514).
    for key, window in _MODEL_CONTEXT_WINDOWS.items():
        if low.startswith(key):
            return window
    # Durable fallback for a Claude family that shares no prefix with any known
    # key. Modern non-haiku Claude (4.x/5 and whatever comes next) is 1M; only
    # haiku, and the legacy 2.x/3.x lines, stay at 200K. This stops each new
    # family from silently regressing to a false-CRITICAL 200K (bug #97). See
    # _LARGE_CONTEXT_WINDOW for why failing large is the safe direction.
    if (
        low.startswith("claude-")
        and "haiku" not in low
        and not low.startswith(("claude-2", "claude-3"))
    ):
        return _LARGE_CONTEXT_WINDOW
    return _DEFAULT_CONTEXT_WINDOW


# Public alias so hermes/__init__.py can import the single source of truth.
context_window_for_model = _context_window_for_model


def _compute_cost_fallback(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Compute USD cost via TO's pricing as a fallback when Hermes has no cost.

    Returns 0.0 on any error (pricing unknown, measure.py unavailable).
    """
    helpers = _get_measure_helpers()
    if helpers is None:
        return 0.0
    _score_to_grade, _score_to_band, _get_model_cost, _normalize_model_name = helpers
    try:
        return _get_model_cost(
            model,
            input_tokens,
            output_tokens,
            cache_read=cache_read,
            cache_create=cache_write,
        )
    except Exception as exc:
        logger.debug("[hermes_session] pricing fallback error for %r: %s", model, exc)
        return 0.0


def _resolve_model_family(model: str) -> str:
    """Return the TO model family label (opus/sonnet/haiku/unknown)."""
    helpers = _get_measure_helpers()
    if helpers is None:
        return _UNKNOWN_MODEL
    _score_to_grade, _score_to_band, _get_model_cost, _normalize_model_name = helpers
    try:
        family = _normalize_model_name(model)
        return family or _UNKNOWN_MODEL
    except Exception:
        return _UNKNOWN_MODEL


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def compute_quality_score(
    input_tokens: int,
    output_tokens: int,
    message_count: int,
    model: str = _UNKNOWN_MODEL,
    *,
    context_window: int | None = None,
    cache_read: int = 0,
) -> dict[str, Any]:
    """Score Hermes session context quality from available session-level fields.

    Uses only the three signals available in Hermes session rows.  Missing
    per-message data is omitted rather than fabricated.  Grade thresholds
    reuse measure.py's ``score_to_grade`` / ``score_to_band`` exactly.

    ``cache_read`` is included in the fill numerator because cached tokens
    occupy the same context window as freshly-billed input: a session that
    read 160 K tokens from cache against a 200 K window is 80 % full, not 0 %.

    Returns a dict with: score (0-100 int), grade (letter), band (label),
    signals_active (list), signals_omitted (list).
    """
    ctx_win = context_window if context_window and context_window > 0 else _context_window_for_model(model)

    # Signal 1: Context fill (40% weight). Numerator = fresh input + cache_read
    # because cache-read tokens consume context window space just as fresh tokens
    # do.  Cap at 1.0 so emitted fill never exceeds 100% downstream (nudges, UI).
    fill_numerator = input_tokens + max(0, cache_read)
    fill_ratio = min(1.0, max(0.0, fill_numerator / ctx_win)) if ctx_win > 0 else 0.0
    if fill_ratio < 0.30:
        fill_score = 100
    elif fill_ratio < 0.50:
        fill_score = 80
    elif fill_ratio < 0.70:
        fill_score = 55
    elif fill_ratio < 0.85:
        fill_score = 30
    else:
        fill_score = 10

    # Signal 2: Message count risk (35% weight)
    if message_count <= 20:
        msg_score = 100
    elif message_count <= 40:
        msg_score = 80
    elif message_count <= 60:
        msg_score = 55
    elif message_count <= 100:
        msg_score = 30
    else:
        msg_score = 10

    # Signal 3: Output / input ratio (25% weight)
    if input_tokens > 0:
        oi_ratio = output_tokens / input_tokens
    else:
        oi_ratio = 1.0
    if oi_ratio >= 0.05:
        oi_score = 100
    elif oi_ratio >= 0.02:
        oi_score = 70
    elif oi_ratio >= 0.01:
        oi_score = 40
    else:
        oi_score = 15

    raw = fill_score * 0.40 + msg_score * 0.35 + oi_score * 0.25
    final = int(round(min(100, max(0, raw))))

    helpers = _get_measure_helpers()
    if helpers:
        score_to_grade, score_to_band = helpers[0], helpers[1]
        grade = score_to_grade(final)
        band = score_to_band(final)
    else:
        # Inline fallback thresholds matching measure.py exactly.
        if final >= 90:
            grade = "S"
        elif final >= 80:
            grade = "A"
        elif final >= 70:
            grade = "B"
        elif final >= 55:
            grade = "C"
        elif final >= 40:
            grade = "D"
        else:
            grade = "F"
        if final >= 80:
            band = "Good"
        elif final >= 60:
            band = "Fair"
        elif final >= 40:
            band = "Needs Work"
        else:
            band = "Poor"

    return {
        "score": final,
        "grade": grade,
        "band": band,
        "fill_ratio": round(fill_ratio, 4),
        "context_window_used": ctx_win,
        "signals_active": list(ACTIVE_QUALITY_SIGNALS),
        "signals_omitted": list(OMITTED_QUALITY_SIGNALS),
        "signal_scores": {
            "fill": fill_score,
            "message_count": msg_score,
            "output_input_ratio": oi_score,
        },
        "signal_weights": {
            "fill": 0.40,
            "message_count": 0.35,
            "output_input_ratio": 0.25,
        },
    }


# ---------------------------------------------------------------------------
# Session normalizer
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> str | None:
    """Convert a float epoch or ISO string to ISO-8601 string, or None."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        return str(value)
    except (OSError, ValueError, OverflowError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def normalize_session(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a Hermes sessions row into TO's canonical session shape.

    Returns a dict matching the keys that ``measure.py`` / the dashboard
    consume from Codex / Claude Code session data.  Returns None when the
    row is empty or has no meaningful activity (no tokens and no messages).

    Cost strategy:
      - If ``estimated_cost_usd`` is non-NULL → use it directly (cost_source
        set to "hermes_estimated").
      - If NULL / cost_status=="unknown" → compute via TO's _get_model_cost on
        stored token counts (cost_source set to "to_pricing_fallback").

    Quality:
      Calls ``compute_quality_score`` with session-level fields.  The quality
      dict is embedded under the "quality" key.
    """
    if not row:
        return None

    model = str(row.get("model") or _UNKNOWN_MODEL).strip() or _UNKNOWN_MODEL
    # M2: clamp all five token fields to non-negative so a corrupt/negative DB
    # row can never produce a negative cost or fabricated savings.
    input_tokens = max(0, _safe_int(row.get("input_tokens")))
    output_tokens = max(0, _safe_int(row.get("output_tokens")))
    cache_read = max(0, _safe_int(row.get("cache_read_tokens")))
    cache_write = max(0, _safe_int(row.get("cache_write_tokens")))
    reasoning = max(0, _safe_int(row.get("reasoning_tokens")))
    message_count = _safe_int(row.get("message_count"))
    tool_call_count = _safe_int(row.get("tool_call_count"))
    api_call_count = _safe_int(row.get("api_call_count"))

    # Reject completely empty sessions.
    if input_tokens == 0 and output_tokens == 0 and message_count == 0:
        return None

    # Cost resolution.
    raw_cost = row.get("estimated_cost_usd")
    cost_status = str(row.get("cost_status") or "unknown").lower()

    if raw_cost is not None and cost_status != "unknown":
        try:
            cost_usd = float(raw_cost)
            used_cost_source = "hermes_estimated"
        except (TypeError, ValueError):
            cost_usd = None
            used_cost_source = "unknown"
    else:
        cost_usd = None
        used_cost_source = "unknown"

    if cost_usd is None:
        cost_usd = _compute_cost_fallback(model, input_tokens, output_tokens, cache_read, cache_write)
        used_cost_source = "to_pricing_fallback"

    # Model family for downstream model_usage breakdown.
    model_family = _resolve_model_family(model)

    # Context window for fill calculation.
    ctx_window = _context_window_for_model(model)

    # M1: Align with the savings engine's convention.
    #
    # _session_token_vector (measure.py) assumes session_log.input_tokens already
    # contains TOTAL billed input = fresh_input + cache_read + cache_write (same
    # as the Claude Code / Codex convention: input_tokens from the API is the
    # aggregate billed count).  It then reconstructs each class via cache_hit_rate
    # and cache_create_1h_tokens.
    #
    # Hermes DB stores input_tokens as FRESH-ONLY and cache tokens separately, so
    # we must roll them up before writing to session_log:
    #   total_input_tokens  = fresh_input + cache_read + cache_write
    #   total_cache_create_1h = cache_write  (Hermes has no TTL split; use 1h)
    #   total_cache_create_5m = 0            (no 5-min TTL in Hermes)
    #   cache_hit_rate = cache_read / total_input_tokens
    #
    # This lets _session_token_vector reconstruct (fresh, cw, cr, out) accurately:
    #   cw  = cache_create_1h = cache_write
    #   hit = cache_read / total_input
    #   cr  = total_input * hit ≈ cache_read
    #   fi  = total_input * (1 - hit) - cw ≈ fresh_input
    total_input_for_savings = input_tokens + cache_read + cache_write
    cache_hit_rate = cache_read / total_input_for_savings if total_input_for_savings > 0 else 0.0

    # Duration.
    started_at_raw = row.get("started_at")
    ended_at_raw = row.get("ended_at")
    duration_minutes = 0.0
    if started_at_raw is not None and ended_at_raw is not None:
        try:
            duration_minutes = max(0.0, (float(ended_at_raw) - float(started_at_raw)) / 60.0)
        except (TypeError, ValueError):
            duration_minutes = 0.0

    # Quality score from available signals only.
    # M3: pass cache_read into compute_quality_score so the fill numerator
    # includes cached tokens (they occupy the same context window space).
    quality = compute_quality_score(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        message_count=message_count,
        model=model,
        context_window=ctx_window,
        cache_read=cache_read,
    )

    # Topic: use title from Hermes if available.
    title = str(row.get("title") or "").strip() or None

    # model_usage / model_usage_breakdown: mirrors the Codex normalizer's shape.
    # M1: billable uses the original fresh_input (input_tokens) + output so the
    # model_usage key reflects actual API call volume, not the rolled-up total.
    billable = input_tokens + output_tokens
    model_key = model if model != _UNKNOWN_MODEL else "hermes"
    model_usage = {model_key: billable}
    model_usage_breakdown = {
        model_key: {
            "fresh_input": input_tokens,
            "cache_read": cache_read,
            "cache_create": cache_write,
            "output": output_tokens + reasoning,
        }
    }

    return {
        # Identity
        "slug": str(row.get("id") or ""),
        "topic": title,
        # Timestamps
        "first_ts": _parse_ts(started_at_raw),
        "duration_minutes": round(duration_minutes, 2),
        # Token counts — engine convention: input = total billed (fresh+cr+cw).
        # M1: total_input_tokens is the rolled-up total so _session_token_vector
        #     can reconstruct each class via cache_hit_rate + cache_create_1h.
        "total_input_tokens": total_input_for_savings,
        "total_output_tokens": output_tokens,
        "total_cache_read": cache_read,
        "total_cache_create": cache_write,
        "total_cache_create_1h": cache_write,   # M1: forward cache_write here
        "total_cache_create_5m": 0,             # no 5-min TTL in Hermes
        # Context window (for fill %, dashboard)
        "model_context_window": ctx_window,
        # Cache efficiency
        "cache_hit_rate": round(cache_hit_rate, 4),
        # Cost
        "cost_usd": round(cost_usd, 6),
        "cost_source": used_cost_source,
        # Model
        "model": model,
        "model_family": model_family if model_family != _UNKNOWN_MODEL else None,
        "model_usage": model_usage,
        "model_usage_breakdown": model_usage_breakdown,
        # Activity counts
        "message_count": message_count,
        "api_calls": api_call_count,
        "tool_calls": {"total": tool_call_count},
        # Standard keys measure.py checks
        "estimated": False,  # token counts come directly from Hermes DB, not estimated
        "token_source": "hermes_db",
        "runtime": "hermes",
        "version": None,
        # Placeholders for signals Codex provides that Hermes does not.
        "avg_call_gap_seconds": None,
        "max_call_gap_seconds": None,
        "p95_call_gap_seconds": None,
        "rate_limits": None,
        "effort": None,
        "effort_breakdown": {},
        "skills_used": {},
        "subagents_used": {},
        "tool_duration_p90_ms": None,
        "task_duration_ms_max": None,
        "ttft_ms_avg": None,
        # Quality
        "quality": quality,
        "quality_score": quality["score"],
        "quality_grade": quality["grade"],
        "quality_band": quality["band"],
        # Session metadata
        "end_reason": str(row.get("end_reason") or ""),
        "archived": bool(_safe_int(row.get("archived"))),
        "cwd": row.get("cwd"),
        "billing_provider": row.get("billing_provider"),
    }


if __name__ == "__main__":
    import json
    import sys as _sys

    # Quick smoke test against a fixture DB when run directly.
    try:
        from hermes_state import get_session  # noqa: PLC0415

        row = get_session("s-heavy")
        if row:
            norm = normalize_session(row)
            print(json.dumps(norm, indent=2, default=str))
        else:
            print("No session found (DB may not be at default path).", file=_sys.stderr)
    except Exception as exc:
        print(f"Error: {exc}", file=_sys.stderr)
        raise
