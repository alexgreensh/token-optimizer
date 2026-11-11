"""Store retention must account for activity recorded in a WAL file."""
import os
import errno
import select
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import session_store
import session_store_guard


def test_recent_wal_protects_old_database(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "SESSION_STORE_DIR", tmp_path)
    db = tmp_path / "active.db"
    wal = tmp_path / "active.db-wal"
    db.write_text("database")
    wal.write_text("recent activity")
    old = time.time() - 72 * 3600
    os.utime(db, (old, old))
    assert session_store.cleanup_old_stores() == 0
    assert db.exists() and wal.exists()


def test_cleanup_cannot_unlink_a_store_with_an_active_connection(tmp_path, monkeypatch):
    store = session_store.SessionStore("active", snapshot_dir=tmp_path)
    store.set_meta("synthetic", "kept")
    monkeypatch.setattr(session_store, "SESSION_STORE_DIR", store.db_path.parent)
    old = time.time() - 72 * 3600
    os.utime(store.db_path, (old, old))
    wal = store.db_path.with_suffix(".db-wal")
    original_exists = Path.exists
    def racing_exists(path):
        return False if path == wal else original_exists(path)
    try:
        with patch.object(Path, "exists", racing_exists):
            assert session_store.cleanup_old_stores() == 0
        assert store.db_path.exists()
        assert store.get_meta("synthetic") == "kept"
    finally:
        store.close()


def test_cleanup_respects_a_store_open_in_another_process(tmp_path, monkeypatch):
    if session_store_guard.fcntl is None:
        return
    script = ("import sys; from pathlib import Path; from session_store import SessionStore; "
              "s=SessionStore('cross-process', snapshot_dir=Path(sys.argv[1])); "
              "s.set_meta('synthetic','kept'); print('ready',flush=True); sys.stdin.readline(); s.close()")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts")
    proc = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert select.select([proc.stdout], [], [], 10)[0] and proc.stdout.readline().strip() == "ready"
        db = tmp_path / "session-store" / "cross-process.db"
        monkeypatch.setattr(session_store, "SESSION_STORE_DIR", db.parent)
        old = time.time() - 72 * 3600
        os.utime(db, (old, old))
        wal = db.with_suffix(".db-wal")
        original_exists = Path.exists
        with patch.object(Path, "exists", lambda path: False if path == wal else original_exists(path)):
            assert session_store.cleanup_old_stores() == 0
        assert db.exists()
    finally:
        proc.communicate("\n", timeout=10)


def test_store_waits_for_a_short_cleanup_lock(tmp_path, monkeypatch):
    if session_store_guard.fcntl is None:
        return
    directory = tmp_path / "session-store"
    directory.mkdir()
    db = directory / "wait.db"
    guard = session_store_guard._open_guard(db)
    fcntl = session_store_guard.fcntl
    original_flock = fcntl.flock
    original_flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    blocked = threading.Event()
    def observe(fd, operation):
        try:
            return original_flock(fd, operation)
        except BlockingIOError:
            blocked.set()
            raise
    monkeypatch.setattr(fcntl, "flock", observe)
    def write():
        store = session_store.SessionStore("wait", snapshot_dir=tmp_path, busy_timeout_ms=5000)
        try:
            store.set_meta("synthetic", "kept")
            return store.get_meta("synthetic")
        finally:
            store.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            assert blocked.wait(5)
            guard.close()
            assert future.result(timeout=5) == "kept"
    finally:
        guard.close()


def test_store_lock_timeout_never_opens_the_database(tmp_path):
    if session_store_guard.fcntl is None:
        return
    directory = tmp_path / "session-store"
    directory.mkdir()
    db = directory / "busy.db"
    guard = session_store_guard._open_guard(db)
    session_store_guard.fcntl.flock(guard.fileno(), session_store_guard.fcntl.LOCK_EX)
    store = session_store.SessionStore("busy", snapshot_dir=tmp_path, busy_timeout_ms=1)
    try:
        with pytest.raises(sqlite3.OperationalError, match="cleanup lock timed out"):
            store.set_meta("synthetic", "value")
        assert not db.exists()
    finally:
        guard.close()
        store.close()


def test_failed_connection_releases_cleanup_lock(tmp_path, monkeypatch):
    if session_store_guard.fcntl is None:
        return
    directory = tmp_path / "session-store"
    directory.mkdir()
    store = session_store.SessionStore("failed", snapshot_dir=tmp_path)
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic connect failure")
    monkeypatch.setattr(session_store_guard.sqlite3, "connect", fail)
    with pytest.raises(sqlite3.OperationalError, match="synthetic connect failure"):
        store._connect()
    with session_store_guard._open_guard(store.db_path) as guard:
        session_store_guard.fcntl.flock(guard.fileno(), session_store_guard.fcntl.LOCK_EX | session_store_guard.fcntl.LOCK_NB)


def test_failed_close_retains_lock_until_connection_can_close(tmp_path, monkeypatch):
    if session_store_guard.fcntl is None:
        return
    store = session_store.SessionStore("close-failure", snapshot_dir=tmp_path)
    store.set_meta("synthetic", "kept")
    conn = store._conn
    class BrokenClose:
        def close(self):
            raise sqlite3.ProgrammingError("wrong thread")
    store._conn = BrokenClose()
    try:
        store.close()
        with session_store_guard._open_guard(store.db_path) as guard:
            with pytest.raises(BlockingIOError):
                session_store_guard.fcntl.flock(guard.fileno(), session_store_guard.fcntl.LOCK_EX | session_store_guard.fcntl.LOCK_NB)
    finally:
        store._conn = conn
        store.close()


def test_cleanup_prunes_an_idle_expired_store(tmp_path, monkeypatch):
    store = session_store.SessionStore("idle", snapshot_dir=tmp_path)
    store.set_meta("synthetic", "value")
    store.close()
    monkeypatch.setattr(session_store, "SESSION_STORE_DIR", store.db_path.parent)
    old = time.time() - 72 * 3600
    os.utime(store.db_path, (old, old))
    if session_store_guard.fcntl is not None:
        assert session_store.cleanup_old_stores() == 1
        assert not store.db_path.exists()


def test_cleanup_fails_closed_without_file_lock_support(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    db.write_text("synthetic")
    old = time.time() - 72 * 3600
    os.utime(db, (old, old))
    monkeypatch.setattr(session_store, "SESSION_STORE_DIR", tmp_path)
    monkeypatch.setattr(session_store_guard, "fcntl", None)
    assert session_store.cleanup_old_stores() == 0
    assert db.exists()


def test_store_operates_when_filesystem_does_not_support_locks(tmp_path, monkeypatch):
    if session_store_guard.fcntl is None:
        return
    def unsupported(*_args):
        raise OSError(errno.ENOTSUP, "locks unsupported")
    monkeypatch.setattr(session_store_guard.fcntl, "flock", unsupported)
    store = session_store.SessionStore("unsupported", snapshot_dir=tmp_path)
    try:
        store.set_meta("synthetic", "kept")
        assert store.get_meta("synthetic") == "kept"
    finally:
        store.close()
    monkeypatch.setattr(session_store, "SESSION_STORE_DIR", store.db_path.parent)
    assert session_store.cleanup_old_stores(max_age_hours=-1) == 0
    assert store.db_path.exists()


def test_parallel_session_writes_are_distinct_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store.time, "time", lambda: 1700000000.0)
    def write(sid):
        store = session_store.SessionStore(sid, snapshot_dir=tmp_path)
        try:
            conn = store._connect()
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 50
            for i in range(50):
                for _ in range(2):
                    store.insert_tool_output(f"tool-{i}", "Read", "file", sid, "hash", 10, 3)
            return [(r[0], r[1]) for r in conn.execute("SELECT tool_use_id,command_or_path FROM tool_outputs ORDER BY tool_use_id")]
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["first", "second"]))
    assert all(len(rows) == 50 for rows in results)
    assert {row[1] for row in results[0]} == {"first"}
    assert {row[1] for row in results[1]} == {"second"}


def test_restore_store_queries_avoid_sorting(tmp_path):
    store = session_store.SessionStore("query-test", snapshot_dir=tmp_path)
    try:
        conn = store._connect()
        queries = (
            ("SELECT tool_name,summary,output_chars,timestamp FROM context_intel_events ORDER BY timestamp DESC LIMIT ?", (5,)),
            ("SELECT COALESCE(tokens_est,0) FROM file_reads WHERE read_count >= ? ORDER BY last_access DESC LIMIT ?", (2, 25)),
        )
        for query, args in queries:
            plan = [tuple(row) for row in conn.execute("EXPLAIN QUERY PLAN " + query, args)]
            assert not any("TEMP B-TREE" in str(row) for row in plan), plan
    finally:
        store.close()


def test_replayed_intel_event_is_idempotent(tmp_path):
    store = session_store.SessionStore("replay", snapshot_dir=tmp_path)
    try:
        store.insert_intel_event("Read", "tool-1", "Useful result", 100)
        store.insert_intel_event("Read", "tool-1", "Useful result", 100)
        assert len(store.get_intel_events()) == 1
    finally:
        store.close()


def test_concurrent_duplicate_intel_event_is_idempotent(tmp_path):
    # Initialize once; writers open separate connections to the same session.
    session_store.SessionStore("shared", snapshot_dir=tmp_path)._connect().close()
    def write(_):
        store = session_store.SessionStore("shared", snapshot_dir=tmp_path, busy_timeout_ms=5000)
        try:
            store.insert_intel_event("Read", "tool-1", "Useful result", 100)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    store = session_store.SessionStore("shared", snapshot_dir=tmp_path)
    try:
        assert len(store.get_intel_events()) == 1
    finally:
        store.close()
