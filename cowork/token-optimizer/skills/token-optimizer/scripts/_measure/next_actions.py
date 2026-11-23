"""Read-only next steps, grounded in the user's own recent savings data."""
import importlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

IDS = ('apply_safe', 'cold_fresh', 'skill_list', 'writes', 'stale_reads', 'routing')


def _now():
    return datetime.now(timezone.utc)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
        if not hasattr(os, 'fchmod'):
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _call(module, function, *args):
    try:
        return getattr(importlib.import_module('.' + module, __package__), function)(*args)
    except Exception:
        return None


def _summary(facade):
    # Only a complete 30-day summary can substitute for the report calculation.
    candidates = []
    try:
        directory = facade._status_bar_dir()
        candidates.extend(Path(directory).glob('*.json'))
    except Exception:
        pass
    for name in ('dashboard.json', 'dashboard_data.json', 'savings_summary.json'):
        candidates.append(Path(facade.SNAPSHOT_DIR) / name)
    fresh = []
    try:
        path = Path(facade.DASHBOARD_PATH)
        stamp = path.stat().st_mtime
        if 0 <= time.time() - stamp < 86400:
            html = path.read_text(encoding='utf-8')
            marker = 'window.__TOKEN_DATA__ = '
            start = html.index(marker) + len(marker)
            data, _ = json.JSONDecoder().raw_decode(html[start:])
            summary = data.get('savings') if isinstance(data, dict) else None
            if isinstance(summary, dict) and summary.get('period_days') == 30:
                fresh.append((stamp, summary))
    except (AttributeError, OSError, ValueError, TypeError, UnicodeError):
        pass
    for path in candidates:
        try:
            data = _read(path)
            stamp = data.get('computed_at', path.stat().st_mtime)
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            age = time.time() - float(stamp)
            summary = data.get('savings', data)
            if (0 <= age < 86400 and isinstance(summary, dict)
                    and summary.get('period_days') == 30
                    and any(k in summary for k in ('output_waste', 'stale_reads_reclaimable', 'model_routing'))):
                fresh.append((float(stamp), summary))
        except (OSError, ValueError, TypeError, OverflowError):
            continue
    if fresh:
        stamp, summary = max(fresh, key=lambda item: item[0])
        return summary, datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    try:
        summary = facade._get_merged_savings(days=30)
        if not isinstance(summary, dict):
            summary = {}
    except Exception:
        summary = {}
    if 'skill_listing_budget' not in summary:
        listing = None
        try:
            from . import structural_report
            listing = structural_report.skill_listing_budget(days=30, f=facade)
        except Exception:
            pass
        if listing is not None:
            summary = dict(summary, skill_listing_budget=listing)
    return summary, None


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _build(facade, summary, as_of, show_all, plan=None):
    if plan is None:
        plan = _call('apply_safe', 'build_plan', facade)
    plan = plan if isinstance(plan, dict) else {}
    items = plan.get('items') if isinstance(plan.get('items'), list) else []
    pool = _call('cold_guard', 'pool', facade)
    pool = pool if isinstance(pool, dict) else {}
    counted = _call('counted_headline', 'build', facade)
    counted = counted if isinstance(counted, dict) else None
    listing = summary.get('skill_listing_budget') or {}
    listing = listing if isinstance(listing, dict) else {}
    def part(key):
        value = summary.get(key)
        return value if isinstance(value, dict) else {}
    candidates = [
        ('apply_safe', 'Apply safe savings ({} changes, one approval)'.format(len(items)), plan.get('total_net_usd_month'), 'firm', 'Based on your last 30 days, after each change’s own costs.', 'measure.py apply-safe'),
        ('skill_list', 'Trim the skill list', listing.get('cost_usd') if listing.get('available') else None, 'firm', 'Based on skill list growth and your calls in the last 30 days.', 'Open the dashboard Manage section and find the skill list fix.'),
        ('cold_fresh', 'Start fresh on cold big sessions', pool.get('cost_saved_usd', pool.get('cost_usd')), 'upper', 'Based on cold sessions in your last 30 days.', 'Start fresh when you return after an hour: /clear, then your message'),
        ('writes', 'Use targeted edits', part('output_waste').get('cost_saved_usd'), 'upper', 'Based on full-file writes in your last 30 days.', 'Use targeted edits instead of rewriting whole files.'),
        ('stale_reads', 'Avoid stale re-reads', part('stale_reads_reclaimable').get('cost_usd'), 'upper', 'Based on stale re-reads in your last 30 days.', 'Avoid re-reading unchanged files or use .contextignore.'),
        ('routing', 'Review model routing', part('model_routing').get('potential_cost_usd'), 'upper', 'Based on your model usage in the last 30 days.', 'Open the routing section of the dashboard.'),
    ]
    declined = _read(Path(facade.SNAPSHOT_DIR) / 'next_declined.json')
    disabled = _call('routing_default', 'routing_disabled_at', facade) is not None
    try:
        from . import routing_default
        disabled = disabled or routing_default._env_disabled(os.environ)
    except Exception:
        pass
    overlap = any(isinstance(item, dict) and (item.get('kind') in ('cold_guard', 'keepwarm_auto') or str(item.get('id', '')).split(':')[0] in ('cold_guard', 'keepwarm_auto')) for item in items)
    actions, hidden = [], []
    for ident, label, value, bound, basis, how in candidates:
        number = _number(value)
        reason = ('routing-off' if ident == 'routing' and disabled else
                  'declined' if ident in declined else
                  'overlap' if ident == 'cold_fresh' and overlap else
                  'not-positive' if number is None or number <= 0 else None)
        if reason:
            hidden.append({'id': ident, 'why': reason})
        else:
            actions.append({'id': ident, 'label': label, 'net_usd_month': number,
                            'bound': bound, 'basis': basis, 'how': how})
    actions.sort(key=lambda action: (action['bound'] != 'firm', -action['net_usd_month']))
    return {'schema': 1, 'as_of': as_of, 'counted': counted,
            'actions': actions if show_all else actions[:5], 'hidden': hidden}


def build(facade, show_all=False):
    summary, cached = _summary(facade)
    result = _build(facade, summary, cached or _now().isoformat(), show_all)
    if cached:
        result['cached'] = True
    return result


def main(facade, args):
    if args and args[0] in ('decline', 'restore'):
        if len(args) != 2 or args[1] not in IDS:
            print('Unknown action. Run measure.py next --all.')
            return 2
        path = Path(facade.SNAPSHOT_DIR) / 'next_declined.json'
        declined = _read(path)
        if args[0] == 'decline':
            declined[args[1]] = _now().date().isoformat()
        else:
            declined.pop(args[1], None)
        _write(path, declined)
        print('{} {}.'.format('Hidden' if args[0] == 'decline' else 'Restored', args[1]))
        return 0
    if any(arg not in ('--json', '--all') for arg in args):
        print('Usage: measure.py next [--json] [--all]')
        return 2
    result = build(facade, '--all' in args)
    if '--json' in args:
        print(json.dumps(result, indent=2))
        return 0
    print('Token Optimizer')
    display = (result.get('counted') or {}).get('display') or {}
    if display.get('short'):
        print('  {}. This number only drops with a stated correction.'.format(display['short']))
    if result.get('cached'):
        print('  as of {}'.format(result['as_of']))
    if not result['actions']:
        print('Nothing to do right now. You are set.')
    else:
        print("Do next. Worth per month on your own last 30 days, after each action's own costs:")
        for n, action in enumerate(result['actions'], 1):
            value = action['net_usd_month']
            amount = ('${:,.2f}/mo' if value < 10 and value % 1 else '${:,.0f}/mo').format(value)
            print('  {}. {}    {}{}  {}'.format(n, action['label'], 'up to ' if action['bound'] == 'upper' else '', amount, action['how']))
    if any(item['id'] == 'routing' and item['why'] == 'routing-off' for item in result['hidden']):
        print('Hidden: model routing (you turned it off). Everything: measure.py next --all')
    return 0


def on_savings_refresh(facade, summary):
    if not isinstance(summary, dict) or summary.get('period_days', 30) != 30:
        return
    now = _now()
    cache_path = Path(facade.SNAPSHOT_DIR) / 'next_apply_plan.json'
    cached = _read(cache_path)
    try:
        age = (now - datetime.fromisoformat(cached['computed_at'])).total_seconds()
        plan = cached['plan'] if 0 <= age < 3600 and isinstance(cached['plan'], dict) else None
    except (KeyError, TypeError, ValueError):
        plan = None
    if plan is None:
        plan = _call('apply_safe', 'build_plan', facade)
        if isinstance(plan, dict):
            _write(cache_path, {'computed_at': now.isoformat(), 'plan': plan})
        else:
            plan = {}
    result = _build(facade, summary, now.isoformat(), True, plan=plan)
    action = next((item for item in result['actions'] if item['bound'] == 'firm'), None)
    directory = Path(facade.SNAPSHOT_DIR)
    history_path = directory / 'next_action_history.json'
    history = _read(history_path)
    output = {}
    if action and action['net_usd_month'] >= 5:
        ident, amount = action['id'], action['net_usd_month']
        record = history.get(ident, {})
        try:
            first = datetime.fromisoformat(record['first_seen'])
            age = (now - first).total_seconds() / 86400
            old_amount = float(record['net_usd_month'])
        except (KeyError, ValueError, TypeError):
            age, old_amount = 0, amount
            record = {'first_seen': now.isoformat(), 'net_usd_month': amount}
        if amount >= 2 * old_amount or age >= 37:
            record = {'first_seen': now.isoformat(), 'net_usd_month': amount}
            age = 0
        history[ident] = record
        if age < 7:
            short = '+${:,.0f}/mo ready: /token-optimizer:next'.format(amount)
            output = {'id': ident, 'short': short[:40], 'net_usd_month': amount,
                      'first_seen': record['first_seen'], 'computed_at': now.isoformat()}
    _write(history_path, history)
    _write(directory / 'next_action.json', output)


def status_action(facade, session_id):
    data = _read(Path(facade.SNAPSHOT_DIR) / 'next_action.json')
    if data.get('id') in IDS and isinstance(data.get('short'), str):
        return {'id': data['id'], 'short': data['short'][:40]}
    return None
