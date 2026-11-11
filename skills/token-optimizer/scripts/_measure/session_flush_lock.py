"""Hold the Stop flush singleton until release or process exit."""
import os
import time
import uuid

from hook_provenance import _acquire, _unlock
from .lease_candidate_cleanup import owner_is_gone


class _Handle:
    def __init__(self, fd, marker, owner):
        self.fd = fd
        self.marker = marker
        self.owner = owner

    def close(self):
        if self.fd is None:
            return
        try:
            owner_path = self.marker / "owner"
            if owner_path.read_text(encoding="ascii") == self.owner:
                owner_path.unlink()
                self.marker.rmdir()
        except (OSError, UnicodeError):
            pass
        _unlock(self.fd)
        os.close(self.fd)
        self.fd = None

    def __del__(self):
        self.close()


def acquire(facade, max_age_seconds):
    root = facade.SNAPSHOT_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    fd = _acquire(root / ".session-end-flush.guard", 0)
    if fd is None:
        return None
    marker = root / ".session-end-flush.lock"
    try:
        try:
            marker.mkdir(mode=0o700)
        except FileExistsError:
            owner_path = marker / "owner"
            if owner_path.exists():
                owner = owner_path.read_text(encoding="ascii").split()
                if (len(owner) != 2 or not owner[0].isdigit() or len(owner[1]) != 32
                        or any(c not in "0123456789abcdef" for c in owner[1])
                        or not owner_is_gone(int(owner[0]))):
                    return None
                owner_path.unlink()
            elif time.time() - marker.stat().st_mtime <= (120 if max_age_seconds is None else max_age_seconds):
                return None
            marker.rmdir()
            marker.mkdir(mode=0o700)
        owner = "%d %s" % (os.getpid(), uuid.uuid4().hex)
        (marker / "owner").write_text(owner, encoding="ascii")
        result = _Handle(fd, marker, owner)
        fd = None
        return result
    except (OSError, UnicodeError):
        return None
    finally:
        if fd is not None:
            _unlock(fd)
            os.close(fd)


def release(handle):
    if handle is not None:
        handle.close()
