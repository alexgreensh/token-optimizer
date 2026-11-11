"""The v3 continuity prior is bounded and respects boundary evidence."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure.quality_continuity import continuity


@pytest.mark.parametrize('count,expected', [(0, 1), (1, .945), (3, .851), (10, .642), (20, .517), (1000, .45)])
def test_unknown_boundaries(count, expected):
    assert continuity(count, [])[0] == pytest.approx(expected, abs=.001)


def test_evidence_and_size():
    clean = {'pre_tokens': 400000, 'post_tokens': 0, 'restore_evidence': 'transcript', 'status': 'no_concerning_signal_observed'}
    bad = dict(clean, status='needs_attention')
    small = dict(clean, pre_tokens=20000, restore_evidence='none', status='unmeasurable')
    assert 1 > continuity(1, [clean])[0] > continuity(1, [])[0] > continuity(1, [bad])[0]
    assert continuity(1, [small])[0] > continuity(1, [])[0]


def test_empty_window_uses_same_scorer(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'snap'))
    import measure
    data = dict(messages=[], tool_results=[], tool_result_meta=[], system_reminders=[], reads=[], writes=[], compactions=10, tool_calls=0, agent_dispatches=[], decisions=[], total_entries=0, context_tokens=None, model=None, model_context_window=None)
    result = measure.compute_quality_score(data)
    assert result['score'] == pytest.approx(64.2, abs=.1)
    assert result['grade'] == 'C'
    assert result['score_version'] == 3


def test_boundaries_and_plan_exit(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'snap'))
    import measure
    path = tmp_path / 'session.jsonl'
    records = [
        {'type': 'user', 'message': {'content': 'hello'}},
        {'type': 'assistant', 'message': {'usage': {'input_tokens': 60000}, 'content': []}},
        {'type': 'system', 'subtype': 'compact_boundary', 'isSidechain': True},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'ExitPlanMode', 'id': 'x', 'input': {}}]}},
    ]
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    data = measure._parse_jsonl_for_quality(path)
    assert data['compactions'] == 0
    assert data['context_tokens'] == 60000
    assert len(data['messages']) == 3


def test_compaction_count_corrects_after_pending_bump(monkeypatch):
    import measure
    prev = {'compactions': 4, '_compact_bumped_at': 1000}
    assert measure._settled_compactions(3, prev, False, now=1001) == 4
    assert measure._settled_compactions(3, prev, False, now=1100) == 3


@pytest.mark.parametrize('base,effective,expected', [
    (96.8, 0, 96.8), (67.5, 0, 67.5), (96.8, .5, 94.0),
    (96.8, 1, 91.4), (80.5, 3, 68.5), (80.5, 1.5, 74.0),
    (100, 10, 64.2), (100, 5, 77.5), (87.0, 7, 62.0),
    (87.0, 10, 55.8), (74.0, 6, 54.9), (93.5, 20, 48.3),
])
def test_review_worked_examples(base, effective, expected):
    from _measure.quality_continuity import FLOOR, RHO
    assert base * (FLOOR + (1 - FLOOR) * RHO ** effective) == pytest.approx(expected, abs=.5)


def test_checkpoint_tail_matches_full_parse(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'snap'))
    import measure
    path = tmp_path / 'tail.jsonl'
    rows = [
        {'type':'user','message':{'content':'hello'}},
        {'type':'system','subtype':'compact_boundary','compactMetadata':{'preTokens':300000,'postTokens':50000}},
        {'type':'assistant','message':{'usage':{'input_tokens':90000},'content':[]}},
        {'type':'system','subtype':'compact_boundary','compactMetadata':{'preTokens':200000,'postTokens':40000}},
        {'type':'user','message':{'content':'tail'}},
    ]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    first = measure._parse_jsonl_for_quality(path)
    checkpoint = first['_quality_checkpoint']
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':50000},'content':[]}})+'\n')
    full = measure._parse_jsonl_for_quality(path)
    tail = measure._parse_jsonl_for_quality(path, checkpoint)
    for key in ('messages', 'compactions', 'context_tokens', 'compaction_events', 'tool_calls'):
        assert tail[key] == full[key]
    assert tail['_parsed_from_offset'] > 0


def test_parser_failure_reasons(monkeypatch, tmp_path):
    import measure
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('')
    assert measure._parse_jsonl_for_quality(empty)['parse_reason'] == 'no_messages'
    missing = measure._parse_jsonl_for_quality(tmp_path / 'missing.jsonl')
    assert not missing and missing.reason == 'unreadable'
    large = tmp_path / 'large.jsonl'
    large.write_text('{"type":"user","message":{"content":"hello"}}\n')
    monkeypatch.setattr(measure.codex_session, 'MAX_PARSE_FILE_BYTES', 1)
    failed = measure._parse_jsonl_for_quality(large)
    assert not failed and failed.reason == 'too_large'


def test_checkpoint_rejects_replaced_file(monkeypatch, tmp_path):
    import json
    import measure
    path = tmp_path / 'tail.jsonl'
    path.write_text(json.dumps({'type':'system','subtype':'compact_boundary'})+'\n')
    data = measure._parse_jsonl_for_quality(path)
    cp = data['_quality_checkpoint']
    replacement = tmp_path / 'replacement'
    replacement.write_text(json.dumps({'type':'user','message':{'content':'new'}})+'\n')
    replacement.replace(path)
    result = measure._parse_jsonl_for_quality(path, cp)
    assert result['_parsed_from_offset'] == 0
    assert result['compactions'] == 0


def test_hermes_uses_common_v3_score(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'snap'))
    import hermes_session
    measured = hermes_session.compute_quality_score(10000, 1000, 4, context_window=200000, context_tokens=50000)
    assert measured['score_version'] == 3
    assert measured['measured'] is True
    missing = hermes_session.compute_quality_score(10000, 1000, 1000, context_window=200000, context_tokens=None)
    assert missing['measured'] is False
    assert missing['grade'] != 'S'


@pytest.mark.parametrize('fill,count,clean,bad,expected', [
    (.03, 0, 0, 0, 96.8), (.5, 0, 0, 0, 67.5), (.06, 1, 1, 0, 94.0),
    (.06, 1, 0, 0, 91.4), (.3, 3, 0, 0, 68.5), (.3, 3, 3, 0, 74.0),
    (0, 10, 0, 0, 64.2), (0, 10, 10, 0, 77.5), (.207, 7, 0, 0, 62.0),
    (.207, 10, 0, 0, 55.8), (.4, 5, 0, 2, 54.9), (.1, 20, 0, 0, 48.3),
])
def test_worked_examples_use_real_scorer(monkeypatch, tmp_path, fill, count, clean, bad, expected):
    import measure
    # Supply the same recovery evidence that the recovery tracker emits.
    events = ([{'pre_tokens': 400000, 'post_tokens': 0, 'restore_evidence': 'transcript', 'status': 'no_concerning_signal_observed'}] * clean
              + [{'pre_tokens': 400000, 'post_tokens': 0, 'restore_evidence': 'none', 'status': 'needs_attention'}] * bad)
    monkeypatch.setattr(measure, '_assess_compaction_recovery', lambda data, sid: {'status': 'observing' if count else None, 'score': None, 'evidence': events, 'coverage': {}})
    data = dict(messages=[], tool_results=[], tool_result_meta=[], system_reminders=[], reads=[], writes=[], compactions=count, tool_calls=0, agent_dispatches=[], decisions=[], total_entries=0, context_tokens=int(fill * 1000000) if fill else None, model='claude-sonnet-4-6', model_context_window=1000000)
    result = measure.compute_quality_score(data)
    assert result['score'] == pytest.approx(expected, abs=.5)


def test_cache_matches_scorer_after_compaction(monkeypatch, tmp_path):
    import json
    import measure
    monkeypatch.setattr(measure, 'QUALITY_CACHE_DIR', tmp_path)
    sid = 'aaaaaaaa-1111-2222-3333-444444444444'
    path = tmp_path / (sid + '.jsonl')
    monkeypatch.setattr(measure, '_context_window_for_model_str', lambda model: 200000)
    monkeypatch.setattr(measure, 'detect_context_window', lambda: (200000, 'test'))
    before = {'type': 'assistant', 'message': {'usage': {'input_tokens': 94000}, 'content': []}}
    boundary = {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 94000, 'postTokens': 20000}}
    path.write_text(json.dumps(before) + '\n')
    pre = measure.compute_quality_score(measure._parse_jsonl_for_quality(path), session_id=sid)
    assert pre['model_fill_pct'] == 47.0
    with path.open('a') as stream:
        stream.write(json.dumps(boundary) + '\n')
    expected = measure.compute_quality_score(measure._parse_jsonl_for_quality(path), session_id=sid)
    actual = measure.quality_cache(quiet=True, session_jsonl=str(path), force=True, session_id=sid)
    cache = json.loads((tmp_path / ('quality-cache-' + sid + '.json')).read_text())
    for key in ('score', 'grade', 'score_version'):
        assert cache[key] == expected[key]
    assert actual == expected['score'] < 100
    assert actual <= 100 * expected['continuity']
    assert cache['_quality_checkpoint']['offset'] > 0


def test_unmeasured_cache_keeps_live_fill_warning(monkeypatch, tmp_path, capsys):
    import json, time
    import measure
    sid = 'bbbbbbbb-1111-2222-3333-444444444444'
    monkeypatch.setattr(measure, 'QUALITY_CACHE_DIR', tmp_path)
    path = tmp_path / (sid + '.jsonl')
    path.write_text('{"type":"user"}\n')
    cache = tmp_path / ('quality-cache-' + sid + '.json')
    cache.write_text(json.dumps({'score': 67.7, 'score_version': 3, 'compactions': 3}))
    (tmp_path / 'live-fill.json').write_text(json.dumps({'session_id': sid, 'used_percentage': 95, 'timestamp': int(time.time() * 1000)}))
    monkeypatch.setattr(measure, '_parse_jsonl_for_quality', lambda *args: None)
    measure.quality_cache(quiet=True, session_jsonl=str(path), force=True, session_id=sid)
    result = json.loads(cache.read_text())
    assert result['measured'] is False and result['score'] <= 67.7 and result['grade'] != 'S'
    assert result['fill_warning']['level'] == 'CRITICAL'
    assert result['_last_fill_warn_level'] == 'CRITICAL'
    assert 'CRITICAL' in capsys.readouterr().out


def test_old_score_version_is_never_comparable():
    import measure
    assert not measure._quality_scores_comparable({'score_version': 2}, {'score_version': 3})
    assert measure._quality_scores_comparable({'score_version': 3}, {'score_version': 3})


def test_waste_uses_session_window(monkeypatch):
    import measure
    data = dict(messages=[], tool_results=[], tool_result_meta=[], system_reminders=[], reads=[], writes=[], compactions=0, tool_calls=0, agent_dispatches=[], decisions=[], total_entries=0, context_tokens=20000, model='claude-sonnet-4-6', model_context_window=200000)
    monkeypatch.setattr(measure, 'detect_stale_reads', lambda q: {'estimated_waste_tokens': 1000, 'stale_reads': [], 'total_reads': 0, 'count': 0})
    monkeypatch.setattr(measure, 'detect_context_window', lambda: (1000000, 'test'))
    one = measure.compute_quality_score(data)
    monkeypatch.setattr(measure, 'detect_context_window', lambda: (200000, 'test'))
    two = measure.compute_quality_score(data)
    assert one['signals']['absolute_waste_tokens'] == two['signals']['absolute_waste_tokens'] == 95


def test_too_large_cache_keeps_prior_score(monkeypatch, tmp_path):
    import json
    import measure
    monkeypatch.setattr(measure, 'QUALITY_CACHE_DIR', tmp_path)
    sid = 'cccccccc-1111-2222-3333-444444444444'
    path = tmp_path / (sid + '.jsonl')
    path.write_text('{"type":"user","message":{"content":"a message"}}\n')
    cache_path = tmp_path / ('quality-cache-' + sid + '.json')
    cache_path.write_text(json.dumps({'score': 67.7, 'score_version': 3, 'compactions': 3}))
    monkeypatch.setattr(measure.codex_session, 'MAX_PARSE_FILE_BYTES', 1)
    measure.quality_cache(quiet=True, session_jsonl=str(path), force=True, session_id=sid)
    result = json.loads(cache_path.read_text())
    assert result['parse_reason'] == 'too_large'
    assert result['score'] <= 67.7 and not result['measured'] and result['grade'] != 'S'


def test_post_compaction_tokens_are_used_without_next_message(tmp_path):
    import json
    import measure
    path = tmp_path / 'session.jsonl'
    path.write_text(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 300000, 'postTokens': 50000}}) + '\n')
    result = measure._parse_jsonl_for_quality(path)
    assert result['context_tokens'] == 50000
    assert result['parse_reason'] == 'no_messages'


def test_checkpoint_preserves_recovery_call_aliases(tmp_path):
    import json
    import measure
    path = tmp_path / 'recovery.jsonl'
    def call(n):
        return [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'id': f't{n}', 'input': {'command': 'echo ok'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': f't{n}', 'content': 'ok'}]}},
        ]
    rows = sum((call(i) for i in range(12)), [])
    rows += [{'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 300000, 'postTokens': 40000}}]
    rows += sum((call(i) for i in range(12, 24)), [])
    rows += [{'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 250000, 'postTokens': 35000}}]
    rows += sum((call(i) for i in range(24, 30)), [])
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    cp = measure._parse_jsonl_for_quality(path)['_quality_checkpoint']
    with path.open('a') as stream:
        stream.write(''.join(json.dumps(row) + '\n' for i in range(30, 37) for row in call(i)))
    full = measure._parse_jsonl_for_quality(path)
    tail = measure._parse_jsonl_for_quality(path, cp)
    assert full['compaction_events'] == tail['compaction_events']
    assert measure.compute_quality_score(full)['score'] == measure.compute_quality_score(tail)['score']


@pytest.mark.parametrize('count', [0, 1, 3, 10, 1000])
def test_empty_scorer_is_monotonic_and_bounded(count):
    import measure
    data = dict(messages=[], tool_results=[], tool_result_meta=[], system_reminders=[], reads=[], writes=[], compactions=count, tool_calls=0, agent_dispatches=[], decisions=[], total_entries=0, context_tokens=None, model=None, model_context_window=None)
    current = measure.compute_quality_score(data)
    data['compactions'] += 1
    following = measure.compute_quality_score(data)
    assert following['score'] <= current['score']
    assert current['continuity'] >= .45
    assert current['score'] > 0
    if count == 0:
        assert current['score'] >= 90
    if count > 0:
        assert current['score'] < 100
    if count == 10:
        assert current['grade'] in ('C', 'D')


def test_incremental_status_telemetry_matches_full_scan(tmp_path):
    import json
    import measure
    from _measure.quality_checkpoint import status_telemetry
    path = tmp_path / 'session.jsonl'
    first = [
        {'type':'assistant','timestamp':'2026-01-01T00:00:00Z','message':{'usage':{'input_tokens':100,'cache_read_input_tokens':300},'content':[{'type':'tool_use','name':'Agent','id':'a','input':{'description':'research'}}]}},
    ]
    path.write_text(''.join(json.dumps(row)+'\n' for row in first))
    saved = status_telemetry(path)
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'a','content':'ok'}]}})+'\n')
        stream.write(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':100,'cache_read_input_tokens':100},'content':[]}})+'\n')
    resumed = status_telemetry(path, saved)
    full = status_telemetry(path)
    assert resumed['total_input'] == full['total_input'] == 600
    assert resumed['cache_read'] == full['cache_read'] == 400
    assert resumed['agents'] == full['agents'] == measure._extract_active_agents(path) == []
    assert resumed['started'] == full['started']
    assert resumed['read_from'] == saved['offset']


def test_unmeasured_postcompact_bumps_only_once(monkeypatch, tmp_path):
    import json
    import measure
    sid = 'dddddddd-1111-2222-3333-444444444444'
    path = tmp_path / (sid + '.jsonl')
    path.write_text('{"type":"user"}\n')
    monkeypatch.setattr(measure, 'QUALITY_CACHE_DIR', tmp_path)
    monkeypatch.setattr(measure, '_parse_jsonl_for_quality', lambda *args: None)
    cache_path = tmp_path / ('quality-cache-' + sid + '.json')
    cache_path.write_text(json.dumps({'score': 67.7, 'score_version': 3, 'compactions': 3}))
    for _ in range(2):
        measure.quality_cache(quiet=True, session_jsonl=str(path), force=True, session_id=sid, post_compact=True)
        result = json.loads(cache_path.read_text())
        assert result['compactions'] == 4
        assert result['measured'] is False


def test_codex_tail_parse_matches_full_parse(tmp_path):
    import json
    import codex_session
    path = tmp_path / 'rollout.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'example'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'first'}},
        {'type': 'compacted', 'payload': {'replacement_history': [{'role': 'user', 'content': 'summary'}]}},
        {'type': 'event_msg', 'payload': {'type': 'context_compacted'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'second'}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    first = codex_session.parse_jsonl_for_quality(path)
    cp = first['_quality_checkpoint']
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'answer'}}) + '\n')
    full = codex_session.parse_jsonl_for_quality(path)
    tail = codex_session.parse_jsonl_for_quality(path, cp)
    for key in ('messages', 'compactions', 'compaction_events', 'context_tokens', 'compaction_ratios'):
        assert tail[key] == full[key]
    assert tail['_parsed_from_offset'] > 0
    import measure
    from unittest.mock import patch
    with patch.object(measure, '_use_codex_session_adapter', return_value=True):
        through_adapter = measure._parse_jsonl_for_quality(path, cp)
    assert through_adapter['_parsed_from_offset'] > 0
    assert through_adapter['compaction_events'] == full['compaction_events']


def test_codex_compaction_drops_old_fill(tmp_path):
    import json
    import codex_session
    path = tmp_path / 'rollout.jsonl'
    usage = {'input_tokens': 180000, 'cached_input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0}
    rows = [
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage, 'last_token_usage': usage}}},
        {'type': 'compacted', 'payload': {'replacement_history': [{'role': 'user', 'content': 'short summary'}]}},
        {'type': 'event_msg', 'payload': {'type': 'context_compacted'}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = codex_session.parse_jsonl_for_quality(path)
    assert 0 < result['context_tokens'] < 180000
    assert result['context_tokens'] == result['compaction_ratios'][0]['after_context_tokens']
    assert result['compaction_events'][0]['post_tokens'] is None
