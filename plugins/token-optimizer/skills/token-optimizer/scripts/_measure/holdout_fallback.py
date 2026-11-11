"""Persist a first holdout assignment when the trends database is busy."""
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime

from hook_provenance import _exclusive
from .lease_candidate_cleanup import owner_is_gone

_SESSION = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def read(directory, session_id):
    path = directory / ".holdout-pending" / (session_id + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("arm") not in ("control", "treatment"):
            return None
        share = float(data["share"])
        if not 0 < share <= 1:
            return None
        return data["arm"], share, str(data["assigned_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def claim(directory, session_id, arm, share):
    root = directory / ".holdout-pending"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stripe = int(session_id.replace("-", "")[:2], 16) & 15
    with _exclusive(root / (".guard-%x" % stripe), timeout=0.25) as held:
        if not held:
            return read(directory, session_id)
        found = read(directory, session_id)
        if found is not None:
            return found
        assigned_at = datetime.now().isoformat()
        temporary = root / (".candidate-%d-%s.tmp" % (os.getpid(), uuid.uuid4().hex))
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"arm": arm, "share": share, "assigned_at": assigned_at}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, root / (session_id + ".json"))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return arm, share, assigned_at


def sweep(directory, conn):
    """Reclaim only aged candidates already committed to the trends ledger."""
    root = directory / ".holdout-pending"
    if not root.is_dir():
        return
    deadline = time.monotonic() + 0.01
    with _exclusive(root / ".sweep.guard", timeout=0) as held:
        if not held:
            return
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        return
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if entry.name.startswith(".candidate-") and entry.name.endswith(".tmp"):
                            parts = entry.name[len(".candidate-"):-4].split("-")
                            if (len(parts) == 2 and parts[0].isdigit() and len(parts[1]) == 32
                                    and all(c in "0123456789abcdef" for c in parts[1])
                                    and entry.stat(follow_symlinks=False).st_mtime < time.time() - 86400
                                    and owner_is_gone(int(parts[0]))):
                                os.unlink(entry.path)
                            continue
                        sid = entry.name[:-5] if entry.name.endswith(".json") else ""
                        if not _SESSION.match(sid) or entry.stat(follow_symlinks=False).st_mtime >= time.time() - 30 * 86400:
                            continue
                        stripe = int(sid.replace("-", "")[:2], 16) & 15
                        with _exclusive(root / (".guard-%x" % stripe), timeout=0) as locked:
                            if not locked:
                                continue
                            row = conn.execute("SELECT arm FROM holdout_sessions WHERE session_uuid=?", (sid,)).fetchone()
                            current = read(directory, sid)
                            if row and current and row[0] == current[0]:
                                os.unlink(entry.path)
                    except (OSError, ValueError, sqlite3.Error):
                        continue
        except OSError:
            pass
