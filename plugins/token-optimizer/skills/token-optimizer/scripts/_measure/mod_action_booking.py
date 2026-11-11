"""Durable, idempotent receipts for actions taken in the Claude Code mod."""
import json
import math
import os
from pathlib import Path
import re
import stat

_ID = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
_USAGE = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def _append(path, row):
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n')


def _identity(event):
    sid, ident = event.get('session_id'), event.get('action_id')
    if not isinstance(sid, str) or not _ID.fullmatch(sid) or not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise ValueError('invalid identity')
    when = float(event['timestamp'])
    if not math.isfinite(when) or when <= 0:
        raise ValueError('invalid timestamp')
    return sid, ident, when


def _snapshot(facade, event, sid):
    path = Path(event['transcript_path'])
    size, mtime = event['byte_size'], float(event['transcript_mtime'])
    if (path.name != sid + '.jsonl' or path.is_symlink() or type(size) is not int or size <= 0 or
            not math.isfinite(mtime)):
        raise ValueError('invalid snapshot')
    current = path.stat()
    if not stat.S_ISREG(current.st_mode) or current.st_size != size or abs(current.st_mtime - mtime) > 0.001:
        raise ValueError('transcript changed')
    usage, model, last = facade._keepwarm_last_usage(path)
    if not usage or not model or not facade._is_priced_model(model):
        raise ValueError('model unpriced')
    return {'path': str(path), 'byte_size': size, 'mtime': mtime, 'device': current.st_dev,
            'inode': current.st_ino, 'model': model, 'ttl': facade._keepwarm_ttl_kind(usage),
            'prefix': facade._keepwarm_prefix_proxy(usage), 'last_turn': facade._keepwarm_iso_to_epoch(last)}


def _lock(facade):
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + '.lease')
    return facade.lease_lock(path, acquire_timeout=2.0, cohort_throttle=False, reclaim_released=True)


def _compatible(rows, sid):
    return all(row.get('session_id') == sid and row.get('action') == 'warm' for row in rows)


def _prepare(facade, event, sid, ident, when):
    snapshot = _snapshot(facade, event, sid)
    estimate = facade._keepwarm_ping_cost_est(snapshot['prefix'], snapshot['model'])
    if not math.isfinite(estimate) or estimate <= 0:
        raise ValueError('estimate unavailable')
    db = facade._init_trends_db()
    db.close()
    with _lock(facade) as acquired:
        if not acquired:
            return {'ok': False, 'reason': 'ledger-locked'}
        existing = [r for r in facade._keepwarm_load_ledger_rows() if r.get('action_id') == ident]
        if existing:
            return {'ok': False, 'reason': 'attempt-already-reserved' if _compatible(existing, sid) else 'identity-conflict'}
        _append(facade._keepwarm_ledger_path(), {'status': 'firing', 'source': 'claude-code-mod',
            'action': 'warm', 'action_id': ident, 'attempt_key': ident, 'session_id': sid,
            'ts': when, 'est_cost_usd': estimate, 'snapshot': snapshot})
    return {'ok': True, 'duplicate': False}


def _cancel(facade, sid, ident, when):
    with _lock(facade) as acquired:
        if not acquired:
            return {'ok': False, 'reason': 'ledger-locked'}
        rows = [r for r in facade._keepwarm_load_ledger_rows() if r.get('action_id') == ident]
        if not rows or not _compatible(rows, sid) or any(r.get('status') == 'ok' for r in rows):
            return {'ok': False, 'reason': 'reservation-unavailable'}
        if not any(r.get('status') == 'cancelled' for r in rows):
            _append(facade._keepwarm_ledger_path(), {'status': 'cancelled', 'source': 'claude-code-mod',
                'action': 'warm', 'action_id': ident, 'session_id': sid, 'ts': when})
    return {'ok': True}


def _snapshot_still_usable(snapshot):
    try:
        path = Path(snapshot['path'])
        current = path.stat()
        return (not path.is_symlink() and stat.S_ISREG(current.st_mode) and
                (current.st_dev, current.st_ino) == (snapshot['device'], snapshot['inode']) and
                current.st_size >= snapshot['byte_size'] and
                (current.st_size > snapshot['byte_size'] or abs(current.st_mtime - snapshot['mtime']) <= 0.001))
    except (KeyError, TypeError, OSError):
        return False


def record(facade, event):
    """Reserve then book usage once; credit only a subsequently proven warm return."""
    try:
        if not isinstance(event, dict) or event.get('action') not in ('warm', 'warm-prepare', 'warm-cancel'):
            return {'ok': False, 'reason': 'unsupported-action'}
        sid, ident, when = _identity(event)
        if event['action'] == 'warm-prepare':
            return _prepare(facade, event, sid, ident, when)
        if event['action'] == 'warm-cancel':
            return _cancel(facade, sid, ident, when)
        usage = event['usage']
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in _USAGE):
            return {'ok': False, 'reason': 'invalid-usage'}
        # Old callers can submit an already answered attempt; new mod calls reserve first.
        facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        with _lock(facade) as acquired:
            if not acquired:
                return {'ok': False, 'reason': 'ledger-locked'}
            existing = [r for r in facade._keepwarm_load_ledger_rows() if r.get('action_id') == ident]
            if existing and not _compatible(existing, sid):
                return {'ok': False, 'reason': 'identity-conflict'}
            if any(r.get('status') == 'cancelled' for r in existing):
                return {'ok': False, 'reason': 'reservation-cancelled'}
            reservation = next((r for r in existing if r.get('status') == 'firing'), None)
            snapshot = reservation.get('snapshot') if reservation else _snapshot(facade, event, sid)
            if not isinstance(snapshot, dict):
                return {'ok': False, 'reason': 'snapshot-unavailable'}
            model, ttl = snapshot['model'], snapshot['ttl']
            creation = usage['cache_creation_input_tokens']
            cost = facade._get_model_cost(model, usage['input_tokens'], usage['output_tokens'],
                 usage['cache_read_input_tokens'], creation, cache_create_1h=creation if ttl == '1h' else 0,
                 cache_create_5m=0 if ttl == '1h' else creation, per_request=True)
            if not math.isfinite(cost) or cost <= 0:
                return {'ok': False, 'reason': 'cost-unavailable'}
            db = facade._init_trends_db()
            db.close()
            pause_key = facade._keepwarm_pause_key(sid, when)
            arms = facade.load_keepwarm_records()
            same_pause = next((r for r in arms if r.get('session_id') == sid and
                               r.get('byte_size') == snapshot['byte_size'] and
                               r.get('transcript_mtime') == snapshot['mtime'] and r.get('pings_fired')), None)
            if same_pause is not None:
                pause_key = facade._keepwarm_pause_key(sid, same_pause['ts'])
            row = {'status': 'ok', 'source': 'claude-code-mod', 'action': 'warm', 'action_id': ident,
                   'attempt_key': ident, 'session_id': sid, 'pause_key': pause_key, 'ts': when,
                   'model': model, 'cost_usd': cost, 'usage': dict(usage)}
            finished = [r for r in existing if r.get('status') == 'ok']
            if not finished:
                _append(facade._keepwarm_ledger_path(), row)
            usable = _snapshot_still_usable(snapshot)
            if event.get('answered', True) is True and usable and same_pause is None and not any(r.get('manual_action_id') == ident for r in arms):
                _append(facade._keepwarm_sidecar_path(), {'session_id': sid, 'transcript_path': snapshot['path'],
                    'transcript_mtime': snapshot['mtime'], 'byte_size': snapshot['byte_size'], 'model': model,
                    'prefix_proxy': snapshot['prefix'], 'ttl_kind': ttl, 'last_turn_ts': snapshot['last_turn'] or when,
                    'ts': when, 'state': 'armed', 'owner': None, 'pings_fired': [when],
                    'pause_key': pause_key, 'manual_action_id': ident})
        return {'ok': True, 'pause_key': pause_key, 'cost_usd': cost, 'duplicate': bool(finished), 'resume_trackable': usable}
    except Exception:
        return {'ok': False, 'reason': 'receipt-unavailable'}


def main(facade):
    """Read a single bounded action receipt from standard input."""
    try:
        raw = facade.sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError('receipt too large')
        result = record(facade, json.loads(raw))
    except (ValueError, TypeError):
        result = {'ok': False, 'reason': 'invalid-receipt'}
    print(json.dumps(result, separators=(',', ':')))
    return 0 if result['ok'] else 1
