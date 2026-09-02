#!/usr/bin/env python3
"""Google Antigravity session normalizer for Token Optimizer.

Converts a raw session dict from ``antigravity_state.read_conversation()`` into
Token Optimizer's canonical session shape (the keys ``measure.py``, the savings
engine, and the dashboard consume). Mirrors ``copilot_session`` but keeps the
module stdlib-only at import time (KTD12: it must NEVER import ``measure``,
because ``measure.py`` imports this module at its own top level).

Token convention (verified against real ``gen_metadata`` rows, 2026-09-01):
Antigravity's ``input_tokens`` is FRESH-ONLY and ``cache_read_tokens`` is a
separate billed quantity (a row can be input=7556 with cache_read=24462), so
the total billed input is ALWAYS the rollup ``fresh + cache_read`` — never the
aggregate+subset heuristic Copilot needs. This is the Hermes convention.

Cost convention (R8): cost is Antigravity's own credit figure when
``consumed_credits``/``credit_cost`` decode non-zero; otherwise a USD list-price
ESTIMATE priced by the collector through ``measure._get_model_cost`` for a
recognized model, and ``antigravity_no_cost_data`` for an unknown model. The
normalizer only classifies: it returns ``model_id`` (or None) and ``credits``
(or None); the collector in ``measure.py`` does the pricing (KTD12).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UNKNOWN_MODEL = "unknown"

# Antigravity-served Gemini default context window (observed across all decoded
# rows on the reference machine: max_context_tokens=256000). Used only when the
# last generation carries no max_context_tokens.
_DEFAULT_CONTEXT_WINDOW = 256_000

# Recognized Gemini rate-card families (mirrors GEMINI_MODEL_PRICING keys in
# measure.py so a display name like "Gemini 3.5 Flash (Medium)" maps to a priced
# id and "Gemini 3.7 Flash (High)" — not yet on the card — maps to None).
_GEMINI_CARD_IDS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro",
    "gemini-3-flash",
    "gemini-3-pro",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def model_id_for_display_name(name: str) -> Optional[str]:
    """Map an Antigravity model display name to a priced model id, or None.

    Lowercases, strips the parenthesised effort suffix ("(Medium)","(High)"),
    collapses non-alphanumerics to hyphens, then recognizes Gemini rate-card
    families and Claude-prefixed names. Unknown → None.
    """
    if not name:
        return None
    s = re.sub(r"\s*\([^)]*\)", "", str(name)).strip().lower()
    s = re.sub(r"[^a-z0-9.]+", "-", s).strip("-")
    if not s:
        return None
    if s.startswith("gemini-"):
        for card_id in _GEMINI_CARD_IDS:
            if s == card_id or s.startswith(card_id + "-"):
                return card_id
        return None
    if "claude" in s or "sonnet" in s or "opus" in s or "haiku" in s:
        return s
    return None


def context_window_for_model(model_id: str) -> int:
    """Context window for an Antigravity-served model id (256K default)."""
    if model_id and model_id.lower().startswith("gemini-2.5"):
        return 1_000_000
    return _DEFAULT_CONTEXT_WINDOW


def _parse_ts(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _quality(
    input_tokens: int,
    output_tokens: int,
    message_count: int,
    model: str,
    ctx_window: int,
    cache_read: int,
) -> dict:
    """Three-signal quality score (same constrained-signal path as Hermes/Copilot)."""
    try:
        from hermes_session import compute_quality_score

        return compute_quality_score(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            message_count=message_count,
            model=model,
            context_window=ctx_window,
            cache_read=cache_read,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.debug("[antigravity_session] quality scorer unavailable: %s", exc)
        fill = min(1.0, (input_tokens + cache_read) / ctx_window) if ctx_window else 0.0
        score = max(0.0, 100.0 - fill * 50.0)
        band = "healthy" if score >= 70 else ("watch" if score >= 50 else "critical")
        if score >= 90:
            grade = "A"
        elif score >= 75:
            grade = "B"
        elif score >= 60:
            grade = "C"
        else:
            grade = "D"
        return {
            "score": round(score, 1),
            "grade": grade,
            "band": band,
            "fill_ratio": round(fill, 4),
            "context_window_used": ctx_window,
            "signals": {"fill": round(fill, 4)},
            "signal_scores": {"fill": round(fill * 100, 1)},
            "signals_active": ["context_fill"],
            "signals_omitted": [],
            "estimated": True,
        }


def _base_canonical(slug: str) -> dict:
    return {
        "slug": slug,
        "topic": None,
        "first_ts": None,
        "duration_minutes": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_create": 0,
        "total_cache_create_1h": 0,
        "total_cache_create_5m": 0,
        "model_context_window": _DEFAULT_CONTEXT_WINDOW,
        "cache_hit_rate": 0.0,
        "cost_usd": 0.0,
        "cost_source": "antigravity_no_cost_data",
        "credits": None,
        "model": _UNKNOWN_MODEL,
        "model_id": None,
        "model_family": None,
        "model_usage": {},
        "model_usage_breakdown": {},
        "message_count": 0,
        "api_calls": 0,
        "tool_calls": {"total": 0},
        "estimated": False,
        "token_source": "antigravity_gen_metadata",
        "runtime": "antigravity",
        "billing_provider": "google-antigravity",
        "version": None,
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
        "end_reason": "",
        "archived": False,
        "cwd": None,
        "incomplete": False,
        "surface": None,
        "dedup_key": None,
    }


def normalize_session(raw: dict) -> Optional[dict]:
    """Normalize an ``antigravity_state`` raw session dict, or None when empty."""
    if not isinstance(raw, dict) or not raw:
        return None

    input_tokens = max(0, _safe_int(raw.get("input_tokens")))
    output_tokens = max(0, _safe_int(raw.get("output_tokens")))
    cache_read = max(0, _safe_int(raw.get("cache_read_tokens")))
    thinking_tokens = max(0, _safe_int(raw.get("thinking_tokens")))
    user_inputs = max(0, _safe_int(raw.get("user_input_count")))
    tool_calls = max(0, _safe_int(raw.get("tool_call_count")))
    generations = (
        raw.get("generations") if isinstance(raw.get("generations"), list) else []
    )

    if len(generations) == 0 and user_inputs == 0:
        return None

    # Gemini input is fresh-only (cache_read is a separate billed quantity), so
    # the billed input is the rollup. cache_write is not exposed by Antigravity.
    total_input = input_tokens + cache_read
    total_output = output_tokens
    total_cache_read = cache_read
    total_cache_create = 0

    display_name = str(raw.get("model_display_name") or "")
    model_id = model_id_for_display_name(display_name)
    model = model_id or display_name or _UNKNOWN_MODEL

    ctx_window = _safe_int(raw.get("last_max_context")) or context_window_for_model(
        model_id
    )

    # Cost classification (pricing itself is the collector's job, KTD12).
    credit_cost = max(0, _safe_int(raw.get("credit_cost")))
    consumed_credits = max(0, _safe_int(raw.get("consumed_credits")))
    credits_val = consumed_credits or credit_cost or None
    if credits_val is not None:
        cost_usd = 0.0
        cost_source = "antigravity_credits"
        credits = credits_val
    elif model_id is not None:
        cost_usd = 0.0
        cost_source = "antigravity_list_price_estimate"
        credits = None
    else:
        cost_usd = 0.0
        cost_source = "antigravity_no_cost_data"
        model = display_name or _UNKNOWN_MODEL
        credits = None

    surface = str(raw.get("surface") or "")
    conversation_id = str(raw.get("conversation_id") or "")
    dedup_key = (
        f"antigravity:{surface}:{conversation_id}"
        if surface and conversation_id
        else None
    )

    st = raw.get("start_time")
    et = raw.get("end_time")
    duration_minutes = 0.0
    if st is not None and et is not None:
        try:
            duration_minutes = max(0.0, (float(et) - float(st)) / 60.0)
        except (TypeError, ValueError):
            duration_minutes = 0.0

    cache_hit_rate = (total_cache_read / total_input) if total_input > 0 else 0.0
    quality = _quality(
        total_input, total_output, user_inputs, model, ctx_window, total_cache_read
    )

    killed = bool(raw.get("killed"))
    not_fully_idle = bool(raw.get("not_fully_idle"))
    end_reason = "killed" if killed else ("not_fully_idle" if not_fully_idle else "")

    # Per-model usage: single model id for the whole conversation (highest
    # volume was resolved upstream in antigravity_state.model_display_name).
    billable = total_input + total_output
    model_usage = {model: billable} if model not in ("", _UNKNOWN_MODEL) else {}
    model_usage_breakdown = {}
    if model_usage:
        model_usage_breakdown[model] = {
            "fresh_input": total_input - total_cache_read,
            "cache_read": total_cache_read,
            "cache_create": 0,
            "output": total_output,
        }

    session = _base_canonical(str(conversation_id))
    session.update(
        {
            "topic": raw.get("title") or None,
            "first_ts": _parse_ts(st),
            "duration_minutes": round(duration_minutes, 2),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read": total_cache_read,
            "total_cache_create": total_cache_create,
            "total_cache_create_1h": total_cache_create,
            "model_context_window": ctx_window,
            "cache_hit_rate": round(cache_hit_rate, 4),
            "cost_usd": round(cost_usd, 6),
            "cost_source": cost_source,
            "credits": credits,
            "model": model,
            "model_id": model_id,
            "model_usage": model_usage,
            "model_usage_breakdown": model_usage_breakdown,
            "message_count": user_inputs,
            "api_calls": len(generations),
            "tool_calls": {"total": tool_calls},
            "estimated": cost_source == "antigravity_list_price_estimate"
            or cost_source == "antigravity_credits",
            "cwd": raw.get("workspace"),
            "incomplete": killed or not_fully_idle,
            "end_reason": end_reason,
            "surface": surface,
            "dedup_key": dedup_key,
            "thinking_tokens": thinking_tokens,
            "quality": quality,
            "quality_score": quality.get("score"),
            "quality_grade": quality.get("grade"),
            "quality_band": quality.get("band"),
        }
    )
    return session
