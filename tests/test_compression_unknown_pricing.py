"""Compression cost coverage must follow the event's model and session."""
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / 'skills' / 'token-optimizer' / 'scripts'

@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop('measure', None)
    mod = importlib.import_module('measure')
    mod._init_trends_db().close()
    yield mod
    sys.modules.pop('measure', None)


def test_unpriced_and_mixed_model_compression(m):
    td = m.PRICING_TIERS[m._load_pricing_tier()]
    assert m._compression_priced_rate('unknown', None, {}, 5, td) == 0
    assert m._compression_priced_rate('future-model-unknown', None, {}, 5, td) == 0
    assert m._compression_priced_rate('claude-opus-99-1', None, {}, 5, td) == 0
    assert m._compression_priced_rate('claude-3-5-sonnet-20241022', None, {}, 5, td) == pytest.approx(m._get_model_cost('claude-3-5-sonnet-20241022', 1_000_000, 0))
    conn = sqlite3.connect(str(m.TRENDS_DB))
    uid = '11111111-2222-3333-4444-555555555555'
    models = {'claude-opus-4-6': 1, 'gpt-5.4': 1, 'future-model-unknown': 2}
    conn.execute('INSERT INTO session_log (jsonl_path,date,session_uuid,model_usage_json) VALUES (?,?,?,?)',
                 ('/safe.jsonl', datetime.now().date().isoformat(), uid, json.dumps(models)))
    now = (datetime.now() - timedelta(days=1)).isoformat()
    for sid, model, feature in [(uid, 'unknown', 'bash_compress_git'),
                                ('other', 'future-model-unknown', 'bash_compress_pipeline'),
                                ('another', 'gpt-5.4', 'bash_compress_ls')]:
        conn.execute('INSERT INTO compression_events (timestamp,session_id,session_uuid,feature,original_tokens,compressed_tokens,tier,model) VALUES (?,?,?,?,?,?,?,?)',
                     (now, sid, uid if sid == uid else None, feature, 1000100, 100, 'measured', model))
    conn.commit(); conn.close()
    summary = m._get_compression_summary(days=30)
    parts = summary['by_feature']
    expect = (m._get_model_cost('claude-opus-4-6', 1_000_000, 0) + m._get_model_cost('gpt-5.4', 1, 0) * 1_000_000) / 4
    assert parts['bash_compress_git']['cost_saved_usd'] == pytest.approx(expect, abs=1e-6)
    assert parts['bash_compress_git']['unpriced_tokens'] == 500_000
    assert parts['bash_compress_pipeline']['cost_saved_usd'] == 0
    assert parts['bash_compress_pipeline']['unpriced_tokens'] == 1_000_000
    assert parts['bash_compress_ls']['cost_saved_usd'] == pytest.approx(m._get_model_cost('gpt-5.4', 1, 0) * 1_000_000, abs=1e-6)
    assert summary['total_tokens_saved'] == 3_000_000
    assert summary['unpriced_tokens'] == 1_500_000
    bucket = m._realized_savings_buckets(m._init_trends_db(), (datetime.now()-timedelta(days=30)).isoformat())['']
    assert bucket['usd'] == pytest.approx(sum(p['cost_saved_usd'] for p in parts.values()), abs=0.0002)


def test_session_mix_prefers_generation_breakdown_over_family(m):
    conn = m._init_trends_db()
    uid = '11111111-2222-3333-4444-555555555555'
    breakdown = {'claude-opus-4-6': {'fresh_input': 100, 'cache_read': 10000},
                 'claude-opus-5-5': {'fresh_input': 100},
                 'claude-opus-99-1': {'fresh_input': 200}}
    conn.execute('INSERT INTO session_log (jsonl_path,date,session_uuid,model_usage_json,model_usage_breakdown_json) VALUES (?,?,?,?,?)',
                 ('/x', datetime.now().date().isoformat(), uid, '{"opus": 400}', json.dumps(breakdown)))
    rates = m._compression_session_rates(conn, [uid])
    td = m.PRICING_TIERS[m._load_pricing_tier()]
    rate, coverage = m._compression_rate_coverage('unknown', uid, rates, 99, td)
    expected = (m._get_model_cost('claude-opus-4-6', 1_000_000, 0) + m._get_model_cost('claude-opus-5-5', 1_000_000, 0)) / 4
    assert rate == pytest.approx(expected)
    assert coverage == pytest.approx(.5)
    conn.close()


def test_foreign_session_mix_requires_an_unambiguous_exact_session(m):
    conn = m._init_trends_db()
    conn.execute('INSERT INTO session_log (jsonl_path,date,model_usage_json) VALUES (?,?,?)',
                 ('hermes:foreign-session', datetime.now().date().isoformat(), '{"gpt-5.4": 100}'))
    rates = m._compression_session_rates(conn, [], ['foreign-session'])
    td = m.PRICING_TIERS[m._load_pricing_tier()]
    assert m._compression_rate_coverage('unknown', ('session_id', 'foreign-session'), rates, 99, td)[1] == 1
    conn.execute('INSERT INTO session_log (jsonl_path,date,model_usage_json) VALUES (?,?,?)',
                 ('cursor:foreign-session', datetime.now().date().isoformat(), '{"opus": 100}'))
    rates = m._compression_session_rates(conn, [], ['foreign-session'])
    assert m._compression_rate_coverage('unknown', ('session_id', 'foreign-session'), rates, 99, td) == (0, 0)
    conn.close()


def test_unpriced_coverage_flows_into_savings_report(m, monkeypatch, capsys):
    categories = {}
    m._merge_v5_compression(categories, {'bash_compress_git': {
        'events': 1, 'tokens_saved': 1000, 'cost_saved_usd': 0.0, 'unpriced_tokens': 1000}}, 5)
    assert categories['bash_compress_git']['unpriced_tokens'] == 1000
    summary = {'total_tokens': 1000, 'total_events': 1, 'total_cost_usd': 0,
               'compression_unpriced_tokens': 1000, 'by_category': categories}
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kwargs: dict(summary))
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda days: {'available': False})
    monkeypatch.setattr(m, '_install_date', lambda: None)
    m.savings_report(days=30)
    assert '1,000 compression tokens are counted but have no known model rate' in capsys.readouterr().out
    m.savings_report(days=30, as_json=True)
    assert json.loads(capsys.readouterr().out)['compression_unpriced_tokens'] == 1000


def _add_events(m, rows):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    ts = (datetime.now() - timedelta(days=1)).isoformat()
    for feature, model, original, compressed in rows:
        conn.execute('INSERT INTO compression_events (timestamp,session_id,session_uuid,feature,original_tokens,compressed_tokens,tier,model)'
                     ' VALUES (?,?,?,?,?,?,?,?)', (ts, 'unpriced-session', None, feature, original, compressed, 'measured', model))
    conn.commit(); conn.close()


def test_unknown_model_debit_is_kept_as_unpriced(m):
    _add_events(m, [('bash_compress_git', 'future-model-unknown', 100, 200)])
    summary = m._get_compression_summary(days=30)
    part = summary['by_feature']['bash_compress_git']
    assert part['tokens_saved'] == -100
    assert part['unpriced_debit_tokens'] == 100
    assert part['unpriced_credit_tokens'] == 0
    assert part['unpriced_tokens'] == 100
    assert summary['unpriced_tokens'] == 100


def test_unknown_credit_and_debit_in_one_group_do_not_cancel(m):
    _add_events(m, [('bash_compress_git', 'future-model-unknown', 1000, 100),
                    ('bash_compress_git', 'future-model-unknown', 100, 200)])
    summary = m._get_compression_summary(days=30)
    part = summary['by_feature']['bash_compress_git']
    assert part['tokens_saved'] == 800
    assert (part['unpriced_credit_tokens'], part['unpriced_debit_tokens']) == (900, 100)
    assert part['unpriced_tokens'] == 1000
    assert summary['unpriced_credit_tokens'] == 900
    assert summary['unpriced_debit_tokens'] == 100


def test_unpriced_credit_and_debit_survive_merge_and_report(m, monkeypatch, capsys):
    categories = {}
    m._merge_v5_compression(categories, {'bash_compress_git': {
        'events': 2, 'tokens_saved': 800, 'cost_saved_usd': 0.0, 'unpriced_tokens': 1000,
        'unpriced_credit_tokens': 900, 'unpriced_debit_tokens': 100}}, 5)
    assert categories['bash_compress_git']['unpriced_credit_tokens'] == 900
    assert categories['bash_compress_git']['unpriced_debit_tokens'] == 100
    summary = {'total_tokens': 800, 'total_events': 2, 'total_cost_usd': 0,
               'compression_unpriced_tokens': 1000, 'compression_unpriced_credit_tokens': 900,
               'compression_unpriced_debit_tokens': 100, 'by_category': categories}
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kwargs: dict(summary))
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda days: {'available': False})
    monkeypatch.setattr(m, '_install_date', lambda: None)
    m.savings_report(days=30)
    out = capsys.readouterr().out
    assert '1,000 compression tokens are counted but have no known model rate' in out
    assert '900 credits, 100 debits' in out


def test_merged_summary_totals_unpriced_credits_and_debits(m, monkeypatch):
    monkeypatch.setattr(m, '_get_savings_summary', lambda **kw: {'by_category': {}, 'total_tokens': 0,
                                                                  'total_cost_usd': 0.0, 'total_events': 0})
    monkeypatch.setattr(m, '_get_compression_summary', lambda **kw: {'by_feature': {'bash_compress_git': {
        'events': 2, 'tokens_saved': 800, 'cost_saved_usd': 0.0, 'unpriced_tokens': 1000,
        'unpriced_credit_tokens': 900, 'unpriced_debit_tokens': 100}}})
    merged = m._get_merged_savings(days=30)
    assert merged['compression_unpriced_tokens'] == 1000
    assert merged['compression_unpriced_credit_tokens'] == 900
    assert merged['compression_unpriced_debit_tokens'] == 100
