"""Incremental, bounded-memory telemetry index for large Codex transcripts.

Raw tool output is never copied into the index. Each pass commits at most
64 MiB of new input; pending files are revisited even when their mtime is idle.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from plugin_env import resolve_snapshot_dir

PASS_BYTES = 64 * 1024 * 1024
PASS_SECONDS = 1.0
LINE_BYTES = 16 * 1024 * 1024
SCHEMA_VERSION = 3

_SCHEMA = '''
  CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, identity TEXT, offset INTEGER,
    size INTEGER, skipped INTEGER, revision INTEGER, mtime INTEGER);
  CREATE TABLE IF NOT EXISTS records(path TEXT, offset INTEGER, data TEXT,
    PRIMARY KEY(path, offset));
'''


def _open_db(db_path):
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(_SCHEMA)
        if 'mtime' not in {row[1] for row in conn.execute('PRAGMA table_info(files)')}:
            try:
                conn.execute('ALTER TABLE files ADD COLUMN mtime INTEGER')
            except sqlite3.OperationalError:
                pass  # concurrent migration: column already added
        if 'anchor' not in {row[1] for row in conn.execute('PRAGMA table_info(files)')}:
            conn.execute('ALTER TABLE files ADD COLUMN anchor TEXT')
    except Exception:
        # On Windows an abandoned handle pins the file and defeats the
        # unlink-and-rebuild below, so a failed open must not leak it.
        conn.close()
        raise
    return conn


def _connect():
    root = resolve_snapshot_dir()
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / 'codex-log-index.db'
    # Locked/contended is not corruption: never delete a live index. A burst
    # of concurrent first-opens races the journal_mode/schema setup and a
    # writer mid-commit owns the file, so a lock can outlast the in-connection
    # busy_timeout (and lock-upgrade deadlocks return BUSY without consulting
    # the busy handler at all). Wait it out on a deadline before giving up.
    deadline = time.monotonic() + 5
    while True:
        try:
            return _open_db(db_path)
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
        except sqlite3.DatabaseError:
            break  # corrupt DB: self-heal by removing and rebuilding
    # Windows refuses to unlink a file while any handle is open -- _open_db
    # already closed the broken connection, but an indexer or another process
    # can hold a transient share lock, so retry each file briefly.
    for suffix in ('', '-wal', '-shm'):
        target = db_path.parent / (db_path.name + suffix)
        for _ in range(10):
            try:
                target.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.1)
    return _open_db(db_path)


def pending(filepath):
    path = Path(filepath).resolve()
    try:
        conn = _connect()
        try:
            row = conn.execute('SELECT offset,revision,mtime,skipped FROM files WHERE path=?', (str(path),)).fetchone()
            stat = path.stat()
            return (row is None or row[0] != stat.st_size or
                    row[1] != SCHEMA_VERSION or row[2] != stat.st_mtime_ns or row[3] > 0)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return True


def _archive_chars(output):
    if isinstance(output, str):
        return [len(output), len(output)]
    text = sum(len(v.get('text') or '') for v in output if isinstance(v, dict)) if isinstance(output, list) else 0
    return [text, len(json.dumps(output))]


def _slim(record):
    from codex_session import _extract_text, _event_output_text
    payload = record.get('payload')
    if not isinstance(payload, dict):
        return None
    kind = payload.get('type')
    if record.get('type') in ('session_meta', 'turn_context'):
        fields = ('id', 'cwd', 'cli_version', 'model', 'effort', 'thread_settings', 'collaboration_mode')
        payload = {k: payload[k] for k in fields if k in payload}
    elif kind in ('user_message', 'agent_message', 'message'):
        text = _extract_text(payload)
        payload = {'type': kind, 'role': payload.get('role'), 'message': text[:1000],
                   'content': text[:1000], '_optimizer_chars': len(text)}
    elif kind in ('function_call_output', 'custom_tool_call_output', 'mcp_tool_call_output', 'exec_command_end', 'patch_apply_end'):
        output = payload.get('output')
        chars = len(str(payload.get('output') or '')) if kind.endswith('call_output') else len(_event_output_text(payload))
        payload = {'type': kind, '_optimizer_output_chars': chars, 'duration': payload.get('duration'),
                   '_optimizer_archive_chars': _archive_chars(output)}
    elif kind in ('function_call', 'custom_tool_call'):
        payload = {'type': kind, 'name': payload.get('name'),
                   'arguments': payload.get('arguments') if payload.get('name') == 'spawn_agent' else None}
    elif kind not in ('token_count', 'task_complete', 'collab_agent_spawn_end', 'mcp_tool_call_end'):
        return None
    # Bound metadata as well as outputs; abnormal records are diagnosed as skipped.
    compact = {'type': record.get('type'), 'timestamp': record.get('timestamp'), 'payload': payload}
    return compact


def records(filepath):
    path = Path(filepath).resolve()
    key = str(path)
    conn = _connect()
    try:
        # Read the file and parse OUTSIDE the write transaction to avoid
        # holding the SQLite write lock during the (potentially multi-second)
        # JSON parse loop. The parsed records are buffered in memory (bounded
        # by PASS_BYTES of compact metadata per pass), then committed in a
        # tight transaction.
        try:
            handle = path.open('rb')
        except OSError:
            # Missing/deleted/directory: degrade gracefully like pending().
            conn.close()
            def empty_iter():
                if False:
                    yield
            return empty_iter(), {'incomplete': True, 'scan_mode': 'indexing',
                                  'indexed_bytes': 0, 'source_bytes': 0, 'skipped_records': 0}
        with handle:
            stat = path.stat()
            identity = f'{SCHEMA_VERSION}:{stat.st_dev}:{stat.st_ino}:' + hashlib.sha256(handle.readline(65536)).hexdigest()
            # Decide whether to re-index BEFORE acquiring the write lock.
            row = conn.execute('SELECT identity,offset,skipped,mtime,anchor,size FROM files WHERE path=?', (key,)).fetchone()
            # Validate the old boundary before resuming an append.
            from _measure.transcript_stream import _anchor
            restart = (not row or row[0] != identity or row[1] > stat.st_size
                       or row[4] != _anchor(handle, row[1])
                       or (row[5] == stat.st_size and row[3] != stat.st_mtime_ns))
            if restart:
                offset, skipped = 0, 0
            else:
                offset, skipped = row[1], row[2]
            handle.seek(offset)
            end = min(stat.st_size, offset + PASS_BYTES)
            parsed = []
            parse_deadline = time.monotonic() + PASS_SECONDS
            while handle.tell() < end and time.monotonic() < parse_deadline:
                start = handle.tell()
                line = handle.readline(LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > LINE_BYTES:
                    while line and not line.endswith(b'\n') and time.monotonic() < parse_deadline:
                        line = handle.readline(65536)
                    if not line or not line.endswith(b'\n'):
                        handle.seek(start)
                        break
                    skipped += 1
                elif not line.endswith(b'\n'):
                    handle.seek(start)
                    break
                else:
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        if not line.endswith(b'\n'):
                            handle.seek(start)  # writer has not completed this record
                            break
                        skipped += 1
                        record = None
                    if isinstance(record, dict):
                        compact = _slim(record)
                        if compact:
                            value = json.dumps(compact, ensure_ascii=False)
                            if len(value) <= 65536:
                                parsed.append((key, start, value))
                            else:
                                skipped += 1
                offset = handle.tell()
            anchor = _anchor(handle, offset)
        # Now acquire the write lock for a tight batch commit only. A losing
        # thread can still outlast busy_timeout under heavy contention (or hit
        # the no-retry lock-upgrade deadlock path), so retry the transaction
        # on a deadline instead of surfacing a transient BUSY.
        deadline = time.monotonic() + 5
        while True:
            try:
                conn.execute('BEGIN IMMEDIATE')
                if restart:
                    conn.execute('DELETE FROM records WHERE path=?', (key,))
                conn.executemany('INSERT OR REPLACE INTO records(path, offset, data) VALUES (?,?,?)', parsed)
                conn.execute('INSERT OR REPLACE INTO files(path, identity, offset, size, skipped, revision, mtime, anchor) VALUES (?,?,?,?,?,?,?,?)',
                             (key, identity, offset, stat.st_size, skipped, SCHEMA_VERSION, stat.st_mtime_ns, anchor))
                conn.commit()
                break
            except sqlite3.OperationalError:
                conn.rollback()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        info = {'incomplete': offset < stat.st_size or skipped > 0,
                'scan_mode': 'indexing' if offset < stat.st_size else 'indexed_full',
                'indexed_bytes': offset, 'source_bytes': stat.st_size, 'skipped_records': skipped}
        # Cursor iteration keeps memory bounded even for years of tool events.
        def iterate():
            try:
                for (value,) in conn.execute('SELECT data FROM records WHERE path=? ORDER BY offset', (key,)):
                    yield json.loads(value)
            finally:
                conn.close()
        return iterate(), info
    except Exception:
        conn.close()
        raise
