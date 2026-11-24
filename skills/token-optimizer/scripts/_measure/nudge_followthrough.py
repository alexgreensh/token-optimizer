"""Record context nudges and price observed follow-through from transcripts."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _diagnostic(facade, exc):
    """Keep a bounded, path-free diagnostic without breaking collection."""
    try:
        directory = Path(facade.SNAPSHOT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'nudge-resolve.log'
        line = json.dumps({'operation': 'resolve', 'error': type(exc).__name__}) + '\n'
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            if stream.tell() + len(line.encode('utf-8')) > 65536:
                stream.close()
                fd = os.open(str(path), os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'w', encoding='utf-8') as replacement:
                    replacement.write(line)
            else:
                stream.write(line)
    except (OSError, AttributeError):
        pass


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _rows(path):
    with Path(path).open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and not row.get('isSidechain'):
                    yield row
            except ValueError:
                continue


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS nudge_events (id INTEGER PRIMARY KEY, session_uuid TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, context_tokens INTEGER, model TEXT, transcript_path TEXT, status TEXT NOT NULL DEFAULT "pending", action_ts TEXT, savings_event_id INTEGER)')


def record(facade, sid, kind, tokens, model, path):
    if not sid:
        return
    try:
        conn = facade._init_trends_db()
        try:
            _schema(conn)
            stamp = datetime.now(timezone.utc).isoformat()
            conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,model,transcript_path) SELECT ?,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM nudge_events WHERE session_uuid=? AND kind=? AND ts=?)',
                         (sid, kind, stamp, tokens, model, str(path or ''), sid, kind, stamp))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _diagnostic(facade, exc)


def _context(message):
    usage = message.get('usage') or {}
    try:
        return sum(int(usage.get(k) or 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0



_MISSING = object()


def _message(row):
    message = row.get('message')
    return message if isinstance(message, dict) else None


def _ident(row, message):
    ident = message.get('id') or row.get('uuid')
    try:
        hash(ident)
    except TypeError:
        return _MISSING
    return ident


def _requests(rows):
    seen, requests = set(), []
    for row in rows:
        if row.get('type') != 'assistant':
            continue
        message = _message(row)
        if message is None:
            continue
        ident = _ident(row, message)
        if ident is _MISSING:
            continue
        if ident not in seen and _context(message) > 0:
            seen.add(ident)
            requests.append(message)
            if len(requests) == 50:
                break
    return requests

def classify(rows, emitted, new_rows=None):
    """Return status, action timestamp and observed post-action requests."""
    stamp = _epoch(emitted)
    if stamp is None:
        return 'pending', None, []
    replies, action, following, seen = 0, None, [], set()
    for row in rows:
        ts = _epoch(row.get('timestamp'))
        if ts is None or ts < stamp:
            continue
        if action is None:
            if row.get('type') == 'system' and row.get('subtype') == 'compact_boundary':
                action = row.get('timestamp')
                continue
            message = _message(row)
            if row.get('type') == 'user' and message is not None and isinstance(message.get('content'), str) and message['content'].strip() == '/clear':
                action = row.get('timestamp')
                continue
            if row.get('type') == 'assistant' and message is not None:
                ident = _ident(row, message)
                if ident is _MISSING:
                    continue
                if ident not in seen:
                    seen.add(ident); replies += 1
                if replies > 20:
                    return 'ignored', None, []
        elif row.get('type') == 'system' and row.get('subtype') == 'compact_boundary':
            break
        elif row.get('type') == 'assistant':
            message = _message(row)
            if message is None:
                continue
            ident = _ident(row, message)
            if ident is _MISSING:
                continue
            if ident not in seen and _context(message) > 0:
                seen.add(ident); following.append(message)
                if len(following) == 50:
                    break
    if action is not None:
        return 'followed', action, following
    if new_rows:
        end = max((_epoch(r.get('timestamp')) or 0 for r in rows), default=0)
        start = min((_epoch(r.get('timestamp')) or float('inf') for r in new_rows), default=float('inf'))
        if end and start - end > 1800:
            return 'ignored', None, []
        return 'followed', new_rows[0].get('timestamp'), _requests(new_rows)
    return 'pending', None, []


def _start(path):
    """First timestamp of an append-only transcript, without reading the rest."""
    with Path(path).open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and not row.get('isSidechain'):
                stamp = _epoch(row.get('timestamp'))
                if stamp is not None:
                    return stamp
    return None


def _project_index(directory, since):
    """Start times of transcripts written after `since`.

    Only these can follow a nudged session. Reading every transcript in full
    held a whole project history in memory: 17 GB of logs grew past 11 GB.
    """
    starts, failures = {}, []
    for path in Path(directory).glob('*.jsonl'):
        if path.is_symlink():
            continue
        try:
            if path.stat().st_mtime < since:
                continue
            start = _start(path)
            if start is not None:
                starts[path] = start
        except Exception as exc:
            failures.append(exc)
    return starts, failures


def _nudged_rows(path, stamp):
    """Rows from the nudge on, plus the newest row, and the session's last timestamp."""
    kept, end, newest = [], None, None
    for row in _rows(Path(path)):
        ts = _epoch(row.get('timestamp'))
        if ts is None:
            continue
        if end is None or ts > end:
            end, newest = ts, row
        if ts >= stamp:
            kept.append(row)
    if newest is not None and (_epoch(newest.get('timestamp')) or 0) < stamp:
        kept.append(newest)
    return kept, end


def _successor_rows(path):
    """The opening rows of the next session, up to the requests classify prices."""
    rows = []
    for row in _rows(Path(path)):
        rows.append(row)
        if row.get('type') == 'assistant' and len(_requests(rows)) >= 50:
            break
    return rows


def _next_session(path, stamp, end, starts):
    current = Path(path)
    choices = [(start, str(candidate)) for candidate, start in starts.items()
               if candidate != current and start > end and start > stamp]
    return str(min(choices)[1]) if choices else None


def _session_ended(facade, sid, last_ts):
    """A successor counts only once the nudged session has ended."""
    if last_ts is None:
        return False
    if datetime.now(timezone.utc).timestamp() - last_ts >= 1800:
        return True
    probe = getattr(facade, '_session_process_alive', None)
    if callable(probe):
        try:
            return probe(sid) is False
        except Exception:
            pass
    return False


def resolve(facade, conn=None):
    """Update estimates on collection; requests may arrive after the action."""
    owned = conn is None
    try:
        conn = conn or facade._init_trends_db()
        try:
            if getattr(conn, 'in_transaction', False):
                _diagnostic(facade, RuntimeError('caller transaction open'))
                return
            _schema(conn)
            conn.commit()
            events = conn.execute("SELECT id,session_uuid,ts,context_tokens,transcript_path,savings_event_id FROM nudge_events WHERE status='pending' AND savings_event_id IS NULL").fetchall()
            events = [event for event in events if event[4] and _epoch(event[2]) is not None]
            first_stamp = {}
            for _, _, ts, _, path, _ in events:
                first_stamp[path] = min(first_stamp.get(path, float('inf')), _epoch(ts))
            nudged = {}
            for path, stamp in first_stamp.items():
                try:
                    rows, end = _nudged_rows(path, stamp)
                    if end is not None:
                        nudged[path] = rows, end
                except Exception as exc:
                    _diagnostic(facade, exc)
            indexes = {}
            for project in {Path(path).parent for path in first_stamp}:
                ends = [end for path, (_, end) in nudged.items() if Path(path).parent == project]
                try:
                    # A successor is written after the nudged session ends; allow clock skew.
                    starts, failures = _project_index(project, min(ends, default=float('inf')) - 300)
                except Exception as exc:
                    starts, failures = {}, [exc]
                for failure in failures:
                    _diagnostic(facade, failure)
                indexes[project] = starts
            successors = {}
            prepared = {}
            claimed_sessions = set()
            events.sort(key=lambda event: (_epoch(event[2]) or 0, event[0]), reverse=True)
            for ident, sid, ts, before, path, event_id in events:
                if path not in nudged:
                    continue
                rows, end = nudged[path]
                status, action, requests = classify(rows, ts)
                if status == 'pending':
                    next_path = _next_session(path, _epoch(ts), end, indexes[Path(path).parent])
                    next_rows = None
                    if next_path and _session_ended(facade, sid, end):
                        if next_path not in successors:
                            try:
                                successors[next_path] = _successor_rows(next_path)
                            except Exception as exc:
                                _diagnostic(facade, exc)
                                successors[next_path] = None
                        next_rows = successors[next_path]
                    if next_rows:
                        status, action, requests = classify(rows, ts, next_rows)
                        if status == 'followed':
                            if next_path in claimed_sessions:
                                status, action, requests = 'ignored', None, []
                            else:
                                claimed_sessions.add(next_path)
                prepared[ident] = status, action, requests
            conn.execute('BEGIN IMMEDIATE')
            claimed = set()
            for ident, sid, ts, before, path, event_id in events:
                try:
                    if ident not in prepared:
                        continue
                    status, action, requests = prepared[ident]
                    current = conn.execute('SELECT status,savings_event_id FROM nudge_events WHERE id=?', (ident,)).fetchone()
                    if current is None or current[0] != 'pending' or current[1] is not None:
                        continue
                    event_id = current[1]
                    cost = 0.0
                    tokens = 0
                    duplicate = (sid, action) in claimed
                    if action:
                        claimed.add((sid, action))
                    if status == 'followed' and requests and before and not duplicate:
                        after = _context(requests[0])
                        delta = max(0, int(before) - after)
                        for message in requests:
                            model = message.get('model')
                            if facade._is_priced_model(model):
                                cost += facade._get_model_cost(model, 0, 0, delta, 0)
                                tokens += delta
                        # Existing compaction credits take precedence for the same action.
                        try:
                            for table, kind in (('compression_events', 'feature'), ('savings_events', 'event_type')):
                                old = conn.execute(f"SELECT timestamp FROM {table} WHERE (session_uuid=? OR session_id=?) AND ({kind} LIKE '%compact%' OR {kind} IN ('quality_nudge','mod_clean','mod_clean_first'))" + (" AND verified=1" if table == 'compression_events' else " AND event_type != 'mod_compaction_cost'"), (sid, sid)).fetchall()
                                if any(abs((_epoch(t) or 0) - (_epoch(action) or 0)) <= 120 for (t,) in old):
                                    cost = tokens = 0
                        except Exception as exc:
                            _diagnostic(facade, exc)
                            continue
                    if cost > 0:
                        cur = conn.execute('INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail,model,unjoinable) VALUES(?,?,?,?,?,?,?,?,?)',
                                           (datetime.fromtimestamp(_epoch(action)).isoformat(), 'nudge_followed', tokens, cost, sid, sid, 'nudge_id=' + str(ident), None, 0))
                        event_id = cur.lastrowid
                    conn.execute('UPDATE nudge_events SET status=?,action_ts=?,savings_event_id=? WHERE id=?', (status, action, event_id, ident))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise exc
            conn.commit()
        finally:
            if owned:
                conn.close()
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        _diagnostic(facade, exc)



def weekly_counts(facade):
    """Read the shipped event schema for the last seven days."""
    from datetime import timedelta
    try:
        conn = facade._open_trends_db_readonly(timeout=1.0)
    except Exception:
        import sqlite3
        path = Path(facade.TRENDS_DB)
        if not path.exists():
            return {'followed': 0, 'total': 0}
        conn = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nudge_events'").fetchone():
            return {'followed': 0, 'total': 0}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
        rows = conn.execute('SELECT ts, status FROM nudge_events').fetchall()
        recent = [status for ts, status in rows if (_epoch(ts) or 0) >= cutoff]
        return {'followed': sum(status == 'followed' for status in recent), 'total': len(recent)}
    finally:
        conn.close()

def record_emitted(facade, sid, path, quality_data, fresh, compact, advisory):
    try:
        telemetry = facade._absolute_context_tokens(quality_data, sid) or {}
        model = facade._resolve_session_model(sid)
        for kind, message in (('fresh_session', fresh), ('compact_tip', compact), ('context_size', advisory)):
            if message:
                record(facade, sid, kind, telemetry.get('tokens'), model, path)
    except Exception:
        pass
