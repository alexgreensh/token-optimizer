"""Read-only tool-search diagnosis uses effective config, never guessed bills."""
import sys
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))


def check(env=None, settings=None, runtime='claude'):
    from tool_search_diagnostic import diagnose_tool_search
    return diagnose_tool_search(env or {}, settings or {}, runtime=runtime)


@pytest.mark.parametrize('value', [None, 'false'])
def test_custom_proxy_disabled_or_default_warns(value):
    env = {'ANTHROPIC_BASE_URL': 'https://proxy.example/v1'}
    if value is not None:
        env['ENABLE_TOOL_SEARCH'] = value
    row = check(env)
    assert row[0] == '!!'
    assert 'upfront' in row[2]
    assert 'tool_reference' in row[2]


@pytest.mark.parametrize('value', ['true', 'auto', 'auto:5'])
def test_explicit_setting_is_reported_without_claiming_proxy_compatibility(value):
    row = check({'ANTHROPIC_BASE_URL': 'https://proxy.example', 'ENABLE_TOOL_SEARCH': value})
    assert value in row[2]
    assert 'compatibility' in row[2]
    assert 'always on' not in row[2]


@pytest.mark.parametrize('url', ['https://api.anthropic.com', 'https://api.anthropic.com/v1'])
def test_official_endpoint_needs_no_warning(url):
    assert check({'ANTHROPIC_BASE_URL': url}) is None


@pytest.mark.parametrize('url', ['https://api.anthropic.com.evil.test',
    'https://api.anthropic.com@evil.test', 'https://evil.test/api.anthropic.com'])
def test_hostname_not_substring(url):
    assert check({'ANTHROPIC_BASE_URL': url})[0] == '!!'


@pytest.mark.parametrize('value', ['maybe', 'auto:wrong', 'auto:-1', 'auto:101', '1'])
def test_invalid_value_is_not_assumed_enabled(value):
    row = check({'ENABLE_TOOL_SEARCH': value})
    assert row[0] == '!!'
    assert 'invalid' in row[2].lower()


def test_config_precedence_including_empty_env():
    settings = {'env': {'ANTHROPIC_BASE_URL': 'https://proxy.example', 'ENABLE_TOOL_SEARCH': 'false'}}
    assert check({}, settings)[0] == '!!'
    assert 'true' in check({'ENABLE_TOOL_SEARCH': 'true'}, settings)[2]
    assert check({'ANTHROPIC_BASE_URL': '', 'ENABLE_TOOL_SEARCH': ''}, settings) is None


def test_no_credentials_or_raw_setting_leak():
    row = check({'ANTHROPIC_BASE_URL': 'https://name:secret@proxy.example/v1?key=other-secret',
                 'ENABLE_TOOL_SEARCH': 'malformed-private-token'})
    assert 'secret' not in repr(row)
    assert 'name:' not in repr(row)
    assert 'malformed-private-token' not in repr(row)


@pytest.mark.parametrize('runtime', ['codex', 'opencode', 'cursor', 'hermes', 'pi'])
def test_not_a_claim_about_other_runtimes(runtime):
    assert check({'ANTHROPIC_BASE_URL': 'https://proxy.example'}, runtime=runtime) is None


@pytest.mark.parametrize('url', ['not a url', 'https://[', 'http://api.anthropic.com', 'https://api.anthropic.com:1234'])
def test_bad_or_nondefault_endpoint_not_treated_first_party(url):
    assert check({'ANTHROPIC_BASE_URL': url})[0] == '!!'


def test_experimental_beta_disable_has_precedence():
    row = check({'ENABLE_TOOL_SEARCH': 'true', 'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS': '1'})
    assert row[0] == '!!'
    assert 'disabled' in row[2]


def test_doctor_contains_diagnostic_integration():
    # The pure classifier has no filesystem/network dependency. Doctor must
    # actually call it, not leave a tested but unused utility in the package.
    import ast
    tree = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    doctor = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'doctor')
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == 'diagnose_tool_search' for n in ast.walk(doctor))


def test_real_doctor_json_exposes_custom_proxy_warning(tmp_path):
    import json
    import os
    import subprocess
    env = {**os.environ, 'HOME': str(tmp_path), 'CLAUDE_CONFIG_DIR': str(tmp_path / '.claude'),
           'TOKEN_OPTIMIZER_RUNTIME': 'claude', 'ANTHROPIC_BASE_URL': 'https://proxy.example',
           'ENABLE_TOOL_SEARCH': ''}
    env.pop('CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS', None)
    result = subprocess.run([sys.executable, str(SCRIPTS / 'measure.py'), 'doctor', '--json'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    row = next(r for r in data['checks'] if r['name'] == 'Tool search')
    assert row['status'] == '!!'
    assert 'upfront' in row['detail']


@pytest.mark.parametrize('environ', [None, [], 'not-a-mapping', 42])
def test_wrong_type_environment_fails_safe(environ):
    from tool_search_diagnostic import diagnose_tool_search
    assert diagnose_tool_search(environ, {}) is None
