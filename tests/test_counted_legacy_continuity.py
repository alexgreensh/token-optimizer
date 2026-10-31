"""Rows stored before file digests existed are re-scored when it is provable
that they were computed from the earlier part of the same transcript."""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from test_counted_cumulative import (m, _db, _transcript, _add_event, _local_iso,
                                     _iso_z, _utc_now)


def _epoch(utc_naive):
    return utc_naive.replace(tzinfo=timezone.utc).timestamp()


def _append_turns(path, start, indices, model="claude-opus-5-5"):
    with path.open("a") as f:
        for i in indices:
            entry = {"type": "assistant", "timestamp": _iso_z(start + timedelta(minutes=i)),
                     "requestId": f"req-{i}",
                     "message": {"model": model, "usage": {
                         "input_tokens": 2, "cache_read_input_tokens": 1000,
                         "cache_creation_input_tokens": 10}}}
            f.write(json.dumps(entry) + "\n")


def _legacy_setup(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    start = _utc_now() - timedelta(minutes=30)
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=3,
                              model="claude-opus-5-5", start=start)
    written_at = _epoch(start + timedelta(minutes=2, seconds=5))
    os.utime(path, (written_at, written_at))
    _add_event(db, "tool_archive", 1_000_000, _local_iso(start - timedelta(seconds=30)))
    conn = sqlite3.connect(db)
    assert m._update_counted_cumulative(conn)["rows_written"] == 1
    conn.execute("UPDATE counted_reread SET pricing_detail=NULL")
    conn.commit()
    return conn, path, start


def test_pre_digest_row_rescores_after_the_transcript_is_appended(m, tmp_path, monkeypatch):
    conn, path, start = _legacy_setup(m, tmp_path, monkeypatch)
    _append_turns(path, start, [3, 4])
    later = _epoch(start + timedelta(minutes=4, seconds=5))
    os.utime(path, (later, later))
    assert m._update_counted_cumulative(conn)["rows_written"] == 1
    row = conn.execute("SELECT turns_counted, reread_tokens, pricing_detail "
                       "FROM counted_reread").fetchone()
    assert row[0] == 4 and row[1] == 4_000_000
    assert m._counted_pricing_detail(row[2])["file_sha256"]
    summary = m._counted_cumulative_summary()
    assert summary["legacy_unverified_events"] == 0
    assert summary["reread_tokens"] == 4_000_000
    conn.close()


def test_pre_digest_row_keeps_rescoring_across_later_appends(m, tmp_path, monkeypatch):
    conn, path, start = _legacy_setup(m, tmp_path, monkeypatch)
    for step, idx in enumerate(([3], [4], [5]), start=1):
        _append_turns(path, start, idx)
        later = _epoch(start + timedelta(minutes=idx[0], seconds=5))
        os.utime(path, (later, later))
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
    assert conn.execute("SELECT turns_counted FROM counted_reread").fetchone()[0] == 5
    conn.close()


def test_pre_digest_row_does_not_rescore_a_truncated_then_regrown_file(m, tmp_path, monkeypatch):
    conn, path, start = _legacy_setup(m, tmp_path, monkeypatch)
    lines = path.read_text().splitlines()
    # Keep only the first turn (two duplicated lines), then write new turns.
    path.write_text("\n".join(lines[:2]) + "\n")
    _append_turns(path, start, [3, 4, 5])
    later = _epoch(start + timedelta(minutes=5, seconds=5))
    os.utime(path, (later, later))
    assert m._update_counted_cumulative(conn)["rows_written"] == 0
    assert m._counted_cumulative_summary()["legacy_unverified_events"] == 1
    conn.close()


def test_pre_digest_row_does_not_rescore_a_replaced_file_with_no_new_turns(m, tmp_path, monkeypatch):
    conn, path, start = _legacy_setup(m, tmp_path, monkeypatch)
    path.write_bytes(path.read_bytes().replace(b"claude-opus-5-5", b"claude-sonnet-4-5"))
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    assert m._update_counted_cumulative(conn)["rows_written"] == 0
    conn.close()


def _marker_session(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=5, start=datetime(2026, 3, 10, 12))
    marker = json.dumps({"type": "user", "timestamp": _iso_z(start + timedelta(seconds=80)),
                         "message": {"content": "[Full result archived (40,000 chars) - saved]"}})
    lines = path.read_text().splitlines()
    lines.insert(4, marker)
    path.write_text("\n".join(lines) + "\n")
    old = datetime(2026, 3, 11).timestamp()
    os.utime(path, (old, old))
    return db, path, lines, marker


def test_marker_row_without_mtime_is_accepted_when_identity_recomputes_exactly(m, tmp_path, monkeypatch):
    db, path, lines, marker = _marker_session(m, tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert m._counted_marker_backfill(conn)["rows_written"] == 1
        conn.execute("UPDATE counted_reread SET pricing_detail=NULL,transcript_mtime=NULL")
        conn.commit()
        assert m._counted_cumulative_summary()["legacy_unverified_events"] == 1
        result = m._counted_marker_backfill(conn)
        assert result["rows_written"] == 1
        assert m._counted_cumulative_summary()["legacy_unverified_events"] == 0
        detail = m._counted_pricing_detail(conn.execute(
            "SELECT pricing_detail FROM counted_reread").fetchone()[0])
        assert detail["file_sha256"]


def test_marker_row_without_mtime_stays_quarantined_when_identity_differs(m, tmp_path, monkeypatch):
    db, path, lines, marker = _marker_session(m, tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert m._counted_marker_backfill(conn)["rows_written"] == 1
        conn.execute("UPDATE counted_reread SET pricing_detail=NULL,transcript_mtime=NULL,"
                     "reread_tokens=reread_tokens+1")
        conn.commit()
        assert m._counted_marker_backfill(conn)["rows_written"] == 0
        assert m._counted_cumulative_summary()["legacy_unverified_events"] == 1
