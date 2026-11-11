"""Store retention must account for activity recorded in a WAL file."""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import session_store


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
