"""Share window savings and keep session-specific figures separate."""
import time


def atomic_write(facade, path, data):
    tmp = path.with_name('.' + path.name + '.' + facade.os.urandom(16).hex() + '.tmp')
    try:
        tmp.write_text(facade.json.dumps(data), encoding='utf-8')
        for attempt in range(3):
            try:
                facade.os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def retry_delay(failures):
    return min(600, 60 * 2 ** min(max(0, int(failures) - 1), 6))


def write_cache(facade, session_id, savings, reason):
    # One failure is routine (a hook writing): the retry note needs two in a row.
    directory = facade._status_bar_dir()
    directory.mkdir(parents=True, exist_ok=True)
    prior = facade._status_bar_read_cache(session_id)
    failed = savings is None
    stale = bool(failed and prior and prior.get('savings') is not None)
    failures = min(10, int((prior or {}).get('failures', 0)) + 1) if failed else 0
    if stale:
        savings = prior['savings']
    friendly = 'no savings recorded yet' if reason == 'no savings recorded yet' else 'still counting savings'
    data = {
        'schema': facade._STATUS_BAR_SCHEMA, 'computed_at': prior['computed_at'] if stale else facade.time.time(),
        'savings': savings, 'reason': friendly if savings is None else None, 'stale': stale,
        'last_attempt': facade.time.time() if failed else 0, 'failures': failures, 'refresh_failed': failed and failures >= 2 and reason != 'no savings recorded yet',
    }
    try:
        atomic_write(facade, facade._status_bar_cache_path(session_id), data)
    except PermissionError:
        atomic_write(facade, directory / ('global-pending-' + facade.os.urandom(16).hex() + '.json'), data)
    else:
        try:
            drop_older_pending(facade, directory, data['computed_at'])
        except OSError:
            pass
    cutoff = facade.time.time() - facade._STATUS_BAR_CACHE_RETENTION_S
    try:
        for path in directory.iterdir():
            try:
                orphan = path.name.endswith(('.tmp', '.stale', '.release')) and path.stat().st_mtime < facade.time.time() - 86400
                if orphan or (path.name.endswith(('.json', '.session')) and path.stat().st_mtime < cutoff):
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def drop_older_pending(facade, directory, computed_at):
    """Once global.json holds a result, pending files no newer than it are dead
    weight that every poll would still parse; remove them."""
    for path in directory.glob('global-pending-*.json'):
        try:
            data = facade.json.loads(path.read_text(encoding='utf-8'))
            stamp = data.get('computed_at') if isinstance(data, dict) else None
            if not isinstance(stamp, (int, float)) or stamp <= computed_at:
                path.unlink()
        except (OSError, ValueError):
            try:
                path.unlink()
            except OSError:
                pass


def read_cache(facade, session_id):
    path = facade._status_bar_cache_path(session_id)
    valid = []
    try:
        paths = [path, *path.parent.glob('global-pending-*.json')]
        for candidate in paths:
            try:
                data = facade.json.loads(candidate.read_text(encoding='utf-8'))
                if isinstance(data, dict) and isinstance(data.get('computed_at'), (int, float)):
                    valid.append(data)
            except (OSError, ValueError):
                pass
        if valid:
            return max(valid, key=lambda item: (item['computed_at'], item.get('last_attempt', 0) if isinstance(item.get('last_attempt', 0), (int, float)) else 0))
    except OSError:
        pass
    return seed_legacy(facade, session_id)


def seed_legacy(facade, session_id):
    directory = facade._status_bar_dir()
    try:
        candidates = []
        for path in directory.glob('*.json'):
            if path.name == 'global.json' or path.name.startswith('compactions-'):
                continue
            try:
                data = facade.json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict) and isinstance(data.get('savings'), dict) and isinstance(data.get('computed_at'), (int, float)):
                    candidates.append(data)
            except (OSError, ValueError):
                continue
        if not candidates:
            return None
        data = max(candidates, key=lambda item: item['computed_at'])
        data.update(stale=True, last_attempt=0, failures=0, refresh_failed=False)
        try:
            atomic_write(facade, facade._status_bar_cache_path(session_id), data)
        except OSError:
            pass
        return data
    except OSError:
        return None


_SESSION_REUSE_S = 300
_SESSION_RETRY_S = 30


def _session_path(facade, session_id):
    return facade._status_bar_dir() / ('session-' + facade._status_bar_key(session_id) + '.session')


def _save_session(facade, session_id, values):
    try:
        path = _session_path(facade, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(facade, path, values)
    except OSError:
        pass


def for_session(facade, session_id, savings):
    if savings is None:
        return None
    path = _session_path(facade, session_id)
    try:
        if facade.time.time() - path.stat().st_mtime < _SESSION_REUSE_S:
            values = facade.json.loads(path.read_text(encoding='utf-8'))
            return {**savings, 'session_tokens': values['session_tokens'], 'session_usd': values['session_usd']}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        conn = facade._open_trends_db_readonly()
        try:
            bucket = facade._realized_savings_buckets(conn, '', session_uuid=session_id).get('', {})
        finally:
            conn.close()
        values = {'session_tokens': int(bucket.get('tokens', 0)), 'session_usd': float(bucket.get('usd', 0.0))}
        _save_session(facade, session_id, values)
    except Exception:
        try:
            values = facade.json.loads(path.read_text(encoding='utf-8'))
            values = {'session_tokens': values['session_tokens'], 'session_usd': values['session_usd']}
        except (OSError, ValueError, TypeError, KeyError):
            values = {'session_tokens': None, 'session_usd': None}
        # Keep what we have and wait _SESSION_RETRY_S before querying again, so
        # a busy database does not cost every poll its full timeout.
        _save_session(facade, session_id, values)
        try:
            stamp = facade.time.time() - _SESSION_REUSE_S + _SESSION_RETRY_S
            facade.os.utime(path, (stamp, stamp))
        except OSError:
            pass
    return {**savings, **values}


def failed_payload(facade, session_id, out):
    savings = out.get('savings')
    if savings is None:
        try:
            cached = facade._status_bar_read_cache(session_id)
            savings = for_session(facade, session_id, cached.get('savings')) if cached else None
        except Exception:
            pass
    out.update(savings=savings, savings_state='stale' if savings is not None else 'loading',
               savings_reason=None if savings is not None else 'still counting savings',
               savings_refresh_failed=True)
    return out


def sync(facade, session_id, out):
    try:
        from . import config_cache
        with config_cache.pricing_snapshot(facade), config_cache.skip_cache_scan():
            savings, reason = facade._status_bar_savings_or_reason(session_id)
    except Exception as exc:
        savings, reason = None, str(exc)
    if savings is None:
        facade._log_spawn_failure("status-bar savings refresh failed: " + str(reason))
    try:
        facade._status_bar_write_cache(session_id, savings, reason)
    except OSError:
        pass
    cached = None
    if savings is None:
        cached = facade._status_bar_read_cache(session_id)
        if cached and cached.get("savings") is not None:
            out.update(savings=for_session(facade, session_id, cached["savings"]), savings_state="stale", savings_reason=None,
                       savings_age_s=round(max(0.0, facade.time.time() - cached["computed_at"]), 1), savings_refresh_failed=bool(cached.get('refresh_failed')))
            return out
    if savings is not None:
        _save_session(facade, session_id, {k: savings[k] for k in ('session_tokens', 'session_usd')})
    out.update(savings=savings, savings_state="fresh" if savings is not None else ("unavailable" if reason == "no savings recorded yet" else "loading"),
               savings_reason=None if savings is not None else ("no savings recorded yet" if reason == "no savings recorded yet" else "still counting savings"),
               savings_age_s=0.0, savings_refresh_failed=bool(savings is None and cached and cached.get('refresh_failed')))
    return out
