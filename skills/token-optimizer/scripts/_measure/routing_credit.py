"""Limit realized routing credit to the period with routing guidance."""
from datetime import date, datetime, timedelta
from . import _compat


def routing_enabled_since(data_dir):
    try:
        value = (data_dir / 'routing_injected_at').read_text(encoding='utf-8').strip()
        return date.fromisoformat(value).isoformat()
    except (OSError, ValueError, TypeError):
        return None


def compute(days=30, since=None):
    f = _compat.facade()
    enabled = routing_enabled_since(f.SNAPSHOT_DIR)
    cutoff = str(since)[:10] if since else (datetime.now() - timedelta(days=days or 30)).strftime('%Y-%m-%d')
    if enabled:
        cutoff = max(cutoff, enabled)
    result = f._routing_unrestricted(days=days, since=cutoff)
    result['routing_enabled'] = bool(enabled)
    if not enabled:
        result['realized_cost_usd'] = 0.0
        result['measured_baseline_realized_cost_usd'] = None
    return result
