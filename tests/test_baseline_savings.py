"""Baseline-savings stability tests (v5.11.18).

Proves the headline fix: the pre-TO baseline ("old way / session") is a FROZEN,
factual anchor. It must NOT change when this period's workload volume changes; it
moves only with prices. The "now / session" figure must move only with EFFICIENCY
(model mix + cache reuse), never with workload size. Monthly = per-session x count.

Run: python3 -m pytest tests/test_baseline_savings.py -v
"""
from _activity_fixtures import stamp_activity
import importlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"

# A frozen typical pre-TO session (mirrors the real baseline_state.json shape).
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


# Native cache-hit of FROZEN_BASELINE: 13.5M / (13.5M + 30K) ~= 0.9978. Tests default
# the CURRENT hit to the same value so the caching lever is ~0 and routing is isolated;
# tests that exercise caching pass an explicit hit above/below this.
_BASE_HIT = 13500000.0 / (13500000.0 + 30000.0)


def _build_env(tmp, *, n_sessions, per_session_input, opus_share, hit=_BASE_HIT):
    """Create an isolated snapshot dir with a trends.db + frozen baseline.

    n_sessions / per_session_input define the CURRENT window (the thing that must NOT
    move the baseline). opus_share + hit define the CURRENT efficiency (the thing that
    SHOULD move the 'now' figure).
    """
    snap = Path(tmp)
    (snap).mkdir(parents=True, exist_ok=True)
    (snap / "baseline_state.json").write_text(json.dumps(FROZEN_BASELINE))
    db = snap / "trends.db"
    if db.exists():
        db.unlink()  # reset so a reused tmp dir (two _run calls) starts clean
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    today = datetime.now().strftime("%Y-%m-%d")
    out_per = int(per_session_input * 0.01)
    cw_per = int(per_session_input * 0.03)
    opus_tok = int(per_session_input * opus_share)
    sonnet_tok = int(per_session_input * (1 - opus_share))
    for i in range(n_sessions):
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, cache_create_1h_tokens, "
            "all_model_usage_json, is_sidechain, duration_minutes) VALUES (?,?,?,?,?,?,?,?,0,5.0)",
            (f"/s/{tmp}/{i}.jsonl", today, per_session_input, out_per, hit,
             cw_per, 0, json.dumps({OPUS: opus_tok, SONNET: sonnet_tok})),
        )
    stamp_activity(conn)
    conn.commit()
    conn.close()
    return snap


@pytest.fixture
def measure(monkeypatch):
    """Import measure.py fresh under a temp snapshot dir (env read at import time)."""
    tmp = tempfile.mkdtemp(prefix="to-baseline-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    # The subagent pool scans the REAL ~/.claude transcript dir (not under SNAPSHOT_DIR),
    # so stub it to zero for hermetic, deterministic tests of the main-pool math.
    monkeypatch.setattr(mod, "_subagent_pool_savings", lambda **kw: {
        "actual_usd": 0.0, "counterfactual_usd": 0.0, "transformation_usd": 0.0,
        "premium_delegation_usd": 0.0, "sessions": 0, "by_model": {}})
    yield mod, tmp
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _run(mod, tmp, **kw):
    _build_env(tmp, **kw)
    return mod._estimate_before_after_savings(days=30)


# ---------------------------------------------------------------- core invariant

def test_baseline_is_frozen_across_different_current_volumes(measure):
    """THE fix: 5x the current per-session volume, the 'old way / session' must not move."""
    mod, tmp = measure
    light = _run(mod, tmp, n_sessions=40, per_session_input=2_000_000, opus_share=0.56)
    heavy = _run(mod, tmp, n_sessions=40, per_session_input=10_000_000, opus_share=0.56)
    assert light["before_cost_per_session"] == heavy["before_cost_per_session"], \
        "baseline per-session cost moved when current workload volume changed"
    assert light["after_cost_per_session"] == heavy["after_cost_per_session"], \
        "'now' per-session cost moved on volume alone (should move only with efficiency)"
    assert light["savings_per_session"] == heavy["savings_per_session"]


def test_baseline_frozen_across_session_count(measure):
    """Changing the session COUNT must not move the per-session baseline (only monthly)."""
    mod, tmp = measure
    few = _run(mod, tmp, n_sessions=20, per_session_input=4_000_000, opus_share=0.56)
    many = _run(mod, tmp, n_sessions=200, per_session_input=4_000_000, opus_share=0.56)
    assert few["before_cost_per_session"] == many["before_cost_per_session"]
    assert few["savings_per_session"] == many["savings_per_session"]
    # Monthly scales ~linearly with count (10x sessions -> ~10x monthly savings).
    ratio = many["monthly_savings_usd"] / max(1e-9, few["monthly_savings_usd"])
    assert 9.0 < ratio < 11.0, f"monthly did not scale with count (ratio={ratio})"


def test_before_tokens_is_per_session_not_monthly_total(measure):
    """Bug: before_tokens must equal the typical SESSION footprint, not the period total."""
    mod, tmp = measure
    r = _run(mod, tmp, n_sessions=100, per_session_input=4_000_000, opus_share=0.56)
    ts = FROZEN_BASELINE["typical_session"]
    expected = int(ts["fresh_input"] + ts["cache_read"] + ts["cache_write"] + ts["output"])
    assert r["before_tokens"] == expected
    assert r["after_tokens"] == expected
    # Sanity: it must be ~14M (one session), nowhere near 100 * 4M = 400M (the period total).
    assert r["before_tokens"] < 50_000_000


# ---------------------------------------------------------------- efficiency reacts

def test_now_reacts_to_model_mix(measure):
    """Higher current Opus share -> smaller routing saving -> higher 'now' cost."""
    mod, tmp = measure
    lean = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    heavy_opus = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.90)
    # Old way is the SAME frozen anchor regardless of current mix.
    assert lean["before_cost_per_session"] == heavy_opus["before_cost_per_session"]
    # Running more Opus now costs more now -> less saved per session.
    assert heavy_opus["after_cost_per_session"] > lean["after_cost_per_session"]
    assert heavy_opus["savings_per_session"] < lean["savings_per_session"]


def test_premium_sidechain_mix_does_not_raise_main_work_actual_cost(measure):
    """Premium delegation is a separate population, not the headline's now arm."""
    mod, tmp = measure
    _build_env(tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    conn = sqlite3.connect(Path(tmp) / "trends.db")
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,cache_hit_rate,"
        "all_model_usage_json,is_sidechain,duration_minutes) VALUES (?,?,?,?,?,?,1,?)",
        ("/s/premium-sidechain.jsonl", today, 100_000_000, 1_000_000, _BASE_HIT,
         json.dumps({"claude-opus-4-7": 101_000_000}), 1.0),
    )
    stamp_activity(conn)
    conn.commit()
    conn.close()
    with_premium_delegation = mod._estimate_before_after_savings(days=30)
    without_delegation = _run(
        mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    assert with_premium_delegation["after_cost_per_session"] == without_delegation["after_cost_per_session"]
    assert with_premium_delegation["sessions_per_month"] == 40


def test_headline_scales_with_delegated_window_volume(measure):
    """The sidechain arms aggregate the full window, so 5x the delegated volume at
    the same unit economics is 5x the real monthly saving. No session-count ratio
    may rescale it (the DB main count and disk sidechain count are measured under
    different inclusion rules, so a ratio of them is unitless)."""
    mod, tmp = measure
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 100.0, "counterfactual_usd": 300.0,
        "transformation_usd": 200.0, "premium_delegation_usd": 0.0,
        "sessions": 10, "by_model": {"Haiku": 100.0}}
    normal = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)

    # Five times as many identical delegated sessions: five times the window's
    # delegated spend AND savings, all of it real money.
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 500.0, "counterfactual_usd": 1_500.0,
        "transformation_usd": 1_000.0, "premium_delegation_usd": 0.0,
        "sessions": 50, "by_model": {"Haiku": 500.0}}
    burst = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)

    assert normal["subagent_transformation_usd"] == pytest.approx(200.0)
    assert burst["subagent_transformation_usd"] == pytest.approx(1_000.0)
    # The scaling property is checked on the uncapped figure: the spend cap
    # (which prevents the headline from exceeding actual spend) is a separate
    # concern that may bind on either scenario, so the pure arithmetic must
    # be verified on the pre-cap number.
    assert burst["uncapped_monthly_savings_usd"] == pytest.approx(
        normal["uncapped_monthly_savings_usd"] + 800.0, abs=0.01), (
            "delegated window savings must enter the headline at face value")


def test_headline_moves_when_delegated_session_efficiency_changes(measure):
    """The normalized delegated-session rate must still react to unit economics."""
    mod, tmp = measure
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 200.0, "counterfactual_usd": 300.0,
        "transformation_usd": 100.0, "premium_delegation_usd": 0.0,
        "sessions": 10, "by_model": {"Haiku": 200.0}}
    less_efficient = _run(
        mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)

    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 100.0, "counterfactual_usd": 300.0,
        "transformation_usd": 200.0, "premium_delegation_usd": 0.0,
        "sessions": 10, "by_model": {"Haiku": 100.0}}
    more_efficient = _run(
        mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)

    assert more_efficient["monthly_savings_usd"] > less_efficient["monthly_savings_usd"]


def test_costlier_delegated_session_rate_remains_negative(measure):
    """A genuinely costlier delegated-session pool must not be silently clamped to zero."""
    mod, tmp = measure
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 500.0, "counterfactual_usd": 100.0,
        "transformation_usd": 0.0, "premium_delegation_usd": 400.0,
        "sessions": 10, "by_model": {"Fable": 500.0}}
    result = _run(
        mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)

    assert result["subagent_transformation_usd"] == pytest.approx(-400.0)


def test_costlier_delegation_reduces_headline_and_stays_disclosed(measure):
    """Signed sidechain economics carry into the headline at window face value."""
    mod, tmp = measure
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 600.0, "counterfactual_usd": 400.0,
        "transformation_usd": 200.0, "premium_delegation_usd": 400.0,
        "sessions": 12, "by_model": {"Fable": 500.0, "Haiku": 100.0}}
    mixed = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 100.0, "counterfactual_usd": 300.0,
        "transformation_usd": 200.0, "premium_delegation_usd": 0.0,
        "sessions": 4, "by_model": {"Haiku": 100.0}}
    cheap_only = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    assert mixed["subagent_transformation_usd"] == pytest.approx(-200.0)
    # The fixture's main pool saves ~$178/mo, so a -$200 sidechain pool tips the
    # combined net negative: the hero hides rather than claim a saving.
    assert mixed["monthly_savings_usd"] == 0.0
    assert mixed["reason"] == "net_negative"
    assert mixed["premium_delegation_cost_usd"] == pytest.approx(400.0)
    assert cheap_only["subagent_transformation_usd"] == pytest.approx(200.0)
    assert cheap_only["monthly_savings_usd"] > mixed["monthly_savings_usd"]
    assert cheap_only["premium_delegation_cost_usd"] == 0.0


def test_headline_moves_when_delegated_session_mix_becomes_costlier(measure):
    """Changing sidechain unit economics must move the headline dollar for dollar."""
    mod, tmp = measure
    base_pool = {
        "actual_usd": 100.0, "counterfactual_usd": 300.0,
        "transformation_usd": 200.0, "premium_delegation_usd": 0.0,
        "sessions": 8, "by_model": {"Haiku": 100.0}}
    mod._subagent_pool_savings = lambda **kw: dict(base_pool)
    before = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    # Same window plus premium bundles: actual +$250 for work priced $50 at the
    # baseline mix. Cheap-delegation savings are untouched.
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 350.0, "counterfactual_usd": 350.0,
        "transformation_usd": 200.0, "premium_delegation_usd": 200.0,
        "sessions": 12, "by_model": {"Haiku": 100.0, "Fable": 250.0}}
    after = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    assert before["subagent_transformation_usd"] == pytest.approx(200.0)
    assert after["subagent_transformation_usd"] == 0.0
    assert after["monthly_savings_usd"] == pytest.approx(
        before["monthly_savings_usd"] - 200.0, abs=0.01)
    assert after["premium_delegation_cost_usd"] == pytest.approx(200.0)


def test_pool_payload_schema_does_not_change_signed_sidechain_rate(measure):
    """Old and new cache payload shapes must use the same raw-arm arithmetic."""
    mod, tmp = measure
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 500.0, "counterfactual_usd": 100.0,
        "transformation_usd": 0.0, "sessions": 12, "by_model": {"Fable": 500.0}}
    old_payload = _run(
        mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    mod._subagent_pool_savings = lambda **kw: {
        "actual_usd": 500.0, "counterfactual_usd": 100.0,
        "transformation_usd": 0.0, "premium_delegation_usd": 400.0,
        "sessions": 12, "by_model": {"Fable": 500.0}}
    new_payload = _run(
        mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.30)
    assert old_payload["subagent_transformation_usd"] == pytest.approx(-400.0)
    assert new_payload["subagent_transformation_usd"] == old_payload["subagent_transformation_usd"]
    assert new_payload["monthly_savings_usd"] == old_payload["monthly_savings_usd"]


def test_now_reacts_to_cache_reuse(measure):
    """Better current cache reuse -> lower 'now' cost -> more saved."""
    mod, tmp = measure
    cold = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.56, hit=0.95)
    warm = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.56, hit=0.99)
    assert cold["before_cost_per_session"] == warm["before_cost_per_session"]
    assert warm["after_cost_per_session"] < cold["after_cost_per_session"]


# ---------------------------------------------------------------- internal consistency

def test_monthly_equals_per_session_times_count(measure):
    """main counterfactual / actual monthly == per-session anchor x session count."""
    mod, tmp = measure
    n = 73
    r = _run(mod, tmp, n_sessions=n, per_session_input=4_000_000, opus_share=0.56)
    assert r["sessions_per_month"] == n
    # before_cost_per_session is rounded to 4dp, so x73 drifts a few cents from the
    # unrounded monthly. Allow that quantization gap.
    assert r["main_counterfactual_monthly_usd"] == pytest.approx(
        r["before_cost_per_session"] * n, abs=0.05)
    assert r["main_actual_monthly_usd"] == pytest.approx(
        r["after_cost_per_session"] * n, abs=0.05)


def test_breakdown_levers_telescope_to_main_transformation(measure):
    """routing + caching lever dollars must sum to the main transformation (no leakage)."""
    mod, tmp = measure
    r = _run(mod, tmp, n_sessions=50, per_session_input=4_000_000, opus_share=0.56)
    bd = {b["key"]: b["monthly_usd"] for b in r["breakdown"]}
    main = r["main_transformation_usd"]
    levers = bd.get("routing", 0.0) + bd.get("context_rereads", 0.0)
    assert levers == pytest.approx(main, abs=0.05), \
        f"levers {levers} != main transformation {main}"


def test_per_session_reconciles_to_main_not_headline(measure):
    """CRITICAL-1 guard: savings_per_session x sessions == MAIN transformation. The headline
    additionally folds in signed subagent, compression, and verbosity transformations.
    Encodes the intentionally non-equal relationship so prose and UX cannot silently drift."""
    mod, tmp = measure
    # Give the subagent pool a fixed non-zero contribution to prove the headline exceeds
    # the per-session-derived main figure by exactly that pool.
    monkeypatch_sub = {"actual_usd": 100.0, "counterfactual_usd": 350.0,
                       "transformation_usd": 250.0, "sessions": 5, "by_model": {}}
    mod._subagent_pool_savings = lambda **kw: dict(monkeypatch_sub)
    n = 50
    r = _run(mod, tmp, n_sessions=n, per_session_input=4_000_000, opus_share=0.56)
    main = r["main_transformation_usd"]
    assert r["savings_per_session"] * n == pytest.approx(main, abs=0.05)
    # Sidechain window net = $350 - $100, entering the headline at face value.
    window_subagent = 250.0
    assert r["monthly_savings_usd"] == pytest.approx(
        main + window_subagent + r["compression_transformation_usd"]
        + r["verbosity_transformation_usd"]
        + r["short_session_transformation_usd"],
        abs=0.05)
    assert r["monthly_savings_usd"] > r["savings_per_session"] * n  # headline strictly exceeds


def test_caching_regression_reports_negative_main_and_hides_headline(measure):
    """A cache regression stays visible in the main-pool accounting, while the hero hides.

    The headline must not claim a saving by clamping a net-negative pool to zero: its arms
    and attribution use the same honest (counterfactual - actual) value.
    """
    mod, tmp = measure
    r = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.90, hit=0.40)
    assert r["main_transformation_usd"] < 0
    assert r["main_actual_monthly_usd"] > r["main_counterfactual_monthly_usd"]
    # CHANGED 2026-08-29: this previously asserted `before_cost_per_session == 0.0`.
    # That zeroing was itself the product bug -- the dashboard rendered an EMPTY card and
    # the user could not distinguish "no savings this period" from "the math broke". The
    # arms are now reported honestly on the net-negative path; only the HEADLINE stays
    # hidden (monthly_savings_usd == 0), which is what the "hides_headline" half of this
    # test's name is about.
    assert r["monthly_savings_usd"] == 0.0, "a net-negative period must not claim a headline"
    assert r["before_cost_per_session"] > 0.0, "net-negative card must still show the arms"
    assert r["after_cost_per_session"] > 0.0
    assert r["savings_per_session"] < 0.0, "the per-session delta must read honestly negative"
    assert r["transformation_state"] == "net_negative"
    # And crucially it does NOT raise / returns a well-formed dict.
    assert r["reason"] == "net_negative"
    # The net_negative path populates the fields known at that point (the CLI
    # printer reads baseline_source) instead of leaving them at the `zero` defaults.
    assert r["baseline_source"] == "pretool_baseline"
    assert r["sessions_per_month"] == 40
    assert r["before_tokens"] > 0


def test_savings_per_session_positive_for_opus_heavy_baseline(measure):
    """Sanity: a 95%-Opus baseline vs a 56%-Opus now must show a real per-session saving."""
    mod, tmp = measure
    r = _run(mod, tmp, n_sessions=40, per_session_input=4_000_000, opus_share=0.56)
    assert r["before_cost_per_session"] > r["after_cost_per_session"] > 0
    assert r["savings_per_session"] > 0
    assert r["before_opus"] == pytest.approx(0.95)
