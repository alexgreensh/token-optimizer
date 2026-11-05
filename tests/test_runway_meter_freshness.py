"""Runway card meter-freshness (v5.11.62).

The dashboard "Your Plan Goes Further" card is fed by runway_snapshot(), which
reads the 5h/7d rate-limit meter. The meter only refreshes while Claude Code is
actively running, so a dashboard opened later reads a meter that is routinely
>30 min old. The old code hard-returned None on any stale meter, so the whole
card vanished -- the "this section doesn't update" report. The fix:

  * a stale-but-present meter is shown (last real reading), carrying meter_stale
    + meter_ts so the card can date it honestly and warn when old;
  * a window whose reading is older than the window's own span is dropped (its
    limit has certainly reset, so "you'd already be cut off" would be a lie);
  * None only when there is no reading at all, or every window has reset.

Run: python3 -m pytest tests/test_runway_meter_freshness.py -v
"""
import importlib
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-runway-fresh-")
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
    """A trends DB with real consumed + saved so the context lever is non-trivial."""
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
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))
    monkeypatch.setattr(m, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})


def test_fresh_meter_renders_and_is_not_flagged_stale(m, tmp_path, monkeypatch):
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": False, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3})
    r = m.runway_snapshot(days=30)
    assert r is not None
    assert r["meter_stale"] is False and r["meter_ts"] is not None
    assert {w["key"] for w in r["windows"]} == {"five_hour", "seven_day"}


def test_stale_meter_shows_last_reading_instead_of_vanishing(m, tmp_path, monkeypatch):
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": True, "five_hour_pct": 40.0,
        "seven_day_pct": 25.0, "age_s": 2 * 3600, "ts": time.time() - 2 * 3600})
    r = m.runway_snapshot(days=30)
    assert r is not None, "a stale-but-present meter must NOT make the card vanish"
    assert r["meter_stale"] is True
    # 2h old: within both window spans, so both windows still render.
    assert {w["key"] for w in r["windows"]} == {"five_hour", "seven_day"}


def test_card_survives_when_meter_unavailable(m, tmp_path, monkeypatch):
    """Bug A: an UNAVAILABLE meter (missing/contentless rate-limits file) used to
    vanish the whole card via `if not available: return None`. But the headline
    throughput multiplier and the weekly savings come from the ledger, not the
    meter, so the card must survive with empty windows (renderer shows a "meter
    refreshing" note). Only the per-window bars need the live meter."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": False, "stale": True, "five_hour_pct": None,
        "seven_day_pct": None, "age_s": None, "ts": None})
    r = m.runway_snapshot(days=30)
    assert r is not None, "an unavailable meter must NOT vanish the card; the multiplier + savings survive"
    assert r.get("windows") == [], "no meter reading -> no per-window bars"
    assert r.get("meter_available") is False, "meter_available reflects the real (missing) meter state"
    assert isinstance(r.get("extra_work_pct"), (int, float)), "headline multiplier must survive"


def test_none_when_no_meter_and_no_ledger_story(m, tmp_path, monkeypatch):
    """The card is silent only when there is genuinely nothing to say. With the
    meter unavailable AND no measurable throughput shift (no trends/savings), the
    multiplier cannot be computed, so runway_snapshot returns None -- not because
    of the meter, but because the ledger has no story."""
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": False, "stale": True, "five_hour_pct": None,
        "seven_day_pct": None, "age_s": None, "ts": None})
    # no _temp_trends -> consumed <= 0 -> honest silence
    assert m.runway_snapshot(days=30) is None


def test_window_older_than_its_span_is_dropped(m, tmp_path, monkeypatch):
    """A 26h-old reading: the 5h limit has reset (drop it), the 7d limit has not."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": True, "five_hour_pct": 95.0,
        "seven_day_pct": 25.0, "age_s": 26 * 3600, "ts": time.time() - 26 * 3600})
    r = m.runway_snapshot(days=30)
    assert r is not None
    assert {w["key"] for w in r["windows"]} == {"seven_day"}


def test_card_survives_when_reading_older_than_all_windows(m, tmp_path, monkeypatch):
    """Older than a week: both per-window limits have reset, so the live 5h/7d bars
    drop (their reset-era numbers would be a lie). But the throughput multiplier and
    weekly savings do NOT depend on the live meter, so the card must NOT vanish
    (regression: it used to return None and the whole "plan goes further" surface
    disappeared after a quiet week). It returns with empty windows; the renderer
    shows the headline + a "meter refreshing" note instead of the bars."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": True, "five_hour_pct": 60.0,
        "seven_day_pct": 80.0, "age_s": 8 * 24 * 3600, "ts": time.time() - 8 * 24 * 3600})
    r = m.runway_snapshot(days=30)
    assert r is not None, "stale-meter card must not vanish; the multiplier + savings survive"
    assert r.get("windows") == [], "both windows drop once the reading is older than the 7d span"
    assert isinstance(r.get("extra_work_pct"), (int, float)), "headline multiplier must survive"


def test_period_days_exposed_for_the_multiplier_scope(m, tmp_path, monkeypatch):
    """period_days scopes the throughput multipliers (context/routing), not the
    per-window dollars (which now price each window over its OWN span). It must
    still be exposed so the surface can label the multiplier's window."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": False, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3})
    r = m.runway_snapshot(days=30)
    assert r is not None
    assert r.get("period_days") == 30, "period_days must be exposed for the multiplier scope"


def test_weekly_dollar_is_span_based_not_outer_days(m, tmp_path, monkeypatch):
    """The weekly window prices overage over its OWN 7d span, so the outer `days`
    argument must NOT distort it, and the weekly dollar must equal the top-level
    7d spine (both read the same 7d ledger). No proration, no over-report."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": False, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3})
    # Even with a tiny outer days=1, the weekly window still reads its 7d span.
    r = m.runway_snapshot(days=1)
    assert r is not None
    spine = round(r.get("saved_usd_context", 0) + r.get("saved_usd_routing", 0), 2)
    by_key = {w["key"]: w for w in r["windows"]}
    wk = by_key.get("seven_day", {}).get("saved_usd")
    if wk is not None:
        assert wk == spine, (
            f"weekly window dollar ({wk}) must equal the 7d spine ({spine}); a "
            "mismatch means the window is prorated or scoped by the outer days"
        )
