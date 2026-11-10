"""Feature health: tell any user when a savings feature has gone quiet.

For each savings feature this compares how often it fired in the last 30 days
with how often it fired before, scaled by how active the user was in each
period (distinct active days). A feature that used to fire on a share of the
user's active days and now fires far less than that share predicts is flagged.

Universal by design: no fixed dates, no absolute event thresholds. The only
constants are statistical: a warning needs an expected count of at least
MIN_EXPECTED events (under a Poisson model, seeing zero by chance when about
six are expected is under a 0.3% event), so light users are never warned on
noise and heavy users are warned early.

Read-only. An unavailable database or missing table never reads as zero: the
result says ``available: False`` (or ``status: "unknown"`` per feature).
Pure stdlib; never imported by measure.py itself, only by the dashboard.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

RECENT_DAYS = 30
HISTORY_DAYS = 180          # earlier rate looks back at most this far
MIN_EXPECTED = 6.0          # expected events needed before "quiet" can fire
QUIET_RATIO = 0.15          # observed/expected at or below this = went quiet
FADING_RATIO = 0.4          # below this (with enough volume) = fading
MIN_EXPECTED_FADING = 12.0
ZERO_EFFECT_MIN = 8         # recent events needed to judge "fires, saves nothing"
ZERO_EFFECT_SHARE = 0.9

# (key, label, table, column, names, prefix_match)
# delta_read and structure_map also write a mirror row to compression_events;
# only the savings_events row is counted so nothing is double counted.
FEATURES = (
    ("structure_map", "Structure map", "savings_events", "event_type", ("structure_map",), False),
    ("delta_read", "Delta re-reads", "savings_events", "event_type", ("delta_read",), False),
    ("crossturn_dedup", "Cross-turn dedup", "compression_events", "feature", ("crossturn_dedup",), False),
    ("hint_followed", "Followed hints", "savings_events", "event_type", ("hint_followed",), False),
    ("first_read_skeleton", "First-read skeleton", "compression_events", "feature", ("first_read_skeleton",), False),
    ("tool_archive", "Tool output archive", "savings_events", "event_type", ("tool_archive",), False),
    # Per-command bash categories were folded into one pipeline over time;
    # grouping them keeps that rename from reading as an outage.
    ("bash_compression", "Bash compression", "compression_events", "feature", ("bash_compress_", "bash_generic"), True),
    ("build_output_compress", "Build output compression", "compression_events", "feature", ("build_output_compress",), False),
    ("checkpoint_restore", "Checkpoint restore", "savings_events", "event_type", ("checkpoint_restore",), False),
    ("resume_lean", "Lean resume", "savings_events", "event_type", ("resume_lean",), False),
)

_SAVED_EXPR = {
    "savings_events": "COALESCE(tokens_saved, 0)",
    "compression_events": "COALESCE(original_tokens, 0) - COALESCE(compressed_tokens, 0)",
}


def _connect_ro(db_path):
    if db_path is None:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.5)
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.Error:
        return None


def _tables(conn):
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _where(column, names, prefix):
    if prefix:
        parts, params = [], []
        for n in names:
            if n.endswith("_"):
                parts.append(f"{column} LIKE ? ESCAPE '!'")
                params.append(n.replace("_", "!_") + "%")
            else:
                parts.append(f"{column} = ?")
                params.append(n)
        return "(" + " OR ".join(parts) + ")", params
    marks = ",".join("?" for _ in names)
    return f"{column} IN ({marks})", list(names)


def _active_days(conn, tables, start, end):
    """Distinct days with any recorded activity in [start, end)."""
    days = set()
    queries = []
    if "session_log" in tables:
        queries.append("SELECT DISTINCT substr(date, 1, 10) FROM session_log WHERE date >= ? AND date < ?")
    for t in ("savings_events", "compression_events"):
        if t in tables:
            queries.append(f"SELECT DISTINCT substr(timestamp, 1, 10) FROM {t} WHERE timestamp >= ? AND timestamp < ?")
    for q in queries:
        try:
            days.update(r[0] for r in conn.execute(q, (start, end)) if r[0])
        except sqlite3.Error:
            continue
    return days


def _status(recent, expected, earlier, zero_recent):
    if recent >= ZERO_EFFECT_MIN and zero_recent >= ZERO_EFFECT_SHARE * recent:
        return "no_effect"
    if expected is None:
        return "new" if recent else ("never" if not earlier else "idle")
    if expected >= MIN_EXPECTED and recent <= QUIET_RATIO * expected:
        return "quiet"
    if expected >= MIN_EXPECTED_FADING and recent < FADING_RATIO * expected:
        return "fading"
    if recent == 0:
        return "rare"
    return "ok"


_MESSAGES = {
    "quiet": "Used to fire on your sessions and has gone quiet. It may be broken or switched off.",
    "fading": "Firing far less than your own earlier rate.",
    "no_effect": "Fires, but recorded no saving. The hook may report a replacement that never took effect.",
}


def collect(db_path, now=None, recent_days=RECENT_DAYS):
    """Return the feature-health payload. Never writes; never raises."""
    try:
        return _collect(db_path, now, recent_days)
    except Exception:  # fail-open: the dashboard must always render
        return {"available": False, "features": [], "warnings": 0}


def _collect(db_path, now, recent_days):
    now = now or datetime.now()
    if getattr(now, "tzinfo", None) is not None:
        now = now.replace(tzinfo=None)
    conn = _connect_ro(db_path)
    if conn is None:
        return {"available": False, "features": [], "warnings": 0}
    try:
        tables = _tables(conn)
        if not tables & {"savings_events", "compression_events"}:
            return {"available": False, "features": [], "warnings": 0}
        recent_start = (now - timedelta(days=recent_days)).isoformat()
        history_start = (now - timedelta(days=recent_days + HISTORY_DAYS)).isoformat()
        end = (now + timedelta(days=1)).isoformat()
        recent_active = len(_active_days(conn, tables, recent_start, end))
        rows = []
        for key, label, table, column, names, prefix in FEATURES:
            row = {"feature": key, "label": label, "events": None, "earlier_events": None,
                   "expected": None, "last_event": None, "status": "unknown", "warning": False}
            if table not in tables:
                rows.append(row)
                continue
            where, params = _where(column, names, prefix)
            try:
                recent, zero_recent = conn.execute(
                    f"SELECT COUNT(*), COALESCE(SUM({_SAVED_EXPR[table]} <= 0), 0) FROM {table} "
                    f"WHERE {where} AND timestamp >= ?", params + [recent_start]).fetchone()
                earlier, first_earlier = conn.execute(
                    f"SELECT COUNT(*), MIN(timestamp) FROM {table} WHERE {where} "
                    f"AND timestamp >= ? AND timestamp < ?", params + [history_start, recent_start]).fetchone()
                last = conn.execute(f"SELECT MAX(timestamp) FROM {table} WHERE {where}", params).fetchone()[0]
            except sqlite3.Error:
                rows.append(row)
                continue
            expected = None
            if earlier and first_earlier:
                # Earlier rate = events per active day since the feature first fired
                # in the lookback, so a feature added mid-history is not diluted.
                earlier_active = len(_active_days(conn, tables, str(first_earlier)[:10], recent_start))
                if earlier_active:
                    expected = round(earlier / earlier_active * recent_active, 1)
            status = _status(recent, expected, earlier, zero_recent)
            row.update(events=recent, earlier_events=earlier, expected=expected,
                       last_event=str(last)[:10] if last else None, status=status,
                       warning=status in ("quiet", "no_effect"))
            if status in _MESSAGES:
                row["message"] = _MESSAGES[status]
            rows.append(row)
        return {"available": True, "window_days": recent_days, "active_days": recent_active,
                "features": rows, "warnings": sum(1 for r in rows if r["warning"])}
    except sqlite3.Error:
        return {"available": False, "features": [], "warnings": 0}
    finally:
        conn.close()
