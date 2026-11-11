"""Still on the table: skill listing growth since its smallest month.

Synthetic transcripts and sizes only. listing_growth compares the median
initial skill listing of the last 7 days with the smallest monthly median
(months with enough sessions); skill_listing_budget prices the growth on
every local call through the same per-request pricing as setup trims, and
reports "not available" instead of a number when evidence is thin.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/token-optimizer/scripts'))
from _measure import structural_listing as listing  # noqa: E402
from _measure import structural_report as report  # noqa: E402

NOW = datetime(2026, 11, 10, 12).timestamp()
DAY = 86400


def _sessions(spec):
    """spec: [(start_epoch, chars)] -> sessions plus a fake reader."""
    sizes = {'s%d' % i: c for i, (_, c) in enumerate(spec)}
    return [(t, 's%d' % i) for i, (t, _) in enumerate(spec)], sizes.get


def _month(y, m, chars, n):
    t = datetime(y, m, 10).timestamp()
    return [(t + i * 60, chars) for i in range(n)]


def test_growth_uses_smallest_monthly_median_and_recent_week():
    spec = (_month(2026, 4, 6000, 25) + _month(2026, 6, 8000, 30) + _month(2026, 9, 30000, 40)
            + [(NOW - 2 * DAY + i, 30400) for i in range(5)])
    sessions, read = _sessions(spec)
    g = listing.listing_growth(sessions, NOW, 4.0, read=read)
    assert g['baseline_month'] == '2026-04' and g['baseline_chars'] == 6000
    assert g['current_chars'] == 30400 and g['recent_sessions'] == 5
    assert g['growth_tokens'] == 6100


def test_small_months_cannot_set_the_baseline():
    # Median month is 30 sessions, so a month needs at least 3 (10%, floor 3).
    spec = (_month(2026, 3, 1000, 2) + _month(2026, 6, 8000, 30) + _month(2026, 7, 9000, 30)
            + [(NOW - DAY + i, 30000) for i in range(3)])
    sessions, read = _sessions(spec)
    g = listing.listing_growth(sessions, NOW, 4.0, read=read)
    assert g['baseline_month'] == '2026-06' and g['growth_tokens'] == 5500 and g['month_floor'] == 3


def test_light_user_with_few_sessions_still_gets_a_baseline():
    spec = _month(2026, 5, 5000, 4) + _month(2026, 8, 12000, 6) + [(NOW - DAY + i, 20000) for i in range(3)]
    sessions, read = _sessions(spec)
    g = listing.listing_growth(sessions, NOW, 4.0, read=read)
    assert g['baseline_month'] == '2026-05' and g['growth_tokens'] == 3750


def test_heavy_user_floor_scales_with_volume():
    spec = (_month(2026, 4, 2000, 15) + _month(2026, 5, 6000, 300) + _month(2026, 6, 7000, 300)
            + [(NOW - DAY + i, 30000) for i in range(5)])
    sessions, read = _sessions(spec)
    g = listing.listing_growth(sessions, NOW, 4.0, read=read)
    assert g['month_floor'] == 30 and g['baseline_month'] == '2026-05'


def test_only_this_months_history_has_nothing_to_compare():
    spec = [(NOW - 5 * DAY + i, 9000) for i in range(30)] + [(NOW - DAY + i, 30000) for i in range(5)]
    sessions, read = _sessions(spec)
    assert listing.listing_growth(sessions, NOW, 4.0, read=read) is None


def test_thin_evidence_returns_none():
    sessions, read = _sessions(_month(2026, 6, 8000, 30) + [(NOW - DAY, 30000)] * 2)
    assert listing.listing_growth(sessions, NOW, 4.0, read=read) is None
    sessions, read = _sessions(_month(2026, 6, 8000, 2) + [(NOW - DAY, 30000)] * 5)
    assert listing.listing_growth(sessions, NOW, 4.0, read=read) is None


def test_shrinking_listing_has_no_growth():
    spec = _month(2026, 6, 30000, 30) + [(NOW - DAY + i, 9000) for i in range(4)]
    sessions, read = _sessions(spec)
    assert listing.listing_growth(sessions, NOW, 4.0, read=read)['growth_tokens'] == 0


def test_first_listing_chars_reads_initial_attachment(tmp_path):
    p = tmp_path / 't.jsonl'
    p.write_text(json.dumps({'type': 'user'}) + '\n' + json.dumps(
        {'attachment': {'type': 'skill_listing', 'isInitial': True, 'content': 'x' * 1234}}) + '\n')
    assert listing.first_listing_chars(str(p)) == 1234


def test_first_listing_chars_replaces_invalid_utf8(tmp_path):
    p = tmp_path / 't.jsonl'
    p.write_bytes(b'{"type":"user","text":"\xff"}\n'
                  b'{"attachment":{"type":"skill_listing","isInitial":true,"content":"a\xffb"}}\n')
    assert listing.first_listing_chars(str(p)) == 3


def test_first_listing_chars_supports_carriage_return_lines(tmp_path):
    p = tmp_path / 't.jsonl'
    p.write_bytes(b'{"type":"user"}\r'
                  b'{"attachment":{"type":"skill_listing","isInitial":true,"content":"abc"}}\r')
    assert listing.first_listing_chars(str(p)) == 3


class _Facade:
    def __init__(self, path):
        self.TRENDS_DB = path
        self.CHARS_PER_TOKEN = 4.0
        self.HOME = '/home/u'


def test_budget_is_not_available_without_usage_data(tmp_path):
    out = report.skill_listing_budget(f=_Facade(tmp_path / 'missing.db'))
    assert out == {'available': False, 'reason': 'no usage data yet'}


def test_budget_prices_growth_through_synthetic_trim(monkeypatch, tmp_path):
    db = tmp_path / 'trends.db'
    db.write_text('')
    f = _Facade(db)

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr(report, '_open_db', lambda _f: Conn())
    monkeypatch.setattr(report, '_local_sessions', lambda conn, home: [(1, 'a')])
    monkeypatch.setattr(report.listing, 'listing_growth', lambda s, now, cpt: {
        'current_chars': 30000, 'baseline_chars': 6000, 'baseline_month': '2026-04',
        'recent_sessions': 9, 'baseline_sessions': 30, 'growth_tokens': 6000})
    seen = {}

    def price(s, days, now=None, f=None, home=None):
        seen['s'], seen['days'] = s, days
        return {'events': 1200, 'cost_saved_usd': 10.81234}
    monkeypatch.setattr(report, 'price_synthetic', price)
    out = report.skill_listing_budget(days=30, f=f)
    assert seen == {'s': 6000, 'days': 30}
    assert out['available'] and out['calls'] == 1200 and out['cost_usd'] == 10.8123
    assert out['growth_tokens'] == 6000 and out['baseline_month'] == '2026-04'


def test_budget_never_raises(monkeypatch, tmp_path):
    db = tmp_path / 'trends.db'
    db.write_text('')

    def boom(_f):
        raise RuntimeError('x')
    monkeypatch.setattr(report, '_open_db', boom)
    assert report.skill_listing_budget(f=_Facade(db)) == {'available': False, 'reason': 'RuntimeError'}
