"""Activity-day consumers must count sessions, not session-days.

Review findings on the activity-dating branch:

- A3: the legacy before/after savings comparison and the short-session pool
  evaluated the session quality predicate per activity-day row. One session
  spanning several qualifying days counted as several sessions, and a session
  could land in the main pool on one day and the short-session pool on
  another. Eligibility belongs at session level; only token slices are
  windowed.
- A6: routing pending counted every stale on-disk transcript for every
  window. A record whose known activity is entirely before the window holds
  no in-window work and must not keep routing pending; only unknown
  coverage stays conservatively pending.
- A9: the resume-lean picker still ordered candidates by ingestion time
  (collected_at) and read the date column through a raw connection that
  never ran the activity migration.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from test_usage_counted_once import _load, _noon, SID_A, SID_B

FROZEN_BASELINE = {
    "version": 4,
    "activity_basis": 1,
    "typical_session": {
        "fresh_input": 30000.0, "cache_write": 487000.0,
        "cache_read": 13500000.0, "output": 51000.0,
    },
    "opus_share": 0.95,
    "opus_share_source": "pretool_baseline",
    "model_shares": {"opus": 0.95, "sonnet": 0.05},
    "window": {"start": "2026-03-18", "end": "2026-04-17",
               "sessions_used": 750, "sessions_total": 750, "elapsed": True},
    "method": "winsorized_mean", "winsor_pct": 0.99,
    "structural_overhead_tokens": 25288,
    "captured_at": "2026-04-17T01:21:13", "source": "frozen_from_history",
}

MODEL = "claude-sonnet-4-6"

_ZE_SUB_POOL = {
    "actual_usd": 0.0, "counterfactual_usd": 0.0, "transformation_usd": 0.0,
    "premium_delegation_usd": 0.0, "sessions": 0, "by_model": {}}


@pytest.fixture
def m(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_subagent_pool_savings", lambda **kw: dict(_ZE_SUB_POOL))
    return mod


def _insert_activity_session(conn, tag, days, duration_total, model=MODEL):
    """Insert one session_log row with per-day usage and per-day metrics.

    `days` is a list of (days_ago, fresh_input, output, day_duration_minutes).
    Session-level totals are derived from the same slices, matching what the
    collector stores: input_tokens is the sum of billed input across days and
    duration_minutes is the whole-session span.
    """
    usage, metrics, calls = {}, {}, {}
    for ago, fresh, out, dur in days:
        day = _noon(ago).date().isoformat()
        usage[day] = {model: {"fresh_input": fresh, "cache_read": 0,
                              "cache_create": 0, "cache_create_1h": 0,
                              "cache_create_5m": 0, "output": out}}
        metrics[day] = {"message_count": 1, "duration_minutes": dur,
                        "skills": {}, "subagents": {}, "tool_calls": {}}
        calls[day] = 1
    ordered = sorted(usage)
    input_total = sum(d[1] for d in days)
    output_total = sum(d[2] for d in days)
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, project, duration_minutes, "
        "input_tokens, output_tokens, message_count, api_calls, cache_hit_rate, "
        "model_usage_json, all_model_usage_json, is_sidechain, session_uuid, "
        "daily_usage_json, parent_daily_usage_json, first_activity_day, "
        "last_activity_day, daily_calls_json, daily_metrics_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"/s/{tag}.jsonl", ordered[-1], "-proj", duration_total, input_total,
         output_total, len(days), len(days), 0.0,
         json.dumps({model: input_total}), json.dumps({model: input_total}),
         0, tag, json.dumps(usage), json.dumps(usage),
         ordered[0], ordered[-1], json.dumps(calls), json.dumps(metrics)))


def _before_after(m, conn):
    conn.commit()
    conn.close()
    (m.SNAPSHOT_DIR / "baseline_state.json").write_text(json.dumps(FROZEN_BASELINE))
    return m._estimate_before_after_savings(days=30)


# --------------------------------------------------------------------- A3

def test_before_after_counts_sessions_not_activity_days(m):
    """One session active on three qualifying days is ONE session, not three.

    The review probe: a single session with three days of 1,000 input tokens
    and two minutes each produced 3 eligible rows vs 1 distinct session.
    """
    conn = m._init_trends_db()
    for i in range(10):
        _insert_activity_session(conn, f"fill{i}", [(1, 50_000, 500, 5.0)], 5.0)
    _insert_activity_session(
        conn, "multi",
        [(1, 1_000, 100, 2.0), (2, 1_000, 100, 2.0), (3, 1_000, 100, 2.0)], 6.0)
    r = _before_after(m, conn)
    # 10 single-day sessions + 1 multi-day session = 11 sessions. Counting
    # activity-day rows instead gives 13.
    assert r["sessions_per_month"] == 11


def test_short_session_pool_eligibility_is_per_session(m):
    """A session passing the quality gates at session level contributes
    nothing to the short-session pool, even on its below-threshold days."""
    conn = m._init_trends_db()
    for i in range(10):
        _insert_activity_session(conn, f"fill{i}", [(1, 50_000, 500, 5.0)], 5.0)
    # Session totals pass the gates (2,999 input tokens, 2.2 minutes); the
    # second day alone (999 input, 0.2 min) would fail them. The pools must
    # not split one session across both sides of the partition.
    _insert_activity_session(
        conn, "mixed", [(1, 2_000, 100, 2.0), (2, 999, 50, 0.2)], 2.2)
    r = _before_after(m, conn)
    assert r["short_session_count"] == 0
    assert r["short_session_transformation_usd"] == 0.0
    assert r["short_session_actual_usd"] == 0.0


# --------------------------------------------------------------------- A6

def _touch_transcript(m, name):
    path = m.CLAUDE_DIR / "projects" / "sample" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return str(path)


def _insert_log_row(conn, path, day, usage, version, first=None, last=None):
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, input_tokens, is_sidechain, "
        "session_uuid, daily_usage_json, usage_parser_version, "
        "first_activity_day, last_activity_day) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (path, day or "", 10, 0, Path(path).stem,
         json.dumps(usage) if usage is not None else None, version,
         first, last))


def test_pending_sessions_only_count_window_overlapping_activity(m):
    """A stale transcript whose known activity is entirely before the window
    is not in-window work; unknown coverage stays conservatively pending."""
    conn = m._init_trends_db()
    old_day = _noon(60).date().isoformat()
    new_day = _noon(1).date().isoformat()
    usage_for = lambda day: {day: {MODEL: {"fresh_input": 10, "output": 1}}}
    # Stale parser + transcript on disk, but activity 60 days ago: outside
    # every recent window, so it must not block routing.
    _insert_log_row(conn, _touch_transcript(m, "old.jsonl"), old_day,
                    usage_for(old_day), 2, old_day, old_day)
    # Same staleness with activity inside the window: pending.
    _insert_log_row(conn, _touch_transcript(m, "new.jsonl"), new_day,
                    usage_for(new_day), 2, new_day, new_day)
    # No dated evidence at all: cannot prove it is outside the window.
    _insert_log_row(conn, _touch_transcript(m, "unknown.jsonl"), "", None, 2)
    conn.commit()
    conn.close()
    assert m._routing_pending_sessions(days=30) == 2
    # A window starting today excludes even the in-window stale session;
    # only the unknown-coverage record remains pending.
    assert m._routing_pending_sessions(days=30, since=_noon(0).date().isoformat()) == 1


# --------------------------------------------------------------------- A9

def test_resume_lean_orders_by_activity_not_ingestion(m):
    """Recollecting an old session must not move it ahead of newer work."""
    conn = m._init_trends_db()
    old_day = _noon(20).date().isoformat()
    new_day = _noon(1).date().isoformat()
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, session_uuid, collected_at, "
        "first_activity_day, last_activity_day, input_tokens, is_sidechain) "
        "VALUES (?,?,?,?,?,?,?,0)",
        ("/s/old-act.jsonl", old_day, SID_A, _noon(0).isoformat(),
         old_day, old_day, 100))
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, session_uuid, collected_at, "
        "first_activity_day, last_activity_day, input_tokens, is_sidechain) "
        "VALUES (?,?,?,?,?,?,?,0)",
        ("/s/new-act.jsonl", new_day, SID_B, _noon(10).isoformat(),
         new_day, new_day, 100))
    conn.commit()
    conn.close()
    cands = m.resume_lean_candidates(limit=15, days=30)
    assert [c["session_id"] for c in cands] == [SID_B, SID_A]


_OLD_SESSION_LOG_SCHEMA = """
CREATE TABLE session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jsonl_path TEXT UNIQUE, date TEXT NOT NULL, project TEXT,
    duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
    message_count INTEGER, api_calls INTEGER, cache_hit_rate REAL,
    cache_create_1h_tokens INTEGER DEFAULT 0, cache_create_5m_tokens INTEGER DEFAULT 0,
    cache_ttl_scanned INTEGER DEFAULT 0, avg_call_gap_seconds REAL,
    max_call_gap_seconds REAL, p95_call_gap_seconds REAL, skills_json TEXT,
    subagents_json TEXT, tool_calls_json TEXT, model_usage_json TEXT,
    all_model_usage_json TEXT, model_usage_breakdown_json TEXT, version TEXT,
    slug TEXT, topic TEXT, collected_at TEXT, quality_score REAL, quality_grade TEXT,
    stale_waste_tokens INTEGER DEFAULT 0, session_uuid TEXT, is_sidechain INTEGER DEFAULT 0,
    reported_input_tokens INTEGER, reported_output_tokens INTEGER,
    reported_model_usage_json TEXT, daily_usage_json TEXT, origin TEXT
);
"""


def test_resume_lean_migrates_old_db_before_reading_dates(m):
    """The direct picker path must not consume an unmigrated mtime-era date.

    A pre-activity-dating DB has no dated usage evidence; migration repairs
    the date to empty, so the session is honestly absent from the window
    rather than ranked by a stale ingestion-era date.
    """
    db = m.TRENDS_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_SESSION_LOG_SCHEMA)
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, session_uuid, collected_at, "
        "input_tokens, is_sidechain) VALUES (?,?,?,?,?,0)",
        ("/s/legacy.jsonl", today, SID_A, datetime.now().isoformat(), 1000))
    conn.commit()
    conn.close()
    assert m.resume_lean_candidates(limit=15, days=30) == []
