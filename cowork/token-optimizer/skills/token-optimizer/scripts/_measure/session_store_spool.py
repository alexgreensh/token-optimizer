"""Durable, idempotent queue for session intelligence events."""
import json
import os
import sqlite3
import sys
import time
import uuid
from .lease_candidate_cleanup import owner_is_gone


def hint_epoch(store):
    """Read the reset generation without acquiring the schema writer lock."""
    if not store.db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(store.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.025)
        try:
            row = conn.execute("SELECT value FROM session_meta WHERE key='reset_epoch'").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


def enqueue_intel(store, tool_name, tool_use_id, summary, output_chars):
    enqueue(store, {"kind": "intel", "tool_name": tool_name,
                    "tool_use_id": tool_use_id, "summary": summary,
                    "output_chars": output_chars, "timestamp": time.time()})


def enqueue(store, data):
    try:
        _enqueue(store, data)
        if data.get("kind") == "hint_serves" and data.get("reset_epoch") is None:
            print("[Session Store] Hint tracking deferred: reset generation unavailable", file=sys.stderr)
    except OSError as exc:
        print("[Session Store] Tracking queue I/O failed: " + str(exc), file=sys.stderr)
        raise


def _enqueue(store, data):
    directory = store._store_dir / (store.session_id + ".spool")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_id = uuid.uuid4().hex
    temporary = directory / (event_id + "." + str(os.getpid()) + ".writing")
    uncertain = data.get("kind") == "hint_serves" and data.get("reset_epoch") is None
    pending = directory / (event_id + (".deferred" if uncertain else ".pending"))
    data = dict(data, id=event_id)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pending)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    try:
        conn = store._connect()
        drain(store, conn)
    except sqlite3.OperationalError as exc:
        store.close()
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise


def drain(store, conn, limit=64, deadline=None):
    if conn.in_transaction:
        return
    directory = store._store_dir / (store.session_id + ".spool")
    if not directory.is_dir():
        return
    deadline = time.monotonic() + 0.1 if deadline is None else deadline
    paths = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(paths) >= limit or time.monotonic() >= deadline:
                break
            if entry.name.endswith(".pending") and entry.is_file(follow_symlinks=False):
                paths.append(directory / entry.name)
    for path in paths:
        if time.monotonic() >= deadline:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["id"] != path.stem:
                continue
            if data.get("kind") == "hint_serves" and data.get("reset_epoch") is None:
                continue
            conn.execute("BEGIN IMMEDIATE")
            if not path.is_file():
                conn.rollback()
                continue
            conn.execute("CREATE TABLE IF NOT EXISTS spool_receipts (id TEXT PRIMARY KEY)")
            receipt = conn.execute("INSERT OR IGNORE INTO spool_receipts (id) VALUES (?)", (data["id"],))
            if receipt.rowcount and data.get("kind", "intel") == "intel":
                conn.execute(
                    "INSERT INTO context_intel_events (tool_name, tool_use_id, summary, output_chars, timestamp) "
                    "SELECT ?, ?, ?, ?, ? WHERE ? = '' OR NOT EXISTS "
                    "(SELECT 1 FROM context_intel_events WHERE tool_use_id = ?)",
                    (data["tool_name"], data["tool_use_id"], data["summary"],
                     data["output_chars"], data["timestamp"], data["tool_use_id"], data["tool_use_id"]),
                )
            elif receipt.rowcount and data.get("kind") == "tool_output":
                conn.execute(
                    "INSERT OR IGNORE INTO tool_outputs (tool_use_id, tool_name, tool_type, command_or_path, "
                    "output_hash, output_chars, output_tokens_est, compressed_preview, timestamp, source_file_path, "
                    "language, archived_from, output_text, truncated, index_truncated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", data["values"])
            elif receipt.rowcount and data.get("kind") == "activity":
                from activity_tracker import detect_mode, _WINDOW_SIZE, _PRUNE_THRESHOLD, _PRUNE_KEEP
                conn.execute("INSERT INTO activity_log (tool_name, tool_bucket, has_error, timestamp) VALUES (?, ?, ?, ?)",
                             (data["tool_name"], data["bucket"], data["has_error"], data["timestamp"]))
                rows = conn.execute("SELECT tool_bucket, has_error FROM activity_log ORDER BY timestamp DESC, id DESC LIMIT ?", (_WINDOW_SIZE,)).fetchall()
                if conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] > _PRUNE_THRESHOLD:
                    conn.execute("DELETE FROM activity_log WHERE id NOT IN (SELECT id FROM activity_log ORDER BY timestamp DESC, id DESC LIMIT ?)", (_PRUNE_KEEP,))
                mode = detect_mode([row[0] for row in rows], any(row[1] for row in rows))
                conn.execute("INSERT OR REPLACE INTO session_meta (key, value) VALUES ('current_mode', ?)", (mode,))
            elif receipt.rowcount and data.get("kind") == "hint_serves":
                row = conn.execute("SELECT value FROM session_meta WHERE key='reset_epoch'").fetchone()
                current = int(row[0]) if row else 0
                if current == data["reset_epoch"]:
                    conn.executemany(
                        "INSERT INTO hint_serves (file_path, served_at, credited) VALUES (?, ?, 0) "
                        "ON CONFLICT(file_path) DO NOTHING",
                        [(path, data["timestamp"]) for path in data["paths"]],
                    )
            elif receipt.rowcount and data.get("kind") == "file_edit":
                conn.execute("INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, '1')",
                             ("checkpoint_edit:" + data["fingerprint"],))
            elif receipt.rowcount and data.get("kind") == "decisions":
                from context_intel import _MAX_DECISIONS
                row = conn.execute("SELECT value FROM session_meta WHERE key='session_decisions'").fetchone()
                existing = json.loads(row[0]) if row and row[0] else []
                if not isinstance(existing, list):
                    raise ValueError("invalid decision state")
                for decision in data["decisions"]:
                    if decision not in existing and len(existing) < _MAX_DECISIONS:
                        existing.append(decision)
                conn.execute("INSERT OR REPLACE INTO session_meta (key, value) VALUES ('session_decisions', ?)",
                             (json.dumps(existing, ensure_ascii=False),))
            elif receipt.rowcount:
                raise ValueError("unknown spool record")
            conn.commit()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            conn.execute("DELETE FROM spool_receipts WHERE id = ?", (data["id"],))
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return
            raise
        except (OSError, ValueError, KeyError, TypeError):
            conn.rollback()


def drain_collect(snapshot_dir, seconds=0.5, max_sessions=8):
    """Make bounded progress on pending events from inactive sessions."""
    from session_store import SessionStore

    root = snapshot_dir / "session-store"
    deadline = time.monotonic() + seconds
    count = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if count >= max_sessions or time.monotonic() >= deadline:
                    break
                if not entry.name.endswith(".spool") or not entry.is_dir(follow_symlinks=False):
                    continue
                sid = entry.name[:-6]
                if not sid or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in sid):
                    continue
                with os.scandir(entry.path) as pending:
                    found = False
                    for item in pending:
                        if time.monotonic() >= deadline:
                            break
                        if item.name.endswith(".pending"):
                            found = True
                            break
                        _cleanup_interrupted(item)
                    if not found:
                        continue
                store = SessionStore(sid, snapshot_dir=snapshot_dir)
                try:
                    drain(store, store._connect(), deadline=deadline)
                except (OSError, sqlite3.Error):
                    pass
                finally:
                    store.close()
                count += 1
    except OSError:
        pass


def _cleanup_interrupted(entry):
    """Remove only old, unpublished files owned by a dead writer."""
    parts = entry.name.split(".")
    if (len(parts) != 3 or parts[2] != "writing" or len(parts[0]) != 32
            or any(c not in "0123456789abcdef" for c in parts[0]) or not parts[1].isdigit()):
        return
    try:
        if not entry.is_file(follow_symlinks=False) or entry.stat(follow_symlinks=False).st_mtime >= time.time() - 86400:
            return
        if owner_is_gone(int(parts[1])):
            os.unlink(entry.path)
    except OSError:
        pass
