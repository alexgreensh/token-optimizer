"""Recover historical lean-resume receivers from delivered hook attachments."""
import json
import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_MARK = re.compile(r'Cold-resume lean \(session ([0-9a-fA-F]{8})(?=[, )])')


def _stat_matches(path, size, mtime):
    try:
        s = Path(path).stat()
        return s.st_size == size and s.st_mtime_ns == mtime
    except OSError:
        return False


def _generation(conn, include_sessions=True):
    events = conn.execute("SELECT id,timestamp,COALESCE(session_uuid,session_id),detail FROM savings_events WHERE event_type='resume_lean' ORDER BY id").fetchall()
    sessions = conn.execute('SELECT session_uuid,jsonl_path FROM session_log WHERE session_uuid IS NOT NULL AND jsonl_path IS NOT NULL ORDER BY id').fetchall()
    return hashlib.sha256(repr(('receiver_scan_v3', events, sessions if include_sessions else [])).encode()).hexdigest()


def collect(conn, to_utc, max_files=8, deadline_seconds=None):
    started = time.monotonic()
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_candidates (event_id INTEGER, receiver TEXT, path TEXT, size INTEGER, mtime_ns INTEGER, PRIMARY KEY(event_id,receiver,path,size,mtime_ns))')
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_scans (path TEXT, size INTEGER, mtime_ns INTEGER, generation TEXT, PRIMARY KEY(path,size,mtime_ns,generation))')
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_state (id INTEGER PRIMARY KEY, complete INTEGER, generation TEXT)')
    generation = _generation(conn)
    event_generation = _generation(conn, include_sessions=False)
    targets = defaultdict(list)
    for ident, stamp, sid, detail in conn.execute("SELECT id,timestamp,COALESCE(session_uuid,session_id),detail FROM savings_events WHERE event_type='resume_lean'"):
        dt = to_utc(stamp)
        if sid and dt and 'receiver=' not in (detail or ''):
            targets[sid[:8].lower()].append((ident, sid, dt))
    if not targets:
        conn.execute('INSERT OR REPLACE INTO lean_receiver_state VALUES (1, 1, ?)', (generation,))
        conn.commit()
        return {'scanned': 0, 'matches': 0, 'pending': 0, 'unavailable': 0}
    sessions = {}
    for sid, path in conn.execute('SELECT session_uuid,jsonl_path FROM session_log WHERE session_uuid IS NOT NULL AND jsonl_path IS NOT NULL ORDER BY id'):
        sessions[path] = sid
    out = {'scanned': 0, 'matches': 0, 'pending': 0, 'unavailable': 0}
    for path, receiver in sessions.items():
        try:
            st = Path(path).stat()
        except OSError:
            out['unavailable'] += 1
            continue
        proof = (path, st.st_size, st.st_mtime_ns)
        if conn.execute('SELECT 1 FROM lean_receiver_scans WHERE path=? AND size=? AND mtime_ns=? AND generation=?', (*proof, event_generation)).fetchone():
            continue
        if (max_files is not None and out['scanned'] >= max_files) or (deadline_seconds is not None and time.monotonic() - started >= deadline_seconds):
            out['pending'] += 1
            continue
        out['scanned'] += 1
        found = set()
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    if 'Cold-resume lean (session ' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                        when = datetime.fromisoformat(obj.get('timestamp', '').replace('Z', '+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
                    except (ValueError, TypeError):
                        continue
                    att = obj.get('attachment') or {}
                    if obj.get('type') != 'attachment' or obj.get('isSidechain') or att.get('type') != 'hook_additional_context':
                        continue
                    for match in _MARK.finditer(json.dumps(att)):
                        possible = [(ident, target) for ident, target, stamp in targets.get(match.group(1).lower(), ())
                                    if receiver != target and abs((when - stamp).total_seconds()) <= 180]
                        if len(possible) == 1:
                            found.add((possible[0][0], receiver, path, st.st_size, st.st_mtime_ns))
        except OSError:
            out['unavailable'] += 1
            continue
        if not _stat_matches(*proof):
            out['pending'] += 1
            continue
        for item in found:
            r = conn.execute('INSERT OR IGNORE INTO lean_receiver_candidates VALUES (?,?,?,?,?)', item)
            out['matches'] += max(0, r.rowcount)
        conn.execute('INSERT OR IGNORE INTO lean_receiver_scans VALUES (?,?,?,?)', (*proof, event_generation))
    conn.execute('INSERT OR REPLACE INTO lean_receiver_state VALUES (1, ?, ?)', (int(out['pending'] == 0), generation))
    conn.commit()
    return out


def resolved(conn):
    try:
        ready = conn.execute('SELECT complete,generation FROM lean_receiver_state WHERE id=1').fetchone()
        if not ready or not ready[0] or ready[1] != _generation(conn):
            return {}
        candidates = defaultdict(set)
        for ident, receiver, path, size, mtime in conn.execute('SELECT event_id,receiver,path,size,mtime_ns FROM lean_receiver_candidates'):
            if _stat_matches(path, size, mtime):
                candidates[ident].add(receiver)
        return {ident: next(iter(receivers)) for ident, receivers in candidates.items() if len(receivers) == 1}
    except Exception:
        return {}
