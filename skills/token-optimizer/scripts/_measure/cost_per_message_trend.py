"""An unattributed cost-per-message trend on one constant price basis: Opus
list prices for every model, so model mix changes do not move the index
through price; the renderer labels it "at constant Opus prices"."""
import json
import math
from collections import defaultdict


def index_rows(rows):
    monthly = defaultdict(lambda: [0.0, 0])
    for day, messages, raw in rows:
        try:
            messages = int(messages or 0)
            models = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
            if not day or messages < 0 or not isinstance(models, dict):
                continue
            cost = 0.0
            for parts in models.values():
                if not isinstance(parts, dict):
                    continue
                cost += (float(parts.get('fresh_input') or 0) * 5 + float(parts.get('output') or 0) * 25
                         + float(parts.get('cache_read') or 0) * .5 + float(parts.get('cache_create') or 0) * 6.25)
            if not math.isfinite(cost) or cost < 0:
                continue
            monthly[str(day)[:7]][0] += cost
            monthly[str(day)[:7]][1] += messages
        except (TypeError, ValueError, OverflowError):
            continue
    values = [(month, cost / messages) for month, (cost, messages) in sorted(monthly.items()) if messages and cost > 0]
    if not values:
        return []
    base = values[0][1]
    return [{'month': month, 'index': round(value / base * 100, 1)} for month, value in values]


def query(conn):
    from session_activity import ensure_activity_view
    ensure_activity_view(conn)
    rows = conn.execute("SELECT date, message_count, model_usage_breakdown_json FROM session_activity WHERE COALESCE(is_sidechain,0)=0 ORDER BY date").fetchall()
    return index_rows(rows)
