"""Identify archive rows contradicted by a uniquely matched full tool result."""
import json
import re
from datetime import datetime

_ROW = re.compile(r'^replaced (\S+) \(([\d,]+) chars -> [\d,]+ chars\)')


def full_result_ids(path, rows):
    """Return row IDs only when one result and one row agree on time, tool and size."""
    expected = []
    for ident, timestamp, detail in rows:
        match = _ROW.match(detail or '')
        try:
            stamp = datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            continue
        if match and '__' in match.group(1):
            expected.append((ident, stamp, match.group(1), int(match.group(2).replace(',', ''))))
    uses, results = {}, []
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    obj = json.loads(line)
                    stamp = datetime.fromisoformat(obj.get('timestamp', '').replace('Z', '+00:00')).astimezone().replace(tzinfo=None)
                except (ValueError, TypeError, AttributeError):
                    continue
                if obj.get('isSidechain'):
                    continue
                content = (obj.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if obj.get('type') == 'assistant' and block.get('type') == 'tool_use':
                        uses[block.get('id')] = block.get('name')
                    elif obj.get('type') == 'user' and block.get('type') == 'tool_result':
                        value = block.get('content')
                        if isinstance(value, list):
                            value = '\n'.join(b.get('text', '') for b in value if isinstance(b, dict) and b.get('type') == 'text')
                        if isinstance(value, str):
                            results.append((stamp, uses.get(block.get('tool_use_id')), len(value)))
    except OSError:
        return None
    matches = {ident: [i for i, (stamp, name, chars) in enumerate(results)
                       if name == tool and chars == size and abs((stamp - event_time).total_seconds()) <= 20]
               for ident, event_time, tool, size in expected}
    hits = {i: sum(i in values for values in matches.values()) for values in matches.values() for i in values}
    return {ident for ident, values in matches.items() if len(values) == 1 and hits[values[0]] == 1}


def proof_current(path, size, mtime_ns):
    try:
        stat = __import__('pathlib').Path(path).stat()
        return int(stat.st_size == size and stat.st_mtime_ns == mtime_ns)
    except (OSError, TypeError, ValueError):
        return 0


def collect(conn, max_sessions=8, deadline_seconds=None):
    """Append evidence after a stable scan; never alter the source ledger."""
    import time
    from pathlib import Path
    started = time.monotonic()
    conn.execute('CREATE TABLE IF NOT EXISTS archive_void_evidence (event_id INTEGER, path TEXT, size INTEGER, mtime_ns INTEGER, PRIMARY KEY(event_id,path,size,mtime_ns))')
    conn.execute('CREATE TABLE IF NOT EXISTS archive_void_scans (session_id TEXT, path TEXT, size INTEGER, mtime_ns INTEGER, PRIMARY KEY(session_id,path,size,mtime_ns))')
    groups = {}
    for ident, stamp, detail, sid in conn.execute("SELECT id,timestamp,detail,COALESCE(session_uuid,session_id) FROM savings_events WHERE event_type='tool_archive' AND timestamp>='2026-08-01' AND timestamp<'2026-09-01' ORDER BY timestamp"):
        if sid:
            groups.setdefault(sid, []).append((ident, stamp, detail))
    out = {'scanned': 0, 'proven': 0, 'pending': 0}
    for sid, events in groups.items():
        row = conn.execute('SELECT jsonl_path FROM session_log WHERE session_uuid=? AND jsonl_path IS NOT NULL ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
        if not row:
            continue
        path = Path(row[0])
        try:
            before = path.stat()
        except OSError:
            continue
        proof = (sid, str(path), before.st_size, before.st_mtime_ns)
        if conn.execute('SELECT 1 FROM archive_void_scans WHERE session_id=? AND path=? AND size=? AND mtime_ns=?', proof).fetchone():
            continue
        if (max_sessions is not None and out['scanned'] >= max_sessions) or (deadline_seconds is not None and time.monotonic() - started >= deadline_seconds):
            out['pending'] += 1
            continue
        out['scanned'] += 1
        ids = full_result_ids(path, events)
        if ids is None or not proof_current(str(path), before.st_size, before.st_mtime_ns):
            continue
        for ident in ids:
            inserted = conn.execute('INSERT OR IGNORE INTO archive_void_evidence VALUES (?,?,?,?)', (ident, str(path), before.st_size, before.st_mtime_ns))
            out['proven'] += max(0, inserted.rowcount)
        conn.execute('INSERT OR IGNORE INTO archive_void_scans VALUES (?,?,?,?)', proof)
    conn.commit()
    return out
