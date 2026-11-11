"""Book a verified manual compaction and its actual model cost atomically."""
from datetime import datetime
import json
import math
from pathlib import Path
import re

_ID = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
_USAGE = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def _boundary(facade, event, sid, compact_id):
    path = Path(event['transcript_path'])
    if path.name != sid + '.jsonl' or path.is_symlink() or path.stat().st_size > 128 * 1024 * 1024:
        return None
    found, last_model = [], None
    started = float(event.get('started_at', event['timestamp']))
    finished = float(event['timestamp'])
    if not math.isfinite(started) or not math.isfinite(finished) or started > finished:
        return None
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if 'compact_boundary' not in line and '"assistant"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get('isSidechain'):
                continue
            if row.get('type') == 'assistant' and isinstance(row.get('message'), dict):
                last_model = row['message'].get('model') or last_model
            if (row.get('type') == 'system' and row.get('subtype') == 'compact_boundary'
                    and row.get('sessionId') == sid and (row.get('compactMetadata') or {}).get('trigger') == 'manual'):
                stamp = facade._keepwarm_iso_to_epoch(row.get('timestamp'))
                rid = row.get('uuid')
                if (isinstance(rid, str) and _ID.fullmatch(rid) and (not compact_id or rid == compact_id)
                        and stamp is not None and started - 1 <= stamp <= finished + 120):
                    found.append((rid, stamp, last_model))
    return found[0] if len(found) == 1 else None


def _record_verified(facade, event):
    """Write one counted removal and its cost only after matching a manual boundary."""
    try:
        if not isinstance(event, dict) or event.get('action') not in ('clean', 'clean-first'):
            return {'ok': False, 'reason': 'unsupported-action'}
        sid, ident, compact_id = (event.get(key) for key in ('session_id', 'action_id', 'compaction_id'))
        if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (sid, ident)) or (compact_id is not None and (not isinstance(compact_id, str) or not _ID.fullmatch(compact_id))):
            return {'ok': False, 'reason': 'invalid-identity'}
        uuid, unjoinable = facade._extract_session_uuid(sid)
        if not uuid or unjoinable:
            return {'ok': False, 'reason': 'session-unjoinable'}
        before, after = event['before_tokens'], event['after_tokens']
        if type(before) is not int or type(after) is not int or not 0 <= after < before:
            return {'ok': False, 'reason': 'invalid-token-delta'}
        usage = event['usage']
        if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in _USAGE):
            return {'ok': False, 'reason': 'usage-unavailable'}
        ttl, creation = event.get('ttl_kind'), usage['cache_creation_input_tokens']
        if (creation or event['action'] == 'clean-first') and ttl not in ('1h', '5m'):
            return {'ok': False, 'reason': 'cache-write-class-unavailable'}
        boundary = _boundary(facade, event, sid, compact_id)
        if boundary is None:
            return {'ok': False, 'reason': 'manual-boundary-unverified'}
        compact_id, timestamp, preceding_model = boundary
        model = event.get('model') or preceding_model
        if not facade._is_priced_model(model):
            return {'ok': False, 'reason': 'model-unpriced'}
        cost = event.get('cost_usd')
        if (isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or
                cost < 0 or (cost == 0 and any(usage[k] for k in _USAGE))):
            return {'ok': False, 'reason': 'cost-unavailable'}
        expected = facade._get_model_cost(model, usage['input_tokens'], usage['output_tokens'],
            usage['cache_read_input_tokens'], creation, cache_create_1h=creation if ttl == '1h' else 0,
            cache_create_5m=creation if ttl == '5m' else 0, per_request=True)
        if not math.isfinite(expected) or abs(cost - expected) > max(0.00001, expected * 0.01):
            return {'ok': False, 'reason': 'cost-mismatch'}
        conn = facade._init_trends_db()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS mod_action_receipts '
                         '(action_id TEXT PRIMARY KEY, session_uuid TEXT NOT NULL, compaction_id TEXT NOT NULL, '
                         'compression_event_id INTEGER NOT NULL, cost_event_id INTEGER NOT NULL, action TEXT NOT NULL, ttl_kind TEXT, '
                         'UNIQUE(session_uuid, compaction_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS mod_action_observations '
                         '(action_id TEXT PRIMARY KEY, session_uuid TEXT NOT NULL, action TEXT NOT NULL, '
                         'timestamp TEXT NOT NULL, reason TEXT NOT NULL)')
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            observed = conn.execute('SELECT session_uuid, action FROM mod_action_observations WHERE action_id=?', (ident,)).fetchone()
            if observed and observed != (uuid, event['action']):
                conn.rollback()
                return {'ok': False, 'reason': 'identity-conflict'}
            old = conn.execute('SELECT action_id, session_uuid, compaction_id, action FROM mod_action_receipts '
                               'WHERE action_id = ? OR (session_uuid = ? AND compaction_id = ?)',
                               (ident, uuid, compact_id)).fetchall()
            if old:
                conn.rollback()
                if any(r[0] == ident and r[3] != event['action'] for r in old):
                    return {'ok': False, 'reason': 'identity-conflict'}
                if all(r[1:3] == (uuid, compact_id) for r in old):
                    return {'ok': True, 'duplicate': True, 'canonical_action_id': old[0][0]}
                return {'ok': False, 'reason': 'identity-conflict'}
            marker = 'compaction_id=' + compact_id
            # Existing compaction credits with the same identity take precedence.
            existing = conn.execute("SELECT 1 FROM savings_events WHERE session_uuid = ? AND detail LIKE ? "
                                    "UNION ALL SELECT 1 FROM compression_events WHERE session_uuid = ? AND detail LIKE ? LIMIT 1",
                                    (uuid, '%' + marker + '%', uuid, '%' + marker + '%')).fetchone()
            if existing:
                conn.rollback()
                return {'ok': False, 'reason': 'existing-compaction-credit'}
            stamp = datetime.fromtimestamp(timestamp).isoformat()
            feature = 'mod_clean_first' if event['action'] == 'clean-first' else 'mod_clean'
            detail = 'from the Claude Code mod; compaction cost from engine session cost delta; ' + marker
            cur = conn.execute('INSERT INTO compression_events(timestamp,session_id,session_uuid,feature,command_pattern,'
                'original_tokens,compressed_tokens,compression_ratio,quality_preserved,verified,detail,model,tier) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (stamp, sid, uuid, feature, 'manual-compaction', before, after,
                after / before, 0, 1, detail, model, 'measured'))
            if cur.rowcount != 1:
                raise ValueError('removal not written')
            compression_id = cur.lastrowid
            cur = conn.execute('INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,'
                'session_uuid,detail,model,unjoinable) VALUES(?,?,?,?,?,?,?,?,?)',
                (stamp, 'mod_compaction_cost', 0, -cost, sid, uuid, detail, model, 0))
            if cur.rowcount != 1:
                raise ValueError('cost not written')
            cost_id = cur.lastrowid
            cur = conn.execute('INSERT INTO mod_action_receipts(action_id,session_uuid,compaction_id,compression_event_id,cost_event_id,action,ttl_kind) '
                'VALUES(?,?,?,?,?,?,?)', (ident, uuid, compact_id, compression_id, cost_id, event['action'], ttl))
            if cur.rowcount != 1:
                raise ValueError('receipt not written')
            conn.commit()
            return {'ok': True, 'duplicate': False, 'removed_tokens': before - after, 'cost_usd': cost}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception:
        return {'ok': False, 'reason': 'receipt-unavailable'}



def _observe(facade, event, reason):
    try:
        sid, ident, action = event.get('session_id'), event.get('action_id'), event.get('action')
        stamp = float(event['timestamp'])
        if (any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (sid, ident)) or
                action not in ('clean', 'clean-first') or not math.isfinite(stamp) or stamp <= 0):
            return False
        uuid, unjoinable = facade._extract_session_uuid(sid)
        if not uuid or unjoinable:
            return False
        conn = facade._init_trends_db()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS mod_action_observations '
                         '(action_id TEXT PRIMARY KEY, session_uuid TEXT NOT NULL, action TEXT NOT NULL, '
                         'timestamp TEXT NOT NULL, reason TEXT NOT NULL)')
            cur = conn.execute('INSERT OR IGNORE INTO mod_action_observations(action_id,session_uuid,action,timestamp,reason) '
                'VALUES(?,?,?,?,?)', (ident, uuid, action, datetime.fromtimestamp(stamp).isoformat(), reason))
            conn.commit()
            row = conn.execute('SELECT session_uuid,action FROM mod_action_observations WHERE action_id=?', (ident,)).fetchone()
            return cur.rowcount == 1 or row == (uuid, action)
        finally:
            conn.close()
    except Exception:
        return False


def _existing_receipt(facade, event):
    try:
        sid = event['session_id']
        uuid, unjoinable = facade._extract_session_uuid(sid)
        boundary = _boundary(facade, event, sid, event.get('compaction_id'))
        if unjoinable or not uuid or not boundary:
            return None
        conn = facade._open_trends_db_readonly(timeout=1.0)
        try:
            row = conn.execute('SELECT action_id FROM mod_action_receipts WHERE session_uuid=? AND compaction_id=?',
                               (uuid, boundary[0])).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def record(facade, event):
    """Record a verified compaction, or retain a confirmed but unpriced action."""
    result = _record_verified(facade, event)
    if result.get('ok') and result.get('duplicate'):
        if result.get('canonical_action_id') == event.get('action_id') and not _observe(facade, event, 'verified'):
            return {'ok': False, 'reason': 'observation-unavailable'}
        return dict(result, priced=True)
    if result.get('ok'):
        return dict(result, priced=True) if _observe(facade, event, 'verified') else {'ok': False, 'reason': 'observation-unavailable'}
    if isinstance(event, dict) and (event.get('observed_manual') is True or event.get('observed_action') is True):
        canonical = _existing_receipt(facade, event)
        if canonical:
            return {'ok': True, 'priced': True, 'duplicate': True, 'canonical_action_id': canonical}
    if (isinstance(event, dict) and (event.get('observed_manual') is True or event.get('observed_action') is True) and result.get('reason') in
            ('usage-unavailable', 'cost-unavailable', 'cost-mismatch', 'cache-write-class-unavailable', 'model-unpriced',
             'manual-boundary-unverified', 'receipt-unavailable')):
        if _observe(facade, event, result.get('reason')):
            return {'ok': True, 'priced': False, 'reason': result.get('reason')}
    return result


def summary(facade, cutoff):
    try:
        conn = facade._open_trends_db_readonly(timeout=1.0)
        try:
            has_receipts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mod_action_receipts'").fetchone()
            join = 'LEFT JOIN mod_action_receipts r ON r.action_id=o.action_id' if has_receipts else ''
            unpriced = 'sum(r.action_id IS NULL)' if has_receipts else 'count(*)'
            row = conn.execute('SELECT count(*),' + unpriced + ' FROM mod_action_observations o ' + join +
                               ' WHERE o.timestamp >= ?', (cutoff,)).fetchone()
        finally:
            conn.close()
        return {'events': row[0], 'unpriced_events': int(row[1] or 0)} if row[0] else None
    except Exception:
        return None

def adjust_score(facade, action, ttl, turns, event_time, tokens, tier_data, original):
    """Put post-compaction reads in the counted stream; add one cold write for clean-first."""
    import bisect
    score = dict(original)
    if action not in ('clean', 'clean-first') or not score.get('observed'):
        return score
    first = bisect.bisect_right([turn[0] for turn in turns], event_time)
    if first >= len(turns):
        return score
    turn = turns[first]
    card = facade._counted_exact_card(turn[1], tier_data, turn[2] if len(turn) > 2 else None)
    for key in ('oneshot_unpriced_credit_tokens', 'oneshot_unpriced_debit_tokens', 'priced_oneshot_events',
                'oneshot_class_events', 'oneshot_floor_events', 'oneshot_floor_tokens'):
        score[key] = 0
    score['oneshot_usd'] = 0.0
    score['oneshot_class_premium_usd'] = 0.0
    score['reread_tokens'] += tokens
    score['turns_counted'] += 1
    read_rate = card.get('cache_read') if card else None
    if isinstance(read_rate, (int, float)) and math.isfinite(read_rate) and read_rate >= 0:
        score['reread_usd'] += tokens * read_rate / 1e6
        score['priced_reread_turns'] += 1
    else:
        score['reread_unpriced_credit_tokens'] += tokens
    cache_class = turn[3] if len(turn) > 3 else None
    observed_write = isinstance(cache_class, tuple) and len(cache_class) >= 3 and ((ttl == '1h' and cache_class[1] > 0) or (ttl == '5m' and cache_class[2] > 0))
    if action == 'clean-first' and observed_write:
        write_rate = card.get('cache_write_1h' if ttl == '1h' else 'cache_write') if card and ttl in ('1h', '5m') else None
        if isinstance(write_rate, (int, float)) and math.isfinite(write_rate) and write_rate >= 0:
            score['reread_usd'] += tokens * write_rate / 1e6
        else:
            score['reread_unpriced_credit_tokens'] += tokens
    return facade._counted_coverage_totals(score)


def event_features(conn):
    """Find mod removals once per counted pass, including unlinked older rows."""
    try:
        has_receipts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mod_action_receipts'").fetchone()
        join = 'LEFT JOIN mod_action_receipts r ON r.compression_event_id=c.id' if has_receipts else ''
        ttl = 'r.ttl_kind' if has_receipts else 'NULL'
        rows = conn.execute('SELECT c.id,c.feature,' + ttl + ' FROM compression_events c ' + join +
                            " WHERE c.feature IN ('mod_clean','mod_clean_first')")
        return {'ce:' + str(row[0]): ('clean-first' if row[1] == 'mod_clean_first' else 'clean', row[2]) for row in rows}
    except Exception:
        return {}
