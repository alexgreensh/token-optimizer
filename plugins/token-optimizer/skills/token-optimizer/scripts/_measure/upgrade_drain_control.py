"""Coordinate upgrade-drain processes without expiring a live worker."""
import os
import itertools
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager

from hook_provenance import _exclusive
from .lease_candidate_cleanup import owner_is_gone


def _marker(facade):
    return facade.SNAPSHOT_DIR / facade._UPGRADE_DRAIN_LOCK_NAME


def _write_marker(marker, pid, exclusive=False):
    fd, name = tempfile.mkstemp(prefix="." + marker.name + "." + str(os.getpid()) + ".", dir=str(marker.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(pid))
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(name, marker)
        else:
            os.replace(name, marker)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _clean_temporaries(facade, marker):
    prefix = "." + marker.name + "."
    deadline = time.monotonic() + 0.025
    try:
        with os.scandir(marker.parent) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    break
                if not entry.name.startswith(prefix):
                    continue
                parts = entry.name[len(prefix):].split(".")
                if (len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) != 8
                        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in parts[1])):
                    continue
                try:
                    if (entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < time.time() - 86400
                            and owner_is_gone(int(parts[0]))):
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        pass


def claim(facade):
    marker = _marker(facade)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive(marker.with_name(marker.name + ".claim"), timeout=0.05) as held:
            if not held:
                return False
            _clean_temporaries(facade, marker)
            if marker.exists():
                age = time.time() - marker.stat().st_mtime
                try:
                    pid = int(marker.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    pid = 0
                if age < 1 or (pid > 0 and not owner_is_gone(pid)):
                    return False
                marker.unlink()
            _write_marker(marker, os.getpid(), exclusive=True)
            return True
    except OSError:
        return False


def publish_child(facade, child_pid):
    marker = _marker(facade)
    try:
        with _exclusive(marker.with_name(marker.name + ".claim"), timeout=0.05) as held:
            if held and marker.read_text(encoding="utf-8") == str(os.getpid()):
                _write_marker(marker, child_pid)
    except OSError:
        pass


def release(facade):
    marker = _marker(facade)
    try:
        with _exclusive(marker.with_name(marker.name + ".claim"), timeout=0.05) as held:
            if held and marker.read_text(encoding="utf-8") == str(os.getpid()):
                marker.unlink()
    except OSError:
        pass


@contextmanager
def running(facade):
    marker = _marker(facade)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield False
        return
    with _exclusive(marker.with_name(marker.name + ".running"), timeout=0) as held:
        yield held


def retry_ready(raw, key, now):
    parts = (raw or "").split(":")
    if len(parts) < 2 or parts[0] != key:
        return True, 0
    try:
        attempts = max(0, int(parts[1]))
        last = float(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return True, 0
    age = now - last
    delay = min(300, 2 ** min(attempts, 9))
    return not (0 <= age < delay), attempts


def transcript_progress(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS transcript_backfill_progress "
                 "(session_uuid TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)")
    return {sid: [size, mtime] for sid, size, mtime in conn.execute(
        "SELECT session_uuid, size, mtime_ns FROM transcript_backfill_progress")}


def commit_transcript(conn, session_uuid, stamp):
    conn.execute("INSERT INTO transcript_backfill_progress (session_uuid, size, mtime_ns) VALUES (?, ?, ?) "
                 "ON CONFLICT(session_uuid) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns",
                 (session_uuid, stamp[0], stamp[1]))
    conn.commit()


def commit_marker_cursor(conn, key, cursor):
    conn.execute("INSERT INTO token_optimizer_meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE token_optimizer_meta.value < excluded.value",
                 (key, cursor))
    conn.commit()


def commit_drain_session(conn):
    if os.environ.get("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "").strip() == "1":
        conn.commit()


def backfill_pending(stats):
    return bool(stats.get("pending") or any(
        (value.get("pending") if isinstance(value, dict) else value)
        for key, value in stats.items() if key.endswith("error") or isinstance(value, dict)))


_SESSION_WRITE = re.compile(r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+["`\[]?session_log(?=[\s(\]`";]|$)', re.I)


class _RawConnection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, parameters=()):
        return sqlite3.Connection.execute(self.conn, sql, parameters)

    def executemany(self, sql, parameters):
        return sqlite3.Connection.executemany(self.conn, sql, parameters)


class SessionLogConnection(sqlite3.Connection):
    """Apply the bounded retry to direct session_log writes from every adapter."""
    def execute(self, sql, parameters=()):
        if _SESSION_WRITE.match(sql):
            result = session_write(self, sql, parameters)
            if result is None:
                raise sqlite3.OperationalError("session_log write deferred: database is locked")
            return result
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        if _SESSION_WRITE.match(sql):
            result = session_write(self, sql, parameters, many=True)
            if result is None:
                raise sqlite3.OperationalError("session_log write deferred: database is locked")
            return result
        return super().executemany(sql, parameters)


def session_write(conn, sql, parameters, budget_seconds=0.3, many=False):
    """Retry a session-row mutation briefly; None leaves it for the next pass."""
    raw = _RawConnection(conn) if isinstance(conn, SessionLogConnection) else conn
    previous = raw.execute("PRAGMA busy_timeout").fetchone()[0]
    deadline = time.monotonic() + budget_seconds
    raw.execute("PRAGMA busy_timeout=50")
    source = iter(parameters) if many else None
    try:
        for attempt in range(6):
            try:
                if not many:
                    return raw.execute(sql, parameters)
                batch, source = itertools.tee(source)
                if conn.isolation_level is not None and not conn.in_transaction:
                    raw.execute("BEGIN")
                raw.execute("SAVEPOINT to_session_write")
                try:
                    result = raw.executemany(sql, batch)
                except BaseException:
                    raw.execute("ROLLBACK TO to_session_write")
                    raw.execute("RELEASE to_session_write")
                    raise
                raw.execute("RELEASE to_session_write")
                return result
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(remaining, 0.01 * (2 ** attempt)))
        return None
    finally:
        raw.execute("PRAGMA busy_timeout=" + str(int(previous)))


class DailyUpdates:
    """Commit each completed session in a drain; keep hook passes batched."""
    def __init__(self, conn, facade):
        self.conn = conn
        self.facade = facade
        self.pending = []
        self.count = 0
        self.immediate = facade._collect_drain_mode() == "full"

    def append(self, row):
        self.pending.append(row)
        if self.immediate and self.facade._retry_db_operation(self.conn, self.flush) is None:
            raise self.facade.sqlite3.OperationalError("daily backfill remained locked")

    def flush(self):
        count = len(self.pending)
        result = session_write(self.conn,
            "UPDATE session_log SET daily_usage_json=?, session_uuid=COALESCE(session_uuid, ?), "
            "first_activity_day=?, last_activity_day=?, date=?, parent_daily_usage_json=?, "
            "daily_calls_json=?, daily_metrics_json=? WHERE jsonl_path=? AND daily_usage_json IS NULL", self.pending, many=True)
        if result is None:
            raise sqlite3.OperationalError("database is locked")
        self.conn.commit()
        self.pending.clear()
        self.count += count
        return count


def counted_backfill_all(facade, conn, quiet):
    stats = facade._update_counted_cumulative(conn, max_sessions=10**9, deadline_seconds=None, quiet=quiet)
    marker_total = {"files_scanned": 0, "rows_written": 0, "done": False}

    def cursor():
        try:
            row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key=?",
                               (facade._COUNTED_META_MARKER_CURSOR,)).fetchone()
            return row[0] if row else ""
        except facade.sqlite3.Error:
            return None

    seen = {cursor()}
    for _ in range(1000):
        marker = facade._counted_marker_backfill(conn, max_files=500, deadline_seconds=None, quiet=quiet)
        marker_total["files_scanned"] += int(marker.get("files_scanned", 0) or 0)
        marker_total["rows_written"] += int(marker.get("rows_written", 0) or 0)
        marker_total["done"] = bool(marker.get("done"))
        if marker_total["done"] or not marker.get("files_scanned"):
            break
        current = cursor()
        if current in seen:
            break
        seen.add(current)
    stats.update({"marker_files_scanned": marker_total["files_scanned"],
                  "marker_rows_written": marker_total["rows_written"],
                  "marker_backfill_done": marker_total["done"],
                  "rows_removed_no_transcript": facade._purge_counted_without_transcripts(conn)})
    stats["pricing_coverage"] = facade._counted_rollup_rows(facade._counted_stored_rows(conn).values(), conn)
    return stats
