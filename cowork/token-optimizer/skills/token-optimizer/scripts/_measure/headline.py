"""One token headline shared by dashboard, CLI and runway."""


def _number(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def token_headline(summary):
    s = summary or {}
    pools = ('mcp_cap_estimated', 'hint_followed', 'verbosity_steer',
             'resume_lean_estimated', 'checkpoint_restore_estimated')
    removed = max(0, _number(s.get('total_tokens')))
    for name in pools:
        pool = s.get(name) or s.get(name + '_estimated') or {}
        if isinstance(pool, dict):
            removed += max(0, _number(pool.get('tokens_saved')))
    counted = s.get('counted_period') or {}
    available = bool(counted.get('available'))
    reread = _number(counted.get('reread_tokens')) if available else 0
    return {
        'removed_tokens': removed, 'reread_tokens': reread,
        'total_tokens': max(0, removed + reread), 'reread_available': available,
        'removed_priced_tokens': _number(counted.get('removed_tokens')) if available else 0,
        'removed_usd': counted.get('oneshot_usd') if available else None,
        'reread_usd': counted.get('reread_usd') if available else None,
        'oneshot_prices_available': counted.get('oneshot_prices_available', True),
        'reread_prices_available': counted.get('reread_prices_available', True),
        'refresh_incomplete_events': _number(counted.get('refresh_incomplete_events')),
        'unmeasured_events': _number(counted.get('unmeasured_events')),
        'pricing_note': counted.get('pricing_note', ''),
        'oneshot_floor_note': counted.get('oneshot_floor_note', ''),
    }


def input_traffic(conn, start, end):
    """Dated parent input and output, including cache, without sidechains."""
    import json
    columns = {row[1] for row in conn.execute('PRAGMA table_info(session_log)')}
    parent = 'parent_daily_usage_json' if 'parent_daily_usage_json' in columns else 'NULL'
    daily = 'daily_usage_json' if 'daily_usage_json' in columns else 'NULL'
    side = 'COALESCE(is_sidechain,0)' if 'is_sidechain' in columns else '0'
    total = missing = combined = 0
    for own, merged in conn.execute('SELECT {}, {} FROM session_log WHERE {}=0'.format(parent, daily, side)):
        try:
            usage = json.loads(own if own is not None else merged or 'null')
        except (TypeError, ValueError):
            usage = None
        if not isinstance(usage, dict):
            missing += 1
            continue
        combined += int(own is None and bool(usage))
        for day, models in usage.items():
            if not isinstance(day, str) or not (start <= day <= end) or not isinstance(models, dict):
                continue
            for bucket in models.values():
                if isinstance(bucket, dict):
                    total += sum(max(0, _number(bucket.get(k))) for k in
                                 ('fresh_input', 'cache_create', 'cache_read', 'output'))
    return {'tokens': total, 'complete': missing == 0 and combined == 0,
            'basis': 'dated input and output, including cache reads and writes',
            'missing_rows': missing, 'combined_rows': combined}


def cli_headline(h, days):
    cached = ('~{:,} cached'.format(h['reread_tokens']) if h['reread_available']
              else 'cached unavailable')
    text = '  ~{:,} tokens kept out of context ({} days) · ~{:,} fresh · {}'.format(
        h['total_tokens'], days, h['removed_tokens'], cached)
    if h['refresh_incomplete_events'] or h['unmeasured_events']:
        text += '\n  Estimated; some transcript coverage or refreshes are incomplete.'
    if h['pricing_note']:
        text += '\n  ' + h['pricing_note']
    return text
