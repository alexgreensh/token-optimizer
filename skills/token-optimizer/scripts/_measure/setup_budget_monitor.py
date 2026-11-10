"""Observe skill-list budget values without changing the user's settings.

This is a candidate change detector, not a savings counter. Unset and invalid
values break the comparison chain so a later value is not assumed to be a drop.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """CREATE TABLE IF NOT EXISTS setup_budget_observations (
 event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project TEXT NOT NULL,
 transcript_path TEXT NOT NULL, budget INTEGER, observed_at TEXT NOT NULL
)"""


def _epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def record(conn, session_id, project, transcript_path, value, observed_at):
    """Append one observation, returning True only after reading it back."""
    if not all(isinstance(v, str) and v for v in (session_id, project, transcript_path)):
        return False
    if len(session_id) > 256 or len(project) > 2048 or len(transcript_path) > 4096 or _epoch(observed_at) is None:
        return False
    try:
        budget = int(value) if value is not None and not isinstance(value, bool) else None
        if budget is not None and budget < 0:
            budget = None
    except (TypeError, ValueError, OverflowError):
        budget = None
    identity = json.dumps([session_id, project, transcript_path, budget, observed_at], separators=(",", ":"))
    event_id = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()
    conn.execute(_SCHEMA)
    cursor = conn.execute("INSERT OR IGNORE INTO setup_budget_observations VALUES (?,?,?,?,?,?)",
                          (event_id, session_id, project, transcript_path, budget, observed_at))
    changed = cursor.rowcount == 1
    conn.commit()
    return changed and conn.execute("SELECT 1 FROM setup_budget_observations WHERE event_id=?", (event_id,)).fetchone() is not None


def decreases(conn):
    """Return observed lower-budget candidates, never inferred savings."""
    try:
        rows = conn.execute("SELECT session_id,project,transcript_path,budget,observed_at FROM setup_budget_observations").fetchall()
    except Exception:
        return []
    grouped = {}
    for sid, project, path, budget, stamp in rows:
        timestamp = _epoch(stamp)
        if timestamp is not None:
            grouped.setdefault(project, {}).setdefault(timestamp, []).append((sid, path, budget, stamp))
    output = []
    for project, times in grouped.items():
        previous = None
        for ts in sorted(times):
            group = times[ts]
            budgets = {item[2] for item in group}
            if len(budgets) != 1:
                previous = None
                continue
            sid, path, budget, stamp = group[0]
            if previous and previous[2] is not None and budget is not None and budget < previous[2]:
                output.append({"project": project, "session_id": sid, "before_transcript": previous[1],
                               "after_transcript": path, "old_budget": previous[2], "new_budget": budget,
                               "changed_at": stamp})
            previous = (sid, path, budget, stamp)
    return output


def observe_hook(hook_input, db_path, environ=None, observed_at=None):
    """Record an isolated SessionStart sample without creating a missing DB."""
    env = os.environ if environ is None else environ
    if str(env.get("CLAUDE_CODE_REMOTE", "")).lower() in ("1", "true", "yes", "on"):
        return False
    sid, cwd, transcript = (hook_input.get("session_id"), hook_input.get("cwd"), hook_input.get("transcript_path"))
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not isinstance(transcript, str) or not os.path.isabs(transcript):
        return False
    if not os.path.isfile(db_path):
        return False
    from .structural_ledger import encode_project
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(db_path), timeout=0.05) as conn:
            return record(conn, sid, encode_project(cwd), transcript,
                          env.get("SLASH_COMMAND_TOOL_CHAR_BUDGET"), observed_at)
    except (OSError, sqlite3.Error, ValueError):
        return False


def evaluate(conn, estimate, card_fn, window_start=None, window_end=None):
    """Evaluate observed budget drops as unbooked, scope-local diagnostics."""
    from .setup_attachment_counter import measure, read_transcript
    try:
        rows = conn.execute("SELECT session_id,project,transcript_path,budget,observed_at FROM setup_budget_observations").fetchall()
    except sqlite3.Error:
        return {"booked": False, "attribution_verified": False, "candidates": []}
    groups = {}
    for sid, project, path, budget, stamp in rows:
        ts = _epoch(stamp)
        if ts is not None:
            groups.setdefault(project, {}).setdefault(ts, []).append((sid, path, budget, stamp))
    if (window_start is not None and _epoch(window_start) is None) or (window_end is not None and _epoch(window_end) is None):
        return {"booked": False, "attribution_verified": False, "candidates": []}
    candidates, seen_requests = [], set()
    for project, times in groups.items():
        previous, active, baseline = None, None, None
        ordered_times = sorted(times)
        segment_end = None
        for index, ts in enumerate(ordered_times):
            group = times[ts]
            budgets = {row[2] for row in group}
            if len(budgets) != 1:
                previous, active, baseline = None, None, None
                continue
            sid, path, budget, stamp = group[0]
            if previous and previous[2] != budget:
                active, baseline = None, None
                if previous[2] is not None and budget is not None and budget < previous[2]:
                    segment_end = next((future for future in ordered_times[index + 1:]
                                        if {item[2] for item in times[future]} != {budget}), None)
                    try:
                        baseline = [r for r in read_transcript(previous[1]) if r.get("type") in ("attachment", "system")]
                    except OSError:
                        baseline = None
                    identity = json.dumps([project, sid, stamp, previous[2], budget], separators=(",", ":"))
                    active = {"id": hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest(),
                              "changed_at": stamp, "old_budget": previous[2], "new_budget": budget,
                              "calls": 0, "read_tokens": 0, "write_tokens": 0, "usd": 0.0,
                              "read_usd": 0.0, "write_1h_usd": 0.0, "write_5m_usd": 0.0,
                              "unpriced_calls": 0, "missing_transcripts": int(baseline is None)}
                    candidates.append(active)
            if active is not None and baseline is not None:
                end_epoch = _epoch(window_end) if window_end is not None else None
                if segment_end is not None and (end_epoch is None or segment_end < end_epoch):
                    effective_end = datetime.fromtimestamp(segment_end, timezone.utc).isoformat()
                else:
                    effective_end = window_end
                for _, transcript, _, _ in group:
                    try:
                        part = measure(baseline, read_transcript(transcript), active["changed_at"], "skill_listing",
                                       estimate, card_fn, seen_requests=seen_requests,
                                       window_start=window_start, window_end=effective_end)
                    except OSError:
                        active["missing_transcripts"] += 1
                        continue
                    for key in ("calls", "read_tokens", "write_tokens", "usd", "read_usd", "write_1h_usd", "write_5m_usd", "unpriced_calls"):
                        active[key] += part[key]
            previous = (sid, path, budget, stamp)
    return {"booked": False, "attribution_verified": False, "candidates": candidates}


def measured_values(conn, trims, estimate, card_fn, window_start, window_end):
    """Value observed drops unless another credited listing trim may overlap."""
    from .structural_listing import LISTING_COMPONENTS
    start, end = _epoch(window_start), _epoch(window_end)
    zero = {"calls": 0, "tokens_saved": 0, "usd": 0.0, "read_usd": 0.0,
            "write_1h_usd": 0.0, "write_5m_usd": 0.0, "overlap_suppressed": False,
            "attribution_verified": False, "candidates": []}
    if start is None or end is None or end <= start:
        return zero
    for trim in trims:
        if (trim.get("component") in LISTING_COMPONENTS and trim.get("tier") in ("measured", "estimated")
                and int(trim.get("tokens") or 0) > 0 and (trim.get("ts_epoch") or 0) < end
                and (trim.get("closed_epoch") is None or trim["closed_epoch"] > start)):
            return {**zero, "overlap_suppressed": True}
    result = evaluate(conn, estimate, card_fn, window_start, window_end)
    candidates = result["candidates"]
    return {"calls": sum(c["calls"] for c in candidates),
            "tokens_saved": sum(c["read_tokens"] + c["write_tokens"] for c in candidates),
            "usd": sum(c["usd"] for c in candidates), "read_usd": sum(c["read_usd"] for c in candidates),
            "write_1h_usd": sum(c["write_1h_usd"] for c in candidates),
            "write_5m_usd": sum(c["write_5m_usd"] for c in candidates),
            "overlap_suppressed": False, "attribution_verified": False, "candidates": candidates}
