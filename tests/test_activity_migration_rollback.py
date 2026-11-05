"""The activity-dating migration is reversible and collision-safe.

`activity_dating_v1` rewrites session_log dates and rebuilds the four aggregate
tables. Before rewriting it must journal the old dates and snapshot the old
aggregates inside the DB, derived JSON state must leave its live name even when
its `.pre-activity-v1` archive already exists, rebuilt anchors must carry an
activity-basis marker readers enforce, and a failure mid-migration must roll the
DB transaction back so a later init can retry cleanly.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from test_usage_counted_once import _load, _noon


@pytest.fixture
def m(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    return mod


def _seed_repairable(m):
    """One session row whose stored date disagrees with its daily evidence,
    plus a sentinel aggregate row, then un-mark the migration for a re-run."""
    conn = m._init_trends_db()
    day = _noon(40).date().isoformat()
    wrong = _noon(70).date().isoformat()
    model = "claude-opus-4-8"
    usage = {day: {model: {"fresh_input": 100, "output": 10}}}
    conn.execute(
        "INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,daily_usage_json) "
        "VALUES ('legacy.jsonl',?,?,?,?)",
        (wrong, 100, 10, json.dumps(usage)))
    conn.execute(
        "INSERT INTO daily_stats (date, session_count, total_input) VALUES ('2020-01-01', 7, 700)")
    conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
    conn.commit()
    conn.close()
    return wrong, day


def _session_date(conn):
    return conn.execute(
        "SELECT date FROM session_log WHERE jsonl_path='legacy.jsonl'").fetchone()[0]


def _marker(conn):
    return conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone()


def test_migration_journals_old_dates_and_backs_up_aggregates(m):
    wrong, day = _seed_repairable(m)
    conn = m._init_trends_db()
    journal = conn.execute(
        "SELECT old_date, old_first_activity_day, old_last_activity_day "
        "FROM activity_dating_v1_journal j JOIN session_log s ON s.id = j.session_id "
        "WHERE s.jsonl_path='legacy.jsonl'").fetchone()
    assert journal is not None
    assert journal[0] == wrong
    assert _session_date(conn) == day
    # The four rebuilt aggregate tables were copied aside before the wipe.
    assert conn.execute(
        "SELECT date, session_count, total_input FROM daily_stats_pre_activity_v1"
    ).fetchall() == [("2020-01-01", 7, 700)]
    for table in ("model_daily", "skill_daily", "subagent_daily"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table}_pre_activity_v1").fetchone()[0] == 0
    # Live aggregates reflect the repaired activity day, not the sentinel.
    assert conn.execute("SELECT date FROM daily_stats").fetchall() == [(day,)]
    conn.close()


def test_rollback_restores_dates_aggregates_and_marker(m):
    wrong, day = _seed_repairable(m)
    conn = m._init_trends_db()
    assert _session_date(conn) == day
    assert m.rollback_activity_dating(conn) is True
    assert _session_date(conn) == wrong
    assert conn.execute(
        "SELECT date, session_count FROM daily_stats").fetchall() == [("2020-01-01", 7)]
    assert _marker(conn) is None
    conn.close()
    # The migration can run again cleanly after a rollback.
    conn = m._init_trends_db()
    assert _session_date(conn) == day
    assert _marker(conn) is not None
    conn.close()


def test_failed_rebuild_rolls_back_and_retries(m, monkeypatch):
    wrong, day = _seed_repairable(m)
    real = m._rebuild_aggregate_tables

    def boom(conn):
        raise sqlite3.OperationalError("injected rebuild failure")

    monkeypatch.setattr(m, "_rebuild_aggregate_tables", boom)
    conn = m._init_trends_db()  # must not raise; the DB stays usable
    assert _session_date(conn) == wrong
    assert _marker(conn) is None
    # The failed attempt's journal rows and aggregate backups rolled back with
    # it; the aggregates still hold the pre-migration sentinel.
    assert conn.execute("SELECT COUNT(*) FROM activity_dating_v1_journal").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM daily_stats_pre_activity_v1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0] == 1
    conn.close()
    monkeypatch.setattr(m, "_rebuild_aggregate_tables", real)
    conn = m._init_trends_db()
    assert _session_date(conn) == day
    assert _marker(conn) is not None
    conn.close()


def test_failed_prestate_snapshot_rolls_back_and_retries(m, monkeypatch):
    wrong, day = _seed_repairable(m)
    real = m._snapshot_activity_dating_prestate

    def boom(conn, old_rows):
        raise sqlite3.OperationalError("injected snapshot failure")

    monkeypatch.setattr(m, "_snapshot_activity_dating_prestate", boom)
    conn = m._init_trends_db()  # must not raise; the DB stays usable
    assert _session_date(conn) == wrong
    assert _marker(conn) is None
    conn.close()
    monkeypatch.setattr(m, "_snapshot_activity_dating_prestate", real)
    conn = m._init_trends_db()
    assert _session_date(conn) == day
    assert _marker(conn) is not None
    conn.close()


def test_failure_between_renames_rolls_back_db_and_retry_converges(m, monkeypatch):
    wrong, day = _seed_repairable(m)
    snap = m.SNAPSHOT_DIR
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "baseline_state.json").write_text('{"old": 1}')
    (snap / "workload_anchor.json").write_text('{"old": 2}')
    derived = {"baseline_state.json", "workload_anchor.json"}
    real_rename = Path.rename
    calls = []

    def flaky_rename(self, target):
        if self.name in derived and self.parent == snap:
            calls.append(self.name)
            if len(calls) == 2:
                raise OSError("injected rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    conn = m._init_trends_db()
    assert _session_date(conn) == wrong
    assert _marker(conn) is None
    conn.close()
    # The first rename already happened and is not undone; the second file
    # still holds its live name, and the DB side rolled back.
    assert calls == ["baseline_state.json", "workload_anchor.json"]
    assert not (snap / "baseline_state.json").exists()
    assert (snap / "baseline_state.json.pre-activity-v1").exists()
    assert (snap / "workload_anchor.json").exists()
    monkeypatch.setattr(Path, "rename", real_rename)
    conn = m._init_trends_db()
    assert _session_date(conn) == day
    assert _marker(conn) is not None
    conn.close()
    assert not (snap / "workload_anchor.json").exists()
    assert (snap / "workload_anchor.json.pre-activity-v1").exists()


def test_existing_archive_does_not_rescue_a_stale_anchor(m):
    snap = m.SNAPSHOT_DIR
    snap.mkdir(parents=True, exist_ok=True)
    stale = {"month": "2026-01",
             "metrics": {"sessions": 200, "usd": 1.0, "tokens": 1.0,
                         "flat_usd": 999.0, "api_calls": 9, "messages": 9},
             "rates": m._WEIGHT_POOL_FLAT_RATES}
    (snap / "workload_anchor.json").write_text(json.dumps(stale))
    (snap / "workload_anchor.json.pre-activity-v1").write_text('{"earlier": true}')
    conn = m._init_trends_db()
    conn.close()
    # The stale active copy left its live name; both JSON copies are kept.
    assert not (snap / "workload_anchor.json").exists()
    assert (snap / "workload_anchor.json.pre-activity-v1").read_text() == '{"earlier": true}'
    assert len(list(snap.glob("workload_anchor.json.pre-activity-v1*"))) == 2
    metrics, _month = m._stable_workload_anchor(None, "2026-10")
    assert metrics is None


def test_workload_anchor_requires_the_activity_basis_marker(m):
    snap = m.SNAPSHOT_DIR
    snap.mkdir(parents=True, exist_ok=True)
    metrics_dict = {"sessions": 200, "usd": 900.0, "tokens": 1e9,
                    "flat_usd": 1000.0, "api_calls": 5000, "messages": 6000}
    # A well-formed pre-activity anchor is rejected: its figures were computed
    # on session-date windows, not activity days.
    (snap / "workload_anchor.json").write_text(json.dumps(
        {"month": "2026-03", "metrics": metrics_dict,
         "rates": m._WEIGHT_POOL_FLAT_RATES}))
    assert m._stable_workload_anchor(None, "2026-10") == (None, "2026-10")
    (snap / "workload_anchor.json").write_text(json.dumps(
        {"month": "2026-03", "metrics": metrics_dict,
         "rates": m._WEIGHT_POOL_FLAT_RATES, "activity_basis": 1}))
    metrics, month = m._stable_workload_anchor(None, "2026-10")
    assert month == "2026-03" and metrics["flat_usd"] == 1000.0
