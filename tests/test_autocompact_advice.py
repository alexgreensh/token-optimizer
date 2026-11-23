"""Auto-compact setting changes honor the existing configuration."""
import copy
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
from _measure import autocompact_advice


def test_apply_and_undo_hold_lock_across_read_and_write():
    from contextlib import contextmanager
    state = {'locked': False, 'settings': {'autoCompactWindow': 800000, 'other': 1}}
    @contextmanager
    def lock(**kwargs):
        assert not state['locked']
        state['locked'] = True
        try:
            yield True
        finally:
            state['locked'] = False
    def read():
        assert state['locked']
        return copy.deepcopy(state['settings']), True
    def write(data, **kwargs):
        assert state['locked']
        state['settings'] = copy.deepcopy(data)
        return True
    facade = SimpleNamespace(_settings_lock=lock, _read_settings_for_write=read,
                             _write_settings_atomic_locked=write)
    receipt = autocompact_advice.apply_item(facade, {'model': 'model-full', 'target': 450000})
    assert receipt['ok']
    assert autocompact_advice.undo_item(facade, receipt)
    assert state['settings'] == {'autoCompactWindow': 800000, 'other': 1}


def test_apply_uses_per_model_setting_and_preserves_other_values():
    original = {'autoCompactWindow': 800000, 'modelSettings': {'model-full': {'autoCompactWindow': 700000}}, 'other': 1}
    writes = []
    facade = SimpleNamespace(_settings_lock=lambda **kw: nullcontext(True), _read_settings_for_write=lambda: (copy.deepcopy(original), True),
                             _write_settings_atomic_locked=lambda data, **kw: (writes.append(copy.deepcopy(data)) or True))
    result = autocompact_advice.apply_item(facade, {'model': 'model-full', 'target': 450000})
    assert result['ok']
    assert result['before'] == 700000
    assert writes[0]['autoCompactWindow'] == 800000
    assert writes[0]['modelSettings']['model-full']['autoCompactWindow'] == 450000
    assert 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE' not in str(writes[0])


def test_unreadable_settings_are_never_written():
    writes = []
    facade = SimpleNamespace(_settings_lock=lambda **kw: nullcontext(True), _read_settings_for_write=lambda: ({}, False),
                             _write_settings_atomic_locked=lambda data, **kw: writes.append(data))
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
    facade = SimpleNamespace(_settings_lock=lambda **kw: nullcontext(True), _read_settings_for_write=lambda: (copy.deepcopy(settings), True),
                             _write_settings_atomic_locked=lambda data, **kw: (writes.append(data) or True))
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


def test_compaction_estimate_net_positive_and_negative(monkeypatch):
    from datetime import datetime, timedelta
    facade = SimpleNamespace()
    monkeypatch.setattr(autocompact_advice.limit_units, '_resolve_card', lambda *a, **kw: {'input': 1, 'cache_read': 1, 'cache_write': 1, 'cache_write_1h': 2, 'output': 1})
    stamp = datetime(2026, 1, 1, 12)
    prior = {'stamp': stamp - timedelta(seconds=10), 'model': 'model-full', 'cache_creation_1h': 0}
    later = [{'stamp': stamp + timedelta(seconds=n), 'model': 'model-full', 'cache_read': 400000} for n in range(1, 11)]
    event = {'stamp': stamp, 'before': 450000, 'after': 50000, 'window': 450000, 'previous_window': 800000}
    result = autocompact_advice.estimate_compaction(facade, event, prior, later, 10)
    assert round(result['cost_saved_usd'], 2) == 3.45
    assert result['tokens_saved'] == 4000000
    negative = autocompact_advice.estimate_compaction(facade, event, prior, later[:1], 10)
    assert round(negative['cost_saved_usd'], 2) == -.15


def test_compaction_at_previous_window_is_not_credited(monkeypatch):
    from datetime import datetime
    event = {'stamp': datetime(2026, 1, 1), 'before': 450000, 'after': 50000, 'window': 450000, 'previous_window': 450000}
    assert autocompact_advice.estimate_compaction(object(), event, {}, [], 10) is None


def test_manual_receipt_prevents_auto_booking(monkeypatch):
    from datetime import datetime
    event = {'stamp': datetime(2026, 1, 1), 'before': 450000, 'after': 50000, 'window': 450000, 'previous_window': 800000, 'manual_overlap': True}
    assert autocompact_advice.estimate_compaction(object(), event, {}, [], 10) is None


def test_auto_booking_is_deduplicated_and_manual_receipts_take_precedence(tmp_path, monkeypatch):
    import sqlite3
    from datetime import datetime, timedelta
    db = tmp_path / 'ledger.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE compression_events(session_id TEXT, session_uuid TEXT, feature TEXT, detail TEXT)')
    conn.execute('CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    conn.execute("INSERT INTO compression_events VALUES('main','main','mod_clean','compaction_id=boundary')")
    conn.commit()
    conn.close()
    stamp = datetime(2026, 1, 1, 12)
    prior = {'stamp': stamp - timedelta(seconds=10), 'model': 'model-full', 'cache_creation_1h': 0}
    later = [{'stamp': stamp + timedelta(seconds=n), 'model': 'model-full', 'cache_read': 400000} for n in range(1, 11)]
    sessions = [(list([prior] + later), [stamp], str(tmp_path / (name + '.jsonl'))) for name in ['main', 'a', 'b', 'c', 'd', 'e']]
    event = {'stamp': stamp, 'before': 450000, 'after': 50000, 'boundary_id': 'boundary'}
    facade = SimpleNamespace(Path=Path, _extract_session_uuid=lambda sid: (sid, False), _init_trends_db=lambda: sqlite3.connect(db))
    monkeypatch.setattr(autocompact_advice, '_applications', lambda _: [{'start': stamp - timedelta(days=1), 'end': None, 'window': 450000, 'previous_window': 800000, 'receipt_id': 'receipt', 'model': None}])
    monkeypatch.setattr(autocompact_advice, '_recent_sessions', lambda *a, **kw: sessions)
    monkeypatch.setattr(autocompact_advice, '_automatic_boundaries', lambda path: [dict(event)] if Path(path).stem == 'main' else [])
    monkeypatch.setattr(autocompact_advice.limit_units, '_resolve_card', lambda *a, **kw: {'input': 1, 'cache_read': 1, 'cache_write': 1, 'output': 1})
    assert autocompact_advice.refresh_bookings(facade) == 0
    conn = sqlite3.connect(db)
    conn.execute('DELETE FROM compression_events')
    conn.commit()
    conn.close()
    assert autocompact_advice.refresh_bookings(facade) == 1
    assert autocompact_advice.refresh_bookings(facade) == 0
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT event_type,cost_saved_usd,detail FROM savings_events").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0][0] == 'autocompact_window_estimated'
    assert round(rows[0][1], 2) == 3.45
    assert json.loads(rows[0][2])['basis'] == 'pessimistic estimate'
    sessions[0] = ([prior] + later[:1], [stamp], str(tmp_path / 'main.jsonl'))
    assert autocompact_advice.refresh_bookings(facade) == 0
    conn = sqlite3.connect(db)
    assert round(conn.execute('SELECT cost_saved_usd FROM savings_events').fetchone()[0], 2) == -.15
    conn.close()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO compression_events VALUES('main','main','mod_clean','compaction_id=boundary')")
    conn.commit()
    conn.close()
    assert autocompact_advice.refresh_bookings(facade) == 0
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT tokens_saved,cost_saved_usd FROM savings_events').fetchone() == (0, 0.0)
    conn.close()


def test_auto_boundary_parser_ignores_manual_records(tmp_path):
    path = tmp_path / 'session.jsonl'
    rows = [
        {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-01-01T12:00:00Z', 'uuid': 'a', 'compactMetadata': {'trigger': 'auto', 'preTokens': 450000, 'postTokens': 50000}},
        {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-01-01T12:01:00Z', 'uuid': 'b', 'compactMetadata': {'trigger': 'manual', 'preTokens': 450000, 'postTokens': 50000}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    events = autocompact_advice._automatic_boundaries(path)
    assert len(events) == 1 and events[0]['boundary_id'] == 'a'
    assert events[0]['before'] == 450000 and events[0]['after'] == 50000


def test_transcript_to_ledger_with_real_parser(tmp_path, monkeypatch):
    import importlib
    import uuid
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()
    import measure
    measure = importlib.reload(measure)
    stamp = datetime.now(timezone.utc) - timedelta(minutes=20)
    model = 'claude-opus-5-5'
    conn = measure._init_trends_db()
    for index in range(6):
        sid = str(uuid.uuid4())
        path = tmp_path / (sid + '.jsonl')
        rows = []
        for turn in range(11):
            ts = stamp + timedelta(seconds=turn - 1)
            if index == 0 and turn == 1:
                rows.append({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': stamp.isoformat(), 'uuid': 'compact-one', 'compactMetadata': {'trigger': 'auto', 'preTokens': 450000, 'postTokens': 50000}})
            req = f'{sid}-{turn}'
            rows.append({'type': 'assistant', 'requestId': req, 'timestamp': ts.isoformat(), 'sessionId': sid,
                         'message': {'id': req, 'model': model, 'role': 'assistant', 'content': [],
                                     'usage': {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 400000, 'cache_creation_input_tokens': 0}}})
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        day = stamp.date().isoformat()
        conn.execute('INSERT INTO session_log(jsonl_path,date,last_activity_day,session_uuid,is_sidechain) VALUES(?,?,?,?,0)', (str(path), day, day, sid))
    conn.commit()
    conn.close()
    naive = stamp.replace(tzinfo=None)
    monkeypatch.setattr(autocompact_advice, '_applications', lambda _: [{'start': naive - timedelta(days=1), 'end': None, 'window': 450000, 'previous_window': 800000, 'receipt_id': 'receipt', 'model': model}])
    assert autocompact_advice.refresh_bookings(measure) == 1
    assert autocompact_advice.refresh_bookings(measure) == 0
    conn = measure._init_trends_db()
    rows = conn.execute("SELECT event_type,detail FROM savings_events WHERE event_type='autocompact_window_estimated'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert json.loads(rows[0][1])['basis'] == 'pessimistic estimate'


def test_hook_process_never_refreshes_compaction_history(monkeypatch):
    calls = []
    monkeypatch.setattr(autocompact_advice.sys, 'argv', ['measure.py', 'session-end-flush'])
    monkeypatch.setattr(autocompact_advice, 'refresh_bookings', lambda _: calls.append(True))
    monkeypatch.setattr(autocompact_advice, '_last_refresh', 0.0)
    autocompact_advice.refresh_if_allowed(object())
    assert calls == []
