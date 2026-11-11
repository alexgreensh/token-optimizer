from test_since_install_all_in import m, _install


def test_since_install_skips_unused_comparison_without_changing_other_calls(m, monkeypatch):
    _install(m, 2)
    calls = []
    monkeypatch.setattr(m, '_estimate_before_after_savings', lambda **kwargs: calls.append(kwargs) or {'marker': 1})
    assert m._savings_since_install() is not None
    assert calls == []
    assert m._get_merged_savings(days=30)['before_after'] == {'marker': 1}
    assert len(calls) == 1


def test_comparison_flag_resets_after_failure():
    import pytest
    from _measure import savings_story
    def fail(**kwargs):
        raise RuntimeError('failure')
    with pytest.raises(RuntimeError):
        savings_story.since_install_summary(fail, 30)
    assert savings_story.skip_comparison() is False
