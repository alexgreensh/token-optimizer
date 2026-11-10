"""Each user's savings compare against their OWN pre-install baseline, pinned.

The ledger keeps a rolling window, so the months before install age out of it.
The workload anchor must never slide forward onto months that already ran
Token Optimizer (that compares the tool against itself), and the pre-install
anchor is rebuilt from the user's own baseline window, never a fixed month.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_cost_accuracy_200 import SID, _assistant, _load, _write  # noqa: E402

METRICS = {"sessions": 200, "usd": 900.0, "tokens": 1e9, "flat_usd": 1000.0,
           "api_calls": 5000, "messages": 6000}


def _freeze(measure, month):
    (measure.SNAPSHOT_DIR / "workload_anchor.json").write_text(json.dumps({
        "month": month, "metrics": METRICS, "rates": measure._WEIGHT_POOL_FLAT_RATES,
        "activity_basis": 1}))


def test_frozen_anchor_does_not_slide_to_a_later_month(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _freeze(measure, "2026-03")
    later = dict(METRICS, flat_usd=5.0)
    metrics, month = measure._stable_workload_anchor(later, "2026-06")
    assert month == "2026-03" and metrics["flat_usd"] == 1000.0


def test_an_earlier_month_from_a_backfill_does_re_anchor(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _freeze(measure, "2026-06")
    earlier = dict(METRICS, flat_usd=7.0)
    metrics, month = measure._stable_workload_anchor(earlier, "2026-03")
    assert month == "2026-03" and metrics["flat_usd"] == 7.0


def _baseline_window(measure, start, end):
    (measure.SNAPSHOT_DIR / "baseline_state.json").write_text(json.dumps(
        {"window": {"start": start, "end": end}}))


def test_pinned_anchor_does_not_call_post_install_window_pre_install(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(measure, "_install_date", lambda: "2026-03-03")
    (measure.SNAPSHOT_DIR / measure._PRETOOL_ANCHOR_FILE).write_text(json.dumps({
        "status": "pinned", "activity_basis": 1, "rates": measure._WEIGHT_POOL_FLAT_RATES,
        "metrics": METRICS, "window": ["2026-03-17", "2026-04-16"],
        "label": "your pre-install baseline (2026-03-17 to 2026-04-16)"}))
    assert measure._pinned_workload_anchor()["label"] == "your tracked baseline (2026-03-17 to 2026-04-16)"


def test_pre_install_anchor_is_built_from_this_users_own_window(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(measure, "_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS", 2)
    day = datetime.now() - timedelta(days=60)
    start, end = (day - timedelta(days=2)).strftime("%Y-%m-%d"), (day + timedelta(days=2)).strftime("%Y-%m-%d")
    _baseline_window(measure, start, end)
    projects = tmp_path / "claude_home" / "projects"
    for i in range(3):
        sid = SID[:-1] + str(i)
        _write(projects / "-repo" / f"{sid}.jsonl", [_assistant(f"r{i}", day)], mtime=day.timestamp())
    # A session outside the window is not part of the baseline.
    _write(projects / "-repo" / f"{SID[:-1]}9.jsonl", [_assistant("late", datetime.now())])

    assert measure._pretool_anchor_step(budget_seconds=30) == "pinned"
    pinned = measure._pinned_workload_anchor()
    assert pinned["metrics"]["sessions"] == 3
    assert pinned["metrics"]["api_calls"] == 3
    assert start in pinned["label"] and end in pinned["label"]
    # Pinned once: later calls do no work and keep the same anchor.
    assert measure._pretool_anchor_step(budget_seconds=30) == "pinned"


def test_pre_install_anchor_without_surviving_transcripts_stops_trying(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _baseline_window(measure, "2025-01-01", "2025-01-31")
    assert measure._pretool_anchor_step(budget_seconds=5) == "unavailable"
    assert measure._pinned_workload_anchor() is None


def test_savings_report_counts_avoided_re_reads(tmp_path, monkeypatch, capsys):
    measure = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(measure, "_counted_cumulative_summary",
                        lambda days: {"available": True, "reread_tokens": 1_000_000, "reread_usd": 12.5})
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    assert "repeat reads avoided: ~$12.50" in out and "[modeled]" in out


def test_tripwire_flags_a_collapse_that_activity_does_not_explain(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (measure.SNAPSHOT_DIR / measure._HEADLINE_HISTORY_FILE).write_text(json.dumps(
        {"d30": [{"date": "2026-01-01", "usd": 515.0, "events": 3600}]}))
    msg = measure._headline_tripwire(84.0, 3637, 30)
    assert msg and "$515" in msg and "$84" in msg


def test_tripwire_stays_quiet_when_activity_also_fell(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (measure.SNAPSHOT_DIR / measure._HEADLINE_HISTORY_FILE).write_text(json.dumps(
        {"d30": [{"date": "2026-01-01", "usd": 515.0, "events": 3600}]}))
    assert measure._headline_tripwire(84.0, 900, 30) is None
