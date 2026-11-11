import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import first_call_context as context


def test_cache_expires_after_scan_finishes(monkeypatch):
    clock = [59.0]
    calls = []
    monkeypatch.setattr(context.time, 'monotonic', lambda: clock[0])

    def slow_scan(db, data):
        calls.append((db, data))
        clock[0] += 90
        return {'months': [], 'trim_dates': [], 'missing': 0}

    monkeypatch.setattr(context, '_collect_uncached', slow_scan)
    context._cache.clear()
    first = context.collect('test-db', 'test-data')
    assert context.collect('test-db', 'test-data') == first
    assert len(calls) == 1
    clock[0] += 61
    context.collect('test-db', 'test-data')
    assert len(calls) == 2


def test_transcript_ignores_irrelevant_records_but_keeps_escaped_keys(tmp_path):
    path = tmp_path / 'session.jsonl'
    path.write_text('{"type":"progress"}\n{"\\u0074ype":"assistant","message":{"usage":{"input_tokens":3}},"timestamp":"2026-01-01"}\n')
    rows = list(context.transcript(path))
    assert len(rows) == 1
    assert context.observe(rows, len)['total'] == 3
