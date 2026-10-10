#!/usr/bin/env python3
"""GitHub Copilot session normalizer for Token Optimizer.

Converts raw session dicts from ``copilot_state.py`` (CLI plane) and
``copilot_vscode.py`` (VS Code plane) into TO's canonical session shape —
the keys measure.py, the savings engine, and the dashboard consume.

Token convention (the load-bearing math — see worked examples in
tests/test_copilot_session.py):

  The savings engine expects ``total_input_tokens`` = TOTAL billed input
  (fresh + cache_read + cache_write).  Copilot's token fields are
  community-reverse-engineered, and the two upstream conventions differ:

  - OpenAI-style usage (most Copilot models): ``inputTokens`` is the
    AGGREGATE prompt count and ``cacheReadTokens`` is a SUBSET of it.
    Rolling up again would double-count.
  - Anthropic-style usage: ``input_tokens`` is FRESH-ONLY with cache
    fields separate (the Hermes case, which once overstated savings).

  Detection heuristic: when ``cache_read > input`` the input field cannot
  be an aggregate (a subset can't exceed its superset), so the source is
  fresh-only and we roll up.  Otherwise we treat input as aggregate (the
  OpenAI convention Copilot's API surface follows).  This is conservative
  in the ambiguous zone: treating fresh-only data as aggregate UNDERSTATES
  cost/savings, never overstates (per the savings-copy mandate: lean
  understates).

Cost convention: platform-computed cost first, never
OpenAI list prices against Copilot sessions.

  - VS Code plane: ``copilotUsageNanoAiu`` is authoritative.
    1 AIU = 1e9 nanoAIU = 1 AI credit = $0.01 (June 2026 billing).
  - CLI plane: ``session.shutdown.totalPremiumRequests`` × the premium-
    request overage rate (default $0.04, override via
    TOKEN_OPTIMIZER_COPILOT_PREMIUM_RATE). The multiplier for premium
    models is already applied upstream in the count.
  - No cost data → cost_usd 0.0 with cost_source "copilot_no_cost_data"
    (never a fabricated estimate).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UNKNOWN_MODEL = "unknown"

# Per-model context windows AS SERVED BY COPILOT (June 2026). Copilot caps
# most models at 128K regardless of what the raw model supports elsewhere;
# OpenAI reasoning models are served with their 200K windows. Default for
# unknown models under Copilot is 128K — NOT the 1M
# Claude Code fallback, which suppressed fill nudges by 7-8x.
_COPILOT_DEFAULT_CONTEXT_WINDOW = 128_000
_CONTEXT_WINDOW_PREFIXES = (
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-4.1", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-5", 128_000),
    ("claude", 128_000),
    ("gemini", 128_000),
)

_NANO_AIU_PER_CREDIT = 1_000_000_000  # 1 AIU = 1e9 nanoAIU
_USD_PER_CREDIT_DEFAULT = 0.01
_USD_PER_PREMIUM_REQUEST_DEFAULT = 0.04


def context_window_for_model(model: str) -> int:
    """Context window for a Copilot-served model. 128K default (Q8)."""
    name = (model or "").strip().lower()
    for prefix, window in _CONTEXT_WINDOW_PREFIXES:
        if name.startswith(prefix):
            return window
    return _COPILOT_DEFAULT_CONTEXT_WINDOW


def _safe_topic(value: Any) -> Optional[str]:
    """Topic is user text persisted to session_log — redact or drop."""
    if not value:
        return None
    try:
        from credential_patterns import redact_credentials
        return redact_credentials(str(value))
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    # Contract: returns a numeric default on failure (cost math needs a number).
    # copilot_state._safe_float intentionally differs: returns None (timestamps).
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _env_rate(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        rate = float(raw)
        return rate if rate >= 0 else default
    except ValueError:
        return default


def usd_from_nano_aiu(nano_aiu: int) -> float:
    """Convert nanoAIU to USD: 1e9 nano = 1 credit = $0.01 (configurable)."""
    usd_per_credit = _env_rate("TOKEN_OPTIMIZER_COPILOT_USD_PER_CREDIT", _USD_PER_CREDIT_DEFAULT)
    return (max(0, nano_aiu) / _NANO_AIU_PER_CREDIT) * usd_per_credit


def usd_from_premium_requests(premium_requests: float) -> float:
    """Convert premium-request count to USD at the overage rate (configurable)."""
    rate = _env_rate("TOKEN_OPTIMIZER_COPILOT_PREMIUM_RATE", _USD_PER_PREMIUM_REQUEST_DEFAULT)
    return max(0.0, premium_requests) * rate


def resolve_billed_input(input_tokens: int, cache_read: int, cache_write: int) -> dict:
    """Resolve TOTAL billed input from possibly-ambiguous upstream fields.

    Returns {"total_input": int, "cache_read": int, "cache_write": int,
    "convention": "aggregate"|"fresh_rollup"}. See module docstring for the
    heuristic; all inputs are clamped non-negative first.
    """
    inp = max(0, input_tokens)
    cr = max(0, cache_read)
    cw = max(0, cache_write)
    # >= not >: when cache_read EQUALS input, input cannot be an aggregate that
    # contains it (a superset is strictly larger), so treat as fresh-only. The
    # > form misread cr==inp as 100% cache hit and halved the cost.
    if cr >= inp:
        # input cannot be an aggregate that contains cache_read → fresh-only.
        return {
            "total_input": inp + cr + cw,
            "cache_read": cr,
            "cache_write": cw,
            "convention": "fresh_rollup",
        }
    if cw <= inp:
        # OpenAI aggregate convention: inp already contains the cache fields.
        return {"total_input": inp, "cache_read": cr, "cache_write": cw, "convention": "aggregate"}
    # cw > inp means cache_write cannot be a subset of inp, so it is a separate
    # billed charge: inp + cw is the minimal consistent total (not a double
    # count). Distinct label so a downstream caller can spot the odd shape.
    return {"total_input": inp + cw, "cache_read": cr, "cache_write": cw, "convention": "aggregate_cw_separate"}


def _quality(input_tokens: int, output_tokens: int, message_count: int, model: str,
             ctx_window: int, cache_read: int) -> dict:
    """Quality score from the signals Copilot exposes.

    Reuses the runtime-agnostic scorer from hermes_session (same constrained-
    signal situation: session-level fields only). Falls back to a minimal
    fill-based score if that import ever breaks.
    """
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
        logger.debug("[copilot_session] quality scorer unavailable: %s", exc)
        fill = min(1.0, (input_tokens + cache_read) / ctx_window) if ctx_window else 0.0
        score = max(0.0, 100.0 - fill * 50.0)
        band = "healthy" if score >= 70 else ("watch" if score >= 50 else "critical")
        grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))
        # Mirror the real scorer's top-level keys so downstream consumers
        # (dashboard, tests) never KeyError on the degraded path.
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


def _parse_ts(value: Any) -> Optional[str]:
    """Epoch seconds → ISO-8601 UTC string (None when absent/invalid)."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _base_canonical(slug: str, runtime_source: str) -> dict:
    """Shared canonical skeleton with the placeholder keys measure.py checks."""
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
        "model_context_window": _COPILOT_DEFAULT_CONTEXT_WINDOW,
        "cache_hit_rate": 0.0,
        "cost_usd": 0.0,
        "cost_source": "copilot_no_cost_data",
        "credits": None,
        "model": _UNKNOWN_MODEL,
        "model_family": None,
        "model_usage": {},
        "model_usage_breakdown": {},
        "message_count": 0,
        "api_calls": 0,
        "tool_calls": {"total": 0},
        "estimated": False,
        "token_source": runtime_source,
        "runtime": "copilot",
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
        "billing_provider": "github-copilot",
        "incomplete": False,
    }


def normalize_cli_session(raw: dict) -> Optional[dict]:
    """Normalize a copilot_state.read_session() dict (CLI plane)."""
    if not raw:
        return None

    # isinstance guard, not `or {}`: a non-empty list is truthy and would crash
    # the .items() loop below with AttributeError.
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    output_observed = max(0, _safe_int(raw.get("output_tokens_observed")))
    message_count = _safe_int(raw.get("message_count"))

    # Aggregate across models with per-model convention resolution.
    total_input = 0
    total_output = 0
    total_cr = 0
    total_cw = 0
    api_calls = 0
    model_usage = {}
    model_usage_breakdown = {}
    estimated = False
    for model_name, entry in models.items():
        if not isinstance(entry, dict):
            continue
        resolved = resolve_billed_input(
            _safe_int(entry.get("input_tokens")),
            _safe_int(entry.get("cache_read_tokens")),
            _safe_int(entry.get("cache_write_tokens")),
        )
        out = max(0, _safe_int(entry.get("output_tokens")))
        total_input += resolved["total_input"]
        total_output += out
        total_cr += resolved["cache_read"]
        total_cw += resolved["cache_write"]
        api_calls += max(0, _safe_int(entry.get("api_calls")))
        billable = resolved["total_input"] + out
        key = str(model_name) or _UNKNOWN_MODEL
        model_usage[key] = billable
        # Cap cache_create so fresh+cache_read+cache_create == total_input. In
        # the aggregate convention cr+cw can exceed inp; without the cap the
        # breakdown would sum higher than model_usage and the dashboard would
        # show two conflicting totals for one session.
        _avail_for_cw = max(0, resolved["total_input"] - resolved["cache_read"])
        _cw = min(resolved["cache_write"], _avail_for_cw)
        model_usage_breakdown[key] = {
            "fresh_input": max(0, _avail_for_cw - _cw),
            "cache_read": resolved["cache_read"],
            "cache_create": _cw,
            "output": out,
        }

    # Crash/incomplete sessions may have NO model totals; fall back to the
    # persisted observations so the session still counts (honest "estimated").
    if total_input == 0 and total_output == 0:
        if output_observed == 0 and message_count == 0:
            return None  # genuinely empty session
        total_output = output_observed
        content_chars = max(0, _safe_int(raw.get("content_chars")))
        checkpoint_tokens = raw.get("checkpoint_tokens")
        if checkpoint_tokens is not None:
            total_input = max(0, _safe_int(checkpoint_tokens))
        elif content_chars:
            total_input = content_chars // 4  # chars/4 estimate, flagged below
        estimated = True

    # Primary model = highest billable volume.
    primary_model = _UNKNOWN_MODEL
    if model_usage:
        primary_model = max(model_usage, key=lambda k: model_usage[k])
    ctx_window = context_window_for_model(primary_model)

    # Cost: premium-request pass-through only (C5 — no list-price math).
    premium = raw.get("total_premium_requests")
    if premium is not None:
        cost_usd = usd_from_premium_requests(_safe_float(premium))
        cost_source = "copilot_premium_requests"
        credits = None
    else:
        cost_usd = 0.0
        cost_source = "copilot_no_cost_data"
        credits = None

    duration_minutes = 0.0
    st, et = raw.get("start_time"), raw.get("end_time")
    if st is not None and et is not None:
        try:
            duration_minutes = max(0.0, (float(et) - float(st)) / 60.0)
        except (TypeError, ValueError):
            duration_minutes = 0.0

    cache_hit_rate = (total_cr / total_input) if total_input > 0 else 0.0
    quality = _quality(total_input, total_output, message_count, primary_model, ctx_window, total_cr)

    session = _base_canonical(str(raw.get("session_id") or ""), "copilot_cli_events")
    session.update(
        {
            "first_ts": _parse_ts(st),
            "last_ts": _parse_ts(et),
            "duration_minutes": round(duration_minutes, 2),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read": total_cr,
            "total_cache_create": total_cw,
            "total_cache_create_1h": total_cw,
            "model_context_window": ctx_window,
            "cache_hit_rate": round(cache_hit_rate, 4),
            "cost_usd": round(cost_usd, 6),
            "cost_source": cost_source,
            "credits": credits,
            "model": primary_model,
            "model_usage": model_usage,
            "model_usage_breakdown": model_usage_breakdown,
            "message_count": message_count,
            "api_calls": api_calls,
            "tool_calls": {"total": len(raw.get("tool_calls") or [])},
            "estimated": estimated,
            "version": raw.get("version"),
            "cwd": raw.get("cwd"),
            "incomplete": not bool(raw.get("complete")),
            "end_reason": raw.get("incomplete_reason") or ("shutdown" if raw.get("complete") else ""),
            "quality": quality,
            "quality_score": quality.get("score"),
            "quality_grade": quality.get("grade"),
            "quality_band": quality.get("band"),
        }
    )
    return session


def normalize_vscode_session(raw: dict) -> Optional[dict]:
    """Normalize a copilot_vscode.read_sessions() dict (VS Code plane)."""
    if not raw:
        return None
    totals = raw.get("totals") or {}
    requests = raw.get("requests") or []

    input_tokens = max(0, _safe_int(totals.get("input_tokens")))
    output_tokens = max(0, _safe_int(totals.get("output_tokens")))
    cached = max(0, _safe_int(totals.get("cached_tokens")))
    nano_aiu = totals.get("nano_aiu")
    request_count = max(0, _safe_int(totals.get("requests"), len(requests)))

    if input_tokens == 0 and output_tokens == 0 and request_count == 0:
        return None

    resolved = resolve_billed_input(input_tokens, cached, 0)
    total_input = resolved["total_input"]

    # Per-model split from requests.
    model_usage = {}
    model_usage_breakdown = {}
    ttft_values = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        model = str(req.get("model") or _UNKNOWN_MODEL)
        r = resolve_billed_input(
            _safe_int(req.get("input_tokens")),
            _safe_int(req.get("cached_tokens")),
            0,
        )
        out = max(0, _safe_int(req.get("output_tokens")))
        model_usage[model] = model_usage.get(model, 0) + r["total_input"] + out
        slot = model_usage_breakdown.setdefault(
            model, {"fresh_input": 0, "cache_read": 0, "cache_create": 0, "output": 0}
        )
        slot["fresh_input"] += max(0, r["total_input"] - r["cache_read"])
        slot["cache_read"] += r["cache_read"]
        slot["output"] += out
        ttft = req.get("ttft_ms")
        if ttft is not None:
            ttft_values.append(_safe_float(ttft))

    primary_model = max(model_usage, key=lambda k: model_usage[k]) if model_usage else _UNKNOWN_MODEL
    ctx_window = context_window_for_model(primary_model)

    # Cost: nanoAIU pass-through is authoritative (C5).
    if nano_aiu is not None:
        nano = max(0, _safe_int(nano_aiu))
        cost_usd = usd_from_nano_aiu(nano)
        credits = nano / _NANO_AIU_PER_CREDIT
        cost_source = "copilot_nano_aiu"
    else:
        cost_usd = 0.0
        credits = None
        cost_source = "copilot_no_cost_data"

    cache_hit_rate = (resolved["cache_read"] / total_input) if total_input > 0 else 0.0
    # One user message typically drives one llm_request; summing them would
    # ~2x the turn count and depress message-density quality signals. Take the
    # larger of the two as the meaningful turn count.
    user_messages = max(0, _safe_int(raw.get("user_messages")))
    message_count = max(user_messages, request_count)
    quality = _quality(total_input, output_tokens, message_count, primary_model, ctx_window, resolved["cache_read"])

    session = _base_canonical(
        f"vscode-{raw.get('workspace_hash', '')[:8]}-{raw.get('session_id', '')}",
        str(raw.get("data_source") or "copilot_vscode_debuglogs"),
    )
    session.update(
        {
            "topic": _safe_topic(raw.get("title")),
            "first_ts": _parse_ts(raw.get("first_ts_epoch")),
            "total_input_tokens": total_input,
            "total_output_tokens": output_tokens,
            "total_cache_read": resolved["cache_read"],
            "model_context_window": ctx_window,
            "cache_hit_rate": round(cache_hit_rate, 4),
            "cost_usd": round(cost_usd, 6),
            "cost_source": cost_source,
            "credits": round(credits, 4) if credits is not None else None,
            "model": primary_model,
            "model_usage": model_usage,
            "model_usage_breakdown": model_usage_breakdown,
            "message_count": message_count,
            "api_calls": request_count,
            "tool_calls": {"total": len(raw.get("tool_calls") or [])},
            "ttft_ms_avg": round(sum(ttft_values) / len(ttft_values), 1) if ttft_values else None,
            "quality": quality,
            "quality_score": quality.get("score"),
            "quality_grade": quality.get("grade"),
            "quality_band": quality.get("band"),
        }
    )
    return session


def normalize_session(raw: dict) -> Optional[dict]:
    """Dispatch by data_source. The two planes are separate session
    populations and are NEVER merged or summed."""
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("data_source") or "")
    if source.startswith("copilot_vscode"):
        return normalize_vscode_session(raw)
    return normalize_cli_session(raw)
