"""Load only the data used by the cache TTL period summary."""


def base_trends(facade, days):
    result = facade._collect_trends_from_db(days)
    return facade._collect_trends_from_jsonl(days) if result is None else result
