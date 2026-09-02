#!/usr/bin/env python3
"""Grok Build session normalizer for Token Optimizer.

Converts raw session dicts from ``grok_state.py`` into TO's canonical session
shape — the keys measure.py, the savings engine, and the dashboard consume.

Token convention (source-verified from the cloned repo):

  ``updates.jsonl`` ``turn_completed`` records carry ``usage`` (``PromptUsage``)
  whose ``inputTokens`` is the FULL prompt sum including cache reads (the ACP
  identity, notification.rs). So:

    total_input_tokens = sum(inputTokens)        # already includes cache reads
    total_output_tokens = sum(outputTokens)
    total_cache_read    = sum(cachedReadTokens)  # subset of input, never re-added
    total_cache_create  = sum(cacheCreationTokens)

  When no ``turn_completed`` usage exists (crash before first turn end), fall
  back to ``signals.json`` ``contextTokensUsed`` as a single input estimate
  (``estimated = True``, ``token_source = "grok_signals_only"``). A session is
  NEVER dropped for lacking tokens.

Cost convention: ``costUsdTicks`` is 1e10 ticks per USD (notification.rs), and
is scrubbed (absent) when ``usageIsIncomplete`` or ``costIsPartial``. Absence of
cost means unknown, not free — so cost_usd is 0.0 with
``cost_source = "grok_no_cost_data"`` unless trustworthy ticks were summed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UNKNOWN_MODEL = "unknown"

# Grok Build's default context window (crates/codegen/xai-grok-shell/src/
# remote/client.rs: DEFAULT_CONTEXT_WINDOW = 256_000). signals.json's
# ``contextWindowTokens`` is authoritative when present; this is the fallback.
_GROK_DEFAULT_CONTEXT_WINDOW = 256_000

# USD ticks per dollar (notification.rs: "costUsdTicks is 1e10 ticks per $1").
_USD_TICKS_PER_USD = 10_000_000_000


def _default_context_window() -> int:
    """Grok Build's fixed 256K context-window fallback (remote/client.rs).

    signals.json's ``contextWindowTokens`` is authoritative when present; this
    is only the fallback. Grok does not expose a per-model window map to the
    adapter in contract-only mode, so there is no per-model lookup here (the
    copilot/hermes ``context_window_for_model`` names would overclaim one).
    """
    return _GROK_DEFAULT_CONTEXT_WINDOW


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> Optional[str]:
    """Normalize a timestamp to an ISO-8601 UTC string.

    summary.json timestamps are RFC3339 strings (chrono ``DateTime<Utc>``);
    hook/signals timestamps may be epoch seconds. Returns None when absent or
    unparseable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            return None
    return None


def _quality(input_tokens, output_tokens, message_count, model, ctx_window, cache_read):
    """Quality score from Grok's session-level fields (mirrors copilot_session)."""
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
        logger.debug("[grok_session] quality scorer unavailable: %s", exc)
        fill = min(1.0, (input_tokens + cache_read) / ctx_window) if ctx_window else 0.0
        score = max(0.0, 100.0 - fill * 50.0)
        band = "healthy" if score >= 70 else ("watch" if score >= 50 else "critical")
        grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))
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


def _base_canonical(slug: str, token_source: str) -> dict:
    """Shared canonical skeleton shaped like copilot_session._base_canonical."""
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
        "model_context_window": _GROK_DEFAULT_CONTEXT_WINDOW,
        "cache_hit_rate": 0.0,
        "cost_usd": 0.0,
        "cost_source": "grok_no_cost_data",
        "credits": None,
        "model": _UNKNOWN_MODEL,
        "model_family": None,
        "model_usage": {},
        "model_usage_breakdown": {},
        "message_count": 0,
        "api_calls": 0,
        "tool_calls": {"total": 0},
        "estimated": False,
        "token_source": token_source,
        "runtime": "grok",
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
        "billing_provider": "grok",
        "incomplete": False,
    }


def normalize_session(raw: dict) -> Optional[dict]:
    """Normalize a grok_state.read_session() dict into the canonical shape."""
    if not isinstance(raw, dict):
        return None
    session_id = str(raw.get("session_id") or "")
    if not session_id:
        return None

    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    signals = raw.get("signals") if isinstance(raw.get("signals"), dict) else {}

    # Token resolution: authoritative turn_completed usage first, then the
    # signals contextTokensUsed estimate, then a zero-token row (never dropped).
    turns = _safe_int(usage.get("turns"))
    total_input = _safe_int(usage.get("input_tokens"))
    total_output = _safe_int(usage.get("output_tokens"))
    total_cache_read = _safe_int(usage.get("cache_read_tokens"))
    total_cache_create = _safe_int(usage.get("cache_create_tokens"))
    usage_incomplete = bool(usage.get("usage_incomplete"))

    if turns > 0 or total_input > 0 or total_output > 0:
        token_source = "grok_updates_jsonl"
        estimated = usage_incomplete  # an incomplete bill under-counts
    else:
        ctx_used = _safe_int(signals.get("contextTokensUsed"))
        total_input = ctx_used
        total_output = 0
        token_source = "grok_signals_only"
        estimated = True

    # Per-model usage: billable = input + output per model (usage.modelUsage).
    model_usage = {}
    model_usage_breakdown = {}
    raw_model_usage = usage.get("model_usage") if isinstance(usage.get("model_usage"), dict) else {}
    for model, row in raw_model_usage.items():
        if not isinstance(row, dict):
            continue
        inp = _safe_int(row.get("input_tokens"))
        out = _safe_int(row.get("output_tokens"))
        cr = _safe_int(row.get("cache_read_tokens"))
        cc = _safe_int(row.get("cache_create_tokens"))
        key = str(model) or _UNKNOWN_MODEL
        model_usage[key] = inp + out
        model_usage_breakdown[key] = {
            "fresh_input": max(0, inp - cr),
            "cache_read": cr,
            "cache_create": cc,
            "output": out,
        }

    # Primary model: signals.primaryModelId, else summary.currentModelId, else
    # the highest-billable model from usage.
    model = signals.get("primaryModelId") or raw.get("model_id") or _UNKNOWN_MODEL
    if model == _UNKNOWN_MODEL and model_usage:
        model = max(model_usage, key=lambda k: model_usage[k])
    model = str(model) if isinstance(model, str) and model else _UNKNOWN_MODEL

    ctx_window = _safe_int(signals.get("contextWindowTokens")) or _default_context_window()

    # Cost: trustworthy ticks only (scrub rule in notification.rs).
    ticks = _safe_int(usage.get("cost_usd_ticks"))
    if ticks > 0 and not usage_incomplete:
        cost_usd = ticks / _USD_TICKS_PER_USD
        cost_source = "grok_cost_usd_ticks"
    else:
        cost_usd = 0.0
        cost_source = "grok_no_cost_data"

    # Duration: updatedAt - createdAt (RFC3339), else sessionDurationSeconds.
    duration_minutes = 0.0
    created = raw.get("created_at")
    updated = raw.get("updated_at")
    if isinstance(created, str) and isinstance(updated, str):
        try:
            c = datetime.fromisoformat(created.replace("Z", "+00:00"))
            u = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            duration_minutes = max(0.0, (u - c).total_seconds() / 60.0)
        except (ValueError, TypeError):
            duration_minutes = 0.0
    if duration_minutes == 0.0:
        duration_minutes = _safe_int(signals.get("sessionDurationSeconds")) / 60.0

    message_count = _safe_int(raw.get("num_messages")) or _safe_int(raw.get("num_chat_messages"))
    tool_calls_total = _safe_int(signals.get("toolCallCount"))
    compactions = _safe_int(signals.get("compactionCount"))

    cache_hit_rate = (total_cache_read / total_input) if total_input > 0 else 0.0
    quality = _quality(total_input, total_output, message_count, model, ctx_window, total_cache_read)

    session = _base_canonical(session_id, token_source)
    session.update(
        {
            "topic": raw.get("title"),
            "first_ts": _parse_ts(created),
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
            "model": model,
            "model_usage": model_usage,
            "model_usage_breakdown": model_usage_breakdown,
            "message_count": message_count,
            "api_calls": turns,
            "tool_calls": {"total": tool_calls_total},
            "estimated": estimated,
            "version": raw.get("agent_name"),
            "cwd": raw.get("cwd"),
            "incomplete": usage_incomplete,
            "end_reason": "usage_incomplete" if usage_incomplete else "",
            "compactions": compactions,
            "quality": quality,
            "quality_score": quality.get("score"),
            "quality_grade": quality.get("grade"),
            "quality_band": quality.get("band"),
        }
    )
    return session
