import json
import os
import select
import signal
import sqlite3
import sys
import time
import pytest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import lean_resume_receivers as lr


def test_historical_receiver_requires_unique_delivered_hook(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.executescript('CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, session_id TEXT, session_uuid TEXT, detail TEXT); CREATE TABLE session_log(id INTEGER PRIMARY KEY, session_uuid TEXT, jsonl_path TEXT);')
    target = 'abcd1234-1111-2222-3333-444455556666'
    receiver = 'efab5678-1111-2222-3333-444455556666'
    conn.execute('INSERT INTO savings_events VALUES (1, ?, ?, ?, ?, ?)', ('2026-01-01T10:00:00', 'resume_lean', target, target, 'old'))
    path = tmp_path / 'receiver.jsonl'
    obj = {'type': 'attachment', 'timestamp': '2026-01-01T10:00:00Z', 'attachment': {'type': 'hook_additional_context', 'content': '[Token Optimizer] Cold-resume lean (session abcd1234, 2026-01-01): work'}}
    path.write_text(json.dumps(obj) + '\n')
    conn.execute('INSERT INTO session_log VALUES (1, ?, ?)', (receiver, str(path)))
    convert = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)
    assert lr.collect(conn, convert, max_files=None)['matches'] == 1
    assert lr.resolved(conn) == {1: receiver}
    extra = tmp_path / 'later.jsonl'
    extra.write_text('')
    conn.execute('INSERT INTO session_log VALUES (2, ?, ?)', ('other', str(extra)))
    assert lr.resolved(conn) == {}
    assert lr.collect(conn, convert, max_files=None)['scanned'] == 1
    assert lr.resolved(conn) == {1: receiver}
    path.write_text(path.read_text() + '\n')
    assert lr.resolved(conn) == {}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="kill -9 fixture requires POSIX")
def test_killed_receiver_scan_keeps_completed_sessions(tmp_path, monkeypatch):
    db = tmp_path / "receiver.db"
    conn = sqlite3.connect(str(db))
    conn.executescript('CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, session_id TEXT, session_uuid TEXT, detail TEXT); CREATE TABLE session_log(id INTEGER PRIMARY KEY, session_uuid TEXT, jsonl_path TEXT);')
    target = 'abcd1234-1111-2222-3333-444455556666'
    conn.execute('INSERT INTO savings_events VALUES (1, ?, ?, ?, ?, ?)', ('2026-01-01T10:00:00', 'resume_lean', target, target, 'old'))
    for i in range(2):
        path = tmp_path / f'receiver-{i}.jsonl'
        obj = {'type': 'attachment', 'timestamp': '2026-01-01T10:00:00Z', 'attachment': {'type': 'hook_additional_context', 'content': '[Token Optimizer] Cold-resume lean (session abcd1234, 2026-01-01): work'}}
        path.write_text(json.dumps(obj) + '\n')
        conn.execute('INSERT INTO session_log VALUES (?, ?, ?)', (i + 1, f'receiver-{i}', str(path)))
    conn.commit()
    baseline = tmp_path / 'baseline.db'
    with sqlite3.connect(str(baseline)) as copy:
        conn.backup(copy)
    conn.close()
    convert = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)
    monkeypatch.setenv('TOKEN_OPTIMIZER_UPGRADE_DRAIN', '1')
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        original = lr._stat_matches
        calls = [0]
        def pause_second(*args):
            calls[0] += 1
            if calls[0] == 2:
                os.write(write_fd, b'ready')
                time.sleep(60)
            return original(*args)
        lr._stat_matches = pause_second
        with sqlite3.connect(str(db)) as child:
            lr.collect(child, convert, max_files=None)
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0]
        assert os.read(read_fd, 5) == b'ready'
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lean_receiver_scans').fetchone()[0] == 1
        lr.collect(conn, convert, max_files=None)
        actual = conn.execute('SELECT event_id,receiver FROM lean_receiver_candidates ORDER BY receiver').fetchall()
    with sqlite3.connect(str(baseline)) as conn:
        lr.collect(conn, convert, max_files=None)
        expected = conn.execute('SELECT event_id,receiver FROM lean_receiver_candidates ORDER BY receiver').fetchall()
    assert actual == expected
