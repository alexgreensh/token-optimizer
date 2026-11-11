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


# A marker is delivered within three minutes of its event; a day of slack keeps
# the comparison safe across time zones and clock skew.
_MARGIN_S = 86400 + 180


def _epoch(stamp):
    """Seconds for an event or file time, or None. Naive stamps are local time."""
    try:
        return datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _session_rows(conn):
    """(session_uuid, path, start day or None), oldest row first."""
    cols = {row[1] for row in conn.execute('PRAGMA table_info(session_log)')}
    days = [c for c in ('first_activity_day', 'date') if c in cols]
    day = ('MIN(' + ','.join("COALESCE(NULLIF(%s,''),'9999')" % c for c in days) + ')') if len(days) == 2 else (days[0] if days else 'NULL')
    out = []
    for sid, path, start in conn.execute('SELECT session_uuid,jsonl_path,' + day + ' FROM session_log WHERE session_uuid IS NOT NULL AND jsonl_path IS NOT NULL ORDER BY id'):
        start = str(start)[:10] if start else None
        out.append((sid, path, start if start and start[:1].isdigit() and start < '9999' else None))
    return out


def _could_hold(start_day, mtime_s, event_epoch):
    """Whether a transcript that began on `start_day` and was last written at
    `mtime_s` can contain the delivery of an event at `event_epoch`."""
    if event_epoch is None:
        return True
    if mtime_s < event_epoch - _MARGIN_S:
        return False
    begin = _epoch(start_day) if start_day else None
    return begin is None or begin <= event_epoch + _MARGIN_S


def _coverage(conn):
    """{generation: highest event id its scans looked for}."""
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_generations (generation TEXT PRIMARY KEY, max_event_id INTEGER)')
    return dict(conn.execute('SELECT generation,max_event_id FROM lean_receiver_generations').fetchall())


def collect(conn, to_utc, max_files=8, deadline_seconds=None):
    started = time.monotonic()
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_candidates (event_id INTEGER, receiver TEXT, path TEXT, size INTEGER, mtime_ns INTEGER, PRIMARY KEY(event_id,receiver,path,size,mtime_ns))')
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_scans (path TEXT, size INTEGER, mtime_ns INTEGER, generation TEXT, PRIMARY KEY(path,size,mtime_ns,generation))')
    conn.execute('CREATE TABLE IF NOT EXISTS lean_receiver_state (id INTEGER PRIMARY KEY, complete INTEGER, generation TEXT)')
    generation = _generation(conn)
    event_generation = _generation(conn, include_sessions=False)
    targets = defaultdict(list)
    newest = conn.execute("SELECT COALESCE(MAX(id),0) FROM savings_events WHERE event_type='resume_lean'").fetchone()[0]
    covered = _coverage(conn)
    conn.execute('INSERT OR REPLACE INTO lean_receiver_generations VALUES (?,?)', (event_generation, newest))
    pending_events = []
    for ident, stamp, sid, detail in conn.execute("SELECT id,timestamp,COALESCE(session_uuid,session_id),detail FROM savings_events WHERE event_type='resume_lean'"):
        dt = to_utc(stamp)
        if sid and dt and 'receiver=' not in (detail or ''):
            targets[sid[:8].lower()].append((ident, sid, dt))
            pending_events.append((ident, _epoch(stamp)))
    if not targets:
        conn.execute('INSERT OR REPLACE INTO lean_receiver_state VALUES (1, 1, ?)', (generation,))
        conn.commit()
        return {'scanned': 0, 'matches': 0, 'pending': 0, 'unavailable': 0}
    sessions, starts = {}, {}
    for sid, path, start in _session_rows(conn):
        sessions[path], starts[path] = sid, start
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
        # An unchanged file already searched for every event up to id N only
        # needs reading again when a later event could have been delivered in it.
        seen = max([covered.get(g, -1) for (g,) in conn.execute('SELECT generation FROM lean_receiver_scans WHERE path=? AND size=? AND mtime_ns=?', proof)] or [-1])
        if seen >= 0 and not any(ident > seen and _could_hold(starts.get(path), st.st_mtime, when) for ident, when in pending_events):
            conn.execute('INSERT OR IGNORE INTO lean_receiver_scans VALUES (?,?,?,?)', (*proof, event_generation))
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


def _delivered(path, size, mtime):
    """A transcript only grows, so a marker found in it earlier is still there."""
    try:
        s = Path(path).stat()
        return s.st_size >= size and s.st_mtime_ns >= mtime
    except OSError:
        return False


# Unsearched transcripts read on the spot per call, so a session indexed since
# the last collect pass does not hold recent events back until the next one.
_INLINE_FILES = 16
_INLINE_BYTES = 256 * 1024 * 1024


def _deliveries(path):
    """[(session prefix, epoch seconds)] of every lean-resume marker delivered in
    a transcript, or None when it cannot be read."""
    out = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if 'Cold-resume lean (session ' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                att = obj.get('attachment') or {}
                if obj.get('type') != 'attachment' or obj.get('isSidechain') or att.get('type') != 'hook_additional_context':
                    continue
                when = _epoch(obj.get('timestamp'))
                if when is not None:
                    out.extend((match.group(1).lower(), when) for match in _MARK.finditer(json.dumps(att)))
    except OSError:
        return None
    return out


def resolved(conn):
    """{event id: receiver session} for events with exactly one proven receiver.

    An event is held back only while a transcript that could contain a second
    delivery of it has not been searched: a file never scanned that was active
    around the event, or lines appended after the last scan to a file that was.
    A session that began later, or text appended long after the event, cannot
    hold that delivery, so it never takes back a receiver already proven.
    """
    try:
        candidates = defaultdict(set)
        for ident, receiver, path, size, mtime in conn.execute('SELECT event_id,receiver,path,size,mtime_ns FROM lean_receiver_candidates'):
            if _delivered(path, size, mtime):
                candidates[ident].add(receiver)
        single = {ident: next(iter(r)) for ident, r in candidates.items() if len(r) == 1}
        if not single:
            return {}
        events, prefixes = {}, {}
        for ident, stamp, sid in conn.execute(
                "SELECT id,timestamp,COALESCE(session_uuid,session_id) FROM savings_events WHERE event_type='resume_lean'"):
            if ident in single:
                events[ident], prefixes[ident] = _epoch(stamp), str(sid or '')[:8].lower()
        single = {ident: receiver for ident, receiver in single.items() if ident in events}
        if not single:
            return {}
        covered = _coverage(conn)
        scans = defaultdict(list)
        for path, size, mtime, generation in conn.execute('SELECT path,size,mtime_ns,generation FROM lean_receiver_scans'):
            if generation in covered:
                scans[path].append((size, mtime, covered[generation]))
        need = max(single)
        unsearched = []  # (start day, mtime seconds, exact cover id, [(older scan mtime seconds, cover id)], path, receiver, size)
        for sid, path, start in _session_rows(conn):
            try:
                st = Path(path).stat()
            except OSError:
                continue
            exact = max([top for size, mtime, top in scans.get(path, ()) if size == st.st_size and mtime == st.st_mtime_ns] or [-1])
            if exact >= need:
                continue
            older = [(mtime / 1e9, top) for size, mtime, top in scans.get(path, ()) if size <= st.st_size and mtime < st.st_mtime_ns]
            unsearched.append((start, st.st_mtime, exact, older, path, sid, st.st_size))
        out, read, budget = {}, {}, [_INLINE_FILES, _INLINE_BYTES]

        def second_delivery(path, size, sid, ident, when):
            """True when this unsearched transcript holds, or may hold, a delivery
            of the event to a different session. Reads it once, within a budget."""
            if path not in read:
                if budget[0] <= 0 or size > budget[1]:
                    return True  # over budget: stay conservative until collect scans it
                budget[0] -= 1
                budget[1] -= size
                read[path] = _deliveries(path)
            found = read[path]
            if found is None or when is None:
                return True
            return sid != single[ident] and any(
                prefix == prefixes[ident] and abs(stamp - when) <= _MARGIN_S for prefix, stamp in found)

        for ident, receiver in single.items():
            when = events[ident]
            for start, mtime_s, exact, older, path, sid, size in unsearched:
                if exact >= ident or not _could_hold(start, mtime_s, when):
                    continue
                if when is not None and any(top >= ident and when < seen - _MARGIN_S for seen, top in older):
                    continue  # only lines written well after the event are unread
                if second_delivery(path, size, sid, ident, when):
                    break
            else:
                out[ident] = receiver
        return out
    except Exception:
        return {}
