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
import importlib
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
        conn.commit()
        conn.close()
        mod._subagent_pool_savings = lambda **kw: dict(pool)
        results.append(mod._estimate_before_after_savings(days=30))
    assert results[0]["subagent_transformation_usd"] == pytest.approx(600.0)
    assert results[1]["subagent_transformation_usd"] == pytest.approx(600.0)


# ------------------------------------------------------- leak 2: now-arm mix source

def test_now_arm_mix_ignores_rolled_up_subagent_usage(measure):
    """all_model_usage_json includes subagent tokens (fix #18 rollup); the main
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
    conn.commit()
    conn.close()
    with_rollup = mod._estimate_before_after_savings(days=30)

    # Env 2: identical parent threads, no subagent rollup at all.
    conn = _fresh_env(tmp)
    _insert_quality_sessions(
        conn, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=1.0,
        parent_usage={OPUS: 4_000_000}, all_usage={OPUS: 4_000_000})
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


def test_compression_reprice_and_runway_use_same_complete_mix_ratio(measure):
    """The global input-rate ratio includes delegated usage on both surfaces."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.60)
    conn.commit()
    conn.close()

    baseline = {"opus": 0.95, "sonnet": 0.05}
    complete_current = {OPUS: 0.20, SONNET: 0.80}
    main_only_current = {OPUS: 0.60, SONNET: 0.40}
    mod._pretool_baseline_mix = lambda: dict(baseline)
    mod._model_mix_shares = lambda **kw: {
        "shares": dict(complete_current), "total_tokens": 1_000_000, "days": 30}
    mod._mix_from_session_rows = lambda cutoff: dict(main_only_current)
    mod._keepwarm_read_meters = lambda **kw: {
        "available": True, "stale": False, "five_hour_pct": 10.0,
        "seven_day_pct": 20.0, "age_s": 1.0}
    original_summary = mod._get_savings_summary

    def savings_summary(**kw):
        summary = original_summary(**kw)
        summary["total_cost_usd"] = 10.0
        return summary

    mod._get_savings_summary = savings_summary
    transformation = mod._estimate_before_after_savings(days=30)
    runway = mod.runway_snapshot(days=30)

    rates = mod.PRICING_TIERS[mod._load_pricing_tier()]["claude_models"]
    expected = ((0.95 * rates["opus"]["input"] + 0.05 * rates["sonnet"]["input"])
                / (0.20 * rates["opus"]["input"] + 0.80 * rates["sonnet"]["input"]))
    main_only = ((0.95 * rates["opus"]["input"] + 0.05 * rates["sonnet"]["input"])
                 / (0.60 * rates["opus"]["input"] + 0.40 * rates["sonnet"]["input"]))
    assert transformation["compression_reprice_ratio"] == pytest.approx(expected, rel=1e-4)
    assert runway["routing_multiplier"] == pytest.approx(expected, abs=0.001)
    assert transformation["compression_reprice_ratio"] != pytest.approx(main_only, rel=1e-4)


def test_short_sessions_without_breakdown_contribute_nothing(measure):
    """Rows with no stored per-model breakdown cannot be priced; they must be
    skipped (conservative), never guessed."""
    mod, tmp = measure
    conn = _fresh_env(tmp)
    _insert_quality_sessions(conn, tmp, n_sessions=40,
                             per_session_input=4_000_000, opus_share=0.30)
    _insert_short_sessions(conn, tmp, n_sessions=50, breakdown=None)
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
    conn.commit()
    conn.close()
    r = mod._estimate_before_after_savings(days=30)
    assert r["short_session_transformation_usd"] < 0
