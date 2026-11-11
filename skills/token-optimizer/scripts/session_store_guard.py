"""Coordinate session-store connections with age-based cleanup."""
import errno
import os
import sqlite3
import time

try:
    import fcntl
except ImportError:
    fcntl = None


def _open_guard(db_path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(str(db_path.with_suffix(".db.guard")), flags, 0o600), "r+b")


def connect(store, timeout):
    if fcntl is None:
        return sqlite3.connect(str(store.db_path), timeout=timeout)
    guard = _open_guard(store.db_path)
    try:
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise sqlite3.OperationalError("session store cleanup lock timed out") from exc
                    time.sleep(min(0.01, remaining))
        except OSError as exc:
            if exc.errno in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
                guard.close()
                return sqlite3.connect(str(store.db_path), timeout=timeout)
            raise
        conn = sqlite3.connect(str(store.db_path), timeout=timeout)
    except BaseException:
        guard.close()
        raise
    store._cleanup_guard = guard
    return conn


def release(store):
    guard = getattr(store, "_cleanup_guard", None)
    if guard is not None:
        store._cleanup_guard = None
        guard.close()


def prune_old_stores(directory, cutoff):
    # Unsupported platforms retain stores rather than race an active connection.
    if fcntl is None or not directory.exists():
        return 0
    deleted = 0
    for db_path in directory.glob("*.db"):
        try:
            with _open_guard(db_path) as guard:
                try:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if db_path.stat().st_mtime >= cutoff or db_path.with_suffix(".db-wal").exists():
                    continue
                db_path.unlink()
                shm = db_path.with_suffix(".db-shm")
                if shm.exists():
                    shm.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted
