import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
sys.path.insert(0, str(SCRIPTS))


def test_eight_contended_intel_writes_are_recovered(tmp_path):
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    db = tmp_path / "session-store" / "shared.db"
    blocker = sqlite3.connect(str(db))
    blocker.execute("BEGIN IMMEDIATE")
    code = (
        "import sys; from pathlib import Path; from session_store import SessionStore; "
        "s=SessionStore('shared', snapshot_dir=Path(sys.argv[1])); "
        "s.insert_intel_event('Read', sys.argv[2], 'summary', 7); s.close()"
    )
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    start = time.monotonic()
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(8)]
    try:
        results = [child.communicate(timeout=5) for child in children]
        elapsed = time.monotonic() - start
    finally:
        blocker.rollback()
        blocker.close()
    assert all(child.returncode == 0 for child in children), results
    assert elapsed < 2.0
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "next", "summary", 7)
    store._connect()
    rows = store._connect().execute("SELECT tool_use_id FROM context_intel_events").fetchall()
    store.close()
    assert sorted(row[0] for row in rows) == [str(i) for i in range(8)] + ["next"]


def test_replay_after_commit_does_not_duplicate_event(tmp_path, monkeypatch):
    from session_store import SessionStore

    original = Path.unlink

    def interrupted_unlink(path, *args, **kwargs):
        if path.suffix == ".pending":
            raise OSError("simulated crash after commit")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", interrupted_unlink)
        store = SessionStore("shared", snapshot_dir=tmp_path)
        store.insert_intel_event("Read", "", "summary", 7)
        store.close()
    assert len(list((tmp_path / "session-store" / "shared.spool").glob("*.pending"))) == 1
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "next", "summary", 7)
    conn = store._connect()
    assert conn.execute("SELECT COUNT(*) FROM context_intel_events WHERE tool_use_id = ''").fetchone()[0] == 1
    store.close()


def test_collect_recovers_idle_session_queue(tmp_path, monkeypatch):
    import measure
    from session_store import SessionStore

    store = SessionStore("idle", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    db = sqlite3.connect(str(tmp_path / "session-store" / "idle.db"))
    db.execute("BEGIN IMMEDIATE")
    store = SessionStore("idle", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "idle-event", "summary", 7)
    db.rollback()
    db.close()
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(measure, "collect_sessions", lambda **kwargs: None)
    monkeypatch.setattr(measure, "_running_under_hook", lambda: False)
    measure._dispatch_collect([])
    store = SessionStore("idle", snapshot_dir=tmp_path)
    rows = store._connect().execute("SELECT tool_use_id FROM context_intel_events").fetchall()
    store.close()
    assert [row[0] for row in rows] == ["idle-event"]


def test_contended_tool_output_is_recovered(tmp_path):
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    blocker = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    blocker.execute("BEGIN IMMEDIATE")
    code = (
        "import sys; from pathlib import Path; from session_store import SessionStore; "
        "s=SessionStore('shared', snapshot_dir=Path(sys.argv[1])); "
        "s.insert_tool_output(sys.argv[2], 'Read', 'file', 'a.py', 'hash', 10, 3, "
        "output_text='searchable text'); s.close()"
    )
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(8)]
    try:
        results = [child.communicate(timeout=5) for child in children]
    finally:
        blocker.rollback()
        blocker.close()
    assert all(child.returncode == 0 for child in children), results
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "next", "summary", 7)
    rows = store.search_tool_outputs("searchable")
    store.close()
    assert sorted(row["tool_use_id"] for row in rows) == [str(i) for i in range(8)]


def test_contended_hint_serves_are_recovered(tmp_path):
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    blocker = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    blocker.execute("BEGIN IMMEDIATE")
    code = (
        "import sys; from pathlib import Path; from session_store import SessionStore; "
        "s=SessionStore('shared', snapshot_dir=Path(sys.argv[1])); "
        "s.record_hint_serve([sys.argv[2]]); s.close()"
    )
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), f"file{i}.py"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(8)]
    try:
        results = [child.communicate(timeout=5) for child in children]
    finally:
        blocker.rollback()
        blocker.close()
    assert all(child.returncode == 0 for child in children), results
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "drain", "summary", 7)
    rows = store._connect().execute("SELECT file_path FROM hint_serves").fetchall()
    store.close()
    assert sorted(Path(row[0]).name for row in rows) == [f"file{i}.py" for i in range(8)]


def test_contended_file_edit_evidence_is_recovered(tmp_path):
    from session_store import SessionStore
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()


    blocker = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    blocker.execute("BEGIN IMMEDIATE")
    code = (
        "import sys; from pathlib import Path; from session_store import SessionStore; "
        "s=SessionStore('shared', snapshot_dir=Path(sys.argv[1])); "
        "s.record_file_edit(sys.argv[2]); s.close()"
    )
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), f"file{i}.py"],
                                 env=dict(os.environ, PYTHONPATH=str(SCRIPTS)),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(8)]
    try:
        results = [child.communicate(timeout=5) for child in children]
    finally:
        blocker.rollback()
        blocker.close()
    assert all(child.returncode == 0 for child in children), results
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "drain", "summary", 7)
    assert all(store.was_file_edited(f"file{i}.py") for i in range(8))
    store.close()


def test_queued_hint_before_context_reset_is_not_restored(tmp_path):
    from session_store import SessionStore
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    blocker = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    blocker.execute("BEGIN IMMEDIATE")
    store = SessionStore("shared", snapshot_dir=tmp_path)
    try:
        store.record_hint_serve(["before-reset.py"])
    finally:
        blocker.rollback()
        blocker.close()
        store.close()
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.reset_context_state()
    store.insert_intel_event("Read", "drain", "summary", 7)
    assert store._connect().execute("SELECT count(*) FROM hint_serves").fetchone()[0] == 0
    store.close()


def test_unknown_hint_generation_is_preserved_without_restoring_it(tmp_path, monkeypatch, capsys):
    from session_store import SessionStore
    from _measure import session_store_spool
    monkeypatch.setattr(session_store_spool, "hint_epoch", lambda store: None)
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.record_hint_serve(["uncertain.py"])
    assert "reset generation unavailable" in capsys.readouterr().err
    store.reset_context_state()
    store.insert_intel_event("Read", "drain", "summary", 7)
    assert store._connect().execute("SELECT count(*) FROM hint_serves").fetchone()[0] == 0
    assert len(list((tmp_path / "session-store" / "shared.spool").glob("*.deferred"))) == 1
    store.close()


def test_collect_cleans_old_interrupted_spool_writes(tmp_path):
    from _measure.session_store_spool import drain_collect

    code = (
        "import sys,time,os; from pathlib import Path; from session_store import SessionStore; "
        "original=os.replace; "
        "def_replacement=0; "
        "exec('def stop(src,dst):\\n print(\"ready\", flush=True)\\n time.sleep(60)'); "
        "os.replace=stop; "
        "SessionStore('interrupted', snapshot_dir=Path(sys.argv[1])).insert_intel_event('Read','x','summary',7)"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                             env=dict(os.environ, PYTHONPATH=str(SCRIPTS)),
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
    finally:
        child.kill()
        child.wait(timeout=5)
    writing = list((tmp_path / "session-store" / "interrupted.spool").glob("*.writing"))
    assert len(writing) == 1
    old = time.time() - 90000
    os.utime(writing[0], (old, old))
    drain_collect(tmp_path)
    assert not writing[0].exists()


def test_collect_preserves_old_live_spool_write(tmp_path):
    from _measure.session_store_spool import drain_collect

    spool = tmp_path / "session-store" / "live.spool"
    spool.mkdir(parents=True)
    writing = spool / ("a" * 32 + "." + str(os.getpid()) + ".writing")
    writing.write_text("unfinished")
    old = time.time() - 90000
    os.utime(writing, (old, old))
    drain_collect(tmp_path)
    assert writing.exists()


def test_failed_connection_setup_retries_cleanly(tmp_path):
    from session_store import SessionStore

    root = tmp_path / "session-store"
    root.mkdir()
    blocker = sqlite3.connect(str(root / "shared.db"))
    blocker.execute("CREATE TABLE example (id INTEGER)")
    blocker.execute("BEGIN EXCLUSIVE")
    store = SessionStore("shared", snapshot_dir=tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store._connect()
    finally:
        blocker.rollback()
        blocker.close()
    conn = store._connect()
    assert conn.execute("SELECT COUNT(*) FROM context_intel_events").fetchone()[0] == 0
    store.close()


def test_unwritable_spool_reports_lost_event(tmp_path, monkeypatch, capsys):
    from _measure import session_store_spool
    from session_store import SessionStore

    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")

    with monkeypatch.context() as m:
        m.setattr(session_store_spool.os, "open", fail)
        with pytest.raises(OSError):
            SessionStore("shared", snapshot_dir=tmp_path).insert_intel_event("Read", "a", "s", 1)
    assert "session" in capsys.readouterr().err.lower()


def test_eight_activity_events_survive_database_contention(tmp_path):
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    blocker = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    blocker.execute("BEGIN IMMEDIATE")
    code = (
        "import sys; from pathlib import Path; from session_store import SessionStore; "
        "from activity_tracker import log_tool_use; "
        "s=SessionStore('shared',snapshot_dir=Path(sys.argv[1])); "
        "log_tool_use(s,sys.argv[2]); s.close()"
    )
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), "Read"], env=env)
                for _ in range(8)]
    try:
        for child in children:
            assert child.wait(timeout=5) == 0
    finally:
        blocker.rollback()
        blocker.close()
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "drain", "summary", 7)
    assert store._connect().execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 8
    store.close()


def test_drained_events_do_not_leave_permanent_receipts(tmp_path):
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    for i in range(5):
        store.insert_intel_event("Read", str(i), "summary", 7)
    conn = store._connect()
    assert conn.execute("SELECT COUNT(*) FROM context_intel_events").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM spool_receipts").fetchone()[0] == 0
    store.close()


def test_concurrent_drainers_do_not_duplicate_an_unkeyed_event(tmp_path):
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    db = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    db.execute("BEGIN IMMEDIATE")
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "", "original", 7)
    db.rollback()
    db.close()
    code = ("import sys; from pathlib import Path; from session_store import SessionStore; "
            "s=SessionStore('shared',snapshot_dir=Path(sys.argv[1])); "
            "s.insert_intel_event('Read',sys.argv[2],'other',7); s.close()")
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)],
                                 env=dict(os.environ, PYTHONPATH=str(SCRIPTS))) for i in range(8)]
    for child in children:
        assert child.wait(timeout=5) == 0
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "last", "other", 7)
    assert store._connect().execute("SELECT COUNT(*) FROM context_intel_events WHERE summary='original'").fetchone()[0] == 1
    store.close()


def test_late_activity_does_not_replace_newer_mode(tmp_path):
    from _measure import session_store_spool
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    for i in range(10):
        session_store_spool.enqueue(store, {"kind": "activity", "tool_name": "Read", "bucket": "read",
                                            "has_error": 0, "timestamp": 200 + i})
    for i in range(4):
        session_store_spool.enqueue(store, {"kind": "activity", "tool_name": "Edit", "bucket": "edit",
                                            "has_error": 0, "timestamp": 100 + i})
    assert store.get_meta("current_mode") == "review"
    store.close()


def test_concurrent_decisions_are_not_dropped(tmp_path):
    import json
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    store._connect()
    store.close()
    blocker = sqlite3.connect(str(tmp_path / "session-store" / "shared.db"))
    blocker.execute("BEGIN IMMEDIATE")
    code = ("import sys; from pathlib import Path; from session_store import SessionStore; "
            "from context_intel import _extract_decisions; "
            "s=SessionStore('shared',snapshot_dir=Path(sys.argv[1])); "
            "_extract_decisions('We decided to use option '+sys.argv[2]+' because it works for this synthetic example',s); s.close()")
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)],
                                 env=dict(os.environ, PYTHONPATH=str(SCRIPTS))) for i in range(8)]
    try:
        for child in children:
            assert child.wait(timeout=5) == 0
    finally:
        blocker.rollback()
        blocker.close()
    store = SessionStore("shared", snapshot_dir=tmp_path)
    store.insert_intel_event("Read", "drain", "summary", 7)
    decisions = json.loads(store.get_meta("session_decisions"))
    store.close()
    assert len(decisions) == 8


def test_unknown_reset_epoch_cannot_revive_cleared_context(tmp_path, monkeypatch):
    from reset_epoch import snapshot_reset_epoch
    from session_store import SessionStore

    store = SessionStore("shared", snapshot_dir=tmp_path)
    with monkeypatch.context() as m:
        m.setattr(store, "get_reset_epoch", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        epoch = snapshot_reset_epoch(store)
    store.reset_context_state()
    written = store.upsert_file_entry("example.py", {"mtime_ns": 1, "size_bytes": 1}, expected_reset_epoch=epoch)
    assert written is False
    assert store.get_file_entry("example.py") is None
    store.close()
