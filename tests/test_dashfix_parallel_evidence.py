import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import session_effect


def test_parallel_evidence_matches_serial_for_small_transcripts(tmp_path, monkeypatch):
    from _measure import evidence_parallel, transcript_stream
    paths = []
    for number in range(260):
        path = tmp_path / ('%04d.jsonl' % number)
        path.write_text('{"type":"user","timestamp":"2026-01-01T00:00:00Z"}\n')
        paths.append(str(path))
    items = [(path, Path(path).stat()) for path in paths]
    parallel = {item[0]: result for batch in evidence_parallel.batches(items, None, transcript_stream.LARGE_BYTES)
                for item, result in batch}
    monkeypatch.setattr(session_effect, '_PARALLEL_EVIDENCE_MIN', 10000)
    with sqlite3.connect(':memory:') as conn:
        serial = session_effect._evidence(conn, paths, None)
    assert parallel == serial


def test_pool_failure_falls_back_without_losing_evidence(tmp_path, monkeypatch):
    from _measure import evidence_parallel
    path = tmp_path / 'session.jsonl'
    path.write_text('{"type":"user","timestamp":"2026-01-01T00:00:00Z"}\n')
    item = (str(path), path.stat())
    def unavailable(*args, **kwargs):
        raise OSError('process creation unavailable')
    monkeypatch.setattr(evidence_parallel, 'ProcessPoolExecutor', unavailable)
    batches = list(evidence_parallel.batches([item], None, transcript_stream_limit()))
    assert batches[0][0][0][0] == str(path)
    assert batches[0][0][1] == session_effect.scan_transcript(str(path))


def transcript_stream_limit():
    from _measure import transcript_stream
    return transcript_stream.LARGE_BYTES


def test_pool_is_limited_to_the_guarded_cli(monkeypatch):
    from _measure import evidence_parallel
    main = sys.modules['__main__']
    monkeypatch.setattr(main, '__file__', '/unrelated/measure.py', raising=False)
    assert not evidence_parallel.safe_entrypoint()
    actual = Path(evidence_parallel.__file__).resolve().parents[1] / 'measure.py'
    monkeypatch.setattr(main, '__file__', str(actual), raising=False)
    assert evidence_parallel.safe_entrypoint()


def test_parallel_first_calls_match_serial(tmp_path):
    from _measure import evidence_parallel, first_call_context, transcript_stream
    paths = []
    for number in range(30):
        path = tmp_path / ('first-%04d.jsonl' % number)
        path.write_text('{"type":"assistant","timestamp":"2026-01-01T00:00:00Z","message":{"usage":{"input_tokens":30}}}\n')
        paths.append(path)
    items = [(str(path), path.stat()) for path in paths]
    got = [value for batch in evidence_parallel.batches(items, None, transcript_stream.LARGE_BYTES,
                                                        evidence_parallel.first_call) for _, value in batch]
    expected = [(first_call_context.observe(first_call_context.transcript(path), lambda value: round(len(value) / 4)), False)
                for path in paths]
    assert got == expected


def test_mixed_batch_preserves_large_and_small_results(tmp_path):
    from _measure import evidence_parallel
    small, large = tmp_path / 'small.jsonl', tmp_path / 'large.jsonl'
    small.write_text('{"type":"user"}\n')
    large.write_text('{"type":"user","text":"' + 'x' * 200 + '"}\n')
    items = [(str(path), path.stat()) for path in (small, large)]
    result = [value for batch in evidence_parallel.batches(items, None, 100) for _, value in batch]
    assert result == [session_effect.scan_transcript(str(path)) for path in (small, large)]
