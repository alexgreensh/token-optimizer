"""Savings evidence labels: counterfactual credits are estimates, not measured.

FINDINGS.md (arxiv-2611.05306) section 4: checkpoint-restore credits, loop
detection events, followed-hint causality and fresh-session nudge dollars are
counterfactual proxies, not observed token changes. "Measured" may only mean
observed tokens and actions.

Run: python3 -m pytest tests/test_savings_evidence_labels.py -x -q
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

SID = "11111111-2222-3333-4444-555555555555"


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


def _ts(days_ago=1):
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _seed(m):
    """One of each evidence class, all inside the 30-day window."""
    conn = sqlite3.connect(str(m.TRENDS_DB))
    conn.executescript(m._SCHEMA)
    se = [
        ("tool_archive", 10000, 0.05, "replaced bash (40,000 chars -> 1,200 chars)"),
        ("checkpoint_restore", 50000, 0.25, "restored from auto"),
        ("hint_followed", 5000, 0.025, "hint->read: auth.py"),
        ("resume_lean", 20000, 0.10, "lean resume"),
    ]
    for et, tok, cost, detail in se:
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, "
            "session_id, session_uuid, detail, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_ts(), et, tok, cost, SID, SID, detail, "claude-sonnet-5"))
    ce = [
        # A real measured compression: output actually shrank.
        ("bash_compress_git", 20000, 5000, 1, "measured", "git log"),
        # Legacy loop row: NULL tier (predates labels) — must not count as measured.
        ("loop_detection", 8000, 4, 1, None, "type=message_loop count=4 measured_tokens=8000"),
        # New loop row: labelled estimated + unverified at write time.
        ("loop_detection", 6000, 4, 0, "estimated", "type=retry_churn count=3 measured_tokens=6000"),
    ]
    for feat, orig, comp, ver, tier, detail in ce:
        conn.execute(
            "INSERT INTO compression_events (timestamp, session_id, session_uuid, feature, "
            "command_pattern, original_tokens, compressed_tokens, verified, detail, model, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_ts(), SID, SID, feat, "cmd", orig, comp, ver, detail, "claude-sonnet-5", tier))
    conn.commit()
    conn.close()


def test_checkpoint_restore_is_estimated_not_measured(m):
    _seed(m)
    merged = m._get_merged_savings(days=30)
    # The 50K counterfactual working-set credit must not sit in the measured total.
    assert "checkpoint_restore" not in merged["by_category"]
    measured_tokens = merged["total_tokens"]
    # Measured = tool_archive (10K) + bash_compress_git (15K) only.
    assert measured_tokens == 25000
    est = merged.get("checkpoint_restore_estimated") or {}
    assert est.get("tokens_saved") == 50000
    assert est.get("events") == 1
    # The dashboard token split counts it on the estimated side, not the floor.
    shown, measured = m._dashboard_saved_tokens(merged)
    assert measured == 25000
    assert shown == measured + 50000 + 5000 + 20000  # + hint_followed + resume_lean
    # The runway "measured floor" leaves it out too.
    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        assert m._runway_saved_tokens(conn, (datetime.now() - timedelta(days=30)).isoformat()) == 25000
    finally:
        conn.close()


def test_checkpoint_restore_flows_through_action_savings(m):
    _seed(m)
    merged = m._get_merged_savings(days=30)
    parts = m._action_savings_components(merged, {"available": True, "reread_usd": 0})
    assert "checkpoint_restore_estimated" in parts["estimates_usd"]
    assert parts["estimates_usd"]["checkpoint_restore_estimated"] > 0
    # It is never inside the logged/measured component.
    assert "checkpoint_restore" not in merged["by_category"]


def test_loop_detection_is_not_a_measured_saving(m):
    _seed(m)
    merged = m._get_merged_savings(days=30)
    # Neither the legacy NULL-tier row nor the new estimated row may reach the
    # measured headline: the event records an observed loop, not saved tokens.
    assert "loop_detection" not in merged["by_category"]
    # Measured stays tool_archive + bash_compress_git (checkpoint_restore and
    # hint_followed/resume_lean already live in the estimated tier).
    assert merged["total_tokens"] == 25000
    # The diagnostic coverage view files both rows under estimated, never measured.
    cov = m._get_compression_coverage(days=30)
    measured_feats = {e["feature"] for e in cov["tiers"]["measured"]}
    assert "loop_detection" not in measured_feats
    est_feats = {e["feature"] for e in cov["tiers"]["estimated"]}
    assert "loop_detection" in est_feats
    # The avoided-continuation estimate still sees both loop rows (it has its
    # own SQL and never filtered on tier): 8000-4 + 6000-4 = 13992 tokens.
    beh = m._estimate_behavioral_savings(days=30)
    assert beh["loop_events"] == 2
    assert beh["tokens_saved"] == 13992
    assert beh["evidence"] == "estimated"


def test_hint_followed_stays_estimated(m):
    """Regression guard: the observed hint->read trigger was already relocated
    to the estimated tier; the magnitude is a fixed estimate, not a meter."""
    _seed(m)
    merged = m._get_merged_savings(days=30)
    assert "hint_followed" not in merged["by_category"]
    est = merged.get("hint_followed") or merged.get("hint_followed_estimated") or {}
    assert est.get("events") == 1


def test_fresh_session_nudge_usd_is_net_of_handover_and_rebuild(m, monkeypatch):
    """Nudge dollars = warm-context avoided (cache-read) minus the working set
    the fresh session must carry, minus the one-time handover write
    (cache-write) and rebuild re-reads (input). Floored at $0, labelled est."""
    # sonnet rates: input 2.0, cache_read 0.2, cache_write 2.5 per MTok.
    saved = 100_000
    usd = m._fresh_session_savings_usd(saved, SID)
    # No session store -> rebuild falls back to the lean-block floor (1,000).
    rebuild = m._FRESH_NUDGE_LEAN_BLOCK_TOKENS
    avoided = max(0, saved - rebuild) * 0.2 / 1_000_000
    handover = rebuild * 2.5 / 1_000_000
    rebuild_usd = rebuild * 2.0 / 1_000_000
    expected = max(0.0, avoided - handover - rebuild_usd)
    assert usd == pytest.approx(expected, abs=1e-6)
    assert usd < saved * 2.0 / 1_000_000       # strictly below the old gross figure
    # A large rebuild working set can zero the net out (honest floor at $0).
    monkeypatch.setattr(m, "_checkpoint_restore_recovery_tokens",
                        lambda sid, floor_tokens: 500_000)
    assert m._fresh_session_savings_usd(saved, SID) == 0.0


def test_fresh_session_nudge_message_marks_estimate(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_is_v5_feature_enabled", lambda name: True)
    monkeypatch.setattr(m, "_log_compression_event", lambda **kw: None)
    qdir = tmp_path / "quality"
    qdir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", qdir)
    cache = qdir / f"quality-cache-{SID}.json"
    result = {"score": 67.7, "fill_pct": 60.0, "model_context_window": 1_000_000,
              "_nudge_previous_score": 68.0}
    msg = m._maybe_fresh_session_nudge(result, cache, {})
    assert msg and "Fresh session reclaims" in msg
    assert "est" in msg  # the dollar figure is labelled an estimate
