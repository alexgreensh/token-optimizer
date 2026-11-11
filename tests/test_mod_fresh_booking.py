"""Fresh recommendations stay estimated and are idempotent per old session."""
import importlib
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def case(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home))
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'snap'))
    monkeypatch.setenv('TOKEN_OPTIMIZER_RUNTIME', 'claude')
    sys.modules.pop('measure', None)
    facade = importlib.import_module('measure')
    sid = '11111111-2222-4333-8444-555555555555'
    path = tmp_path / (sid + '.jsonl')
    lines = [{'type': 'assistant', 'timestamp': f'2026-01-01T00:00:0{i}Z', 'requestId': f'req-{i}',
              'message': {'model': 'claude-sonnet-4-5', 'usage': {'input_tokens': 1,
              'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 0}}} for i in range(1, 4)]
    path.write_text(''.join(json.dumps(line) + '\n' for line in lines))
    conn = facade._init_trends_db()
    try:
        for i in range(10):
            conn.execute('INSERT INTO session_log(jsonl_path,date,session_uuid,api_calls,is_sidechain) VALUES(?,?,?,?,?)',
                (f'/synthetic/{i}', '2025-12-01', f'other-{i}', 30, 0))
        conn.commit()
    finally:
        conn.close()
    event = {'action': 'fresh', 'action_id': 'fresh-1', 'session_id': sid, 'timestamp': 1767225610,
             'transcript_path': str(path), 'before_tokens': 20000, 'handoff_tokens_estimate': 1000}
    return facade, event


def test_fresh_receipt_is_estimated_capped_and_deduplicated(case):
    from _measure import mod_fresh_booking
    facade, event = case
    first = mod_fresh_booking.record(facade, event)
    retry = mod_fresh_booking.record(facade, dict(event, action_id='fresh-2'))
    assert first['ok'] and retry['ok'] and retry['duplicate']
    conn = facade._init_trends_db()
    try:
        rows = conn.execute('SELECT dropped_tokens, estimated_turns, estimated_usd, cohort_size FROM mod_fresh_receipts').fetchall()
        measured = conn.execute("SELECT count(*) FROM savings_events WHERE event_type = 'mod_fresh'").fetchone()[0]
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0][0] == 19000 and rows[0][1] == 10 and rows[0][3] == 10
    assert rows[0][2] == pytest.approx(0.057)
    assert measured == 0


def test_fresh_with_thin_history_has_no_invented_dollar_value(case):
    from _measure import mod_fresh_booking
    facade, event = case
    conn = facade._init_trends_db()
    try:
        conn.execute('UPDATE session_log SET is_sidechain=1')
        conn.commit()
    finally:
        conn.close()
    assert mod_fresh_booking.record(facade, event)['ok']
    conn = facade._init_trends_db()
    try:
        estimate = conn.execute('SELECT estimated_usd FROM mod_fresh_receipts').fetchone()[0]
    finally:
        conn.close()
    assert estimate is None


def test_fresh_estimate_is_not_added_on_top_of_handover(case):
    from _measure import mod_fresh_booking
    facade, event = case
    assert mod_fresh_booking.record(facade, event)['ok']
    pool = mod_fresh_booking.summary(facade, '2025-01-01')
    assert pool['events'] == 1 and pool['evidence'] == 'estimated'
    alone = facade._action_savings_components({'mod_fresh_estimated': pool})
    overlap = facade._action_savings_components({'mod_fresh_estimated': pool, 'handover_rerun': {'cost_saved_usd': 1}})
    assert alone['estimates_usd']['mod_fresh_estimated'] == pytest.approx(0.057)
    assert overlap['estimates_usd']['mod_fresh_estimated'] == 0


def test_generic_receipt_surfaces_only_in_estimated_pool(case):
    from _measure import mod_action_booking
    facade, event = case
    assert mod_action_booking.record(facade, event)['ok']
    summary = facade._get_savings_summary(days=9999)
    assert summary['total_cost_usd'] == 0
    assert summary['mod_fresh_estimated']['events'] == 1
    assert summary['mod_fresh_estimated']['cost_saved_usd'] == pytest.approx(0.057)


def test_fresh_without_transcript_is_counted_but_unpriced(case):
    from _measure import mod_fresh_booking
    facade, event = case
    event['transcript_path'] = '/nonexistent/' + event['session_id'] + '.jsonl'
    event['before_tokens'] = None
    assert mod_fresh_booking.record(facade, event)['ok']
    summary = mod_fresh_booking.summary(facade, '2025-01-01')
    assert summary['events'] == 1 and summary['unpriced_events'] == 1 and summary['cost_saved_usd'] == 0


def test_generic_cli_records_fresh_in_estimated_table(case):
    import os
    import subprocess
    facade, event = case
    env = dict(os.environ, TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(facade.SNAPSHOT_DIR),
               TOKEN_OPTIMIZER_RUNTIME='claude', PYTHONDONTWRITEBYTECODE='1')
    cmd = [sys.executable, str(SCRIPTS / 'measure.py'), 'mod-action']
    result = subprocess.run(cmd, input=json.dumps(event), text=True, capture_output=True, env=env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)['estimated_usd'] == pytest.approx(0.057)
    conn = facade._init_trends_db()
    try:
        assert conn.execute('SELECT count(*) FROM mod_fresh_receipts').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM savings_events WHERE event_type='mod_fresh'").fetchone()[0] == 0
    finally:
        conn.close()


def test_fresh_without_handoff_size_stays_unpriced(case):
    from _measure import mod_fresh_booking
    facade, event = case
    event.pop('handoff_tokens_estimate')
    assert mod_fresh_booking.record(facade, event)['ok']
    summary = mod_fresh_booking.summary(facade, '2025-01-01')
    assert summary['events'] == 1 and summary['unpriced_events'] == 1


def test_merged_fresh_pool_remains_estimated(case):
    from _measure import mod_action_booking
    facade, event = case
    assert mod_action_booking.record(facade, event)['ok']
    merged = facade._get_merged_savings(days=9999)
    assert merged['mod_fresh_estimated']['events'] == 1
    assert merged['mod_fresh_estimated']['evidence'] == 'estimated'
    assert 'mod_fresh' not in merged['by_category']
    parts = facade._action_savings_components(merged)
    assert parts['estimates_usd']['mod_fresh_estimated'] == pytest.approx(0.057)
