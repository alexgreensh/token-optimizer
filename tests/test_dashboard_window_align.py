"""Unit F: Dashboard weekly-dollar window alignment to subscription limit reset.

The runway card's weekly "$X more in API credits" figure used a rolling
7-day lookback (``now - 7 days``) so the dollar drifted down as heavy savings
days aged out. The fix aligns it to the ACTUAL subscription limit period
start: the meter's ``seven_day.resets_at`` minus 7 days. When the meter is
unavailable, the behaviour falls back to the rolling 7-day lookback.

Run: python3 -m pytest tests/test_dashboard_window_align.py -v
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
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-win-align-")
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
    """Minimal trends DB so the context lever is non-trivial."""
    dbp = tmp_path / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE session_log (id INTEGER PRIMARY KEY, date TEXT,
            input_tokens INTEGER, output_tokens INTEGER);
        CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            event_type TEXT, tokens_saved INTEGER);
        CREATE TABLE compression_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            original_tokens INTEGER, compressed_tokens INTEGER, tier TEXT);
    """)
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


def _meters(seven_day_pct=10.0, seven_day_resets_at=None):
    """Return a _keepwarm_read_meters substitute with a controllable resets_at."""
    def _read(**k):
        return {
            "available": True, "stale": False, "five_hour_pct": 12.0,
            "seven_day_pct": seven_day_pct,
            "seven_day_resets_at": seven_day_resets_at,
            "age_s": 3.0, "ts": time.time() - 3,
        }
    return _read


# ---------------------------------------------------------------------------
# Window-aligned weekly dollar
# ---------------------------------------------------------------------------

def test_weekly_dollar_uses_resets_at_window_start(m, tmp_path, monkeypatch):
    """When the meter provides resets_at, the weekly dollar sums the ledger from
    (resets_at - 7 days) to now, NOT from (now - 7 days).

    We capture the *since* parameter passed to _get_merged_savings and assert:
    1. A *since* string IS passed (not days-only rolling behaviour).
    2. The *since* timestamp matches (resets_at - 7*24*3600).
    """
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})

    # A resets_at 3 days from now (oldest event is 4 days old).
    now = time.time()
    resets_at = now + 3 * 24 * 3600  # resets in 3 days
    monkeypatch.setattr(m, "_keepwarm_read_meters",
                        _meters(seven_day_pct=10.0, seven_day_resets_at=resets_at))

    # Capture the call to _get_merged_savings so we can inspect the *since* param.
    calls = []
    def _capture_merged(days=30, since=None):
        calls.append({"days": days, "since": since})
        return {
            "total_cost_usd": 100.0,
            "model_routing": {"realized_cost_usd": 50.0},
        }
    monkeypatch.setattr(m, "_get_merged_savings", _capture_merged)

    r = m.runway_snapshot(days=30)
    assert r is not None, "card must render"
    assert len(calls) > 0, "_get_merged_savings was never called"

    # Find the call for the 7d window (wdays=7 with a since param)
    weekly_calls = [c for c in calls if c["days"] == 7 or c["since"] is not None]
    assert len(weekly_calls) >= 1, (
        f"No weekly-aligned call found among {len(calls)} calls; all: {calls}"
    )

    wc = weekly_calls[0]
    assert wc["since"] is not None, (
        "weekly _get_merged_savings was NOT called with a since param; "
        "still using rolling days-only lookup"
    )

    # Verify the since timestamp = resets_at - 7*24*3600
    expected_window_start = resets_at - (7 * 24 * 3600)
    actual_ts = datetime.fromisoformat(wc["since"]).timestamp()
    assert abs(actual_ts - expected_window_start) < 5.0, (
        f"window_start mismatch: expected ~{expected_window_start} "
        f"({datetime.fromtimestamp(expected_window_start).isoformat()}), "
        f"got {actual_ts} ({wc['since']})"
    )

    # The dollar figures should still be correct (150.0)
    for w in r["windows"]:
        if w["key"] == "seven_day":
            assert w["saved_usd"] == 150.0, f"weekly saved_usd={w['saved_usd']}, expected 150.0"


def test_weekly_dollar_window_start_is_correctly_computed(m, tmp_path, monkeypatch):
    """Given a known resets_at, the *since* param passed to _get_merged_savings
    is exactly (resets_at - 7*24*3600) converted to ISO-8601. This verifies
    the window-start math regardless of real DB state."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})

    now = time.time()
    # resets_at in 3 days => window_start should be 4 days ago
    resets_at = now + 3 * 24 * 3600
    expected_window_start = resets_at - (7 * 24 * 3600)

    monkeypatch.setattr(m, "_keepwarm_read_meters",
                        _meters(seven_day_pct=10.0, seven_day_resets_at=resets_at))

    calls = []
    def _capture_merged(days=30, since=None):
        calls.append({"days": days, "since": since})
        return {
            "total_cost_usd": 100.0,
            "model_routing": {"realized_cost_usd": 50.0},
        }
    monkeypatch.setattr(m, "_get_merged_savings", _capture_merged)

    r = m.runway_snapshot(days=30)
    assert r is not None, "card must render"

    # Find the weekly-aligned call
    weekly_calls = [c for c in calls if c["since"] is not None]
    assert len(weekly_calls) >= 1, (
        f"No weekly-aligned _get_merged_savings call with since= found among {calls}"
    )

    wc = weekly_calls[0]
    actual_ts = datetime.fromisoformat(wc["since"]).timestamp()
    assert abs(actual_ts - expected_window_start) < 5.0, (
        f"window_start mismatch: expected ~{expected_window_start} "
        f"({datetime.fromtimestamp(expected_window_start).isoformat()}), "
        f"got {actual_ts} ({wc['since']})"
    )

    # resets_at = now + 3d, window_start = resets_at - 7d = now - 4d.
    # That is 4 days ago from now, not 7.
    rolling_7d_start = now - 7 * 24 * 3600
    assert abs(actual_ts - rolling_7d_start) > 1 * 24 * 3600, (
        f"window_start ({actual_ts}) is too close to the rolling 7d start "
        f"({rolling_7d_start}); should be different (resets_at - 7d != now - 7d)"
    )

    # The weekly dollar should render (150.0 from mock).
    by_key = {w["key"]: w for w in r["windows"]}
    assert by_key["seven_day"]["saved_usd"] == 150.0


# ---------------------------------------------------------------------------
# Fallback: no meter / no resets_at -> rolling 7-day
# ---------------------------------------------------------------------------

def test_weekly_dollar_falls_back_to_rolling_when_resets_at_is_none(m, tmp_path, monkeypatch):
    """When resets_at is None (unavailable meter), the weekly dollar falls back
    to the rolling 7-day lookback — _get_merged_savings is called without a
    *since* parameter (days=7 only)."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})

    # Meter with no resets_at.
    monkeypatch.setattr(m, "_keepwarm_read_meters",
                        _meters(seven_day_pct=10.0, seven_day_resets_at=None))

    calls = []
    def _capture_merged(days=30, since=None):
        calls.append({"days": days, "since": since})
        return {
            "total_cost_usd": 100.0,
            "model_routing": {"realized_cost_usd": 50.0},
        }
    monkeypatch.setattr(m, "_get_merged_savings", _capture_merged)

    r = m.runway_snapshot(days=30)
    assert r is not None

    # Every call to _get_merged_savings should have since=None (rolling fallback).
    for c in calls:
        assert c["since"] is None, (
            f"_get_merged_savings called with since={c['since']!r} "
            f"when resets_at is None; should fall back to rolling days"
        )

    # The weekly dollar should still render (150.0 from our mock).
    by_key = {w["key"]: w for w in r["windows"]}
    assert by_key["seven_day"]["saved_usd"] == 150.0


def test_weekly_dollar_falls_back_to_rolling_when_meter_has_no_resets_at_field(m, tmp_path, monkeypatch):
    """When the meter dict omits seven_day_resets_at entirely (legacy meter
    format), the weekly dollar falls back to rolling 7-day."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})

    # Meter without the seven_day_resets_at key at all (legacy format).
    def _legacy_meters(**k):
        return {
            "available": True, "stale": False, "five_hour_pct": 12.0,
            "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3,
        }
    monkeypatch.setattr(m, "_keepwarm_read_meters", _legacy_meters)

    calls = []
    def _capture_merged(days=30, since=None):
        calls.append({"days": days, "since": since})
        return {
            "total_cost_usd": 100.0,
            "model_routing": {"realized_cost_usd": 50.0},
        }
    monkeypatch.setattr(m, "_get_merged_savings", _capture_merged)

    r = m.runway_snapshot(days=30)
    assert r is not None

    for c in calls:
        assert c["since"] is None, (
            f"_get_merged_savings called with since={c['since']!r} "
            f"when meter has no seven_day_resets_at; should fall back to rolling"
        )


# ---------------------------------------------------------------------------
# 5h window: unchanged (no dollar line, no alignment attempt)
# ---------------------------------------------------------------------------

def test_5h_window_never_receives_since_param(m, tmp_path, monkeypatch):
    """The 5h window is sub-day and has no dollar line. Even when resets_at
    is available, the 5h window must NOT pass a since parameter (its behaviour
    is unchanged)."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})

    resets_at = time.time() + 3 * 24 * 3600
    monkeypatch.setattr(m, "_keepwarm_read_meters",
                        _meters(seven_day_pct=10.0, seven_day_resets_at=resets_at))

    calls = []
    def _capture_merged(days=30, since=None):
        calls.append({"days": days, "since": since})
        return {
            "total_cost_usd": 100.0,
            "model_routing": {"realized_cost_usd": 50.0},
        }
    monkeypatch.setattr(m, "_get_merged_savings", _capture_merged)

    r = m.runway_snapshot(days=30)
    assert r is not None

    # 5h has no dollar line, so _window_overage_usd returns (None, None) early.
    # It should never call _get_merged_savings with wdays=0 (sub-day).
    for c in calls:
        assert c["days"] != 0, "_get_merged_savings called for sub-day (5h) window"


# ---------------------------------------------------------------------------
# Human-readable span label in dashboard
# ---------------------------------------------------------------------------

def test_dashboard_label_says_since_your_limit_reset():
    """The dashboard HTML labels the weekly dollar as 'since your limit reset'
    instead of the old rolling 'this week'."""
    html = (
        SCRIPTS.parent / "assets" / "dashboard.html"
    ).read_text(encoding="utf-8")
    start = html.index("function runwayCardHtml(")
    body = html[start:start + 16000]

    assert "since your limit reset" in body, (
        "weekly dollar label must say 'since your limit reset', not the old "
        "rolling 'this week'"
    )
    # The old label must not appear (it would mean the change wasn't applied).
    assert "'this week'" not in body, (
        "old rolling label 'this week' is still present in the dashboard"
    )


# ---------------------------------------------------------------------------
# _keepwarm_read_meters exposes resets_at
# ---------------------------------------------------------------------------

def test_read_meters_extracts_numeric_resets_at(m, tmp_path):
    """_keepwarm_read_meters extracts seven_day_resets_at from a numeric epoch field."""
    import json
    rl_path = tmp_path / "rate-limits.json"
    payload = {
        "seven_day": {
            "used_percentage": 42.0,
            "resets_at": 1718400000.0,
        },
        "timestamp": int(time.time() * 1000),
    }
    rl_path.write_text(json.dumps(payload), encoding="utf-8")

    result = m._keepwarm_read_meters(rate_limits_path=str(rl_path))
    assert result["available"] is True
    assert result["seven_day_pct"] == 42.0
    assert result["seven_day_resets_at"] == 1718400000.0, (
        "numeric resets_at must be extracted as-is"
    )


def test_read_meters_extracts_iso_resets_at(m, tmp_path):
    """_keepwarm_read_meters parses an ISO-8601 string resets_at."""
    import json
    rl_path = tmp_path / "rate-limits.json"
    payload = {
        "seven_day": {
            "used_percentage": 55.0,
            "resets_at": "2026-08-30T12:00:00Z",
        },
        "timestamp": int(time.time() * 1000),
    }
    rl_path.write_text(json.dumps(payload), encoding="utf-8")

    result = m._keepwarm_read_meters(rate_limits_path=str(rl_path))
    assert result["available"] is True
    assert result["seven_day_resets_at"] is not None, (
        "ISO-8601 resets_at must be parsed"
    )
    from datetime import timezone as _tz
    expected = datetime(2026, 8, 30, 12, 0, 0, tzinfo=_tz.utc).timestamp()
    assert abs(result["seven_day_resets_at"] - expected) < 60.0


def test_read_meters_returns_none_when_resets_at_missing(m, tmp_path):
    """When resets_at is absent from the meter data, seven_day_resets_at is None."""
    import json
    rl_path = tmp_path / "rate-limits.json"
    payload = {
        "seven_day": {
            "used_percentage": 33.0,
        },
        "timestamp": int(time.time() * 1000),
    }
    rl_path.write_text(json.dumps(payload), encoding="utf-8")

    result = m._keepwarm_read_meters(rate_limits_path=str(rl_path))
    assert result["seven_day_resets_at"] is None


def test_read_meters_returns_none_when_resets_at_is_invalid(m, tmp_path):
    """NaN or zero resets_at returns None."""
    import json
    rl_path = tmp_path / "rate-limits.json"
    payload = {
        "seven_day": {
            "used_percentage": 10.0,
            "resets_at": 0,
        },
        "timestamp": int(time.time() * 1000),
    }
    rl_path.write_text(json.dumps(payload), encoding="utf-8")

    result = m._keepwarm_read_meters(rate_limits_path=str(rl_path))
    assert result["seven_day_resets_at"] is None


def test_read_meters_corrupt_file_returns_none_resets_at(m, tmp_path):
    """A corrupt/missing rate-limits file returns available=False and None resets_at."""
    result = m._keepwarm_read_meters(rate_limits_path="/nonexistent/path.json")
    assert result["available"] is False
    assert result["seven_day_resets_at"] is None
