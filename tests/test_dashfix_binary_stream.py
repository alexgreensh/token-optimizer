import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import transcript_stream


def test_binary_stream_matches_text_projection_with_invalid_utf8(tmp_path):
    path = tmp_path / 'session.jsonl'
    path.write_bytes(b'{"text":"hello"}\n{"text":"\xff"}\n')
    with transcript_stream.open_transcript(path) as lines:
        expected = [line.encode('utf-8') for line in lines]
    assert list(transcript_stream.binary_lines(path)) == expected


def test_binary_stream_still_skips_oversize_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript_stream, 'LINE_BYTES', 8)
    path = tmp_path / 'session.jsonl'
    path.write_bytes(b'0123456789\nsmall\n')
    assert list(transcript_stream.binary_lines(path)) == [b'small\n']


def test_large_binary_stream_preserves_indexed_records(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(transcript_stream, 'LARGE_BYTES', 1)
    monkeypatch.setattr(transcript_stream, 'resolve_snapshot_dir', lambda: tmp_path / 'index')
    path = tmp_path / 'large.jsonl'
    records = [{'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
                'message': {'id': 'a', 'content': 'שלום'}}]
    path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records))
    assert [json.loads(line) for line in transcript_stream.binary_lines(path)] == records
    def no_decode(*args, **kwargs):
        raise AssertionError('indexed records should not be decoded and re-encoded')
    monkeypatch.setattr(transcript_stream.json, 'loads', no_decode)
    assert len(list(transcript_stream.binary_lines(path))) == 1


def test_large_index_scanner_matches_text_projection(tmp_path, monkeypatch):
    import json
    from _measure import session_effect
    monkeypatch.setattr(transcript_stream, 'LARGE_BYTES', 1)
    monkeypatch.setattr(transcript_stream, 'resolve_snapshot_dir', lambda: tmp_path / 'index')
    path = tmp_path / 'session.jsonl'
    events = [
        {'type': 'attachment', 'timestamp': '2026-01-01T00:00:00Z',
         'attachment': {'type': 'hook_additional_context', 'content': '[Token Optimizer] hello'}},
        {'type': 'assistant', 'requestId': 'a'},
        {'type': 'assistant', 'requestId': 'b'},
    ]
    path.write_text(''.join(json.dumps(event) + '\n' for event in events))
    actual = session_effect.scan_transcript(path)
    def projected(source):
        with transcript_stream.open_transcript(source) as lines:
            yield from (line.encode('utf-8') for line in lines)
    monkeypatch.setattr(transcript_stream, 'binary_lines', projected)
    assert session_effect.scan_transcript(path) == actual
    assert actual['injections'] == 1 and actual['days']['2026-01-01'][1] > 0
