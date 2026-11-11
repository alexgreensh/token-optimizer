"""A session rollup's live occupancy must refresh a row an earlier collect locked.

WHY THIS EXISTS
---------------
Hermes collection dedups first-write-wins on ``hermes:<slug>``: a mid-session
/token-optimizer collect (or a sibling session's rollup) stores the row with
occupancy unavailable, and the session's own end-rollup — the only collector
carrying the real provider-reported prompt size — then hits the dedup guard
and vanishes. The stored grade stays fill-blind for the life of the DB. A
rollup that supplies context_tokens now refreshes the stored quality columns
in place instead of being dropped by the dedup.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hermes_state  # noqa: E402
import measure  # noqa: E402

NOW = time.time()


def _session_row(**over):
    row = {
        "id": "sess-live", "model": "gpt-4o",
        "started_at": NOW - 60, "ended_at": NOW,
        "message_count": 12,
        "input_tokens": 20_000, "output_tokens": 4_000,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_status": "unknown",
    }
    row.update(over)
    return row


@pytest.fixture()
def trends_db(tmp_path, monkeypatch):
    """Redirect the trends DB at a tmp file and stub the Hermes state source."""
    db = tmp_path / "trends.db"
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        hermes_state, "recent_sessions",
        lambda days=30, max_rows=2000: [_session_row()],
    )
    monkeypatch.setattr(measure, "_HERMES_ROLLUP_CONTEXT", {}, raising=False)
    return db


def _stored_quality(db, slug="sess-live"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT quality_score, quality_grade FROM session_log WHERE jsonl_path = ?",
            (f"hermes:{slug}",),
        ).fetchone()
    finally:
        conn.close()


def test_rollup_refreshes_row_locked_by_earlier_collect(trends_db, monkeypatch):
    # V3 marks missing occupancy unmeasured rather than grading it S.
    assert measure._collect_hermes_sessions(quiet=True) == 1
    assert _stored_quality(trends_db) == (100, "?")

    # The session's own rollup later carries the real last-prompt occupancy:
    # 180k prompt against gpt-4o's 128k window clamps fill to 1.0.
    # The shared v3 scorer returns 28.6/F. No new row is inserted — the dedup
    # key is taken — but the stored quality must still move to the real grade.
    monkeypatch.setattr(
        measure, "_HERMES_ROLLUP_CONTEXT", {"sess-live": 180_000}, raising=False
    )
    assert measure._collect_hermes_sessions(quiet=True) == 0
    assert _stored_quality(trends_db) == (28.6, "F")


def test_rollup_without_occupancy_does_not_overwrite(trends_db, monkeypatch):
    """A rollup that carries no occupancy must leave an existing row alone —
    dedup still wins when there is nothing new to contribute."""
    monkeypatch.setattr(
        measure, "_HERMES_ROLLUP_CONTEXT", {"sess-live": 180_000}, raising=False
    )
    assert measure._collect_hermes_sessions(quiet=True) == 1
    assert _stored_quality(trends_db) == (28.6, "F")

    # A later collect with no live reading must not downgrade the stored grade.
    monkeypatch.setattr(measure, "_HERMES_ROLLUP_CONTEXT", {}, raising=False)
    assert measure._collect_hermes_sessions(quiet=True) == 0
    assert _stored_quality(trends_db) == (28.6, "F")


def test_rollup_context_arg_requires_positive_int():
    """--context-tokens feeds a stored score: only a positive integer is a real
    reading. Zero, negative, or non-numeric values degrade to 'unavailable'."""
    parse = measure._parse_hermes_rollup_context
    assert parse(["--session", "s1", "--context-tokens", "278545"]) == {"s1": 278545}
    for bad in ("0", "-5", "abc", "12.5"):
        assert parse(["--session", "s1", "--context-tokens", bad]) == {}, bad
    assert parse(["--session", "s1"]) == {}
    assert parse([]) == {}
