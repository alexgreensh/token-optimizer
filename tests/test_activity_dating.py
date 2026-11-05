"""Usage windows follow request activity, independent of transcript ingestion."""
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from test_usage_counted_once import _load, _noon, _chunk, _write, SID_A, SID_B


@pytest.fixture
def m(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    return mod


def transcript(m, sid=SID_A):
    return m.CLAUDE_DIR / "projects" / "sample" / (sid + ".jsonl")


def spent(m):
    conn = m._init_trends_db()
    try:
        return m._dashboard_spent_token_basis(conn, 30)
    finally:
        conn.close()


def collect(m):
    m.collect_sessions(days=120, quiet=True)


def test_touched_old_session_does_not_enter_volume_window(m):
    path = transcript(m)
    _write(path, [_chunk(_noon(70), req="old", inp=1000, out=100)])
    collect(m)
    assert spent(m)["tokens"] == 0
    os.utime(path, None)
    collect(m)
    assert spent(m)["tokens"] == 0
    assert m._model_mix_shares(days=30)["shares"] == {}
    conn = m._init_trends_db()
    assert conn.execute("SELECT date FROM session_log").fetchone()[0] == _noon(70).date().isoformat()
    assert conn.execute("SELECT COUNT(*) FROM daily_stats WHERE date >= date('now','-30 day')").fetchone()[0] == 0
    conn.close()


def test_crossing_window_and_resumed_session_only_count_new_usage(m):
    _write(transcript(m), [_chunk(_noon(70), req="old", inp=9000, out=900),
                            _chunk(_noon(1), req="new", inp=1000, out=100)])
    collect(m)
    assert spent(m)["tokens"] == 1100
    conn = m._init_trends_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT date,total_input,total_output FROM daily_stats ORDER BY date").fetchall()
    assert [(r["total_input"], r["total_output"]) for r in rows] == [(9000, 900), (1000, 100)]
    result = m._price_parent_window(conn, "AND date >= ?", (_noon(30).date().isoformat(),), "standard")
    assert result["tokens"] == 1100
    assert result["api_calls"] == 1
    conn.close()


def test_three_stream_observations_and_replayed_request_count_once(m):
    records = [_chunk(_noon(1), req="shared", inp=1000, out=100)] * 3
    _write(transcript(m), records)
    _write(transcript(m, SID_B), records)
    collect(m)
    assert spent(m)["tokens"] == 1100
    assert spent(m)["complete"] is True


@pytest.mark.parametrize("usage", [None, {}, {"undated": {"claude-opus-4-8": {"fresh_input": 10, "output": 2}}}])
def test_missing_or_undated_usage_is_incomplete_never_mtime_dated(m, usage):
    conn = m._init_trends_db()
    conn.execute("INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,reported_input_tokens,reported_output_tokens,daily_usage_json) VALUES ('missing.jsonl',?,10,2,30,6,?)",
                 (datetime.now().date().isoformat(), json.dumps(usage) if usage is not None else None))
    conn.commit()
    basis = m._dashboard_spent_token_basis(conn, 30)
    assert basis["tokens"] == 0
    assert basis["complete"] is False
    assert basis["fallback_rows"] == 1
    conn.close()


def test_migration_repairs_corrupt_date_and_rebuilds_aggregates(m):
    conn = m._init_trends_db()
    day = _noon(70).date().isoformat()
    model = "claude-opus-4-8"
    usage = {day: {model: {"fresh_input": 100, "output": 10}}}
    conn.execute("INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,daily_usage_json,all_model_usage_json,skills_json,subagents_json) VALUES ('missing.jsonl',?,100,10,?,?,?,?)",
                 (datetime.now().date().isoformat(), json.dumps(usage), json.dumps({model: 110}), '{"sample":2}', '{"research":1}'))
    conn.execute("DELETE FROM schema_migrations WHERE name = 'activity_dating_v1'")
    conn.commit()
    conn.close()
    conn = m._init_trends_db()
    row = conn.execute("SELECT date,first_activity_day,last_activity_day FROM session_log").fetchone()
    assert tuple(row) == (day, day, day)
    for table in ("daily_stats", "model_daily", "skill_daily", "subagent_daily"):
        assert {r[0] for r in conn.execute(f"SELECT date FROM {table}")} == {day}
    conn.close()


def test_model_mix_uses_days_inside_window(m):
    _write(transcript(m), [_chunk(_noon(70), req="old", inp=9000, out=900, model="claude-sonnet-4-6"),
                            _chunk(_noon(1), req="new", inp=1000, out=100, model="claude-opus-4-8")])
    collect(m)
    shares = m._model_mix_shares(days=30)["shares"]
    assert len(shares) == 1 and next(iter(shares.values())) == 1.0


def test_session_cost_date_query_totals_only_matching_day(m):
    _write(transcript(m), [_chunk(_noon(70), req="old", inp=9000, out=900),
                            _chunk(_noon(1), req="new", inp=1000, out=100)])
    collect(m)
    result = m.session_cost_query(["--date", _noon(1).date().isoformat()])
    assert result["match_count"] == 1
    entry = result["sessions"][0]
    assert entry["cost_usd"]["total"] > entry["cost_usd"]["on_date"]
    assert result["total_cost_usd"] == entry["cost_usd"]["on_date"]


def test_jsonl_fallback_slices_activity_and_deduplicates(m):
    _write(transcript(m), [_chunk(_noon(70), req="old", inp=9000, out=900),
                            *([_chunk(_noon(1), req="new", inp=1000, out=100)] * 3)])
    result = m._collect_trends_from_jsonl(30)
    assert result["total_tokens"] == 1100
    assert result["total_input_tokens"] == 1000


def test_activity_bounds_and_ingestion_metadata_are_separate(m):
    _write(transcript(m), [_chunk(_noon(70), req="old"), _chunk(_noon(1), req="new")])
    collect(m)
    conn = m._init_trends_db()
    row = conn.execute("SELECT first_activity_day,last_activity_day,transcript_mtime FROM session_log").fetchone()
    assert tuple(row[:2]) == (_noon(70).date().isoformat(), _noon(1).date().isoformat())
    assert row[2] == transcript(m).stat().st_mtime
    conn.close()


def test_trends_volumes_and_population_are_activity_dated(m):
    _write(transcript(m), [_chunk(_noon(70), req="old", inp=9000, out=900),
                            _chunk(_noon(1), req="new", inp=1000, out=100)])
    collect(m)
    conn = m._init_trends_db()
    conn.row_factory = sqlite3.Row
    trends = m._query_trends_db(conn, 30)
    assert trends["session_count"] == 1
    assert trends["total_tokens"] == 1100
    assert trends["total_input_tokens"] == 1000
    assert trends["total_output_tokens"] == 100
    assert len(trends["daily"]) == 1
    assert trends["daily"][0]["session_details"][0]["metadata_basis"].startswith("lifetime")
    population = m._query_dashboard_population_metrics(conn, 30)
    assert population["all_sessions"] == 1
    conn.close()


def test_missing_transcript_keeps_dated_evidence_and_unknown_coverage(m):
    path = transcript(m)
    _write(path, [_chunk(_noon(1), req="recent", inp=1000, out=100)])
    collect(m)
    path.unlink()
    assert spent(m)["tokens"] == 1100
    assert spent(m)["complete"] is True


def test_fallback_ignores_preserved_early_mtime(m):
    path = transcript(m)
    _write(path, [_chunk(_noon(1), req="recent", inp=1000, out=100)], mtime=_noon(100).timestamp())
    assert m._collect_trends_from_jsonl(30)["total_tokens"] == 1100
    collect(m)
    assert spent(m)["tokens"] == 1100


def test_real_undated_request_is_incomplete_and_never_gets_a_day(m):
    row = _chunk(_noon(1), req="unknown", inp=1000, out=100)
    row.pop("timestamp")
    path = transcript(m)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row) + "\n")
    collect(m)
    assert spent(m)["tokens"] == 0
    assert spent(m)["complete"] is False
    conn = m._init_trends_db()
    assert conn.execute("SELECT date FROM session_log").fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0] == 0
    conn.row_factory = sqlite3.Row
    result = m._query_trends_db(conn, 30)
    assert result["session_count"] == 0
    assert result["spent_token_basis"]["complete"] is False
    conn.close()


def test_dated_work_units_and_tools_are_split_at_the_window_edge(m):
    old = _chunk(_noon(70), req="old", inp=9000, out=900)
    new = _chunk(_noon(1), req="new", inp=1000, out=100)
    for row in (old, new):
        row["message"]["content"].append({"type": "tool_use", "name": "Write", "id": row["requestId"], "input": {}})
    _write(transcript(m), [old, new])
    collect(m)
    assert m._sum_tool_calls(days=30)["Write"] == 1
    conn = m._init_trends_db()
    result = m._price_parent_window(conn, "AND date >= ?", (_noon(30).date().isoformat(),), "anthropic")
    assert result["api_calls"] == 1
    assert result["messages"] == 1
    conn.close()


def test_migration_archives_derived_state_and_does_not_rewrite_events(m):
    conn = m._init_trends_db()
    conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
    stamp = _noon(70).isoformat()
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved) VALUES (?, 'checkpoint_restore', 42)", (stamp,))
    conn.commit()
    conn.close()

    for name in ("baseline_state.json", "workload_anchor.json", "weekly_full_value.json"):
        (m.SNAPSHOT_DIR / name).write_text('{"old_basis":true}')
    conn = m._init_trends_db()
    assert conn.execute("SELECT timestamp,tokens_saved FROM savings_events").fetchone() == (stamp, 42)
    for name in ("baseline_state.json", "workload_anchor.json", "weekly_full_value.json"):
        assert not (m.SNAPSHOT_DIR / name).exists()
        assert (m.SNAPSHOT_DIR / (name + ".pre-activity-v1")).read_text() == '{"old_basis":true}'
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='activity_dating_v1'").fetchone()[0] == 1
    conn.close()


def test_codex_cumulative_usage_is_split_by_activity_day(m):
    def record(ago, total, latest):
        usage = lambda n: {"input_tokens": n, "cached_input_tokens": 0, "output_tokens": n // 10, "reasoning_output_tokens": 0}
        return {"type": "event_msg", "timestamp": _noon(ago).isoformat(), "payload": {
            "type": "token_count", "info": {"total_token_usage": usage(total), "last_token_usage": usage(latest)}}}
    result = m.codex_session._parse_session_records([
        {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
        record(70, 9000, 9000), record(1, 10000, 1000), record(1, 10000, 1000)])
    assert result["daily_usage"][_noon(70).date().isoformat()]["gpt-5.4"]["fresh_input"] == 9000
    assert result["daily_usage"][_noon(1).date().isoformat()]["gpt-5.4"]["fresh_input"] == 1000


def test_multi_day_session_does_not_manufacture_baseline_population(m):
    _write(transcript(m), [_chunk(_noon(ago), req=f"request-{ago}", inp=1000, out=100)
                            for ago in range(70, 30, -1)])
    collect(m)
    assert m._compute_baseline_state() is None
    assert m._baseline_progress()["sessions_in_window"] == 1


def test_delegated_skills_keep_the_child_activity_day_and_shared_usage_counts_once(m):
    shared = _chunk(_noon(1), req="shared", inp=1000, out=100)
    _write(transcript(m), [shared])
    child = transcript(m).with_suffix("") / "subagents" / "agent-sample.jsonl"
    old = _chunk(_noon(70), req="child-old", inp=9000, out=900)
    old["message"]["content"].append({"type": "tool_use", "name": "Skill", "input": {"skill": "sample-old"}})
    _write(child, [old, shared])
    collect(m)
    assert spent(m)["tokens"] == 1100
    conn = m._init_trends_db()
    assert conn.execute("SELECT date,invocations FROM skill_daily WHERE skill='sample-old'").fetchall() == [(_noon(70).date().isoformat(), 1)]
    conn.close()
