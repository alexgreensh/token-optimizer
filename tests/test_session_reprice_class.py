"""Read-time session repricing keeps each event's token class and exact model generation."""
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    yield mod
    sys.modules.pop("measure", None)


def _conn(usage, events):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,"
        " tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, detail TEXT, model TEXT,"
        " session_uuid TEXT);"
        "CREATE TABLE session_log(session_uuid TEXT, model_usage_json TEXT, all_model_usage_json TEXT);")
    conn.execute("INSERT INTO session_log VALUES('s', ?, NULL)", (json.dumps(usage),))
    ts = (datetime.now() - timedelta(hours=1)).isoformat()
    for event_type, tokens, cost in events:
        conn.execute(
            "INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_uuid)"
            " VALUES(?,?,?,?,'s')", (ts, event_type, tokens, cost))
    return conn


CUTOFF = "2000-01-01"


def test_output_class_event_keeps_output_rate_on_exact_generation(m):
    conn = _conn({"claude-opus-5-5": 1_000_000},
                 [("verbosity_steer_measured", 1_000_000, 20.0)])
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF)
    assert deltas.get("verbosity_steer_measured", 0.0) == pytest.approx(0.0)


def test_input_class_event_uses_exact_generation_not_family(m):
    conn = _conn({"claude-opus-5-5": 1_000_000}, [("checkpoint_restore", 1_000_000, 4.0)])
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF)
    assert deltas.get("checkpoint_restore", 0.0) == pytest.approx(0.0)


def test_output_class_event_stored_at_input_rate_is_lifted_to_output(m):
    conn = _conn({"claude-opus-5-5": 1_000_000},
                 [("verbosity_steer_measured", 1_000_000, 4.0)])
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF)
    assert deltas["verbosity_steer_measured"] == pytest.approx(16.0)


def test_older_generation_session_prices_at_its_own_card(m):
    conn = _conn({"claude-opus-4-1": 1_000_000}, [("tool_archive", 1_000_000, 4.0)])
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF)
    old = m._routing_class_rates("claude-opus-4-1")["fresh_input"]
    assert deltas["tool_archive"] == pytest.approx(old - 4.0)


def test_unknown_generation_is_left_at_the_stored_price(m):
    conn = _conn({"claude-opus-99-9": 1_000_000}, [("tool_archive", 1_000_000, 4.0)])
    deltas, repriced, unmeasured = m._session_model_reprice(conn, CUTOFF)
    assert deltas == {}
    assert unmeasured == 1_000_000


def test_new_output_class_events_are_stored_at_the_output_rate(m, monkeypatch, tmp_path):
    db = tmp_path / "trends.db"
    monkeypatch.setattr(m, "TRENDS_DB", db)
    monkeypatch.setattr(m, "detect_runtime", lambda: "claude")
    m._log_savings_event("verbosity_steer_measured", 1_000_000, model="claude-opus-5-5")
    m._log_savings_event("checkpoint_restore", 1_000_000, model="claude-opus-5-5")
    conn = sqlite3.connect(str(db))
    costs = dict(conn.execute("SELECT event_type, cost_saved_usd FROM savings_events"))
    assert costs["verbosity_steer_measured"] == pytest.approx(20.0)
    assert costs["checkpoint_restore"] == pytest.approx(4.0)
