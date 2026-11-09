"""The token headline adds the counted cohort once and preserves coverage."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_counted_cumulative import m

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
HTML = SCRIPTS.parent / 'assets/dashboard.html'
sys.path.insert(0, str(SCRIPTS))


def sample():
    return {'period_days': 30, 'total_tokens': 1000,
            'resume_lean_estimated': {'tokens_saved': 200},
            'counted_period': {'available': True, 'removed_tokens': 900,
                'reread_tokens': 5000, 'oneshot_usd': 1.5, 'reread_usd': .25,
                'refresh_incomplete_events': 2, 'pricing_note': 'Incomplete prices'}}


def test_headline_adds_only_counted_rereads_and_retains_coverage():
    from _measure.headline import token_headline
    result = token_headline(sample())
    assert result['removed_tokens'] == 1200
    assert result['reread_tokens'] == 5000
    assert result['total_tokens'] == 6200
    assert result['removed_priced_tokens'] == 900
    assert result['refresh_incomplete_events'] == 2
    assert result['pricing_note'] == 'Incomplete prices'


def test_unavailable_cohort_never_adds_lifetime_or_raw_rows():
    from _measure.headline import token_headline
    s = sample(); s['counted_period'] = {'available': False, 'reread_tokens': 999}
    s['counted_cumulative'] = {'available': True, 'reread_tokens': 1000000}
    result = token_headline(s)
    assert result['total_tokens'] == 1200
    assert not result['reread_available']


def test_actual_dashboard_renderer():
    from _measure.headline import token_headline
    from test_savings_render import render, fixture
    s = fixture(); s.update(sample()); s['token_headline'] = token_headline(s)
    out = render(s).split('savings-actions')[1].split('savings-history')[0]
    assert '~6K tokens kept out of context' in out
    assert '~1K context removed' in out and '~5K re-reads avoided' in out
    assert 'reload avoided' in out and ' fresh' not in out
    assert 'incomplete coverage' in out and 'estimated' in out


def test_cli_json_and_text_show_same_components(m, monkeypatch, capsys):
    s = sample()
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kw: dict(s))
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda **kw: s['counted_period'])
    m.savings_report(days=30, as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data['token_headline']['total_tokens'] == 6200
    m.savings_report(days=30, as_json=False)
    text = capsys.readouterr().out
    assert '6,200' in text and '1,000 context removed' in text and '200 reload avoided' in text
    assert '5,000 re-reads avoided' in text and ' fresh' not in text


def test_opus_55_uses_its_exact_cache_read_rate(m):
    card = m._counted_exact_card('claude-opus-5-5', m.PRICING_TIERS['anthropic'])
    assert card['cache_read'] == pytest.approx(.20)


def test_traffic_includes_cache_and_uses_activity_dates():
    import sqlite3
    from _measure.headline import input_traffic
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE session_log(parent_daily_usage_json TEXT, daily_usage_json TEXT, is_sidechain INTEGER)')
    buckets = {'2026-11-01': {'opus': {'fresh_input': 100, 'cache_create': 200,
                                       'cache_read': 700, 'output': 50}},
               '2026-09-01': {'opus': {'cache_read': 9999}}}
    db.execute('INSERT INTO session_log VALUES (?, NULL, 0)', (json.dumps(buckets),))
    db.execute('INSERT INTO session_log VALUES (?, NULL, 1)', (json.dumps(buckets),))
    result = input_traffic(db, '2026-10-10', '2026-11-09')
    assert result['tokens'] == 1050
    assert result['complete'] is True


def test_runway_token_line_matches_multiplier():
    from test_runway_routing_unify import _card_text, _rw
    rw = _rw(tokens_consumed=1_000_000, context_tokens_saved=400_000,
             context_multiplier=1.4, token_headline={'removed_tokens': 100_000,
                                                       'reread_tokens': 300_000, 'reread_available': True})
    text = _card_text(rw)
    assert '~400000' in text and '~100000 removed' in text and '~300000 re-reads avoided' in text
    assert '28.6% of would-be input and output including cache' in text
    assert 28.6 == pytest.approx(100 * (1 - 1 / rw['context_multiplier']), abs=0.05)
