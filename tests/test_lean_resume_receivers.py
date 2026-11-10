import json
import sqlite3
import sys
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
