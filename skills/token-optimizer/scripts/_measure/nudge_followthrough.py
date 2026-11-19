"""Record context nudges and price observed follow-through from transcripts."""
import json
from datetime import datetime, timezone
from pathlib import Path


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


def _next_session(path, stamp):
    """Find a later transcript in the same project after this transcript ended."""
    current = Path(path)
    end = max((_epoch(r.get('timestamp')) or 0 for r in _rows(current)), default=0)
    if not end:
        return None
    choices = []
    for candidate in current.parent.glob('*.jsonl'):
        if candidate == current or candidate.is_symlink():
            continue
        try:
            rows = list(_rows(candidate))
            start = min((_epoch(r.get('timestamp')) for r in rows if _epoch(r.get('timestamp'))), default=0)
            if start > end and start > stamp:
                choices.append((start, rows))
        except OSError:
            continue
    return min(choices, key=lambda x: x[0])[1] if choices else None


def resolve(facade, conn=None):
    """Update estimates on collection; requests may arrive after the action."""
    owned = conn is None
    try:
        conn = conn or facade._init_trends_db()
        try:
            _schema(conn)
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            events = conn.execute('SELECT id,session_uuid,ts,context_tokens,transcript_path,savings_event_id FROM nudge_events').fetchall()
            claimed = set()
            for ident, sid, ts, before, path, event_id in events:
                try:
                    if not path or not Path(path).is_file():
                        continue
                    rows = list(_rows(path))
                    status, action, requests = classify(rows, ts)
                    if status == 'pending' or (status == 'followed' and not requests):
                        next_rows = _next_session(path, _epoch(ts))
                        if next_rows and status == 'pending':
                            status, action, requests = classify(rows, ts, next_rows)
                        elif next_rows:
                            requests = [r['message'] for r in next_rows if r.get('type') == 'assistant' and _context(r.get('message') or {}) > 0][:50]
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
                except (OSError, ValueError, TypeError, KeyError):
                    continue
            conn.commit()
        finally:
            if owned:
                conn.close()
    except Exception:
        pass


def record_emitted(facade, sid, path, quality_data, fresh, compact, advisory):
    try:
        telemetry = facade._absolute_context_tokens(quality_data, sid) or {}
        model = facade._resolve_session_model(sid)
        for kind, message in (('fresh_session', fresh), ('compact_tip', compact), ('context_size', advisory)):
            if message:
                record(facade, sid, kind, telemetry.get('tokens'), model, path)
    except Exception:
        pass
