"""Identify archive rows contradicted by a uniquely matched full tool result."""
import json
import re
import hashlib
from pathlib import Path
from datetime import datetime

_ROW = re.compile(r'^replaced (\S+) \(([\d,]+) chars -> [\d,]+ chars\)')


def full_result_ids(path, rows, allow_sidechain=False):
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
                if obj.get('isSidechain') and not allow_sidechain:
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


def session_files(parent):
    parent = Path(parent)
    return [parent] + sorted((parent.with_suffix('') / 'subagents').glob('agent-*.jsonl'))


def session_fingerprint(parent):
    try:
        values = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in session_files(parent)]
        return hashlib.sha256(repr(values).encode()).hexdigest()
    except OSError:
        return None


def proof_current(path, size, mtime_ns, parent, fingerprint):
    try:
        stat = Path(path).stat()
        return int(stat.st_size == size and stat.st_mtime_ns == mtime_ns and session_fingerprint(parent) == fingerprint)
    except (OSError, TypeError, ValueError):
        return 0


# Scan memo. v2 scans covered August 2026 rows only; a session marked scanned
# there may still hold unscanned rows from other months, so every session is
# rescanned once under the all-dates memo.
_SCANS = 'archive_void_scans_v3'


def collect(conn, max_sessions=8, deadline_seconds=None):
    """Append evidence after stable parent/subagent scans, preserving source rows.

    Every archive row is checked, whatever its date: a row is voided only when
    its own transcript holds the full result (see ``full_result_ids``), so a
    period where archiving worked produces no evidence.
    """
    import time
    from . import upgrade_drain_control
    started = time.monotonic()
    conn.execute('CREATE TABLE IF NOT EXISTS archive_void_evidence_v2 (event_id INTEGER, path TEXT, size INTEGER, mtime_ns INTEGER, parent TEXT, fingerprint TEXT, PRIMARY KEY(event_id,path,size,mtime_ns,fingerprint))')
    conn.execute('CREATE TABLE IF NOT EXISTS ' + _SCANS + ' (session_id TEXT, parent TEXT, fingerprint TEXT, PRIMARY KEY(session_id,parent,fingerprint))')
    groups = {}
    for ident, stamp, detail, sid in conn.execute("SELECT id,timestamp,detail,COALESCE(session_uuid,session_id) FROM savings_events WHERE event_type='tool_archive' ORDER BY timestamp"):
        if sid:
            groups.setdefault(sid, []).append((ident, stamp, detail))
    out = {'scanned': 0, 'proven': 0, 'pending': 0}
    rollouts = None
    for sid, events in groups.items():
        row = conn.execute('SELECT jsonl_path FROM session_log WHERE session_uuid=? AND jsonl_path IS NOT NULL ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
        codex = None
        if not row:
            if rollouts is None:
                from . import archive_void_codex
                rollouts = archive_void_codex.rollout_index()
            codex = rollouts.get(str(sid).lower())
            if codex is None:
                continue
        parent = row[0] if row else str(codex[-1])
        fingerprint = session_fingerprint(parent)
        if fingerprint is None:
            continue
        proof = (sid, parent, fingerprint)
        if conn.execute('SELECT 1 FROM ' + _SCANS + ' WHERE session_id=? AND parent=? AND fingerprint=?', proof).fetchone():
            continue
        if (max_sessions is not None and out['scanned'] >= max_sessions) or (deadline_seconds is not None and time.monotonic() - started >= deadline_seconds):
            out['pending'] += 1
            continue
        out['scanned'] += 1
        if codex is not None:
            _record_codex(conn, out, codex, events, proof)
            upgrade_drain_control.commit_drain_session(conn)
            continue
        matches = {}
        for index, path in enumerate(session_files(parent)):
            ids = full_result_ids(path, events, allow_sidechain=index > 0)
            if ids is None:
                matches = None
                break
            for ident in ids:
                matches.setdefault(ident, []).append(path)
        if matches is None or session_fingerprint(parent) != fingerprint:
            continue
        for ident, paths in matches.items():
            if len(paths) != 1:
                continue
            path = paths[0]
            st = path.stat()
            inserted = conn.execute('INSERT OR IGNORE INTO archive_void_evidence_v2 VALUES (?,?,?,?,?,?)', (ident, str(path), st.st_size, st.st_mtime_ns, parent, fingerprint))
            out['proven'] += max(0, inserted.rowcount)
        conn.execute('INSERT OR IGNORE INTO ' + _SCANS + ' VALUES (?,?,?)', proof)
        upgrade_drain_control.commit_drain_session(conn)
    conn.commit()
    return out


def _record_codex(conn, out, rollouts, events, proof):
    """Void evidence for a Codex thread whose rollouts never showed the pointer.

    The proof is bound to the thread's newest rollout file (where the thread
    keeps writing); a change there makes the evidence stale until rescanned.
    """
    from . import archive_void_codex
    _sid, parent, fingerprint = proof
    ids = archive_void_codex.void_ids(rollouts, events)
    if ids is None or session_fingerprint(parent) != fingerprint:
        out['pending'] += 1
        return
    st = Path(parent).stat()
    for ident in sorted(ids):
        inserted = conn.execute('INSERT OR IGNORE INTO archive_void_evidence_v2 VALUES (?,?,?,?,?,?)', (ident, parent, st.st_size, st.st_mtime_ns, parent, fingerprint))
        out['proven'] += max(0, inserted.rowcount)
    conn.execute('INSERT OR IGNORE INTO ' + _SCANS + ' VALUES (?,?,?)', proof)
