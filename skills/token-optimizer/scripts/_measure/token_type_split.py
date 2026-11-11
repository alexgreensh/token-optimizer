"""Conservative token classes available in counted-ledger evidence."""
import json


def from_rows(rows, pricing_version):
    values = {'cache_reread': 0, 'cache_write': 0}
    present = set()
    for row in rows:
        try:
            raw = row.get('pricing_detail') or '{}'
            detail = json.loads(raw) if isinstance(raw, str) else raw
            if (not isinstance(detail, dict) or detail.get('version') != pricing_version
                    or detail.get('refresh_incomplete')):
                continue
            tokens = int(row.get('tokens') or 0)
            turns = int(row.get('turns_counted') or 0)
            rereads = int(row.get('reread_tokens') or 0)
            misses = detail.get('reread_miss_turns')
            if (type(misses) is int and 0 <= misses <= turns and turns > 0
                    and rereads == tokens * turns):
                values['cache_reread'] += rereads - tokens * misses
                values['cache_write'] += tokens * misses
                present.update(('cache_reread', 'cache_write'))
            if detail.get('oneshot_class_events') == 1 and detail.get('priced_oneshot_events') == 1:
                values['cache_write'] += tokens
                present.add('cache_write')
        except (ValueError, TypeError, OverflowError):
            continue
    return {key: values[key] for key in values if key in present}
