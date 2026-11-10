import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import transcript_product
import measure


def _write(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def _attachment(kind, **fields):
    return {'type': 'attachment', 'timestamp': '2026-01-25T00:00:00Z',
            'attachment': {'type': kind, **fields}}


def test_skill_cap_requires_observed_listing_and_known_budget(tmp_path):
    path = _write(tmp_path / 'a.jsonl', [_attachment('skill_listing', isInitial=True, content='x' * 990)])
    now = datetime(2026, 1, 30, tzinfo=timezone.utc)
    assert transcript_product.scan([path], budget=1000, now=now)['listing']['at_cap'] is True
    assert transcript_product.scan([path], budget=2000, now=now)['listing']['at_cap'] is False
    assert transcript_product.scan([path], now=now)['listing']['at_cap'] is None
    assert transcript_product.scan([], budget=1000)['listing']['at_cap'] is None


def test_repeated_plateau_with_changing_skill_count_is_inferred(tmp_path):
    paths = [_write(tmp_path / ('%d.jsonl' % i), [_attachment('skill_listing', isInitial=True,
                     skillCount=100 + i * 5, content='x' * (1000 + i))]) for i in range(5)]
    result = transcript_product.scan(paths, now=datetime(2026, 1, 30, tzinfo=timezone.utc))
    assert result['listing']['at_cap'] is True
    assert result['listing']['basis'] == 'observed plateau'


def test_server_instruction_names_match_tool_namespace(tmp_path):
    now = datetime(2026, 1, 30, tzinfo=timezone.utc)
    path = _write(tmp_path / 'a.jsonl', [
        _attachment('deferred_tools_delta', addedNames=['mcp__claude_ai_Fixture__fetch'],
                    addedLines=['mcp__claude_ai_Fixture__fetch']),
        _attachment('mcp_instructions_delta', addedNames=['claude.ai Fixture'], addedBlocks=['x' * 40]),
        {'type': 'assistant', 'timestamp': '2026-01-21T00:00:00Z', 'message': {'content': [
            {'type': 'tool_use', 'name': 'mcp__claude_ai_Fixture__fetch'}]}}])
    assert transcript_product.scan([path], now=now)['unused_connectors'] == []


def test_connector_costs_use_attachments_and_calls_within_window(tmp_path):
    now = datetime(2026, 1, 30, tzinfo=timezone.utc)
    path = _write(tmp_path / 'a.jsonl', [
        _attachment('deferred_tools_delta', addedNames=['mcp__alpha__one', 'mcp__beta__two'],
                    addedLines=['mcp__alpha__one', 'mcp__beta__two']),
        _attachment('mcp_instructions_delta', addedNames=['alpha', 'beta'],
                    addedBlocks=['a' * 40, 'b' * 80]),
        {'type': 'assistant', 'timestamp': '2026-01-21T00:00:00Z', 'message': {'content': [
            {'type': 'tool_use', 'name': 'mcp__alpha__one', 'id': 'call-a'}]}},
        {'type': 'assistant', 'timestamp': '2025-01-21T00:00:00Z', 'message': {'content': [
            {'type': 'tool_use', 'name': 'mcp__beta__two', 'id': 'old-call'}]}}])
    result = transcript_product.scan([path], now=now)
    assert [x['server'] for x in result['unused_connectors']] == ['beta']
    row = result['unused_connectors'][0]
    assert row['instruction_chars'] == 80
    assert row['tool_chars'] == len('mcp__beta__two')
    assert row['estimated_tokens_per_call'] == (80 + len('mcp__beta__two')) // 4


def test_incomplete_transcript_does_not_claim_zero_calls(tmp_path):
    path = _write(tmp_path / 'a.jsonl', [
        _attachment('deferred_tools_delta', addedNames=['mcp__beta__two'], addedLines=['mcp__beta__two']),
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'mcp__beta__two'}]}}])
    result = transcript_product.scan([path], now=datetime(2026, 1, 30, tzinfo=timezone.utc))
    assert result['unused_connectors'] == []


def test_wire_hidden_names_do_not_become_prefix_cost(tmp_path):
    path = _write(tmp_path / 'a.jsonl', [
        _attachment('deferred_tools_delta', addedNames=['mcp__hidden__one'], addedLines=[])])
    result = transcript_product.scan([path], now=datetime(2026, 1, 30, tzinfo=timezone.utc))
    assert not result['incomplete']
    assert result['unused_connectors'] == []


def test_coach_and_audit_use_transcript_evidence(monkeypatch, capsys):
    check = {'listing': {'at_cap': True}, 'unused_connectors': [
        {'server': 'example', 'estimated_tokens_per_call': 9, 'tool_chars': 16,
         'instruction_chars': 20, 'sessions': 1}], 'incomplete': False}
    monkeypatch.setattr(transcript_product, 'from_facade', lambda *a, **k: check)
    monkeypatch.setattr(measure, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(measure, '_find_all_jsonl_files', lambda **k: [])
    monkeypatch.setattr(measure, '_get_compression_coverage', lambda **k: {})
    names = ['skill-%d' % i for i in range(30)]
    components = {'skills': {'count': 30, 'tokens': 9000, 'names': names},
                  'skills_detail': {name: {'frontmatter_tokens': 300} for name in names},
                  'hooks': {'configured': True, 'names': ['SessionEnd']}}
    trends = {'skills': {'never_used': names}}
    coach = measure.generate_coach_data(components=components, trends=trends)
    text = json.dumps(coach)
    assert 'SLASH_COMMAND_TOOL_CHAR_BUDGET' in text
    assert 'example' in text and '~9 tokens per call' in text
    assert '9,000 tokens (measured descriptions' not in text
    monkeypatch.setattr(measure, 'measure_components', lambda: components)
    monkeypatch.setattr(measure, '_collect_trends_data', lambda **k: trends)
    measure.quick_scan(as_json=True)
    text = capsys.readouterr().out
    assert 'SLASH_COMMAND_TOOL_CHAR_BUDGET' in text and 'example' in text


def test_claude_structural_potential_does_not_price_file_inventory(monkeypatch):
    monkeypatch.setattr(measure, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(measure, '_cached_measure_components', lambda: {
        'skills': {'names': ['one'], 'count': 1, 'tokens': 100}})
    tokens, count = measure._unused_skill_overhead_tokens(days=30)
    assert (tokens, count) == (0, 1)
    potential = measure._compute_structural_potential(days=30)
    assert potential['recoverable_tokens'] == 0
    assert potential['unused_skills'] == 1
    assert potential['basis'] == 'no measured listing reduction'
