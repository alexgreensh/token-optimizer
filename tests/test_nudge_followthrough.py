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


def test_next_session_must_start_within_thirty_minutes():
    stamp = '2026-01-01T00:00:00+00:00'
    original = [assistant(1)]
    near = dict(assistant(2), timestamp='2026-01-01T00:10:01+00:00')
    late = dict(assistant(2), timestamp='2026-01-03T00:00:01+00:00')
    assert nudge.classify(original, stamp, [near])[0] == 'followed'
    assert nudge.classify(original, stamp, [late])[0] == 'ignored'


def test_resolver_reads_each_project_transcript_once_before_write_lock(tmp_path, monkeypatch):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    conn.execute('CREATE TABLE compression_events (timestamp TEXT, session_uuid TEXT, session_id TEXT, feature TEXT, verified INTEGER)')
    nudge._schema(conn)
    path = tmp_path / 'session.jsonl'
    path.write_text(json.dumps(assistant(1)))
    other = tmp_path / 'next.jsonl'
    other.write_text(json.dumps(assistant(2)))
    for _ in range(2):
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)', ('s', 'tip', '2026-01-01T00:00:00+00:00', 0, str(path)))
    conn.commit()
    read = nudge._rows
    seen = []
    def tracked(file):
        assert not conn.in_transaction
        seen.append(file)
        yield from read(file)
    monkeypatch.setattr(nudge, '_rows', tracked)
    nudge.resolve(object(), conn)
    assert sorted(seen) == sorted([path, other])
    conn.close()


def test_resolver_failure_is_logged_and_event_stays_pending(tmp_path, monkeypatch):
    conn = sqlite3.connect(':memory:')
    nudge._schema(conn)
    conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,transcript_path) VALUES(?,?,?,?)', ('s', 'tip', '2026-01-01T00:00:00+00:00', str(tmp_path / 's.jsonl')))
    conn.commit()
    class Facade:
        SNAPSHOT_DIR = tmp_path
    def fail(_):
        raise PermissionError('private transcript path')
    monkeypatch.setattr(nudge, '_project_index', fail)
    nudge.resolve(Facade(), conn)
    assert conn.execute('SELECT status FROM nudge_events').fetchone() == ('pending',)
    line = (tmp_path / 'nudge-resolve.log').read_text()
    assert 'PermissionError' in line and 'private transcript path' not in line
    conn.close()


def test_new_session_streamed_rows_count_once():
    stamp = '2026-01-01T00:00:00+00:00'
    row = assistant(2)
    status, _, requests = nudge.classify([assistant(1)], stamp, [row, row, assistant(3)])
    assert status == 'followed'
    assert [request['id'] for request in requests] == ['2', '3']
