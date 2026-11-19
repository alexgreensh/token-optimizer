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
    with Path(path).open(encoding='utf-8') as stream:
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
            conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,model,transcript_path) VALUES(?,?,?,?,?,?)',
                         (sid, kind, datetime.now(timezone.utc).isoformat(), tokens, model, str(path or '')))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _context(message):
    usage = message.get('usage') or {}
    return sum(int(usage.get(k) or 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))


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
        message = row.get('message') or {}
        if action is None:
            if row.get('type') == 'system' and row.get('subtype') == 'compact_boundary':
                action = row.get('timestamp')
                continue
            if row.get('type') == 'user' and isinstance(message.get('content'), str) and message['content'].strip() == '/clear':
                action = row.get('timestamp')
                continue
            if row.get('type') == 'assistant':
                ident = message.get('id') or row.get('uuid')
                if ident not in seen:
                    seen.add(ident); replies += 1
                if replies > 20:
                    return 'ignored', None, []
        elif row.get('type') == 'system' and row.get('subtype') == 'compact_boundary':
            break
        elif row.get('type') == 'assistant':
            ident = message.get('id') or row.get('uuid')
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
        return 'followed', new_rows[0].get('timestamp'), [r['message'] for r in new_rows if r.get('type') == 'assistant' and _context(r.get('message') or {}) > 0][:50]
    return 'pending', None, []


def _project_index(directory):
    """Read each transcript once before the database write transaction."""
    transcripts = {}
    for path in Path(directory).glob('*.jsonl'):
        if path.is_symlink():
            continue
        try:
            rows = list(_rows(path))
            stamps = [_epoch(row.get('timestamp')) for row in rows]
            stamps = [stamp for stamp in stamps if stamp is not None]
            if stamps:
                transcripts[path] = (min(stamps), max(stamps), rows)
        except OSError:
            raise
    return transcripts


def _next_session(path, stamp, index):
    current = Path(path)
    entry = index.get(current)
    if not entry:
        return None
    end = entry[1]
    choices = [(start, str(candidate), rows) for candidate, (start, _, rows) in index.items()
               if candidate != current and start > end and start > stamp]
    return min(choices, default=(None, None, None))[2]


def resolve(facade, conn=None):
    """Update estimates on collection; requests may arrive after the action."""
    owned = conn is None
    try:
        conn = conn or facade._init_trends_db()
        try:
            _schema(conn)
            conn.commit()
            events = conn.execute('SELECT id,session_uuid,ts,context_tokens,transcript_path,savings_event_id FROM nudge_events').fetchall()
            projects = {Path(path).parent for _, _, _, _, path, _ in events if path}
            indexes = {project: _project_index(project) for project in projects}
            prepared = {}
            for ident, sid, ts, before, path, event_id in events:
                if not path:
                    continue
                entry = indexes[Path(path).parent].get(Path(path))
                if not entry:
                    continue
                rows = entry[2]
                status, action, requests = classify(rows, ts)
                if status == 'pending' or (status == 'followed' and not requests):
                    next_rows = _next_session(path, _epoch(ts), indexes[Path(path).parent])
                    if next_rows and status == 'pending':
                        status, action, requests = classify(rows, ts, next_rows)
                    elif next_rows:
                        requests = [r['message'] for r in next_rows if r.get('type') == 'assistant' and _context(r.get('message') or {}) > 0][:50]
                prepared[ident] = status, action, requests
            conn.execute('BEGIN IMMEDIATE')
            claimed = set()
            for ident, sid, ts, before, path, event_id in events:
                try:
                    if ident not in prepared:
                        continue
                    status, action, requests = prepared[ident]
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
                        for table, kind in (('compression_events', 'feature'), ('savings_events', 'event_type')):
                            try:
                                old = conn.execute(f"SELECT timestamp FROM {table} WHERE (session_uuid=? OR session_id=?) AND ({kind} LIKE '%compact%' OR {kind} IN ('quality_nudge','mod_clean','mod_clean_first'))" + (" AND verified=1" if table == 'compression_events' else " AND event_type != 'mod_compaction_cost'"), (sid, sid)).fetchall()
                                if any(abs((_epoch(t) or 0) - (_epoch(action) or 0)) <= 120 for (t,) in old):
                                    cost = tokens = 0
                            except Exception:
                                pass
                    if event_id:
                        conn.execute('UPDATE savings_events SET tokens_saved=?,cost_saved_usd=? WHERE id=?', (tokens, cost, event_id))
                    elif cost > 0:
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
