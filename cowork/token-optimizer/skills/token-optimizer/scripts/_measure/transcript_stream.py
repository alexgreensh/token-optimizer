"""Resumable transcript projection, with bounded input and time per pass.

Only conversation, usage, boundary and hook records are retained. Raw progress
events (which can dominate long Claude sessions) never enter the disk index.
"""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from collections import deque
from pathlib import Path

from plugin_env import resolve_snapshot_dir

PASS_BYTES = 64 * 1024 * 1024
LINE_BYTES = 16 * 1024 * 1024
PASS_SECONDS = 1.0
LARGE_BYTES = 96 * 1024 * 1024
VERSION = 2


def _connect():
    root = resolve_snapshot_dir()
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / 'transcript-index.db', timeout=0.1)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, identity TEXT,
          offset INTEGER, size INTEGER, mtime INTEGER, anchor TEXT, skipped INTEGER);
        CREATE TABLE IF NOT EXISTS records(path TEXT, offset INTEGER, data TEXT,
          PRIMARY KEY(path, offset));
    ''')
    return conn


def _anchor(handle, offset):
    handle.seek(max(0, offset - 4096))
    return hashlib.sha256(handle.read(min(offset, 4096))).hexdigest()


def status(path):
    """Return checkpoint state without advancing it; unreadable counts as pending."""
    conn = None
    try:
        path = Path(path).resolve()
        stat = path.stat()
        conn = _connect()
        row = conn.execute('SELECT offset,size,mtime,skipped,identity FROM files WHERE path=?',
                           (str(path),)).fetchone()
        return {'indexed_bytes': row[0] if row else 0, 'source_bytes': stat.st_size,
                'incomplete': not row or row[:3] != (stat.st_size, stat.st_size, stat.st_mtime_ns)
                or row[3] > 0 or row[4] != f'{VERSION}:{stat.st_dev}:{stat.st_ino}',
                'skipped_records': row[3] if row else 0}
    except (OSError, sqlite3.Error):
        return {'incomplete': True, 'indexed_bytes': 0, 'skipped_records': 0}
    finally:
        if conn is not None:
            conn.close()


def pending(path):
    return status(path)['incomplete']


def _project(record):
    if record.get('type') in ('session_meta', 'turn_context', 'response_item', 'event_msg'):
        from codex_log_index import _slim
        return _slim(record)
    if record.get('type') not in ('assistant', 'user', 'system', 'attachment', 'tool_result'):
        fields = ('type', 'timestamp', 'isSidechain', 'version', 'slug')
        return {key: record[key] for key in fields if key in record} if record.get('timestamp') else None
    return {key: value for key, value in record.items() if key not in (
        'toolUseResult', 'snapshot', 'fileHistorySnapshot', 'data')}


def records(filepath, seconds=None, raw=False):
    """Advance a durable offset and return a cursor over the indexed prefix.

    Appends validate the old boundary, so an mtime change does not restart a
    growing session. Replacement, truncation and same-size rewrites restart it.
    Incomplete final lines remain at their starting offset until completed.
    """
    deadline = time.monotonic() + (PASS_SECONDS if seconds is None else max(0, seconds))
    path = Path(filepath).resolve()
    key = str(path)
    conn = _connect()
    try:
        with path.open('rb') as handle:
            stat = path.stat()
            identity = f'{VERSION}:{stat.st_dev}:{stat.st_ino}'
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT identity,offset,size,mtime,anchor,skipped FROM files WHERE path=?',
                               (key,)).fetchone()
            restart = (not row or row[0] != identity or row[1] > stat.st_size
                       or row[4] != _anchor(handle, row[1])
                       or (row[2] == stat.st_size and row[3] != stat.st_mtime_ns))
            if restart:
                offset, skipped = 0, 0
                conn.execute('DELETE FROM records WHERE path=?', (key,))
            else:
                offset, skipped = row[1], row[5]
            handle.seek(offset)
            stop = min(stat.st_size, offset + PASS_BYTES)
            while handle.tell() < stop and time.monotonic() < deadline:
                start = handle.tell()
                line = handle.readline(LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > LINE_BYTES:
                    # A pathological single record is explicit missing coverage.
                    # Keep its offset until the full record can be drained.
                    while line and not line.endswith(b'\n') and time.monotonic() < deadline:
                        line = handle.readline(65536)
                    if not line.endswith(b'\n'):
                        handle.seek(start)
                        break
                    skipped += 1
                elif not line.endswith(b'\n'):
                    handle.seek(start)
                    break
                else:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            compact = _project(record)
                            if compact is not None:
                                conn.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',
                                             (key, start, json.dumps(compact, ensure_ascii=False)))
                    except (ValueError, UnicodeError, RecursionError):
                        skipped += 1
                offset = handle.tell()
            anchor = _anchor(handle, offset)
            conn.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)',
                         (key, identity, offset, stat.st_size, stat.st_mtime_ns, anchor, skipped))
            conn.commit()
        info = {'indexed_bytes': offset, 'source_bytes': stat.st_size,
                'skipped_records': skipped, 'incomplete': offset < stat.st_size or skipped > 0}

        def iterate():
            try:
                for (value,) in conn.execute('SELECT data FROM records WHERE path=? ORDER BY offset', (key,)):
                    yield value if raw else json.loads(value)
            finally:
                conn.close()
        return iterate(), info
    except BaseException:
        conn.close()
        raise


def needs_resume(path):
    try:
        if Path(path).stat().st_size <= LARGE_BYTES:
            return False
        from codex_session import is_codex_session_path
        if is_codex_session_path(path):
            from codex_log_index import pending as codex_pending
            return codex_pending(path)
        return pending(path)
    except OSError:
        return False


def open_transcript(path, *args, **kwargs):
    """Text-line interface for existing metrics, sharing the same projection."""
    if Path(path).stat().st_size > LARGE_BYTES:
        try:
            from codex_session import is_codex_session_path
            if is_codex_session_path(path):
                from codex_log_index import records as read_records
            else:
                read_records = records
            values, _ = read_records(path)
        except sqlite3.Error as exc:
            raise OSError('transcript index unavailable') from exc
        return _indexed_lines(values)
    return _raw_lines(open(path, 'rb'))


@contextmanager
def _indexed_lines(values):
    try:
        yield (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n' for value in values)
    finally:
        values.close()


@contextmanager
def _raw_lines(handle):
    with handle:
        def lines():
            while True:
                line = handle.readline(LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > LINE_BYTES:
                    while line and not line.endswith(b'\n'):
                        line = handle.readline(65536)
                    continue
                yield line.decode('utf-8', errors='replace')
        yield lines()


def binary_lines(path):
    """Keep byte-filtering consumers streaming as well."""
    if Path(path).stat().st_size <= LARGE_BYTES:
        return _raw_binary_lines(open(path, 'rb'))
    from codex_session import is_codex_session_path
    if not is_codex_session_path(path):
        def indexed():
            try:
                values, _ = records(path, raw=True)
            except sqlite3.Error as exc:
                raise OSError('transcript index unavailable') from exc
            try:
                for value in values:
                    yield (value + '\n').encode('utf-8')
            finally:
                values.close()
        return indexed()
    context = open_transcript(path)

    def iterate():
        with context as lines:
            for line in lines:
                yield line.encode('utf-8')
    return iterate()


def _raw_binary_lines(handle):
    with handle:
        while True:
            line = handle.readline(LINE_BYTES + 1)
            if not line:
                return
            if len(line) > LINE_BYTES:
                while line and not line.endswith(b'\n'):
                    line = handle.readline(65536)
                continue
            if not line.isascii():
                try:
                    line.decode('utf-8')
                except UnicodeDecodeError:
                    line = line.decode('utf-8', errors='replace').encode('utf-8')
            yield line


def tail_lines(path, count):
    """Retain only the requested suffix of the indexed conversation."""
    with open_transcript(path) as lines:
        return list(deque(lines, maxlen=count))


def sampled_records(path):
    """Bounded fallback while an index is locked; callers must label it incomplete."""
    with Path(path).open('rb') as handle:
        head = handle.readline(65536)
        try:
            record = json.loads(head)
            if isinstance(record, dict) and record.get('type') == 'session_meta':
                yield record
        except (ValueError, UnicodeError):
            pass
        size = handle.seek(0, 2)
        offset = max(0, size - 8 * 1024 * 1024)
        handle.seek(offset)
        if offset:
            handle.readline(LINE_BYTES + 1)
        while handle.tell() < size:
            line = handle.readline(LINE_BYTES + 1)
            if len(line) > LINE_BYTES or not line.endswith(b'\n'):
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    yield record
            except (ValueError, UnicodeError, RecursionError):
                continue
