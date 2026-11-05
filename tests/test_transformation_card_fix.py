"""Transformation-card correctness tests (2026-08-29).

Covers four defects found in `_estimate_before_after_savings` and the rate card it
prices against:

1. ERA PRICING. Every Claude Sonnet collapsed into one `"sonnet"` bucket priced at
   the Sonnet 5 rate ($2/$10). Sonnet 5 was released 2026-06-30; the frozen baseline
   window (2026-03-18..2026-04-17) ran Sonnet 4.6 at $3/$15. Verified 2026-08-29
   against platform.claude.com/docs/en/about-claude/pricing.

2. SONNET 5 INTRO EXPIRY. `_SONNET_INTRO_PRICING_UNTIL` was 2026-09-01. Anthropic's
   pricing page now states the $2/$10 rate "is now the standard price" and "the
   previously scheduled increase to $3/$15 ... on September 1, 2026 will not occur."

3. before_cache_hit = 0.9995. Computed as cr/(fi+cr), which omits cache-WRITE from
   the denominator. A cache write is a miss (you pay 1.25x input to create it), and
   `fresh_input` is a residual (input*(1-hit) - cache_write), so the ratio is driven
   to ~1 mechanically. Must use cr/(fi+cw+cr).

4. NET-NEGATIVE IS INDISTINGUISHABLE FROM ZERO. On net <= 0 the function returned
   all-zero per-session fields, so the dashboard showed nothing and the user could
   not tell "no savings" from "the math broke".

Run: python3 -m pytest tests/test_transformation_card_fix.py -v
"""
from _activity_fixtures import stamp_activity
import importlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

# The real frozen JUNE baseline (captured 2026-06-23), window 2026-03-18..2026-04-17.
JUNE_BASELINE = {
    "version": 4,
    "activity_basis": 1,
    "typical_session": {
        "fresh_input": 43395.8, "cache_write": 688319.3,
        "cache_read": 20616554.3, "output": 74232.5,
    },
    "opus_share": 0.8369,
    "opus_share_source": "robust_earliest",
    "model_shares": {"haiku": 0.0947, "opus": 0.8369, "sonnet": 0.0683},
    "window": {"start": "2026-03-18", "end": "2026-04-17",
               "sessions_used": 478, "sessions_total": 478, "elapsed": True},
    "method": "winsorized_mean", "winsor_pct": 0.99,
    "structural_overhead_tokens": 0,
    "captured_at": "2026-06-23T23:01:19", "source": "frozen_from_history",
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


@pytest.fixture
def measure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-xform-fix-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_subagent_pool_savings", lambda **kw: {
        "actual_usd": 0.0, "counterfactual_usd": 0.0, "transformation_usd": 0.0,
        "premium_delegation_usd": 0.0, "sessions": 0, "by_model": {}})
    yield mod, tmp
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _seed(tmp, sessions, baseline=None):
    """sessions = list of (input_tokens, hit, cache_write, output, model_usage, minutes)."""
    snap = Path(tmp)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "baseline_state.json").write_text(json.dumps(baseline or JUNE_BASELINE))
    db = snap / "trends.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    today = datetime.now().strftime("%Y-%m-%d")
    for i, (inp, hit, cw, out, mu, mins) in enumerate(sessions):
        calls_per = 20
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, cache_create_1h_tokens, "
            "model_usage_json, all_model_usage_json, is_sidechain, duration_minutes) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (f"/s/{tmp}/{i}.jsonl", today, inp, out, hit, cw, 0,
             json.dumps(mu), json.dumps(mu), mins),
        )
    stamp_activity(conn)
    conn.commit()
    conn.close()
    return snap


# ------------------------------------------------------------------ 1. era pricing

def test_sonnet_4_6_prices_at_three_dollars_not_two(measure):
    """Sonnet 4.6 is $3/MTok input. Sonnet 5 ($2) did not exist until 2026-06-30.

    Source: platform.claude.com/docs/en/models/sonnet-4-6/overview (released
    February 17, 2026; input $3 / MTok, output $15, cache read $0.30, 5m write $3.75).
    """
    mod, _ = measure
    tier = "anthropic"
    legacy = mod._get_model_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0, tier=tier)
    current = mod._get_model_cost("claude-sonnet-5", 1_000_000, 0, 0, 0, tier=tier)
    assert legacy == pytest.approx(3.0), f"Sonnet 4.6 input priced at {legacy}, expected 3.0"
    assert current == pytest.approx(2.0), f"Sonnet 5 input priced at {current}, expected 2.0"


def test_sonnet_4_6_full_rate_card(measure):
    """Output $15, cache read $0.30, 5m cache write $3.75, 1h cache write $6."""
    mod, _ = measure
    t = "anthropic"
    assert mod._get_model_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0, tier=t) == pytest.approx(15.0)
    assert mod._get_model_cost("claude-sonnet-4-6", 0, 0, 1_000_000, 0, tier=t) == pytest.approx(0.30)
    assert mod._get_model_cost("claude-sonnet-4-6", 0, 0, 0, 1_000_000, tier=t) == pytest.approx(3.75)
    assert mod._get_model_cost("claude-sonnet-4-6", 0, 0, 0, 1_000_000, tier=t,
                               cache_create_1h=1_000_000, cache_create_5m=0) == pytest.approx(6.0)


def test_opus_4_6_through_opus_5_share_one_rate_card(measure):
    """Verified: Opus 4.5/4.6/4.7/4.8/5 are ALL $5/$25. No era reprice is warranted.

    Source: platform.claude.com/docs/en/about-claude/pricing (model pricing table).
    """
    mod, _ = measure
    for mid in ("claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5"):
        assert mod._get_model_cost(mid, 1_000_000, 0, 0, 0, tier="anthropic") == pytest.approx(5.0)
        assert mod._get_model_cost(mid, 0, 1_000_000, 0, 0, tier="anthropic") == pytest.approx(25.0)


# --------------------------------------------------- 2. Sonnet 5 intro became standard

def test_sonnet_5_two_dollars_is_permanent(measure):
    """$2/$10 is now the STANDARD Sonnet 5 price; the 2026-09-01 rise will not occur.

    Verbatim from platform.claude.com/docs/en/about-claude/pricing:
    "The $2/$10 per million input/output token pricing for Claude Sonnet 5, announced
    at launch as introductory pricing through August 31, 2026, is now the standard
    price. The previously scheduled increase to $3/$15 per million input/output tokens
    on September 1, 2026 will not occur."
    """
    mod, _ = measure
    for as_of in ("2026-08-29", "2026-09-01", "2026-12-31", "2027-06-01"):
        mod._apply_sonnet_intro_pricing(datetime.strptime(as_of, "%Y-%m-%d"))
        card = mod.PRICING_TIERS["anthropic"]["claude_models"]["sonnet"]
        assert card["input"] == pytest.approx(2.0), f"sonnet input flipped on {as_of}: {card}"
        assert card["output"] == pytest.approx(10.0), f"sonnet output flipped on {as_of}: {card}"
        assert card["cache_read"] == pytest.approx(0.20)
        assert card["cache_write"] == pytest.approx(2.50)


# --------------------------------------------------------------- 3. before_cache_hit

def test_before_cache_hit_counts_cache_write_as_a_miss(measure):
    """A cache WRITE is a miss: you pay 1.25x input to create it.

    With the JUNE anchor, cr/(fi+cr) = 0.9979 and cr/(fi+cw+cr) = 0.9657. The first is
    a decomposition artifact (fresh_input is the residual input*(1-hit) - cache_write),
    not a cache hit rate. The JULY anchor is the pathological case: 0.9995.
    """
    mod, tmp = measure
    _seed(tmp, [(20_000_000, 0.97, 500_000, 100_000,
                 {"claude-opus-4-8": 900_000, "claude-sonnet-5": 100_000}, 12.0)] * 30)
    res = mod._estimate_before_after_savings(days=30)
    ts = JUNE_BASELINE["typical_session"]
    expect = ts["cache_read"] / (ts["fresh_input"] + ts["cache_write"] + ts["cache_read"])
    got = res.get("before_cache_hit")
    assert got is not None, "before_cache_hit missing from the payload"
    assert got == pytest.approx(expect, abs=1e-4), \
        f"before_cache_hit={got}, expected {expect:.4f} (cache-write-inclusive)"
    assert got < 0.99, f"before_cache_hit={got} is still the >=0.99 artifact"


def test_after_cache_hit_uses_the_same_denominator(measure):
    """Both arms must use one definition or the displayed pair is not comparable."""
    mod, tmp = measure
    _seed(tmp, [(20_000_000, 0.97, 500_000, 100_000,
                 {"claude-opus-4-8": 900_000, "claude-sonnet-5": 100_000}, 12.0)] * 30)
    res = mod._estimate_before_after_savings(days=30)
    after = res.get("after_cache_hit")
    assert after is not None
    # input=20M, hit=0.97 -> cr=19.4M; cw=500K; fi=20M*0.03-500K=100K.
    expect = 19_400_000.0 / (19_400_000.0 + 500_000.0 + 100_000.0)
    assert after == pytest.approx(expect, abs=1e-3), \
        f"after_cache_hit={after}, expected {expect:.4f} (cache-write-inclusive)"


# ------------------------------------------------------- 4. net-negative is honest

def test_net_negative_still_reports_the_real_arms(measure):
    """A net-negative transformation must be DISTINGUISHABLE from a genuine zero.

    The user seeing an empty card cannot tell "no savings" from "the math broke".
    On net <= 0 the payload must still carry the real per-session arms and a
    machine-readable state, not all-zero fields.
    """
    mod, tmp = measure
    # A costlier-than-baseline current mix (heavy Fable 5 at $10/MTok) drives net < 0.
    _seed(tmp, [(25_000_000, 0.999, 600_000, 150_000,
                 {"claude-fable-5": 1_000_000}, 12.0)] * 40)
    res = mod._estimate_before_after_savings(days=30)
    assert res.get("reason") == "net_negative", f"expected net_negative, got {res.get('reason')}"
    assert res.get("transformation_state") == "net_negative", \
        "payload must carry a machine-readable transformation_state"
    assert res.get("before_cost_per_session", 0) > 0, \
        "net-negative card zeroed before_cost_per_session; indistinguishable from no data"
    assert res.get("after_cost_per_session", 0) > 0, \
        "net-negative card zeroed after_cost_per_session; indistinguishable from no data"
    assert res.get("savings_per_session") is not None
    assert res.get("savings_per_session") < 0, \
        "a net-negative card must report the negative per-session delta honestly"


def test_positive_transformation_state_is_labelled(measure):
    """The healthy path must carry transformation_state == 'ok' so the two are separable."""
    mod, tmp = measure
    _seed(tmp, [(2_000_000, 0.99, 50_000, 20_000,
                 {"claude-haiku-4-5-20251001": 1_000_000}, 12.0)] * 40)
    res = mod._estimate_before_after_savings(days=30)
    if res.get("monthly_savings_usd", 0) > 0:
        assert res.get("transformation_state") == "ok"


# ------------------------------------------------- 5. the session-weight (volume) pool

def _seed_two_months(tmp, heavy_n, heavy_inp, light_n, light_inp, mu=None,
                     heavy_calls=40, light_calls=10):
    """Anchor month = heavy sessions; current window = many light ones.

    Mirrors the real ledger's shape: tokens-per-session collapses while the session
    COUNT rises, and most of the new sessions fall below the quality gate.
    """
    mu = mu or {"claude-opus-4-8": 1_000_000}
    snap = Path(tmp)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "baseline_state.json").write_text(json.dumps(JUNE_BASELINE))
    db = snap / "trends.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    now = datetime.now()
    anchor_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    rows = []
    for i in range(heavy_n):
        rows.append((anchor_month.strftime("%Y-%m-%d"), heavy_inp, 12.0, heavy_calls))
    for i in range(light_n):
        # duration below the 1.0-minute quality gate: invisible to the legacy pool
        rows.append((now.strftime("%Y-%m-%d"), light_inp, 0.4, light_calls))
    for i, (date, inp, mins, calls_per) in enumerate(rows):
        # api_calls / message_count are the PARTITION-INVARIANT work units the pool
        # measures against. calls_per is set by the caller so a test can hold work
        # constant while session count changes -- the exact confound that made a
        # per-session dollar figure an accounting artifact.
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, cache_create_1h_tokens, "
            "model_usage_json, all_model_usage_json, is_sidechain, duration_minutes, "
            "api_calls, message_count) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (f"/s/{tmp}/{i}.jsonl", date, inp, int(inp * 0.01), 0.97,
             int(inp * 0.02), 0, json.dumps(mu), json.dumps(mu), mins,
             calls_per, calls_per * 3),
        )
    stamp_activity(conn)
    conn.commit()
    conn.close()


def test_session_weight_pool_measures_work_not_sessions(measure):
    """THE fix: the volume lever must be measured per PARTITION-INVARIANT unit of work.

    A per-session dollar gap is an accounting artifact on this ledger: the same work is
    split across 3.4x more, lighter sessions (real numbers: 45,386 API calls in the
    anchor month vs 49,382 now, total tokens 14.75B -> 13.96B, i.e. work is flat), so
    cost per session falls ~72% even when nothing is saved. Here work is held EXACTLY
    constant (400 sessions x 20 calls = 8,000 calls both eras) while the session count
    doubles and per-session tokens halve. A construction that priced the current session
    count at the old per-session cost would invent a large saving; the correct one
    reports ~nothing, because nothing was saved.
    """
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=400, light_inp=10_000_000, light_calls=20)
    res = mod._estimate_before_after_savings(days=30)
    pool = res.get("session_weight_pool")
    assert pool is not None, "session-weight pool did not engage on a two-month ledger"
    assert pool["work_unit"] in ("api_call", "message")
    assert pool["before_units"] == pool["now_units"] == 8000, \
        f"work units not held constant: {pool['before_units']} vs {pool['now_units']}"
    # Per-session cost halved; cost per unit of work did NOT move.
    assert pool["before_cost_per_session"] > 1.8 * pool["after_cost_per_session"], \
        "fixture did not actually halve per-session cost"
    assert pool["before_cost_per_unit"] == pytest.approx(pool["after_cost_per_unit"], rel=1e-6), \
        "cost per unit of work moved even though only session partitioning changed"
    assert abs(pool["transformation_usd"]) < 1.0, \
        (f"claimed ${pool['transformation_usd']:.2f} for pure session repartitioning -- "
         "this is the capacity-as-savings inflation the pool exists to avoid")


def test_session_weight_pool_credits_a_real_per_unit_saving(measure):
    """When cost per unit of work genuinely falls, the pool must credit it."""
    mod, tmp = measure
    # Same 8,000 calls both eras, but each current call carries half the tokens.
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=200, light_inp=10_000_000, light_calls=40)
    res = mod._estimate_before_after_savings(days=30)
    pool = res["session_weight_pool"]
    assert pool["before_units"] == pool["now_units"] == 8000
    assert pool["before_cost_per_unit"] > pool["after_cost_per_unit"], \
        "a real fall in cost per unit of work was not detected"
    assert pool["transformation_usd"] > 0
    assert res["monthly_savings_usd"] > 0
    assert res["transformation_state"] == "ok"


def test_session_weight_pool_counts_below_gate_sessions(measure):
    """Sessions under the quality gate are ~85% of the real current window.

    `_SESSION_QUALITY_FILTER` is `input_tokens >= 1000 AND duration_minutes >= 1.0`.
    Every current-window session in this fixture has duration 0.4 min, so the legacy
    per-session count sees NONE of them. Their work must still be counted.
    """
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=400, light_inp=10_000_000, light_calls=20)
    res = mod._estimate_before_after_savings(days=30)
    assert res["session_weight_pool"]["sessions"] == 400, \
        "below-gate sessions were dropped from the current-window count"
    assert res["session_weight_pool"]["now_units"] == 8000, \
        "below-gate sessions' work units were dropped"


def test_capacity_is_disclosed_as_counts_never_as_dollars(measure):
    """Extra capacity is real upside but is NOT money saved, so no dollar figure may
    be attached to it. The assumption must ride on the payload, not be buried."""
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=400, light_inp=10_000_000, light_calls=20)
    res = mod._estimate_before_after_savings(days=30)
    pool = res["session_weight_pool"]
    assert pool["capacity_assumption"], "capacity assumption missing"
    assert res.get("capacity_neutral_monthly_usd") is None, \
        "a session-basis dollar figure reappeared; that is the inflation this avoids"


def test_price_and_mix_drift_is_excluded_from_the_headline(measure):
    """Generational repricing (Sonnet 4.6 $3 -> Sonnet 5 $2) and mix drift are real
    money off the bill, but Token Optimizer did not cause them. The headline arm must
    price both eras with ONE constant rate card; the engine-priced variant that
    includes the drift is disclosed separately and never claimed."""
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=200, light_inp=20_000_000, light_calls=40,
                     mu={"claude-sonnet-5": 1_000_000})
    res = mod._estimate_before_after_savings(days=30)
    pool = res["session_weight_pool"]
    # Identical volume and identical work both eras -> the constant-rate arm must be ~0
    # even though the recorded mix differs from the flat measuring stick.
    assert abs(pool["transformation_usd"]) < 1.0, \
        f"constant-rate arm moved on price/mix alone: {pool['transformation_usd']}"
    assert "engine_priced_transformation_usd" in pool
    assert "price_mix_drift_usd" in pool


def test_crosscheck_unit_is_reported(measure):
    """The other work unit is computed as a disclosed cross-check, never summed in."""
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=200, light_inp=10_000_000, light_calls=40)
    res = mod._estimate_before_after_savings(days=30)
    pool = res["session_weight_pool"]
    assert pool["crosscheck"] is not None
    assert pool["crosscheck"]["unit"] == "message"
    total = sum(b["monthly_usd"] for b in res["breakdown"])
    assert total == pytest.approx(res["monthly_savings_usd"], abs=0.05), \
        "cross-check leaked into the headline"


def test_single_month_ledger_falls_back_to_the_legacy_path(measure):
    """No usable anchor month -> the pool must return None and change nothing.

    A new install has one partial month; it must not get a fabricated headline.
    """
    mod, tmp = measure
    _seed(tmp, [(4_000_000, 0.97, 80_000, 40_000,
                 {"claude-opus-4-8": 1_000_000}, 12.0)] * 40)
    res = mod._estimate_before_after_savings(days=30)
    assert res.get("session_weight_pool") is None
    assert res.get("capacity_neutral_monthly_usd") is None


def test_breakdown_reconciles_to_the_headline(measure):
    """Every lever must sum to monthly_savings_usd, or the card contradicts itself."""
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=200, light_inp=10_000_000, light_calls=40)
    res = mod._estimate_before_after_savings(days=30)
    total = sum(b["monthly_usd"] for b in res["breakdown"])
    assert total == pytest.approx(res["monthly_savings_usd"], abs=0.05), \
        f"breakdown sums to {total}, headline is {res['monthly_savings_usd']}"


def test_breakdown_reconciles_when_estimated_pools_are_non_zero(measure):
    """Regression: the waterfall must decompose the headline EXACTLY, even when the
    estimated-volume pools have real dollars in them.

    Those pools are superseded by the session-weight pool (their avoided reads already
    show up as fewer billed tokens per API call). The scalar add-back was zeroed but the
    per-pool dicts still fed the waterfall, so on the real ledger the breakdown summed to
    $2,147.86 under a $1,975.06 headline -- the card contradicting itself, which is the
    class of bug this whole task exists to remove.
    """
    mod, tmp = measure
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=200, light_inp=10_000_000, light_calls=40)
    # Force every estimated pool to carry real dollars.
    res = mod._estimate_before_after_savings(days=30, estimated_pools={
        "uncaptured_runtime": {"cost_saved_usd": 120.0},
        "behavioral_loops": {"cost_saved_usd": 45.0},
        "hint_followed": {"cost_saved_usd": 30.0},
        "handover_rerun": {"cost_saved_usd": 15.0},
        "retrieval_serve": {"cost_saved_usd": 60.0},
    })
    assert res["session_weight_pool"] is not None
    total = sum(b["monthly_usd"] for b in res["breakdown"])
    assert total == pytest.approx(res["monthly_savings_usd"], abs=0.05), \
        f"breakdown sums to {total}, headline is {res['monthly_savings_usd']}"
    for pool in (res.get("estimated_volume_pools") or {}).values():
        assert pool["transformation_usd"] == 0.0
        assert pool.get("superseded_by") == "session_weight"
    assert res["estimated_volume_transformation_usd"] == 0.0


def test_net_negative_path_supersedes_estimated_pools(measure):
    """Regression: when the weight pool is active and the net is negative (the
    card returns reason='net_negative'), the estimated_volume_pools must STILL
    be superseded -- their transformation_usd zeroed and superseded_by set.

    Before the fix, the supersession zeroing happened AFTER the net-negative
    early return, so the payload leaked unsuperseded pools with nonzero
    transformation_usd values. A consumer summing the nested pools would
    double-count against the zeroed scalar.
    """
    mod, tmp = measure
    # Seed sessions where the current window is MORE expensive per call than
    # the anchor, producing a net-negative transformation.
    _seed_two_months(tmp, heavy_n=200, heavy_inp=10_000_000, heavy_calls=40,
                     light_n=200, light_inp=20_000_000, light_calls=40)
    res = mod._estimate_before_after_savings(days=30, estimated_pools={
        "uncaptured_runtime": {"cost_saved_usd": 120.0},
        "behavioral_loops": {"cost_saved_usd": 45.0},
    })
    # The card should be net-negative (headline gated to 0).
    assert res["monthly_savings_usd"] == 0.0
    assert res.get("reason") == "net_negative"
    # The estimated_volume_pools must STILL be superseded even in this path.
    pools = res.get("estimated_volume_pools") or {}
    assert pools, "estimated_volume_pools should be present"
    for pool_name, pool in pools.items():
        assert pool["transformation_usd"] == 0.0, (
            f"pool {pool_name} leaked nonzero transformation_usd "
            f"({pool['transformation_usd']}) in net-negative path"
        )
        assert pool.get("superseded_by") == "session_weight", (
            f"pool {pool_name} missing superseded_by in net-negative path"
        )


# ------------------------------------------------------------------ 8. spend cap


def test_saving_capped_at_actual_spend_and_disclosed(measure):
    """The reported saving can never exceed the user's actual spend for the
    period. A counterfactual saving larger than the real bill is indefensible
    to a paying customer, no matter how correct the arithmetic is.

    This constructs the cheap-month-expensive-anchor case: the anchor month
    has very high tokens-per-call (expensive), the current month has very low
    tokens-per-call (cheap). The uncapped saving exceeds the actual current
    spend. The cap must bind and the card must disclose it.
    """
    mod, tmp = measure
    # Anchor month: 200 sessions, 50M tokens each, 40 calls each.
    # At $5/MTok flat rate, that's ~$50/call -> $2,000/session.
    # Current month: 200 sessions, 1M tokens each, 40 calls each.
    # At $5/MTok flat rate, that's ~$1/call -> $40/session.
    # Uncapped saving = ($50 - $1) * 8,000 calls = $392,000.
    # Actual spend = $1 * 8,000 = $8,000.
    # The uncapped saving ($392K) vastly exceeds actual spend ($8K).
    _seed_two_months(tmp, heavy_n=200, heavy_inp=50_000_000, heavy_calls=40,
                     light_n=200, light_inp=1_000_000, light_calls=40)
    res = mod._estimate_before_after_savings(days=30)

    # The card must have a positive headline (the transformation is real).
    assert res["monthly_savings_usd"] > 0, "headline should be positive"

    # The uncapped figure must exceed actual spend.
    uncapped = res.get("uncapped_monthly_savings_usd", 0.0)
    actual = res.get("actual_monthly_usd", 0.0)
    assert uncapped > actual, (
        f"uncapped saving (${uncapped:,.2f}) should exceed actual spend "
        f"(${actual:,.2f}) in this scenario"
    )

    # The cap must bind: the reported saving must not exceed actual spend.
    assert res["monthly_savings_usd"] <= actual + 0.01, (
        f"capped saving (${res['monthly_savings_usd']:,.2f}) must not exceed "
        f"actual spend (${actual:,.2f})"
    )

    # The cap must be disclosed, not silently clamped.
    assert res.get("savings_capped") is True, (
        "savings_capped must be True when the cap binds"
    )
    note = res.get("savings_cap_note") or ""
    assert note, "savings_cap_note must be non-empty when the cap binds"
    assert "conservatively" in note or "capped" in note.lower(), (
        f"savings_cap_note must explain the cap to the user, got: {note}"
    )
    # The uncapped figure must be in the note so the user can see what was capped.
    assert str(int(uncapped)) in note or f"{uncapped:,.2f}" in note, (
        f"savings_cap_note should mention the uncapped figure, got: {note}"
    )


def test_cap_does_not_bind_when_saving_is_below_spend(measure):
    """When the saving is legitimately below actual spend, the cap must NOT
    bind and no disclosure should appear. This prevents false alarms on the
    normal case (the real ledger: $1,975 saving on $11,187 spend = 0.18x)."""
    mod, tmp = measure
    # Anchor: 200 sessions at 20M tokens, 40 calls each.
    # Current: 200 sessions at 10M tokens, 40 calls each.
    # This produces a modest saving well within actual spend.
    _seed_two_months(tmp, heavy_n=200, heavy_inp=20_000_000, heavy_calls=40,
                     light_n=200, light_inp=10_000_000, light_calls=40)
    res = mod._estimate_before_after_savings(days=30)

    if res["monthly_savings_usd"] > 0:
        assert res.get("savings_capped") is False, (
            "cap must not bind when saving is below actual spend"
        )
        assert res.get("savings_cap_note") is None, (
            "no cap note should appear when the cap does not bind"
        )
        assert res["monthly_savings_usd"] == res.get("uncapped_monthly_savings_usd"), (
            "capped and uncapped should be equal when cap does not bind"
        )
