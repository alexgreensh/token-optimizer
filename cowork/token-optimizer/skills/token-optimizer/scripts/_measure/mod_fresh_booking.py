"""Estimated-only receipts for a mod's successful Start fresh action."""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import re
import statistics

_ID = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
_MIN_COHORT = 5
_MAX_TURNS = 10


def record(facade, event):
    """Capture one old session; derive a capped continuation estimate from prior sessions."""
    try:
        if not isinstance(event, dict) or event.get('action') != 'fresh':
            return {'ok': False, 'reason': 'unsupported-action'}
        sid, ident = event.get('session_id'), event.get('action_id')
        if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (sid, ident)):
            return {'ok': False, 'reason': 'invalid-identity'}
        uuid, unjoinable = facade._extract_session_uuid(sid)
        if not uuid or unjoinable:
            return {'ok': False, 'reason': 'session-unjoinable'}
        stamp, tokens = float(event['timestamp']), event.get('before_tokens')
        handoff = event.get('handoff_tokens_estimate')
        if (not math.isfinite(stamp) or stamp <= 0 or (tokens is not None and (type(tokens) is not int or tokens < 0))
                or (handoff is not None and (type(handoff) is not int or handoff < 0))):
            return {'ok': False, 'reason': 'invalid-evidence'}
        tokens = max(0, tokens - handoff) if tokens is not None and handoff is not None else None
        hint = event.get('transcript_path')
        if not hint:
            candidates = list((facade.CLAUDE_DIR / 'projects').glob('*/' + sid + '.jsonl'))
            hint = candidates[0] if len(candidates) == 1 else None
        path = Path(hint) if hint else None
        usable = bool(path and path.name == sid + '.jsonl' and not path.is_symlink() and path.is_file()
                      and path.stat().st_size <= 128 * 1024 * 1024)
        usage, model, _ = facade._keepwarm_last_usage(path) if usable else (None, None, None)
        turns, _, _ = facade._counted_walk_transcript(path) if usable else ([], [], [])
        cutoff = datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
        prior_turns = sum(1 for turn in turns if turn[0] <= cutoff) if usable else None
        tier = facade.PRICING_TIERS[facade._load_pricing_tier()]
        card = facade._counted_exact_card(model, tier, tokens) if model and tokens else None
        conn = facade._init_trends_db()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS mod_fresh_receipts '
                         '(action_id TEXT PRIMARY KEY, session_uuid TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL, '
                         'dropped_tokens INTEGER, prior_turns INTEGER, estimated_turns REAL, '
                         'estimated_usd REAL, cohort_size INTEGER NOT NULL, model TEXT, '
                         'detail TEXT NOT NULL)')
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT action_id, session_uuid FROM mod_fresh_receipts '
                                    'WHERE action_id=? OR session_uuid=?', (ident, uuid)).fetchall()
            if previous:
                conn.rollback()
                return {'ok': all(row[1] == uuid for row in previous), 'duplicate': True}
            start = (cutoff - timedelta(days=90)).date().isoformat()
            end = cutoff.date().isoformat()
            calls = [int(row[0]) for row in conn.execute(
                'SELECT api_calls FROM session_log WHERE date >= ? AND date < ? AND session_uuid != ? '
                'AND COALESCE(is_sidechain,0)=0 AND api_calls >= ? ORDER BY date DESC LIMIT 1000',
                (start, end, uuid, prior_turns)) if row[0] is not None] if prior_turns else []
            priced = bool(usage and tokens and card and isinstance(card.get('cache_read'), (int, float)))
            estimated_turns = min(_MAX_TURNS, statistics.median(max(0, n - prior_turns) for n in calls)) if priced and len(calls) >= _MIN_COHORT else None
            estimate = tokens * estimated_turns * card['cache_read'] / 1e6 if estimated_turns is not None else None
            cur = conn.execute('INSERT INTO mod_fresh_receipts(action_id,session_uuid,timestamp,dropped_tokens,prior_turns,'
                'estimated_turns,estimated_usd,cohort_size,model,detail) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (ident, uuid, datetime.fromtimestamp(stamp).isoformat(), tokens, prior_turns, estimated_turns,
                 estimate, len(calls), model, 'from the Claude Code mod; estimated old context less handoff; persistent setup may remain; continuation capped at 10 turns'))
            if cur.rowcount != 1:
                raise ValueError('receipt not written')
            conn.commit()
            return {'ok': True, 'duplicate': False, 'estimated_usd': estimate, 'estimated_turns': estimated_turns}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception:
        return {'ok': False, 'reason': 'receipt-unavailable'}


def summary(facade, cutoff):
    """Return an estimated-only pool; missing history is reported as unpriced."""
    try:
        conn = facade._open_trends_db_readonly(timeout=1.0)
        try:
            row = conn.execute('SELECT count(*), sum(estimated_usd), sum(estimated_usd IS NULL) '
                               'FROM mod_fresh_receipts WHERE timestamp >= ?', (cutoff,)).fetchone()
        finally:
            conn.close()
        return {'events': row[0], 'cost_saved_usd': float(row[1] or 0),
                'unpriced_events': int(row[2] or 0), 'evidence': 'estimated'} if row[0] else None
    except Exception:
        return None


def additive(pool, handover):
    """Do not stack two overlapping counterfactuals when identity is unavailable."""
    try:
        if float((handover or {}).get('cost_saved_usd', 0) or 0) > 0:
            return 0.0
        return max(0.0, float((pool or {}).get('cost_saved_usd', 0) or 0))
    except (TypeError, ValueError):
        return 0.0
