#!/usr/bin/env python3
"""Cursor quality uses the shared v3 scorer with explicit provenance.

Cursor's pre-compaction occupancy is historical, not current fill. Without a
current occupancy reading the score is unmeasured. Existing per-signal data
is retained as diagnostics and does not affect the composite score.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Base-signal relative weights, identical to hermes_session.compute_quality_score.
_BASE_WEIGHTS = {
    "context_fill": 0.40,
    "message_count": 0.35,
    "output_input_ratio": 0.25,
}
# Deeper Cursor signals; when any is active the base family scales by 0.6 and
# these take their 0.4 family share (Claude resource/efficiency split, ported).
_EXTRA_WEIGHTS = {
    "compaction_depth": 0.15,
    "agent_efficiency": 0.15,
    "decision_density": 0.10,
}
_BASE_FAMILY_SCALE_WITH_EXTRAS = 0.6

# Signals unavailable from current persisted Cursor data, with the evidence.
# If a future Cursor version persists a real source, the signal can activate.
_NEVER_AVAILABLE = {
    "stale_reads": (
        "read-cache events are not aggregated into this scorer, and current "
        "persisted Cursor data carries no reread evidence to measure"
    ),
    "bloated_results": (
        "Cursor disk transcripts never persist tool outputs (bundle-proven "
        "writer 2026.11.05) and hook tool_output is consumed live only"
    ),
    "duplicates": (
        "Cursor's transcript writer strips system-reminder content; there is "
        "no reminder channel to measure"
    ),
}

# Rolling-window + floor for decision density, ported from the Claude scorer
# (measure.py _QUALITY_ROLLING_WINDOW / _DENSITY_MIN_MESSAGES defaults).
_DENSITY_WINDOW = 20
_DENSITY_MIN_MESSAGES = 6

_NO_TOOL_RESULTS_CAVEAT = (
    "Cursor disk transcripts carry no tool results (bundle-proven writer "
    "2026.11.05); recorded failures reflect wired-hook coverage, never proof "
    "of clean runs."
)
_HISTORICAL_FILL_CAVEAT = (
    "preCompact context numbers are the host's occupancy at compaction time "
    "(historical), reported as metadata only and never graded; current "
    "occupancy is unknown until a fresh host snapshot exists."
)

# Terminal statuses on a disk turn_ended line that count as failure evidence.
_TERMINAL_ERROR_STATUSES = ("error", "aborted", "abandoned", "failed")


# ---------------------------------------------------------------------------
# Banding tables - exact mirrors of hermes_session.compute_quality_score
# ---------------------------------------------------------------------------


def _message_score(count: int) -> int:
    if count <= 20:
        return 100
    if count <= 40:
        return 80
    if count <= 60:
        return 55
    if count <= 100:
        return 30
    return 10


def _oi_score(ratio: float) -> int:
    if ratio >= 0.05:
        return 100
    if ratio >= 0.02:
        return 70
    if ratio >= 0.01:
        return 40
    return 15


def _compaction_depth_score(count: int, latest_fill_ratio: Optional[float]) -> int:
    """Frequency + heat of compactions. A single shallow compaction is fine."""
    score = 100 - 15 * max(0, count - 1)
    if latest_fill_ratio is not None:
        if latest_fill_ratio >= 0.85:
            score -= 20
        elif latest_fill_ratio >= 0.70:
            score -= 10
    return max(10, score)


def _agent_efficiency_score(failures: int, tool_calls: int, terminal_errors: int) -> int:
    rate = failures / tool_calls if tool_calls > 0 else 0.0
    if rate <= 0.0:
        score = 100
    elif rate < 0.02:
        score = 90
    elif rate < 0.05:
        score = 75
    elif rate < 0.10:
        score = 55
    elif rate < 0.20:
        score = 30
    else:
        score = 10
    # Terminal turn_ended errors are the disk's strongest failure evidence.
    return max(0, score - 10 * min(terminal_errors, 3))


def _decision_density_score(messages: list[dict]) -> Optional[float]:
    """Substantive-message ratio over the rolling window (Claude port).

    Substantive mirrors the Claude parse: user text > 10 words; assistant
    text > 20 words or any tool_use (tool invocations ARE decisions).
    """
    window = messages[-_DENSITY_WINDOW:]
    if len(window) < _DENSITY_MIN_MESSAGES:
        return None
    substantive = 0
    for msg in window:
        role = msg.get("role")
        words = len((msg.get("content") or "").split())
        if role == "user" and words > 10:
            substantive += 1
        elif role == "assistant" and (msg.get("tool_calls") or words > 20):
            substantive += 1
    return min(100.0, (substantive / len(window)) * 200)


# ---------------------------------------------------------------------------
# Grade scale - measure.py's via hermes_session's helper, with the same
# inline fallback the shared scorer uses when measure.py is not importable.
# ---------------------------------------------------------------------------


def _grade_and_band(score: int) -> tuple[str, str]:
    try:
        import hermes_session

        helpers = hermes_session._get_measure_helpers()  # noqa: SLF001
        if helpers:
            # measure.py's facade functions resolve sys.modules['measure'] at
            # CALL time; in harnesses that swap that module out the call can
            # still fail, so the fallback lives around the call, not the lookup.
            return helpers[0](score), helpers[1](score)
    except Exception:  # pragma: no cover - defensive
        pass
    if score >= 90:
        grade = "S"
    elif score >= 80:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 55:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"
    if score >= 80:
        band = "Good"
    elif score >= 60:
        band = "Fair"
    elif score >= 40:
        band = "Needs Work"
    else:
        band = "Poor"
    return grade, band


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _default_cursor_home() -> Path:
    # Delegate to runtime_env.cursor_home() so TOKEN_OPTIMIZER_CURSOR_HOME gets
    # the same validation the hook bridge applies (absolute, existing,
    # non-symlink, confined under $HOME / the WSL /mnt opt-in). A bare
    # Path(override) here let scoring read a different home than the bridge
    # writes to.
    try:
        from runtime_env import cursor_home

        return cursor_home()
    except Exception:
        return Path.home() / ".cursor"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _latest_compaction_snapshot(compactions: Any) -> Optional[dict]:
    """The most recent preCompact record carrying real host numbers, or None."""
    if not isinstance(compactions, list):
        return None
    for rec in reversed(compactions):
        if not isinstance(rec, dict):
            continue
        tokens = _safe_int(rec.get("context_tokens"))
        window = _safe_int(rec.get("context_window_size"))
        pct = rec.get("context_usage_percent")
        if tokens > 0 and window > 0:
            return {"fill_ratio": min(1.0, tokens / window), "measured_at": rec.get("ts")}
        try:
            pct_val = float(pct)
        except (TypeError, ValueError):
            continue
        if pct_val > 0:
            return {"fill_ratio": min(1.0, pct_val / 100.0), "measured_at": rec.get("ts")}
    return None


def _read_transcript_messages(tally: dict, cursor_home: Path, transcript_path: Any) -> list[dict]:
    path = transcript_path if transcript_path else tally.get("transcript_path")
    if not path:
        return []
    try:
        import cursor_transcript

        messages, _skipped = cursor_transcript.read_cursor_messages(path, Path(cursor_home))
        return messages
    except Exception as exc:  # pragma: no cover - defensive; readers never raise
        logger.debug("[cursor_quality] transcript read failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_cursor_quality(
    tally: Any,
    *,
    cursor_home: Any = None,
    transcript_path: Any = None,
) -> Optional[dict]:
    """Score one Cursor session tally with per-signal provenance.

    ``tally`` is the cursor_hook_bridge session tally, optionally carrying
    the collector-injected ``bubble_tokens`` / ``transcript_tokens`` keys
    (the same convention ``cursor_session.normalize_session`` consumes).
    Returns None for a tally without a session identity. Never raises.
    """
    try:
        if not isinstance(tally, dict):
            return None
        conversation_id = str(tally.get("conversation_id") or tally.get("session_id") or "")
        if not conversation_id:
            return None

        home = Path(cursor_home) if cursor_home is not None else _default_cursor_home()

        import cursor_session

        bubble_tokens = tally.get("bubble_tokens")
        transcript_tokens = tally.get("transcript_tokens")
        total_input, total_output, token_source, estimated = cursor_session.resolve_token_source(
            bubble_tokens, transcript_tokens
        )

        # context_window_used is a positive fact only for a single known
        # model (bubble_tokens.model, or a one-model tally). Picking the
        # dominant model from a multi-model tally would mislabel recency,
        # and an unknown model must not inherit a default window as "fact".
        models = tally.get("models") if isinstance(tally.get("models"), dict) else {}
        model = None
        if isinstance(bubble_tokens, dict) and bubble_tokens.get("model"):
            model = str(bubble_tokens["model"])
        elif len(models) == 1:
            model = str(next(iter(models)))
        ctx_window = (
            cursor_session.context_window_for_model(model) if model else None
        )

        turns = _safe_int(tally.get("turns"))
        tool_calls = _safe_int(tally.get("tool_calls"))
        compactions = tally.get("compactions") if isinstance(tally.get("compactions"), list) else []
        failures = tally.get("failures") if isinstance(tally.get("failures"), dict) else {}
        failure_count = sum(_safe_int(v) for v in failures.values())

        messages = _read_transcript_messages(tally, home, transcript_path)
        terminal_errors = sum(
            1
            for m in messages
            if str(m.get("turn_ended_status") or "").lower() in _TERMINAL_ERROR_STATUSES
        )

        snapshot = _latest_compaction_snapshot(compactions)

        signals_active: list[str] = []
        signals_omitted: list[str] = []
        omitted_reasons: dict[str, str] = {}
        signal_scores: dict[str, Optional[float]] = {}
        signal_provenance: dict[str, str] = {}

        def _omit(name: str, reason: str) -> None:
            signals_omitted.append(name)
            omitted_reasons[name] = reason
            signal_scores[name] = None

        # --- base signals ---------------------------------------------------
        # Historical preCompact occupancy is metadata, never a grade: it is
        # host fill at compaction time, not current fill. context_fill stays
        # omitted until a current occupancy source exists; the composite
        # renormalizes over the remaining signals.
        fill_ratio = None
        if snapshot is not None:
            fill_ratio = snapshot["fill_ratio"]
        _omit(
            "context_fill",
            "no current host occupancy source; preCompact context numbers "
            "are historical (fill at compaction time), reported as metadata "
            "only and never graded; a lifetime token sum is never a fill",
        )

        signals_active.append("message_count")
        signal_scores["message_count"] = _message_score(turns)
        signal_provenance["message_count"] = "cursor_tally_turns"

        if token_source == "cursor_state_vscdb" and total_input > 0:
            signals_active.append("output_input_ratio")
            signal_scores["output_input_ratio"] = _oi_score(total_output / total_input)
            signal_provenance["output_input_ratio"] = "cursor_state_vscdb"
        else:
            _omit(
                "output_input_ratio",
                "no verified input/output token pair (state.vscdb absent; "
                "estimates carry no output side)",
            )

        # --- deeper Cursor signals -----------------------------------------
        if compactions:
            signals_active.append("compaction_depth")
            signal_scores["compaction_depth"] = _compaction_depth_score(
                len(compactions), fill_ratio
            )
            signal_provenance["compaction_depth"] = "cursor_precompact_events"
        else:
            _omit(
                "compaction_depth",
                "no preCompact events observed (none fired, or the hook is not "
                "wired on this install)",
            )

        if tool_calls > 0:
            signals_active.append("agent_efficiency")
            signal_scores["agent_efficiency"] = _agent_efficiency_score(
                failure_count, tool_calls, terminal_errors
            )
            signal_provenance["agent_efficiency"] = (
                "cursor_tally_failures+cursor_transcript_turn_ended"
            )
        else:
            _omit("agent_efficiency", "no tool calls recorded")

        density = _decision_density_score(messages)
        if density is not None:
            signals_active.append("decision_density")
            signal_scores["decision_density"] = round(density, 1)
            signal_provenance["decision_density"] = "cursor_transcript_disk"
        else:
            _omit(
                "decision_density",
                "no usable disk transcript under <cursor_home>/projects "
                f"(need >= {_DENSITY_MIN_MESSAGES} messages)",
            )

        for name, reason in _NEVER_AVAILABLE.items():
            _omit(name, reason)

        # Cursor's pre-compaction occupancy is historical. With no current
        # occupancy, the common scorer marks the estimate unmeasured.
        from _measure.quality_continuity import session_quality
        common = session_quality(None, ctx_window, model, len(compactions))
        score, grade, band = common["score"], common["grade"], common["band"]
        weights = {"context_fill": 5 / 7, "absolute_waste_tokens": 2 / 7}

        caveats = [_NO_TOOL_RESULTS_CAVEAT, "Per-signal Cursor values are diagnostics, not v3 score inputs."]
        if snapshot is not None:
            caveats.append(_HISTORICAL_FILL_CAVEAT)
        if token_source != "cursor_state_vscdb":
            caveats.append(
                "token figures are estimated or unknown for this session; "
                "Cursor exposes no billing data and no dollars are computed"
            )

        return {
            "score": score,
            "measured": common["measured"],
            "score_version": common["score_version"],
            "continuity": common["continuity"],
            "grade": grade,
            "band": band,
            "runtime": "cursor",
            "quality_pending": False,
            "score_signals_active": [],
            "signals_active": signals_active,
            "signals_omitted": signals_omitted,
            "omitted_reasons": omitted_reasons,
            "signal_scores": signal_scores,
            "signal_weights": {k: round(v, 4) for k, v in weights.items()},
            "signal_provenance": signal_provenance,
            "caveats": caveats,
            "fill_ratio": round(fill_ratio, 4) if fill_ratio is not None else None,
            "fill_is_current": False if snapshot is not None else None,
            "fill_measured_at": snapshot["measured_at"] if snapshot is not None else None,
            "context_window_used": ctx_window,
            "context_source": (
                "cursor_precompact_historical" if snapshot is not None else "unavailable"
            ),
            "token_source": token_source,
            "estimated": estimated,
            "evidence": {
                "turns": turns,
                "tool_calls": tool_calls,
                "tool_failures": failure_count,
                "terminal_errors": terminal_errors,
                "compactions": len(compactions),
                "transcript_messages": len(messages),
            },
        }
    except Exception as exc:  # pragma: no cover - scoring must never break a session
        logger.debug("[cursor_quality] scoring failed: %s", exc)
        return None


def quality_for_session(raw: Any, *, cursor_home: Any = None) -> Optional[dict]:
    """Integration seam for cursor_session.normalize_session / status surfaces.

    Single call site for the Cursor score so no surface computes its own.
    ``raw`` is the tally (+ collector-injected token keys), exactly as
    ``normalize_session`` receives it.
    """
    return compute_cursor_quality(raw, cursor_home=cursor_home)
