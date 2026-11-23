"""Tests for SPEC-local-adapt Parts A (local self-pause) and B (fixture ratchet)."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
for p in (str(SCRIPTS), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _measure import local_adapt as la  # noqa: E402
import check_fixture_ratchet as cfr  # noqa: E402
import replay_critical_vectors as rcv  # noqa: E402


def _make_ledger(replacements: int, reexpands: int, feature: str = "bash_compress_pipeline",
                 now: datetime | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE compression_events (feature TEXT, timestamp TEXT, tier TEXT)")
    conn.execute("CREATE TABLE savings_events (event_type TEXT, timestamp TEXT, detail TEXT)")
    ts = (now or datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)).isoformat()
    for _ in range(replacements):
        conn.execute("INSERT INTO compression_events VALUES (?, ?, 'measured')", (feature, ts))
    for i in range(reexpands):
        conn.execute("INSERT INTO savings_events VALUES ('tool_archive_reexpand', ?, ?)",
                     (ts, f"key_{i} ({feature})"))
    return conn


def test_local_adapt_under_20_replacements_no_action(tmp_path):
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    conn = _make_ledger(19, 10, now=now)
    sp = tmp_path / "local-adapt-state.json"
    la.evaluate_session_start(conn, sp, ["bash_compress_pipeline"], plugin_version="5.14.0", now=now)
    st = la.read_hot_path_state(sp, "bash_compress_pipeline")
    assert st["paused"] is False
    assert st["min_size_mult"] == 1
    assert sp.stat().st_size <= la.MAX_STATE_BYTES


def test_local_adapt_tighten_at_20pct_and_pause_at_30pct_with_clock(tmp_path):
    t0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    sp = tmp_path / "local-adapt-state.json"

    # 20% unfold rate -> tightens once, not paused
    conn20 = _make_ledger(20, 4, now=t0)
    la.evaluate_session_start(conn20, sp, ["bash_compress_pipeline"], plugin_version="5.14.0", now=t0)
    st = la.read_hot_path_state(sp, "bash_compress_pipeline")
    assert st["paused"] is False
    assert st["min_size_mult"] == 2
    assert st["keep_lines_mult"] == 2

    # 30%+ unfold rate -> paused for 14 days
    conn30 = _make_ledger(24, 9, now=t0)
    la.evaluate_session_start(conn30, sp, ["bash_compress_pipeline"], plugin_version="5.14.0", now=t0)
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is True
    assert la.paused_summary(sp) == ["paused here: bash_compress_pipeline, unfolded 9 of 24"]

    # Day 10: still paused
    t10 = t0 + timedelta(days=10)
    la.evaluate_session_start(_make_ledger(0, 0, now=t10), sp, ["bash_compress_pipeline"],
                              plugin_version="5.14.0", now=t10)
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is True

    # Day 15: pause expired -> resumes at tightened step
    t15 = t0 + timedelta(days=15)
    la.evaluate_session_start(_make_ledger(0, 0, now=t15), sp, ["bash_compress_pipeline"],
                              plugin_version="5.14.0", now=t15)
    st15 = la.read_hot_path_state(sp, "bash_compress_pipeline")
    assert st15["paused"] is False
    assert st15["min_size_mult"] == 2
    assert st15["keep_lines_mult"] == 2

    # Second pause -> stays off until next plugin version
    la.evaluate_session_start(_make_ledger(25, 10, now=t15), sp, ["bash_compress_pipeline"],
                              plugin_version="5.14.0", now=t15)
    t40 = t15 + timedelta(days=25)
    la.evaluate_session_start(_make_ledger(0, 0, now=t40), sp, ["bash_compress_pipeline"],
                              plugin_version="5.14.0", now=t40)
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is True

    # Next plugin version arrives -> resumes tightened
    la.evaluate_session_start(_make_ledger(0, 0, now=t40), sp, ["bash_compress_pipeline"],
                              plugin_version="5.14.1", now=t40)
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is False


def test_local_adapt_corrupt_or_oversize_state_means_defaults_and_env_override(tmp_path, monkeypatch):
    sp = tmp_path / "local-adapt-state.json"
    sp.write_text("{corrupt json", encoding="utf-8")
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is False

    sp.write_text("x" * (la.MAX_STATE_BYTES + 10), encoding="utf-8")
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is False

    monkeypatch.setenv("TOKEN_OPTIMIZER_BASH_COMPRESS_PIPELINE", "0")
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is True
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASH_COMPRESS_PIPELINE", "1")
    assert la.read_hot_path_state(sp, "bash_compress_pipeline")["paused"] is False


def test_fixture_ratchet_rules():
    assert cfr.check_counts(Counter({"bash_compress": 10}), Counter({"bash_compress": 10})) == []
    assert cfr.check_counts(Counter({"bash_compress": 9}), Counter({"bash_compress": 10}))
    assert cfr.check_commit_touch(["skills/token-optimizer/scripts/bash_compress.py"], "fix bug")
    assert cfr.check_commit_touch(
        ["skills/token-optimizer/scripts/bash_compress.py"],
        "fix bug\nfixture: none needed (refactor only)",
    ) == []
    assert cfr.check_commit_touch(
        ["skills/token-optimizer/scripts/bash_compress.py",
         "skills/token-optimizer/scripts/fixtures/c7_review_ratchet.json"],
        "fix bug",
    ) == []


def test_replay_gate_flags_hidden_nonzero_exit_status():
    raw = "\n".join([f"step {i} ok" for i in range(30)])
    emitted = "step 0 ok\n... 28 lines omitted ...\nstep 29 ok"
    res = rcv.check_event(raw, emitted, exit_code=2)
    assert res["CRITICAL_HIDDEN"] >= 1
