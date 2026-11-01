"""Cross-check verdict: the two work-unit methods must actually agree before
the transformation is headlined.

The session-weight pool measures the transformation through two independent
units of work (API calls and messages). The CLI used to claim the cross-check
"agrees within ~10%" without ever comparing the figures, so a disagreeing
pair could not stop the hero from rendering.

The verdict is now computed once on the pool (crosscheck.agrees /
crosscheck.gap_pct) and every surface reads the same answer. When the two
methods disagree by more than 10% or on sign, the transformation is not
headlined: the payload returns reason="methods_disagree" with
monthly_savings_usd = 0 and both disputed figures preserved on
session_weight_pool; the CLI prints a plain disagreement line; the dashboard
renders a neutral card like net_negative; the weekly full-workload figure
falls back to measured savings.

Run: python3 -m pytest tests/test_crosscheck_verdict.py -v
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_transformation_card_fix import JUNE_BASELINE, _SCHEMA, measure


def _seed_divergent_units(tmp, heavy_n=200, heavy_inp=20_000_000,
                        light_n=200, light_inp=10_000_000,
                        heavy_calls=40, heavy_msgs=120,
                        light_calls=40, light_msgs=600):
    """Two months where the calls-per-message ratio differs between eras.

    Heavy (anchor) month: `heavy_n` sessions x `heavy_calls` calls x `heavy_msgs`
    messages. Light (current) window: `light_n` sessions x `light_calls` calls x
    `light_msgs` messages. Holding calls constant while messages grow 5x makes
    the per-API-call and per-message transformations land far apart: the
    disagreement the verdict exists to catch.
    """
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
    mu = {"claude-opus-4-8": 1_000_000}
    rows = []
    for _ in range(heavy_n):
        rows.append((anchor_month.strftime("%Y-%m-%d"), heavy_inp, 12.0,
                     heavy_calls, heavy_msgs))
    for _ in range(light_n):
        rows.append((now.strftime("%Y-%m-%d"), light_inp, 12.0,
                     light_calls, light_msgs))
    for i, (date, inp, mins, calls, msgs) in enumerate(rows):
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, cache_create_1h_tokens, "
            "model_usage_json, all_model_usage_json, is_sidechain, duration_minutes, "
            "api_calls, message_count) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (f"/s/{tmp}/{i}.jsonl", date, inp, int(inp * 0.01), 0.97,
             int(inp * 0.02), 0, json.dumps(mu), json.dumps(mu), mins,
             calls, msgs),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------- 1. the helper

def test_agrees_within_ten_percent(measure):
    mod, _ = measure
    assert mod._crosscheck_agrees(100.0, 105.0) == (True, 4.8)
    assert mod._crosscheck_agrees(2000.0, 2000.0) == (True, 0.0)
    # Boundary: |a-b| == 10% of max(|a|,|b|) still agrees.
    assert mod._crosscheck_agrees(100.0, 111.11)[0] is True
    # Two negative figures close together agree too.
    assert mod._crosscheck_agrees(-100.0, -105.0)[0] is True


def test_disagrees_on_size(measure):
    mod, _ = measure
    agrees, gap = mod._crosscheck_agrees(100.0, 500.0)
    assert agrees is False
    assert gap == pytest.approx(80.0)


def test_disagrees_on_sign(measure):
    mod, _ = measure
    agrees, gap = mod._crosscheck_agrees(1000.0, -9000.0)
    assert agrees is False
    assert gap == pytest.approx(111.1)
    assert mod._crosscheck_agrees(-50.0, 50.0)[0] is False
    assert mod._crosscheck_agrees(0.0, 500.0)[0] is False


def test_zero_and_missing_inputs(measure):
    mod, _ = measure
    assert mod._crosscheck_agrees(0.0, 0.0) == (True, 0.0)
    assert mod._crosscheck_agrees(0.0, -0.0) == (True, 0.0)
    assert mod._crosscheck_agrees(None, 5.0) == (False, None)
    assert mod._crosscheck_agrees(5.0, None) == (False, None)
    assert mod._crosscheck_agrees(float("nan"), 5.0) == (False, None)
    assert mod._crosscheck_agrees(float("inf"), 5.0) == (False, None)
    assert mod._crosscheck_agrees("12.5", 12.0) == (True, 4.0)


def test_verdict_recomputes_ignoring_stored_flag(measure):
    """A stored `agrees` flag is payload for JSON consumers only. Decision
    paths must recompute from the raw figures: a stale or hand-edited flag
    can lie in either direction."""
    mod, _ = measure
    assert mod._crosscheck_verdict(None) is True
    assert mod._crosscheck_verdict({}) is True
    # Stale TRUE flag over disagreeing numbers -> disagree.
    assert mod._crosscheck_verdict(
        {"transformation_usd": 1000.0,
         "crosscheck": {"transformation_usd": -9000.0,
                        "agrees": True}}) is False
    # Stale FALSE flag over agreeing numbers -> agree.
    assert mod._crosscheck_verdict(
        {"transformation_usd": 100.0,
         "crosscheck": {"transformation_usd": 105.0,
                        "agrees": False}}) is True
    # Missing primary figure cannot agree.
    assert mod._crosscheck_verdict(
        {"crosscheck": {"transformation_usd": 5.0,
                        "agrees": True}}) is False


# ------------------------------------------- 2. verdict stored on the payload

def test_pool_payload_carries_the_verdict(measure):
    """crosscheck.agrees / crosscheck.gap_pct must ride on every payload so all
    surfaces read the same answer instead of comparing (or not) themselves."""
    mod, tmp = measure
    _seed_divergent_units(tmp, light_msgs=120)  # proportional: agrees
    res = mod._estimate_before_after_savings(days=30)
    cross = (res.get("session_weight_pool") or {}).get("crosscheck")
    assert cross is not None
    assert cross["agrees"] is True
    assert isinstance(cross["gap_pct"], float)
    assert cross["gap_pct"] <= 10.0


# ------------------------------------------------ 3. methods_disagree state

def test_divergent_units_suppress_the_headline(measure):
    """The disagreeing pair demotes the transformation: no hero, both figures
    stay available for display."""
    mod, tmp = measure
    _seed_divergent_units(tmp)  # 5x messages per call in the current era
    res = mod._estimate_before_after_savings(days=30)
    pool = res.get("session_weight_pool")
    assert pool is not None, "session-weight pool did not engage"
    assert pool["crosscheck"]["agrees"] is False
    assert res.get("reason") == "methods_disagree"
    assert res.get("transformation_state") == "methods_disagree"
    # monthly_savings_usd must not drive a hero number.
    assert res.get("monthly_savings_usd") == 0.0
    # Both disputed figures remain available for display.
    assert pool["transformation_usd"] > 0
    assert pool["crosscheck"]["transformation_usd"] != pytest.approx(
        pool["transformation_usd"], rel=0.1)
    # The suppressed positive delta is still readable, not zeroed away.
    assert res.get("main_transformation_usd", 0) > 0
    assert res.get("actual_monthly_usd", 0) > 0


def test_net_negative_takes_precedence_over_disagreement(measure):
    """A period with no net saving reports net_negative, not methods_disagree:
    the flat/negative result is the primary truth."""
    mod, tmp = measure
    # Current era costlier per call AND per message (both negative, disagreeing
    # in size) -> net < 0 must still win.
    _seed_divergent_units(tmp, heavy_inp=10_000_000, light_inp=20_000_000,
                        light_msgs=600)
    res = mod._estimate_before_after_savings(days=30)
    assert res.get("monthly_savings_usd") == 0.0
    assert res.get("reason") == "net_negative", \
        f"expected net_negative, got {res.get('reason')}"


def test_methods_disagree_keeps_estimated_pools_superseded(measure):
    """Parity with the net_negative early return: the estimated-volume pools
    must still be superseded (zeroed + labelled) on the disagree path."""
    mod, tmp = measure
    _seed_divergent_units(tmp)
    res = mod._estimate_before_after_savings(days=30, estimated_pools={
        "uncaptured_runtime": {"cost_saved_usd": 120.0},
        "behavioral_loops": {"cost_saved_usd": 45.0},
    })
    assert res.get("reason") == "methods_disagree"
    for name, pool in (res.get("estimated_volume_pools") or {}).items():
        assert pool["transformation_usd"] == 0.0, \
            f"pool {name} leaked a nonzero transformation on the disagree path"
        assert pool.get("superseded_by") == "session_weight"


def test_agreeing_units_still_headline(measure):
    """The agree path is unchanged: proportional units -> ok headline with the
    verdict stored for display."""
    mod, tmp = measure
    _seed_divergent_units(tmp, light_msgs=120)
    res = mod._estimate_before_after_savings(days=30)
    pool = res.get("session_weight_pool")
    assert pool is not None
    assert pool["crosscheck"]["agrees"] is True
    assert res.get("reason") != "methods_disagree"
    assert res.get("monthly_savings_usd") > 0
    assert res.get("transformation_state") == "ok"


# ----------------------------------------- 4. weekly full-workload fallback

def test_weekly_full_value_falls_back_when_methods_disagree(measure, monkeypatch):
    """The weekly card reads the same pool: a disagreeing cross-check drops the
    whole-workload figure so the card falls back to measured savings."""
    mod, _ = measure
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    now = datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()
    pool = dict(transformation_usd=1000.0, actual_usd=30000.0,
                counterfactual_usd=31000.0, now_units=10,
                anchor_month="2026-06",
                crosscheck={"unit": "message",
                            "transformation_usd": -9000.0,
                            "agrees": False, "gap_pct": 1000.0})
    monkeypatch.setattr(mod, "_session_weight_pool_savings",
                        lambda *a, **kw: pool)
    assert mod._weekly_full_savings(resets_at=now + 3 * 86400, now=now) is None


def test_weekly_full_value_passes_when_methods_agree(measure, monkeypatch):
    mod, _ = measure
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    now = datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()
    pool = dict(transformation_usd=50.0, actual_usd=100.0,
                counterfactual_usd=150.0, now_units=10,
                anchor_month="2026-06",
                crosscheck={"unit": "message", "transformation_usd": 52.0,
                            "agrees": True, "gap_pct": 3.8})
    monkeypatch.setattr(mod, "_session_weight_pool_savings",
                        lambda *a, **kw: pool)
    value = mod._weekly_full_savings(resets_at=now + 3 * 86400, now=now)
    assert value is not None
    assert value["saved_usd"] == pytest.approx(50.0)


# ------------------------------------------------------- 5. CLI rendering

def _cli_summary(before_after):
    return {
        "total_tokens": 120_000,
        "total_cost_usd": 12.34,
        "total_events": 5,
        "by_category": {},
        "daily_avg_usd": 0.41,
        "period_days": 30,
        "before_after": before_after,
    }


def test_cli_prints_the_real_crosscheck_figure_when_agreeing(
        measure, monkeypatch, capsys):
    """Agree path: the line stays, prints the actual cross-check figure on its
    real unit, and only claims agreement because the verdict says so."""
    mod, _ = measure
    monkeypatch.setattr(mod, "_install_date", lambda: "2026-01-01")
    ba = {
        "monthly_savings_usd": 1200.0,
        "actual_monthly_usd": 30000.0,
        "counterfactual_monthly_usd": 31200.0,
        "session_weight_pool": {
            "anchor_month": "2026-06", "work_unit": "api_call",
            "now_units": 150000, "before_cost_per_unit": 0.22,
            "after_cost_per_unit": 0.21,
            "transformation_usd": 1200.0,
            "price_mix_drift_usd": 0.0,
            "crosscheck": {"unit": "message",
                           "transformation_usd": 1150.0,
                           "agrees": True, "gap_pct": 4.2},
        },
        "breakdown": [], "breakdown_caveat": "",
    }
    monkeypatch.setattr(mod, "_get_merged_savings",
                        lambda days=30: _cli_summary(ba))
    mod.savings_report(days=30)
    out = capsys.readouterr().out
    assert "YOUR TRANSFORMATION: ~$1,200/mo" in out
    assert "Cross-check on messages: $1,150/mo" in out
    assert "agrees" in out
    assert "does not agree" not in out


def test_cli_never_claims_agreement_without_verdict(measure, monkeypatch,
                                                    capsys):
    """A disagreeing cross-check must never print 'agrees within ~10%'."""
    mod, _ = measure
    monkeypatch.setattr(mod, "_install_date", lambda: "2026-01-01")
    ba = {
        "monthly_savings_usd": 1200.0,
        "actual_monthly_usd": 30000.0,
        "counterfactual_monthly_usd": 31200.0,
        "session_weight_pool": {
            "anchor_month": "2026-06", "work_unit": "api_call",
            "now_units": 150000, "before_cost_per_unit": 0.22,
            "after_cost_per_unit": 0.21,
            "transformation_usd": 1200.0,
            "price_mix_drift_usd": 0.0,
            "crosscheck": {"unit": "message",
                           "transformation_usd": -8400.0,
                           "agrees": False, "gap_pct": 114.3},
        },
        "breakdown": [], "breakdown_caveat": "",
    }
    monkeypatch.setattr(mod, "_get_merged_savings",
                        lambda days=30: _cli_summary(ba))
    mod.savings_report(days=30)
    out = capsys.readouterr().out
    assert "agrees within ~10%" not in out
    assert "does not agree" in out


def test_cli_methods_disagree_branch_shows_both_figures(
        measure, monkeypatch, capsys):
    mod, _ = measure
    monkeypatch.setattr(mod, "_install_date", lambda: "2026-01-01")
    ba = {
        "monthly_savings_usd": 0.0,
        "reason": "methods_disagree",
        "transformation_state": "methods_disagree",
        "session_weight_pool": {
            "work_unit": "api_call", "transformation_usd": 1200.0,
            "crosscheck": {"unit": "message",
                           "transformation_usd": -8400.0,
                           "agrees": False, "gap_pct": 114.3},
        },
    }
    monkeypatch.setattr(mod, "_get_merged_savings",
                        lambda days=30: _cli_summary(ba))
    mod.savings_report(days=30)
    out = capsys.readouterr().out
    assert "not headlined this period" in out
    assert "$1,200" in out
    assert "-$8,400" in out
    assert "~$1,200/mo" not in out  # no hero claim
    assert "agrees within ~10%" not in out


# -------------------------------------------------- 6. dashboard rendering

def test_dashboard_methods_disagree_suppresses_the_hero():
    """The dashboard must render a neutral card stating both figures instead of
    the savings-amount hero when reason='methods_disagree'."""
    from test_savings_render import fixture, render
    s = fixture()
    s["before_after"] = {
        "monthly_savings_usd": 0,
        "reason": "methods_disagree",
        "transformation_state": "methods_disagree",
        "session_weight_pool": {
            "work_unit": "api_call", "transformation_usd": 1200.0,
            "crosscheck": {"unit": "message",
                           "transformation_usd": -8400.0,
                           "agrees": False, "gap_pct": 114.3},
        },
    }
    html = render(s)
    assert "savings-transformation" not in html, \
        "the transformation hero must not render on methods_disagree"
    assert "Two estimates disagree" in html
    assert "$1,200" in html
    assert "-$8,400" in html


def test_dashboard_agreeing_payload_still_shows_the_hero():
    from test_savings_render import fixture, render
    s = fixture()
    s["before_after"]["session_weight_pool"]["crosscheck"] = {
        "unit": "message", "transformation_usd": 1950.0,
        "agrees": True, "gap_pct": 2.5}
    html = render(s)
    assert "savings-transformation" in html
    assert "$2,000" in html


def test_flat_band_reads_near_zero_methods_as_no_change():
    import importlib, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"))
    m = importlib.import_module("measure")
    pool = {"transformation_usd": 54.73, "crosscheck": {"transformation_usd": -704.65}}
    assert m._crosscheck_flat(pool, 27_820.04) is True
    big = {"transformation_usd": 1_500.0, "crosscheck": {"transformation_usd": 1_400.0}}
    assert m._crosscheck_flat(big, 27_820.04) is False
    split = {"transformation_usd": 54.73, "crosscheck": {"transformation_usd": -3_000.0}}
    assert m._crosscheck_flat(split, 27_820.04) is False
    assert m._crosscheck_flat(pool, 0) is False
