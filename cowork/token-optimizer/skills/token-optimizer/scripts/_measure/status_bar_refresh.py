"""Share window savings and keep session-specific figures separate."""


def _session_path(facade, session_id):
    return facade._status_bar_dir() / ('session-' + facade._status_bar_key(session_id) + '.session')


def _save_session(facade, session_id, values):
    try:
        path = _session_path(facade, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.' + path.name + '.' + facade.os.urandom(16).hex() + '.tmp')
        tmp.write_text(facade.json.dumps(values), encoding='utf-8')
        facade.os.replace(tmp, path)
    except OSError:
        pass


def for_session(facade, session_id, savings):
    if savings is None:
        return None
    path = _session_path(facade, session_id)
    try:
        if facade.time.time() - path.stat().st_mtime < 5:
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
            values = facade.json.loads(_session_path(facade, session_id).read_text(encoding='utf-8'))
            values = {'session_tokens': values['session_tokens'], 'session_usd': values['session_usd']}
        except (OSError, ValueError, TypeError, KeyError):
            values = {'session_tokens': None, 'session_usd': None}
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
               savings_reason=None if savings is not None else 'still counting savings, retrying')
    return out


def sync(facade, session_id, out):
    try:
        from . import config_cache
        with config_cache.pricing_snapshot(facade):
            savings, reason = facade._status_bar_savings_or_reason(session_id)
    except Exception as exc:
        savings, reason = None, str(exc)
    if savings is None:
        facade._log_spawn_failure("status-bar savings refresh failed: " + str(reason))
    try:
        facade._status_bar_write_cache(session_id, savings, reason)
    except OSError:
        pass
    if savings is None:
        cached = facade._status_bar_read_cache(session_id)
        if cached and cached.get("savings") is not None:
            out.update(savings=for_session(facade, session_id, cached["savings"]), savings_state="stale", savings_reason=None,
                       savings_age_s=round(max(0.0, facade.time.time() - cached["computed_at"]), 1))
            return out
    if savings is not None:
        _save_session(facade, session_id, {k: savings[k] for k in ('session_tokens', 'session_usd')})
    out.update(savings=savings, savings_state="fresh" if savings is not None else "loading",
               savings_reason=None if savings is not None else "still counting savings, retrying",
               savings_age_s=0.0)
    return out
