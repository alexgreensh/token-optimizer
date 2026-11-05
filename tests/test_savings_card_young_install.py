#!/usr/bin/env python3
"""Savings-card honesty for young installs + since-install reconciliation.

Three defects under test:

1. Young install (<30 tracked days): the savings surfaces used to stamp "/mo"
   on a cumulative partial sample (4 days of value labeled as a monthly
   run-rate). Under 30 tracked days the CLI must print raw "so far" sums and
   the dashboard hero must be measured-only ("Saved so far"); at >=30 days the
   "/mo" run-rate framing is unchanged.

2. Since-install estimated tier: the right-hand card summed only 2 estimated
   categories while the left card summed 6, so the two cards disagreed about
   what "estimated" contains. It must now sum the same six — double-count-safe:
   mcp_cap_estimated is DISJOINT from uncaptured_runtime (_get_savings_summary
   pops mcp_cap out of the totals before _estimate_uncaptured_runtime derives
   its per-session proxy from them), and avoided-search is hint_followed XOR
   retrieval_serve, never both. cache_drop / output_waste stay excluded
   (opportunity, not realized).

3. Billing-mode transparency: both dashboard payload builders must carry
   billing_mode, and the template must caption the heroes mode-aware.

Dashboard assertions are template/wiring-level (renderSavings runs client-side;
there is no JS harness) — the established pattern of test_runway_card_wiring.
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module, measure_source as _split_measure_source

import importlib.util
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
ASSETS = REPO / "skills" / "token-optimizer" / "assets"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
        str(tmp_path / "base" / "token-optimizer-a" / "data"),
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def _summary_fixture():
    """Minimal merged-savings shape savings_report consumes (fail-open .gets)."""
    return {
        "total_tokens": 120_000,
        "total_cost_usd": 12.34,
        "total_events": 5,
        "by_category": {},
        "daily_avg_usd": 0.41,
        "period_days": 30,
        "model_routing": {
            "realized_cost_usd": 2.5,
            "baseline_opus_share": 0.9,
            "current_opus_share": 0.4,
        },
        "uncaptured_runtime": {"cost_saved_usd": 3.0, "subagent_dispatches": 7},
        "behavioral_estimate": {"cost_saved_usd": 1.5, "loop_events": 2, "prevented_iterations": 3},
        "mcp_cap_estimated": {"cost_saved_usd": 0.8, "events": 4},
    }


# ---------------------------------------------------------------- CLI guard


def test_young_install_cli_prints_so_far_not_monthly(measure, monkeypatch, capsys):
    four_days_ago = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
    monkeypatch.setattr(measure, "_install_date", lambda: four_days_ago)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30: _summary_fixture())
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    assert "Estimated monthly" not in out, "young install must not project a run-rate"
    assert "days tracked" in out
    assert "Total so far (5 days tracked): $12.34" in out
    # Per-component estimated lines drop "/mo" for raw "so far" sums.
    assert "/mo" not in out
    assert "~$3.00 so far" in out  # uncaptured runtime: raw sum, not x30/4 projection


def test_mature_install_cli_keeps_monthly_run_rate(measure, monkeypatch, capsys):
    long_ago = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    monkeypatch.setattr(measure, "_install_date", lambda: long_ago)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30: _summary_fixture())
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    assert "Estimated monthly: $12.34 (measured)" in out
    assert " so far" not in out


def test_exactly_30_days_is_run_rate(measure, monkeypatch, capsys):
    # tracked = elapsed_days + 1, so an install 29 days ago = 30 tracked days:
    # the window is fully observed and "/mo" is honest.
    edge = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
    monkeypatch.setattr(measure, "_install_date", lambda: edge)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30: _summary_fixture())
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    assert "Estimated monthly" in out
    assert "days tracked" not in out


def test_future_install_date_clamps_to_cumulative(measure, monkeypatch, capsys):
    # Clock skew: a "future" install must clamp to 1 tracked day (cumulative,
    # fails safe) rather than projecting or crashing.
    tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    monkeypatch.setattr(measure, "_install_date", lambda: tomorrow)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30: _summary_fixture())
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    assert "Total so far (1 day tracked)" in out
    assert "Estimated monthly" not in out


def test_no_install_date_falls_back_to_run_rate(measure, monkeypatch, capsys):
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30: _summary_fixture())
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    assert "Estimated monthly" in out


# ------------------------------------------- since-install six-category sum


def _full_fixture(hint_events):
    return {
        "total_cost_usd": 10.0,
        "model_routing": {"realized_cost_usd": 2.0},
        "behavioral_estimate": {"cost_saved_usd": 1.0},
        "uncaptured_runtime": {"cost_saved_usd": 2.0},
        "mcp_cap_estimated": {"cost_saved_usd": 4.0},
        "contamination_exit": {"cost_saved_usd": 8.0},
        "handover_rerun": {"cost_saved_usd": 16.0},
        "hint_followed": {"cost_saved_usd": 32.0, "events": hint_events},
        "retrieval_serve": {"cost_saved_usd": 64.0, "served_sessions": 2},
        # Opportunity tier: must NEVER enter the estimated sum.
        "cache_drop": {"cost_saved_usd": 999.0},
        "output_waste": {"cost_saved_usd": 999.0},
    }


def test_since_install_sums_six_categories_hint_wins(measure, monkeypatch):
    monkeypatch.setattr(measure, "_install_date", lambda: "2026-07-01")
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days: _full_fixture(hint_events=3))
    si = measure._savings_since_install()
    assert si["measured_usd"] == 12.0
    # 1 + 2 + 4 + 8 + 16 + 32 (hint_followed observed; retrieval_serve excluded;
    # cache_drop/output_waste excluded; mcp_cap included — disjoint from runtime).
    assert si["estimated_usd"] == 63.0


def test_since_install_avoided_search_falls_back_to_retrieval(measure, monkeypatch):
    monkeypatch.setattr(measure, "_install_date", lambda: "2026-07-01")
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days: _full_fixture(hint_events=0))
    si = measure._savings_since_install()
    # 1 + 2 + 4 + 8 + 16 + 64 (no hint fired yet -> cohort estimate, never both).
    assert si["estimated_usd"] == 95.0


def test_since_install_survives_missing_estimate_blocks(measure, monkeypatch):
    # mcp_cap_estimated ships as None when no mcp_cap rows exist; every other
    # block may be absent. The sum must degrade to the present categories.
    monkeypatch.setattr(measure, "_install_date", lambda: "2026-07-01")
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days: {
        "total_cost_usd": 5.0,
        "behavioral_estimate": {"cost_saved_usd": 1.0},
        "uncaptured_runtime": {"cost_saved_usd": 2.0},
        "mcp_cap_estimated": None,
    })
    si = measure._savings_since_install()
    assert si["measured_usd"] == 5.0
    assert si["estimated_usd"] == 3.0


# ------------------------------------------------- billing-mode transparency


def test_live_payload_carries_billing_mode(measure):
    p = measure._live_savings_payload(days=30)
    assert p["savings"].get("billing_mode") in ("api", "subscription")


def test_both_generators_plumb_billing_mode():
    src = _split_measure_source(SCRIPTS / "measure.py")
    hits = re.findall(
        r'savings_data\["billing_mode"\] = keepwarm_billing_mode\(\)', src
    )
    assert len(hits) >= 2, (
        "billing_mode must be set in BOTH generate_standalone_dashboard and "
        "_live_savings_payload, or the live-patched tile diverges from a regen"
    )


def test_template_renders_mode_aware_captions():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("function renderSavings(")
    body = html[start:start + 40000]
    assert "s.billing_mode" in body, "template no longer reads billing_mode"
    assert "billed spend you did not incur" in body  # api caption
    assert "capacity freed inside your limits" in body  # subscription caption
    # The caption sits under BOTH heroes (left counted card + right to-date card).
    assert body.count("billingCaption") >= 3, "caption must render on both cards"
    # The old far-away flat-plan sentence in the section header is gone.
    assert "On a flat monthly plan, this is the API spend you are not paying" not in html


# --------------------------------------------------- dashboard young-install


def test_template_has_young_install_guard():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("function renderSavings(")
    body = html[start:start + 40000]
    # Guard derives tracked days from since_install.days, clamped to the window.
    assert "trackedDays" in body
    assert "Math.min(days, Number(si.days))" in body
    assert "trackedDays >= 30" in body, "exactly-30 must stay run-rate ('/mo')"
    # Both framings exist and are selected by the guard, never hardcoded.
    assert "runRate ? '/mo' : ' so far'" in body
    assert "days tracked" in body
    # The new action summary labels its estimated portions at every install age.
    # It shows cumulative value without projecting a partial sample to a month.
    assert "logged + estimated" in body
    assert "var heroAmt = gettingMo" in body
