"""Load only the data used by the cache TTL period summary."""


def base_trends(facade, days):
    result = facade._collect_trends_from_db(days)
    return facade._collect_trends_from_jsonl(days) if result is None else result


def cache_health(f, days, fresh):
    """Cache each window and runtime without discarding legacy sidecars."""
    path = f.SNAPSHOT_DIR / 'cache_health.json'
    runtime = f.detect_runtime()
    key = f'{days}:{runtime}'
    payload = {}
    try:
        mtime = path.stat().st_mtime
        memo = f._cache_health_memo
        if memo is not None and memo[0] == mtime:
            payload = memo[1]
        else:
            payload = f.json.loads(path.read_text(encoding='utf-8'))
            f._cache_health_memo = (mtime, payload)
        if not isinstance(payload, dict):
            payload = {}
    except (f.json.JSONDecodeError, OSError, ValueError, TypeError, IndexError):
        payload = {}

    stored = payload.get('entries')
    entries = dict(stored) if isinstance(stored, dict) else {}
    if 'period_days' in payload and 'platform' in payload:
        legacy_key = f"{payload['period_days']}:{payload['platform']}"
        entries.setdefault(legacy_key, {k: v for k, v in payload.items() if k != 'entries'})
    candidate = entries.get(key)
    if not fresh and isinstance(candidate, dict):
        try:
            if (int(candidate.get('period_days', -1)) == int(days)
                    and candidate.get('platform') == runtime and candidate.get('engine') == 2
                    and f.time.time() - float(candidate.get('_cached_ts', 0)) < f._CACHE_HEALTH_CACHE_TTL_SECONDS):
                return candidate
        except (TypeError, ValueError):
            pass
    result = f._get_cache_ttl_waste(days=days)
    try:
        record = dict(result)
        record['_cached_ts'] = f.time.time()
        entries[key] = record
        sidecar = {**record, 'entries': entries}
        f.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = f.tempfile.mkstemp(prefix='.cache_health.', suffix='.tmp', dir=str(f.SNAPSHOT_DIR))
        try:
            if hasattr(f.os, 'fchmod'):
                f.os.fchmod(fd, 0o600)
            with f.os.fdopen(fd, 'w', encoding='utf-8') as stream:
                f.json.dump(sidecar, stream)
            f.os.replace(tmp_name, str(path))
            f._cache_health_memo = (f.os.stat(path).st_mtime, sidecar)
        except Exception:
            try:
                f.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass
    return result
