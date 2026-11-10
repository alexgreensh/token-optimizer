import importlib.util
from pathlib import Path

p = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts/_measure/rework_metrics.py'
spec = importlib.util.spec_from_file_location('rework_metrics', p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def msg(kind, content, **kw):
    return dict(type=kind, message={'content': content}, **kw)


def tool(i, name, args):
    return msg('assistant', [{'type': 'tool_use', 'id': str(i), 'name': name, 'input': args}], message_id=str(i))


def test_metrics_respect_turn_window_and_failure_evidence():
    rows = [msg('user', 'fix it'), tool(1, 'Edit', {'file_path': 'a'}),
            tool(2, 'Bash', {'command': 'test'}),
            msg('user', [{'type': 'tool_result', 'tool_use_id': '2', 'is_error': True, 'content': 'Exit code 1'}]),
            tool(3, 'Edit', {'file_path': 'a'}), tool(4, 'Bash', {'command': 'test'}),
            msg('user', [{'type': 'tool_result', 'tool_use_id': '4', 'is_error': True, 'content': 'Exit code 1'}]),
            {'type': 'system', 'subtype': 'compact_boundary'}]
    result = m.session_metrics(rows, window=4)
    assert result['repeat_edits'] == 1
    assert result['repeat_failures'] == 1
    assert result['tool_calls'] == 4 and result['user_requests'] == 1
    assert result['compactions_per_100_calls'] == 25
    assert m.session_metrics(rows, window=1)['repeat_edits'] == 0


def test_missing_failure_is_not_a_failure_and_sidechains_excluded():
    rows = [msg('user', 'go'), tool(1, 'Bash', {'command': 'x'}),
            msg('user', [{'type': 'tool_result', 'tool_use_id': '1', 'content': 'failure'}]),
            tool(2, 'Bash', {'command': 'x'}),
            msg('user', [{'type': 'tool_result', 'tool_use_id': '2', 'is_error': True, 'content': 'Exit code 1'}]),
            dict(tool(3, 'Edit', {'file_path': 'a'}), isSidechain=True)]
    assert m.session_metrics(rows)['repeat_failures'] == 0
    assert m.session_metrics(rows)['tool_calls'] == 2


def test_matched_comparison_intervals_and_no_matches():
    rows = [{'project': 'a', 'size': 10, 'arm': 'on', 'value': 2},
            {'project': 'a', 'size': 11, 'arm': 'off', 'value': 4},
            {'project': 'b', 'size': 10, 'arm': 'on', 'value': 10}]
    out = m.matched_effect(rows, 'value', bootstrap=100, seed=7)
    assert out['pairs'] == 1 and out['difference'] == -2
    assert out['interval_95'] is None
    assert m.matched_effect(rows[:1], 'value')['difference'] is None


def test_hook_status_requires_linked_evidence_and_timeout_wins():
    rows = [{'type': 'attachment', 'attachment': {'hookName': 'PostToolUse:Bash', 'toolUseID': '1', 'stderr': 'bash_compress_hook budget exceeded', 'stdout': ''}},
            {'type': 'attachment', 'attachment': {'hookName': 'PostToolUse:Bash', 'toolUseID': '2', 'stderr': '', 'stdout': '[Full result archived (10 chars)'}},
            {'type': 'attachment', 'attachment': {'hookName': 'PostToolUse:Bash', 'toolUseID': '3', 'stderr': '', 'stdout': ''}}]
    assert m.hook_statuses(rows) == {'1': 'timeout', '2': 'compressed'}


def test_cloud_comparison_uses_paired_sessions_and_full_followup():
    rows = [msg('user', 'work'), tool(1, 'Bash', {'command': 'a'}),
            {'type': 'attachment', 'attachment': {'hookName': 'PostToolUse:Bash', 'toolUseID': '1', 'stderr': 'bash_compress_hook budget exceeded'}},
            tool(2, 'Edit', {'file_path': 'a'}), tool(3, 'Edit', {'file_path': 'a'}),
            tool(4, 'Bash', {'command': 'b'}),
            {'type': 'attachment', 'attachment': {'hookName': 'PostToolUse:Bash', 'toolUseID': '4', 'stdout': '[bash_compress]'}},
            tool(5, 'Edit', {'file_path': 'b'}), tool(6, 'Edit', {'file_path': 'c'})]
    out = m.cloud_effect([rows], window=2)
    assert out['sessions'] == 1 and out['difference'] == 1
    assert out['interval_95'] is None
    assert m.cloud_effect([rows[:-2]], window=2)['sessions'] == 0
