"""Concurrency and interruption regressions for upgrade drains."""
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from test_upgrade_drain_and_remeasure import (m, _db, _transcript, _add_event, _local_iso,
    SID, SCRIPTS, _meta, _load, _noon, _write, _streamed, _projects)

def test_drain_retries_after_three_interrupted_attempts(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setenv("TOKEN_OPTIMIZER_TEST_ALLOW_DRAIN_SPAWN", "1")
    monkeypatch.setattr(m, "_spawn_upgrade_drain", lambda: True)
    with sqlite3.connect(str(db)) as conn:
        m._upgrade_meta_set(conn, m._UPGRADE_DRAIN_ATTEMPTS_META, m._upgrade_drain_key() + ":3")
        assert m._upgrade_drain_after_pass(conn, backlog=True)
        m._upgrade_drain_release_lock()

def test_retry_backoff_is_bounded_and_survives_clock_rollback(m):
    from _measure.upgrade_drain_control import retry_ready

    assert retry_ready("v:99:1000", "v", 1299) == (False, 99)
    assert retry_ready("v:99:1000", "v", 1300) == (True, 99)
    assert retry_ready("v:99:1000", "v", 900) == (True, 99)

def test_failed_spawn_is_also_backed_off(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setenv("TOKEN_OPTIMIZER_TEST_ALLOW_DRAIN_SPAWN", "1")
    calls = []
    monkeypatch.setattr(m, "_spawn_upgrade_drain", lambda: calls.append(1) and True)
    with sqlite3.connect(str(db)) as conn:
        assert not m._upgrade_drain_after_pass(conn, backlog=True)
        assert not m._upgrade_drain_after_pass(conn, backlog=True)
    assert calls == [1]

def test_marker_drain_stops_if_cursor_does_not_advance(m, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_update_counted_cumulative", lambda *a, **kw: {})
    def stalled(*args, **kwargs):
        calls.append(1)
        return {"files_scanned": 1, "done": len(calls) >= 4}
    monkeypatch.setattr(m, "_counted_marker_backfill", stalled)
    monkeypatch.setattr(m, "_purge_counted_without_transcripts", lambda conn: 0)
    monkeypatch.setattr(m, "_counted_stored_rows", lambda conn: {})
    monkeypatch.setattr(m, "_counted_rollup_rows", lambda rows, conn: {})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE token_optimizer_meta (key TEXT PRIMARY KEY, value TEXT)")
    stats = m._counted_backfill_all(conn, quiet=True)
    conn.close()
    assert len(calls) <= 2
    assert not stats["marker_backfill_done"]

def test_marker_checkpoint_never_moves_backward(m):
    from _measure.upgrade_drain_control import commit_marker_cursor

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE token_optimizer_meta (key TEXT PRIMARY KEY, value TEXT)")
    commit_marker_cursor(conn, "cursor", "/b")
    commit_marker_cursor(conn, "cursor", "/a")
    assert conn.execute("SELECT value FROM token_optimizer_meta WHERE key='cursor'").fetchone()[0] == "/b"
    conn.close()

def test_live_drain_lock_is_not_stolen_when_old(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    m.SNAPSHOT_DIR.mkdir()
    lock = m.SNAPSHOT_DIR / m._UPGRADE_DRAIN_LOCK_NAME
    lock.write_text(str(os.getpid()))
    old = lock.stat().st_mtime - m._UPGRADE_DRAIN_LOCK_STALE_SECONDS - 10
    os.utime(lock, (old, old))
    assert not m._upgrade_drain_claim_lock()
    assert lock.read_text() == str(os.getpid())

def test_upgrade_drain_commits_each_session(m):
    assert m._COLLECT_DRAIN_BUDGETS["full"]["commit_batch"] == 1

def test_drain_is_not_marked_done_before_aggregates_commit(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550099"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    monkeypatch.setattr(measure, "_collect_drain_mode", lambda: "full")
    monkeypatch.setattr(measure, "_rebuild_aggregate_tables", lambda conn: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        measure.collect_sessions(days=30, quiet=True)
    assert _meta(measure.TRENDS_DB, measure._UPGRADE_DRAIN_META) != measure._upgrade_drain_key()

def test_drain_is_not_marked_done_after_backfill_error(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550098"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    monkeypatch.setattr(measure, "_collect_drain_mode", lambda: "full")
    monkeypatch.setattr(measure._transcript_backfill, "collect_pass", lambda *a, **kw: {"error": "synthetic"})
    measure.collect_sessions(days=30, quiet=True)
    assert _meta(measure.TRENDS_DB, measure._UPGRADE_DRAIN_META) != measure._upgrade_drain_key()

@pytest.mark.skipif(not hasattr(os, "fork"), reason="kill -9 fixture requires POSIX")
def test_killed_upgrade_drain_resumes_to_same_totals(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    when = _noon()
    for i in range(3):
        sid = "aaaaaaaa-2222-4333-8444-55555555010%d" % i
        records = []
        for turn in range(3):
            records += _streamed(when + timedelta(minutes=turn), 2, req=f"r{i}-{turn}", mid=f"{i}-{turn}", cr=1000)
        _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", records)
    conn = measure._init_trends_db()
    db = measure.TRENDS_DB
    for i in range(3):
        sid = "aaaaaaaa-2222-4333-8444-55555555010%d" % i
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,session_id,session_uuid) "
                     "VALUES (?, 'tool_archive', 1000, ?, ?)", ((when - timedelta(seconds=1)).isoformat(), sid, sid))
    conn.commit()
    baseline = tmp_path / "baseline.db"
    with sqlite3.connect(str(baseline)) as copy:
        conn.backup(copy)
    conn.close()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        original = measure._parse_session_jsonl
        calls = [0]
        def pause_second(path, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                os.write(write_fd, b"ready")
                time.sleep(60)
            return original(path, *args, **kwargs)
        measure._parse_session_jsonl = pause_second
        measure._run_upgrade_drain(days=30, quiet=True)
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 15)[0]
        assert os.read(read_fd, 5) == b"ready"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_log").fetchone()[0] == 1
    measure._run_upgrade_drain(days=30, quiet=True)
    monkeypatch.setattr(measure, "TRENDS_DB", baseline)
    measure._run_upgrade_drain(days=30, quiet=True)
    def totals(path):
        with sqlite3.connect(str(path)) as conn:
            return (conn.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(api_calls) FROM session_log").fetchone(),
                    conn.execute("SELECT COUNT(*), SUM(tokens), SUM(reread_tokens), SUM(reread_usd) FROM counted_reread").fetchone())
    resumed = totals(db)
    assert resumed[0][0] == 3 and resumed[1][0:2] == (3, 3000) and resumed[1][2] > 0
    assert resumed == totals(baseline)

@pytest.mark.skipif(not hasattr(os, "fork"), reason="kill -9 fixture requires POSIX")
def test_only_one_drain_runs_and_kill_releases_lock(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        def active(**kwargs):
            os.write(write_fd, b"ready")
            time.sleep(60)
        m.collect_sessions = active
        m._run_upgrade_drain(days=30, quiet=True)
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0]
        assert os.read(read_fd, 5) == b"ready"
        calls = []
        monkeypatch.setattr(m, "collect_sessions", lambda **kwargs: calls.append(1))
        m._run_upgrade_drain(days=30, quiet=True)
        assert calls == []
        env = dict(os.environ, TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(m.SNAPSHOT_DIR))
        second = subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "upgrade-drain", "--quiet"],
                                env=env, capture_output=True, text=True, timeout=15)
        assert second.returncode == 0, second.stderr
        assert "database is locked" not in second.stderr.lower()
        with sqlite3.connect(str(db)) as conn:
            assert not m._upgrade_drain_after_pass(conn, backlog=False)
        assert _meta(db, m._UPGRADE_DRAIN_META) is None
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    m._run_upgrade_drain(days=30, quiet=True)
    assert calls == [1]

@pytest.mark.skipif(not hasattr(os, "fork"), reason="process lock fixture requires POSIX")
def test_eight_processes_have_one_drain_owner(m, tmp_path, monkeypatch):
    from _measure import upgrade_drain_control

    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    read_fd, write_fd = os.pipe()
    children = []
    for _ in range(8):
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            with upgrade_drain_control.running(m) as held:
                os.write(write_fd, f"{os.getpid()}:{int(held)}\n".encode())
                if held:
                    time.sleep(60)
            os._exit(0)
        children.append(pid)
    os.close(write_fd)
    messages = b""
    try:
        while messages.count(b"\n") < 8:
            assert select.select([read_fd], [], [], 10)[0]
            chunk = os.read(read_fd, 1024)
            assert chunk
            messages += chunk
        assert sum(int(line.split(b":")[1]) for line in messages.splitlines()) == 1
    finally:
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(read_fd)

@pytest.mark.skipif(not hasattr(os, "fork"), reason="kill -9 fixture requires POSIX")
def test_killed_daily_backfill_preserves_each_session(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    for i in range(3):
        sid = "aaaaaaaa-2222-4333-8444-55555555020%d" % i
        _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(when, 2, mid=f"m{i}"))
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET daily_usage_json = NULL")
    conn.commit()
    baseline = tmp_path / "daily-baseline.db"
    with sqlite3.connect(str(baseline)) as copy:
        conn.backup(copy)
    conn.close()
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        original = measure._session_activity_evidence
        calls = [0]
        def pause_second(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                os.write(write_fd, b"ready")
                time.sleep(60)
            return original(*args, **kwargs)
        measure._session_activity_evidence = pause_second
        with sqlite3.connect(str(measure.TRENDS_DB)) as child_conn:
            measure._backfill_daily_usage(child_conn, days=30, budget_seconds=60)
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0]
        assert os.read(read_fd, 5) == b"ready"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    with sqlite3.connect(str(measure.TRENDS_DB)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_log WHERE daily_usage_json IS NOT NULL").fetchone()[0] == 1
        measure._backfill_daily_usage(conn, days=30, budget_seconds=60)
        actual = conn.execute("SELECT jsonl_path,daily_usage_json FROM session_log ORDER BY jsonl_path").fetchall()
    with sqlite3.connect(str(baseline)) as conn:
        measure._backfill_daily_usage(conn, days=30, budget_seconds=60)
        expected = conn.execute("SELECT jsonl_path,daily_usage_json FROM session_log ORDER BY jsonl_path").fetchall()
    assert actual == expected

@pytest.mark.skipif(not hasattr(os, "fork"), reason="kill -9 fixture requires POSIX")
def test_killed_marker_backfill_preserves_each_session(m, tmp_path, monkeypatch):
    from datetime import datetime
    from test_counted_cumulative import _iso_z

    db = _db(m, tmp_path, monkeypatch)
    start = datetime(2026, 3, 10, 12, 0, 0)
    for i in range(2):
        sid = "0ad00000-1111-2222-3333-44445555666%d" % i
        path, _ = _transcript(m, tmp_path, monkeypatch, sid=sid, n_turns=6, start=start)
        lines = path.read_text().splitlines()
        lines.insert(4, json.dumps({"type": "user", "timestamp": _iso_z(start + timedelta(seconds=80)),
                                    "message": {"content": "[Full result archived (40,000 chars) - saved]"}}))
        path.write_text("\n".join(lines) + "\n")
        old = datetime(2026, 3, 11).timestamp()
        os.utime(path, (old, old))
    baseline = tmp_path / "marker-baseline.db"
    with sqlite3.connect(str(db)) as source, sqlite3.connect(str(baseline)) as copy:
        source.backup(copy)
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        original = m._counted_walk_transcript
        calls = [0]
        def pause_second(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                os.write(write_fd, b"ready")
                time.sleep(60)
            return original(*args, **kwargs)
        m._counted_walk_transcript = pause_second
        with sqlite3.connect(str(db)) as conn:
            m._counted_marker_backfill(conn, quiet=True)
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0]
        assert os.read(read_fd, 5) == b"ready"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM counted_reread").fetchone()[0] == 1
        m._counted_marker_backfill(conn, quiet=True)
        actual = conn.execute("SELECT event_key,tokens,reread_tokens FROM counted_reread ORDER BY event_key").fetchall()
    with sqlite3.connect(str(baseline)) as conn:
        m._counted_marker_backfill(conn, quiet=True)
        expected = conn.execute("SELECT event_key,tokens,reread_tokens FROM counted_reread ORDER BY event_key").fetchall()
    assert actual == expected

def test_daily_backfill_does_not_overwrite_newer_session_data(m, monkeypatch):
    from _measure.upgrade_drain_control import DailyUpdates

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE session_log (jsonl_path TEXT PRIMARY KEY, daily_usage_json TEXT, "
                 "session_uuid TEXT, first_activity_day TEXT, last_activity_day TEXT, date TEXT, "
                 "parent_daily_usage_json TEXT, daily_calls_json TEXT, daily_metrics_json TEXT)")
    conn.execute("INSERT INTO session_log(jsonl_path) VALUES ('x')")
    conn.commit()
    monkeypatch.setattr(m, "_collect_drain_mode", lambda: "inline")
    updates = DailyUpdates(conn, m)
    updates.append(("old", None, None, None, "", None, None, None, "x"))
    conn.execute("UPDATE session_log SET daily_usage_json='new' WHERE jsonl_path='x'")
    conn.commit()
    updates.flush()
    assert conn.execute("SELECT daily_usage_json FROM session_log").fetchone()[0] == "new"
    conn.close()

def test_drain_owner_handoff_is_never_partial(m, tmp_path, monkeypatch):
    from _measure import upgrade_drain_control

    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    m.SNAPSHOT_DIR.mkdir()
    marker = m.SNAPSHOT_DIR / m._UPGRADE_DRAIN_LOCK_NAME
    owner = str(os.getpid())
    marker.write_text(owner)
    stop = threading.Event()
    bad = []
    def read_loop():
        while not stop.is_set():
            try:
                value = marker.read_text()
            except OSError:
                value = None
            if value != owner:
                bad.append(value)
                return
    reader = threading.Thread(target=read_loop)
    reader.start()
    try:
        for _ in range(200):
            upgrade_drain_control.publish_child(m, os.getpid())
    finally:
        stop.set()
        reader.join(timeout=5)
    assert not bad

def test_old_dead_upgrade_marker_temporary_is_cleaned(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    m.SNAPSHOT_DIR.mkdir()
    stale = m.SNAPSHOT_DIR / ".upgrade-drain.lock.2147483647.abcdefgh"
    stale.write_text("old")
    old = time.time() - 90000
    os.utime(stale, (old, old))
    fresh = m.SNAPSHOT_DIR / ".upgrade-drain.lock.2147483647.abcdefgi"
    fresh.write_text("fresh")
    assert m._upgrade_drain_claim_lock()
    m._upgrade_drain_release_lock()
    assert not stale.exists()
    assert fresh.exists()

def test_incomplete_marker_backfill_triggers_upgrade_drain(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550097"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    monkeypatch.setattr(measure, "_collect_drain_mode", lambda: None)
    monkeypatch.setattr(measure._transcript_backfill, "collect_pass", lambda *a, **kw: {"pending": 0})
    monkeypatch.setattr(measure, "_update_counted_cumulative", lambda *a, **kw: {"pending_sessions": 0})
    monkeypatch.setattr(measure, "_counted_marker_backfill", lambda *a, **kw: {"done": False, "files_scanned": 1})
    calls = []
    monkeypatch.setattr(measure, "_upgrade_drain_after_pass", lambda conn, backlog: calls.append(backlog))
    measure.collect_sessions(days=30, quiet=True)
    assert calls == [True]

def test_nested_backfill_pending_triggers_upgrade_drain(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550096"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    monkeypatch.setattr(measure, "_collect_drain_mode", lambda: None)
    monkeypatch.setattr(measure._transcript_backfill, "collect_pass", lambda *a, **kw: {"pending": 0, "fixture_void": {"pending": 1}})
    monkeypatch.setattr(measure, "_update_counted_cumulative", lambda *a, **kw: {"pending_sessions": 0})
    monkeypatch.setattr(measure, "_counted_marker_backfill", lambda *a, **kw: {"done": True, "files_scanned": 0})
    calls = []
    monkeypatch.setattr(measure, "_upgrade_drain_after_pass", lambda conn, backlog: calls.append(backlog))
    measure.collect_sessions(days=30, quiet=True)
    assert calls == [True]

def test_session_log_insert_retries_transient_lock(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550095"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    measure._init_trends_db().close()
    real_init = measure._init_trends_db
    attempts = []

    class ContendedConnection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO session_log\n"):
                attempts.append(1)
                if len(attempts) <= 2:
                    raise sqlite3.OperationalError("database is locked")
            return self.conn.execute(sql, *args)

    monkeypatch.setattr(measure, "_init_trends_db", lambda: ContendedConnection(real_init()))
    measure.collect_sessions(days=30, quiet=True)
    with sqlite3.connect(str(measure.TRENDS_DB)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_log WHERE session_uuid=?", (sid,)).fetchone()[0] == 1
    assert len(attempts) == 3

def test_persistent_session_log_lock_is_deferred_without_crash(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550094"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    measure._init_trends_db().close()
    real_init = measure._init_trends_db
    attempts, pending = [], []

    class ContendedConnection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO session_log\n"):
                attempts.append(1)
                raise sqlite3.OperationalError("database is locked")
            return self.conn.execute(sql, *args)

    monkeypatch.setattr(measure, "_init_trends_db", lambda: ContendedConnection(real_init()))
    monkeypatch.setattr(measure, "_upgrade_drain_after_pass", lambda conn, backlog: pending.append(backlog))
    start = time.monotonic()
    measure.collect_sessions(days=30, quiet=True)
    assert len(attempts) <= 6 and time.monotonic() - start < 5 and pending == [True]
    with sqlite3.connect(str(measure.TRENDS_DB)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_log WHERE session_uuid=?", (sid,)).fetchone()[0] == 0

def test_failed_session_detail_update_rolls_back_partial_row(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    sid = "aaaaaaaa-2222-4333-8444-555555550093"
    _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(_noon(), 2, mid="m"))
    measure._init_trends_db().close()
    real_init = measure._init_trends_db
    pending = []
    class ContendedConnection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def execute(self, sql, *args):
            if sql.startswith("UPDATE session_log SET first_activity_day="):
                raise sqlite3.OperationalError("database is locked")
            return self.conn.execute(sql, *args)
    monkeypatch.setattr(measure, "_init_trends_db", lambda: ContendedConnection(real_init()))
    monkeypatch.setattr(measure, "_upgrade_drain_after_pass", lambda conn, backlog: pending.append(backlog))
    measure.collect_sessions(days=30, quiet=True)
    with sqlite3.connect(str(measure.TRENDS_DB)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_log WHERE session_uuid=?", (sid,)).fetchone()[0] == 0
    assert pending == [True]

def test_real_sqlite_session_write_retries_until_lock_is_released(tmp_path):
    db = tmp_path / "locked.db"
    blocker = sqlite3.connect(str(db))
    blocker.execute("CREATE TABLE session_log (id INTEGER PRIMARY KEY)")
    blocker.execute("BEGIN IMMEDIATE")
    code = '''
import sqlite3, sys
from _measure.upgrade_drain_control import session_write
class Observer:
    def __init__(self, conn): self.conn = conn
    def __getattr__(self, name): return getattr(self.conn, name)
    def execute(self, sql, *args):
        try: return self.conn.execute(sql, *args)
        except sqlite3.OperationalError:
            print("locked", flush=True)
            raise
c = Observer(sqlite3.connect(sys.argv[1], timeout=0))
result = session_write(c, "INSERT INTO session_log(id) VALUES (?)", (1,))
if result is None: sys.exit(3)
c.commit()
'''
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    child = subprocess.Popen([sys.executable, "-c", code, str(db)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
    finally:
        blocker.rollback()
        blocker.close()
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, (stdout, stderr)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT id FROM session_log").fetchall() == [(1,)]

def test_drain_defers_unexpected_sqlite_contention(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "collect_sessions", locked)
    assert m._run_upgrade_drain(days=30, quiet=True) == 0


def test_collect_command_defers_sqlite_contention(m, monkeypatch):
    def locked(**kwargs):
        raise sqlite3.OperationalError("session_log write deferred: database is locked")
    monkeypatch.setattr(m, "collect_sessions", locked)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    m._dispatch_collect(["collect", "--quiet"])

def test_usage_version_stamp_retries_transient_lock(m):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE session_log (jsonl_path TEXT PRIMARY KEY, usage_parser_version INTEGER)")
    conn.execute("INSERT INTO session_log (jsonl_path) VALUES ('path')")
    conn.commit()
    attempts = []
    class ContendedConnection:
        def __getattr__(self, name): return getattr(conn, name)
        def execute(self, sql, *args):
            if sql.startswith("UPDATE session_log SET usage_parser_version"):
                attempts.append(1)
                if len(attempts) <= 2:
                    raise sqlite3.OperationalError("database is locked")
            return conn.execute(sql, *args)
    m._stamp_usage_parser_version(ContendedConnection(), "path")
    assert len(attempts) == 3
    assert conn.execute("SELECT usage_parser_version FROM session_log").fetchone()[0] == m._USAGE_PARSER_VERSION
    conn.close()

@pytest.mark.parametrize("bulk", [False, True])
def test_trends_connection_retries_other_session_updates(m, tmp_path, monkeypatch, bulk):
    monkeypatch.setattr(m, "TRENDS_DB", tmp_path / "trends.db")
    setup = m._connect_trends_db()
    setup.execute("CREATE TABLE session_log (id INTEGER PRIMARY KEY, value INTEGER)")
    setup.executemany("INSERT INTO session_log VALUES (?, 0)", [(1,), (2,), (3,)])
    setup.commit()
    setup.close()
    ready, start, attempted = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def writer():
        conn = m._connect_trends_db()
        conn.execute("PRAGMA busy_timeout=0")
        conn.set_trace_callback(lambda sql: attempted.set() if sql.startswith("UPDATE session_log") else None)
        ready.set()
        start.wait(5)
        try:
            if bulk:
                conn.executemany("UPDATE session_log SET value=? WHERE id=?", ((i, i) for i in range(1, 4)))
            else:
                conn.execute("UPDATE session_log SET value=1 WHERE id=1")
            conn.commit()
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()
    thread = threading.Thread(target=writer)
    thread.start()
    assert ready.wait(5)
    blocker = sqlite3.connect(str(m.TRENDS_DB))
    blocker.execute("BEGIN IMMEDIATE")
    try:
        start.set()
        assert attempted.wait(5)
        time.sleep(0.12)
    finally:
        blocker.rollback()
        blocker.close()
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    with sqlite3.connect(str(m.TRENDS_DB)) as conn:
        assert conn.execute("SELECT value FROM session_log ORDER BY id").fetchall() == ([(1,), (2,), (3,)] if bulk else [(1,), (0,), (0,)])

def test_interrupted_batch_retry_does_not_duplicate_first_update(m, tmp_path, monkeypatch):
    from _measure import upgrade_drain_control as control

    monkeypatch.setattr(m, "TRENDS_DB", tmp_path / "trends.db")
    conn = m._connect_trends_db()
    conn.execute("CREATE TABLE session_log (id INTEGER PRIMARY KEY, value INTEGER)")
    conn.executemany("INSERT INTO session_log VALUES (?, 0)", [(1,), (2,)])
    conn.commit()
    original = control._RawConnection.executemany
    attempts = []
    def interrupted(raw, sql, rows):
        attempts.append(1)
        if len(attempts) == 1:
            sqlite3.Connection.execute(raw.conn, sql, next(rows))
            raise sqlite3.OperationalError("database is locked")
        return original(raw, sql, rows)
    monkeypatch.setattr(control._RawConnection, "executemany", interrupted)
    conn.executemany("UPDATE session_log SET value=value+1 WHERE id=?", ((i,) for i in (1, 2)))
    conn.commit()
    assert conn.execute("SELECT value FROM session_log ORDER BY id").fetchall() == [(1,), (1,)]
    assert len(attempts) == 2
    conn.close()
