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


def _assert_activity_bounds(conn, m):
    rows = conn.execute(
        "SELECT first_activity_day,last_activity_day,date,daily_usage_json,"
        "parent_daily_usage_json,daily_metrics_json FROM session_log").fetchall()
    assert rows
    for first, last, activity_date, *maps in rows:
        days = sorted({day for raw in maps for day in m._activity_days(m._safe_json_dict(raw))})
        if days:
            assert first is not None and first <= days[0]
            assert last is not None and last >= days[-1]
            assert activity_date == last


@pytest.mark.parametrize("writer", ["collect", "backfill", "migration", "adapter", "fallback"])
@pytest.mark.parametrize("latest_source", ["parent", "metrics"])
def test_every_writer_bounds_combined_parent_and_metadata_days(m, monkeypatch, writer, latest_source):
    old, parent_day, metric_day = [_noon(n).date().isoformat() for n in (70, 5, 1)]
    path = transcript(m)
    _write(path, [_chunk(_noon(5), req="parent", inp=1000, out=100)])
    parsed = m._parse_session_jsonl(path)
    monkeypatch.setattr(m, "_parse_session_jsonl", lambda *a, **k: parsed)
    monkeypatch.setattr(m, "_find_all_jsonl_files",
                        lambda days: [(path, path.stat().st_mtime, "sample")])
    combined = {old: {"claude-opus-4-8": {"fresh_input": 100, "output": 10}}}
    parent = {parent_day: {"claude-opus-4-8": {"fresh_input": 1000, "output": 100}}}
    # Each source must widen bounds independently, without another source
    # hiding its omission.
    metrics = {metric_day if latest_source == "metrics" else old: {"message_count": 1}}
    monkeypatch.setattr(m, "_daily_usage_from_merged", lambda merged: combined)
    monkeypatch.setattr(m, "_session_daily_metrics", lambda *a, **k: metrics)
    if writer == "fallback":
        real_query = m._query_trends_db
        checked = []

        def check(conn, days):
            _assert_activity_bounds(conn, m)
            checked.append(True)
            return real_query(conn, days)

        monkeypatch.setattr(m, "_query_trends_db", check)
        m._collect_trends_from_jsonl(30)
        assert checked
        return
    if writer == "collect":
        collect(m)
        conn = m._init_trends_db()
    else:
        conn = m._init_trends_db()
        conn.execute(
            "INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,daily_usage_json,"
            "parent_daily_usage_json,daily_metrics_json) VALUES (?,?,?,?,?,?,?)",
            (str(path), old, 1000, 100, None if writer == "backfill" else json.dumps(combined),
             json.dumps(parent), json.dumps(metrics)))
        conn.commit()
        if writer == "backfill":
            assert m._backfill_daily_usage(conn, budget_seconds=5) == 1
        elif writer == "migration":
            conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
            conn.commit()
            conn.close()
            conn = m._init_trends_db()
        else:
            parsed = m._parse_session_jsonl(path)
            assert m._store_adapter_activity(conn, str(path), parsed) == 1
            conn.commit()
    try:
        _assert_activity_bounds(conn, m)
    finally:
        conn.close()


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


def test_jsonl_fallback_preserves_session_metadata_and_call_units(m, monkeypatch):
    """The DB-free fallback stores the same schema contract as collect: call-gap
    stats, cache-write splits, lifetime counts and dated per-day call units."""
    t0 = _noon(3)
    rows = [
        _chunk(t0, req="a", inp=1000, out=100),
        _chunk(t0 + timedelta(seconds=600), req="b", inp=1000, out=100),
        _chunk(t0 + timedelta(seconds=1800), req="c", inp=1000, out=100),
    ]
    rows[0]["message"]["usage"]["cache_creation_input_tokens"] = 100
    rows[0]["message"]["usage"]["ephemeral_1h_input_tokens"] = 40
    rows[0]["message"]["usage"]["ephemeral_5m_input_tokens"] = 60
    _write(transcript(m), rows)
    collect(m)
    conn = m._init_trends_db()
    db = conn.execute(
        "SELECT avg_call_gap_seconds,max_call_gap_seconds,p95_call_gap_seconds,"
        "cache_create_1h_tokens,cache_create_5m_tokens,api_calls,message_count,"
        "daily_calls_json FROM session_log").fetchone()
    conn.close()
    seen = {}
    real_query = m._query_trends_db

    def spy(conn, days):
        seen["daily_calls"] = [
            json.loads(r[0]) for r in conn.execute("SELECT daily_calls_json FROM session_log")]
        return real_query(conn, days)

    monkeypatch.setattr(m, "_query_trends_db", spy)
    result = m._collect_trends_from_jsonl(30)
    details = [sd for d in result["daily"] for sd in d["session_details"]]
    assert len(details) == 1
    sd = details[0]
    assert (sd["avg_call_gap_seconds"], sd["max_call_gap_seconds"],
            sd["p95_call_gap_seconds"]) == tuple(db[:3])
    assert (sd["cache_create_1h_tokens"], sd["cache_create_5m_tokens"]) == (db[3], db[4])
    assert sd["api_calls"] == db[5] and sd["message_count"] == db[6]
    assert seen["daily_calls"] == [json.loads(db[7])]


def test_jsonl_fallback_keeps_adapter_daily_calls(m, monkeypatch):
    """Adapters with dated call counts but no request map keep daily_calls and
    the activity bounds in the DB-free fallback."""
    day = _noon(5).date().isoformat()
    parsed = {
        "version": "test", "slug": "s", "topic": None,
        "duration_minutes": 12.0, "total_input_tokens": 5000,
        "total_output_tokens": 500, "total_cache_read": 0,
        "total_cache_create": 0, "total_cache_create_1h": 0,
        "total_cache_create_5m": 0, "cache_hit_rate": 0.0,
        "avg_call_gap_seconds": 30.0, "max_call_gap_seconds": 60.0,
        "p95_call_gap_seconds": 60.0,
        "model_usage": {"gpt-5.4": 5500},
        "model_usage_breakdown": {"gpt-5.4": {"fresh_input": 5000, "cache_read": 0,
                                               "cache_create": 0, "output": 500}},
        "daily_usage": {day: {"gpt-5.4": {"fresh_input": 5000, "cache_read": 0,
                                          "cache_create": 0, "output": 500}}},
        "daily_calls": {day: 4},
        "daily_metrics": {day: {"message_count": 9, "duration_minutes": 12.0}},
        "skills_used": {}, "subagents_used": {}, "tool_calls": {},
        "message_count": 9, "api_calls": 4, "is_sidechain": False,
    }
    fake = m.CLAUDE_DIR / "projects" / "sample" / "adapter-session.jsonl"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("{}\n")
    monkeypatch.setattr(m, "_parse_session_jsonl", lambda *a, **k: parsed)
    monkeypatch.setattr(m, "_find_all_jsonl_files",
                        lambda days: [(fake, fake.stat().st_mtime, "sample")])
    seen = {}
    real_query = m._query_trends_db

    def spy(conn, days):
        seen["rows"] = conn.execute(
            "SELECT daily_calls_json, avg_call_gap_seconds, max_call_gap_seconds, "
            "p95_call_gap_seconds, first_activity_day, last_activity_day "
            "FROM session_log").fetchall()
        return real_query(conn, days)

    monkeypatch.setattr(m, "_query_trends_db", spy)
    result = m._collect_trends_from_jsonl(30)
    assert result is not None
    row = seen["rows"][0]
    assert json.loads(row[0]) == {day: 4}
    assert tuple(row[1:4]) == (30.0, 60.0, 60.0)
    assert (row[4], row[5]) == (day, day)
    details = [sd for d in result["daily"] for sd in d["session_details"]]
    assert details and details[0]["avg_call_gap_seconds"] == 30.0


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


def test_legacy_combined_usage_is_labelled_not_certified_top_level(m):
    conn = m._init_trends_db()
    day = _noon(1).date().isoformat()
    model = "claude-opus-4-8"
    # daily_usage_json merges parent + delegated requests; the parent-only map a
    # reparsed transcript stores in parent_daily_usage_json is much smaller.
    combined = {day: {model: {"fresh_input": 1000, "output": 100}}}
    parent = {day: {model: {"fresh_input": 100, "output": 10}}}
    conn.execute("INSERT INTO session_log (jsonl_path,date,daily_usage_json) VALUES ('legacy.jsonl',?,?)",
                 (day, json.dumps(combined)))
    conn.execute("INSERT INTO session_log (jsonl_path,date,daily_usage_json,parent_daily_usage_json) VALUES ('reparsed.jsonl',?,?,?)",
                 (day, json.dumps(combined), json.dumps(parent)))
    conn.commit()
    basis = m._dashboard_spent_token_basis(conn, 30)
    assert basis["tokens"] == 1210
    assert basis["fallback_rows"] == 0
    assert basis["combined_fallback_rows"] == 1
    assert basis["complete"] is False
    assert basis["basis"] != m._DASHBOARD_SPENT_TOKEN_BASIS
    conn.row_factory = sqlite3.Row
    trends = m._query_trends_db(conn, 30)
    assert trends["spent_token_basis"]["combined_fallback_rows"] == 1
    conn.execute("DELETE FROM session_log WHERE jsonl_path='legacy.jsonl'")
    conn.commit()
    basis = m._dashboard_spent_token_basis(conn, 30)
    assert basis["tokens"] == 110
    assert basis["complete"] is True
    assert basis["combined_fallback_rows"] == 0
    conn.close()


def test_missing_dated_call_evidence_is_unknown_not_measured_zero(m):
    conn = m._init_trends_db()
    day1 = _noon(5).date().isoformat()
    day2 = _noon(1).date().isoformat()
    model = "claude-opus-4-8"
    single = {day1: {model: {"fresh_input": 100, "output": 10}}}
    multi = {day1: {model: {"fresh_input": 200, "output": 20}},
             day2: {model: {"fresh_input": 300, "output": 30}}}
    conn.execute("INSERT INTO session_log (jsonl_path,date,api_calls,daily_usage_json) VALUES ('single.jsonl',?,3,?)",
                 (day1, json.dumps(single)))
    conn.execute("INSERT INTO session_log (jsonl_path,date,api_calls,daily_usage_json) VALUES ('multi.jsonl',?,9,?)",
                 (day2, json.dumps(multi)))
    conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
    conn.commit()
    conn.close()
    conn = m._init_trends_db()
    calls = {r[0]: r[1] for r in conn.execute("SELECT jsonl_path,daily_calls_json FROM session_log")}
    assert json.loads(calls["single.jsonl"]) == {day1: 3}
    assert calls["multi.jsonl"] is None
    m.ensure_activity_view(conn)
    rows = conn.execute(
        "SELECT jsonl_path,date,api_calls,api_calls_known FROM session_activity "
        "ORDER BY jsonl_path,date").fetchall()
    assert [(r[0], r[2], r[3]) for r in rows] == [
        ("multi.jsonl", None, 0), ("multi.jsonl", None, 0), ("single.jsonl", 3, 1)]
    result = m._price_parent_window(conn, "AND date >= ?", (_noon(30).date().isoformat(),), "anthropic")
    assert result["api_calls"] == 3
    assert result["api_calls_unknown"] == 1
    terms = m._compound_structural(100, 30, fallback_prefix=1000)
    assert terms["api_calls_complete"] is False
    assert terms["api_calls_unknown_days"] == 2
    conn.execute("DELETE FROM session_log WHERE jsonl_path='multi.jsonl'")
    conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
    conn.commit()
    conn.close()
    conn = m._init_trends_db()
    result = m._price_parent_window(conn, "AND date >= ?", (_noon(30).date().isoformat(),), "anthropic")
    assert result["api_calls"] == 3
    assert result["api_calls_unknown"] == 0
    terms = m._compound_structural(100, 30, fallback_prefix=1000)
    assert terms["api_calls_complete"] is True
    conn.close()


def test_metadata_only_activity_day_survives_the_projection(m):
    conn = m._init_trends_db()
    day_use = _noon(5).date().isoformat()
    day_meta = _noon(1).date().isoformat()
    model = "claude-opus-4-8"
    usage = {day_use: {model: {"fresh_input": 100, "output": 10}}}
    metrics = {
        day_use: {"message_count": 2, "duration_minutes": 3.0,
                  "skills": {"sample": 1}, "subagents": {}, "tool_calls": {"Read": 1}},
        day_meta: {"message_count": 4, "duration_minutes": 1.5,
                   "skills": {"sample": 2}, "subagents": {"research": 1},
                   "tool_calls": {"Write": 2, "Read": 1}},
    }
    conn.execute(
        "INSERT INTO session_log (jsonl_path,date,first_activity_day,last_activity_day,"
        "daily_usage_json,daily_metrics_json,quality_score,quality_grade) "
        "VALUES ('meta.jsonl',?,?,?,?,?,?,?)",
        (day_meta, day_use, day_meta, json.dumps(usage), json.dumps(metrics), 0.9, "A"))
    conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
    conn.commit()
    conn.close()
    conn = m._init_trends_db()
    m.ensure_activity_view(conn)
    conn.row_factory = sqlite3.Row
    rows = {r["date"]: r for r in conn.execute(
        "SELECT date,input_tokens,output_tokens,message_count,tool_calls_json,"
        "skills_json,subagents_json,quality_score,quality_grade FROM session_activity")}
    assert set(rows) == {day_use, day_meta}
    meta = rows[day_meta]
    assert meta["input_tokens"] == 0 and meta["output_tokens"] == 0
    assert meta["message_count"] == 4
    assert json.loads(meta["tool_calls_json"]) == {"Write": 2, "Read": 1}
    assert json.loads(meta["skills_json"]) == {"sample": 2}
    assert json.loads(meta["subagents_json"]) == {"research": 1}
    assert meta["quality_score"] == 0.9 and meta["quality_grade"] == "A"
    for table in ("daily_stats", "skill_daily", "subagent_daily"):
        assert day_meta in {r[0] for r in conn.execute(f"SELECT date FROM {table}")}
    conn.close()
