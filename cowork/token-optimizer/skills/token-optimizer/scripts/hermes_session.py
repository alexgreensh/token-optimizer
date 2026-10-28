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

  Omitted signals (not weighted in the score):
    - Cache hit rate (cache_read is present but unreliable on Hermes; included
      as informational only; not wired into the score to avoid noise)
    - Compaction events (the sessions row carries no successful-compaction
      count. It does carry compression-health fields: failure cooldown, error,
      fallback streak, ineffective count, recovery deadline — those gate the
      nudge, not the quality score)
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
import re
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

# Fail-safe window for an unrecognised *modern* Claude model with no parseable
# version. Too large only understates fill, whereas the 200K default cries
# "100% CRITICAL" wolf on a 1M model.
_LARGE_CONTEXT_WINDOW = 1_000_000

_CLAUDE_FAMILY_RE = re.compile(
    r"(fable|mythos|opus|sonnet|haiku)(?:[-_.](\d+))?(?:[-_.](\d{1,2}))?(?!\d)")


def _claude_context_window(low: str):
    """Claude window per Claude Code's model-config docs (checked 2026-10-10).

    Fable, Sonnet 5+, Opus 4.7+ and Haiku 5.5 are 1M with no suffix; Sonnet
    4.6 / Opus 4.6 are 1M only as the ``[1m]`` variant (200K without); every
    older Claude is 200K. None when ``low`` is not a Claude id.
    """
    if not (low.startswith("claude") or re.match(
            r"^(fable|mythos|opus|sonnet|haiku)(?:[-_.]\d|$)", low)):
        return None
    one_m = "[1m]" in low or "1000k" in low
    ident = re.sub(r"[-@]\d{8}$", "", low.replace("[1m]", ""))
    if re.search(r"claude[-_]?[0-3]\b", ident) or re.search(
            r"claude-\d(?:[-.]\d)?-(opus|sonnet|haiku)", ident):
        return 200_000
    m = _CLAUDE_FAMILY_RE.search(ident)
    if not m:
        return None
    family, major_raw, minor_raw = m.groups()
    if family in ("fable", "mythos"):
        return 1_000_000
    major = int(major_raw) if major_raw else None
    minor = int(minor_raw) if minor_raw else 0
    if family == "haiku":
        if major is None:
            return 200_000
        return 1_000_000 if (major, minor) >= (5, 5) else 200_000
    if major is None or major >= 5:
        return 1_000_000
    if major == 4 and family == "opus" and minor >= 7:
        return 1_000_000
    if major == 4 and minor == 6:
        return 1_000_000 if one_m else 200_000
    return 200_000


# Known Hermes model → approximate context window (tokens).
# Claude context windows; OpenAI/Gemini models handled via measure.py pricing.
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Claude ids resolve through _claude_context_window() (per-model doc table).
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
    "compaction_events",   # no successful-compaction count in the sessions row
                           # (compression-health fields exist and gate the nudge)
    "api_per_message",     # api_call_count not directly comparable to CC turns
)


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

# Bedrock inference-profile ids are dot-qualified
# ("us.anthropic.claude-haiku-4-5", "global.anthropic.claude-sonnet-4-5"): a
# leading chain of known provider/region tokens. Mirror of measure.py's
# _DOTTED_PROVIDER_PREFIX_RE; dots are only cut on KNOWN tokens so versioned
# ids like "gpt-4.1" or "claude-3.5-sonnet" are never split.
_DOTTED_PROVIDER_PREFIX_RE = re.compile(
    r"^(?:(?:anthropic|openai|google|gemini|vertex|bedrock|openrouter|gateway"
    r"|litellm|azure|aws|amazon|us|us-gov|eu|ap|apac|au|ca|cn|global|jp|sa|me"
    r"|af|il)\.)+")


def _context_window_for_model(model: str) -> int:
    """Return the context window (tokens) for a Hermes model string."""
    if not model or model == _UNKNOWN_MODEL:
        return _DEFAULT_CONTEXT_WINDOW
    low = model.lower().strip()
    # Strip a provider prefix (e.g. "anthropic/claude-fable-5",
    # "openrouter/anthropic/claude-sonnet-5", "us.anthropic.claude-haiku-4-5")
    # so vendor-qualified ids resolve like their bare form instead of falling
    # through to the default.
    if "/" in low:
        low = low.rsplit("/", 1)[-1]
    low = _DOTTED_PROVIDER_PREFIX_RE.sub("", low)
    claude = _claude_context_window(low)
    if claude is not None:
        return claude
    # Direct lookup, then prefix match for versioned variants.
    if low in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[low]
    for key, window in _MODEL_CONTEXT_WINDOWS.items():
        if low.startswith(key):
            return window
    if low.startswith("claude-") and not low.startswith(("claude-2", "claude-3")):
        return _LARGE_CONTEXT_WINDOW
    return _DEFAULT_CONTEXT_WINDOW


_PLAIN_46_RE = re.compile(r"(?:^|[^a-z])(?:opus|sonnet)[-_.]4[-_.]6(?!\d)")


def promote_window_for_observed_tokens(model: str, window: int, tokens) -> int:
    """Opus/Sonnet 4.6 are 1M only as the ``[1m]`` variant, but runtimes record
    the plain id. Tokens above the 200K window prove it is the 1M variant, so
    widen instead of clamping to 100%. Any other model, or tokens that fit,
    leave the window alone.
    """
    low = (model or "").lower()
    if (window >= _LARGE_CONTEXT_WINDOW or "[1m]" in low or not _PLAIN_46_RE.search(low)):
        return window
    try:
        if tokens is not None and float(tokens) > window:
            return _LARGE_CONTEXT_WINDOW
    except (TypeError, ValueError):
        pass
    return window


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

# Distinguishes "caller did not opt in" from "opted in but occupancy is
# unavailable": callers that never pass ``context_tokens`` keep the lifetime
# fill their scores shipped with, while Hermes callers pass an explicit value
# (``None`` = measured-but-unavailable) to get live-occupancy scoring.
_LIVE_CONTEXT_UNSET: Any = object()


def compute_quality_score(
    input_tokens: int,
    output_tokens: int,
    message_count: int,
    model: str = _UNKNOWN_MODEL,
    *,
    context_window: int | None = None,
    cache_read: int = 0,
    context_tokens: int | None | object = _LIVE_CONTEXT_UNSET,
) -> dict[str, Any]:
    """Score Hermes session context quality from available session-level fields.

    Two fill modes, chosen by whether the caller passes ``context_tokens``:

    - Not passed (the Cursor/Copilot/Grok/Antigravity adapters): the legacy
      lifetime fill numerator ``input_tokens + cache_read`` — both occupy the
      same context window, so a session that read 160K cached tokens against
      a 200K window is 80% full, not 0%.
    - Passed (the Hermes path): live prompt occupancy only. Hermes session
      rows store lifetime totals — every API call re-sends the prompt, so
      summing them is never a context-window measurement — and only the
      provider-reported latest prompt counts. A non-positive or non-numeric
      value is "measured but unavailable": fill is omitted rather than
      fabricated and the two remaining signals renormalize below.

    Grade thresholds reuse measure.py's ``score_to_grade`` / ``score_to_band``
    exactly.

    Returns a dict with: score (0-100 int), grade (letter), band (label),
    signals_active (list), signals_omitted (list).
    """
    ctx_win = context_window if context_window and context_window > 0 else _context_window_for_model(model)
    # Only a live prompt reading proves a window; the legacy lifetime sum
    # re-counts every call and exceeds any window by construction.
    if context_tokens is not _LIVE_CONTEXT_UNSET:
        ctx_win = promote_window_for_observed_tokens(model, ctx_win, context_tokens)

    # Signal 1: Context fill (40% weight when present).
    if context_tokens is _LIVE_CONTEXT_UNSET:
        fill_available = True
        fill_numerator = input_tokens + max(0, cache_read)
        fill_ratio = min(1.0, max(0.0, fill_numerator / ctx_win)) if ctx_win > 0 else 0.0
        context_source = "lifetime_tokens"
    else:
        fill_available = (
            isinstance(context_tokens, (int, float))
            and not isinstance(context_tokens, bool)
            and context_tokens > 0
        )
        fill_ratio = min(1.0, max(0.0, context_tokens / ctx_win)) if fill_available and ctx_win > 0 else None
        context_source = "provider_prompt_tokens" if fill_available else "unavailable"
    if not fill_available:
        fill_score = None
    elif fill_ratio < 0.30:
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

    if fill_available:
        weights = {"fill": 0.40, "message_count": 0.35, "output_input_ratio": 0.25}
        raw = (
            fill_score * weights["fill"]
            + msg_score * weights["message_count"]
            + oi_score * weights["output_input_ratio"]
        )
    else:
        # Preserve the relative 35:25 weighting when occupancy is unavailable.
        weights = {"fill": 0.0, "message_count": 7 / 12, "output_input_ratio": 5 / 12}
        raw = msg_score * weights["message_count"] + oi_score * weights["output_input_ratio"]
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
        "fill_ratio": round(fill_ratio, 4) if fill_ratio is not None else None,
        "context_window_used": ctx_win,
        "context_source": context_source,
        "signals_active": list(ACTIVE_QUALITY_SIGNALS if fill_available else ACTIVE_QUALITY_SIGNALS[1:]),
        "signals_omitted": list(OMITTED_QUALITY_SIGNALS) + ([] if fill_available else ["context_fill"]),
        "signal_scores": {
            "fill": fill_score,
            "message_count": msg_score,
            "output_input_ratio": oi_score,
        },
        "signal_weights": weights,
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
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_session(row: dict[str, Any], *, context_tokens: int | None = None) -> dict[str, Any] | None:
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
    ctx_window = promote_window_for_observed_tokens(
        model, _context_window_for_model(model), context_tokens)

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

    # Quality score from available signals only. The session counters remain
    # useful for cost, but only the latest provider prompt can measure occupancy.
    quality = compute_quality_score(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        message_count=message_count,
        model=model,
        context_window=ctx_window,
        cache_read=cache_read,
        context_tokens=context_tokens,
    )

    # Topic: use title from Hermes if available. It is user text persisted to
    # session_log — redact credentials, or drop if the redactor refuses.
    title = str(row.get("title") or "").strip() or None
    if title:
        try:
            from credential_patterns import redact_credentials
            title = redact_credentials(title)
        except Exception:
            title = None

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
        "last_ts": _parse_ts(ended_at_raw),
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


# ---------------------------------------------------------------------------
# Checkpoint support: Hermes messages -> Claude-Code-shaped transcript records
# ---------------------------------------------------------------------------
#
# The shared checkpoint engine (measure.compact_capture) reads a Claude Code
# JSONL transcript. Hermes keeps its transcript in state.db instead, so we
# translate the message rows into the record shape that engine reads. Only the
# fields the engine looks at are produced: user text, assistant text, and
# tool_use blocks named like their Claude Code equivalents so file edits,
# reads, todos and delegated agents land in the right checkpoint sections.

# Progressive checkpoint fill bands and edit-batch thresholds. Mirrors of
# measure._PROGRESSIVE_BANDS / _EDIT_BATCH_*_THRESHOLD (pinned by a test) so
# the in-process Hermes plugin can decide when to checkpoint without importing
# the 60K-line measure module into the Hermes host.
PROGRESSIVE_BANDS = (20, 35, 50, 65, 80)
EDIT_BATCH_WRITE_THRESHOLD = 4
EDIT_BATCH_FILE_THRESHOLD = 3

# Hermes read_file's default line limit (tools/file_operations_common.py).
HERMES_DEFAULT_READ_LIMIT = 2000

_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.MULTILINE)


def _json_or(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            import json as _json

            return _json.loads(value)
        except ValueError:
            return default
    return default


def _text_of(content: Any) -> str:
    """Plain text of a Hermes message content (string or list of parts)."""
    parsed = _json_or(content, None) if isinstance(content, str) else content
    if isinstance(parsed, list):
        parts = []
        for part in parsed:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(p for p in parts if p)
    return content if isinstance(content, str) else ""


def hermes_tool_to_claude(name: str, args: Any) -> list[tuple[str, dict]]:
    """Map one Hermes tool call to Claude Code (name, input) pairs.

    Returns a list because one Hermes call can stand for several Claude Code
    calls (a multi-file patch, a delegate_task batch). Unknown tools keep their
    Hermes name, so the checkpoint engine simply ignores them.
    """
    args = args if isinstance(args, dict) else {}
    name = str(name or "")
    if name == "read_file":
        # Hermes: offset is 1-indexed (default 1), limit defaults to 2000 lines.
        # Claude Code: offset/limit absent (0) means "the whole file". Map the
        # Hermes defaults to absent so a plain read counts as a full read.
        inp: dict = {"file_path": str(args.get("path") or "")}
        offset = _safe_int(args.get("offset"), 0)
        limit = _safe_int(args.get("limit"), 0)
        if offset > 1:
            inp["offset"] = offset
        if limit > 0 and limit != HERMES_DEFAULT_READ_LIMIT:
            inp["limit"] = limit
        return [("Read", inp)]
    if name == "write_file":
        return [("Write", {"file_path": str(args.get("path") or "")})]
    if name == "patch":
        path = args.get("path")
        if isinstance(path, str) and path:
            return [("Edit", {"file_path": path})]
        body = args.get("patch")
        if isinstance(body, str):
            return [("Edit", {"file_path": p}) for p in _PATCH_FILE_RE.findall(body)]
        return []
    if name == "terminal":
        return [("Bash", {"command": str(args.get("command") or "")})]
    if name == "todo_list":
        todos = args.get("todos")
        return [("TodoWrite", {"todos": todos if isinstance(todos, list) else []})]
    if name == "delegate_task":
        tasks = args.get("tasks")
        if not isinstance(tasks, list):
            tasks = [args]
        out = []
        for task in tasks:
            if isinstance(task, dict):
                goal = str(task.get("goal") or "")
                out.append(("Agent", {"subagent_type": "delegate_task", "description": goal}))
        return out
    return [(name, args)]


def messages_to_transcript_records(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Hermes message rows into Claude-Code-shaped transcript records.

    ``tool`` and ``system`` rows are skipped: tool output is not task text and
    must not become the checkpoint's "active task". Never raises.
    """
    records: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            text = _text_of(msg.get("content"))
            if text.strip():
                records.append({"type": "user", "message": {"role": "user", "content": text}})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _text_of(msg.get("content"))
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for call in _json_or(msg.get("tool_calls"), []) or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else call
                tool_args = _json_or(fn.get("arguments"), fn.get("arguments") or {})
                for cc_name, cc_input in hermes_tool_to_claude(fn.get("name"), tool_args):
                    blocks.append({"type": "tool_use", "name": cc_name, "input": cc_input})
            if blocks:
                records.append({"type": "assistant", "message": {"role": "assistant", "content": blocks}})
    return records


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
