"""Serialize refresh-marker transitions and retain live owners regardless of age."""
import hashlib
import os
import time
import uuid
from contextlib import contextmanager

from hook_provenance import _exclusive
from .lease_candidate_cleanup import owner_is_gone


@contextmanager
def _guard(lock):
    # Fixed stripes avoid one permanent kernel-lock file per session.
    stripe = hashlib.sha256(lock.name.encode("utf-8")).digest()[0] & 15
    with _exclusive(lock.parent / (".status-refresh-guard-%x" % stripe), timeout=0.05) as held:
        yield held


def _owner(lock):
    try:
        parts = lock.read_text(encoding="ascii").split()
        return parts[0], int(parts[1]) if len(parts) == 2 else 0
    except (OSError, ValueError, IndexError):
        return "", 0


def _publish(lock, token):
    temporary = lock.parent / (".status-refresh-owner-%d-%s.tmp" % (os.getpid(), uuid.uuid4().hex))
    fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write((token + " " + str(os.getpid())).encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(lock))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sweep(parent):
    deadline = time.monotonic() + 0.01
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    return
                name = entry.name
                if not name.startswith(".status-refresh-owner-") or not name.endswith(".tmp"):
                    continue
                parts = name[len(".status-refresh-owner-"):-4].split("-")
                if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) != 32:
                    continue
                if any(c not in "0123456789abcdef" for c in parts[1]):
                    continue
                try:
                    if (entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < time.time() - 86400
                            and owner_is_gone(int(parts[0]))):
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        pass


def acquire(facade, session_id):
    lock = facade._status_bar_lock_path(session_id)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        with _guard(lock) as held:
            if not held:
                return None
            _sweep(lock.parent)
            if lock.exists():
                _, pid = _owner(lock)
                if time.time() - lock.stat().st_mtime < facade._STATUS_BAR_LOCK_STALE_S:
                    return None
                if pid and not owner_is_gone(pid):
                    return None
                lock.unlink()
            token = os.urandom(16).hex()
            _publish(lock, token)
            return token
    except OSError:
        return None


def adopt(facade, session_id, token):
    """A detached child must still own the marker before it starts work."""
    if not token:
        return False
    lock = facade._status_bar_lock_path(session_id)
    try:
        with _guard(lock) as held:
            if not held or _owner(lock)[0] != token:
                return False
            _publish(lock, token)
            return True
    except OSError:
        return False


def release(facade, session_id, token):
    if not token:
        return False
    lock = facade._status_bar_lock_path(session_id)
    try:
        with _guard(lock) as held:
            if not held or _owner(lock)[0] != token:
                return False
            lock.unlink()
            return True
    except OSError:
        return False
