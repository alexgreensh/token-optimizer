import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'


def run_hook(tmp_path, command, raw):
    home = tmp_path / 'home'
    home.mkdir()
    env = {'PATH': os.environ['PATH'], 'HOME': str(home),
           'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(home / 'snap'),
           'CLAUDE_PLUGIN_DATA': str(home / 'data'),
           'TOKEN_OPTIMIZER_PLUGIN_DATA': str(home / 'data'),
           'PYTHONDONTWRITEBYTECODE': '1'}
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command},
               'tool_response': {'stdout': raw, 'stderr': '', 'exit_code': 0},
               'session_id': 'regression', 'tool_use_id': 'regression'}
    proc = subprocess.run([sys.executable, str(SCRIPTS / 'bash_compress_hook.py')],
                          input=json.dumps(payload), text=True, capture_output=True, env=env, check=True)
    if not proc.stdout.strip():
        return raw
    return json.loads(proc.stdout)['hookSpecificOutput']['updatedToolOutput']['stdout']


def search_output():
    return ''.join(f'src/file_{i % 30}.py:{i}:match_{i}\n' for i in range(150))


@pytest.mark.parametrize('command,extra', [
    ('grep -rn lookup_handler . && ls -la', ''.join(f'-rw-r--r-- user group {i} file_{i}\n' for i in range(90))),
    ("rg -n lookup_handler && echo 'BOUNDARY' && du -sh src/*", 'BOUNDARY\n' + ''.join(f'{i}K\tsrc/path_{i}\n' for i in range(80))),
])
def test_compound_search_preserves_other_output(tmp_path, command, extra):
    raw = search_output() + extra
    assert run_hook(tmp_path, command, raw) == raw


def test_bare_search_still_compresses(tmp_path):
    raw = search_output()
    visible = run_hook(tmp_path, 'grep -rn lookup_handler .', raw)
    assert '[Search results:' in visible
    assert len(visible) < len(raw)


def test_compound_pytest_preserves_error_between_runs(tmp_path):
    run = ''.join(f'tests/test_{i}.py::test_one PASSED [{i}%]\n' for i in range(100))
    raw = run + 'ERROR failure\n' * 100 + 'BOUNDARY\n' + run
    visible = run_hook(tmp_path, 'pytest -v tests && echo BOUNDARY && python3 -m pytest -v', raw)
    assert visible == raw


@pytest.mark.parametrize('command', [
    'jest && ls -la',
    'cargo test && ls -la',
    'go test ./... && du -sh src/*',
    'npm test && ls -la',
    'yarn test && ls -la',
    'eslint . && ls -la',
    'gcc -c main.c && ls -la',
], ids=['jest', 'cargo', 'go', 'npm', 'yarn', 'eslint', 'gcc'])
def test_compound_renderer_preserves_other_producer(tmp_path, command):
    raw = (''.join(f'tests/example_{i}.test.js PASSED\n' for i in range(150))
           + 'Tests: 150 passed\n' + ''.join(f'listing_{i}\n' for i in range(100)))
    assert run_hook(tmp_path, command, raw) == raw


def test_cd_then_npm_test_still_compresses(tmp_path):
    raw = (''.join(f'tests/example_{i}.test.js PASSED\n' for i in range(150))
           + 'Tests: 150 passed\n')
    visible = run_hook(tmp_path, 'cd p && npm test', raw)
    assert len(visible) < len(raw)
    assert '150 passed' in visible


def test_single_search_with_echo_boundary_compresses_and_keeps_boundary(tmp_path):
    raw = search_output() + 'BOUNDARY\n'
    visible = run_hook(tmp_path, 'grep -rn lookup_handler . && echo BOUNDARY', raw)
    assert len(visible) < len(raw)
    assert 'BOUNDARY' in visible.splitlines()


def test_echo_boundary_is_never_dropped(tmp_path):
    raw = search_output() + ''.join(f'extra output {i}\n' for i in range(30)) + 'BOUNDARY\n'
    visible = run_hook(tmp_path, 'grep -rn lookup_handler . && echo BOUNDARY', raw)
    assert 'BOUNDARY' in visible.splitlines()
