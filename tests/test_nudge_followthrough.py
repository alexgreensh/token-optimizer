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
    conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)', ('session', 'compact', '2026-01-01T00:00:00+00:00', 1000, str(transcript)))
    conn.commit(); conn.close()
    nudge.resolve(facade)
    conn = facade._init_trends_db()
    rows = conn.execute("SELECT tokens_saved,cost_saved_usd FROM savings_events WHERE event_type='nudge_followed'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 45000 and abs(rows[0][1] - 0.0675) < 1e-10
    assert conn.execute('SELECT status,savings_event_id FROM nudge_events ORDER BY id').fetchall() == [('followed', 1), ('followed', None)]
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


def test_malformed_usage_is_skipped_without_stopping_followthrough():
    bad = assistant(12)
    bad['message']['usage']['cache_read_input_tokens'] = 'bad'
    status, _, requests = nudge.classify([boundary(), bad, assistant(13)], '2026-01-01T00:00:00+00:00')
    assert status == 'followed'
    assert [r['id'] for r in requests] == ['13']


def test_new_session_credits_only_most_recent_nudge(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    conn.execute('CREATE TABLE compression_events (timestamp TEXT, session_uuid TEXT, session_id TEXT, feature TEXT, verified INTEGER)')
    nudge._schema(conn)
    old, recent, new = [tmp_path / (name + '.jsonl') for name in ('old', 'recent', 'new')]
    for path, second in ((old, 4), (recent, 4), (new, 10)):
        path.write_text(json.dumps(assistant(second)))
    for path, stamp in ((old, '2026-01-01T00:00:01+00:00'), (recent, '2026-01-01T00:00:03+00:00')):
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)', (path.stem, 'tip', stamp, 0, str(path)))
    conn.commit()
    nudge.resolve(object(), conn)
    assert conn.execute('SELECT session_uuid,status FROM nudge_events ORDER BY id').fetchall() == [('old', 'ignored'), ('recent', 'followed')]
    conn.close()


def test_successor_counts_only_after_nudged_session_ends(tmp_path):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    def at(minutes):
        return (now - datetime.timedelta(minutes=minutes)).isoformat()
    active = tmp_path / 'active.jsonl'
    active.write_text('\n'.join(json.dumps(dict(assistant(i), timestamp=at(m))) for i, m in ((1, 20), (2, 15))))
    following = tmp_path / 'following.jsonl'
    following.write_text('\n'.join(json.dumps(dict(assistant(i), timestamp=at(m))) for i, m in ((11, 10), (12, 9))))
    conn = sqlite3.connect(':memory:')
    nudge._schema(conn)
    conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)',
                 ('active', 'tip', at(25), 0, str(active)))
    conn.commit()
    nudge.resolve(object(), conn)
    assert conn.execute('SELECT status FROM nudge_events').fetchone() == ('pending',)
    class Alive:
        def _session_process_alive(self, sid):
            return True
    nudge.resolve(Alive(), conn)
    assert conn.execute('SELECT status FROM nudge_events').fetchone() == ('pending',)
    class Gone:
        def _session_process_alive(self, sid):
            return False
    nudge.resolve(Gone(), conn)
    assert conn.execute('SELECT status,action_ts FROM nudge_events').fetchone() == ('followed', at(10))
    conn.close()


def test_malformed_message_rows_never_abort_the_batch(tmp_path):
    bad = tmp_path / 'bad.jsonl'
    bad.write_text('\n'.join(json.dumps(r) for r in [
        {'type': 'assistant', 'timestamp': '2026-01-01T00:00:05+00:00', 'uuid': 'm1', 'message': 'not-a-dict'},
        boundary(),
        {'type': 'assistant', 'timestamp': '2026-01-01T00:00:11+00:00', 'uuid': 'm2', 'message': {'id': ['unhashable'], 'usage': {'cache_read_input_tokens': 50}}},
        assistant(13),
    ]))
    good = tmp_path / 'good.jsonl'
    good.write_text(json.dumps(boundary()) + '\n' + json.dumps(assistant(21)))
    conn = sqlite3.connect(':memory:')
    nudge._schema(conn)
    for name, path in (('bad', bad), ('good', good)):
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)',
                     (name, 'tip', '2026-01-01T00:00:00+00:00', 0, str(path)))
    conn.commit()
    nudge.resolve(object(), conn)
    assert conn.execute('SELECT session_uuid,status FROM nudge_events ORDER BY id').fetchall() == [('bad', 'followed'), ('good', 'followed')]
    conn.close()


def test_in_session_action_is_priced_only_from_its_own_transcript(tmp_path):
    acted = tmp_path / 'acted.jsonl'
    acted.write_text('\n'.join(json.dumps(r) for r in [
        dict(assistant(1), timestamp='2026-01-01T00:00:40+00:00'),
        dict(boundary(), timestamp='2026-01-01T00:00:50+00:00'),
    ]))
    pending_a = tmp_path / 'pending_a.jsonl'
    pending_a.write_text(json.dumps(dict(assistant(40), timestamp='2026-01-01T00:05:00+00:00')))
    pending_b = tmp_path / 'pending_b.jsonl'
    pending_b.write_text('\n'.join(json.dumps(r) for r in [
        dict(assistant(41), timestamp='2026-01-01T00:04:30+00:00'),
        dict(assistant(42), timestamp='2026-01-01T00:06:00+00:00'),
    ]))
    nxt = tmp_path / 'next.jsonl'
    nxt.write_text('\n'.join(json.dumps(dict(assistant(i), timestamp=ts)) for i, ts in
                    ((30, '2026-01-01T00:10:00+00:00'), (31, '2026-01-01T00:10:01+00:00'), (32, '2026-01-01T00:10:02+00:00'))))
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    conn.execute('CREATE TABLE compression_events (timestamp TEXT, session_uuid TEXT, session_id TEXT, feature TEXT, verified INTEGER)')
    nudge._schema(conn)
    for sid, stamp, path in (('acted', '2026-01-01T00:00:30+00:00', acted),
                             ('pending_b', '2026-01-01T00:00:00+00:00', pending_b),
                             ('pending_a', '2026-01-01T00:00:00+00:00', pending_a)):
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)',
                     (sid, 'tip', stamp, 1000, str(path)))
    conn.commit()
    class Facade:
        def _is_priced_model(self, model):
            return model == 'sonnet'
        def _get_model_cost(self, model, a, b, cache, d):
            return cache / 1000000
    nudge.resolve(Facade(), conn)
    assert conn.execute("SELECT status,action_ts,savings_event_id FROM nudge_events WHERE session_uuid='acted'").fetchone() == ('followed', '2026-01-01T00:00:50+00:00', None)
    assert conn.execute("SELECT status,action_ts,savings_event_id FROM nudge_events WHERE session_uuid='pending_a'").fetchone() == ('followed', '2026-01-01T00:10:00+00:00', 1)
    assert conn.execute("SELECT status FROM nudge_events WHERE session_uuid='pending_b'").fetchone() == ('ignored',)
    assert conn.execute("SELECT COUNT(*) FROM savings_events WHERE event_type='nudge_followed'").fetchone()[0] == 1
    assert conn.execute('SELECT tokens_saved FROM savings_events').fetchone()[0] == 2700
    conn.close()


def test_compaction_lookup_failure_keeps_event_pending_and_logs(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    nudge._schema(conn)
    credit = tmp_path / 'credit.jsonl'
    credit.write_text('\n'.join(json.dumps(r) for r in [boundary(), assistant(11), assistant(12)]))
    stale = tmp_path / 'stale.jsonl'
    stale.write_text('\n'.join(json.dumps(assistant(i)) for i in range(1, 25)))
    for name, path in (('credit', credit), ('stale', stale)):
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)',
                     (name, 'tip', '2026-01-01T00:00:00+00:00', 1000, str(path)))
    conn.commit()
    class Facade:
        SNAPSHOT_DIR = tmp_path
        def _is_priced_model(self, model):
            return model == 'sonnet'
        def _get_model_cost(self, model, a, b, cache, d):
            return cache / 1000000
    nudge.resolve(Facade(), conn)
    assert conn.execute("SELECT status FROM nudge_events WHERE session_uuid='credit'").fetchone() == ('pending',)
    assert conn.execute("SELECT status FROM nudge_events WHERE session_uuid='stale'").fetchone() == ('ignored',)
    assert conn.execute("SELECT COUNT(*) FROM savings_events WHERE event_type='nudge_followed'").fetchone()[0] == 0
    assert 'OperationalError' in (tmp_path / 'nudge-resolve.log').read_text()
    conn.close()


def test_failed_nudge_record_is_logged_and_retry_is_idempotent(tmp_path, monkeypatch):
    import datetime
    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(nudge, 'datetime', FrozenDateTime)
    class Failing:
        SNAPSHOT_DIR = tmp_path
        def _init_trends_db(self):
            raise RuntimeError('db unavailable')
    nudge.record(Failing(), 's', 'tip', 10, 'sonnet', 'p.jsonl')
    assert 'RuntimeError' in (tmp_path / 'nudge-resolve.log').read_text()
    db = tmp_path / 't.db'
    class Ok:
        SNAPSHOT_DIR = tmp_path
        def _init_trends_db(self):
            return sqlite3.connect(db)
    nudge.record(Ok(), 's', 'tip', 10, 'sonnet', 'p.jsonl')
    nudge.record(Ok(), 's', 'tip', 10, 'sonnet', 'p.jsonl')
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT COUNT(*) FROM nudge_events').fetchone()[0] == 1
    conn.close()


def test_damaged_transcript_does_not_block_the_batch(tmp_path, monkeypatch):
    corrupt = tmp_path / 'corrupt.jsonl'
    corrupt.write_bytes(b'\xff\xfe\x00not-json\n')
    broken = tmp_path / 'broken.jsonl'
    broken.write_text(json.dumps(assistant(1)))
    good = tmp_path / 'good.jsonl'
    good.write_text(json.dumps(boundary()) + '\n' + json.dumps(assistant(11)))
    conn = sqlite3.connect(':memory:')
    nudge._schema(conn)
    for name, path in (('corrupt', corrupt), ('broken', broken), ('good', good)):
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,transcript_path) VALUES(?,?,?,?,?)',
                     (name, 'tip', '2026-01-01T00:00:00+00:00', 0, str(path)))
    conn.commit()
    real_rows = nudge._rows
    def flaky(path):
        if Path(path) == broken:
            raise OSError('unreadable transcript')
        return real_rows(path)
    monkeypatch.setattr(nudge, '_rows', flaky)
    class Facade:
        SNAPSHOT_DIR = tmp_path
    nudge.resolve(Facade(), conn)
    assert conn.execute('SELECT session_uuid,status FROM nudge_events ORDER BY id').fetchall() == [
        ('corrupt', 'pending'), ('broken', 'pending'), ('good', 'followed')]
    assert 'OSError' in (tmp_path / 'nudge-resolve.log').read_text()
    conn.close()


def test_resolver_never_overwrites_an_existing_credit(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER, cost_saved_usd REAL, session_id TEXT, session_uuid TEXT, detail TEXT, model TEXT, unjoinable INTEGER)')
    conn.execute('CREATE TABLE compression_events (timestamp TEXT, session_uuid TEXT, session_id TEXT, feature TEXT, verified INTEGER)')
    nudge._schema(conn)
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text('\n'.join(json.dumps(r) for r in [boundary()] + [assistant(i) for i in range(11, 21)]))
    for tokens, status in ((777, 'followed'), (888, 'pending')):
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail,model,unjoinable) VALUES(?,?,?,?,?,?,?,?,?)",
                     ('2026-01-01T00:00:10+00:00', 'nudge_followed', tokens, 0.5, 's', 's', 'nudge_id=x', None, 0))
        event_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('INSERT INTO nudge_events(session_uuid,kind,ts,context_tokens,model,transcript_path,status,savings_event_id) VALUES(?,?,?,?,?,?,?,?)',
                     ('s', 'tip', '2026-01-01T00:00:00+00:00', 1000, 'sonnet', str(transcript), status, event_id))
    conn.commit()
    class Facade:
        def _is_priced_model(self, model):
            return model == 'sonnet'
        def _get_model_cost(self, model, a, b, cache, d):
            return cache / 1000000
    nudge.resolve(Facade(), conn)
    assert conn.execute('SELECT tokens_saved,cost_saved_usd FROM savings_events ORDER BY id').fetchall() == [(777, 0.5), (888, 0.5)]
    assert conn.execute('SELECT status,savings_event_id IS NOT NULL FROM nudge_events ORDER BY id').fetchall() == [('followed', 1), ('pending', 1)]
    conn.close()


def test_resolver_leaves_callers_open_transaction_untouched(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE caller_work (id INTEGER PRIMARY KEY, note TEXT)')
    conn.execute("INSERT INTO caller_work(note) VALUES('pending')")
    assert conn.in_transaction
    class Facade:
        SNAPSHOT_DIR = tmp_path
    nudge.resolve(Facade(), conn)
    assert conn.in_transaction
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nudge_events'").fetchone() is None
    conn.rollback()
    assert conn.execute('SELECT COUNT(*) FROM caller_work').fetchone()[0] == 0
    assert 'RuntimeError' in (tmp_path / 'nudge-resolve.log').read_text()
    conn.close()
