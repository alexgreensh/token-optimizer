"""Persist first-call observations without storing transcript text or paths."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from . import first_call_context, setup_step


class Cache:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'setup-observations-v1.json'
        self.entries = {}
        self.dirty = False
        self.pending = 0
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.version = _version()
            if data.get('version') == self.version and isinstance(data.get('entries'), dict):
                self.entries = data.get('entries', {})
        except (OSError, ValueError, TypeError, AttributeError):
            self.version = None

    def observe(self, path):
        path = Path(path)
        key = hashlib.sha256(os.fsencode(str(path))).hexdigest()
        try:
            stat = path.stat()
            signature = [stat.st_size, stat.st_mtime_ns]
            entry = self.entries.get(key)
            if isinstance(entry, dict) and entry.get('signature') == signature:
                return entry.get('observation')
            observation = setup_step.observe_step(first_call_context.transcript(path), lambda v: round(len(v) / 4))
            after = path.stat()
            if signature == [after.st_size, after.st_mtime_ns] and time.time_ns() - after.st_mtime_ns >= 2_000_000_000:
                self.entries[key] = {'signature': signature, 'observation': observation}
                self.dirty = True
                self.pending += 1
                if self.pending >= 200:
                    self.save()
            return observation
        except (OSError, ValueError, TypeError):
            if key in self.entries:
                del self.entries[key]
                self.dirty = True
            return None

    def save(self):
        if not self.dirty:
            return
        try:
            tmp = self.path.with_name('.%s.%s.tmp' % (self.path.name, uuid.uuid4().hex))
            tmp.write_text(json.dumps({'version': _version(), 'entries': self.entries}), encoding='utf-8')
            os.replace(tmp, self.path)
            self.dirty = False
            self.pending = 0
        except OSError:
            pass


def _version():
    files = [Path(first_call_context.__file__), Path(setup_step.__file__)]
    stamps = [[p.stat().st_size, p.stat().st_mtime_ns] for p in files]
    commands = sorted(first_call_context._optimizer_commands())
    digest = hashlib.sha256(json.dumps(commands).encode('utf-8')).hexdigest()
    return {'schema': 1, 'estimator': 'round(len/4)', 'commands': digest, 'code': stamps}
