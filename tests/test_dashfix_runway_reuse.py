from test_runway_net_tokens import m


def test_runway_reuses_dashboard_token_headline(m, monkeypatch):
    monkeypatch.setattr(m, '_get_merged_savings', lambda **kwargs: {'total_tokens': 100000, 'by_category': {}})
    monkeypatch.setattr(m, '_counted_cumulative_summary', lambda **kwargs: {'available': True, 'reread_tokens': 200000})
    original = m.runway_snapshot(days=30)
    headline = original['token_headline']
    assert original['context_tokens_saved'] == 300000
    def unexpected(*args, **kwargs):
        raise AssertionError('runway repeated the ledger summary')
    monkeypatch.setattr(m, '_get_merged_savings', unexpected)
    monkeypatch.setattr(m, '_counted_cumulative_summary', unexpected)
    result = m.runway_snapshot(days=30, token_headline=headline)
    assert result['token_headline'] == headline
    assert result['context_tokens_saved'] == original['context_tokens_saved']
    assert result['context_multiplier'] == original['context_multiplier']
