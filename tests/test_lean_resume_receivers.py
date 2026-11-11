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
    # A newly indexed transcript is read on the spot: it holds no second delivery,
    # so the receiver stands without waiting for the next collect pass.
    assert lr.resolved(conn) == {1: receiver}
    assert lr.collect(conn, convert, max_files=None)['scanned'] == 1
    assert lr.resolved(conn) == {1: receiver}
    # The receiver's own transcript growing cannot remove a delivered marker, and
    # lines written months after the event cannot be a second delivery of it.
    path.write_text(path.read_text() + '\n')
    assert lr.resolved(conn) == {1: receiver}


# A number that drops to zero whenever a new session is indexed is the failure
# these guard: every proven receiver used to be withdrawn until a full rescan.

TARGET = 'abcd1234-1111-2222-3333-444455556666'
RECEIVER = 'efab5678-1111-2222-3333-444455556666'
CONVERT = staticmethod(lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None))


def _marker(stamp, target=TARGET):
    return json.dumps({'type': 'attachment', 'timestamp': stamp, 'attachment': {
        'type': 'hook_additional_context',
        'content': '[Token Optimizer] Cold-resume lean (session %s, x): work' % target[:8]}}) + '\n'


def _ledger(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.executescript('CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, session_id TEXT, session_uuid TEXT, detail TEXT); '
                       'CREATE TABLE session_log(id INTEGER PRIMARY KEY, session_uuid TEXT, jsonl_path TEXT, date TEXT);')
    conn.execute('INSERT INTO savings_events VALUES (1, ?, ?, ?, ?, ?)', ('2026-01-01T10:00:00', 'resume_lean', TARGET, TARGET, 'old'))
    path = tmp_path / 'receiver.jsonl'
    path.write_text(_marker('2026-01-01T10:00:00Z'))
    conn.execute('INSERT INTO session_log VALUES (1, ?, ?, ?)', (RECEIVER, str(path), '2026-01-01'))
    assert lr.collect(conn, CONVERT.__func__, max_files=None)['matches'] == 1
    assert lr.resolved(conn) == {1: RECEIVER}
    return conn


def _add_session(conn, tmp_path, ident, name, day, text=''):
    path = tmp_path / (name + '.jsonl')
    path.write_text(text)
    conn.execute('INSERT INTO session_log VALUES (?, ?, ?, ?)', (ident, name, str(path), day))
    return path


def test_a_session_that_began_later_does_not_withdraw_a_proven_receiver(tmp_path):
    conn = _ledger(tmp_path)
    _add_session(conn, tmp_path, 2, 'later', '2026-03-10')
    assert lr.resolved(conn) == {1: RECEIVER}


def test_an_unsearched_session_from_the_event_day_is_read_before_it_can_hold_anything_back(tmp_path):
    conn = _ledger(tmp_path)
    _add_session(conn, tmp_path, 2, 'same-day', '2026-01-01', 'no marker here\n')
    assert lr.resolved(conn) == {1: RECEIVER}  # no collect pass needed
    assert lr.collect(conn, CONVERT.__func__, max_files=None)['scanned'] == 1
    assert lr.resolved(conn) == {1: RECEIVER}


def test_an_unsearched_second_delivery_is_caught_before_any_collect_pass(tmp_path):
    conn = _ledger(tmp_path)
    _add_session(conn, tmp_path, 2, 'twin', '2026-01-01', _marker('2026-01-01T10:01:00Z'))
    assert lr.resolved(conn) == {}


def test_beyond_the_inline_read_budget_an_unsearched_session_holds_the_event_back(tmp_path, monkeypatch):
    conn = _ledger(tmp_path)
    _add_session(conn, tmp_path, 2, 'same-day', '2026-01-01', 'no marker here\n')
    monkeypatch.setattr(lr, '_INLINE_FILES', 0)
    assert lr.resolved(conn) == {}
    monkeypatch.setattr(lr, '_INLINE_FILES', 16)
    monkeypatch.setattr(lr, '_INLINE_BYTES', 1)
    assert lr.resolved(conn) == {}


def test_an_unreadable_unsearched_session_holds_the_event_back(tmp_path, monkeypatch):
    conn = _ledger(tmp_path)
    _add_session(conn, tmp_path, 2, 'same-day', '2026-01-01', 'x\n')
    monkeypatch.setattr(lr, '_deliveries', lambda path: None)
    assert lr.resolved(conn) == {}


def test_a_second_receiver_makes_the_event_ambiguous(tmp_path):
    conn = _ledger(tmp_path)
    _add_session(conn, tmp_path, 2, 'twin', '2026-01-01', _marker('2026-01-01T10:01:00Z'))
    lr.collect(conn, CONVERT.__func__, max_files=None)
    assert lr.resolved(conn) == {}


def test_a_new_event_keeps_older_receivers_and_rereads_only_files_that_could_hold_it(tmp_path):
    conn = _ledger(tmp_path)
    import os
    old = tmp_path / 'receiver.jsonl'
    stamp = datetime(2026, 1, 2).timestamp()
    os.utime(old, (stamp, stamp))  # last written the day after the first event
    lr.collect(conn, CONVERT.__func__, max_files=None)
    assert lr.resolved(conn) == {1: RECEIVER}
    other = 'ffff0000-1111-2222-3333-444455556666'
    conn.execute('INSERT INTO savings_events VALUES (2, ?, ?, ?, ?, ?)', ('2026-06-01T10:00:00', 'resume_lean', other, other, 'new'))
    assert lr.resolved(conn) == {1: RECEIVER}  # not withdrawn while the new event is pending
    _add_session(conn, tmp_path, 2, 'june', '2026-06-01', _marker('2026-06-01T10:00:30Z', other))
    stats = lr.collect(conn, CONVERT.__func__, max_files=None)
    assert stats['scanned'] == 1 and stats['matches'] == 1  # the January file is not read again
    assert lr.resolved(conn) == {1: RECEIVER, 2: 'june'}


def test_a_replaced_receiver_transcript_is_not_trusted(tmp_path):
    conn = _ledger(tmp_path)
    (tmp_path / 'receiver.jsonl').write_text('')
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
