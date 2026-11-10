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
        ("loop_detection", 8000, 4, 1, None, "type=message_loop count=4 observed_tokens=8000"),
        # New loop row: labelled estimated + unverified at write time.
        ("loop_detection", 6000, 4, 0, "estimated", "type=retry_churn count=3 observed_tokens=6000"),
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
    # Unverified restore and resume estimates are excluded from the headline.
    shown, measured = m._dashboard_saved_tokens(merged)
    assert measured == 25000
    assert shown == measured + 5000  # hint_followed remains estimated
    # The runway "measured floor" leaves it out too.
    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        assert m._runway_saved_tokens(conn, (datetime.now() - timedelta(days=30)).isoformat()) == 25000
    finally:
        conn.close()


def test_unverified_checkpoint_restore_stays_out_of_action_savings(m):
    _seed(m)
    merged = m._get_merged_savings(days=30)
    parts = m._action_savings_components(merged, {"available": True, "reread_usd": 0})
    assert "checkpoint_restore_estimated" in parts["estimates_usd"]
    assert parts["estimates_usd"]["checkpoint_restore_estimated"] == 0
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
    # Non-savings warnings stay out of every compression coverage bucket.
    cov = m._get_compression_coverage(days=30)
    measured_feats = {e["feature"] for e in cov["tiers"]["measured"]}
    assert "loop_detection" not in measured_feats
    est_feats = {e["feature"] for e in cov["tiers"]["estimated"]}
    assert "loop_detection" not in est_feats
    # The avoided-continuation estimate still sees both loop rows (it has its
    # own SQL and never filtered on tier): 8000-4 + 6000-4 = 13992 tokens.
    beh = m._estimate_behavioral_savings(days=30)
    assert beh["loop_events"] == 2
    assert beh["tokens_saved"] == 13992
    assert beh["evidence"] == "estimated"


def test_coverage_omits_non_savings_advisory_events(m):
    _seed(m)
    for feature in ("context_size_advisory", "context_advisory_shadow"):
        m._log_compression_event(feature=feature, session_id=SID,
                                 verified=False, tier="estimated")
    cov = m._get_compression_coverage(days=30)
    features = {e["feature"] for bucket in cov["tiers"].values() for e in bucket}
    assert "context_size_advisory" not in features
    assert "context_advisory_shadow" not in features
    assert "bash_compress_git" in features


def test_hint_followed_stays_estimated(m):
    """Regression guard: the observed hint->read trigger was already relocated
    to the estimated tier; the magnitude is a fixed estimate, not a meter."""
    _seed(m)
    merged = m._get_merged_savings(days=30)
    assert "hint_followed" not in merged["by_category"]
    est = merged.get("hint_followed") or merged.get("hint_followed_estimated") or {}
    assert est.get("events") == 1


def test_fresh_session_nudge_reports_per_turn_and_break_even(m, monkeypatch):
    """Nudge economics = recurring per-turn saving (warm context re-sent at the
    cache-read rate, minus the working set carried anyway) vs the one-time
    handover write (cache-write) + rebuild re-reads (input). Break-even turns
    = ceil(one_time / per_turn); None when per_turn <= 0. No horizon assumed."""
    # sonnet rates: input 2.0, cache_read 0.2, cache_write 2.5 per MTok.
    # Realistic working set: a 40K rebuild, 300K of warm context.
    monkeypatch.setattr(m, "_checkpoint_restore_recovery_tokens",
                        lambda sid, floor_tokens: 40_000)
    econ = m._fresh_session_turn_economics(300_000, SID)
    per_turn = (300_000 - 40_000) * 0.2 / 1_000_000          # $0.052
    one_time = (m._FRESH_NUDGE_LEAN_BLOCK_TOKENS * 2.5
                + 40_000 * 2.0) / 1_000_000                  # $0.0825
    assert econ["per_turn_usd"] == pytest.approx(per_turn, abs=1e-9)
    assert econ["one_time_usd"] == pytest.approx(one_time, abs=1e-9)
    assert econ["breakeven_turns"] == 2                     # ceil(0.0825 / 0.052)
    # Floor case: no session store -> rebuild = lean-block floor (1,000).
    monkeypatch.undo()
    econ_floor = m._fresh_session_turn_economics(300_000, SID)
    per_floor = (300_000 - 1_000) * 0.2 / 1_000_000
    assert econ_floor["per_turn_usd"] == pytest.approx(per_floor, abs=1e-9)
    assert econ_floor["breakeven_turns"] >= 1
    # Safe case: working set swallows the warm context -> no fabricated N.
    monkeypatch.setattr(m, "_checkpoint_restore_recovery_tokens",
                        lambda sid, floor_tokens: 500_000)
    econ_zero = m._fresh_session_turn_economics(100_000, SID)
    assert econ_zero["per_turn_usd"] == 0.0
    assert econ_zero["breakeven_turns"] is None


def test_fresh_session_nudge_message_marks_estimate(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_is_v5_feature_enabled", lambda name: True)
    monkeypatch.setattr(m, "_log_compression_event", lambda **kw: None)
    qdir = tmp_path / "quality"
    qdir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", qdir)
    cache = qdir / f"quality-cache-{SID}.json"
    result = {"score": 67.7, "fill_pct": 60.0, "model_context_window": 1_000_000,
              "_nudge_previous_score": 68.0}
    # Size-based nudges require current-context telemetry before they can fire.
    assert m._maybe_fresh_session_nudge(result, cache, {}) is None
    quality_data = {"context_tokens": 600_000, "model_context_window": 1_000_000}
    msg = m._maybe_fresh_session_nudge(result, cache, quality_data)
    assert msg and "Fresh session reclaims" in msg
    assert "est" in msg  # the dollar figure is labelled an estimate
    # Per-turn + break-even phrasing, never a one-period session total.
    assert "per turn" in msg
    assert "pays back after ~" in msg
    assert "rebuilt for free" not in msg


def test_loop_warning_writer_stores_estimated_unverified_row(m, monkeypatch, tmp_path):
    """_maybe_loop_warning itself must write tier='estimated',
    verified=0 -- the observed loop volume is not a metered saving."""
    monkeypatch.setattr(m, "_is_v5_feature_enabled", lambda name: True)
    msgs = []
    for i in range(8):
        msgs.append((i * 2, "user", 400, True))
        msgs.append((i * 2 + 1, "assistant", 300, True))
    quality_data = {"messages": msgs, "tool_result_meta": []}
    result = {}
    cache = tmp_path / f"quality-cache-{SID}.json"
    msg = m._maybe_loop_warning(result, cache, quality_data)
    assert msg and "Loop" in msg
    conn = sqlite3.connect(str(m.TRENDS_DB))
    try:
        row = conn.execute(
            "SELECT feature, verified, tier, detail FROM compression_events "
            "WHERE feature='loop_detection' ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    feat, verified, tier, detail = row
    assert feat == "loop_detection"
    assert verified == 0
    assert tier == "estimated"
    assert "observed_tokens=" in detail
    assert "measured_tokens=" not in detail


def test_legacy_quality_nudge_is_not_measured(m):
    _seed(m)
    conn = sqlite3.connect(str(m.TRENDS_DB))
    conn.execute("INSERT INTO compression_events (timestamp, session_id, session_uuid, feature, original_tokens, compressed_tokens, verified, tier) VALUES (?, ?, ?, 'quality_nudge', 9000, 1000, 1, NULL)", (_ts(), SID, SID))
    conn.commit()
    conn.close()
    merged = m._get_merged_savings(days=30)
    assert merged['total_tokens'] == 25000
    assert 'quality_nudge' not in merged['by_category']
    coverage = m._get_compression_coverage(days=30)
    assert 'quality_nudge' not in {row['feature'] for row in coverage['tiers']['measured']}
    assert 'quality_nudge' in {row['feature'] for row in coverage['tiers']['estimated']}


def test_checkpoint_restore_does_not_credit_checkpoint_size(m, monkeypatch, tmp_path):
    cp_dir = tmp_path / 'checkpoints'
    cp_dir.mkdir()
    path = cp_dir / (SID + '-checkpoint.md')
    path.write_text('# header\nGenerated: now\n## Task\n' + 'x' * 5000)
    cp = {'filename': path.name, 'path': path, 'created': datetime.now(), 'trigger': 'auto'}
    monkeypatch.setattr(m, 'CHECKPOINT_DIR', cp_dir)
    monkeypatch.setattr(m, 'list_checkpoints', lambda: [cp])
    monkeypatch.setattr(m, '_order_checkpoints_for_restore', lambda rows: rows)
    monkeypatch.setattr(m, '_merge_earlier_checkpoint_context', lambda *args: '')
    monkeypatch.setattr(m, '_safe_checkpoint_file', lambda p: p)
    floors = []
    monkeypatch.setattr(m, '_checkpoint_restore_recovery_tokens', lambda sid, floor_tokens: floors.append(floor_tokens) or 0)
    m.compact_restore(session_id=SID, is_compact=True)
    assert floors == [0]


def test_bash_reexpansion_is_not_subtracted_from_mcp_archive(m):
    _seed(m)
    conn = sqlite3.connect(str(m.TRENDS_DB))
    for detail, tokens in [('abcdef0123456789 (Bash)', 7000), ('toolu_abc (mcp__docs__fetch)', 3000)]:
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail,model) VALUES (?, 'tool_archive_reexpand', ?, ?, ?, ?, ?, 'claude-sonnet-5')", (_ts(), tokens, tokens * .000005, SID, SID, detail))
    conn.commit()
    conn.close()
    summary = m._get_merged_savings(days=30)
    assert summary['by_category']['tool_archive']['tokens_saved'] == 7000
    assert summary['by_category']['bash_archive_reexpand']['tokens_saved'] == -7000
    assert summary['total_tokens'] == 15000


def test_reexpansion_attribution_matches_status_bucket(m):
    _seed(m)
    conn = m._init_trends_db()
    for detail, tokens in [('abcdef0123456789 (Bash)', 7000), ('toolu_abc (mcp__docs__fetch)', 3000)]:
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail,model) VALUES (?, 'tool_archive_reexpand', ?, ?, ?, ?, ?, 'claude-sonnet-5')", (_ts(), tokens, tokens * .000005, SID, SID, detail))
    conn.commit()
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    bucket = m._realized_savings_buckets(conn, cutoff)['']
    assert bucket['tokens'] == m._get_merged_savings(days=30)['total_tokens']
    assert m._runway_saved_tokens(conn, cutoff) == 15000
    conn.close()


def test_named_mcp_reexpansion_is_not_misclassified_by_hex_key(m):
    _seed(m)
    conn = sqlite3.connect(str(m.TRENDS_DB))
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail,model) VALUES (?, 'tool_archive_reexpand', 3000, .01, ?, ?, ?, 'claude-sonnet-5')", (_ts(), SID, SID, 'abcdef0123456789 (mcp__docs__fetch)'))
    conn.commit(); conn.close()
    summary = m._get_merged_savings(days=30)
    assert summary['by_category']['tool_archive']['tokens_saved'] == 7000
    assert 'bash_archive_reexpand' not in summary['by_category']
