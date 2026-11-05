"""Dashboard weekly-$ correctness: reset-window routing scope + the $10 floor.

Two fixes are locked here:

1. Routing scope (`_model_mix_shares` honours `since`). Unit F aligned the
   CONTEXT dollars to the subscription reset boundary but left the ROUTING
   dollars on a rolling `days` window, so a freshly-reset window still showed a
   full week of routing savings. The routing mix must scope to the same `since`.

2. The $10 floor. Right after a limit reset the aligned window is worth a few
   cents; "≈$0.36 more in API credits" reads as noise beside the headroom bars.
   Below `_MIN_OVERAGE_USD_SHOWN` the window carries no dollar line (saved_usd
   None) and the top-level spine reads zero.
"""
from _activity_fixtures import stamp_activity
import importlib
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-usd-threshold-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _temp_trends(m, tmp_path, monkeypatch):
    """Trends DB with non-trivial consumed/saved so the runway card renders."""
    dbp = tmp_path / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript(
        """
        CREATE TABLE session_log (id INTEGER PRIMARY KEY, date TEXT,
            input_tokens INTEGER, output_tokens INTEGER);
        CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            event_type TEXT, tokens_saved INTEGER);
        CREATE TABLE compression_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            original_tokens INTEGER, compressed_tokens INTEGER, tier TEXT);
        """
    )
    today = datetime.now().date().isoformat()
    now_iso = datetime.now().isoformat()
    conn.execute("INSERT INTO session_log(date,input_tokens,output_tokens) VALUES(?,?,?)",
                 (today, 1_000_000, 200_000))
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved) VALUES(?,?,?)",
                 (now_iso, "archive", 50_000))
    stamp_activity(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))


def _fresh_meters():
    return lambda **k: {
        "available": True, "stale": False, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3}


def _ledger(context_usd, routing_usd):
    def _merged(days=30, since=None):
        return {
            "total_cost_usd": context_usd,
            "model_routing": {"realized_cost_usd": routing_usd},
        }
    return _merged


# ---------- the $10 floor ----------

def test_immaterial_weekly_dollar_is_suppressed(m, tmp_path, monkeypatch):
    """A sub-$10 weekly overage carries no dollar line and a zeroed spine."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=0.30, routing_usd=0.0))

    r = m.runway_snapshot(days=30)
    assert r is not None
    by_key = {w["key"]: w for w in r["windows"]}
    assert by_key["seven_day"]["saved_usd"] is None, "sub-$10 weekly $ must be hidden"
    assert r["saved_usd_context"] == 0.0
    assert r["saved_usd_routing"] == 0.0
    assert r["saved_usd_tier"] is None


def test_material_weekly_dollar_is_shown(m, tmp_path, monkeypatch):
    """At/above the $10 floor the weekly dollar renders unchanged."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=12.0, routing_usd=0.0))

    r = m.runway_snapshot(days=30)
    by_key = {w["key"]: w for w in r["windows"]}
    assert by_key["seven_day"]["saved_usd"] == 12.0
    assert r["saved_usd_context"] == 12.0


def test_threshold_boundary_is_ten_dollars(m):
    """The floor constant is $10 (the value the operator asked for)."""
    assert m._MIN_OVERAGE_USD_SHOWN == 10.0


# ---------- routing scope honours `since` ----------

def test_model_mix_shares_scopes_to_since(m, tmp_path, monkeypatch):
    """`since` cuts the model_daily lookback at its date, not a rolling window.

    A heavy pre-reset day must fall OUTSIDE a `since`-anchored window while the
    rolling `days` window still includes it -- the exact gap that left routing
    dollars un-aligned after a reset.
    """
    dbp = tmp_path / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.execute("CREATE TABLE model_daily (date TEXT, model TEXT, total_tokens INTEGER)")
    old_day = (datetime.now() - timedelta(days=4)).date().isoformat()   # pre-reset
    today = datetime.now().date().isoformat()                            # in-window
    conn.execute("INSERT INTO model_daily VALUES(?,?,?)", (old_day, "claude-opus", 900_000))
    conn.execute("INSERT INTO model_daily VALUES(?,?,?)", (today, "claude-haiku", 100_000))
    stamp_activity(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))

    rolling = m._model_mix_shares(days=7)                # includes the old heavy day
    aligned = m._model_mix_shares(days=7, since=datetime.now().date().isoformat())

    assert rolling["total_tokens"] == 1_000_000, "rolling window sees both days"
    assert aligned["total_tokens"] == 100_000, "since-anchored window drops the pre-reset day"
    # And the mix differs: rolling is Opus-heavy, aligned is Haiku-only.
    assert aligned["shares"].get("claude-opus", 0.0) == 0.0


def test_routing_savings_scope_to_since(m, tmp_path, monkeypatch):
    """`_compute_model_routing_savings` forwards `since` to the mix, so a
    since-anchored call cannot return the full rolling-window routing figure."""
    calls = {}

    def _spy_mix(days=14, since=None):
        calls["since"] = since
        return {"shares": {}, "total_tokens": 0, "days": days}

    monkeypatch.setattr(m, "_model_mix_shares", _spy_mix)
    m._compute_model_routing_savings(days=7, since="2026-08-27T20:00:00")
    assert calls["since"] == "2026-08-27T20:00:00", "since must reach the model mix"
