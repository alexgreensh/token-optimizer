"""Unmeasured quality must never be stored or averaged as 100 (#360).

When the quality parser cannot measure a session (no context occupancy, the
normal case for Codex transcripts), compute_quality_score returns
grade "?", measured False, and a fallback score (last_measured_score, default
100). session_log stored that fallback, and the daily_stats aggregate averaged
it as a real score: 491 unmeasured sessions read as a perfect 100/day. The
write path now stores NULL, the aggregate excludes grade '?', a guarded
migration repairs rows already stored, and the metrics backfill no longer
re-parses deliberately-unmeasured rows every flush.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from test_usage_counted_once import _load, _noon, _utc  # noqa: E402


@pytest.fixture
def m(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


def _text_only_transcript(m, sid):
    """A session transcript with no usage/occupancy records: unmeasurable."""
    path = m.CLAUDE_DIR / "projects" / "sample" / (sid + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _utc(_noon(1))
    records = [
        {"type": "user", "timestamp": ts, "message": {"content": "triage the flaky test"}},
        {"type": "assistant", "timestamp": ts,
         "message": {"content": [{"type": "text", "text": "mocked the clock"}]}},
        {"type": "user", "timestamp": ts, "message": {"content": "thanks"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _session_row(m):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        return conn.execute(
            "SELECT quality_score, quality_grade FROM session_log").fetchall()
    finally:
        conn.close()


def test_unmeasured_session_stores_null_score(m):
    sid = "cccccccc-2222-4333-8444-555555555560"
    _text_only_transcript(m, sid)
    m.collect_sessions(days=120, quiet=True)
    rows = _session_row(m)
    assert len(rows) == 1
    assert rows[0][0] is None, "unmeasured session stored a fallback score"
    assert rows[0][1] == "?"


def test_daily_average_excludes_unmeasured(m):
    conn = m._init_trends_db()
    try:
        day = _noon(1).date().isoformat()
        usage = json.dumps({day: {"claude-opus-4-8": {"fresh_input": 10, "output": 10}}})
        for i, (score, grade) in enumerate(((77.3, "B"), (100.0, "?"), (100.0, "?"))):
            conn.execute(
                "INSERT INTO session_log (jsonl_path, date, project, duration_minutes,"
                " input_tokens, output_tokens, message_count, cache_hit_rate,"
                " collected_at, quality_score, quality_grade, daily_usage_json)"
                " VALUES (?, ?, 'p', 1, 10, 10, 2, 0, ?, ?, ?, ?)",
                (f"/t/{i}.jsonl", day, datetime.now().isoformat(), score, grade, usage))
        conn.commit()
        m.ensure_activity_schema(conn)
        m._rebuild_aggregate_tables(conn)
        avg, worst = conn.execute(
            "SELECT avg_quality_score, worst_grade FROM daily_stats WHERE date = ?",
            (day,)).fetchone()
        assert avg == 77.3, "the two phantom 100s leaked into the daily average"
        assert worst == "B"
    finally:
        conn.close()


def test_migration_repairs_phantom_scores(m):
    conn = m._init_trends_db()
    try:
        day = _noon(1).date().isoformat()
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, project, duration_minutes,"
            " input_tokens, output_tokens, message_count, cache_hit_rate,"
            " collected_at, quality_score, quality_grade)"
            " VALUES ('/t/old.jsonl', ?, 'p', 1, 10, 10, 2, 0, ?, 100.0, '?')",
            (day, datetime.now().isoformat()))
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, project, duration_minutes,"
            " input_tokens, output_tokens, message_count, cache_hit_rate,"
            " collected_at, quality_score, quality_grade)"
            " VALUES ('/t/measured.jsonl', ?, 'p', 1, 10, 10, 2, 0, ?, 77.3, 'B')",
            (day, datetime.now().isoformat()))
        conn.commit()
        # Seed the phantoms as if they predate the upgrade: the first init
        # above already recorded the migration marker, so reset it.
        conn.execute(
            "DELETE FROM schema_migrations WHERE name='unmeasured_quality_null_v1'")
        conn.commit()
    finally:
        conn.close()
    # The next DB init runs the guarded one-time repair.
    conn = m._init_trends_db()
    try:
        rows = dict(conn.execute(
            "SELECT jsonl_path, quality_score FROM session_log").fetchall())
        assert rows["/t/old.jsonl"] is None, "phantom 100 survived the migration"
        assert rows["/t/measured.jsonl"] == 77.3, "migration touched a measured row"
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name='unmeasured_quality_null_v1'"
        ).fetchone()
    finally:
        conn.close()


def test_backfill_does_not_reparse_unmeasured_rows(m, monkeypatch, tmp_path):
    conn = m._init_trends_db()
    try:
        day = _noon(1).date().isoformat()
        # Real files, so the backfill's os.path.exists guard lets the parse
        # through whenever a row is actually selected.
        unmeasured_f = tmp_path / "unmeasured.jsonl"
        unmeasured_f.write_text("", encoding="utf-8")
        gap_f = tmp_path / "gap.jsonl"
        gap_f.write_text("", encoding="utf-8")
        # A converged unmeasured row: TTL scanned, gaps filled, NULL score by design.
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, project, duration_minutes,"
            " input_tokens, output_tokens, message_count, cache_hit_rate,"
            " cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,"
            " avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,"
            " collected_at, quality_score, quality_grade)"
            " VALUES (?, ?, 'p', 1, 10, 10, 2, 0,"
            " 0, 0, 1, 1.0, 1.0, 1.0, ?, NULL, '?')",
            (str(unmeasured_f), day, datetime.now().isoformat()))
        # A measured row missing its quality score: a real gap to backfill.
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, project, duration_minutes,"
            " input_tokens, output_tokens, message_count, cache_hit_rate,"
            " cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,"
            " avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,"
            " collected_at, quality_score, quality_grade)"
            " VALUES (?, ?, 'p', 1, 10, 10, 2, 0,"
            " 0, 0, 1, 1.0, 1.0, 1.0, ?, NULL, 'B')",
            (str(gap_f), day, datetime.now().isoformat()))
        conn.commit()
        parsed_calls = []
        monkeypatch.setattr(m, "_parse_session_jsonl",
                            lambda p: parsed_calls.append(p) or None)
        m._backfill_session_metrics(conn, days=30, limit=50)
        assert parsed_calls == [str(gap_f)], (
            "backfill must skip the converged unmeasured row and still pick the real gap")
    finally:
        conn.close()


def _usage_only_transcripts(m, count=6):
    """Sessions with usage records (validate_impact keeps them) but no
    context-occupancy data, so quality is unmeasured - the common Codex case
    and what minimal Claude transcripts parse to."""
    from test_usage_counted_once import _chunk
    for i in range(count):
        sid = "dddddddd-2222-4333-8444-55555555556%d" % i
        path = m.CLAUDE_DIR / "projects" / "sample" / (sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        when = _noon(10 - i)
        records = [
            {"type": "user", "timestamp": _utc(when),
             "message": {"content": "do thing %d" % i}},
            _chunk(when, req="req-%d" % i, inp=2000, out=300),
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                        encoding="utf-8")


def test_validate_impact_all_unmeasured_window_json(m):
    """Regression for the reviewer-found crash: _delta did (a - b) with both
    avg_quality None -> TypeError. Unmeasured windows must yield None deltas,
    not a crash and not a phantom 0/100 comparison."""
    _usage_only_transcripts(m)
    result = m.validate_impact(strategy="halves", days=120, as_json=True)
    assert result["before_summary"]["avg_quality"] is None
    assert result["after_summary"]["avg_quality"] is None
    assert result["deltas"]["quality_pct"] is None
    # The other metrics stay numeric and still drive the verdict.
    assert isinstance(result["deltas"]["tokens_pct"], (int, float))
    assert result["verdict"] in ("improved", "regressed", "no_change")


def test_validate_impact_all_unmeasured_window_text(m, capsys):
    """The text render formatted None with {:>10.1f} -> TypeError. It now
    prints n/a for unmeasured quality."""
    _usage_only_transcripts(m)
    m.validate_impact(strategy="halves", days=120, as_json=False)
    out = capsys.readouterr().out
    assert "Avg quality score" in out
    assert "n/a" in out
