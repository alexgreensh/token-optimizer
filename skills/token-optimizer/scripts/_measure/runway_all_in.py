"""Calculate capacity from the same savings components as the savings story."""
import json


def extra_work(facade, conn, days, summary, estimated_only=False):
    cutoff = (facade.datetime.now() - facade.timedelta(days=days)).date().isoformat()
    today = facade.datetime.now().date().isoformat()
    columns = {row[1] for row in conn.execute('PRAGMA table_info(session_log)')}
    parent = 'parent_daily_usage_json' if 'parent_daily_usage_json' in columns else 'daily_usage_json'
    side = 'COALESCE(is_sidechain,0)=0' if 'is_sidechain' in columns else '1=1'
    actual = 0.0
    for (raw,) in conn.execute(f'SELECT {parent} FROM session_log WHERE {side}'):
        try:
            daily = json.loads(raw or '{}')
            for day, models in daily.items():
                if not cutoff <= day <= today:
                    continue
                for model, usage in models.items():
                    if facade._is_priced_model(model):
                        actual += facade._get_model_cost(model, int(usage.get('fresh_input') or 0), int(usage.get('output') or 0), int(usage.get('cache_read') or 0), int(usage.get('cache_create') or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    if actual <= 0:
        return None
    counted = facade._counted_cumulative_summary(days=days)
    facade._apply_counted_oneshot_pricing(summary, counted)
    components = facade._action_savings_components(summary, counted)
    # These two pools already contribute tokens to the context multiplier.
    if estimated_only:
        estimates = components.get('estimates_usd') or {}
        saved = sum(float(value or 0) for key, value in estimates.items()
                    if key not in ('hint_followed', 'resume_lean_verified'))
    else:
        saved = components['total_usd'] + components['routing_usd']
    return max(0.0, saved / actual * 100)


def safe_extra_work(facade, conn, days):
    try:
        return extra_work(facade, conn, days, facade._get_merged_savings(days=days), estimated_only=True)
    except Exception:
        return None
