"""The window card's "measured token floor" counts measured events only."""
import importlib
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snapshot"))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    yield mod
    sys.modules.pop("measure", None)


def _db(measure, tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(measure._SCHEMA)
    return conn


def _event(conn, event_type, tokens, session="sess-1"):
    conn.execute(
        "INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,"
        "session_id,session_uuid) VALUES(?,?,?,?,?,?)",
        (datetime.now().isoformat(), event_type, tokens, 0.0, session, session))


def test_runway_tokens_leave_out_estimated_tiers(measure, tmp_path):
    conn = _db(measure, tmp_path)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    _event(conn, "tool_archive", 1_000)
    _event(conn, "tool_archive_reexpand", 200)
    _event(conn, "contextignore_block", 50)
    _event(conn, "mcp_cap", 7_000)
    _event(conn, "hint_followed", 3_000)
    _event(conn, "resume_lean", 5_000)
    _event(conn, "setup_optimization", 9_000)
    _event(conn, "verbosity_steer", 4_000)
    conn.commit()
    assert measure._runway_saved_tokens(conn, cutoff) == 850


def test_runway_tokens_match_the_savings_report_measured_total(measure, tmp_path, monkeypatch):
    conn = _db(measure, tmp_path)
    for kind, tokens in (("tool_archive", 1_000), ("mcp_cap", 7_000),
                         ("hint_followed", 3_000), ("resume_lean", 5_000),
                         ("setup_optimization", 9_000)):
        _event(conn, kind, tokens)
    conn.commit()
    conn.close()
    monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "t.db")
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(tmp_path / "t.db"))
    measured = measure._get_savings_summary(days=30)["total_tokens"]
    probe = sqlite3.connect(tmp_path / "t.db")
    runway = measure._runway_saved_tokens(
        probe, (datetime.now() - timedelta(days=30)).isoformat())
    probe.close()
    assert runway == measured == 1_000


def test_estimated_multiplier_keeps_estimated_tiers_while_floor_stays_measured(measure, tmp_path, monkeypatch):
    conn = _db(measure, tmp_path)
    _event(conn, "tool_archive", 10_000)
    _event(conn, "resume_lean", 30_000)
    conn.commit()
    conn.close()
    monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "t.db")
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(tmp_path / "t.db"))
    monkeypatch.setattr(measure, "_dashboard_spent_token_basis",
                        lambda conn, days=30: {"tokens": 1_000_000, "basis": "test", "complete": True})
    monkeypatch.setattr(measure, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.0})
    monkeypatch.setattr(measure, "_keepwarm_read_meters", lambda now=None: {
        "available": False, "stale": True, "age_s": None, "ts": None,
        "five_hour_pct": None, "seven_day_pct": None, "seven_day_resets_at": None})
    result = measure.runway_snapshot()
    # 4% more work per token from 40k of 1M avoided: the card still shows.
    assert result is not None
    assert result["context_multiplier"] == pytest.approx(1.04)
    assert result["tokens_saved"] == 10_000
    assert result["context_tokens_saved"] == 40_000
