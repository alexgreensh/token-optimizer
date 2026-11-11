"""Archive void evidence is date-generic.

A tool_archive row is voided whenever its own transcript holds the full
result, in any month. Sessions memoized by the old August-only scan are
rescanned once so their other months are not skipped.
"""
import importlib
import json
import os
import select
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._init_trends_db().close()
    yield mod
    sys.modules.pop("measure", None)


def _transcript(path, when, size, tool="mcp__x__fetch", tid="t1"):
    records = [
        {"type": "assistant", "timestamp": when.isoformat(), "message": {"content": [{"type": "tool_use", "id": tid, "name": tool}]}},
        {"type": "user", "timestamp": when.isoformat(), "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "a" * size}]}},
    ]
    with path.open("a") as stream:
        stream.write("\n".join(map(json.dumps, records)) + "\n")


def _archive(conn, when, chars, sid=SID):
    conn.execute("INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail) VALUES (?,?,?,?,?,?)",
                 (when.astimezone().replace(tzinfo=None).isoformat(), "tool_archive", 1000, 0.003, sid,
                  "replaced mcp__x__fetch (%s chars -> 300 chars)" % format(chars, ",")))


def _counted(m, conn):
    return conn.execute("SELECT count(*) FROM " + m._counted_table(conn, "savings_events")).fetchone()[0]


@pytest.mark.parametrize("month", [6, 7, 9, 11])
def test_full_result_outside_august_is_voided(m, tmp_path, month):
    from _measure import archive_void_evidence as av
    when = datetime(2026, month, 18, 11, 0, tzinfo=timezone.utc)
    path = tmp_path / "s.jsonl"
    _transcript(path, when, 4000)
    conn = m._init_trends_db()
    conn.execute("INSERT INTO session_log (session_uuid,jsonl_path,date) VALUES (?,?,?)", (SID, str(path), when.date().isoformat()))
    _archive(conn, when, 4000)
    conn.commit()
    assert av.collect(conn, max_sessions=None)["proven"] == 1
    assert conn.execute("SELECT count(*) FROM savings_events").fetchone()[0] == 1
    conn.close()
    conn = m._init_trends_db()
    assert _counted(m, conn) == 0
    conn.close()


def test_pointer_in_transcript_keeps_row_in_any_month(m, tmp_path):
    from _measure import archive_void_evidence as av
    when = datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc)
    path = tmp_path / "s.jsonl"
    _transcript(path, when, 300)            # the pointer, not the full result
    conn = m._init_trends_db()
    conn.execute("INSERT INTO session_log (session_uuid,jsonl_path,date) VALUES (?,?,?)", (SID, str(path), "2026-07-18"))
    _archive(conn, when, 4000)
    conn.commit()
    assert av.collect(conn, max_sessions=None)["proven"] == 0
    conn.close()
    conn = m._init_trends_db()
    assert _counted(m, conn) == 1
    conn.close()


def test_session_memoized_by_august_scan_is_rescanned_for_july(m, tmp_path):
    from _measure import archive_void_evidence as av
    july = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)
    august = datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)
    path = tmp_path / "s.jsonl"
    _transcript(path, july, 4000, tid="t1")
    _transcript(path, august, 300, tid="t2")
    conn = m._init_trends_db()
    conn.execute("INSERT INTO session_log (session_uuid,jsonl_path,date) VALUES (?,?,?)", (SID, str(path), "2026-07-30"))
    _archive(conn, july, 4000)
    _archive(conn, august, 4000)
    # An older build already scanned this exact file for August rows only.
    conn.execute("CREATE TABLE archive_void_scans_v2 (session_id TEXT, parent TEXT, fingerprint TEXT, PRIMARY KEY(session_id,parent,fingerprint))")
    conn.execute("INSERT INTO archive_void_scans_v2 VALUES (?,?,?)", (SID, str(path), av.session_fingerprint(str(path))))
    conn.commit()
    assert av.collect(conn, max_sessions=None)["proven"] == 1
    assert av.collect(conn, max_sessions=None)["scanned"] == 0
    conn.close()
    conn = m._init_trends_db()
    assert _counted(m, conn) == 1
    conn.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="kill -9 fixture requires POSIX")
def test_killed_archive_scan_preserves_completed_sessions(m, tmp_path, monkeypatch):
    from _measure import archive_void_evidence as av

    when = datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc)
    conn = m._init_trends_db()
    for i in range(2):
        sid = f"11111111-2222-3333-4444-55555555555{i}"
        path = tmp_path / f"archive-{i}.jsonl"
        _transcript(path, when, 4000)
        conn.execute("INSERT INTO session_log (session_uuid,jsonl_path,date) VALUES (?,?,?)", (sid, str(path), "2026-07-18"))
        _archive(conn, when, 4000, sid=sid)
    conn.commit()
    baseline = tmp_path / "archive-baseline.db"
    with sqlite3.connect(str(baseline)) as copy:
        conn.backup(copy)
    conn.close()
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        original = av.full_result_ids
        calls = [0]
        def pause_second(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                os.write(write_fd, b"ready")
                time.sleep(60)
            return original(*args, **kwargs)
        av.full_result_ids = pause_second
        with sqlite3.connect(str(m.TRENDS_DB)) as child:
            av.collect(child, max_sessions=None)
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0]
        assert os.read(read_fd, 5) == b"ready"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    with sqlite3.connect(str(m.TRENDS_DB)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_void_scans_v3").fetchone()[0] == 1
        av.collect(conn, max_sessions=None)
        actual = conn.execute("SELECT event_id,parent FROM archive_void_evidence_v2 ORDER BY event_id").fetchall()
    with sqlite3.connect(str(baseline)) as conn:
        av.collect(conn, max_sessions=None)
        expected = conn.execute("SELECT event_id,parent FROM archive_void_evidence_v2 ORDER BY event_id").fetchall()
    assert actual == expected
