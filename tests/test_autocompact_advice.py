"""Auto-compact setting changes honor the existing configuration."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
from _measure import autocompact_advice


def test_apply_uses_per_model_setting_and_preserves_other_values():
    original = {'autoCompactWindow': 800000, 'modelSettings': {'model-full': {'autoCompactWindow': 700000}}, 'other': 1}
    writes = []
    facade = SimpleNamespace(_read_settings_for_write=lambda: (copy.deepcopy(original), True),
                             _write_settings_atomic=lambda data, **kw: (writes.append(copy.deepcopy(data)) or True))
    result = autocompact_advice.apply_item(facade, {'model': 'model-full', 'target': 450000})
    assert result['ok']
    assert result['before'] == 700000
    assert writes[0]['autoCompactWindow'] == 800000
    assert writes[0]['modelSettings']['model-full']['autoCompactWindow'] == 450000
    assert 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE' not in str(writes[0])


def test_unreadable_settings_are_never_written():
    writes = []
    facade = SimpleNamespace(_read_settings_for_write=lambda: ({}, False),
                             _write_settings_atomic=lambda data, **kw: writes.append(data))
    assert not autocompact_advice.apply_item(facade, {'model': 'model-full', 'target': 450000})['ok']
    assert not writes


def _fixture(monkeypatch, peaks, *, window=1000000, rate=1.0, cost=0.0):
    from datetime import datetime, timedelta
    now = datetime.now()
    sessions = [([{'stamp': now, 'context': peak, 'model': 'model-full', 'cost_usd': cost}], [now + timedelta(seconds=1)]) for peak in peaks]
    monkeypatch.setattr(autocompact_advice, '_recent_sessions', lambda facade: sessions)
    monkeypatch.setattr(autocompact_advice.limit_units, '_resolve_card', lambda *args, **kwargs: {'cache_read': rate, 'cache_write_1h': 2.0})
    monkeypatch.delenv('CLAUDE_CODE_AUTO_COMPACT_WINDOW', raising=False)
    return SimpleNamespace(_context_window_for_model_str=lambda model: window,
                           _read_settings_json=lambda: ({}, True),
                           effective_compact_window=lambda *args, **kwargs: (800000, 'default'))


def test_small_context_model_is_not_recommended(monkeypatch):
    facade = _fixture(monkeypatch, [600000] * 20, window=200000)
    assert autocompact_advice.recommend(facade) is None


def test_environment_override_blocks_recommendation(monkeypatch):
    monkeypatch.setenv('CLAUDE_CODE_AUTO_COMPACT_WINDOW', '700000')
    assert autocompact_advice.recommend(object())['blocked'] == 'env'


def test_p75_selects_appropriate_window(monkeypatch):
    for peaks, expected in [([250000] * 80 + [600000] * 20, 400000), ([400000] * 75 + [600000] * 25, 450000), ([600000] * 20, 500000)]:
        facade = _fixture(monkeypatch, peaks)
        assert autocompact_advice.recommend(facade)['target'] == expected


def test_undo_preserves_unrelated_settings_and_refuses_later_edits():
    settings = {'autoCompactWindow': 450000, 'other': 42}
    writes = []
    facade = SimpleNamespace(_read_settings_for_write=lambda: (copy.deepcopy(settings), True),
                             _write_settings_atomic=lambda data, **kw: (writes.append(data) or True))
    receipt = {'model': None, 'before': 800000, 'before_exists': True, 'after': 450000}
    assert autocompact_advice.undo_item(facade, receipt)
    assert writes == [{'autoCompactWindow': 800000, 'other': 42}]
    settings['autoCompactWindow'] = 500000
    assert not autocompact_advice.undo_item(facade, receipt)
    assert len(writes) == 1


def test_recommendation_subtracts_compaction_cost(monkeypatch):
    from datetime import datetime
    facade = _fixture(monkeypatch, [1000000] * 20)
    turns = [([{'stamp': datetime.now(), 'context': 1000000, 'model': 'model-full'}], []) for _ in range(20)]
    monkeypatch.setattr(autocompact_advice, '_recent_sessions', lambda _: turns)
    monkeypatch.setattr(autocompact_advice, '_median_compaction_cost', lambda _: .2)
    monkeypatch.setattr(autocompact_advice.limit_units, '_resolve_card', lambda *a, **kw: {'cache_read': 1, 'cache_write_1h': 0})
    result = autocompact_advice.recommend(facade)
    assert round(result['gross_usd_month'], 2) == 10
    assert round(result['own_cost_usd_month'], 2) == 4
    assert round(result['net_usd_month'], 2) == 6
    monkeypatch.setattr(autocompact_advice, '_median_compaction_cost', lambda _: 1)
    assert autocompact_advice.recommend(facade) is None


def test_recorded_estimate_shows_in_savings_summary(tmp_path, monkeypatch):
    import importlib
    from datetime import datetime
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()
    import measure
    measure = importlib.reload(measure)
    conn = measure._init_trends_db()
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,detail,model) VALUES(?,?,?,?,?,?,?)",
                 (datetime.now().isoformat(), 'autocompact_window_estimated', 1200, .12, '11111111-2222-3333-4444-555555555555', '{"net_limit_units": 1200}', 'claude-opus-5-5'))
    conn.commit()
    conn.close()
    summary = measure._get_savings_summary(days=30)
    pool = summary['autocompact_window_estimated']
    assert pool['label'] == 'Earlier auto-compact'
    assert pool['cost_saved_usd'] == .12
    assert measure._action_savings_components(summary)['estimates_usd']['autocompact_window_estimated'] == .12
    from _measure import limit_units
    conn = measure._init_trends_db()
    limits = limit_units.plan_limits_summary(measure, conn, 30, summary=summary)
    conn.close()
    assert limits['saved_units'] > 0


def test_negative_recorded_estimate_subtracts_from_total():
    import measure
    parts = measure._action_savings_components({'total_cost_usd': 5, 'autocompact_window_estimated': {'cost_saved_usd': -2}}, {})
    assert parts['estimates_usd']['autocompact_window_estimated'] == -2
    assert parts['total_usd'] == 3
