"""A keep-warm fork must not attach, persist, or inherit unrelated secrets."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))


def test_fork_command_and_environment_are_isolated():
    from _measure.keepwarm_fork import fork_command, fork_environment
    command = fork_command('/bin/claude', 'session-id', 'model-id', 'Reply with ok')
    assert '--resume' in command and '--fork-session' in command
    assert '--print' not in command and '-p' not in command
    assert '{"disableAllHooks":true}' in command
    env = fork_environment({'HOME': '/home/test', 'PATH': '/usr/bin', 'UNRELATED_SECRET': 'do-not-inherit',
                            'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE': '1'}, 'fake-key', 'http://127.0.0.1:1234')
    assert env['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:1234'
    assert env['CLAUDE_CODE_SKIP_PROMPT_HISTORY'] == '1'
    assert env['TOKEN_OPTIMIZER_KEEPWARM_PING'] == '1'
    assert 'UNRELATED_SECRET' not in env and 'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE' not in env


def test_fork_rejects_bad_identifiers_and_missing_key():
    from _measure.keepwarm_fork import fork_command, fork_environment
    with pytest.raises(ValueError): fork_command('/bin/claude', '--bad', 'model', 'ok')
    with pytest.raises(ValueError): fork_environment({}, '', 'http://127.0.0.1:1234')
    with pytest.raises(ValueError): fork_environment({}, 'key', 'https://example.com')


def test_guarded_fork_uses_fake_cli_and_suppresses_tool(tmp_path):
    import json
    import os
    from test_keepwarm_prefix import _fixture
    from test_keepwarm_response_filter import _stream
    from _measure.keepwarm_fork import run_guarded
    rows, fork, _ = _fixture()
    fork['stream'] = True
    transcript = tmp_path / 'parent.jsonl'
    transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    stat = transcript.stat()
    record = {'session_id': 'session-id', 'model': 'synthetic-model',
              'transcript_path': str(transcript), 'byte_size': stat.st_size,
              'transcript_mtime': stat.st_mtime}
    (tmp_path / 'body.json').write_text(json.dumps(fork))
    executable = tmp_path / 'fake-claude'
    executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, urllib.request, traceback
root = pathlib.Path(__file__).parent
try:
    body = (root / 'body.json').read_bytes()
    req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages?beta=true', data=body,
        headers={'x-api-key':os.environ['ANTHROPIC_API_KEY'], 'anthropic-beta':'test'})
    with urllib.request.urlopen(req, timeout=5) as response:
        data=response.read()
    if b'tool_use' in data or b'Write' in data:
        (root / 'sentinel').write_text('tool leaked')
except Exception:
    (root / 'fake-error.txt').write_text(traceback.format_exc())
''')
    executable.chmod(0o700)
    checks = []
    def paused():
        checks.append(True)
        return True
    result = run_guarded(record, str(tmp_path), str(executable), 'fake-key',
                         {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, paused,
                         timeout=8, forward=lambda *args: _stream().replace(
                             b'"model": "model"', b'"model": "synthetic-model"'))
    assert result['ok'] is True, (result, (tmp_path / 'fake-error.txt').read_text() if (tmp_path / 'fake-error.txt').exists() else '')
    assert result['usage']['cache_read_input_tokens'] == 20000
    assert not (tmp_path / 'sentinel').exists()
    assert len(checks) >= 2


def test_guarded_fork_refuses_missing_key_before_any_io(tmp_path):
    from _measure.keepwarm_fork import run_guarded
    result = run_guarded({}, str(tmp_path), '/fake/claude', '', {}, lambda: True)
    assert not result['ok'] and result['reason'] == 'api-key-unavailable'
