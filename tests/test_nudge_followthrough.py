"""Follow-through is based on transcript boundaries and actual later requests."""
import importlib.util
import json
import sqlite3
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts/_measure/nudge_followthrough.py'
spec = importlib.util.spec_from_file_location('nudge_followthrough', MODULE)
nudge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nudge)


def assistant(n, model='sonnet', context=100):
    return {'type': 'assistant', 'timestamp': f'2026-01-01T00:{n // 60:02}:{n % 60:02}+00:00', 'uuid': str(n), 'message': {'id': str(n), 'model': model, 'usage': {'cache_read_input_tokens': context}}}


def boundary(n=10):
    return {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': f'2026-01-01T00:00:{n:02}+00:00'}


def test_boundary_is_structural_and_ignored_after_twenty():
    stamp = '2026-01-01T00:00:00+00:00'
    assert nudge.classify([assistant(i) for i in range(1, 22)] + [boundary(30)], stamp)[0] == 'ignored'
    assert nudge.classify([{'type': 'user', 'timestamp': stamp, 'message': {'content': 'compact_boundary'}}], stamp)[0] == 'pending'
    assert nudge.classify([assistant(1), boundary(), assistant(11)], stamp)[0] == 'followed'


def test_new_session_and_clear():
    stamp = '2026-01-01T00:00:00+00:00'
    assert nudge.classify([], stamp, [assistant(5)])[0] == 'followed'
    clear = {'type': 'user', 'timestamp': stamp, 'message': {'content': '/clear'}}
    assert nudge.classify([clear, assistant(1)], stamp)[0] == 'followed'


def test_price_each_request_cap_and_compaction_dedupe(tmp_path):
    db = tmp_path / 'test.db'
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text('\n'.join(json.dumps(r) for r in [boundary()] + [assistant(i, 'opus' if i % 2 else 'sonnet') for i in range(11, 71)]))
    class Facade:
        def _init_trends_db(self):
            return sqlite3.connect(db)
        def _is_priced_model(self, model):
            return model in ('sonnet', 'opus')
        def _get_model_cost(self, model, a, b, cache, d):
            return cache * (2 if model == 'opus' else 1) / 1000000
    facade = Facade()
    conn = facade._init_trends_db()
    conn.execute('CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    conn.execute('CREATE TABLE compression_events (timestamp TEXT, session_uuid TEXT, session_id TEXT, feature TEXT, verified INTEGER)')
    nudge._schema(conn)
    conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)', ('session', 'compact', '2026-01-01T00:00:00+00:00', 1000, str(transcript)))
    conn.commit(); conn.close()
    nudge.resolve(facade)
    conn = facade._init_trends_db()
    row = conn.execute("SELECT tokens_saved,cost_saved_usd FROM savings_events WHERE event_type='nudge_followed'").fetchone()
    assert row[0] == 45000
    assert abs(row[1] - 0.0675) < 1e-10
    conn.execute("INSERT INTO compression_events VALUES(?,?,?,?,?)", ('2026-01-01T00:00:10+00:00', 'session', 'session', 'quality_nudge', 1))
    conn.commit(); conn.close()
    nudge.resolve(facade)
    conn = facade._init_trends_db()
    assert conn.execute("SELECT tokens_saved,cost_saved_usd FROM savings_events WHERE event_type='nudge_followed'").fetchone() == (0, 0)
    conn.close()


def test_runway_uses_all_savings_tiers():
    import datetime
    from types import SimpleNamespace
    path = MODULE.with_name('runway_all_in.py')
    spec = importlib.util.spec_from_file_location('runway_all_in', path)
    runway = importlib.util.module_from_spec(spec); spec.loader.exec_module(runway)
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE session_log(parent_daily_usage_json TEXT, is_sidechain INTEGER)')
    today = datetime.date.today().isoformat()
    conn.execute('INSERT INTO session_log VALUES(?,0)', (json.dumps({today: {'sonnet': {'cache_read': 1000}}}),))
    facade = SimpleNamespace(datetime=datetime.datetime, timedelta=datetime.timedelta,
        _is_priced_model=lambda m: True,
        _get_model_cost=lambda m, i, o, r, w: r / 1000,
        _counted_cumulative_summary=lambda days: {},
        _apply_counted_oneshot_pricing=lambda summary, counted: None,
        _action_savings_components=lambda summary, counted: {'total_usd': 0.6, 'routing_usd': 0.2})
    assert abs(runway.extra_work(facade, conn, 30, {}) - 80) < 1e-9
    conn.close()


def test_runway_estimate_increment_excludes_tokens_already_counted():
    import datetime
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('runway_all_in_increment', MODULE.with_name('runway_all_in.py'))
    runway = importlib.util.module_from_spec(spec); spec.loader.exec_module(runway)
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE session_log(parent_daily_usage_json TEXT, is_sidechain INTEGER)')
    today = datetime.date.today().isoformat()
    conn.execute('INSERT INTO session_log VALUES(?,0)', (json.dumps({today: {'sonnet': {'cache_read': 1000}}}),))
    facade = SimpleNamespace(datetime=datetime.datetime, timedelta=datetime.timedelta,
        _is_priced_model=lambda model: True,
        _get_model_cost=lambda model, i, o, r, w: r / 1000,
        _counted_cumulative_summary=lambda days: {},
        _apply_counted_oneshot_pricing=lambda summary, counted: None,
        _action_savings_components=lambda summary, counted: {'estimates_usd': {
            'nudge_followed': 0.2, 'hint_followed': 0.3, 'resume_lean_verified': 0.4}})
    assert abs(runway.extra_work(facade, conn, 30, {}, estimated_only=True) - 20) < 1e-9
    conn.close()
