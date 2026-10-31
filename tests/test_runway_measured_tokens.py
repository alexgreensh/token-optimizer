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
