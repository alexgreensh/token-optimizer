import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import routing_period


class Facade:
    _ROUTING_CLASSES = ('fresh_input',)
    def _routing_reference_model(self, family): return family
    def _routing_class_rates(self, model): return {'fresh_input': 2.0 if model == 'opus' else 1.0}
    def _normalize_model_name(self, model): return model
    def _add_routing_buckets(self, dest, parts): dest['fresh_input'] = parts.get('fresh_input', 0)


def test_routing_period_requires_reconciliation_and_finds_majority():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE session_log(daily_usage_json TEXT)')
    daily = {'2026-%02d-01' % i: {'sonnet': {'fresh_input': n * 1000000}} for i, n in enumerate((.1, 2, 3, 2, 2), 1)}
    conn.execute('INSERT INTO session_log VALUES(?)', (json.dumps(daily),))
    assert routing_period.strongest(conn, Facade(), 9.1 * .95) == {'start': '2026-02', 'end': '2026-05'}
    assert routing_period.strongest(conn, Facade(), 1000) is None
