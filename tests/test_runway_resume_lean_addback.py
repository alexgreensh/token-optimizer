#!/usr/bin/env python3
"""Regression: the weekly runway card must fold the estimated-tier levers
(resume_lean, verbosity_steer) back into its dollar figure.

The bug (current-week-undercount): `_get_merged_savings` relocates
resume_lean/verbosity out of `total_cost_usd` into the estimated tier, and the
counted transcript window excludes resume_lean too. So the weekly card priced
ONLY the metered removals and silently dropped real, magnitude-metered (v5.13.1)
savings -- on a live 57%-of-limit week that was $18.78 shown against a true
$37.10. The fix adds the window's resume_lean/verbosity dollars back into `ctx`
and flips the tier to "estimated" (the trigger is counterfactual even though the
magnitude is metered).

Gate: with an addback present the card shows counted + resume_lean at tier
"estimated"; with none present it is unchanged at tier "measured". A card that
can't go red on the addback is not a gate -- both branches are asserted.
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


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


def _wire(measure, monkeypatch, resume_lean_usd):
    """Drive runway_snapshot to a live weekly card whose only counted dollar is a
    known counted total, with `resume_lean_usd` waiting in the estimated tier."""
    resets_at = time.time() + 3 * 24 * 3600  # future reset -> window covers now
    monkeypatch.setattr(measure, "_keepwarm_read_meters", lambda now=None: {
        "available": True, "stale": False, "age_s": 60, "ts": time.time(),
        "five_hour_pct": 20.0, "seven_day_pct": 56.0,
        "seven_day_resets_at": resets_at,
    })
    from _measure import headline
    monkeypatch.setattr(headline, "input_traffic", lambda *args: {"tokens": 1_000_000, "basis": "including cache", "complete": True})
    # consumed > 0 so the card is not suppressed; multiplier kept material.
    monkeypatch.setattr(measure, "_dashboard_spent_token_basis",
                        lambda conn, days=30: {"tokens": 1_000_000,
                                               "basis": "test", "complete": True})
    monkeypatch.setattr(measure, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.5})

    rl = {"cost_saved_usd": resume_lean_usd, "tokens_saved": 1, "events": 9} \
        if resume_lean_usd else None
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30, since=None: {
        "total_cost_usd": 18.78,
        "model_routing": {"realized_cost_usd": 0.0},
        "resume_lean_estimated": rl,
        "verbosity_steer_estimated": None,
        "repriced_to_session_mix": False,
    })
    monkeypatch.setattr(measure, "_counted_window_summary",
                        lambda conn, s, e: {"available": True, "total_usd": 18.78})


def test_resume_lean_folds_into_weekly_card(measure, monkeypatch):
    _wire(measure, monkeypatch, resume_lean_usd=18.32)
    snap = measure.runway_snapshot(days=30)
    assert snap is not None, "card unexpectedly suppressed"
    # counted 18.78 + resume_lean 18.32 == 37.10, labelled estimated
    assert snap["saved_usd_context"] == pytest.approx(37.10, abs=0.01)
    assert snap["saved_usd_tier"] == "estimated"
    wk = [w for w in snap["windows"] if w["key"] == "seven_day"][0]
    assert wk["saved_usd"] == pytest.approx(37.10, abs=0.01)
    assert wk["saved_usd_tier"] == "estimated"


def test_no_addback_leaves_measured_total_unchanged(measure, monkeypatch):
    _wire(measure, monkeypatch, resume_lean_usd=0.0)
    snap = measure.runway_snapshot(days=30)
    assert snap is not None
    assert snap["saved_usd_context"] == pytest.approx(18.78, abs=0.01)
    assert snap["saved_usd_tier"] == "measured"
