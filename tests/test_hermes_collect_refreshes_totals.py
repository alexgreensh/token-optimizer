"""A Hermes session collected mid-session must not freeze its token totals.

WHY THIS EXISTS
---------------
Hermes rows are keyed ``hermes:<slug>`` and were written INSERT OR IGNORE. A
dashboard refresh or ``hermes-summary`` that collected a session while it was
still running stored its totals at that moment, and every later collect
(including the session's own end rollup) only refreshed grade and skills, so
the row stayed at the partial totals forever. Later collects now replace the
per-session numbers when the new snapshot is further along, and a stale
snapshot (behind on any counter) never overwrites a newer row.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hermes_state  # noqa: E402
import measure  # noqa: E402

# Keep the fake session (started 10 min before NOW) on one UTC day: right after
# midnight it would start yesterday and today's totals would read zero.
NOW = max(time.time(), time.time() // 86400 * 86400 + 900)


def _row(**over):
    row = {
        "id": "sess-grow", "model": "gpt-4o",
        "started_at": NOW - 600, "ended_at": None,
        "message_count": 6, "api_call_count": 3,
        "input_tokens": 10_000, "output_tokens": 1_000,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_status": "unknown",
    }
    row.update(over)
    return row


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "trends.db"
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(measure, "_HERMES_ROLLUP_CONTEXT", {}, raising=False)
    monkeypatch.setattr(measure, "_HERMES_ROLLUP_SKILLS", {}, raising=False)
    state = {"row": _row()}
    monkeypatch.setattr(
        hermes_state, "recent_sessions",
        lambda days=30, max_rows=2000: [dict(state["row"])],
    )
    return db, state


def _stored(db):
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT input_tokens, output_tokens, message_count, api_calls "
            "FROM session_log WHERE jsonl_path = 'hermes:sess-grow'"
        ).fetchall()
        daily = conn.execute(
            "SELECT COALESCE(SUM(total_input),0), COALESCE(SUM(total_output),0), "
            "COALESCE(SUM(session_count),0) FROM daily_stats"
        ).fetchone()
        return rows, daily
    finally:
        conn.close()


def test_later_collect_updates_totals_of_mid_session_row(env):
    db, state = env
    assert measure._collect_hermes_sessions(quiet=True) == 1
    rows, _ = _stored(db)
    assert rows == [(10_000, 1_000, 6, 3)]

    # The session keeps running: more messages and usage land in state.db.
    state["row"] = _row(message_count=20, api_call_count=10,
                        input_tokens=55_000, output_tokens=7_000,
                        ended_at=NOW)
    assert measure._collect_hermes_sessions(quiet=True) == 0
    rows, daily = _stored(db)
    assert rows == [(55_000, 7_000, 20, 10)], "totals froze at the mid-session collect"
    # Replaced, not added: one session, full-session totals in the aggregates.
    assert daily == (55_000, 7_000, 1)

    # A repeat collect with the same snapshot is a no-op (no double count).
    assert measure._collect_hermes_sessions(quiet=True) == 0
    rows, daily = _stored(db)
    assert rows == [(55_000, 7_000, 20, 10)]
    assert daily == (55_000, 7_000, 1)


def test_stale_collect_does_not_overwrite_final_rollup(env, monkeypatch):
    db, state = env
    # Final rollup at session end writes the full session.
    state["row"] = _row(message_count=20, api_call_count=10,
                        input_tokens=55_000, output_tokens=7_000, ended_at=NOW)
    monkeypatch.setattr(measure, "_HERMES_ROLLUP_CONTEXT", {"sess-grow": 40_000},
                        raising=False)
    assert measure._collect_hermes_sessions(quiet=True) == 1
    monkeypatch.setattr(measure, "_HERMES_ROLLUP_CONTEXT", {}, raising=False)

    # A dashboard collect that read state.db earlier (partial snapshot) lands late.
    state["row"] = _row()
    assert measure._collect_hermes_sessions(quiet=True) == 0
    rows, daily = _stored(db)
    assert rows == [(55_000, 7_000, 20, 10)], "stale partial snapshot clobbered the rollup"
    assert daily[:2] == (55_000, 7_000)

    # Mixed snapshot (more messages but fewer tokens) is also not trusted.
    state["row"] = _row(message_count=25, api_call_count=10,
                        input_tokens=50_000, output_tokens=7_000)
    measure._collect_hermes_sessions(quiet=True)
    rows, _ = _stored(db)
    assert rows == [(55_000, 7_000, 20, 10)]


def test_concurrent_newer_hermes_snapshot_is_not_overwritten(env):
    db, _state = env
    assert measure._collect_hermes_sessions(quiet=True) == 1
    conn = sqlite3.connect(str(db))
    parsed = measure.hermes_session.normalize_session(
        _row(message_count=10, api_call_count=5, input_tokens=20_000, output_tokens=2_000))
    class RacingConnection:
        def __getattr__(self, name): return getattr(conn, name)
        def execute(self, sql, *args):
            result = conn.execute(sql, *args)
            if sql.startswith("SELECT message_count, input_tokens"):
                old = result.fetchone()
                conn.execute("UPDATE session_log SET message_count=20,input_tokens=55000,output_tokens=7000,api_calls=10 WHERE jsonl_path='hermes:sess-grow'")
                return type("OldRow", (), {"fetchone": lambda self: old})()
            return result
    assert not measure._hermes_refresh_session_totals(RacingConnection(), "hermes:sess-grow", parsed)
    assert conn.execute("SELECT input_tokens,output_tokens,message_count,api_calls FROM session_log").fetchone() == (55_000, 7_000, 20, 10)
    conn.close()
