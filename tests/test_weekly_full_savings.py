"""Growing sessions and exact-week full-value regression coverage."""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest
from test_runway_resume_lean_addback import measure, _wire


def _turn(rid, ts, output=10, input_tokens=1000):
    return {'type': 'assistant', 'timestamp': ts, 'requestId': rid,
            'message': {'id': rid, 'model': 'claude-opus-4-6', 'content': [],
                        'usage': {'input_tokens': input_tokens, 'output_tokens': output}}}


def test_collector_refreshes_growing_session_without_duplicating(measure, monkeypatch, tmp_path):
    p = tmp_path / 'growing.jsonl'
    p.write_text(json.dumps(_turn('one', '2026-09-04T00:00:00Z'))+'\n')
    monkeypatch.setattr(measure, '_find_all_jsonl_files', lambda days: [(p, p.stat().st_mtime, 'test')])
    monkeypatch.setattr(measure, '_find_subagent_jsonl_files', lambda p: [])
    measure.collect_sessions(quiet=True)
    with p.open('a') as f:
        f.write(json.dumps(_turn('two', '2026-09-04T00:01:00Z'))+'\n')
    measure.collect_sessions(quiet=True)
    c = sqlite3.connect(measure.TRENDS_DB)
    assert c.execute('SELECT count(*), sum(api_calls) FROM session_log').fetchone() == (1, 2)
    assert measure.collect_sessions(quiet=True) == 0
    assert c.execute('SELECT count(*), sum(api_calls) FROM session_log').fetchone() == (1, 2)
    c.close()


def test_window_parser_excludes_outside_activity_and_dedups_streaming(measure, tmp_path):
    p = tmp_path / 'cross-reset.jsonl'
    rows = [_turn('before', '2026-09-03T16:59:00Z'),
            _turn('inside', '2026-09-03T17:01:00Z', output=2),
            _turn('inside', '2026-09-03T17:01:01Z', output=10),
            _turn('after', '2026-09-10T17:00:00Z')]
    p.write_text('\n'.join(map(json.dumps, rows)))
    start = datetime(2026, 9, 3, 17, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    parsed = measure._parse_session_jsonl(p, window_start=start, window_end=end)
    assert parsed['api_calls'] == 1
    assert parsed['total_output_tokens'] == 10
    assert measure._parse_session_jsonl(p)['api_calls'] == 3  # cache must be window-specific


def test_weekly_full_value_replaces_overlapping_component_sum(measure, monkeypatch):
    _wire(measure, monkeypatch, resume_lean_usd=18.32)
    monkeypatch.setattr(measure, '_weekly_full_savings', lambda **kw: {
        'saved_usd': 200.62, 'start': '2026-09-03T17:00:00+00:00',
        'end': '2026-09-08T00:00:00+00:00', 'method': 'full workload',
    }, raising=False)
    snap = measure.runway_snapshot()
    wk = next(w for w in snap['windows'] if w['key'] == 'seven_day')
    assert wk['saved_usd'] == 200.62  # not 37.10, nor 200.62 + 37.10
    assert snap['saved_usd_context'] + snap['saved_usd_routing'] == 200.62
    assert next(w for w in snap['windows'] if w['key'] == 'five_hour')['saved_usd'] is None


def test_activity_window_uses_grown_old_session_and_includes_child(measure, monkeypatch, tmp_path):
    parent, child = tmp_path / 'parent.jsonl', tmp_path / 'child.jsonl'
    parent.write_text('\n'.join(map(json.dumps, [
        _turn('before', '2026-09-03T16:59:00Z', input_tokens=999999),
        _turn('inside', '2026-09-03T17:01:00Z'),
        _turn('after', '2026-09-10T17:00:00Z', input_tokens=999999)])))
    child.write_text(json.dumps(_turn('child', '2026-09-03T17:02:00Z', input_tokens=500)))
    monkeypatch.setattr(measure, '_find_subagent_jsonl_files', lambda p: [child] if str(p)==str(parent) else [])
    c=measure._init_trends_db()
    c.execute("INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,api_calls) VALUES (?,?,?,?,?)", (str(parent),'2026-08-01',1,0,1))
    c.commit()
    start=datetime(2026,9,3,17,tzinfo=timezone.utc)
    result=measure._price_parent_activity_window(c,start,start+timedelta(days=7),'anthropic')
    c.close()
    assert result['api_calls']==1
    assert result['tokens']==1520
    assert result['flat_usd']==pytest.approx(0.008)


def test_full_week_uses_shared_baseline_and_real_activity(measure, monkeypatch, tmp_path):
    monkeypatch.setenv('TOKEN_OPTIMIZER_RUNTIME','claude')
    monkeypatch.setattr(measure,'detect_runtime',lambda:'claude')
    monkeypatch.setattr(measure,'_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS',2)
    monkeypatch.setattr(measure,'_AFTER_MIN_SESSIONS',1)
    p=tmp_path/'weekly.jsonl'
    p.write_text(json.dumps(_turn('one','2026-09-04T00:00:00Z')))
    monkeypatch.setattr(measure,'_find_subagent_jsonl_files',lambda p:[])
    c=measure._init_trends_db()
    for i in range(2):
        c.execute('INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,api_calls,cache_hit_rate,model_usage_json) VALUES (?,?,?,?,?,?,?)',
            (str(tmp_path/f'anchor{i}'),'2026-06-01',10000,10,1,0,'{"opus":10010}'))
    c.execute('INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,api_calls) VALUES (?,?,?,?,?)',
        (str(p),'2026-09-01',1,0,1))
    c.commit();c.close()
    # Baseline source files may have been archived; only current-period gaps
    # make activity coverage incomplete.
    start=datetime(2026,9,3,17,tzinfo=timezone.utc)
    end=datetime(2026,9,5,tzinfo=timezone.utc)
    value=measure._weekly_full_savings(resets_at=(start+timedelta(days=7)).timestamp(),now=end.timestamp())
    assert value['uncapped_usd']==pytest.approx(0.05,abs=0.005)
    assert value['api_calls']==1
    assert datetime.fromisoformat(value['start'])==start


def test_full_savings_survives_flat_throughput_multiplier(measure, monkeypatch):
    _wire(measure, monkeypatch, resume_lean_usd=0)
    monkeypatch.setattr(measure, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.0})
    monkeypatch.setattr(measure, '_weekly_full_savings', lambda **kw: {'saved_usd': 200.62})
    snap=measure.runway_snapshot()
    assert snap is not None
    assert next(w for w in snap['windows'] if w['key']=='seven_day')['saved_usd']==200.62


def test_child_growth_refresh_does_not_double_count_cached_parent(measure, monkeypatch, tmp_path):
    parent, child=tmp_path/'main.jsonl',tmp_path/'child.jsonl'
    parent.write_text(json.dumps(_turn('parent','2026-09-04T00:00:00Z'))+'\n')
    child.write_text(json.dumps(_turn('child1','2026-09-04T00:00:01Z'))+'\n')
    monkeypatch.setattr(measure,'_find_all_jsonl_files',lambda days:[(parent,parent.stat().st_mtime,'test')])
    monkeypatch.setattr(measure,'_find_subagent_jsonl_files',lambda p:[child] if str(p)==str(parent) else [])
    measure.collect_sessions(quiet=True)
    with child.open('a') as f:
        f.write(json.dumps(_turn('child2','2026-09-04T00:00:02Z'))+'\n')
    measure.collect_sessions(quiet=True)
    c=sqlite3.connect(measure.TRENDS_DB)
    assert c.execute('SELECT count(*),sum(input_tokens),sum(api_calls) FROM session_log').fetchone()==(1,3000,1)
    c.close()


def test_collection_catchup_cannot_move_workload_anchor(measure, monkeypatch):
    monkeypatch.setattr(measure,'_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS',2)
    original=dict(sessions=2,usd=10,tokens=10000,flat_usd=12,api_calls=10,messages=12)
    assert measure._stable_workload_anchor(original,'2026-06')==(original,'2026-06')
    refreshed=dict(original,flat_usd=100,api_calls=15)
    assert measure._stable_workload_anchor(refreshed,'2026-06')==(original,'2026-06')
    assert json.loads((measure.SNAPSHOT_DIR/'workload_anchor.json').read_text())['metrics']==original


@pytest.fixture(autouse=True)
def _isolate_activity_discovery(measure, monkeypatch):
    monkeypatch.setattr(measure, '_find_all_jsonl_files', lambda days: [])


@pytest.mark.parametrize('change', ['older_month', 'rates', 'bad_shape'])
def test_anchor_recovers_when_history_or_rates_change(measure, monkeypatch, change):
    monkeypatch.setattr(measure, '_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS', 2)
    old = dict(sessions=2, usd=10, tokens=10000, flat_usd=12, api_calls=10, messages=12)
    measure._stable_workload_anchor(old, '2026-07')
    month = {'older_month': '2026-06'}.get(change, '2026-07')
    if change == 'rates':
        monkeypatch.setattr(measure, '_WEIGHT_POOL_FLAT_RATES', dict(measure._WEIGHT_POOL_FLAT_RATES, input=4))
    if change == 'bad_shape':
        (measure.SNAPSHOT_DIR / 'workload_anchor.json').write_text('[]')
    updated = dict(old, flat_usd=20)
    assert measure._stable_workload_anchor(updated, month) == (updated, month)
    assert json.loads((measure.SNAPSHOT_DIR / 'workload_anchor.json').read_text())['month'] == month


def test_anchor_never_slides_to_a_later_month(measure, monkeypatch):
    """Retention pruning leaves a later earliest-month; comparing against it
    would measure the tool against itself."""
    monkeypatch.setattr(measure, '_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS', 2)
    old = dict(sessions=2, usd=10, tokens=10000, flat_usd=12, api_calls=10, messages=12)
    measure._stable_workload_anchor(old, '2026-07')
    assert measure._stable_workload_anchor(dict(old, flat_usd=20), '2026-08') == (old, '2026-07')


def _pool(saving=50):
    return dict(transformation_usd=saving, actual_usd=100, counterfactual_usd=100+saving,
                now_units=10, anchor_month='2026-06')


def test_weekly_cache_expires_and_reset_change_invalidates(measure, monkeypatch):
    monkeypatch.setattr(measure, 'detect_runtime', lambda: 'claude')
    now = datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(measure.time, 'time', lambda: now)
    calls = []
    monkeypatch.setattr(measure, '_session_weight_pool_savings', lambda *a, **kw: calls.append(kw) or _pool())
    reset = now + 3*86400
    first = measure._weekly_full_savings(resets_at=reset)
    assert measure._weekly_full_savings(resets_at=reset) == first
    assert len(calls) == 1
    now += 61
    measure._weekly_full_savings(resets_at=reset)
    assert len(calls) == 2
    measure._weekly_full_savings(resets_at=reset+86400)
    assert len(calls) == 3


def test_missing_reset_does_not_claim_an_exact_week(measure, monkeypatch):
    monkeypatch.setattr(measure, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(measure, '_session_weight_pool_savings', lambda *a, **kw: pytest.fail('No known subscription week'))
    assert measure._weekly_full_savings() is None


def test_flat_full_workload_week_still_shows_measured_savings(measure, monkeypatch):
    """A week whose whole workload got heavier (bigger models, bigger contexts)
    must not hide the savings that were actually measured that week."""
    _wire(measure, monkeypatch, resume_lean_usd=30)
    monkeypatch.setattr(measure, '_weekly_full_savings',
                        lambda **kw: measure._positive_full_value({'saved_usd': 0}))
    snap = measure.runway_snapshot()
    assert snap['saved_usd_context'] + snap['saved_usd_routing'] > 0


def test_new_uncollected_activity_is_included(measure, monkeypatch, tmp_path):
    p = tmp_path / 'uncollected.jsonl'
    p.write_text(json.dumps(_turn('new', '2026-09-04T00:00:00Z')))
    monkeypatch.setattr(measure, '_find_all_jsonl_files', lambda days: [(p, p.stat().st_mtime, 'test')])
    monkeypatch.setattr(measure, '_find_subagent_jsonl_files', lambda p: [])
    conn = measure._init_trends_db()
    start = datetime(2026, 9, 3, tzinfo=timezone.utc)
    result = measure._price_parent_activity_window(conn, start, start+timedelta(days=7), 'anthropic')
    conn.close()
    assert result['api_calls'] == 1
    assert result['tokens'] == 1010


def test_child_activity_after_parent_stops_is_still_charged(measure, monkeypatch, tmp_path):
    parent, child = tmp_path/'parent.jsonl', tmp_path/'child.jsonl'
    parent.write_text(json.dumps(_turn('parent', '2026-09-02T00:00:00Z')))
    child.write_text(json.dumps(_turn('child', '2026-09-04T00:00:00Z')))
    monkeypatch.setattr(measure, '_find_subagent_jsonl_files', lambda p: [child])
    conn = measure._init_trends_db()
    conn.execute('INSERT INTO session_log (jsonl_path,date,input_tokens,api_calls) VALUES (?,?,?,?)',
                 (str(parent), '2026-09-02', 1000, 1))
    conn.commit()
    start = datetime(2026, 9, 3, tzinfo=timezone.utc)
    result = measure._price_parent_activity_window(conn, start, start+timedelta(days=7), 'anthropic')
    conn.close()
    assert result['api_calls'] == 0  # preserve the existing parent work unit
    assert result['tokens'] == 1010


def test_older_collector_cannot_overwrite_newer_row(measure, monkeypatch, tmp_path):
    p = tmp_path / 'concurrent.jsonl'
    p.write_text(json.dumps(_turn('one', '2026-09-04T00:00:00Z')))
    monkeypatch.setattr(measure, '_find_all_jsonl_files', lambda days: [(p, p.stat().st_mtime, 'test')])
    monkeypatch.setattr(measure, '_find_subagent_jsonl_files', lambda p: [])
    measure.collect_sessions(quiet=True)
    conn = measure._init_trends_db()
    conn.execute('UPDATE session_log SET collected_at=?,api_calls=2',
                 ((datetime.now()+timedelta(seconds=30)).isoformat(),))
    conn.commit()
    monkeypatch.setattr(measure, '_is_file_collected', lambda *a, **kw: False)
    measure.collect_sessions(quiet=True)
    assert conn.execute('SELECT api_calls FROM session_log').fetchone()[0] == 2
    conn.close()
