"""Codex telemetry and config regression cases, with isolated runtime data.

Ported from @dormancygrace's PR (fix/codex-windows-hook-launcher). Covers the
token-accounting, pricing, session-resolution, and cost-attribution foundation.
"""
import json
import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
import codex_session as cs

SID = '01234567-1234-1234-1234-123456789abc'


def write_session(path, totals=(100, 200)):
    records = [{'type': 'session_meta', 'payload': {'id': SID, 'cwd': '/project'}},
               {'type': 'turn_context', 'payload': {'model': 'gpt-5.4'}}]
    for n in totals:
        usage = dict(input_tokens=n, cached_input_tokens=n // 2,
                     output_tokens=n // 5, reasoning_output_tokens=n // 10)
        records += [{'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'hello'}},
                    {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                        'total_token_usage': usage, 'last_token_usage': usage}}}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(map(json.dumps, records)), encoding='utf-8')
    return path


def test_inclusive_token_counts_and_duplicate_usage(tmp_path):
    p = write_session(tmp_path / 'session.jsonl', (100, 100, 200))
    parsed = cs.parse_session_jsonl(p)
    assert parsed['total_input_tokens'] == 200
    assert parsed['total_output_tokens'] == 40
    assert parsed['total_cache_read'] == 100
    assert parsed['cache_hit_rate'] == 0.5
    parts = dict(parsed['model_usage_breakdown']['gpt-5.4'])
    assert len(parts.pop('requests')) == 2
    assert parts == {
        'fresh_input': 100, 'cache_read': 100, 'cache_create': 0, 'output': 40}
    turns = cs.parse_session_turns(p)
    assert turns[-1]['input_tokens'] == 200
    assert turns[-1]['output_tokens'] == 40
    assert cs.parse_jsonl_for_quality(p)['context_tokens'] == 240


def test_latest_session_is_selected_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, 'session_roots', lambda: (tmp_path,))
    old = write_session(tmp_path / 'old' / 'a.jsonl')
    new = write_session(tmp_path / 'new' / 'z.jsonl')
    os.utime(old, (1, 1))
    assert cs.find_all_jsonl_files(days=90, max_files=1)[0][0] == new


def test_task_duration_excludes_days_between_resumes(tmp_path):
    p = write_session(tmp_path / 'session.jsonl')
    records = [json.loads(line) for line in p.read_text().splitlines()]
    records[0]['timestamp'] = '2026-09-01T00:00:00Z'
    records.append({'timestamp': '2026-09-06T00:00:00Z', 'type': 'event_msg',
                    'payload': {'type': 'task_complete', 'duration_ms': 120000}})
    p.write_text('\n'.join(map(json.dumps, records)))
    parsed = cs.parse_session_jsonl(p)
    assert parsed['duration_minutes'] == 2
    assert parsed['wall_duration_minutes'] == 7200
    assert parsed['duration_source'] == 'task_complete'


def test_duration_wall_fallback_when_no_task_complete(tmp_path):
    """Caveat B: a session with no task_complete events falls back to wall-clock
    with duration_source='wall', not 0/'unavailable'."""
    p = write_session(tmp_path / 'session.jsonl', (100,))
    records = [json.loads(line) for line in p.read_text().splitlines()]
    records[0]['timestamp'] = '2026-09-01T00:00:00Z'
    records[-1]['timestamp'] = '2026-09-01T00:10:00Z'
    p.write_text('\n'.join(map(json.dumps, records)))
    parsed = cs.parse_session_jsonl(p)
    assert parsed['duration_source'] == 'wall'
    assert parsed['duration_minutes'] == pytest.approx(10.0, abs=0.01)
    assert parsed['wall_duration_minutes'] == pytest.approx(10.0, abs=0.01)


@pytest.fixture
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_RUNTIME', 'codex')
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    import measure as m
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', tmp_path / 'data')
    monkeypatch.setattr(m, 'TRENDS_DB', tmp_path / 'data/trends.db')
    monkeypatch.setattr(m, 'detect_runtime', lambda: 'codex')
    monkeypatch.setattr(cs, 'session_roots', lambda: (tmp_path / 'sessions',))
    return m


def test_model_attribution_is_session_scoped(measure, tmp_path, monkeypatch):
    write_session(tmp_path / 'sessions' / f'rollout-2026-09-06-{SID}.jsonl')
    monkeypatch.setenv('CLAUDE_MODEL', 'sonnet')
    assert measure._resolve_session_model(SID) == 'gpt-5.4'
    assert measure._resolve_session_model('missing-session') == 'unknown'
    assert measure._extract_session_uuid(f'rollout-2026-09-06-{SID}') == (SID, False)


def test_savings_use_openai_prices_and_unknown_is_not_sonnet(measure):
    measure._log_savings_event('test', 1000, SID, model='gpt-5.4')
    measure._log_savings_event('test', 1000, SID, model='gpt-future-unknown')
    c = measure._init_trends_db()
    rows = c.execute('SELECT model, cost_saved_usd FROM savings_events ORDER BY id').fetchall()
    c.close()
    assert rows[0] == ('gpt-5.4', 1000 * measure.OPENAI_MODEL_PRICING['gpt-5.4']['input'] / 1e6)
    assert rows[1] == ('gpt-future-unknown', None)


def test_astra_pricing_uses_request_context_not_session_sum(measure):
    m = measure
    assert m._normalize_openai_model_name('gpt-6-astra-2026-09-01') == 'gpt-6-astra'
    small = {'fresh_input': 100000, 'cache_read': 100000, 'output': 1000}
    parts = {k: v * 3 for k, v in small.items()}
    parts['requests'] = [small] * 3
    assert m._cost_from_model_breakdown({'gpt-6-astra': parts}) == pytest.approx(3.45)
    assert m._get_model_cost('gpt-6-astra', 200000, 1000, 100000, 0) == pytest.approx(4.275)


def test_rollout_nudge_identity_matches_only_its_live_task(measure, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_VERBOSITY_STEER", "1")  # default OFF (DECISION row 11)
    m = measure
    transcript = write_session(tmp_path / f'rollout-2026-09-06-{SID}.jsonl')
    cache_path = tmp_path / f'quality-cache-{transcript.stem}.json'
    cache_path.write_text('{}')
    monkeypatch.setattr(m, '_quality_cache_path_for', lambda fp=None: cache_path)
    monkeypatch.setattr(m, '_read_quality_cache', lambda cp: {
        'fill_pct': 47, 'score': 73, 'session_efficiency': 60,
        'nudge_count': 0, 'last_nudge_time': 0})
    monkeypatch.setattr(m, '_log_savings_event', lambda *a, **kw: None)
    assert m.run_verbosity_steer(str(transcript), quiet=True, session_id=SID)
    assert not m.run_verbosity_steer(str(transcript), quiet=True,
                                    session_id='11234567-1234-1234-1234-123456789abc')
