#!/usr/bin/env python3
"""Cursor session normalizer for Token Optimizer.

Converts a Cursor hook tally (plus resolved token sources from
``cursor_state.py``) into TO's canonical session shape — the keys measure.py,
the savings engine, and the dashboard consume.

Token convention (R16, load-bearing): token counts come from, in order:

  1. ``state.vscdb`` bubble ``tokenCount`` when non-zero (``estimated = False``,
     ``token_source = "cursor_state_vscdb"``);
  2. else a chars-over-four estimate from the transcript
     (``estimated = True``, ``token_source = "cursor_transcript_estimate"``);
  3. else zero tokens, still a valid row (``estimated = True``,
     ``token_source = "cursor_tally_only"``).

A tallied session is NEVER dropped for lacking tokens; the hook tally is the
primary record (tool calls, turns, compaction context numbers), and the token
sources only improve it best-effort.

Cost convention (R18): Cursor stores no local cost data, so cost_usd is always
0.0 with ``cost_source = "cursor_no_cost_data"`` and the token-priced
before/after transformation is not rendered under the cursor runtime.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UNKNOWN_MODEL = "unknown"

# Cursor serves a mixed model fleet; there is no single documented context
# window. 128K is the honest conservative default (mirrors copilot_session).
_CURSOR_DEFAULT_CONTEXT_WINDOW = 128_000
_CONTEXT_WINDOW_PREFIXES = (
    ("gpt-5", 128_000),
    ("gpt-4.1", 128_000),
    ("gpt-4o", 128_000),
    ("claude", 200_000),
    ("gemini", 128_000),
    ("o3", 200_000),
    ("o4", 200_000),
)


def surface_from_version(version: Any) -> str:
    """Heuristic: date-shaped cursor_version means CLI, semver means IDE.

    The CLI bundle versions look like ``2026.08.31-4057e58`` (leading year);
    IDE versions are semver like ``3.18.9``. Returns "cli", "ide", or "unknown".
    """
    if not isinstance(version, str) or not version:
        return "unknown"
    text = version.strip()
    # Date-shaped: YYYY.MM.DD or YYYY.M.D anywhere in the leading token.
    import re

    if re.match(r"^\d{4}[.-]\d{1,2}[.-]\d{1,2}", text):
        return "cli"
    if re.match(r"^\d+\.\d+\.\d+", text):
        return "ide"
    return "unknown"


def context_window_for_model(model: str) -> int:
    name = (model or "").strip().lower()
    for prefix, window in _CONTEXT_WINDOW_PREFIXES:
        if name.startswith(prefix):
            return window
    return _CURSOR_DEFAULT_CONTEXT_WINDOW


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        # H-1/N-1b: parse through float() first so float-shaped strings from
        # JSON (e.g. "1234.0") don't raise ValueError and silently zero token
        # counts, and so float('inf') raises OverflowError (caught below)
        # instead of escaping.
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_ts(value: Any) -> Optional[str]:
    """Epoch seconds -> ISO-8601 UTC string (None when absent/invalid)."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _quality(input_tokens, output_tokens, message_count, model, ctx_window):
    """Quality score from Cursor's session-level fields (mirrors copilot_session)."""
    try:
        from hermes_session import compute_quality_score

        return compute_quality_score(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            message_count=message_count,
            model=model,
            context_window=ctx_window,
            cache_read=0,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.debug("[cursor_session] quality scorer unavailable: %s", exc)
        fill = min(1.0, input_tokens / ctx_window) if ctx_window else 0.0
        score = max(0.0, 100.0 - fill * 50.0)
        band = "healthy" if score >= 70 else ("watch" if score >= 50 else "critical")
        grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))
        return {
            "score": round(score, 1),
            "grade": grade,
            "band": band,
            "fill_ratio": round(fill, 4),
            "context_window_used": ctx_window,
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
        "model_context_window": _CURSOR_DEFAULT_CONTEXT_WINDOW,
        "cache_hit_rate": 0.0,
        "cost_usd": 0.0,
        "cost_source": "cursor_no_cost_data",
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
        "runtime": "cursor",
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
        "billing_provider": "cursor",
        "incomplete": False,
        "compactions": 0,
        "surface": "unknown",
    }


def resolve_token_source(bubble_tokens, transcript_tokens, hook_measured=None):
    """R16 ordered token resolution.

    Returns ``(input_tokens, output_tokens, token_source, estimated)``.
    Bubble tokens win only when at least one of input/output is non-zero;
    then the transcript estimate; otherwise a zero-token tally-only row.

    ``hook_measured`` is accepted for signature stability but QUARANTINED:
    afterAgentResponse usage numbers are stored in the tally's
    ``hook_measured_tokens`` bucket for offline correlation only. Whether
    they are per-response or cumulative is unproven, so they must never be
    summed into resolved session tokens; claiming them as trusted fill
    (``estimated = False``) would be a premature candidate claim.
    """
    if isinstance(bubble_tokens, dict) and (
        _safe_int(bubble_tokens.get("input_tokens")) > 0
        or _safe_int(bubble_tokens.get("output_tokens")) > 0
    ):
        return (
            _safe_int(bubble_tokens.get("input_tokens")),
            _safe_int(bubble_tokens.get("output_tokens")),
            "cursor_state_vscdb",
            False,
        )
    if transcript_tokens is not None:
        try:
            tt = max(0, int(transcript_tokens))
        except (TypeError, ValueError):
            tt = 0
        return (tt, 0, "cursor_transcript_estimate", True)
    return (0, 0, "cursor_tally_only", True)


def normalize_session(raw: dict) -> Optional[dict]:
    """Normalize a Cursor tally (+ resolved token sources) into a canonical dict.

    ``raw`` is the tally dict with (optionally) two injected keys from the
    collector: ``bubble_tokens`` (dict from cursor_state.read_state_vscdb_tokens)
    and ``transcript_tokens`` (int from cursor_state.transcript_estimate).
    """
    if not isinstance(raw, dict):
        return None
    conversation_id = str(raw.get("conversation_id") or raw.get("session_id") or "")
    if not conversation_id:
        return None

    bubble_tokens = raw.get("bubble_tokens")
    transcript_tokens = raw.get("transcript_tokens")
    total_input, total_output, token_source, estimated = resolve_token_source(
        bubble_tokens, transcript_tokens
    )

    # Primary model: bubble model first, else the tally's most-seen model.
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    model = _UNKNOWN_MODEL
    if isinstance(bubble_tokens, dict) and bubble_tokens.get("model"):
        model = str(bubble_tokens["model"])
    elif models:
        model = str(max(models, key=lambda k: _safe_int(models.get(k))))
    ctx_window = context_window_for_model(model)

    turns = _safe_int(raw.get("turns"))
    tool_calls_total = _safe_int(raw.get("tool_calls"))
    tool_names = raw.get("tool_names") if isinstance(raw.get("tool_names"), dict) else {}
    message_count = turns

    model_usage = {}
    for name, count in models.items():
        try:
            model_usage[str(name)] = _safe_int(count)
        except (TypeError, ValueError):
            continue

    compactions = raw.get("compactions")
    compactions_count = len(compactions) if isinstance(compactions, list) else 0

    first_ts_raw = raw.get("first_ts")
    updated_at_raw = raw.get("updated_at")
    duration_minutes = 0.0
    if first_ts_raw is not None and updated_at_raw is not None:
        try:
            duration_minutes = max(0.0, (float(updated_at_raw) - float(first_ts_raw)) / 60.0)
        except (TypeError, ValueError):
            duration_minutes = 0.0

    final = bool(raw.get("final"))
    end_reason = str(raw.get("end_reason") or "")
    # A tally finalized as idle is complete for dashboard purposes; a final
    # tally from sessionEnd is complete; anything else is in-progress.
    incomplete = not final

    version = raw.get("cursor_version")
    surface = surface_from_version(version)

    roots = raw.get("workspace_roots")
    roots_list = roots if isinstance(roots, list) else None
    root = None
    if isinstance(raw.get("cwd"), str) and raw["cwd"]:
        root = raw["cwd"]
    elif roots_list and isinstance(roots_list[0], str):
        root = roots_list[0]

    quality = _quality(total_input, total_output, message_count, model, ctx_window)

    session = _base_canonical(conversation_id, token_source)
    session.update(
        {
            "first_ts": _parse_ts(first_ts_raw),
            "last_ts": _parse_ts(updated_at_raw),
            "duration_minutes": round(duration_minutes, 2),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "model_context_window": ctx_window,
            "model": model,
            "model_usage": model_usage,
            "message_count": message_count,
            "api_calls": turns,
            "tool_calls": {"total": tool_calls_total},
            "estimated": estimated,
            "token_source": token_source,
            "version": version,
            "cwd": root,
            "workspace_roots": roots_list,
            "incomplete": incomplete,
            "end_reason": end_reason,
            "compactions": compactions_count,
            "surface": surface,
            "tool_names": tool_names,
            "quality": quality,
            "quality_score": quality.get("score"),
            "quality_grade": quality.get("grade"),
            "quality_band": quality.get("band"),
        }
    )
    return session
