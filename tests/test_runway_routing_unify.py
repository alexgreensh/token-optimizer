"""The plan card's routing factor is the model-routing recipe, not a second model.

runway_snapshot's routing lever used to price its own input-rate ratio: bare
family input cards, premium models in the current burn, unknown models at the
Sonnet card, no snapshot baseline and no pending gate. It now reads
_compute_model_routing_savings (docs/METHODOLOGY.md section 3.2): the factor is
the standardized baseline cost over the standardized actual cost of the same
work. The card states pending/unavailable routing instead of using 1.0, and
never calls a factor below 1 a lighter mix.
"""
import importlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
ASSETS = ROOT / "skills" / "token-optimizer" / "assets"
SID = "11111111-2222-3333-4444-555555555555"
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
FABLE = "claude-fable-5"
UNKNOWN = "mystery-model-x"


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    mod._init_trends_db().close()
    monkeypatch.setattr(mod, "_keepwarm_read_meters", lambda now=None: {
        "available": True, "stale": False, "age_s": 60, "ts": time.time(),
        "five_hour_pct": 20.0, "seven_day_pct": 40.0})
    monkeypatch.setattr(mod, "_dashboard_spent_token_basis",
                        lambda conn, days=30: {"tokens": 1_000_000, "basis": "test", "complete": True})
    monkeypatch.setattr(mod, "_get_merged_savings", lambda days=30, since=None: {
        "total_cost_usd": 0.0, "model_routing": {"realized_cost_usd": 0.0}})
    monkeypatch.setattr(mod, "_counted_window_summary", lambda conn, s, e: {"available": False})
    monkeypatch.setattr(mod, "_weekly_full_savings", lambda **kw: None)
    monkeypatch.setattr(mod, "_routing_pending_sessions", lambda days=30, since=None: 0)
    monkeypatch.setattr(mod, "_earliest_model_mix", lambda lookback_days=14: None)
    # A context lever of x1.10 keeps the card on screen whatever routing says.
    conn = sqlite3.connect(str(mod.TRENDS_DB))
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,session_id,session_uuid)"
                 " VALUES(?,?,?,?,?)",
                 ((datetime.now() - timedelta(minutes=5)).isoformat(), "tool_archive", 100_000, SID, SID))
    conn.commit()
    conn.close()
    yield mod
    sys.modules.pop("measure", None)


def _workload(m, monkeypatch, usage, baseline):
    total = sum(sum(c.values()) for c in usage.values())
    shares = {k: sum(c.values()) / total for k, c in usage.items()}
    monkeypatch.setattr(m, "_model_mix_shares", lambda days=14, since=None: {
        "shares": dict(shares), "total_tokens": total, "days": days})
    monkeypatch.setattr(m, "_routing_usage_by_model",
                        lambda days=30, since=None: {k: dict(v) for k, v in usage.items()})
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: dict(baseline) if baseline else None)


def test_premium_only_shift_is_not_a_heavier_mix(m, monkeypatch):
    _workload(m, monkeypatch, {OPUS: {"fresh_input": 5_000_000}, FABLE: {"fresh_input": 5_000_000}},
              {"opus": 1.0})
    rw = m.runway_snapshot(days=30)
    assert rw["routing_multiplier"] == pytest.approx(1.0)
    assert rw["routing_direction"] == "unchanged"
    assert "fable" in rw["routing_excluded_models"]["premium"]


def test_unknown_model_is_not_priced_as_sonnet(m, monkeypatch):
    _workload(m, monkeypatch, {OPUS: {"fresh_input": 5_000_000}, UNKNOWN: {"fresh_input": 5_000_000}},
              {"opus": 1.0})
    rw = m.runway_snapshot(days=30)
    # Priced at the Sonnet card the unknown half read as a lighter mix (x1.25+).
    assert rw["routing_multiplier"] == pytest.approx(1.0)
    assert UNKNOWN in rw["routing_excluded_models"]["unpriced"]
    assert rw["routing_coverage"] == pytest.approx(0.5, abs=0.01)


def test_pending_routing_is_an_explicit_state_not_one(m, monkeypatch):
    _workload(m, monkeypatch, {OPUS: {"fresh_input": 1_000_000}, SONNET: {"fresh_input": 9_000_000}},
              {"opus": 1.0})
    monkeypatch.setattr(m, "_routing_pending_sessions", lambda days=30, since=None: 4)
    rw = m.runway_snapshot(days=30)
    assert rw is not None
    assert rw["routing_multiplier"] is None
    assert rw["routing_state"] == "pending"
    assert rw["routing_pending_sessions"] == 4
    # The headline carries only the measured context lever while routing waits.
    assert rw["extra_work_pct"] == pytest.approx((rw["context_multiplier"] - 1) * 100, abs=0.1)


def test_unavailable_routing_is_stated(m, monkeypatch):
    _workload(m, monkeypatch, {OPUS: {"fresh_input": 1_000_000}}, None)
    rw = m.runway_snapshot(days=30)
    assert rw is not None
    assert rw["routing_multiplier"] is None
    assert rw["routing_state"] == "unavailable"


def test_snapshot_baseline_is_used_like_the_recipe(m, monkeypatch):
    _workload(m, monkeypatch, {OPUS: {"fresh_input": 2_000_000}, SONNET: {"fresh_input": 8_000_000}}, None)
    monkeypatch.setattr(m, "_earliest_model_mix",
                        lambda lookback_days=14: {"shares": {"sonnet": 1.0}, "first_date": "2026-01-01"})
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (m.SNAPSHOT_DIR / "snapshot_before.json").write_text(
        json.dumps({"model_mix": {"shares": {"opus": 1.0}}}), encoding="utf-8")
    rw = m.runway_snapshot(days=30)
    assert rw["routing_baseline_source"] == "snapshot"
    assert rw["routing_multiplier"] > 1.0
    assert rw["routing_direction"] == "lighter"


def test_card_factor_reconciles_with_the_routing_recipe(m, monkeypatch):
    usage = {
        OPUS: {"fresh_input": 1_000_000, "cache_read": 30_000_000, "cache_create_5m": 2_000_000,
               "output": 500_000},
        SONNET: {"fresh_input": 2_000_000, "cache_read": 40_000_000, "cache_create_1h": 1_000_000,
                 "output": 900_000},
    }
    _workload(m, monkeypatch, usage, {"opus": 0.9, "sonnet": 0.1})
    recipe = m._compute_model_routing_savings(days=30)
    rw = m.runway_snapshot(days=30)
    expected = recipe["standardized_baseline_cost_usd"] / recipe["standardized_actual_cost_usd"]
    assert rw["routing_multiplier"] == pytest.approx(expected, abs=0.001)
    assert rw["routing_basis"] == recipe["realized_basis"]
    assert rw["routing_baseline_source"] == recipe["baseline_source"]
    assert rw["routing_standardized_baseline_cost_usd"] == recipe["standardized_baseline_cost_usd"]
    assert rw["routing_standardized_actual_cost_usd"] == recipe["standardized_actual_cost_usd"]
    assert rw["multiplier"] == pytest.approx(rw["context_multiplier"] * rw["routing_multiplier"], abs=0.01)


def test_heavier_mix_reports_heavier_direction(m, monkeypatch):
    _workload(m, monkeypatch, {OPUS: {"fresh_input": 9_000_000}, SONNET: {"fresh_input": 1_000_000}},
              {"opus": 0.5, "sonnet": 0.5})
    # A net factor below 1.02 hides the card unless weekly savings keep it up.
    monkeypatch.setattr(m, "_weekly_full_savings", lambda **kw: {
        "saved_usd": 20.0, "start": datetime.now().isoformat()})
    rw = m.runway_snapshot(days=30)
    assert rw["routing_multiplier"] < 1.0
    assert rw["routing_direction"] == "heavier"


@pytest.mark.parametrize("split_tokens,split_class", [
    (0, "output"), (1_000_000, "output"), (1_000_000, "cache_read")])
def test_unsplit_usage_is_outside_the_shared_comparison(m, monkeypatch, split_tokens, split_class):
    usage = {SONNET: {"unsplit": 1_000_000}}
    if split_tokens:
        usage[OPUS] = {split_class: split_tokens}
    _workload(m, monkeypatch, usage, {"opus": 1.0})
    recipe = m._compute_model_routing_savings(days=30)
    rw = m.runway_snapshot(days=30)
    coverage = split_tokens / (split_tokens + 1_000_000)
    assert recipe["compared_share"] == coverage
    assert recipe["unsplit_tokens"] == rw["routing_unsplit_tokens"] == 1_000_000
    assert rw["routing_coverage"] == coverage
    assert recipe["realized_cost_usd"] == 0
    if split_tokens:
        # Only split Opus work remains, so the mix is unchanged from the baseline.
        assert recipe["evidence"] == rw["routing_state"] == "estimated"
        assert rw["routing_multiplier"] == 1.0
        assert recipe["standardized_baseline_cost_usd"] == recipe["standardized_actual_cost_usd"] > 0
    else:
        assert recipe["evidence"] == rw["routing_state"] == "unavailable"
        assert rw["routing_multiplier"] is None
        assert recipe["standardized_baseline_cost_usd"] == recipe["standardized_actual_cost_usd"] == 0
    text = _card_text(rw).lower()
    assert "without a token-class split" in text
    assert "100% of this window" not in text


@pytest.mark.parametrize("context_factor,routing_factor", [(1.01, 1.0096), (1.0106, 1.0094)])
def test_visibility_gate_uses_unrounded_routing(m, monkeypatch, context_factor, routing_factor):
    monkeypatch.setattr(m, "_runway_saved_tokens", lambda *a, **kw: (context_factor - 1) * 1_000_000)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda **kw: {
        "state": "estimated", "factor": routing_factor})
    rw = m.runway_snapshot(days=30)
    if context_factor * routing_factor < 1.02:
        assert rw is None
    else:
        assert rw is not None
        assert rw["routing_multiplier"] == round(routing_factor, 3)


@pytest.mark.parametrize("saved_tokens", [10_000, 10_400])
def test_headline_and_bars_use_unrounded_routing(m, monkeypatch, saved_tokens):
    monkeypatch.setattr(m, "_runway_saved_tokens", lambda *a, **kw: saved_tokens)
    monkeypatch.setattr(m, "_runway_routing_lever", lambda **kw: {
        "state": "estimated", "factor": 1.0096})
    monkeypatch.setattr(m, "_weekly_full_savings", lambda **kw: {
        "saved_usd": 20.0, "start": datetime.now().isoformat()})
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **kw: {
        "available": True, "stale": False, "seven_day_pct": 83.0})
    rw = m.runway_snapshot(days=30)
    product = (1 + saved_tokens / 1_000_000) * 1.0096
    assert rw["routing_multiplier"] == 1.01
    assert rw["multiplier"] == round(product, 2)
    assert rw["extra_work_pct"] == round((product - 1) * 100, 1)
    window = rw["windows"][0]
    assert window["without_used_pct"] == round(83 * product, 1)
    assert window["without_headroom_pct"] == round(100 - 83 * product, 1)
    assert window["headroom_multiple"] == round(17 / (100 - 83 * product), 1)


# ------------------------------------------------------------------ render

def _card_text(rw):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute the dashboard renderer")
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("  function runwayCardHtml() {")
    end = html.index("  function renderSavings() {")
    script = (
        "const data = {runway: JSON.parse(process.argv[2]), runtime_label: 'Claude Code'};\n"
        "function esc(s) { return s == null ? '' : String(s); }\n"
        "function fn(n) { return String(n); }\n"
        + html[start:end] + "\nprocess.stdout.write(runwayCardHtml());\n")
    out = subprocess.run([node, "-", json.dumps(rw)], input=script, capture_output=True,
                         text=True, encoding="utf-8", check=True, timeout=30).stdout
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", out))


def _rw(**over):
    rw = {"windows": [], "extra_work_pct": 3.0, "multiplier": 1.03, "context_multiplier": 1.08,
          "routing_multiplier": 0.954, "routing_state": "estimated", "routing_direction": "heavier",
          "proxy": "p", "tokens_consumed": 1_000_000, "tokens_saved": 80_000}
    rw.update(over)
    return rw


def test_factor_below_one_never_renders_lighter():
    text = _card_text(_rw())
    assert "lighter model" not in text.lower()
    assert "heavier model mix" in text.lower()


def test_factor_below_one_with_stale_direction_still_not_lighter():
    text = _card_text(_rw(routing_direction="lighter"))
    assert "lighter model" not in text.lower()


def test_factor_above_one_renders_lighter():
    text = _card_text(_rw(routing_multiplier=1.04, routing_direction="lighter", extra_work_pct=12.3))
    assert "lighter model mix" in text.lower()


def test_lighter_mix_verb_agrees_with_its_subject():
    # Context unchanged: the mix alone is the subject, so the verb is "lets".
    text = _card_text(_rw(context_multiplier=1.0, routing_multiplier=1.04,
                          routing_direction="lighter")).lower()
    assert "lets each token do more" in text
    assert "let each token do more" not in text
    # Lighter context AND a lighter mix: a compound subject takes "let".
    text = _card_text(_rw(context_multiplier=1.08, routing_multiplier=1.04,
                          routing_direction="lighter")).lower()
    assert "let each token do more" in text


def test_heavier_mix_names_the_plan_cost():
    text = _card_text(_rw()).lower()
    assert "means each token uses more of your plan" in text
    assert "uses more of each token" not in text


def test_routing_coverage_names_the_30_day_window():
    text = _card_text(_rw(routing_coverage=0.5)).lower()
    assert "compares 50% of the last 30 days" in text
    assert "of this window" not in text


def test_pending_routing_renders_pending_not_a_number():
    text = _card_text(_rw(routing_multiplier=None, routing_state="pending", routing_direction=None,
                          routing_pending_sessions=4, extra_work_pct=8.0))
    assert "pending" in text.lower() and "4 sessions" in text
    assert "null" not in text and "NaN" not in text and "undefined" not in text


@pytest.mark.parametrize("factor,coverage,state", [(None, 0.0, "unavailable"), (1.0, 0.5, "estimated")])
@pytest.mark.parametrize("cost_efficiency", [None, 1.2])
def test_unsplit_exclusion_is_disclosed_even_without_a_factor(factor, coverage, state, cost_efficiency):
    text = _card_text(_rw(routing_multiplier=factor, routing_state=state,
                          routing_coverage=coverage, routing_unsplit_tokens=1_000_000,
                          cost_efficiency_multiplier=cost_efficiency)).lower()
    assert "1000000 tokens without a token-class split" in text
    assert "left out" in text
    if factor is None and cost_efficiency is None:
        assert "routing unavailable" in text
    elif factor is not None:
        assert "compares 50%" in text
