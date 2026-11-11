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


def record(facade, event):
    """Book a priced manual warm-up and its original transcript snapshot once.

    No savings credit is issued here. Existing resume reconciliation verifies the
    subsequent parent turn before crediting anything.
    """
    try:
        if not isinstance(event, dict) or event.get('action') != 'warm':
            return {'ok': False, 'reason': 'unsupported-action'}
        sid, ident = event.get('session_id'), event.get('action_id')
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or not isinstance(ident, str) or not _ID.fullmatch(ident):
            return {'ok': False, 'reason': 'invalid-identity'}
        when = float(event['timestamp'])
        size = event['byte_size']
        mtime = float(event['transcript_mtime'])
        if not math.isfinite(when) or when <= 0 or not math.isfinite(mtime) or type(size) is not int or size <= 0:
            return {'ok': False, 'reason': 'invalid-snapshot'}
        path = Path(event['transcript_path'])
        if path.name != sid + '.jsonl' or path.is_symlink():
            return {'ok': False, 'reason': 'invalid-transcript'}
        current = path.stat()
        if not stat.S_ISREG(current.st_mode) or current.st_size != size or abs(current.st_mtime - mtime) > 0.001:
            return {'ok': False, 'reason': 'transcript-changed'}
        usage = event['usage']
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in _USAGE):
            return {'ok': False, 'reason': 'invalid-usage'}
        parent_usage, model, last = facade._keepwarm_last_usage(path)
        if not parent_usage or not model or not facade._is_priced_model(model):
            return {'ok': False, 'reason': 'model-unpriced'}
        ttl = facade._keepwarm_ttl_kind(parent_usage)
        creation = usage['cache_creation_input_tokens']
        cost = facade._get_model_cost(model, usage['input_tokens'], usage['output_tokens'],
             usage['cache_read_input_tokens'], creation, cache_create_1h=creation if ttl == '1h' else 0,
             cache_create_5m=0 if ttl == '1h' else creation, per_request=True)
        if not math.isfinite(cost) or cost <= 0:
            return {'ok': False, 'reason': 'cost-unavailable'}
        db = facade._init_trends_db()
        db.close()
        pause_key = facade._keepwarm_pause_key(sid, when)
        row = {'status': 'ok', 'source': 'claude-code-mod', 'action': 'warm', 'action_id': ident,
               'attempt_key': ident, 'session_id': sid, 'pause_key': pause_key, 'ts': when,
               'model': model, 'cost_usd': cost, 'usage': dict(usage)}
        arm = {'session_id': sid, 'transcript_path': str(path), 'transcript_mtime': mtime,
               'byte_size': size, 'model': model, 'prefix_proxy': facade._keepwarm_prefix_proxy(parent_usage),
               'ttl_kind': ttl, 'last_turn_ts': facade._keepwarm_iso_to_epoch(last) or when,
               'ts': when, 'state': 'armed', 'owner': None, 'pings_fired': [when],
               'pause_key': pause_key, 'manual_action_id': ident}
        facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + ".lease")
        with facade.lease_lock(lock_path, acquire_timeout=2.0, cohort_throttle=False, reclaim_released=True) as acquired:
            if not acquired:
                return {'ok': False, 'reason': 'ledger-locked'}
            existing = [r for r in facade._keepwarm_load_ledger_rows() if r.get('action_id') == ident]
            if existing and any(r.get('session_id') != sid or r.get('action') != 'warm' for r in existing):
                return {'ok': False, 'reason': 'identity-conflict'}
            arms = facade.load_keepwarm_records()
            same_pause = next((r for r in arms if r.get('session_id') == sid and
                               r.get('byte_size') == size and r.get('transcript_mtime') == mtime and
                               r.get('pings_fired')), None)
            if same_pause is not None:
                pause_key = facade._keepwarm_pause_key(sid, same_pause['ts'])
                row['pause_key'] = pause_key
            if not existing:
                _append(facade._keepwarm_ledger_path(), row)
            if same_pause is None and not any(r.get('manual_action_id') == ident for r in arms):
                _append(facade._keepwarm_sidecar_path(), arm)
        return {'ok': True, 'pause_key': pause_key, 'cost_usd': cost, 'duplicate': bool(existing)}
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
