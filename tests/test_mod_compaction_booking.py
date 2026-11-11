"""Synthetic recommendation-followed compactions are deduplicated and priced."""
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
    import measure
    facade = importlib.reload(measure)
    sid = '11111111-2222-4333-8444-555555555555'
    path = tmp_path / (sid + '.jsonl')
    boundary = {'type': 'system', 'subtype': 'compact_boundary', 'sessionId': sid, 'uuid': 'boundary-1',
                'timestamp': '2026-01-01T00:00:00Z', 'compactMetadata': {'trigger': 'manual'}}
    path.write_text(json.dumps(boundary) + '\n')
    event = {'action': 'clean', 'action_id': 'act-1', 'compaction_id': 'boundary-1', 'session_id': sid,
        'timestamp': 1767225600, 'transcript_path': str(path), 'model': 'claude-sonnet-4-5',
        'before_tokens': 20000, 'after_tokens': 5000, 'ttl_kind': '1h', 'cost_usd': 0.006342,
        'usage': {'input_tokens': 4, 'output_tokens': 2, 'cache_read_input_tokens': 19000,
                  'cache_creation_input_tokens': 100}}
    return facade, path, event


def test_manual_compaction_receipt_and_cost_are_atomic_and_deduplicated(case):
    from _measure import mod_compaction_booking
    facade, path, event = case
    first = mod_compaction_booking.record(facade, event)
    retry = mod_compaction_booking.record(facade, dict(event, action_id='another-id'))
    assert first['ok'] and retry['ok'] and retry['duplicate']
    conn = facade._init_trends_db()
    try:
        rows = conn.execute("SELECT feature, original_tokens, compressed_tokens, verified FROM compression_events WHERE feature = 'mod_clean'").fetchall()
        costs = conn.execute("SELECT cost_saved_usd FROM savings_events WHERE event_type = 'mod_compaction_cost'").fetchall()
        receipts = conn.execute('SELECT action_id, compaction_id FROM mod_action_receipts').fetchall()
    finally:
        conn.close()
    assert rows == [('mod_clean', 20000, 5000, 1)]
    assert len(costs) == 1 and costs[0][0] < 0
    assert len(receipts) == 1


def test_auto_compaction_never_books_a_mod_action(case):
    from _measure import mod_compaction_booking
    facade, path, event = case
    boundary = json.loads(path.read_text())
    boundary['compactMetadata']['trigger'] = 'auto'
    path.write_text(json.dumps(boundary) + '\n')
    assert not mod_compaction_booking.record(facade, event)['ok']


def test_missing_compaction_usage_is_not_fabricated(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    assert not mod_compaction_booking.record(facade, dict(event, usage=None))['ok']


def test_existing_compaction_credit_is_not_duplicated(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    conn = facade._init_trends_db()
    try:
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,detail) "
                     "VALUES(?,?,?,?,?,?,?)", ('2026-01-01', 'existing_compaction', 1, 0.1, event['session_id'],
                     event['session_id'], 'compaction_id=boundary-1'))
        conn.commit()
    finally:
        conn.close()
    result = mod_compaction_booking.record(facade, event)
    assert not result['ok'] and result['reason'] == 'existing-compaction-credit'


def test_clean_first_is_tagged_separately(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    assert mod_compaction_booking.record(facade, dict(event, action='clean-first'))['ok']
    conn = facade._init_trends_db()
    try:
        assert conn.execute("SELECT feature FROM compression_events").fetchall() == [('mod_clean_first',)]
    finally:
        conn.close()


def test_clean_scoring_counts_reads_and_clean_first_adds_one_write(case):
    from datetime import datetime, timedelta
    from _measure import mod_compaction_booking
    facade, _, _ = case
    start = datetime(2026, 10, 1)
    turns = [(start + timedelta(seconds=1), 'claude-sonnet-4-5', 20000, (10000, 10000, 0, 10000)),
             (start + timedelta(seconds=2), 'claude-sonnet-4-5', 20000, (0, 0, 0, 20000))]
    tier = facade.PRICING_TIERS[facade._load_pricing_tier()]
    base = facade._counted_score_detail(turns, [], start, 15000, tier)
    clean = mod_compaction_booking.adjust_score(facade, 'clean', '1h', turns, start, 15000, tier, base)
    first = mod_compaction_booking.adjust_score(facade, 'clean-first', '1h', turns, start, 15000, tier, base)
    assert clean['reread_tokens'] == 30000 and clean['reread_usd'] == pytest.approx(0.009)
    assert first['reread_tokens'] == 30000 and first['reread_usd'] == pytest.approx(0.099)
    assert clean['oneshot_usd'] == 0 and first['oneshot_usd'] == 0


def test_clean_first_never_credits_an_unobserved_cache_write(case):
    from datetime import datetime, timedelta
    from _measure import mod_compaction_booking
    facade, _, _ = case
    start = datetime(2026, 10, 1)
    turns = [(start + timedelta(seconds=1), 'claude-sonnet-4-5', 20000, (0, 0, 0, 20000))]
    tier = facade.PRICING_TIERS[facade._load_pricing_tier()]
    base = facade._counted_score_detail(turns, [], start, 15000, tier)
    score = mod_compaction_booking.adjust_score(facade, 'clean-first', '1h', turns, start, 15000, tier, base)
    assert score['reread_usd'] == pytest.approx(0.0045)


def test_clean_enters_existing_counted_stream_without_initial_input_credit(case, monkeypatch):
    from _measure import mod_compaction_booking
    facade, path, event = case
    monkeypatch.setattr(facade, '_COUNTED_LEDGER_ERA_START', '2025-01-01')
    assert mod_compaction_booking.record(facade, event)['ok']
    turns = []
    for second in (1, 2):
        turns.append({'type': 'assistant', 'timestamp': f'2026-01-01T00:00:0{second}Z',
            'requestId': f'req-{second}', 'message': {'model': event['model'], 'usage': {
                'input_tokens': 2, 'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 0}}})
    with path.open('a') as stream:
        stream.write(''.join(json.dumps(turn) + '\n' for turn in turns))
    conn = facade._init_trends_db()
    try:
        conn.execute('INSERT INTO session_log(jsonl_path,date,session_uuid) VALUES(?,?,?)',
                     (str(path), '2026-01-01', event['session_id']))
        conn.commit()
        result = facade._update_counted_cumulative(conn, max_sessions=100, quiet=True)
        rows = conn.execute('SELECT oneshot_usd,reread_tokens,reread_usd FROM counted_reread').fetchall()
    finally:
        conn.close()
    assert result['rows_written'] == 1
    assert len(rows) == 1 and rows[0][0] == 0 and rows[0][1] == 30000
    assert rows[0][2] == pytest.approx(0.009)
    summary = facade._get_compression_summary(days=9999)
    assert summary['by_feature']['mod_clean']['cost_saved_usd'] == 0


def test_clean_first_counts_one_observed_rebuild_and_stops_at_next_boundary(case, monkeypatch):
    from _measure import mod_compaction_booking
    facade, path, event = case
    monkeypatch.setattr(facade, '_COUNTED_LEDGER_ERA_START', '2025-01-01')
    assert mod_compaction_booking.record(facade, dict(event, action='clean-first'))['ok']
    turn = lambda second, creation: {'type': 'assistant', 'timestamp': f'2026-01-01T00:00:0{second}Z',
        'requestId': f'req-{second}', 'message': {'model': event['model'], 'usage': {
            'input_tokens': 2, 'cache_read_input_tokens': 10000 if creation else 20000,
            'cache_creation_input_tokens': creation,
            'cache_creation': {'ephemeral_1h_input_tokens': creation}}}}
    with path.open('a') as stream:
        stream.write(json.dumps(turn(1, 10000)) + '\n')
        stream.write(json.dumps(turn(2, 0)) + '\n')
        stream.write(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-01-01T00:00:03Z'}) + '\n')
        stream.write(json.dumps(turn(4, 0)) + '\n')
    conn = facade._init_trends_db()
    try:
        conn.execute('INSERT INTO session_log(jsonl_path,date,session_uuid) VALUES(?,?,?)',
                     (str(path), '2026-01-01', event['session_id']))
        conn.commit()
        result = facade._update_counted_cumulative(conn, max_sessions=100, quiet=True)
        rows = conn.execute('SELECT oneshot_usd,reread_tokens,reread_usd FROM counted_reread').fetchall()
    finally:
        conn.close()
    assert result['rows_written'] == 1
    assert len(rows) == 1 and rows[0][0] == 0 and rows[0][1] == 30000
    assert rows[0][2] == pytest.approx(0.099)


def test_compaction_cost_is_subtracted_from_measured_savings(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    result = mod_compaction_booking.record(facade, event)
    assert result['ok']
    summary = facade._get_savings_summary(days=9999)
    assert summary['by_category']['mod_compaction_cost']['cost_saved_usd'] == pytest.approx(-result['cost_usd'], abs=0.0001)


def test_generic_action_finds_unique_manual_boundary_and_model(case):
    from _measure import mod_action_booking
    facade, path, event = case
    original = path.read_text()
    before = {'type': 'assistant', 'timestamp': '2025-12-31T23:59:59Z',
              'message': {'model': event['model'], 'usage': {'input_tokens': 1}}}
    path.write_text(json.dumps(before) + '\n' + original)
    event.pop('compaction_id')
    event.pop('model')
    event['started_at'] = event['timestamp'] - 5
    assert mod_action_booking.record(facade, event)['ok']


def test_generic_cli_books_the_manual_compaction(case, tmp_path):
    import os
    import subprocess
    facade, path, event = case
    before = {'type': 'assistant', 'timestamp': '2025-12-31T23:59:59Z',
              'message': {'model': event['model'], 'usage': {'input_tokens': 1}}}
    path.write_text(json.dumps(before) + '\n' + path.read_text())
    event.pop('compaction_id')
    event.pop('model')
    event['started_at'] = event['timestamp'] - 5
    env = dict(os.environ, TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(facade.SNAPSHOT_DIR),
               TOKEN_OPTIMIZER_RUNTIME='claude', PYTHONDONTWRITEBYTECODE='1')
    cmd = [sys.executable, str(SCRIPTS / 'measure.py'), 'mod-action']
    result = subprocess.run(cmd, input=json.dumps(event), text=True, capture_output=True, env=env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)['removed_tokens'] == 15000
    conn = facade._init_trends_db()
    try:
        assert conn.execute("SELECT count(*) FROM mod_action_receipts").fetchone()[0] == 1
    finally:
        conn.close()


def test_missing_engine_cost_is_never_guessed(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    event.pop('cost_usd')
    result = mod_compaction_booking.record(facade, event)
    assert not result['ok'] and result['reason'] == 'cost-unavailable'


def test_confirmed_action_with_missing_usage_is_counted_but_unpriced(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    result = mod_compaction_booking.record(facade, dict(event, usage=None, observed_manual=True))
    assert result['ok'] and not result['priced']
    summary = mod_compaction_booking.summary(facade, '2025-01-01')
    assert summary['events'] == 1 and summary['unpriced_events'] == 1
    conn = facade._init_trends_db()
    try:
        assert conn.execute("SELECT count(*) FROM compression_events WHERE feature LIKE 'mod_clean%'").fetchone()[0] == 0
    finally:
        conn.close()


def test_unrelated_cost_in_session_delta_is_not_misattributed(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    result = mod_compaction_booking.record(facade, dict(event, cost_usd=1.0, observed_manual=True))
    assert result['ok'] and result['priced'] is False and result['reason'] == 'cost-mismatch'
    conn = facade._init_trends_db()
    try:
        assert conn.execute("SELECT count(*) FROM savings_events WHERE event_type='mod_compaction_cost'").fetchone()[0] == 0
    finally:
        conn.close()


def test_unpriced_observation_surfaces_in_savings(case):
    from _measure import mod_action_booking
    facade, _, event = case
    assert mod_action_booking.record(facade, dict(event, usage=None, observed_manual=True))['ok']
    summary = facade._get_savings_summary(days=9999)
    assert summary['mod_compaction_actions'] == {'events': 1, 'unpriced_events': 1}
    assert summary['total_cost_usd'] == 0


def test_compaction_removed_tokens_merge_without_fake_initial_savings(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    assert mod_compaction_booking.record(facade, event)['ok']
    features = facade._get_compression_summary(days=9999)['by_feature']
    categories = {}
    tokens, cost, events = facade._merge_v5_compression(categories, features, 3.0)
    assert tokens == 15000 and cost == 0 and events == 1
    assert categories['mod_clean']['cost_saved_usd'] == 0


def test_conflicting_unpriced_action_id_cannot_create_a_credit(case, tmp_path):
    from _measure import mod_compaction_booking
    facade, path, event = case
    assert mod_compaction_booking.record(facade, dict(event, usage=None, observed_manual=True))['ok']
    other = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    other_path = tmp_path / (other + '.jsonl')
    boundary = json.loads(path.read_text())
    boundary['sessionId'] = other
    other_path.write_text(json.dumps(boundary) + '\n')
    result = mod_compaction_booking.record(facade, dict(event, session_id=other, transcript_path=str(other_path)))
    assert not result['ok']
    conn = facade._init_trends_db()
    try:
        assert conn.execute("SELECT count(*) FROM savings_events WHERE event_type='mod_compaction_cost'").fetchone()[0] == 0
    finally:
        conn.close()


def test_retry_repairs_observation_after_committed_receipt(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    assert mod_compaction_booking._record_verified(facade, event)['ok']
    assert mod_compaction_booking.record(facade, event)['ok']
    assert mod_compaction_booking.summary(facade, '2025-01-01') == {'events': 1, 'unpriced_events': 0}


@pytest.mark.parametrize('signal', ['observed_manual', 'observed_action'])
def test_incomplete_retry_of_same_compaction_does_not_double_count(case, signal):
    from _measure import mod_compaction_booking
    facade, _, event = case
    assert mod_compaction_booking.record(facade, event)['ok']
    retry = mod_compaction_booking.record(facade, dict(event, action_id='retry-id', usage=None, **{signal: True}))
    assert retry['ok'] and retry['duplicate']
    assert mod_compaction_booking.summary(facade, '2025-01-01') == {'events': 1, 'unpriced_events': 0}


def test_merged_savings_include_removal_and_subtract_compaction_cost(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    result = mod_compaction_booking.record(facade, event)
    assert result['ok']
    summary = facade._get_merged_savings(days=9999)
    assert summary['by_category']['mod_clean']['tokens_saved'] == 15000
    assert summary['by_category']['mod_compaction_cost']['cost_saved_usd'] == pytest.approx(-result['cost_usd'], abs=0.0001)
    assert summary['mod_compaction_actions']['events'] == 1


def test_unlinked_mod_removal_never_falls_back_to_initial_input_credit(case, monkeypatch):
    facade, path, event = case
    monkeypatch.setattr(facade, '_COUNTED_LEDGER_ERA_START', '2025-01-01')
    with path.open('a') as stream:
        for second in (1, 2):
            stream.write(json.dumps({'type': 'assistant', 'timestamp': f'2026-01-01T00:00:0{second}Z',
                'requestId': f'raw-{second}', 'message': {'model': event['model'], 'usage': {
                    'input_tokens': 2, 'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 0}}}) + '\n')
    conn = facade._init_trends_db()
    try:
        conn.execute('INSERT INTO session_log(jsonl_path,date,session_uuid) VALUES(?,?,?)',
                     (str(path), '2026-01-01', event['session_id']))
        conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,compressed_tokens,verified,tier,session_uuid) "
                     "VALUES(?,?,?,?,?,?,?)", ('2026-01-01T02:00:00', 'mod_clean', 20000, 5000, 1, 'measured', event['session_id']))
        conn.commit()
        facade._update_counted_cumulative(conn, max_sessions=100, quiet=True)
        row = conn.execute('SELECT oneshot_usd,reread_tokens,reread_usd FROM counted_reread').fetchone()
    finally:
        conn.close()
    assert row[0] == 0 and row[1] == 30000 and row[2] == pytest.approx(0.009)


def test_action_id_cannot_be_reused_for_a_different_compaction_kind(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    assert mod_compaction_booking._record_verified(facade, event)['ok']
    result = mod_compaction_booking.record(facade, dict(event, action='clean-first'))
    assert not result['ok'] and result['reason'] == 'identity-conflict'


def test_ambiguous_manual_boundaries_are_counted_without_credit(case):
    from _measure import mod_action_booking
    facade, path, event = case
    second = json.loads(path.read_text())
    second['uuid'] = 'boundary-2'
    with path.open('a') as stream:
        stream.write(json.dumps(second) + '\n')
    event.pop('compaction_id')
    event['observed_manual'] = True
    result = mod_action_booking.record(facade, event)
    assert result['ok'] and not result['priced']
    conn = facade._init_trends_db()
    try:
        assert conn.execute("SELECT count(*) FROM compression_events WHERE feature LIKE 'mod_clean%'").fetchone()[0] == 0
    finally:
        conn.close()


def test_confirmed_action_without_hook_evidence_is_unpriced(case):
    from _measure import mod_compaction_booking
    facade, _, event = case
    result = mod_compaction_booking.record(facade, dict(event, usage=None, observed_action=True))
    assert result['ok'] and not result['priced']
    assert mod_compaction_booking.summary(facade, '2025-01-01')['unpriced_events'] == 1
