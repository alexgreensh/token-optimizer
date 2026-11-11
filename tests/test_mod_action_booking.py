"""Desktop action accounting uses real usage and idempotent ledger receipts."""
import importlib
import json
from contextlib import nullcontext
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def facade(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'claude').mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude'))
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'snap'))
    monkeypatch.setenv('TOKEN_OPTIMIZER_RUNTIME', 'claude')
    import measure
    measure = importlib.reload(measure)
    monkeypatch.setattr(measure, '_keepwarm_lock', lambda: nullcontext(True))
    return measure


def test_manual_warm_books_cost_once_and_arms_resume(facade, tmp_path):
    from _measure import mod_action_booking
    transcript = tmp_path / 'synthetic-session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z', 'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000, 'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n')
    st = transcript.stat()
    event = {'action': 'warm', 'action_id': 'attempt-123', 'session_id': 'synthetic-session',
             'timestamp': 1767229000, 'transcript_path': str(transcript), 'byte_size': st.st_size,
             'transcript_mtime': st.st_mtime, 'usage': {'input_tokens': 4, 'output_tokens': 2,
             'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100}}
    assert mod_action_booking.record(facade, event)['ok']
    assert mod_action_booking.record(facade, event)['ok']
    rows = facade._keepwarm_load_ledger_rows()
    assert len(rows) == 1 and rows[0]['action_id'] == 'attempt-123'
    assert rows[0]['source'] == 'claude-code-mod'
    assert rows[0]['cost_usd'] == pytest.approx(0.006342)
    assert facade.keepwarm_spend_summary(days=None, now=event['timestamp'] + 1, rows=rows)['net_usd'] == pytest.approx(-0.006342)
    from _measure import keepwarm_economics
    pool = keepwarm_economics.headline_pool(facade, '2025-01-01T00:00:00+00:00')
    assert pool['manual_pings'] == 1 and pool['cost_saved_usd'] == pytest.approx(-0.006342)
    arms = facade.load_keepwarm_records()
    assert len(arms) == 1 and arms[0]['pings_fired'] == [event['timestamp']]
    assert arms[0]['pause_key'] == rows[0]['pause_key']


def test_manual_warm_rejects_unknown_model_without_false_zero_cost(facade, tmp_path):
    from _measure import mod_action_booking
    path = tmp_path / 'synthetic-session.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'unknown-model', 'usage': {'input_tokens': 1}}}) + '\n')
    st = path.stat()
    result = mod_action_booking.record(facade, {'action': 'warm', 'action_id': 'one', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(path), 'byte_size': st.st_size, 'transcript_mtime': st.st_mtime,
        'usage': {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}})
    assert not result['ok']
    assert facade._keepwarm_load_ledger_rows() == []


def test_manual_warm_credits_only_after_a_proven_warm_return(facade, tmp_path):
    from datetime import datetime, timezone
    from _measure import mod_action_booking
    transcript = tmp_path / 'synthetic-session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000,
        'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n')
    st = transcript.stat()
    event = {'action': 'warm', 'action_id': 'proof-1', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(transcript), 'byte_size': st.st_size,
        'transcript_mtime': st.st_mtime, 'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100}}
    assert mod_action_booking.record(facade, event)['ok']
    arm = facade.load_keepwarm_records()[0]
    assert facade.keepwarm_detect_realized(now=1767230010, records=[arm], parse_fn=lambda p: [])['realized'] == 0
    with transcript.open('a') as stream:
        stream.write(json.dumps({'type': 'user', 'message': 'synthetic resume'}) + '\n')
    stamp = datetime.fromtimestamp(1767230000, timezone.utc).isoformat()
    parse = lambda p: [{'timestamp': stamp, 'cache_read': 20000, 'cache_creation': 0}]
    outcome = facade.keepwarm_detect_realized(now=1767230010, records=[arm], parse_fn=parse)
    assert outcome['realized'] == 1
    assert facade.keepwarm_detect_realized(now=1767230011, records=[arm], parse_fn=parse)['realized'] == 0


def test_cli_books_receipt_and_deduplicates(tmp_path):
    import os
    import subprocess
    home = tmp_path / 'home'
    home.mkdir()
    snap = tmp_path / 'snap'
    path = tmp_path / 'synthetic-session.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000,
        'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n')
    st = path.stat()
    payload = {'action': 'warm', 'action_id': 'cli-1', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(path), 'byte_size': st.st_size,
        'transcript_mtime': st.st_mtime, 'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100}}
    env = dict(os.environ, HOME=str(home), CLAUDE_CONFIG_DIR=str(home), TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(snap),
               TOKEN_OPTIMIZER_RUNTIME='claude', PYTHONDONTWRITEBYTECODE='1')
    cmd = [sys.executable, str(SCRIPTS / 'measure.py'), 'mod-action']
    for duplicate in (False, True):
        result = subprocess.run(cmd, input=json.dumps(payload), text=True, capture_output=True, env=env)
        assert result.returncode == 0, (result.stdout, result.stderr)
        response = json.loads(result.stdout)
        assert response['ok'] and response['duplicate'] is duplicate
    rows = [json.loads(line) for line in (snap / 'keepwarm_ledger.jsonl').read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['action_id'] == 'cli-1'


def test_concurrent_cli_retries_book_one_attempt(tmp_path):
    import os
    import subprocess
    home = tmp_path / 'home'
    home.mkdir()
    snap = tmp_path / 'snap'
    path = tmp_path / 'synthetic-session.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000,
        'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n')
    st = path.stat()
    payload = {'action': 'warm', 'action_id': 'race-1', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(path), 'byte_size': st.st_size,
        'transcript_mtime': st.st_mtime, 'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100}}
    env = dict(os.environ, HOME=str(home), CLAUDE_CONFIG_DIR=str(home), TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(snap),
               TOKEN_OPTIMIZER_RUNTIME='claude', PYTHONDONTWRITEBYTECODE='1')
    cmd = [sys.executable, str(SCRIPTS / 'measure.py'), 'mod-action']
    processes = [subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env) for _ in range(2)]
    results = [p.communicate(json.dumps(payload), timeout=15) for p in processes]
    assert all(p.returncode == 0 for p in processes), results
    assert sorted(json.loads(stdout)['duplicate'] for stdout, _ in results) == [False, True]
    rows = [json.loads(line) for line in (snap / 'keepwarm_ledger.jsonl').read_text().splitlines()]
    assert len(rows) == 1


def test_two_warmups_in_one_pause_cannot_double_credit(facade, tmp_path):
    from datetime import datetime, timezone
    from _measure import mod_action_booking
    transcript = tmp_path / 'synthetic-session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000,
        'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n')
    st = transcript.stat()
    event = {'action': 'warm', 'action_id': 'first', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(transcript), 'byte_size': st.st_size,
        'transcript_mtime': st.st_mtime, 'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100}}
    first = mod_action_booking.record(facade, event)
    second = mod_action_booking.record(facade, dict(event, action_id='second', timestamp=1767229010))
    assert first['ok'] and second['ok'] and first['pause_key'] == second['pause_key']
    assert len(facade._keepwarm_load_ledger_rows()) == 2
    arms = facade.load_keepwarm_records()
    assert len(arms) == 1
    with transcript.open('a') as stream:
        stream.write(json.dumps({'type': 'user', 'message': 'synthetic return'}) + '\n')
    stamp = datetime.fromtimestamp(1767230000, timezone.utc).isoformat()
    result = facade.keepwarm_detect_realized(now=1767230010, records=arms,
        parse_fn=lambda p: [{'timestamp': stamp, 'cache_read': 20000, 'cache_creation': 0}])
    assert result['realized'] == 1
    assert facade.keepwarm_detect_realized(now=1767230011, records=arms,
        parse_fn=lambda p: [{'timestamp': stamp, 'cache_read': 20000, 'cache_creation': 0}])['realized'] == 0


def test_warm_reservation_books_a_tail_and_accepts_later_transcript_growth(facade, tmp_path):
    from _measure import mod_action_booking
    transcript = tmp_path / 'synthetic-session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000,
        'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n')
    st = transcript.stat()
    event = {'action': 'warm-prepare', 'action_id': 'reserved-1', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(transcript), 'byte_size': st.st_size,
        'transcript_mtime': st.st_mtime}
    assert mod_action_booking.record(facade, event)['ok']
    assert not mod_action_booking.record(facade, event)['ok']
    rows = facade._keepwarm_load_ledger_rows()
    assert len(rows) == 1 and rows[0]['status'] == 'firing' and rows[0]['est_cost_usd'] > 0
    assert facade.keepwarm_spend_summary(days=None, now=1767229001, rows=rows)['spend_usd'] > 0
    with transcript.open('a') as stream:
        stream.write(json.dumps({'type': 'user', 'message': 'new turn'}) + '\n')
    event.update(action='warm', timestamp=1767229001, usage={'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100})
    assert mod_action_booking.record(facade, event)['ok']
    rows = facade._keepwarm_load_ledger_rows()
    assert [r['status'] for r in rows] == ['firing', 'ok']
    summary = facade.keepwarm_spend_summary(days=None, now=1767229002, rows=rows)
    assert summary['pings'] == 1 and summary['spend_usd'] == pytest.approx(0.006342)
    assert len(facade.load_keepwarm_records()) == 1


def test_pending_reservations_match_outcomes_by_attempt_id(facade):
    rows = [
        {'session_id': 'same', 'action_id': 'a', 'ts': 100, 'status': 'firing', 'est_cost_usd': 1.0},
        {'session_id': 'same', 'action_id': 'b', 'ts': 101, 'status': 'firing', 'est_cost_usd': 2.0},
        {'session_id': 'same', 'action_id': 'b', 'ts': 102, 'status': 'ok', 'cost_usd': 0.5},
    ]
    summary = facade.keepwarm_spend_summary(days=None, now=103, rows=rows)
    assert summary['pings'] == 2 and summary['spend_usd'] == pytest.approx(1.5)


def test_manual_count_includes_unresolved_reservations_only_once():
    from _measure.keepwarm_economics import manual_count
    rows = [{'source': 'claude-code-mod', 'action': 'warm', 'action_id': 'a', 'ts': 100, 'status': 'firing'},
            {'source': 'claude-code-mod', 'action': 'warm', 'action_id': 'b', 'ts': 101, 'status': 'firing'},
            {'source': 'claude-code-mod', 'action': 'warm', 'action_id': 'b', 'ts': 102, 'status': 'ok'}]
    assert manual_count(rows, 99) == 2


def test_never_sent_reservation_is_cancelled_without_cost(facade, tmp_path):
    from _measure import mod_action_booking
    path = tmp_path / 'synthetic-session.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000}}}) + '\n')
    st = path.stat()
    event = {'action': 'warm-prepare', 'action_id': 'unsent', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(path), 'byte_size': st.st_size, 'transcript_mtime': st.st_mtime}
    assert mod_action_booking.record(facade, event)['ok']
    assert mod_action_booking.record(facade, dict(event, action='warm-cancel', timestamp=1767229001))['ok']
    rows = facade._keepwarm_load_ledger_rows()
    summary = facade.keepwarm_spend_summary(days=None, now=1767229002, rows=rows)
    assert summary['pings'] == 0 and summary['spend_usd'] == 0
    from _measure.keepwarm_economics import manual_count
    assert manual_count(rows, 0) == 0


def test_unanswered_fork_books_usage_without_resume_credit(facade, tmp_path):
    from _measure import mod_action_booking
    path = tmp_path / 'synthetic-session.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000}}}) + '\n')
    st = path.stat()
    event = {'action': 'warm-prepare', 'action_id': 'unanswered', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(path), 'byte_size': st.st_size, 'transcript_mtime': st.st_mtime}
    assert mod_action_booking.record(facade, event)['ok']
    event.update(action='warm', answered=False, timestamp=1767229001, usage={'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100})
    assert mod_action_booking.record(facade, event)['ok']
    assert facade.keepwarm_spend_summary(days=None, now=1767229002)['spend_usd'] > 0
    assert facade.load_keepwarm_records() == []


def test_manual_write_cost_uses_disclosed_conservative_rate(facade, tmp_path):
    from _measure import mod_action_booking
    path = tmp_path / 'synthetic-session.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000,
        'cache_creation': {'ephemeral_5m_input_tokens': 100}}}}) + '\n')
    st = path.stat()
    event = {'action': 'warm', 'action_id': 'five-minute', 'session_id': 'synthetic-session',
        'timestamp': 1767229000, 'transcript_path': str(path), 'byte_size': st.st_size,
        'transcript_mtime': st.st_mtime, 'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 19000, 'cache_creation_input_tokens': 100}}
    assert mod_action_booking.record(facade, event)['ok']
    row = facade._keepwarm_load_ledger_rows()[0]
    assert row['cost_basis'] == 'conservative-1h-write'
    assert row['cost_usd'] == pytest.approx(0.006342)
