"""Model-mix reprice: repair the event-time model stamp from each session's own mix.

THE UNDER-COUNT
---------------
``_log_savings_event`` stamps every savings row with ``_resolve_session_model()``.
That resolver falls back to ``"sonnet"`` whenever the session JSONL cannot be read
(sub-agent / sidechain sessions, sessions still open at write time), and older rows
carry ``model IS NULL`` and are priced at a flat fallback rate. Measured on the real
ledger (30d, 2026-08-29): 18.9M saved tokens carry ``model IS NULL`` and 13.9M carry
``model='sonnet'``, yet ``session_log.model_usage_json`` records 17.8M and 4.6M of
those tokens inside sessions whose OWN token-weighted model usage is Opus. The rate
card is right; the stamp is wrong.

WHAT IS FIXED, AND WHAT IS DELIBERATELY NOT
-------------------------------------------
Each event is repriced against the model mix of ITS OWN session, never against the
window's blended mix. Repricing to the window mix would lift a saving that happened
inside a session Token Optimizer had routed to a cheaper model up to an Opus-heavy
average -- claiming the routing win a second time inside the context pool that
``_compute_model_routing_savings`` already owns. ``test_no_double_count_with_routing``
is the guard for that.

Structural savings are added by ``_get_merged_savings`` AFTER this summary and are
priced by ``_compound_structural`` at CACHE rates. They must never be dragged to a
fresh-input rate: ``test_structural_stays_at_cache_rates``.

The result is labelled ESTIMATED, not measured. The session mix is a measured
column, but attributing a specific never-sent saving to a session's blended rate is
an allocation, not a measurement.

Run: python3 -m pytest tests/test_savings_model_mix_reprice.py -v
"""
import importlib
import json
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
    tmp = tempfile.mkdtemp(prefix="to-reprice-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    # Pin the pricing regime so the Sonnet-5 introductory flip on 2026-09-01
    # cannot silently change these arithmetic assertions.
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._apply_sonnet_intro_pricing()
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


# Rates asserted against, read from the module's own card rather than hard-coded
# from memory (PRICING_TIERS is the source of truth and must not be "fixed").
def _rate(m, name):
    return float(PRICING(m)["claude_models"][name]["input"])


def PRICING(m):
    return m.PRICING_TIERS[m._load_pricing_tier()]


def _ledger_db(m, tmp_path, monkeypatch, events, sessions):
    """Build a trends DB with the real column shape and point measure.py at it.

    events   : list of (event_type, tokens, stamped_model, cost_usd, session_uuid)
    sessions : list of (session_uuid, model_usage_dict)
    """
    dbp = tmp_path / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE session_log (
            id INTEGER PRIMARY KEY, date TEXT, input_tokens INTEGER,
            output_tokens INTEGER, api_calls INTEGER,
            cache_create_1h_tokens INTEGER DEFAULT 0,
            cache_create_5m_tokens INTEGER DEFAULT 0,
            model_usage_json TEXT, all_model_usage_json TEXT, session_uuid TEXT);
        CREATE TABLE savings_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,
            tokens_saved INTEGER DEFAULT 0, cost_saved_usd REAL DEFAULT 0.0,
            session_id TEXT, detail TEXT, model TEXT, session_uuid TEXT);
        CREATE TABLE compression_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT, feature TEXT,
            original_tokens INTEGER DEFAULT 0, compressed_tokens INTEGER DEFAULT 0,
            compression_ratio REAL DEFAULT 0.0, quality_preserved INTEGER DEFAULT 1,
            verified INTEGER DEFAULT 0, detail TEXT, session_uuid TEXT,
            model TEXT, tier TEXT);
    """)
    now = datetime.now()
    today = now.date().isoformat()
    for uuid, usage in sessions:
        conn.execute(
            "INSERT INTO session_log(date,input_tokens,output_tokens,api_calls,"
            "model_usage_json,all_model_usage_json,session_uuid) VALUES(?,?,?,?,?,?,?)",
            (today, 1_000_000, 200_000, 100, json.dumps(usage), json.dumps(usage), uuid))
    for et, tok, model, cost, uuid in events:
        conn.execute(
            "INSERT INTO savings_events(timestamp,event_type,tokens_saved,"
            "cost_saved_usd,model,session_uuid) VALUES(?,?,?,?,?,?)",
            ((now - timedelta(hours=1)).isoformat(), et, tok, cost, model, uuid))
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))
    return dbp


# --------------------------------------------------------------------------
# 1. THE REPRICE PATH
# --------------------------------------------------------------------------

def test_sonnet_stamped_event_in_an_opus_session_is_repriced_to_opus(m, tmp_path, monkeypatch):
    """The headline defect: a sub-agent saving stamped 'sonnet' inside a session
    whose own recorded token usage is 100% Opus must be priced at the Opus input
    rate, not the resolver's Sonnet fallback."""
    sonnet = _rate(m, "sonnet")
    opus = _rate(m, "opus")
    tokens = 10_000_000
    _ledger_db(m, tmp_path, monkeypatch,
               events=[("tool_archive", tokens, "sonnet", tokens * sonnet / 1e6, "S1")],
               sessions=[("S1", {"claude-opus-5": 4_000_000})])

    s = m._get_savings_summary(days=30)

    assert s["repriced_to_session_mix"] is True
    assert s["total_cost_usd"] == pytest.approx(tokens * opus / 1e6, rel=1e-6)
    assert s["by_category"]["tool_archive"]["cost_saved_usd"] == pytest.approx(
        tokens * opus / 1e6, rel=1e-6)
    # Tokens are a count, never repriced.
    assert s["total_tokens"] == tokens


def test_null_model_rows_are_repriced_from_the_flat_fallback(m, tmp_path, monkeypatch):
    """model IS NULL rows (the largest slice of the real ledger) are priced at a
    flat fallback rate at event time. They must be repriced to the session's mix."""
    opus = _rate(m, "opus")
    tokens = 5_000_000
    _ledger_db(m, tmp_path, monkeypatch,
               # structure_map, not resume_lean: resume_lean is estimated-tier
               # and reprice-exempt since v5.13.1 (see test_resume_lean_savings).
               events=[("structure_map", tokens, None, tokens * 3.0 / 1e6, "S1")],
               sessions=[("S1", {"claude-opus-5": 9_000_000})])

    s = m._get_savings_summary(days=30)
    assert s["repriced_to_session_mix"] is True
    assert s["total_cost_usd"] == pytest.approx(tokens * opus / 1e6, rel=1e-6)


def test_mixed_session_blends_by_its_own_token_weighted_mix(m, tmp_path, monkeypatch):
    """A session that really ran both models is priced at ITS OWN blend, not at
    the dominant model and not at the window average."""
    sonnet = _rate(m, "sonnet")
    opus = _rate(m, "opus")
    tokens = 1_000_000
    _ledger_db(m, tmp_path, monkeypatch,
               events=[("tool_archive", tokens, "sonnet", tokens * sonnet / 1e6, "S1")],
               sessions=[("S1", {"claude-opus-5": 750_000, "claude-sonnet-5": 250_000})])

    s = m._get_savings_summary(days=30)
    expected = tokens * (0.75 * opus + 0.25 * sonnet) / 1e6
    assert s["total_cost_usd"] == pytest.approx(expected, rel=1e-6)


def test_unjoinable_rows_are_left_alone_and_counted_as_not_measured(m, tmp_path, monkeypatch):
    """No session row to price against -> NOT MEASURED. The event-time price
    stands; nothing is invented, and the reprice flag stays False."""
    sonnet = _rate(m, "sonnet")
    tokens = 2_000_000
    _ledger_db(m, tmp_path, monkeypatch,
               events=[("tool_archive", tokens, "sonnet", tokens * sonnet / 1e6, "GHOST")],
               sessions=[])

    s = m._get_savings_summary(days=30)
    assert s["repriced_to_session_mix"] is False
    assert s["total_cost_usd"] == pytest.approx(tokens * sonnet / 1e6, rel=1e-6)
    assert s["reprice_detail"]["unmeasured_tokens"] == tokens
    assert s["reprice_detail"]["repriced_tokens"] == 0


def test_missing_session_uuid_column_degrades_instead_of_zeroing_the_summary(m, tmp_path, monkeypatch):
    """An older DB without the join columns must simply not get the repair.

    The parked 2026-08-28 build let a schema error reach a blanket `except` and
    silently vanished an entire dashboard card. The savings summary must survive."""
    dbp = tmp_path / "old.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE session_log (id INTEGER PRIMARY KEY, date TEXT,
            input_tokens INTEGER, output_tokens INTEGER);
        CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL);
        CREATE TABLE compression_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            feature TEXT, original_tokens INTEGER, compressed_tokens INTEGER,
            compression_ratio REAL, quality_preserved INTEGER, verified INTEGER,
            model TEXT, tier TEXT);
    """)
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd)"
                 " VALUES(?,?,?,?)", (datetime.now().isoformat(), "tool_archive", 1_000_000, 3.0))
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))

    s = m._get_savings_summary(days=30)
    assert s["total_cost_usd"] == pytest.approx(3.0)
    assert s["total_tokens"] == 1_000_000
    assert s["repriced_to_session_mix"] is False


# --------------------------------------------------------------------------
# 2. NO DOUBLE-COUNT WITH THE ROUTING POOL
# --------------------------------------------------------------------------

def test_no_double_count_with_routing(m, tmp_path, monkeypatch):
    """A saving inside a session that genuinely RAN Sonnet stays at the Sonnet
    rate. Lifting it toward an Opus-heavy window average would claim the routing
    win twice: `_compute_model_routing_savings` already owns the Opus->Sonnet
    delta for the tokens that WERE sent."""
    sonnet = _rate(m, "sonnet")
    opus = _rate(m, "opus")
    tokens = 4_000_000
    _ledger_db(
        m, tmp_path, monkeypatch,
        events=[("tool_archive", tokens, "sonnet", tokens * sonnet / 1e6, "ROUTED"),
                ("checkpoint_restore", tokens, "sonnet", tokens * sonnet / 1e6, "OPUS")],
        sessions=[("ROUTED", {"claude-sonnet-5": 8_000_000}),
                  ("OPUS", {"claude-opus-5": 8_000_000})])

    s = m._get_savings_summary(days=30)
    # The routed session is untouched...
    assert s["by_category"]["tool_archive"]["cost_saved_usd"] == pytest.approx(
        tokens * sonnet / 1e6, rel=1e-6)
    # ...while the Opus session in the SAME window IS corrected -- and the
    # correction survives relocation: checkpoint_restore is popped to the
    # estimated tier, carrying its repriced dollars with it. Both assertions
    # must hold together: only the second one fails pre-change.
    assert s["checkpoint_restore_estimated"]["cost_saved_usd"] == pytest.approx(
        tokens * opus / 1e6, rel=1e-6)


# --------------------------------------------------------------------------
# 3. STRUCTURAL TOKENS STAY AT CACHE RATES
# --------------------------------------------------------------------------

def test_structural_stays_at_cache_rates(m, tmp_path, monkeypatch):
    """Structural savings are priced by `_compound_structural` as
    cache_write + cache_read x residual turns (prior art, 2026-08-11/12; the
    "just use cache_read" shortcut was explicitly rejected). The reprice touches
    fresh-input event pricing ONLY: the structural line must pass through
    byte-for-byte, and the merged total must be reprised-events + structural."""
    sonnet = _rate(m, "sonnet")
    opus = _rate(m, "opus")
    tokens = 6_000_000
    _ledger_db(m, tmp_path, monkeypatch,
               events=[("tool_archive", tokens, "sonnet", tokens * sonnet / 1e6, "S1")],
               sessions=[("S1", {"claude-opus-5": 3_000_000})])

    struct = {"events": 400, "tokens_saved": 12_345_678, "cost_saved_usd": 7.5,
              "baseline_source": "snapshot", "baseline_date": "2026-03-03",
              "overhead_delta": 900, "evidence": "estimated"}
    monkeypatch.setattr(m, "_compute_structural_savings", lambda days=30: dict(struct))
    monkeypatch.setattr(m, "_compute_structural_potential", lambda days=30: {})
    monkeypatch.setattr(m, "_compute_model_routing_savings",
                        lambda days=30, since=None: {"realized_cost_usd": 0.0})

    merged = m._get_merged_savings(days=30)
    assert merged["by_category"]["structural_savings"]["cost_saved_usd"] == 7.5
    # cache-rate structural + input-rate repriced events, nothing else.
    assert merged["total_cost_usd"] == pytest.approx(tokens * opus / 1e6 + 7.5, rel=1e-6)
    assert merged["repriced_to_session_mix"] is True


# --------------------------------------------------------------------------
# 4. THE ESTIMATED-VS-MEASURED LABEL, ACROSS THE CACHE
# --------------------------------------------------------------------------

def _temp_runway_db(m, tmp_path, monkeypatch):
    dbp = tmp_path / "runway.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE session_log (id INTEGER PRIMARY KEY, date TEXT,
            input_tokens INTEGER, output_tokens INTEGER);
        CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            event_type TEXT, tokens_saved INTEGER);
        CREATE TABLE compression_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            original_tokens INTEGER, compressed_tokens INTEGER, tier TEXT);
    """)
    conn.execute("INSERT INTO session_log(date,input_tokens,output_tokens) VALUES(?,?,?)",
                 (datetime.now().date().isoformat(), 1_000_000, 200_000))
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved) VALUES(?,?,?)",
                 (datetime.now().isoformat(), "archive", 50_000))
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))


def _fresh_meters():
    return lambda **k: {"available": True, "stale": False, "five_hour_pct": 12.0,
                        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3}


def test_repriced_context_is_labelled_estimated_not_measured(m, tmp_path, monkeypatch):
    """A repriced figure is a counterfactual rate step. Even with ZERO routing
    dollars the tier must read 'estimated', on the per-window line and on the
    top-level spine alike."""
    _temp_runway_db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", lambda days=30, since=None: {
        "total_cost_usd": 120.0,
        "model_routing": {"realized_cost_usd": 0.0},
        "repriced_to_session_mix": True,
    })

    r = m.runway_snapshot(days=30)
    assert r is not None, "the card must still render"
    assert r["saved_usd_tier"] == "estimated"
    for w in r["windows"]:
        if w["saved_usd"] is not None:
            assert w["saved_usd_tier"] == "estimated"


def test_weekly_spine_survives_the_overage_cache_hit(m, tmp_path, monkeypatch):
    """The cache-HIT path that the parked build broke.

    The parked patch read `wm.get("repriced_to_mix")` AFTER the cache block, where
    `wm` is bound only on the cache-MISS branch, so any hit raised
    UnboundLocalError -- swallowed by the outer handler into a vanished card. The
    weekly spine now goes through `_window_overage_usd` unconditionally, so a
    normal render with a live 7d window exercises the hit on every call. The
    ledger must be consulted exactly ONCE for the weekly key (proving the hit),
    the card must render, and the flag must survive the cache round-trip."""
    _temp_runway_db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    calls = []

    def _merged(days=30, since=None):
        calls.append((days, since))
        return {"total_cost_usd": 90.0,
                "model_routing": {"realized_cost_usd": 0.0},
                "repriced_to_session_mix": True}

    monkeypatch.setattr(m, "_get_merged_savings", _merged)

    r = m.runway_snapshot(days=30)
    assert r is not None, "cache hit must not vanish the card (UnboundLocalError regression)"
    weekly = [c for c in calls if c[0] == 7]
    assert len(weekly) == 1, f"weekly ledger recomputed instead of cache-hit: {calls!r}"
    assert r["saved_usd_context"] == 90.0
    assert r["saved_usd_tier"] == "estimated", "reprice flag lost across the overage cache"


def test_tier_stays_measured_when_nothing_was_repriced(m, tmp_path, monkeypatch):
    """The repair running and finding nothing to change leaves a pure event-time
    meter, which is honestly 'measured'. No gratuitous downgrade."""
    _temp_runway_db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)
    monkeypatch.setattr(m, "_keepwarm_read_meters", _fresh_meters())
    monkeypatch.setattr(m, "_get_merged_savings", lambda days=30, since=None: {
        "total_cost_usd": 120.0,
        "model_routing": {"realized_cost_usd": 0.0},
        "repriced_to_session_mix": False,
    })

    r = m.runway_snapshot(days=30)
    assert r is not None
    assert r["saved_usd_tier"] == "measured"
