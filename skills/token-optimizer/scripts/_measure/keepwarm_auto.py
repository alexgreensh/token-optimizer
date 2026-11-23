"""Shared policy for the in-session and headless automatic warm-ups."""
import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import tempfile

from . import keepwarm_economics

MAX_PER_PAUSE = 2
MAX_PER_SESSION_DAY = 4
MAX_PER_DAY = 8
MARGIN = 3
DAY = 86400
BUCKETS = (3300, 6900, 10500)
CACHE_LIFETIME = 3600
MIN_CONTEXT = 100_000
OFF = ('0', 'false', 'off', 'no')
AUTO_SOURCES = ('claude-code-mod', 'keepwarm-headless')
PROOF_STOP = 'Automatic keep-warm stopped for this session: the warm-up did not read the cache.'
HEADLESS_STOP = 'Headless keep-warm stopped for this installation: a warm-up failed the cache proof.'


def _enabled(facade, env=None):
    env = facade.os.environ if env is None else env
    return (facade._read_config_flag('keepwarm_auto', False) is True
            and not facade._keepwarm_llm_ping_disabled(env))


def _history_path(facade):
    return facade.SNAPSHOT_DIR / 'keepwarm_return_history.json'


def _read_history(facade):
    try:
        return json.loads(_history_path(facade).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def refresh_history(facade, now):
    path = _history_path(facade)
    source = keepwarm_economics.load_history(facade, None, True)['gap_sessions']
    sessions = source.sessions if isinstance(source, keepwarm_economics.GapHistory) else source
    last_month = keepwarm_economics.GapHistory(
        [[t for t in session if now - 30 * DAY <= t <= now] for session in sessions])
    buckets = {}
    for elapsed in BUCKETS:
        at_risk, returned = last_month.counts(now, elapsed, CACHE_LIFETIME)
        buckets[str(elapsed)] = {'at_risk': at_risk, 'returned': returned}
    output = {'built_at': now, 'buckets': buckets}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.keepwarm-history-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(output, stream, allow_nan=False)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return output


def prune_markers(facade, now):
    removed = 0
    if not facade.SNAPSHOT_DIR.is_dir():
        return removed
    for path in facade.SNAPSHOT_DIR.iterdir():
        if not path.name.startswith(('keepwarm-ended-', 'keepwarm-owner-')):
            continue
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < now - 14 * DAY:
                path.unlink()
                removed += 1
                if removed == 200:
                    break
        except OSError:
            continue
    return removed


def on_savings_refresh(facade, summary):
    now = facade.time.time()
    if 0 <= now - _read_history(facade).get('built_at', 0) < DAY:
        return
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    lock = facade.SNAPSHOT_DIR / '.keepwarm-history.lease'
    with facade.lease_lock(lock, acquire_timeout=0, cohort_throttle=False, reclaim_released=True) as held:
        if held and not 0 <= now - _read_history(facade).get('built_at', 0) < DAY:
            refresh_history(facade, now)
            prune_markers(facade, now)


def _caps(pause=0, session=0, day=0):
    return {'pause_left': max(0, MAX_PER_PAUSE - pause),
            'session_day_left': max(0, MAX_PER_SESSION_DAY - session),
            'day_left': max(0, MAX_PER_DAY - day)}


def _auto_rows(rows):
    return [row for row in rows if row.get('source') in AUTO_SOURCES
            and row.get('action') == 'warm' and row.get('auto') is True]


def _attempt_id(row):
    return row.get('action_id') or row.get('attempt_key') or (row.get('session_id'), row.get('ts'))


def _cap_counts(rows, session_id, pause_start, now):
    cancelled = {_attempt_id(row) for row in rows if row.get('status') == 'cancelled'}
    attempts = {}
    for row in _auto_rows(rows):
        ident = _attempt_id(row)
        if ident in cancelled or row.get('status') not in ('firing', 'ok', 'error', 'timeout'):
            continue
        ts = float(row['ts'])
        if ident not in attempts or ts < attempts[ident][1]:
            attempts[ident] = (row.get('session_id'), ts)
    return _caps(
        sum(sid == session_id and pause_start <= ts <= now for sid, ts in attempts.values()),
        sum(sid == session_id and now - DAY <= ts <= now for sid, ts in attempts.values()),
        sum(now - DAY <= ts <= now for _, ts in attempts.values()),
    )


def _failed_attempts(rows, now, days, session_id=None):
    return {_attempt_id(row) for row in _auto_rows(rows) if row.get('proof') is False
            and now - days * DAY <= float(row['ts']) <= now
            and (session_id is None or row.get('session_id') == session_id)}


def _trip_reason(facade, now, summary=None):
    if facade.keepwarm_tripwire_mode() == 'off':
        return 'tripwire'
    if summary is None:
        summary = keepwarm_economics.verified_summary(facade, 7, now)
    if summary['pings'] >= 5 and summary['net_usd'] < 0:
        return 'tripwire'
    return None


def avoided_reload(facade, tokens, model):
    read = facade._get_model_cost(model, 0, 0, tokens, 0, per_request=True)
    write = facade._get_model_cost(model, 0, 0, 0, tokens, cache_create_1h=tokens,
                                   cache_create_5m=0, per_request=True)
    return write - read


def _priced_value(facade, tokens, model):
    ping = facade._keepwarm_ping_cost_est(tokens, model)
    # Use the higher quote when a per-request long-context rate applies.
    full_read = facade._get_model_cost(model, 1000, 0, tokens, 0, per_request=True)
    return max(ping, full_read), avoided_reload(facade, tokens, model)


def _probability(at_risk, returned):
    if type(at_risk) is not int or type(returned) is not int or at_risk < 20 or not 0 <= returned <= at_risk:
        return None
    return returned / (at_risk + 10)


def _bucket(elapsed):
    return min(BUCKETS, key=lambda bucket: abs(bucket - elapsed))


def decide(facade, session_id, context_tokens, idle_seconds, model, lifetime,
           five_hour_pct=None, week_pct=None, now=None, *, headless=False, env=None, rows=None):
    """Read only local policy snapshots and the spend ledger. Fail closed."""
    out = {'action': 'stop', 'reason': 'error', 'p_return': 0.0, 'ping_cost_usd': 0.0,
           'avoided_usd': 0.0, 'caps': _caps()}

    def stop(reason):
        out['reason'] = reason
        return out

    try:
        now = facade.time.time() if now is None else now
        if headless:
            from . import keepwarm_safety
            reason = keepwarm_safety.headless_disabled(facade, env)
            if reason:
                return stop(reason)
        elif not _enabled(facade, env):
            return stop('off')
        trip = _trip_reason(facade, now)
        if trip:
            return stop(trip)
        if lifetime != CACHE_LIFETIME:
            return stop('lifetime')
        if context_tokens < MIN_CONTEXT:
            return stop('small')
        if not facade._is_priced_model(model):
            return stop('unpriced')
        for pct in (five_hour_pct, week_pct):
            if pct is not None and (type(pct) not in (int, float) or not math.isfinite(pct) or pct < 0):
                return stop('meters-unknown')
        if ((five_hour_pct is not None and five_hour_pct >= 80)
                or (week_pct is not None and week_pct >= 90)):
            return stop('limits')
        if (five_hour_pct is None or week_pct is None) and facade.keepwarm_billing_mode(env=env) != 'api':
            return stop('meters-unknown')
        if not math.isfinite(idle_seconds) or idle_seconds < 0:
            return out
        rows = facade._keepwarm_load_ledger_rows() if rows is None else rows
        out['caps'] = _cap_counts(rows, session_id, now - idle_seconds, now)
        for key, reason in zip(out['caps'], ('cap-pause', 'cap-session', 'cap-day')):
            if not out['caps'][key]:
                return stop(reason)
        if _failed_attempts(rows, now, 1, session_id):
            return stop('failed-proof')
        history = _read_history(facade)
        if not 0 <= now - history.get('built_at', 0) <= 7 * DAY:
            return stop('no-history')
        counts = history.get('buckets', {}).get(str(_bucket(idle_seconds)), {})
        probability = _probability(counts.get('at_risk'), counts.get('returned'))
        if probability is None:
            return stop('no-history')
        ping, avoided = _priced_value(facade, context_tokens, model)
        if not all(math.isfinite(cost) and cost > 0 for cost in (ping, avoided)):
            return out
        out.update(p_return=probability, ping_cost_usd=ping, avoided_usd=avoided)
        if probability * avoided < MARGIN * ping:
            return stop('not-worth-it')
        out['action'] = 'ping'
        return stop('due')
    except Exception:
        return stop('error')


def proof_ok(usage, context_tokens):
    if not isinstance(usage, dict) or type(context_tokens) is not int or context_tokens <= 0:
        return False
    read = usage.get('cache_read_input_tokens')
    write = usage.get('cache_creation_input_tokens')
    return (type(read) is int and type(write) is int and write >= 0
            and 100 * read >= 90 * context_tokens and 100 * write <= 5 * context_tokens)


def _mod_active(facade, now):
    cutoff = now - 30 * DAY
    if any(row.get('source') == 'claude-code-mod' and float(row['ts']) >= cutoff
           for row in facade._keepwarm_load_ledger_rows()):
        return True
    conn = facade._open_trends_db_readonly(timeout=0.2)
    try:
        date = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        return conn.execute('SELECT 1 FROM mod_action_observations WHERE timestamp >= ? LIMIT 1',
                            (date,)).fetchone() is not None
    finally:
        conn.close()


def capped_forecast(facade, days, now, files=None, parse_fn=None):
    """Replay the same spacing, economics and rolling caps across all pauses."""
    cutoff = now - days * DAY
    if files is None:
        files = [path for path, _, _ in facade._find_all_jsonl_files(days)]
    parse = parse_fn or facade.parse_session_turns
    pauses, sessions = [], []
    for path in files:
        entries = [(facade._keepwarm_iso_to_epoch(turn.get('timestamp')), turn) for turn in parse(path)]
        ordered = sorted(((ts, turn) for ts, turn in entries if ts is not None and cutoff <= ts <= now),
                         key=lambda pair: pair[0])
        sessions.append([ts for ts, _ in ordered])
        ttl = None
        for index, (start, turn) in enumerate(ordered):
            if turn.get('cache_creation_1h') or turn.get('cache_creation_5m'):
                ttl = '1h' if turn.get('cache_creation_1h', 0) > turn.get('cache_creation_5m', 0) else '5m'
            end = ordered[index + 1][0] if index + 1 < len(ordered) else None
            model = turn.get('model')
            tokens = facade._turn_prefix_cached(turn)
            if ttl != '1h' or tokens < MIN_CONTEXT or not facade._is_priced_model(model):
                continue
            for number, elapsed in enumerate(BUCKETS[:MAX_PER_PAUSE]):
                if start + elapsed <= now and (end is None or start + elapsed < end):
                    pauses.append((start + elapsed, str(path), start, number, model, tokens, end))
    history = keepwarm_economics.GapHistory(sessions)
    global_pings, session_pings = deque(), defaultdict(deque)
    credited, first = set(), set()
    spend = gross = 0.0
    pings = 0
    for at, sid, start, number, model, tokens, end in sorted(pauses):
        pause = (sid, start)
        if number and pause not in first:
            continue
        for window in (global_pings, session_pings[sid]):
            while window and window[0] < at - DAY:
                window.popleft()
        if len(global_pings) >= MAX_PER_DAY or len(session_pings[sid]) >= MAX_PER_SESSION_DAY:
            continue
        # Differences of cumulative counts limit the sample to the preceding window.
        recent = history.counts(at, _bucket(at - start), CACHE_LIFETIME)
        older = history.counts(at - 30 * DAY, _bucket(at - start), CACHE_LIFETIME)
        probability = _probability(recent[0] - older[0], recent[1] - older[1])
        ping, saved = _priced_value(facade, tokens, model)
        if probability is None or not 0 < MARGIN * ping <= probability * saved:
            continue
        global_pings.append(at)
        session_pings[sid].append(at)
        first.add(pause)
        spend += ping
        pings += 1
        if end is not None and start + CACHE_LIFETIME < end <= at + CACHE_LIFETIME and pause not in credited:
            credited.add(pause)
            gross += saved
    return {'available': True, 'positive': gross > spend, 'net_usd': gross - spend,
            'ping_cost_usd': spend, 'avoided_writes': len(credited), 'pings': pings}


def plan_item(facade):
    try:
        if facade._read_config_flag('keepwarm_auto', False) is True or not _mod_active(facade, facade.time.time()):
            return None
        estimate = facade.keepwarm_forecast(days=30, automatic=True)
        if not estimate.get('positive') or estimate['net_usd'] <= 0:
            return None
        return {'id': 'keepwarm_auto:on', 'kind': 'keepwarm_auto',
                'label': 'Keep big sessions warm automatically (at most 2 per pause, 8 per day)',
                'tokens_per_request': None, 'net_usd_month': estimate['net_usd'],
                'own_cost_usd_month': estimate['ping_cost_usd'],
                'basis': f"replay of your last 30 days: {estimate['avoided_writes']} returns within the hour, {estimate['pings']} pings",
                'undo': 'measure.py keepwarm-decide set off', 'default_selected': True}
    except Exception:
        return None


def _set_flag(facade, enabled):
    previous = facade._read_config_flag('keepwarm_auto', False)
    facade._write_config_flag('keepwarm_auto', enabled)
    if facade._read_config_flag('keepwarm_auto', None) is not enabled:
        raise OSError('Keep-warm preference could not be saved')
    return previous


def apply_item(facade, item):
    if item.get('id') != 'keepwarm_auto:on':
        raise ValueError('Invalid keep-warm approval')
    previous = _set_flag(facade, True)
    return {'ok': True, 'before': previous, 'after': True}


def undo_item(facade, result):
    if result.get('ok') is not True or result.get('after') is not True:
        return False
    _set_flag(facade, False)
    return True


def health_block(facade):
    from . import keepwarm_safety
    cfg = {'state': 'off', 'reason': 'off', 'pings_7d': 0, 'proof_failed_7d': 0,
           'net_usd_7d': None, 'caps': _caps()}
    try:
        enabled = _enabled(facade)
        headless = keepwarm_safety.headless_disabled(facade)
        cfg.update(state='on' if enabled else 'off', reason='enabled' if enabled else 'off',
                   headless_state='on' if headless is None else 'stopped' if headless == 'headless-proof' else 'off',
                   headless_reason=headless)
        now = facade.time.time()
        rows = facade._keepwarm_load_ledger_rows()
        summary = keepwarm_economics.verified_summary(facade, 7, now)
        cfg.update(pings_7d=summary['pings'], net_usd_7d=summary['net_usd'],
                   proof_failed_7d=len(_failed_attempts(rows, now, 7)),
                   caps=_cap_counts(rows, None, now, now))
        reason = _trip_reason(facade, now, summary)
        if reason:
            cfg.update(state='stopped' if enabled else 'off', reason=reason)
            if cfg['headless_state'] == 'on':
                cfg.update(headless_state='stopped', headless_reason=reason)
    except Exception:
        cfg.update(reason='accounting-unavailable', state='stopped' if cfg['state'] == 'on' else cfg['state'])
        if cfg.get('headless_state') == 'on':
            cfg.update(headless_state='stopped', headless_reason='accounting-unavailable')
    return cfg


def status_headline(facade, headline, keepwarm):
    """Preserve the status bar's existing display contract and signed estimate."""
    from . import keepwarm_safety

    stopped = keepwarm_safety.headless_disabled(facade) == 'headless-proof'
    has_activity = keepwarm and keepwarm.get('events')
    if not (stopped or has_activity):
        return None
    net = float(keepwarm['cost_saved_usd']) if has_activity else None
    short = 'Keep-warm stopped' if stopped else f"Keep-warm {'-' if net < 0 else '+'}${abs(net):.2f} est."
    note = HEADLESS_STOP if stopped else None
    if has_activity and stopped:
        short += f" {'-' if net < 0 else '+'}${abs(net):.2f} est."
    if headline.get('display'):
        headline['display'] = {**headline['display'],
                               'short': headline['display']['short'] + ' · ' + short,
                               'long': headline['display']['long'] + ' · ' + short}
    else:
        headline['display'] = {'kind': None, 'short': short, 'long': short, 'url': None}
    return note


def _parse_args(args):
    parser = argparse.ArgumentParser(prog='measure.py keepwarm-decide')
    parser.add_argument('--session', required=True, dest='session_id')
    parser.add_argument('--context-tokens', type=int, required=True)
    parser.add_argument('--idle-seconds', type=float, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--lifetime', type=int, required=True)
    parser.add_argument('--five-hour-pct', type=float)
    parser.add_argument('--week-pct', type=float)
    parser.add_argument('--json', action='store_true', dest='as_json')
    return parser.parse_args(args)


def main(facade, args):
    if args in (['set', 'on'], ['set', 'off']):
        _set_flag(facade, args[1] == 'on')
        print('In-session automatic keep-warm is on.' if args[1] == 'on'
              else 'Automatic keep-warm is off in session and headless.')
    elif args == ['reset-headless-proof']:
        from . import keepwarm_safety
        reason = keepwarm_safety.reset_headless_proof(facade)
        if reason:
            print(f'Headless keep-warm proof reset failed ({reason}).')
            return 1
        print('Headless keep-warm proof stop cleared.')
    elif args in (['status'], ['status', '--json']):
        result = health_block(facade)
        if '--json' in args:
            print(json.dumps(result))
        else:
            print(f"Automatic keep-warm is {result['state']} in session and {result.get('headless_state', 'unknown')} headless.")
            if result.get('headless_reason') == 'headless-proof':
                print(HEADLESS_STOP)
                print('Run measure.py keepwarm-decide reset-headless-proof to clear the installation stop.')
            print(f"Last 7 days: {result['pings_7d']} pings, {result['proof_failed_7d']} failed proofs.")
            net = result['net_usd_7d']
            print(f"Estimated net at API rates: ${net:.2f}." if net is not None else 'Estimated net is unavailable.')
    elif len(args) == 3 and args[0] == 'watch':
        from . import keepwarm_safety
        return keepwarm_safety.watch_terminal(facade, args[1], int(args[2]))
    elif args and args[0] == 'headless':
        from . import keepwarm_safety
        return keepwarm_safety.desktop_headless_main(facade, args[1:])
    else:
        values = vars(_parse_args(args))
        as_json = values.pop('as_json')
        result = decide(facade, **values)
        print(json.dumps(result) if as_json else f"{result['action']}: {result['reason']}")
    return 0
