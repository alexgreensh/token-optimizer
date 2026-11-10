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


import pytest


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE session_log(daily_usage_json TEXT)')
    daily = {'2026-%02d-01' % i: {'sonnet': {'fresh_input': n * 1000000}} for i, n in enumerate((.1, 2, 3, 2, 2), 1)}
    conn.execute('INSERT INTO session_log VALUES(?)', (json.dumps(daily),))
    return conn


@pytest.mark.parametrize('opus', [0.6, 0.8, 0.95])
def test_routing_period_uses_the_users_own_baseline_mix(opus):
    shares = {'opus': opus, 'sonnet': round(1 - opus, 4)}
    # 9.1M Sonnet tokens; per token the baseline costs opus*2 + (1-opus)*1 vs 1.
    expected = 9.1 * opus
    assert routing_period.strongest(_conn(), Facade(), expected, shares=shares) == {'start': '2026-02', 'end': '2026-05'}
    assert routing_period.strongest(_conn(), Facade(), 1000, shares=shares) is None


def test_routing_period_without_a_baseline_mix_finds_nothing():
    assert routing_period.strongest(_conn(), Facade(), 9.1 * .95) is None
    assert routing_period.strongest(_conn(), Facade(), 9.1 * .6, shares={'opus': 0.95, 'sonnet': 0.05}) is None
