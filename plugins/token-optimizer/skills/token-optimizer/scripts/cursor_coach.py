#!/usr/bin/env python3
"""Cursor-native Token Coach data collection (Slice D / U7 coach gap).

Under the Cursor runtime the token-coach skill previously had only
``cursor-doctor`` and ``cursor-summary`` - hook readiness and a session recap,
not coaching data. This module generates the coach's structured data from
Token Optimizer's OWN Cursor data planes, read-only:

- session tallies under ``<cursor_home>/token-optimizer/sessions/*.json``
  (turns, tool calls, models, recorded failures, preCompact snapshots);
- session quality from ``cursor_quality.compute_cursor_quality`` (the single
  Cursor scorer - per-signal provenance included);
- token figures resolved live through ``cursor_state`` (state.vscdb bubble
  ``tokenCount``, else the transcript chars-over-four estimate - the same
  sources and ordering ``cursor-rollup`` uses);
- the rollup's ``trends.db`` ``session_log`` rows (``platform='cursor'``) for
  aggregate history;
- disk transcripts only through the tally's ``transcript_path`` (via
  cursor_quality, which confines reads under ``<cursor_home>/projects``).

Honesty rules (R15 / KD3, and the disk-writer evidence):

- Cursor exposes no local billing data: the cost block says so, and nothing
  here computes or implies a dollar figure. A 0 is "no data", never "free".
- Every token figure is quoted with its provenance (state.vscdb rollup /
  transcript estimate / tally-only unknown).
- preCompact fill numbers are HISTORICAL pre-compaction occupancy; patterns
  say "before compaction", never imply current fill.
- Cursor disk transcripts carry no tool results: "no failures recorded" is
  hook coverage, not proof of clean runs, and is phrased that way.
- It never scans ~/.claude or any other runtime's files.

The output mirrors the shape the token-coach skill consumes for other
runtimes (snapshot / patterns_good / patterns_bad / history / questions /
health_score) so the coaching conversation works unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_TALLY_FILES = 10_000
_MAX_TALLY_BYTES = 4 * 1024 * 1024
_MAX_TRANSCRIPT_SESSIONS = 20  # transcript parsing for quality, most recent first

_NO_COST_DETAIL = (
    "Cursor exposes no local billing data; coaching is in tokens, never "
    "dollars. The Cursor CLI /usage surface shows account-level usage only."
)


def _default_cursor_home() -> Path:
    # Delegate to runtime_env.cursor_home() so the TOKEN_OPTIMIZER_CURSOR_HOME
    # override gets the same validation the hook bridge applies (absolute,
    # existing, non-symlink, confined under $HOME / WSL /mnt opt-in). A bare
    # Path(override) here let coach/audit read a different home than the
    # bridge writes to.
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


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _read_tally(path: Path) -> Optional[dict]:
    """Bounded, defensive tally read; None on any failure."""
    try:
        if path.stat().st_size > _MAX_TALLY_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def collect_cursor_sessions(
    cursor_home: Any = None, *, limit: int = _MAX_TALLY_FILES
) -> tuple[list[dict], list[str]]:
    """Read session tallies (newest first) plus human-readable skip notes."""
    home = Path(cursor_home) if cursor_home is not None else _default_cursor_home()
    sessions_dir = home / "token-optimizer" / "sessions"
    tallies: list[dict] = []
    skipped: list[str] = []
    try:
        paths = [p for p in sessions_dir.iterdir() if p.is_file() and p.suffix == ".json"]
    except OSError:
        return [], []

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    paths.sort(key=_mtime, reverse=True)
    for path in paths[:limit]:
        tally = _read_tally(path)
        if tally is None:
            skipped.append(f"skipped unreadable tally {path.name} (corrupt or oversized)")
            continue
        tallies.append(tally)
    return tallies, skipped


def _trends_db_path(home: Path) -> Optional[Path]:
    snap = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()
    candidates = []
    if snap:
        candidates.append(Path(snap).expanduser() / "trends.db")
    candidates.append(home / "_backups" / "token-optimizer" / "trends.db")
    return next((p for p in candidates if p.is_file()), None)


def _collect_history(home: Path) -> dict:
    """Rollup session_log rows for Cursor, read-only; honest notes on absence."""
    history: dict[str, Any] = {
        "trended_sessions": 0,
        "tokens_resolved_sessions": 0,
        "average_quality_score": None,
        "note": None,
    }
    db_path = _trends_db_path(home)
    if db_path is None:
        history["note"] = (
            "trends.db not found - token figures unresolved; run "
            "measure.py cursor-rollup to resolve them"
        )
        return history
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        history["note"] = f"trends.db would not open read-only: {exc}"
        return history
    try:
        available = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        want = {"jsonl_path", "input_tokens", "output_tokens", "quality_score"}
        if not {"jsonl_path", "input_tokens", "output_tokens"} <= available:
            history["note"] = "trends.db lacks the session_log columns the coach needs"
            return history
        cols = sorted(want & available)
        if "platform" in available:
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM session_log WHERE platform = 'cursor'"
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM session_log").fetchall()
    except sqlite3.Error as exc:
        history["note"] = f"trends.db query failed: {exc}"
        return history
    finally:
        conn.close()
    idx = {name: i for i, name in enumerate(cols)}
    quality_scores = []
    for row in rows:
        history["trended_sessions"] += 1
        if _safe_int(row[idx["input_tokens"]]) > 0 or _safe_int(row[idx["output_tokens"]]) > 0:
            history["tokens_resolved_sessions"] += 1
        if "quality_score" in idx:
            qs = _safe_float(row[idx["quality_score"]])
            if qs is not None:
                quality_scores.append(qs)
    if quality_scores:
        history["average_quality_score"] = round(sum(quality_scores) / len(quality_scores), 1)
    return history


def _pattern(name: str, severity: str, detail: str, fix: str) -> dict:
    return {"name": name, "severity": severity, "detail": detail, "fix": fix}


def _detect_patterns(session_view: list[dict]) -> tuple[list[dict], list[dict]]:
    """Ground every pattern in collected evidence; quote provenance with numbers."""
    good: list[dict] = []
    bad: list[dict] = []

    hot = [s for s in session_view if (s["latest_compaction_fill"] or 0) >= 0.85]
    for s in hot[:3]:
        bad.append(_pattern(
            "Running Hot Into Compaction",
            "high",
            f"Session {s['label']} reached {round(s['latest_compaction_fill'] * 100)}% "
            "of the context window before compaction (the host's measurement at "
            "compaction time, not a live reading).",
            "Compact earlier or split the work so each rebuilt context starts "
            "leaner; keep .cursor/rules and AGENTS.md short so less comes back "
            "after every compaction.",
        ))

    churn = [s for s in session_view if s["compactions"] >= 3]
    for s in churn[:3]:
        bad.append(_pattern(
            "Repeated Compaction Churn",
            "medium",
            f"Session {s['label']} compacted {s['compactions']} times over "
            f"{s['turns']} turns.",
            "Long single-chat sessions lose working context at every "
            "compaction; break the task at natural seams and start a fresh "
            "chat with a short brief instead.",
        ))

    for s in session_view:
        for tool, count in s["failures_by_tool"].items():
            rate = count / s["tool_calls"] if s["tool_calls"] else 0.0
            if count >= 3 or (count > 0 and rate >= 0.10):
                bad.append(_pattern(
                    "Tool Failures Clustering",
                    "medium",
                    f"Session {s['label']}: {tool} failed {count} of "
                    f"{s['tool_calls']} recorded tool calls (as recorded by "
                    "the wired hooks).",
                    "Repeated failures of one tool usually mean a path, "
                    "permission or command-shape problem; fix the cause "
                    "instead of letting the agent retry into the context.",
                ))
                break

    terminal = [s for s in session_view if s["terminal_errors"] > 0]
    for s in terminal[:3]:
        bad.append(_pattern(
            "Turns Ending In Errors",
            "medium",
            f"Session {s['label']}: {s['terminal_errors']} turn(s) ended with "
            "an error or abort status on disk.",
            "Check what the agent was attempting at those turns; repeated "
            "terminal errors often mean the task needs splitting or a "
            "permission the session lacks.",
        ))

    unknown_tokens = [s for s in session_view if s["token_source"] == "cursor_tally_only"]
    if unknown_tokens:
        bad.append(_pattern(
            "Token Footprint Unknown",
            "low",
            f"{len(unknown_tokens)} of {len(session_view)} session(s) have no "
            "resolved token figures (no state.vscdb read, no transcript "
            "estimate) - their usage is unknown, not zero.",
            "Check that IDE chats exist (state.vscdb) or CLI transcripts are "
            "present under <cursor_home>/projects; measure.py cursor-doctor "
            "names both data planes.",
        ))

    marathon = [s for s in session_view if s["turns"] > 100]
    for s in marathon[:3]:
        bad.append(_pattern(
            "Marathon Sessions",
            "low",
            f"Session {s['label']} ran {s['turns']} turns.",
            "Very long sessions accumulate stale context; a fresh chat with "
            "a short brief is cheaper than dragging the whole history.",
        ))

    vscdb = [s for s in session_view if s["token_source"] == "cursor_state_vscdb"]
    if vscdb:
        good.append(_pattern(
            "Real Token Figures Flowing",
            "info",
            f"{len(vscdb)} of {len(session_view)} session(s) have host-measured "
            "token figures (state.vscdb), so their coaching numbers are "
            "measured rather than estimated.",
            "Nothing to change; this is the provenance every other session "
            "should reach.",
        ))

    clean = [
        s for s in session_view
        if s["tool_calls"] >= 20 and not s["failures_by_tool"] and s["terminal_errors"] == 0
    ]
    if clean:
        good.append(_pattern(
            "Clean Recorded Tool Record",
            "info",
            f"{len(clean)} session(s) with 20+ tool calls and no failures "
            "recorded by the wired hooks (hook coverage, not a guarantee).",
            "Nothing to change.",
        ))

    short = [s for s in session_view if 0 < s["turns"] <= 20]
    if short:
        good.append(_pattern(
            "Short Focused Sessions",
            "info",
            f"{len(short)} session(s) stayed at or under 20 turns.",
            "Short, single-purpose chats keep context lean; keep splitting "
            "work this way.",
        ))

    return good, bad


def generate_cursor_coach_data(
    cursor_home: Any = None, *, days: int = 30, focus: Any = None
) -> dict:
    """Coach-shaped data for the Cursor runtime. Never raises; never writes."""
    try:
        return _generate(cursor_home, days=days, focus=focus)
    except Exception as exc:  # pragma: no cover - coach must never break a CLI run
        logger.debug("[cursor_coach] data generation failed: %s", exc)
        return {
            "runtime": "cursor",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "focus_area": focus,
            "snapshot": {
                "sessions_analyzed": 0,
                "window_days": days,
                "total_turns": 0,
                "total_tool_calls": 0,
                "total_compactions": 0,
                "token_provenance": {},
                "quality": {"scored_sessions": 0, "average_score": None,
                            "signals_note": "data generation failed; see logs"},
                "cost": {"status": "cursor_no_cost_data", "detail": _NO_COST_DETAIL},
            },
            "patterns_good": [],
            "patterns_bad": [],
            "history": {"trended_sessions": 0, "tokens_resolved_sessions": 0,
                         "average_quality_score": None, "note": "data generation failed; see logs"},
            "questions": [],
            "health_score": None,
            "coverage": {"skipped": [], "notes": ["data generation failed; see logs"]},
        }


def _generate(cursor_home: Any = None, *, days: int = 30, focus: Any = None) -> dict:
    home = Path(cursor_home) if cursor_home is not None else _default_cursor_home()
    tallies, skipped = collect_cursor_sessions(home)

    cutoff = time.time() - days * 86400
    in_window: list[dict] = []
    for tally in tallies:
        updated = _safe_float(tally.get("updated_at"))
        if updated is not None and updated < cutoff:
            continue  # outside the window; missing timestamps stay included
        in_window.append(tally)

    # Resolve token provenance before scoring. Raw tally JSON never carries
    # bubble_tokens/transcript_tokens, so without this every session scores
    # cursor_tally_only and output_input_ratio / the "real figures" pattern
    # are unreachable on real installs. Same sources and ordering as
    # _collect_cursor_sessions, bounded to the analysis window.
    import cursor_state as _cst

    composer_ids = [
        str(t.get("conversation_id") or t.get("session_id") or "") for t in in_window
    ]
    try:
        bubble = _cst.read_state_vscdb_tokens(composer_ids)
    except Exception:
        bubble = {}
    resolved_tallies: list[dict] = []
    for tally in in_window:
        enriched = dict(tally)
        cid = str(tally.get("conversation_id") or tally.get("session_id") or "")
        enriched["bubble_tokens"] = bubble.get(cid)
        try:
            enriched["transcript_tokens"] = _cst.transcript_estimate(
                tally.get("transcript_path"), home
            )
        except Exception:
            enriched["transcript_tokens"] = None
        resolved_tallies.append(enriched)
    in_window = resolved_tallies

    import cursor_quality

    session_view: list[dict] = []
    qualities: list[dict] = []
    for rank, tally in enumerate(in_window):
        # Transcript parsing is the expensive part; bound it to recent sessions.
        quality = cursor_quality.compute_cursor_quality(
            tally,
            cursor_home=home,
            transcript_path=None if rank >= _MAX_TRANSCRIPT_SESSIONS else tally.get("transcript_path"),
        )
        conversation_id = str(tally.get("conversation_id") or tally.get("session_id") or "unknown")
        compactions = tally.get("compactions") if isinstance(tally.get("compactions"), list) else []
        latest_fill = None
        for rec in reversed(compactions):
            if not isinstance(rec, dict):
                continue
            tokens, window = _safe_int(rec.get("context_tokens")), _safe_int(rec.get("context_window_size"))
            if tokens > 0 and window > 0:
                latest_fill = min(1.0, tokens / window)
                break
            pct = _safe_float(rec.get("context_usage_percent"))
            if pct and pct > 0:
                latest_fill = min(1.0, pct / 100.0)
                break
        failures = tally.get("failures") if isinstance(tally.get("failures"), dict) else {}
        failures_by_tool = {str(k): _safe_int(v) for k, v in failures.items()}
        view = {
            "label": conversation_id[:12],
            "turns": _safe_int(tally.get("turns")),
            "tool_calls": _safe_int(tally.get("tool_calls")),
            "compactions": len(compactions),
            "latest_compaction_fill": latest_fill,
            "failures_by_tool": failures_by_tool,
            "terminal_errors": (quality or {}).get("evidence", {}).get("terminal_errors", 0),
            "token_source": (quality or {}).get("token_source", "cursor_tally_only"),
            "quality": quality,
        }
        session_view.append(view)
        if quality is not None:
            qualities.append(quality)

    patterns_good, patterns_bad = _detect_patterns(session_view)

    provenance: dict[str, int] = {}
    for view in session_view:
        provenance[view["token_source"]] = provenance.get(view["token_source"], 0) + 1

    scores = [q["score"] for q in qualities]
    health_score = round(sum(scores) / len(scores)) if scores else None

    questions = [
        "What's your goal today - a new build that should stay token-efficient, "
        "a chat that fills its context too fast, or a general health check?",
        "Do your longer Cursor chats slow down or lose track before they "
        "compact?",
        "Are your project rules in .cursor/rules or AGENTS.md, and how long "
        "are they?",
    ]

    return {
        "runtime": "cursor",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "focus_area": focus,
        "snapshot": {
            "sessions_analyzed": len(session_view),
            "window_days": days,
            "total_turns": sum(s["turns"] for s in session_view),
            "total_tool_calls": sum(s["tool_calls"] for s in session_view),
            "total_compactions": sum(s["compactions"] for s in session_view),
            "token_provenance": provenance,
            "quality": {
                "scored_sessions": len(qualities),
                "average_score": round(sum(scores) / len(scores), 1) if scores else None,
                "signals_note": (
                    "per-signal provenance on every score; unavailable signals "
                    "are omitted and the composite renormalizes"
                ),
            },
            "cost": {"status": "cursor_no_cost_data", "detail": _NO_COST_DETAIL},
        },
        "patterns_good": patterns_good,
        "patterns_bad": patterns_bad,
        "history": _collect_history(home),
        "questions": questions,
        "health_score": health_score,
        "coverage": {
            "skipped": skipped,
            "notes": [
                "Cursor disk transcripts carry no tool results (bundle-proven "
                "writer 2026.11.05): failure figures are what the wired hooks "
                "recorded, never proof of clean runs.",
                "preCompact fill numbers are the host's occupancy at "
                "compaction time (historical), not a live reading.",
                "Channels reporting unknown/shadow in cursor-doctor have no "
                "delivery evidence on this install; nothing here claims they "
                "ran.",
            ],
        },
    }
