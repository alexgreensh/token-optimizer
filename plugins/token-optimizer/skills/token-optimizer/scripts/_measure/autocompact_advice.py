"""Lane B: compact-window advice."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import math
import os
import statistics
import json

from . import limit_units

LABEL = 'Earlier auto-compact'


def print_savings_line(summary, cents, period, money):
    pool = summary.get('autocompact_window_estimated') or {}
    if pool.get('events'):
        print('  + est. {}: ~${}{} [estimated]'.format(LABEL, money(cents.get('autocompact_window_estimated', 0)), period))


def _recent_sessions(facade):
    """Read deduplicated per-request usage and compaction boundaries, off hook paths."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)
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
            sessions.append((turns, [boundary for boundary in boundaries if boundary >= cutoff]))
    return sessions


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
                'text': 'Compacting earlier keeps less history in context. Token Optimizer saves a checkpoint before each compaction. Savings after applying are not yet recorded.'}
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
