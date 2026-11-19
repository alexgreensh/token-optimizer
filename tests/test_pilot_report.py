"""`measure.py pilot-report`: the weekly Slack report for the Augury pilot.

Synthetic fixtures only: the dashboard payloads are stubbed, install evidence
comes from a snapshot marker in the temp SNAPSHOT_DIR, routing state from the
t3 marker files, trims from the structural ledger JSONL, and nudges from a
temporary trends.db nudge_events table.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from helpers.measure_split import exec_source_module as _split_exec_module  # noqa: E402
from _measure import pilot_report  # noqa: E402


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
        str(tmp_path / "data"),
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def _install_marker(measure, days_ago=40):
    day = (datetime.now() - timedelta(days=days_ago)).date().isoformat()
    (Path(measure.SNAPSHOT_DIR)).mkdir(parents=True, exist_ok=True)
    (Path(measure.SNAPSHOT_DIR) / "snapshot_before.json").write_text(
        json.dumps({"timestamp": day + "T12:00:00+00:00"}), encoding="utf-8")
    return day


def _payload(days, *, capacity_pct=38, coverage="pre_install",
             saved=150.0, estimated=20.0, routing=30.0, saved7=15.0):
    """A _dashboard_savings_data-shaped payload for one window."""
    if days == 7:
        return {
            "action_savings": {"total_usd": saved7 - (5.0 if routing else 0.0)},
            "model_routing": {"realized_cost_usd": 5.0 if routing else 0.0},
        }
    actual = 100.0
    before = actual * (1 + capacity_pct / 100.0) if capacity_pct else 0.0
    return {
        "before_after": {
            "transformation_state": "ok" if capacity_pct else "insufficient_history",
            "actual_monthly_usd": actual,
            "counterfactual_monthly_usd": before,
            "baseline_provenance": {"coverage": coverage},
        },
        "since_install": {
            "install_date": "2026-10-01",
            "all_in": {
                "total_usd": saved,
                "estimated_usd": estimated,
                "routing_usd": routing,
                "routing_baseline_source": "measured_at_install" if routing else None,
                "components_usd": {
                    "logged": saved - estimated - routing,
                    "class_premium": 0.0,
                    "routing_measured": 0.0,
                    "routing_estimated": routing,
                    "reread": 0.0,
                },
            },
        },
        "action_savings": {"total_usd": 0.0},
        "model_routing": {"realized_cost_usd": 0.0},
        "structural_detail": {"trims": []},
    }


def _wire_dashboard(measure, monkeypatch, *, capacity_pct=38, coverage="pre_install",
                    saved=150.0, estimated=20.0, routing=30.0, saved7=15.0):
    full = _payload(30, capacity_pct=capacity_pct, coverage=coverage,
                    saved=saved, estimated=estimated, routing=routing)
    week = _payload(7, saved7=saved7, routing=bool(routing))
    monkeypatch.setattr(
        measure, "_dashboard_savings_data",
        lambda days=None, **kw: week if days == 7 else full)


def _trims(measure, applied=2, closed=1):
    path = Path(measure.SNAPSHOT_DIR) / "structural_trims.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(applied + closed):
        rows.append({
            "kind": "trim", "id": "t%d" % i, "schema": 1,
            "component": "skills", "item": "skill-%d" % i, "scope": "global",
            "tokens_before": 1000, "tokens_after": 400,
            "ts": "2026-11-0%dT00:00:00+00:00" % (i + 1),
            "source": "audit_action", "tier": "measured",
            "evidence": "", "origin": "audit_action", "batch": "", "note": "",
        })
    for i in range(closed):
        rows.append({"kind": "close", "trim_id": "t%d" % i,
                     "ts": "2026-11-10T00:00:00+00:00", "reason": "reverted"})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _nudges(measure, rows):
    db = Path(measure.TRENDS_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nudge_events (session_uuid TEXT, kind TEXT, ts TEXT, "
                 "context_tokens INTEGER, model TEXT, status TEXT)")
    conn.executemany("INSERT INTO nudge_events VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _nudge_rows(measure, followed=3, ignored=5, pending=3, days_ago=2):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    old = (datetime.now() - timedelta(days=30)).isoformat()
    rows = [("s", "fresh_session", ts, 1000, "claude", "followed")] * followed
    rows += [("s", "fresh_session", ts, 1000, "claude", "ignored")] * ignored
    rows += [("s", "context_size", ts, 2000, "claude", "pending")] * pending
    rows += [("s", "fresh_session", old, 1000, "claude", "followed")]
    return rows


def test_full_report_text_and_json(measure, monkeypatch, capsys, tmp_path):
    _install_marker(measure)
    _wire_dashboard(measure, monkeypatch)
    (Path(measure.SNAPSHOT_DIR) / "routing_injected_at").write_text("2026-10-15")
    _trims(measure)
    _nudges(measure, _nudge_rows(measure))

    report = pilot_report.collect(measure)
    lines = pilot_report.render(report)
    assert len(lines) <= 12
    text = "\n".join(lines)
    assert "+38% more work per token vs your 30 days before install" in text
    assert "Saved since install: $150.00" in text
    assert "Saved in the last 7 days: $15.00" in text
    assert "measured $100.00 / estimated $20.00 / routing $30.00" in text
    assert "Routing guidance: on since 2026-10-15" in text
    assert "Setup trims: 2 applied" in text
    assert "Nudges: followed 3 of 11 this week" in text
    assert "installed" in lines[0]
    # No personal paths in the paste-able output.
    for path in (str(measure.SNAPSHOT_DIR), str(tmp_path), str(Path.home())):
        assert path not in text

    assert pilot_report.main(["--json"], facade=measure) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["capacity_pct"] == 38
    assert out["saved_since_install_usd"] == 150.0
    assert out["routing"] == {"on": True, "since": "2026-10-15"}
    assert out["setup_trims_applied"] == 2
    assert out["nudges"] == {"followed": 3, "total": 11}


def test_fallback_window_is_labeled(measure, monkeypatch):
    _install_marker(measure)
    _wire_dashboard(measure, monkeypatch, coverage="post_install")
    lines = pilot_report.render(pilot_report.collect(measure))
    capacity = next(l for l in lines if l.startswith("Capacity"))
    assert "+38% more work per token vs your earliest tracked month (fallback window)" in capacity


def test_routing_off_zeroes_credit(measure, monkeypatch):
    _install_marker(measure)
    _wire_dashboard(measure, monkeypatch)  # no routing markers written
    report = pilot_report.collect(measure)
    assert report["routing"]["on"] is False
    # The $30 routing component inside all_in is removed, not reported.
    assert report["saved_since_install_usd"] == 120.0
    assert report["split"]["routing_usd"] == 0.0
    assert report["split"]["measured_usd"] == 100.0
    assert report["saved_7d_usd"] == 10.0  # action 10 + routing 0
    text = "\n".join(pilot_report.render(report))
    assert "Routing guidance: off" in text
    assert "routing $0.00" in text


def test_routing_removed_marker_turns_off(measure, monkeypatch):
    _install_marker(measure)
    _wire_dashboard(measure, monkeypatch)
    snap = Path(measure.SNAPSHOT_DIR)
    (snap / "routing_injected_at").write_text("2026-10-15")
    (snap / "routing_removed_at").write_text("2026-11-01")
    report = pilot_report.collect(measure)
    assert report["routing"]["on"] is False
    assert report["saved_since_install_usd"] == 120.0


def test_no_trims_and_no_nudges(measure, monkeypatch):
    _install_marker(measure)
    _wire_dashboard(measure, monkeypatch, capacity_pct=None, saved=0.0,
                    estimated=0.0, routing=0.0, saved7=0.0)
    report = pilot_report.collect(measure)
    text = "\n".join(pilot_report.render(report))
    assert "Setup trims: none yet (run /token-optimizer)" in text
    assert "Nudges: followed 0 of 0 this week" in text
    assert "Capacity since install: not measured yet" in text


def test_no_install_evidence_still_renders(measure, monkeypatch):
    _wire_dashboard(measure, monkeypatch)
    report = pilot_report.collect(measure)
    lines = pilot_report.render(report)
    assert len(lines) <= 12
    assert "date not recorded" in lines[0]


def test_unknown_flag_rejected(measure, capsys):
    assert pilot_report.main(["--bogus"], facade=measure) == 2
    assert "Unknown pilot-report option" in capsys.readouterr().out
