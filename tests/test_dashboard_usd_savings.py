"""USD-per-window savings model.

The runway card used to show three incoherent numbers: a headline
`extra_work_pct` (a per-token throughput multiplier, `context_mult ×
routing_mult − 1`) alongside two per-window panels showing
`headroom_with − headroom_without` (percentage-points of remaining window).
Three metrics, two unit systems, presented as if they combined -- users could
not reconcile 40.2 with 8.4 + 20.1.

This replaces that with a single coherent USD-per-window figure that REUSES the
already-metered savings ledger (`_get_merged_savings`): context `tokens_saved`
priced at the input rate (`total_cost_usd`, metered) + realized model-routing
savings (`model_routing.realized_cost_usd`, estimated counterfactual), apportioned
to each window's span. The dollars are NOT re-derived from the throughput
multipliers (that would double-count savings already in the ledger).

These tests assert the model directly against `runway_snapshot`, using the same
mock pattern as test_runway_meter_freshness.py. The no-double-count test is the
spine: changing the routing multiplier (which changes `extra_work_pct`) while
holding the metered ledger fixed must NOT change `saved_usd`.

Run: python3 -m pytest tests/test_dashboard_usd_savings.py -v
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
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-usd-runway-")
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
    """A trends DB with real consumed + saved so the context lever is non-trivial.

    The context lever (consumed/saved tokens -> context_mult) must be >1 so the
    card renders; the USD figure itself comes from the mocked _get_merged_savings,
    not this DB, so the no-double-count assertion is isolated from the token math.
    """
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


def _fresh_meters():
    return lambda **k: {
        "available": True, "stale": False, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3}


def _ledger(context_usd, routing_usd):
    """A _get_merged_savings payload with known context + routing dollars."""
    return lambda days=30: {
        "total_cost_usd": context_usd,
        "model_routing": {"realized_cost_usd": routing_usd},
    }


# ----- per-window saved_usd equals the apportioned metered sum -----

def test_saved_usd_equals_apportioned_metered_sum(m, tmp_path, monkeypatch):
    """5h and 7d saved_usd = (context_usd + routing_usd) * span_h / (days*24)."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=100.0, routing_usd=50.0))

    r = m.runway_snapshot(days=30)
    assert r is not None, "card must render with a fresh meter and non-trivial levers"

    saved_total = 150.0
    ledger_span_h = 30 * 24  # 720
    by_key = {w["key"]: w for w in r["windows"]}
    assert by_key["five_hour"]["saved_usd"] == round(saved_total * 5 / ledger_span_h, 2)
    assert by_key["seven_day"]["saved_usd"] == round(saved_total * 168 / ledger_span_h, 2)
    # Top-level spine fields trace to the ledger, not the multipliers.
    assert r["saved_usd_context"] == 100.0
    assert r["saved_usd_routing"] == 50.0


# ----- the no-double-count invariant -----

def test_saved_usd_does_not_derive_from_throughput_multipliers(m, tmp_path, monkeypatch):
    """Changing the routing multiplier (-> extra_work_pct) must NOT change saved_usd.

    This is the no-double-count assertion. The USD reuses the metered ledger; if it
    were re-derived from context_mult/routing_mult, swinging the multiplier would
    swing the dollars -- double-counting savings already in the ledger. Hold the
    ledger fixed, change the multiplier, assert saved_usd is identical.
    """
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=200.0, routing_usd=75.0))

    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    r1 = m.runway_snapshot(days=30)
    assert r1 is not None

    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 2.0)
    r2 = m.runway_snapshot(days=30)
    assert r2 is not None

    # The throughput multiplier and the headline % DID change (sanity: the lever
    # actually moved, so this is not a no-op test).
    assert r1["multiplier"] != r2["multiplier"]
    assert r1["extra_work_pct"] != r2["extra_work_pct"]

    # But the per-window USD is identical -- it traces to the ledger, not the
    # multipliers. This is exactly the double-count that must not happen.
    by_key1 = {w["key"]: w for w in r1["windows"]}
    by_key2 = {w["key"]: w for w in r2["windows"]}
    assert by_key1["five_hour"]["saved_usd"] == by_key2["five_hour"]["saved_usd"], (
        "5h saved_usd changed when only the throughput multiplier changed -- the "
        "USD is being derived from the multipliers (double-count)"
    )
    assert by_key1["seven_day"]["saved_usd"] == by_key2["seven_day"]["saved_usd"], (
        "7d saved_usd changed when only the throughput multiplier changed -- the "
        "USD is being derived from the multipliers (double-count)"
    )
    # And the spine fields are unchanged too.
    assert r1["saved_usd_context"] == r2["saved_usd_context"] == 200.0
    assert r1["saved_usd_routing"] == r2["saved_usd_routing"] == 75.0


# ----- graceful degradation -----

def test_zero_metered_savings_yields_none_saved_usd(m, tmp_path, monkeypatch):
    """No metered savings -> saved_usd is None, no NaN/$-0."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=0.0, routing_usd=0.0))

    r = m.runway_snapshot(days=30)
    assert r is not None
    for w in r["windows"]:
        assert w["saved_usd"] is None, (
            f"{w['key']} saved_usd should be None at zero savings, got {w['saved_usd']!r}"
        )
    assert r["saved_usd_tier"] is None


def test_saved_usd_tier_estimated_when_routing_contributes(m, tmp_path, monkeypatch):
    """Routing $ is an estimated counterfactual -> tier is 'estimated' when it contributes."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=100.0, routing_usd=50.0))

    r = m.runway_snapshot(days=30)
    assert r is not None
    assert r["saved_usd_tier"] == "estimated"
    for w in r["windows"]:
        assert w["saved_usd_tier"] == "estimated"


def test_saved_usd_tier_measured_when_only_context(m, tmp_path, monkeypatch):
    """Context $ is metered -> tier is 'measured' when routing does not contribute."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=100.0, routing_usd=0.0))

    r = m.runway_snapshot(days=30)
    assert r is not None
    assert r["saved_usd_tier"] == "measured"
    for w in r["windows"]:
        assert w["saved_usd_tier"] == "measured"


def test_saved_usd_survives_a_dead_ledger(m, tmp_path, monkeypatch):
    """_get_merged_savings raising must not take down the whole card."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())

    def boom(days=30):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(m, "_get_merged_savings", boom)
    r = m.runway_snapshot(days=30)
    assert r is not None, "a dead ledger must not vanish the card"
    for w in r["windows"]:
        assert w["saved_usd"] is None
    assert r["saved_usd_context"] == 0.0
    assert r["saved_usd_routing"] == 0.0


# ----- proxy disclosure carries the measured-vs-estimated boundary -----

def test_proxy_disclosure_mentions_ledger_reuse(m, tmp_path, monkeypatch):
    """The honesty boundary must say the dollars reuse the ledger, not the multipliers."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=100.0, routing_usd=50.0))

    r = m.runway_snapshot(days=30)
    assert r is not None
    assert "metered savings ledger" in r["proxy"]
    assert "not derived from the throughput multipliers" in r["proxy"]


# ----- extra_work_pct is the headline surface -----

def test_extra_work_pct_still_present(m, tmp_path, monkeypatch):
    """The throughput multiplier is present in the snapshot; it drives the headline."""
    _temp_trends(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", _ledger(context_usd=100.0, routing_usd=50.0))

    r = m.runway_snapshot(days=30)
    assert r is not None
    assert "extra_work_pct" in r
    assert r["context_multiplier"] is not None
    assert r["routing_multiplier"] is not None


# ----- surface: % throughput headline, USD in per-window cards, no combination implication -----

def test_dashboard_leads_with_pct_headline_usd_in_cards():
    """The per-token throughput % is the headline; the $ figures live in the per-window
    cards, not the lead line. (Reversed the earlier USD-lead design at product's request:
    a headline that mixed $ and % read as confusing.)"""
    html = (Path(SCRIPTS).parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("function runwayCardHtml(")
    body = html[start:start + 16000]
    # The headline metric-large leads with the throughput %, phrased "more work per token".
    assert "more work per token</div>" in body, (
        "the % throughput is not the headline metric-large"
    )
    # It is built from extra_work_pct in the PRIMARY (truthy) branch of the headline.
    assert "rw.extra_work_pct + '%</em> more work per token</div>'" in body, (
        "the headline is not driven by extra_work_pct as the lead metric-large"
    )
    # The USD-per-window string is kept only as the FALLBACK, never the lead.
    assert "usdHeadline" in body, "usdHeadline fallback construction missing"
    # The dollar figures still come from the per-window saved_usd (rendered in the cards).
    assert "w.saved_usd" in body, "surface no longer reads per-window saved_usd"
    # The units note distinguishing the multiplier from the window figures stays.
    assert "shown separately" in body, (
        "the units note distinguishing the multiplier from the window figures is missing"
    )
    # The old implication that the per-window figures combine into the % is gone.
    assert "Where the" not in body or "comes from: lighter context" not in body, (
        "the old 'Where the X% comes from' combination framing is still present"
    )


def test_dashboard_per_window_card_shows_usd():
    """Each per-window card carries its USD figure when present."""
    html = (Path(SCRIPTS).parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("var rwCards = rw.windows.map")
    end = html.index("}).join('');", start)
    body = html[start:end]
    assert "w.saved_usd" in body
    # The per-window USD is labeled as a pro-rata slice at the recent rate,
    # NOT "freed this window" (which overstated it as window-realized savings).
    assert "recent rate" in body, "per-window card does not render the honest USD rate line"
    # Graceful: when saved_usd is absent/None, no line is rendered (no $-0/NaN).
    assert "hasUsd" in body, "per-window card does not gate the USD line on presence"


# ----- both trees byte-identical -----

def test_measure_py_trees_byte_identical():
    a = (SCRIPTS / "measure.py").read_bytes()
    b = (
        ROOT / "plugins" / "token-optimizer" / "skills" / "token-optimizer"
        / "scripts" / "measure.py"
    ).read_bytes()
    assert a == b, "measure.py drifted between the two install trees"


def test_dashboard_html_trees_byte_identical():
    a = (SCRIPTS).parent / "assets" / "dashboard.html"
    b = (
        ROOT / "plugins" / "token-optimizer" / "skills" / "token-optimizer"
        / "assets" / "dashboard.html"
    )
    assert a.read_bytes() == b.read_bytes(), "dashboard.html drifted between the two install trees"
