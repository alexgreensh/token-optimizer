"""_query_trends_db's activity-window prefilter must not change results.

The dashboard prices every session row it scans. On large histories that is
the dominant cost, so the row scan is restricted to sessions whose activity
bounds reach the window (NULL bounds always stay: they cover missing/empty
per-day JSON that can still fall back to parent_daily_usage_json, and rows
carrying parent evidence). This test builds a fixture exercising every
boundary case and asserts the filtered and unfiltered paths return identical
results.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from test_usage_counted_once import _load

MODEL = "claude-opus-4-8"


def _day(n):
    return (datetime.now() - timedelta(days=n)).date().isoformat()


def _bucket(fresh=1000, out=100, cr=500, cc=200):
    return {"fresh_input": fresh, "cache_read": cr, "cache_create": cc,
            "cache_create_1h": cc // 2, "cache_create_5m": cc - cc // 2,
            "output": out}


def _usage(*days):
    return {d: {MODEL: _bucket()} for d in days}


def _session(**kw):
    row = {
        "jsonl_path": kw.pop("path", "/fake/s.jsonl"),
        "date": "", "project": "proj", "duration_minutes": 30.0,
        "input_tokens": 5000, "output_tokens": 500, "message_count": 20,
        "api_calls": 10, "cache_hit_rate": 0.5,
        "cache_create_1h_tokens": 100, "cache_create_5m_tokens": 100,
        "avg_call_gap_seconds": 60.0, "max_call_gap_seconds": 300.0,
        "p95_call_gap_seconds": 200.0,
        "skills_json": json.dumps({"sample": 2}),
        "subagents_json": json.dumps({"general-purpose": 1}),
        "tool_calls_json": json.dumps({"Read": 5}),
        "model_usage_json": json.dumps({MODEL: 6000}),
        "all_model_usage_json": json.dumps({MODEL: 7000}),
        "model_usage_breakdown_json": json.dumps({MODEL: _bucket()}),
        "version": "5.13", "slug": "s", "topic": None,
        "collected_at": datetime.now().isoformat(),
        "quality_score": 80.0, "quality_grade": "B", "stale_waste_tokens": 0,
        "session_uuid": None, "is_sidechain": 0, "sidechain_reason": None,
        "reported_input_tokens": 5000, "reported_output_tokens": 500,
        "reported_model_usage_json": None,
        "daily_usage_json": None, "origin": "local",
        "first_activity_day": None, "last_activity_day": None,
        "transcript_mtime": None, "parent_daily_usage_json": None,
        "daily_calls_json": None, "daily_metrics_json": None,
        "cost_usd": 0.01, "cost_source": "api", "credits": None,
        "platform": "claude", "incomplete": 0, "usage_parser_version": 3,
    }
    row.update(kw)
    return row


def _insert(conn, rows):
    cols = list(rows[0])
    conn.executemany(
        f"INSERT INTO session_log ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [[r[c] for c in cols] for r in rows])


def _fixture_rows():
    usage = _usage
    return [
        # In-window multi-day session: bounds reach the window.
        _session(path="/fake/in.jsonl", date=_day(2),
                 daily_usage_json=json.dumps(usage(_day(5), _day(2))),
                 parent_daily_usage_json=json.dumps(usage(_day(2))),
                 first_activity_day=_day(5), last_activity_day=_day(2),
                 daily_calls_json=json.dumps({_day(5): 4, _day(2): 6}),
                 daily_metrics_json=json.dumps({_day(5): {"message_count": 9, "duration_minutes": 12},
                                                _day(2): {"message_count": 11, "duration_minutes": 18}})),
        # Spans the window edge: old and new slices; only new ones land in daily.
        _session(path="/fake/cross.jsonl", date=_day(1),
                 daily_usage_json=json.dumps(usage(_day(40), _day(1))),
                 first_activity_day=_day(40), last_activity_day=_day(1)),
        # Entirely old: filtered out, and would contribute nothing anyway.
        _session(path="/fake/old.jsonl", date=_day(50),
                 daily_usage_json=json.dumps(usage(_day(60), _day(50))),
                 first_activity_day=_day(60), last_activity_day=_day(50)),
        # No combined map, parent evidence in-window, bounds NULL (the shape
        # _collect_trends_from_jsonl leaves): must stay in the scan.
        _session(path="/fake/parent.jsonl", date=_day(3),
                 daily_usage_json=None,
                 parent_daily_usage_json=json.dumps(usage(_day(3))),
                 first_activity_day=None, last_activity_day=None),
        # Empty combined map falls back to parent in _price_session_row.
        _session(path="/fake/empty-daily.jsonl", date=_day(4),
                 daily_usage_json="{}",
                 parent_daily_usage_json=json.dumps(usage(_day(4))),
                 first_activity_day=None, last_activity_day=None),
        # Parent evidence entirely old: kept by the parent clause, contributes
        # no window slices.
        _session(path="/fake/parent-old.jsonl", date=_day(70),
                 daily_usage_json=None,
                 parent_daily_usage_json=json.dumps(usage(_day(70))),
                 first_activity_day=None, last_activity_day=None),
        # Metrics-only recent day: bounds reach the window but every usage day
        # is old, so the row is scanned and contributes nothing.
        _session(path="/fake/metrics.jsonl", date=_day(1),
                 daily_usage_json=json.dumps(usage(_day(60))),
                 first_activity_day=_day(60), last_activity_day=_day(1),
                 daily_metrics_json=json.dumps({_day(1): {"message_count": 3, "duration_minutes": 5}})),
        # Malformed per-day JSON: bounds NULL keeps it; nothing parses.
        _session(path="/fake/bad.jsonl", date="",
                 daily_usage_json="not json", first_activity_day=None,
                 last_activity_day=None),
        # Undated usage: no slices anywhere, marks coverage incomplete.
        _session(path="/fake/undated.jsonl", date="",
                 daily_usage_json=None, first_activity_day=None,
                 last_activity_day=None),
        # Bounds never stamped (jsonl-fallback shape) but usage is in-window.
        _session(path="/fake/unstamped.jsonl", date=_day(6),
                 daily_usage_json=json.dumps(usage(_day(6))),
                 first_activity_day=None, last_activity_day=None),
        # Sidechain row inside the window.
        _session(path="/fake/side.jsonl", date=_day(2), is_sidechain=1,
                 daily_usage_json=json.dumps(usage(_day(2))),
                 first_activity_day=_day(2), last_activity_day=_day(2)),
        # No stored quality: exercises the score_session_quality fallback.
        _session(path="/fake/noquality.jsonl", date=_day(2),
                 daily_usage_json=json.dumps(usage(_day(2))),
                 quality_score=None, quality_grade=None,
                 first_activity_day=_day(2), last_activity_day=_day(2)),
    ]


@pytest.fixture
def m(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    return mod


def test_activity_window_filter_preserves_trends_results(m, monkeypatch):
    conn = m._init_trends_db()
    conn.row_factory = sqlite3.Row
    _insert(conn, _fixture_rows())
    conn.executemany(
        "INSERT INTO skill_daily (date,skill,session_count,invocations) VALUES (?,?,?,?)",
        [(_day(2), "sample", 2, 4), (_day(60), "sample", 1, 1)])
    conn.executemany(
        "INSERT INTO model_daily (date,model,total_tokens) VALUES (?,?,?)",
        [(_day(2), MODEL, 5000), (_day(60), MODEL, 9000)])
    conn.executemany(
        "INSERT INTO subagent_daily (date,agent_type,spawn_count) VALUES (?,?,?)",
        [(_day(2), "general-purpose", 1)])
    conn.commit()

    days = 30
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    pred, params = m._activity_window_predicate(conn, cutoff)
    assert pred, "fixture should engage the activity-window filter"
    kept = conn.execute(
        "SELECT COUNT(*) FROM session_log WHERE 1=1" + pred, params).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM session_log").fetchone()[0]
    assert 0 < kept < total, "fixture must contain both kept and dropped rows"

    filtered = m._query_trends_db(conn, days)
    monkeypatch.setattr(m, "_activity_window_predicate", lambda c, cutoff: ("", ()))
    unfiltered = m._query_trends_db(conn, days)

    assert filtered == unfiltered
    assert filtered["session_count"] > 0
    assert filtered["daily"]
    conn.close()
