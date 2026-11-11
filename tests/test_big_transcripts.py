"""Large transcripts keep their history and resume across collection passes."""
import json
import sys
import tracemalloc
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
from _measure import transcript_stream as stream
from test_counted_cumulative import m


def test_resume_and_append(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(stream, 'PASS_BYTES', 100)
    path = tmp_path / 'session.jsonl'
    expected = []
    for run in range(3):
        with path.open('a') as handle:
            for turn in range(12):
                record = {'type': 'assistant', 'requestId': f'{run}-{turn}',
                          'message': {'usage': {'input_tokens': 100}, 'content': []}}
                expected.append(record)
                handle.write(json.dumps(record) + '\n')
        offsets = []
        for _ in range(100):
            records, info = stream.records(path)
            result = list(records)
            offsets.append(info['indexed_bytes'])
            if not info['incomplete']:
                break
        assert result == expected
        assert offsets == sorted(offsets)
        assert not stream.pending(path)
    assert len(result) == 36


def test_partial_line_and_same_size_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    path = tmp_path / 'session.jsonl'
    path.write_text('{"type":"assistant"')
    records, info = stream.records(path)
    assert list(records) == [] and info['incomplete']
    with path.open('a') as handle:
        handle.write('}\n')
    records, info = stream.records(path)
    assert list(records) == [{'type': 'assistant'}] and not info['incomplete']
    path.write_text('{"type":"something"}\n')
    records, info = stream.records(path)
    assert list(records) == []


def test_budget_and_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    path = tmp_path / 'session.jsonl'
    path.write_text('{"type":"assistant"}\n' * 100)
    records, info = stream.records(path, seconds=0)
    assert list(records) == [] and info['indexed_bytes'] == 0 and info['incomplete']
    records, info = stream.records(path)
    assert len(list(records)) == 100 and not info['incomplete']


def test_over_96mb_matches_small_metrics(m, tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    small = tmp_path / 'small.jsonl'
    big = tmp_path / 'claude/projects/project/big.jsonl'
    big.parent.mkdir(parents=True)
    records = [{'type': 'assistant', 'requestId': f'request-{n}',
                'timestamp': f'2026-11-01T10:0{n}:00Z', 'message': {
                    'id': f'msg-{n}', 'model': 'claude-opus-5-5',
                    'usage': {'input_tokens': 100, 'output_tokens': 50,
                              'cache_read_input_tokens': 2000}, 'content': []}}
               for n in range(3)]
    small.write_text(''.join(json.dumps(r) + '\n' for r in records))
    padding = json.dumps({'type': 'progress', 'data': 'x' * (1024 * 1024)}) + '\n'
    with big.open('w') as handle:
        handle.write(json.dumps(records[0]) + '\n')
        for _ in range(100):
            handle.write(padding)
        handle.write(''.join(json.dumps(r) + '\n' for r in records[1:]))
    assert big.stat().st_size > 96 * 1024 * 1024
    from large_transcripts import count
    assert count(tmp_path / 'claude')['unscanned_large_sessions'] == 1
    del padding
    tracemalloc.start()
    offsets = []
    for _ in range(30):
        started = time.monotonic()
        values, info = stream.records(big, seconds=0.15)
        list(values)
        assert time.monotonic() - started < 1.0
        offsets.append(info['indexed_bytes'])
        if not info['incomplete']:
            break
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert not info['incomplete']
    assert len(offsets) > 1 and offsets == sorted(offsets)
    assert peak < 32 * 1024 * 1024
    assert m.parse_session_turns(big) == m.parse_session_turns(small)
    actual, expected = m._parse_session_jsonl(big), m._parse_session_jsonl(small)
    for key in ('total_input_tokens', 'total_output_tokens', 'api_calls', 'model_usage_breakdown'):
        assert actual[key] == expected[key]
    assert count(tmp_path / 'claude')['unscanned_large_sessions'] == 0
    assert not stream.pending(big)
    assert [json.loads(line) for line in m._iter_tail_lines(big, 5)] == records


def test_three_growing_collects_credit_each_append(m, tmp_path, monkeypatch):
    import sqlite3
    from datetime import timedelta
    from test_transcript_backfill import _session, _register, _rows, CLOUD
    from test_counted_cumulative import _db
    from _measure import transcript_backfill as backfill
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, 'CLAUDE_DIR', tmp_path / 'claude')
    monkeypatch.setattr(stream, 'LARGE_BYTES', 0)
    path, start = _session(tmp_path, CLOUD)
    _register(db, CLOUD, path)
    with sqlite3.connect(db) as conn:
        for run in range(3):
            if run:
                extra, _ = _session(tmp_path, CLOUD + str(run), start=start + timedelta(minutes=run * 10))
                text = extra.read_text().replace('toolu_', f'toolu_run{run}_').replace('"requestId": "r', f'"requestId": "run{run}-r')
                with path.open('a') as handle:
                    handle.write(text)
            stats = backfill.run(conn, m, '2000-01-01')
            assert stats['rows_written'] == 2
            assert backfill.run(conn, m, '2000-01-01')['rows_written'] == 0
            ce, se = _rows(db)
            assert len(ce) == len(se) == run + 1
            m._counted_backfill_all(conn, quiet=True)
            summary = m._counted_cumulative_summary(days=30)
            assert summary['events'] >= 2 * (run + 1)


def test_codex_append_during_partial_index_preserves_offset(tmp_path, monkeypatch):
    import codex_log_index as index
    from test_codex_full_features import log_records
    monkeypatch.setattr(index, 'resolve_snapshot_dir', lambda: tmp_path / 'index')
    monkeypatch.setattr(index, 'PASS_BYTES', 200)
    path = tmp_path / 'rollout.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in log_records()))
    offsets = []
    for run in range(3):
        with path.open('a') as handle:
            handle.write(json.dumps(log_records(200 + run * 100)[-1]) + '\n')
        values, info = index.records(path)
        list(values)
        offsets.append(info['indexed_bytes'])
    assert offsets[0] < offsets[1] < offsets[2]
    for _ in range(30):
        values, info = index.records(path)
        result = list(values)
        if not info['incomplete']:
            break
    assert not info['incomplete']
    assert result[-1]['payload']['info']['total_token_usage']['input_tokens'] == 400


def test_collect_refreshes_one_growing_file_three_times(m, tmp_path, monkeypatch):
    import sqlite3
    from test_transcript_backfill import _session, CLOUD
    monkeypatch.setattr(m, 'TRENDS_DB', tmp_path / 'trends.db')
    monkeypatch.setattr(m, 'CLAUDE_DIR', tmp_path / 'claude')
    monkeypatch.setattr(stream, 'LARGE_BYTES', 0)
    path, start = _session(tmp_path, CLOUD)
    original = path.read_text()
    for run in range(3):
        if run:
            with path.open('a') as handle:
                handle.write(original.replace('toolu_', f'toolu_{run}_').replace('"requestId": "r', f'"requestId": "{run}-r'))
        m.collect_sessions(days=30, quiet=True)
        with sqlite3.connect(m.TRENDS_DB) as conn:
            row = conn.execute('SELECT count(*),sum(input_tokens),sum(api_calls) FROM session_log').fetchone()
            assert row == (1, 350714 * (run + 1), 7 * (run + 1))
            rows = conn.execute('SELECT count(*) FROM compression_events WHERE origin="transcript_backfill"').fetchone()[0]
            assert rows == run + 1
        before = m._counted_cumulative_summary(days=30)
        m.collect_sessions(days=30, quiet=True)
        after = m._counted_cumulative_summary(days=30)
        assert after['reread_tokens'] == before['reread_tokens']
        assert after['events'] == before['events']


def test_unparseable_record_stays_disclosed(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(stream, 'LINE_BYTES', 32)
    path = tmp_path / 'claude/projects/p/large.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"user","message":"' + 'x' * 1000 + '"}\n')
    values, info = stream.records(path)
    assert list(values) == [] and info['incomplete'] and info['skipped_records'] == 1
    from large_transcripts import count
    assert count(tmp_path / 'claude', limit=10)['unscanned_large_sessions'] == 1


def test_locked_index_is_disclosed_and_retried(m, tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(stream, 'LARGE_BYTES', 0)
    path = tmp_path / 'claude/projects/p/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"assistant","message":{"usage":{"input_tokens":10}}}\n')
    values, _ = stream.records(path)
    list(values)
    with sqlite3.connect(tmp_path / 'data/transcript-index.db') as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with path.open('a') as handle:
            handle.write('{"type":"assistant","message":{"usage":{"input_tokens":20}}}\n')
        assert m._parse_session_jsonl(path) is None
        from large_transcripts import count
        assert count(tmp_path / 'claude', limit=10)['unscanned_large_sessions'] == 1
    assert m._parse_session_jsonl(path)['total_input_tokens'] == 30


def test_codex_coverage_uses_its_own_index(tmp_path, monkeypatch):
    import codex_log_index as index
    import codex_session
    import runtime_env
    from large_transcripts import count
    from test_codex_full_features import log_records
    monkeypatch.setattr(runtime_env, 'detect_runtime', lambda: 'codex')
    monkeypatch.setattr(codex_session, 'session_roots', lambda: (tmp_path / 'sessions',))
    monkeypatch.setattr(index, 'resolve_snapshot_dir', lambda: tmp_path / 'index')
    path = tmp_path / 'sessions/session.jsonl'
    path.parent.mkdir()
    path.write_text(''.join(json.dumps(r) + '\n' for r in log_records()))
    assert count(tmp_path, limit=10)['unscanned_large_sessions'] == 1
    values, info = index.records(path)
    list(values)
    assert not info['incomplete']
    assert count(tmp_path, limit=10)['unscanned_large_sessions'] == 0
