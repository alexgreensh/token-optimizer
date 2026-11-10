from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import cost_per_message_trend as trend


def test_monthly_index_uses_constant_prices_and_known_message_counts():
    rows = [('2026-01-05', 10, {'opus': {'fresh_input': 1000000}}),
            ('2026-02-05', 10, {'other': {'fresh_input': 2000000}}),
            ('2026-02-06', 0, {'other': {'fresh_input': 1000000}})]
    out = trend.index_rows(rows)
    assert [(x['month'], x['index']) for x in out] == [('2026-01', 100.0), ('2026-02', 300.0)]
    assert 'usd' not in repr(out)
