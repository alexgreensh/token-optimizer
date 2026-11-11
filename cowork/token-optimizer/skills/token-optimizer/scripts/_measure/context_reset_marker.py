"""Publish whole reset markers and serialize competing updates."""
import hashlib
import math
import os
import time
import uuid

from hook_provenance import _exclusive
from .lease_candidate_cleanup import owner_is_gone


def _clean(parent):
    prefix = ".context-reset-"
    deadline = time.monotonic() + 0.01
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    return
                if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
                    continue
                parts = entry.name[len(prefix):-4].split("-")
                if (len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) != 32
                        or any(c not in "0123456789abcdef" for c in parts[1])):
                    continue
                try:
                    if (entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < time.time() - 86400
                            and owner_is_gone(int(parts[0]))):
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        pass


def publish(marker, now):
    # A reset (/clear, compaction) is rare, so waiting up to 0.5 s for a competing writer is cheap; the old 50 ms
    # dropped a reset whenever eight writers raced on a slow-fsync disk (Windows CI), losing the newest timestamp.
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = marker.parent.parent.parent / "session-store"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stripe = hashlib.sha256(str(marker).encode("utf-8")).digest()[0] & 15
    with _exclusive(root / (".context-reset-guard-%x" % stripe), timeout=0.5) as held:
        if not held:
            return False
        _clean(marker.parent)
        try:
            previous = float(marker.read_text(encoding="utf-8").strip())
            if math.isfinite(previous):
                now = max(now, previous)
        except (OSError, ValueError):
            pass
        temporary = marker.parent / (".context-reset-%d-%s.tmp" % (os.getpid(), uuid.uuid4().hex))
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(repr(now))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return True
