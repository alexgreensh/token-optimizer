"""Transformation undercount regression tests.

Three independent leaks made the estimated transformation disagree with the
window's real spend:

1. Sidechain rescale: the sidechain pool's arms already aggregate the full
   `days` window, so they ARE the monthly figures. Multiplying them by
   (quality-filtered DB main count / raw on-disk sidechain count) replaced real
   delegated spend with a unitless ratio of two populations measured under
   different inclusion rules.
2. Main "now" mix source: the actual-arm model mix summed all_model_usage_json,
   which includes rolled-up SUBAGENT tokens. Sidechain work has its own pool, so
   the main arm must be priced at the parent-thread mix (model_usage_json).
3. Below-threshold sessions: the quality gates rightly keep sub-minute one-shot
   sessions out of the session COUNT (the count multiplies a frozen
   typical-session anchor), but those sessions' real billed tokens earned no
   transformation at all. They are now priced as their own pool.

Run: python3 -m pytest tests/test_transformation_undercount.py -v
"""
from _activity_fixtures import stamp_activity
import importlib
import inspect
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"

FROZEN_BASELINE = {
    "version": 4,
    "activity_basis": 1,
    "typical_session": {
        "fresh_input": 30000.0, "cache_write": 487000.0,
        "cache_read": 13500000.0, "output": 51000.0,
    },
    "opus_share": 0.95,
    "opus_share_source": "pretool_baseline",
    "model_shares": {"opus": 0.95, "sonnet": 0.05},
    "window": {"start": "2026-03-18", "end": "2026-04-17",
               "sessions_used": 750, "sessions_total": 750, "elapsed": True},
    "method": "winsorized_mean", "winsor_pct": 0.99,
    "structural_overhead_tokens": 25288,
    "captured_at": "2026-04-17T01:21:13", "source": "frozen_from_history",
}

_SCHEMA = """
CREATE TABLE session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jsonl_path TEXT UNIQUE, date TEXT NOT NULL, project TEXT,
    duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
    message_count INTEGER, api_calls INTEGER, cache_hit_rate REAL,
    cache_create_1h_tokens INTEGER DEFAULT 0, cache_create_5m_tokens INTEGER DEFAULT 0,
    cache_ttl_scanned INTEGER DEFAULT 0, avg_call_gap_seconds REAL,
    max_call_gap_seconds REAL, p95_call_gap_seconds REAL, skills_json TEXT,
    subagents_json TEXT, tool_calls_json TEXT, model_usage_json TEXT,
    all_model_usage_json TEXT, model_usage_breakdown_json TEXT, version TEXT,
    slug TEXT, topic TEXT, collected_at TEXT, quality_score REAL, quality_grade TEXT,
    stale_waste_tokens INTEGER DEFAULT 0, session_uuid TEXT, is_sidechain INTEGER DEFAULT 0
);
"""

_BASE_HIT = 13500000.0 / (13500000.0 + 30000.0)

_ZERO_SUB_POOL = {
    "actual_usd": 0.0, "counterfactual_usd": 0.0, "transformation_usd": 0.0,
    "premium_delegation_usd": 0.0, "sessions": 0, "by_model": {}}


def _insert_quality_sessions(conn, tmp, *, n_sessions, per_session_input,
                             opus_share, hit=_BASE_HIT, parent_usage=None,
                             all_usage=None):
    today = datetime.now().strftime("%Y-%m-%d")
    out_per = int(per_session_input * 0.01)
    cw_per = int(per_session_input * 0.03)
    if all_usage is None:
        all_usage = {OPUS: int(per_session_input * opus_share),
                     SONNET: int(per_session_input * (1 - opus_share))}
    for i in range(n_sessions):
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, cache_create_1h_tokens, "
            "model_usage_json, all_model_usage_json, is_sidechain, duration_minutes) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,5.0)",
            (f"/s/{tmp}/q{i}.jsonl", today, per_session_input, out_per, hit,
             cw_per, 0,
             json.dumps(parent_usage) if parent_usage is not None else None,
             json.dumps(all_usage)),
        )


def _insert_short_sessions(conn, tmp, *, n_sessions, breakdown, input_tokens=50_000,
                           duration=0.4):
    """Below-threshold rows: real tokens, sub-minute duration."""
    today = datetime.now().strftime("%Y-%m-%d")
    for i in range(n_sessions):
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, model_usage_breakdown_json, is_sidechain, duration_minutes) "
            "VALUES (?,?,?,?,?,?,0,?)",
            (f"/s/{tmp}/short{i}.jsonl", today, input_tokens, 500, 0.4,
             json.dumps(breakdown) if breakdown is not None else None, duration),
        )


def _fresh_env(tmp):
    snap = Path(tmp)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "baseline_state.json").write_text(json.dumps(FROZEN_BASELINE))
    db = snap / "trends.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    return conn


@pytest.fixture
def measure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-undercount-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_subagent_pool_savings",
                        lambda **kw: dict(_ZERO_SUB_POOL))
    yield mod, tmp
    if "measure" in sys.modules:
        del sys.modules["measure"]


# ------------------------------------------------------- leak 1: sidechain rescale

def test_sidechain_pool_enters_headline_at_window_value(measure):
    """The sidechain arms aggregate the full window, so they enter the monthly
    net at face value. No session-count ratio may shrink (or inflate) them."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    stamp_activity(conn)
    conn.commit()
    conn.close()
    # 1400 delegated sessions saved a net $250 over the window. The pre-fix
    # rescale multiplied this by 40/1400 and reported ~$7.
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 100.0, "counterfactual_usd": 350.0,
        "transformation_usd": 250.0, "premium_delegation_usd": 0.0,
        "sessions": 1400, "by_model": {"Haiku": 100.0}}
    r = mod._estimate_before_after_savings(days=30)
    assert r["subagent_transformation_usd"] == pytest.approx(250.0)
    assert r["subagent_actual_usd"] == pytest.approx(100.0)
    assert r["subagent_counterfactual_usd"] == pytest.approx(350.0)


def test_sidechain_delta_independent_of_main_session_count(measure):
    """The same delegated window must contribute the same dollars whether the
    user ran 20 or 200 main sessions beside it."""
    mod, tmp = measure
    pool = {"actual_usd": 400.0, "counterfactual_usd": 1_000.0,
            "transformation_usd": 600.0, "premium_delegation_usd": 0.0,
            "sessions": 100, "by_model": {"Haiku": 400.0}}
    results = []
    for n in (20, 200):
        conn = _fresh_env(tmp)
        _insert_quality_sessions(conn, tmp, n_sessions=n,
                                 per_session_input=4_000_000, opus_share=0.30)
        stamp_activity(conn)
        conn.commit()
        conn.close()
        mod._subagent_pool_savings = lambda **kw: dict(pool)
        results.append(mod._estimate_before_after_savings(days=30))
    assert results[0]["subagent_transformation_usd"] == pytest.approx(600.0)
    assert results[1]["subagent_transformation_usd"] == pytest.approx(600.0)


# ------------------------------------------------------- leak 2: now-arm mix source

def test_now_arm_mix_ignores_rolled_up_subagent_usage(measure):
    """all_model_usage_json includes subagent tokens (the rollup); the main
    pool's actual arm must be priced at the parent-thread mix only. Sidechain
    work has its own pool."""
    mod, tmp = measure

    # Env 1: parent threads ran pure Opus; subagents added 9x that volume on
    # Haiku (visible only in all_model_usage_json).
    conn = _fresh_env(tmp)
    _insert_quality_sessions(
        conn, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=1.0,
        parent_usage={OPUS: 4_000_000},
        all_usage={OPUS: 4_000_000, HAIKU: 36_000_000})
    stamp_activity(conn)
    conn.commit()
    conn.close()
    with_rollup = mod._estimate_before_after_savings(days=30)

    # Env 2: identical parent threads, no subagent rollup at all.
    conn = _fresh_env(tmp)
    _insert_quality_sessions(
        conn, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=1.0,
        parent_usage={OPUS: 4_000_000}, all_usage={OPUS: 4_000_000})
    stamp_activity(conn)
    conn.commit()
    conn.close()
    without_rollup = mod._estimate_before_after_savings(days=30)

    assert with_rollup["after_cost_per_session"] == pytest.approx(
        without_rollup["after_cost_per_session"]), (
        "subagent rollup tokens changed the MAIN pool's actual-arm pricing")


def test_now_arm_mix_falls_back_to_all_usage_when_parent_missing(measure):
    """Legacy rows carry only all_model_usage_json; the mix must still resolve
    rather than bailing with no_mix."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30,
                             parent_usage=None)
    stamp_activity(conn)
    conn.commit()
    conn.close()
    r = mod._estimate_before_after_savings(days=30)
    assert r.get("reason") != "no_mix"
    assert r["after_cost_per_session"] > 0


# ------------------------------------------------------- leak 3: short-session pool

_SHORT_BREAKDOWN = {
    SONNET: {"fresh_input": 20_000, "cache_read": 25_000, "cache_create": 5_000,
             "cache_create_1h": 0, "cache_create_5m": 5_000, "output": 500}}


def test_short_sessions_priced_as_their_own_pool(measure):
    """Below-threshold sessions carry real billed tokens. They stay out of the
    session count but their routing delta joins the headline."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    _insert_short_sessions(conn, tmp, n_sessions=300, breakdown=_SHORT_BREAKDOWN)
    stamp_activity(conn)
    conn.commit()
    conn.close()
    r = mod._estimate_before_after_savings(days=30)
    # Count is unchanged: the frozen anchor multiplies quality sessions only.
    assert r["sessions_per_month"] == 40
    # Sonnet one-shots repriced at a 95%-Opus baseline save real dollars.
    assert r["short_session_transformation_usd"] > 0
    assert r["short_session_count"] == 300
    assert r["short_session_counterfactual_usd"] > r["short_session_actual_usd"] > 0


def test_short_session_pool_joins_headline_reconciliation(measure):
    """headline == main + sidechain + short + compression + verbosity."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    _insert_short_sessions(conn, tmp, n_sessions=300, breakdown=_SHORT_BREAKDOWN)
    stamp_activity(conn)
    conn.commit()
    conn.close()
    r = mod._estimate_before_after_savings(days=30)
    assert r["monthly_savings_usd"] == pytest.approx(
        r["main_transformation_usd"] + r["subagent_transformation_usd"]
        + r["short_session_transformation_usd"]
        + r["compression_transformation_usd"]
        + r["verbosity_transformation_usd"], abs=0.05)
    keys = {b["key"] for b in r["breakdown"]}
    assert "short_sessions" in keys
    # The combined arms include the pool on both sides.
    assert r["actual_monthly_usd"] == pytest.approx(
        r["main_actual_monthly_usd"] + r["subagent_actual_usd"]
        + r["short_session_actual_usd"], abs=0.05)


def test_estimated_avoided_volume_pools_join_headline_separately(measure):
    """Optimizer-caused estimates outside billed-volume pools must be explicit arms."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    stamp_activity(conn)
    conn.commit()
    conn.close()

    mod._estimate_uncaptured_runtime = lambda **kw: {"cost_saved_usd": 2.60}
    mod._estimate_behavioral_savings = lambda **kw: {"cost_saved_usd": 0.57}
    mod._estimate_handover_rerun_savings = lambda **kw: {"cost_saved_usd": 0.25}
    mod._estimate_retrieval_serve_savings = lambda **kw: {"cost_saved_usd": 0.08}
    original_summary = mod._get_savings_summary

    def savings_summary(**kw):
        summary = original_summary(**kw)
        summary["hint_followed_estimated"] = {"cost_saved_usd": 0.63}
        summary["mcp_cap_estimated"] = {"cost_saved_usd": 1.81}
        return summary

    mod._get_savings_summary = savings_summary
    r = mod._estimate_before_after_savings(days=30)
    pools = {
        "uncaptured_runtime": 2.60,
        "behavioral_loops": 0.57,
        "hint_followed": 0.63,
        "handover_rerun": 0.25,
        "retrieval_serve": 0.08,
    }
    ratio = r["compression_reprice_ratio"]
    breakdown = {item["key"]: item["monthly_usd"] for item in r["breakdown"]}
    for key, current_mix_usd in pools.items():
        assert breakdown[key] == pytest.approx(current_mix_usd * ratio, abs=0.01)
    assert "mcp_cap_estimated" not in breakdown
    assert r["estimated_volume_transformation_usd"] == pytest.approx(
        sum(pools.values()) * ratio, abs=0.01)
    assert r["monthly_savings_usd"] == pytest.approx(
        r["main_transformation_usd"] + r["subagent_transformation_usd"]
        + r["short_session_transformation_usd"]
        + r["compression_transformation_usd"]
        + r["verbosity_transformation_usd"]
        + r["estimated_volume_transformation_usd"], abs=0.05)


def test_compression_reprice_uses_the_routing_recipe_ratio(measure):
    """The add-back reprice is the routing recipe's standardized baseline/actual
    ratio -- the same factor the Savings tab and the plan card publish. It
    inherits exact-card pricing (no family or default fallback), premium
    exclusion, reference cards and the pending gate; it is NOT a second
    input-rate mix (the retired path priced unknown models as Sonnet, let
    premium burn into the current side and had no pending gate)."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.60)
    stamp_activity(conn)
    conn.commit()
    conn.close()

    mod._pretool_baseline_mix = lambda: {"opus": 0.95, "sonnet": 0.05}
    mod._mix_from_session_rows = lambda cutoff: {OPUS: 0.60, SONNET: 0.40}
    original_summary = mod._get_savings_summary

    def savings_summary(**kw):
        summary = original_summary(**kw)
        summary["total_cost_usd"] = 10.0
        return summary

    mod._get_savings_summary = savings_summary
    recipe = {"evidence": "estimated",
              "standardized_baseline_cost_usd": 12.0,
              "standardized_actual_cost_usd": 8.0}
    # The published caller hands the recipe result it already computed (with
    # its `since` window); a direct call resolves the recipe itself.
    r = mod._estimate_before_after_savings(days=30, model_routing=recipe)
    assert r["compression_reprice_ratio"] == pytest.approx(1.5)
    assert r["compression_reprice_basis"] == "routing_recipe"
    mod._compute_model_routing_savings = lambda days=30, since=None: recipe
    r = mod._estimate_before_after_savings(days=30)
    assert r["compression_reprice_ratio"] == pytest.approx(1.5)
    assert r["compression_reprice_basis"] == "routing_recipe"
    # Pending and unavailable routing reprice neutrally, never guessed, and
    # the payload says WHY the ratio is 1.0 so the card can disclose it.
    for state, basis in (("pending", "routing_pending"),
                         ("unavailable", "routing_unavailable")):
        r = mod._estimate_before_after_savings(
            days=30, model_routing={**recipe, "evidence": state})
        assert r["compression_reprice_ratio"] == 1.0
        assert r["compression_reprice_basis"] == basis
    # An estimated recipe with degenerate arms cannot supply a ratio either.
    r = mod._estimate_before_after_savings(
        days=30, model_routing={"evidence": "estimated",
                                "standardized_baseline_cost_usd": 0.0,
                                "standardized_actual_cost_usd": 8.0})
    assert r["compression_reprice_ratio"] == 1.0
    assert r["compression_reprice_basis"] == "routing_unavailable"


def test_transformation_reprice_has_no_second_routing_implementation(measure):
    """Guard: a published figure must not grow a private routing-price path
    again. `_input_rate_mix_ratio` priced unknown models at the Sonnet card,
    counted premium models in the current burn, used bare family cards and had
    no pending gate -- every defect docs/METHODOLOGY.md section 3.2 forbids."""
    mod, tmp = measure
    assert not hasattr(mod, "_input_rate_mix_ratio")
    src = inspect.getsource(mod._estimate_before_after_savings)
    assert "_input_rate_mix_ratio" not in src
    # The reprice helper answers only from the shared recipe payload.
    assert mod._routing_mix_reprice_ratio(None) == 1.0
    assert mod._routing_mix_reprice_ratio({"evidence": "pending"}) == 1.0
    assert mod._routing_mix_reprice_ratio(
        {"evidence": "estimated", "standardized_baseline_cost_usd": 9.0,
         "standardized_actual_cost_usd": 3.0}) == pytest.approx(3.0)
    assert mod._routing_mix_reprice_ratio(
        {"evidence": "estimated", "standardized_baseline_cost_usd": 0.0,
         "standardized_actual_cost_usd": 3.0}) == 1.0


def test_merged_savings_shares_one_recipe_and_one_window(measure):
    """_get_merged_savings computes the routing recipe once, on the caller's
    window, and hands it to _estimate_before_after_savings. Dropping the
    `model_routing=` kwarg would silently re-run the whole routing pass on the
    rolling `days` window; dropping `since` would price the compression dollars
    on a different window than the ratio that multiplies them."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.60)
    # Routing needs a real activity population to reach the compression add-back.
    stamp_activity(conn)
    conn.commit()
    conn.close()

    sentinel_since = "2026-10-01T00:00:00+00:00"
    recipe = {"evidence": "estimated",
              "standardized_baseline_cost_usd": 12.0,
              "standardized_actual_cost_usd": 8.0}
    routing_calls = []

    def fake_routing(days=30, since=None):
        routing_calls.append({"days": days, "since": since})
        return dict(recipe)

    mod._compute_model_routing_savings = fake_routing

    savings_calls = []
    original_summary = mod._get_savings_summary

    def savings_summary(days=30, since=None):
        savings_calls.append({"days": days, "since": since})
        summary = original_summary(days=days, since=since)
        summary["total_cost_usd"] = 10.0
        return summary

    mod._get_savings_summary = savings_summary

    merged = mod._get_merged_savings(days=7, since=sentinel_since)
    # Exactly one routing pass for the whole merged view, on the caller's
    # window -- the transformation must reuse it, not recompute.
    assert routing_calls == [{"days": 7, "since": sentinel_since}]
    # Every savings summary inside the merged view -- including the one that
    # prices the compression add-back dollars -- sees the same `since` cutoff.
    assert len(savings_calls) >= 2, savings_calls
    assert all(c["days"] == 7 for c in savings_calls), savings_calls
    assert all(c["since"] == sentinel_since for c in savings_calls), savings_calls
    ba = merged["before_after"]
    assert ba.get("reason") is None
    assert ba["after_sessions"] == 40
    assert ba["compression_reprice_ratio"] == pytest.approx(1.5)
    assert ba["compression_reprice_basis"] == "routing_recipe"
    assert ba["compression_measured_usd"] == pytest.approx(10.0)
    assert ba["compression_transformation_usd"] == pytest.approx(15.0)


def test_short_sessions_without_breakdown_contribute_nothing(measure):
    """Rows with no stored per-model breakdown cannot be priced; they must be
    skipped (conservative), never guessed."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    _insert_short_sessions(conn, tmp, n_sessions=50, breakdown=None)
    stamp_activity(conn)
    # stamp_activity synthesizes dated per-model buckets for every row, which
    # is what a real collect stores. A row with NO breakdown anywhere must
    # also lack dated usage evidence: the activity view can only price
    # per-day model buckets, so undated rows are conservatively excluded
    # rather than guessed at lifetime scope.
    conn.execute(
        "UPDATE session_log SET daily_usage_json=NULL, "
        "parent_daily_usage_json=NULL, first_activity_day=NULL, "
        "last_activity_day=NULL, daily_calls_json=NULL "
        "WHERE jsonl_path LIKE '%/short%'")
    conn.commit()
    conn.close()
    r = mod._estimate_before_after_savings(days=30)
    assert r["short_session_transformation_usd"] == 0.0
    assert r["short_session_actual_usd"] == 0.0


def test_short_session_pool_stays_signed_when_costlier(measure):
    """Opus one-shots against a 95%-Opus baseline are roughly flat; a pool run
    on a PREMIUM model must carry its negative sign into the net, not clamp."""
    mod, tmp = measure
    premium_bd = {
        "claude-fable-5": {"fresh_input": 200_000, "cache_read": 0,
                           "cache_create": 0, "cache_create_1h": 0,
                           "cache_create_5m": 0, "output": 20_000}}
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    _insert_short_sessions(conn, tmp, n_sessions=50, breakdown=premium_bd)
    stamp_activity(conn)
    conn.commit()
    conn.close()
    r = mod._estimate_before_after_savings(days=30)
    assert r["short_session_transformation_usd"] < 0
