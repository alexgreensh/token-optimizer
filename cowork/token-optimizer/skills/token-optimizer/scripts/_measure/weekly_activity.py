"""Price a weekly activity window while reporting missing transcript coverage."""


def price(facade, conn, start, end, tier):
    paths = conn.execute(
        "SELECT jsonl_path, date, last_activity_day, cost_usd FROM session_log "
        "WHERE input_tokens IS NOT NULL AND COALESCE(is_sidechain,0)=0").fetchall()
    known = {str(row[0]) for row in paths}
    for path, _mtime, _ in facade._find_all_jsonl_files(None):
        if str(path) not in known:
            paths.append((str(path), '', None, None))
            known.add(str(path))
    rows, missing = [], 0
    costs, missing_cost, cost_complete = 0.0, 0.0, True
    request_ledger = {}
    for name, recorded_date, last_day, recorded_cost in paths:
        path = facade.Path(name)
        if not path.exists():
            if ((last_day or recorded_date) and str(last_day or recorded_date) < start.date().isoformat()) or (recorded_date and str(recorded_date) > end.date().isoformat()):
                continue
            missing += 1
            if recorded_cost is None:
                cost_complete = False
            else:
                missing_cost += max(0.0, float(recorded_cost))
            continue
        children = facade._find_subagent_jsonl_files(path)
        parsed = facade._parse_session_jsonl(path, window_start=start, window_end=end)
        if parsed and parsed.get('is_sidechain'):
            continue
        if parsed:
            parsed = facade._parsed_net_of_ledger(parsed, request_ledger)
        if not parsed:
            parsed = {'total_input_tokens': 0, 'total_output_tokens': 0,
                      'total_cache_create_1h': 0, 'total_cache_create_5m': 0,
                      'cache_hit_rate': 0, 'model_usage': {}, 'api_calls': 0,
                      'message_count': 0}
        inp, out = parsed['total_input_tokens'], parsed['total_output_tokens']
        cw1, cw5 = parsed['total_cache_create_1h'], parsed['total_cache_create_5m']
        child_cache_read = 0
        for child in children:
            cp = facade._parse_session_jsonl(child, window_start=start, window_end=end)
            if cp:
                cp = facade._parsed_net_of_ledger(cp, request_ledger)
                inp += cp['total_input_tokens']
                out += cp['total_output_tokens']
                cw1 += cp['total_cache_create_1h']
                cw5 += cp['total_cache_create_5m']
                child_cache_read += cp['total_cache_read']
        if not parsed['api_calls'] and inp + out == 0:
            continue
        if recorded_cost is None:
            cost_complete = False
        else:
            costs += max(0.0, float(recorded_cost))
        usage = facade.json.dumps(parsed['model_usage'])
        hit = parsed['cache_hit_rate'] if parsed['api_calls'] else child_cache_read / inp if inp else 0
        rows.append((inp, out, cw5, cw1, hit, usage, usage,
                     parsed['api_calls'], parsed['message_count']))
    total = len(rows) + missing
    cost_share = missing_cost / (costs + missing_cost) if cost_complete and costs + missing_cost > 0 else None
    # Beyond 5% missing, a partial workload is too incomplete to headline.
    if not total or missing / total > 0.05 or (cost_share is not None and cost_share > 0.05):
        return None
    result = facade._price_parent_rows(rows, tier)
    if result:
        result['coverage'] = {'priced_sessions': len(rows), 'total_sessions': total,
                              'missing_recorded_cost_share': cost_share}
    return result
