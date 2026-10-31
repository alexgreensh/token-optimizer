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


def test_unknown_generation_is_priced_at_zero_not_at_the_stored_guess(m):
    conn = _conn({"claude-opus-99-9": 1_000_000}, [("tool_archive", 1_000_000, 4.0)])
    out = {}
    deltas, repriced, unmeasured = m._session_model_reprice(conn, CUTOFF, unpriced_out=out)
    assert deltas == {"tool_archive": pytest.approx(-4.0)}
    assert out["tokens"] == 1_000_000
    assert unmeasured == 0


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


def test_keepwarm_event_keeps_its_one_hour_cache_write_price(m):
    # Keep-warm rows are stored at the avoided 1h cache write (input x2).
    conn = _conn({"claude-opus-5-5": 1_000_000}, [("keepwarm", 1_000_000, 8.0)])
    deltas, repriced, _ = m._session_model_reprice(conn, CUTOFF)
    assert deltas.get("keepwarm", 0.0) == pytest.approx(0.0)
    assert repriced == 1_000_000


def test_keepwarm_event_follows_the_session_generation_write_rate(m):
    conn = _conn({"claude-opus-4-1": 1_000_000}, [("keepwarm", 1_000_000, 8.0)])
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF)
    old = m._routing_class_rates("claude-opus-4-1")["cache_create_1h"]
    assert deltas["keepwarm"] == pytest.approx(old - 8.0)


def test_session_on_unpriced_model_reprices_its_events_to_zero_and_discloses(m):
    conn = _conn({"glm-4.6": 1_000_000}, [("tool_archive", 1_000_000, 3.0)])
    out = {}
    deltas, repriced, unmeasured = m._session_model_reprice(conn, CUTOFF, unpriced_out=out)
    assert deltas["tool_archive"] == pytest.approx(-3.0)
    assert out["tokens"] == 1_000_000
    assert unmeasured == 0
    # Stored rows are never rewritten.
    assert conn.execute("SELECT cost_saved_usd FROM savings_events").fetchone()[0] == 3.0


def test_unpriced_share_of_a_mixed_session_is_priced_at_zero(m):
    conn = _conn({"claude-opus-5-5": 500_000, "kimi-k3": 500_000},
                 [("tool_archive", 1_000_000, 3.0)])
    out = {}
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF, unpriced_out=out)
    # Half the tokens at Opus 5.5 ($4/MTok), half unpriced: $2.00 total.
    assert deltas["tool_archive"] == pytest.approx(2.0 - 3.0)
    assert out["tokens"] == pytest.approx(500_000)


def test_codex_model_with_its_own_card_is_priced_not_dropped(m):
    rate = m._routing_class_rates("gpt-5.4")["fresh_input"]
    conn = _conn({"gpt-5.4": 1_000_000}, [("tool_archive", 1_000_000, rate)])
    out = {}
    deltas, _, _ = m._session_model_reprice(conn, CUTOFF, unpriced_out=out)
    assert deltas.get("tool_archive", 0.0) == pytest.approx(0.0)
    assert out.get("tokens", 0) == 0


def test_summary_discloses_unpriced_session_tokens(m, monkeypatch, tmp_path):
    db = tmp_path / "trends.db"
    monkeypatch.setattr(m, "TRENDS_DB", db)
    conn = m._init_trends_db()
    conn.execute("INSERT INTO session_log (jsonl_path, date, session_uuid, model_usage_json) VALUES (?,?,?,?)",
                 ("/x/S9.jsonl", datetime.now().strftime("%Y-%m-%d"), "S9", json.dumps({"glm-4.6": 10})))
    conn.execute("INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, session_uuid, model) VALUES (?,?,?,?,?,?,?)",
                 (datetime.now().isoformat(), "tool_archive", 1_000_000, 3.0, "S9", "S9", "sonnet"))
    conn.commit()
    conn.close()
    s = m._get_savings_summary(days=30)
    assert s["total_cost_usd"] == pytest.approx(0.0)
    assert s["reprice_detail"]["unpriced_tokens"] == 1_000_000
    assert s["total_tokens"] == 1_000_000
    # Row on disk is untouched.
    row = sqlite3.connect(str(db)).execute("SELECT cost_saved_usd FROM savings_events").fetchone()
    assert row[0] == 3.0


def _unjoined(rows):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,"
        " tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, detail TEXT, model TEXT,"
        " session_uuid TEXT);"
        "CREATE TABLE session_log(session_uuid TEXT, model_usage_json TEXT, all_model_usage_json TEXT);")
    ts = (datetime.now() - timedelta(hours=1)).isoformat()
    for model, tokens, cost in rows:
        conn.execute(
            "INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_uuid,model)"
            " VALUES(?,?,?,?,'ghost',?)", (ts, "tool_archive", tokens, cost, model))
    return conn


def test_unjoined_row_naming_an_unpriced_model_is_priced_at_zero_and_disclosed(m):
    conn = _unjoined([("gpt-9-unknown", 1_000_000, 3.0)])
    out = {}
    deltas, repriced, unmeasured = m._session_model_reprice(conn, CUTOFF, unpriced_out=out)
    assert deltas["tool_archive"] == pytest.approx(-3.0)
    assert out["tokens"] == 1_000_000
    assert (repriced, unmeasured) == (0, 0)
    assert conn.execute("SELECT cost_saved_usd FROM savings_events").fetchone()[0] == 3.0


@pytest.mark.parametrize("model", ["opus", "opus_5_5", "sonnet", "claude-opus-5-5", "gpt-5.4", None, ""])
def test_unjoined_row_with_a_known_or_missing_model_keeps_its_stored_price(m, model):
    conn = _unjoined([(model, 1_000_000, 3.0)])
    out = {}
    deltas, repriced, unmeasured = m._session_model_reprice(conn, CUTOFF, unpriced_out=out)
    assert deltas == {}
    assert out == {}
    assert unmeasured == 1_000_000
