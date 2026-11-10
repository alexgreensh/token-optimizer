"""Find the four consecutive months that drove an attested routing estimate."""
import json
from collections import defaultdict


def strongest(conn, facade, expected, start_date=None):
    if expected <= 0:
        return None
    families = ('opus', 'sonnet', 'sonnet_legacy', 'haiku')
    rates = {fam: facade._routing_class_rates(facade._routing_reference_model(fam)) for fam in families}
    if not all(rates.values()):
        return None
    months = defaultdict(float)
    for (raw,) in conn.execute('SELECT daily_usage_json FROM session_log'):
        try:
            daily = json.loads(raw or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(daily, dict):
            continue
        for day, models in daily.items():
            if not isinstance(day, str) or len(day) != 10 or (start_date and day < start_date) or not isinstance(models, dict):
                continue
            for model, parts in models.items():
                family = facade._normalize_model_name(model)
                if family not in families or not isinstance(parts, dict) or facade._routing_class_rates(model) is None:
                    continue
                tokens = {}
                facade._add_routing_buckets(tokens, parts)
                months[day[:7]] += sum(tokens.get(k, 0) * (.95 * rates['opus'][k] + .05 * rates['sonnet'][k] - rates[family][k]) for k in facade._ROUTING_CLASSES) / 1e6
    total = sum(months.values())
    if total <= 0 or abs(total - expected) > max(0.02 * expected, 1.0):
        return None
    keys = sorted(months)
    choices = []
    for i in range(len(keys) - 3):
        span = keys[i:i+4]
        serial = [int(x[:4]) * 12 + int(x[5:]) for x in span]
        if all(serial[n] == serial[0] + n for n in range(4)):
            choices.append((sum(months[x] for x in span), span[0], span[-1]))
    if not choices:
        return None
    amount, start, end = max(choices)
    return {'start': start, 'end': end} if amount > total * .5 else None
