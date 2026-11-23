"""Lane B: compact-window advice."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import math
import os
import statistics
import json
import hashlib
import re
import sys
import time

from . import limit_units, transcript_stream

LABEL = 'Earlier auto-compact'


def estimate_compaction(facade, event, prior, later, cohort_turns):
    """Estimate saved reads less a pessimistic compaction and rebuild cost."""
    before, after = event.get('before'), event.get('after')
    window, previous = event.get('window'), event.get('previous_window')
    if (event.get('manual_overlap') or not all(type(n) is int and n > 0 for n in (before, after, window, previous))
            or after >= before or before >= previous or abs(before - window) > window * .1):
        return None
    if not prior or not later or cohort_turns is None or cohort_turns <= 0:
        return None
    turns = later[:min(10, int(cohort_turns))]
    try:
        card = limit_units._resolve_card(facade, prior['model'], per_request=True, full_input=before)
        next_card = limit_units._resolve_card(facade, turns[0]['model'], per_request=True, full_input=after)
        if not card or not next_card:
            return None
        lifetime = 3600 if int(prior.get('cache_creation_1h') or 0) else 300
        gap = (event['stamp'] - prior['stamp']).total_seconds()
        if gap < 0:
            return None
        prior_write = card.get('cache_write', card['input'] * 1.25)
        request_rate = card['cache_read'] if gap <= lifetime else prior_write
        next_write = (next_card.get('cache_write_1h', next_card['input'] * 2)
                      if int(turns[0].get('cache_creation_1h') or 0)
                      else next_card.get('cache_write', next_card['input'] * 1.25))
        request_cost = before * request_rate / 1000000
        output_cost = after * card['output'] / 1000000
        rebuild_cost = after * next_write / 1000000
        net = -request_cost - output_cost - rebuild_cost
        units = -(request_cost + output_cost) * 1000000 / card['input'] - rebuild_cost * 1000000 / next_card['input']
        tokens = 0
        for turn in turns:
            actual = min(before - after, max(0, int(turn.get('cache_read') or 0)))
            turn_card = limit_units._resolve_card(facade, turn['model'], per_request=True, full_input=int(turn.get('context') or before))
            if not turn_card or not turn_card.get('input') or turn_card.get('cache_read') is None:
                return None
            value = actual * turn_card['cache_read'] / 1000000
            tokens += actual
            net += value
            units += value * 1000000 / turn_card['input']
        return {'tokens_saved': tokens, 'cost_saved_usd': net, 'net_limit_units': units,
                'own_cost_usd': request_cost + output_cost + rebuild_cost, 'turns': len(turns)}
    except (TypeError, ValueError, KeyError, ZeroDivisionError, AttributeError):
        return None


def print_savings_line(summary, cents, period, money):
    pool = summary.get('autocompact_window_estimated') or {}
    if pool.get('events'):
        print('  + est. {}: ~${}{} [estimated]'.format(LABEL, money(cents.get('autocompact_window_estimated', 0)), period))


def _recent_sessions(facade, include_paths=False, days=30):
    """Read deduplicated per-request usage and compaction boundaries, off hook paths."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    conn = facade._open_trends_db_readonly(timeout=1.0)
    try:
        paths = conn.execute('SELECT DISTINCT jsonl_path FROM session_log WHERE COALESCE(last_activity_day,date) >= ? AND COALESCE(is_sidechain,0)=0', (cutoff.date().isoformat(),)).fetchall()
    finally:
        conn.close()
    sessions = []
    seen = set()
    for (path,) in paths:
        if not path:
            continue
        turns = []
        for turn in facade.parse_session_turns(path):
            try:
                stamp = datetime.fromisoformat(turn['timestamp'].replace('Z', '+00:00'))
                stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp
            except (KeyError, ValueError, AttributeError):
                continue
            if stamp < cutoff:
                continue
            request_id = turn.get('request_id')
            if request_id and request_id in seen:
                continue
            if request_id:
                seen.add(request_id)
            turns.append(dict(turn, stamp=stamp, context=sum(max(0, int(turn.get(k) or 0)) for k in ('input_tokens', 'cache_read', 'cache_creation'))))
        if turns:
            _, boundaries, _ = facade._counted_walk_transcript(path)
            boundaries = [boundary for boundary in boundaries if boundary >= cutoff]
            sessions.append((turns, boundaries, path) if include_paths else (turns, boundaries))
    return sessions


def _utc_naive(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp


def _automatic_boundaries(path):
    found = []
    with transcript_stream.open_transcript(path) as stream:
        for line in stream:
            if 'compact_boundary' not in line:
                continue
            try:
                row = json.loads(line)
                meta = row.get('compactMetadata') or {}
                if (row.get('type') != 'system' or row.get('subtype') != 'compact_boundary'
                        or row.get('isSidechain') or meta.get('trigger') != 'auto'):
                    continue
                found.append({'stamp': _utc_naive(row['timestamp']), 'before': int(meta['preTokens']),
                              'after': int(meta['postTokens']) if meta.get('postTokens') is not None else None,
                              'boundary_id': str(row.get('uuid') or '')})
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
    return found


def _applications(facade):
    from . import apply_safe
    rows = apply_safe._receipts(facade)
    settings, _ = facade._read_settings_json()
    if (os.environ.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW') is not None or
            os.environ.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE') is not None or
            (settings.get('env') or {}).get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE') is not None):
        return []
    applications = []
    for receipt in rows:
        if receipt.get('status') != 'complete':
            continue
        for item in receipt.get('items', []):
            if item.get('kind') != 'autocompact_window' or not item.get('ok'):
                continue
            try:
                start = datetime.fromisoformat(item.get('applied_at') or receipt['ts']).astimezone(timezone.utc).replace(tzinfo=None)
                window, previous = int(item['after']), int(item['previous_window'])
                setting = item.get('setting') or {}
                setting_model = setting.get('model')
                model = item.get('main_model')
                current = ((settings.get('modelSettings') or {}).get(setting_model) or {}).get('autoCompactWindow') if setting_model else settings.get('autoCompactWindow')
                undos = [r for r in rows if r.get('undo_of') == receipt.get('receipt_id') and
                         any(i.get('id') == item['id'] and i.get('ok') for i in r.get('items', []))]
                end = min((datetime.fromisoformat(r['ts']).astimezone(timezone.utc).replace(tzinfo=None) for r in undos), default=None)
                if end is None and current != window:
                    continue
                applications.append({'start': start, 'end': end, 'window': window, 'previous_window': previous,
                                     'receipt_id': receipt['receipt_id'], 'model': model})
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return applications


def _manual_overlap(conn, sid, session_uuid, boundary_id):
    """Do not stack an estimate with an existing manual compaction receipt."""
    rows = conn.execute("SELECT detail FROM compression_events WHERE (session_id=? OR session_uuid=?) "
                        "AND feature IN ('mod_clean','mod_clean_first') UNION ALL "
                        "SELECT detail FROM savings_events WHERE (session_id=? OR session_uuid=?) "
                        "AND event_type='mod_compaction_cost'", (sid, session_uuid, sid, session_uuid)).fetchall()
    for (detail,) in rows:
        match = re.search(r'compaction_id=([A-Za-z0-9._-]+)', detail or '')
        if not match or not boundary_id or match.group(1) == boundary_id:
            return True
    return False


def refresh_bookings(facade):
    """Book observed automatic boundaries on an explicit report or dashboard path."""
    applications = _applications(facade)
    if not applications:
        return 0
    days = max(30, (datetime.now(timezone.utc).replace(tzinfo=None) - min(a['start'] for a in applications)).days + 1)
    sessions = _recent_sessions(facade, include_paths=True, days=days)
    conn = facade._init_trends_db()
    booked = 0
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS autocompact_receipts '
                     '(session_uuid TEXT NOT NULL, boundary_id TEXT NOT NULL, event_id INTEGER NOT NULL, '
                     'PRIMARY KEY(session_uuid,boundary_id))')
        conn.commit()
        for turns, all_boundaries, path in sessions:
            sid = facade.Path(path).stem
            session_uuid, unjoinable = facade._extract_session_uuid(sid)
            if not session_uuid or unjoinable:
                continue
            try:
                boundaries = _automatic_boundaries(path)
            except OSError:
                continue
            for event in boundaries:
                active = [a for a in applications if a['start'] <= event['stamp'] and (a['end'] is None or event['stamp'] < a['end'])]
                if not active:
                    continue
                application = max(active, key=lambda item: item['start'])
                earlier = [t for t in turns if t['stamp'] <= event['stamp']]
                later = [t for t in turns if t['stamp'] > event['stamp']]
                following_boundary = next((b for b in all_boundaries if b > event['stamp']), None)
                if following_boundary:
                    later = [t for t in later if t['stamp'] < following_boundary]
                if not earlier or not later or (application['model'] and earlier[-1].get('model') != application['model']):
                    continue
                if event['after'] is None:
                    event['after'] = int(later[0].get('context') or 0)
                position = len(earlier)
                cohort = [max(0, len(other) - position) for other, _, other_path in sessions if other_path != path and len(other) >= position]
                if len(cohort) < 5:
                    continue
                boundary_id = event['boundary_id'] or hashlib.sha256((sid + event['stamp'].isoformat() + str(event['before'])).encode()).hexdigest()
                if _manual_overlap(conn, sid, session_uuid, event['boundary_id']):
                    existing = conn.execute('SELECT event_id FROM autocompact_receipts WHERE session_uuid=? AND boundary_id=?', (session_uuid, boundary_id)).fetchone()
                    if existing:
                        cur = conn.execute("UPDATE savings_events SET tokens_saved=0,cost_saved_usd=0,detail=? WHERE id=? AND event_type='autocompact_window_estimated' AND (tokens_saved<>0 OR cost_saved_usd<>0)",
                                           (json.dumps({'basis': 'voided: manual compaction receipt', 'net_limit_units': 0}), existing[0]))
                        if cur.rowcount:
                            row = conn.execute('SELECT tokens_saved,cost_saved_usd FROM savings_events WHERE id=?', (existing[0],)).fetchone()
                            if row != (0, 0.0):
                                conn.rollback()
                                raise ValueError('reversal not verified')
                            conn.commit()
                    continue
                event.update(window=application['window'], previous_window=application['previous_window'])
                estimate = estimate_compaction(facade, event, earlier[-1], later, statistics.median(cohort))
                if estimate is None:
                    continue
                conn.execute('BEGIN IMMEDIATE')
                try:
                    old = conn.execute('SELECT event_id FROM autocompact_receipts WHERE session_uuid=? AND boundary_id=?', (session_uuid, boundary_id)).fetchone()
                    detail = json.dumps({'basis': 'pessimistic estimate', 'compaction_id': boundary_id,
                                         'net_limit_units': estimate['net_limit_units'], 'own_cost_usd': estimate['own_cost_usd'],
                                         'continuation_turns': estimate['turns'], 'receipt_id': application['receipt_id']}, sort_keys=True)
                    if old:
                        cur = conn.execute("UPDATE savings_events SET tokens_saved=?,cost_saved_usd=?,detail=? WHERE id=? AND event_type='autocompact_window_estimated' AND (tokens_saved<>? OR cost_saved_usd<>? OR detail<>?)",
                            (estimate['tokens_saved'], estimate['cost_saved_usd'], detail, old[0], estimate['tokens_saved'], estimate['cost_saved_usd'], detail))
                        if cur.rowcount:
                            row = conn.execute('SELECT tokens_saved,cost_saved_usd,detail FROM savings_events WHERE id=?', (old[0],)).fetchone()
                            if row != (estimate['tokens_saved'], estimate['cost_saved_usd'], detail):
                                raise ValueError('re-estimate not verified')
                            conn.commit()
                        else:
                            conn.rollback()
                        continue
                    local_stamp = event['stamp'].replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
                    cur = conn.execute('INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail,model,unjoinable) VALUES(?,?,?,?,?,?,?,?,?)',
                        (local_stamp, 'autocompact_window_estimated', estimate['tokens_saved'], estimate['cost_saved_usd'], sid, session_uuid, detail, earlier[-1].get('model'), 0))
                    if cur.rowcount != 1:
                        raise ValueError('event not written')
                    event_id = cur.lastrowid
                    cur = conn.execute('INSERT INTO autocompact_receipts VALUES(?,?,?)', (session_uuid, boundary_id, event_id))
                    if cur.rowcount != 1 or conn.execute('SELECT event_id FROM autocompact_receipts WHERE session_uuid=? AND boundary_id=?', (session_uuid, boundary_id)).fetchone() != (event_id,):
                        raise ValueError('receipt not written')
                    conn.commit()
                    booked += 1
                except Exception:
                    conn.rollback()
                    raise
    finally:
        conn.close()
    return booked


_last_refresh = 0.0


def refresh_if_allowed(facade, explicit=False):
    """Never walk transcripts from hooks; refresh only reports and dashboard processes."""
    global _last_refresh
    program = os.path.basename(sys.argv[0]).lower()
    dashboard = program == 'dashboard-server.py'
    if not explicit and not dashboard:
        return
    if time.monotonic() - _last_refresh < 60:
        return
    _last_refresh = time.monotonic()
    try:
        refresh_bookings(facade)
    except Exception:
        return


def _median_compaction_cost(facade):
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    conn = facade._open_trends_db_readonly(timeout=1.0)
    try:
        costs = [abs(float(row[0])) for row in conn.execute(
            "SELECT cost_saved_usd FROM savings_events WHERE event_type='mod_compaction_cost' AND timestamp >= ? AND cost_saved_usd < 0", (cutoff,))]
    finally:
        conn.close()
    return statistics.median(costs) if costs else None


def recommend(facade):
    """Recommend only when observed history and exact pricing support a net gain."""
    if getattr(facade, '_is_foreign_runtime', lambda: False)():
        return None
    if os.environ.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW') is not None:
        return {'blocked': 'env', 'text': 'The environment override controls the compact window.'}
    if os.environ.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE') is not None:
        return {'blocked': 'env', 'text': 'The existing percentage override changes the effective compact window.'}
    try:
        sessions = _recent_sessions(facade)
        models = Counter(t.get('model') for turns, _ in sessions for t in turns if t.get('model'))
        if not models:
            return None
        model = models.most_common(1)[0][0]
        if facade._context_window_for_model_str(model) != 1000000:
            return None
        sessions = [([turn for turn in turns if turn.get('model') == model], boundaries)
                    for turns, boundaries in sessions]
        sessions = [(turns, boundaries) for turns, boundaries in sessions if turns]
        settings, _ = facade._read_settings_json()
        if (settings.get('env') or {}).get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE') is not None:
            return {'blocked': 'env', 'text': 'The existing percentage override changes the effective compact window.'}
        current, _ = facade.effective_compact_window(model, env=os.environ, settings=settings)
        if current <= 500000:
            return None
        peaks = [max(t['context'] for t in turns) for turns, _ in sessions]
        if sum(peak >= 300000 for peak in peaks) < 20:
            return None
        p75 = sorted(peaks)[max(0, math.ceil(len(peaks) * .75) - 1)]
        target = 400000 if p75 < 300000 else 500000 if p75 > 450000 else 450000
        gross = 0.0
        crossings = 0
        happened = 0
        for turns, boundaries in sessions:
            previous = 0
            boundary_index = 0
            for turn in turns:
                while boundary_index < len(boundaries) and boundaries[boundary_index] <= turn['stamp']:
                    previous = 0
                    boundary_index += 1
                context = turn['context']
                if previous < target <= context:
                    crossings += 1
                previous = context
                if context > target:
                    card = limit_units._resolve_card(facade, turn.get('model'), per_request=True, full_input=context)
                    if not card or card.get('cache_read') is None:
                        return None
                    gross += (context - target) * card['cache_read'] / 1000000
            for boundary in boundaries:
                before = [t for t in turns if t['stamp'] <= boundary]
                if before:
                    happened += 1
        extra = max(0, crossings - happened)
        card = limit_units._resolve_card(facade, model, per_request=True, full_input=target)
        cost = _median_compaction_cost(facade) if extra else 0
        write_rate = card.get('cache_write_1h', card.get('input') * 2 if card.get('input') is not None else None) if card else None
        if write_rate is None or cost is None:
            return None
        own_cost = extra * (cost + target * .15 * write_rate / 1000000)
        net = gross - own_cost
        if net <= 0:
            return None
        per_model = 'autoCompactWindow' in ((settings.get('modelSettings') or {}).get(model) or {})
        return {'model': model, 'target': target, 'current': current, 'gross_usd_month': gross,
                'own_cost_usd_month': own_cost, 'net_usd_month': net, 'per_model': per_model,
                'text': 'Compacting earlier keeps less history in context. Token Optimizer saves a checkpoint before each compaction.'}
    except Exception:
        return None


def plan_item(facade, advice=None):
    if advice is None:
        advice = recommend(facade)
    if not advice or advice.get('blocked'):
        return None
    return dict(advice, id='autocompact_window', kind='autocompact_window',
                label='Compact earlier' + (' for ' + advice['model'] if advice['per_model'] else ''),
                tokens_per_request=0, basis=advice['text'], default_selected=True,
                undo='measure.py apply-safe --undo <receipt_id>')


def apply_item(facade, item):
    """Write the approved window at the setting that currently takes precedence."""
    if getattr(facade, '_is_foreign_runtime', lambda: False)():
        return {'ok': False}
    target, model = item.get('target'), item.get('model')
    if target not in (400000, 450000, 500000) or not isinstance(model, str) or not model:
        return {'ok': False}
    settings, ok = facade._read_settings_for_write()
    if not ok or not isinstance(settings, dict):
        return {'ok': False}
    models = settings.get('modelSettings')
    model_settings = models.get(model) if isinstance(models, dict) else None
    per_model = isinstance(model_settings, dict) and 'autoCompactWindow' in model_settings
    container = model_settings if per_model else settings
    previous = container.get('autoCompactWindow')
    existed = 'autoCompactWindow' in container
    container['autoCompactWindow'] = target
    if not facade._write_settings_atomic(settings, user_initiated=True):
        return {'ok': False}
    return {'ok': True, 'before': previous, 'before_exists': existed, 'after': target,
            'model': model if per_model else None}


def undo_item(facade, receipt):
    """Restore only this setting, refusing to overwrite a later edit."""
    if getattr(facade, '_is_foreign_runtime', lambda: False)():
        return False
    settings, ok = facade._read_settings_for_write()
    if not ok or not isinstance(settings, dict):
        return False
    model = receipt.get('model')
    if model is not None:
        models = settings.get('modelSettings')
        container = models.get(model) if isinstance(models, dict) else None
    else:
        container = settings
    if not isinstance(container, dict) or container.get('autoCompactWindow') != receipt.get('after'):
        return False
    if receipt.get('before_exists'):
        container['autoCompactWindow'] = receipt['before']
    else:
        container.pop('autoCompactWindow', None)
    allow = {'autoCompactWindow'} if model is None and not receipt.get('before_exists') else None
    return bool(facade._write_settings_atomic(settings, allow_removing_keys=allow, user_initiated=True))


def main(facade, args):
    """Show advice without changing settings."""
    if any(arg != '--json' for arg in args):
        print('Usage: measure.py autocompact-advice [--json]')
        return 2
    advice = recommend(facade)
    if '--json' in args:
        print(json.dumps(advice))
    elif advice is None:
        print('No compact-window recommendation is available.')
    elif advice.get('blocked'):
        print(advice['text'])
    else:
        location = 'model-specific setting' if advice['per_model'] else 'top-level setting'
        print('Recommended window: {:,} ({}, estimated net ${:.2f}/month).'.format(advice['target'], location, advice['net_usd_month']))
        print(advice['text'])
    return 0
