"""Since-install all-in total, the window-weighted estimate rate, and the
one-time drain window.

The since-install figure must use the monthly headline's own parts
(logged + repeat reads + estimates, with routing disclosed separately), the
update's one-time drain must
re-read all history so older months can be priced, and estimates must be priced
at the window's model mix rather than the latest session's model.

Run: python3 -m pytest tests/test_since_install_all_in.py -v
"""
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    (tmp_path / "home").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._init_trends_db().close()
    yield mod
    sys.modules.pop("measure", None)


def _install(m, days_ago):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    (m.SNAPSHOT_DIR / "snapshot_before.json").write_text(
        json.dumps({"timestamp": ts, "totals": {"estimated_total": 0}}))


def _model_daily(m, rows):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    day = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    for model, tokens in rows:
        conn.execute("INSERT INTO model_daily (date, model, total_tokens) VALUES (?, ?, ?)",
                     (day, model, tokens))
    conn.commit()
    conn.close()


def test_drain_reads_all_history_since_install(m):
    _install(m, 200)
    assert m._upgrade_drain_days() == 202
    _install(m, 10)
    assert m._upgrade_drain_days() == 90


def test_drain_without_install_date_reads_everything(m):
    assert m._install_date() is None
    assert m._upgrade_drain_days() == 3650


def test_drain_passes_its_window_to_collect(m, monkeypatch):
    _install(m, 200)
    # The drain sets this for its own process; register it so it is restored.
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "0")
    seen = {}
    monkeypatch.setattr(m, "collect_sessions", lambda days=90, quiet=False, rebuild=False: seen.setdefault("days", days))
    monkeypatch.setattr(m, "_upgrade_drain_release_lock", lambda: None)
    m._run_upgrade_drain(quiet=True)
    assert seen["days"] == 202


def test_estimate_rate_follows_the_window_mix(m, monkeypatch):
    tier = m.PRICING_TIERS[m._load_pricing_tier()]["claude_models"]
    opus, sonnet = float(tier["opus"]["input"]), float(tier["sonnet"]["input"])
    _model_daily(m, [("opus", 800), ("sonnet", 200), ("stealth/unknown-model", 1000)])
    expected = 0.8 * opus + 0.2 * sonnet
    assert m._window_input_rate_per_mtok(days=30) == pytest.approx(expected)
    # The latest session's model does not move the rate.
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-5-5")
    m._RESOLVED_MODEL_CACHE.clear()
    assert m._window_input_rate_per_mtok(days=30) == pytest.approx(expected)


def test_estimate_rate_without_priced_usage_falls_back(m):
    _model_daily(m, [("stealth/unknown-model", 1000)])
    assert m._window_input_rate_per_mtok(days=30) == pytest.approx(m._estimate_compression_cost_per_mtok())


def test_since_install_all_in_uses_the_headline_parts(m, monkeypatch):
    _install(m, 120)
    days = (datetime.now() - datetime.fromisoformat(m._install_date())).days + 1
    full = {"total_cost_usd": 12.5, "model_routing": {"realized_cost_usd": 300.25, "evidence": "estimated"},
            "resume_lean_estimated": {"cost_saved_usd": 4.0}, "hint_followed": {"events": 1, "cost_saved_usd": 0.01}}
    counted = {"available": True, "reread_usd": 80.125, "reread_prices_available": True,
               "reread_pricing_complete": True}
    calls = {}
    monkeypatch.setattr(m, "_get_merged_savings", lambda days=30, since=None: calls.setdefault("merged", days) and dict(full))
    monkeypatch.setattr(m, "_counted_cumulative_summary", lambda days=None, **k: calls.setdefault("counted", days) and dict(counted))
    out = m._savings_since_install()
    assert calls == {"merged": days, "counted": days}
    all_in = out["all_in"]
    parts = all_in["components_usd"]
    assert parts["logged"] == 12.5 and parts["routing_estimated"] == 0.0 and parts["reread"] == pytest.approx(80.13, abs=0.011)
    assert all_in["routing_usd"] == pytest.approx(300.25)
    assert all_in["estimated_usd"] == pytest.approx(0.01)
    assert all_in["total_usd"] == pytest.approx(12.5 + 80.125 + 0.01, abs=0.011)
    total_cents = round(all_in["total_usd"] * 100)
    assert total_cents == round(sum(parts.values()) * 100) + round(all_in["estimated_usd"] * 100)
    assert all_in["routing_pending"] is False


def test_since_install_marks_pending_routing(m, monkeypatch):
    _install(m, 120)
    full = {"total_cost_usd": 5.0, "model_routing": {"realized_cost_usd": 0.0, "evidence": "pending",
                                                    "pending_sessions": 657}}
    monkeypatch.setattr(m, "_get_merged_savings", lambda days=30, since=None: dict(full))
    monkeypatch.setattr(m, "_counted_cumulative_summary", lambda days=None, **k: {"available": False})
    out = m._savings_since_install()
    all_in = out["all_in"]
    assert all_in["routing_pending"] is True and all_in["routing_pending_sessions"] == 657
    assert out['excluded_usd']['routing'] is None


def test_since_install_includes_realized_attested_setup_once(m, monkeypatch):
    _install(m, 120)
    full = {'total_cost_usd': 10, 'structural_detail': {'estimated': {'cost_saved_usd': 12.5}}}
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kw: dict(full))
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda **kw: {'available': True, 'reread_usd': 5})
    out = m._savings_since_install()
    assert out['all_in']['estimated_usd'] == 12.5
    assert out['all_in']['total_usd'] == 27.5


def test_since_install_excludes_verbosity_and_reports_its_amount(m, monkeypatch):
    _install(m, 120)
    full = {'total_cost_usd': 20, 'by_category': {'verbosity_steer_measured': {'cost_saved_usd': 7}},
            'verbosity_steer': {'cost_saved_usd': 3}, 'resume_lean_estimated': {'cost_saved_usd': 4},
            'model_routing': {'realized_cost_usd': 2, 'evidence': 'estimated'}}
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kw: dict(full))
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda **kw: {'available': True, 'reread_usd': 5})
    out = m._savings_since_install()
    assert out['excluded_usd'] == {'routing': 2, 'lean_resumes': 4, 'verbosity': 10}
    assert out['all_in']['components_usd']['logged'] == 13
    assert out['all_in']['estimated_usd'] == 0


def test_since_install_includes_stated_routing_only_as_estimate(m, monkeypatch):
    _install(m, 120)
    routing = {'realized_cost_usd': 300.25, 'evidence': 'estimated', 'baseline_source': 'pretool_baseline',
               'baseline_opus_share': 0.95, 'measured_baseline_opus_share': 0.8369,
               'measured_baseline_realized_cost_usd': 0.0}
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kw: {'total_cost_usd': 12.5, 'model_routing': routing})
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda **kw: {'available': True, 'reread_usd': 80})
    out = m._savings_since_install()
    assert out['measured_usd'] == 12.5
    assert out['all_in']['components_usd']['routing_estimated'] == 300.25
    assert out['all_in']['total_usd'] == 392.75
    assert out['all_in']['routing_measured_baseline_realized_cost_usd'] == 0
