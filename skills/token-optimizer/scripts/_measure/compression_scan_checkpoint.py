"""Persist stable per-transcript fixture-scan evidence for upgrade drains."""
import hashlib
import json
from datetime import datetime


def _digest(path, size):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = size
        while remaining:
            block = stream.read(min(131072, remaining))
            if not block:
                raise OSError("transcript truncated during hashing")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


class FixtureScan:
    def __init__(self, conn, commands):
        self.conn = conn
        self.generation = hashlib.sha256(json.dumps(sorted(commands)).encode()).hexdigest()
        self.proofs = []
        self.digests = {}
        conn.execute("CREATE TABLE IF NOT EXISTS compression_fixture_scan_progress "
                     "(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sidechain INTEGER, generation TEXT, calls TEXT)")

    def calls(self, path, sidechain, before, commands, sub):
        proof = (before.st_size, before.st_mtime_ns)
        row = self.conn.execute(
            "SELECT calls FROM compression_fixture_scan_progress WHERE path=? AND size=? AND mtime_ns=? AND sidechain=? AND generation=?",
            (str(path), *proof, int(sidechain), self.generation + ":" + self.digests[path])).fetchone()
        if row:
            return [(command, datetime.fromisoformat(stamp)) for command, stamp in json.loads(row[0])]
        found = []
        for dt, _tid, name, command, _size in sub.index_results(path, sidechain):
            if name == "Bash" and command:
                stored = sub._stored_command(command)
                if stored in commands:
                    found.append((stored, dt))
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != proof or _digest(path, proof[0]) != self.digests[path]:
            return None
        payload = json.dumps([(command, dt.isoformat()) for command, dt in found])
        self.conn.execute("INSERT OR REPLACE INTO compression_fixture_scan_progress VALUES (?,?,?,?,?,?)",
                          (str(path), *proof, int(sidechain), self.generation + ":" + self.digests[path], payload))
        self.conn.commit()
        return found

    def track(self, path, before):
        digest = _digest(path, before.st_size)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise OSError("transcript changed during scan")
        self.digests[path] = digest
        self.proofs.append((path, (before.st_size, before.st_mtime_ns)))

    def track_metadata(self, path, before):
        self.proofs.append((path, (before.st_size, before.st_mtime_ns)))

    def stable(self):
        for path, proof in self.proofs:
            try:
                current = path.stat()
                if (current.st_size, current.st_mtime_ns) != proof:
                    return False
                if path in self.digests and _digest(path, proof[0]) != self.digests[path]:
                    return False
            except OSError:
                return False
        return True
