"""A request for the stored original must not produce a second preview."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
NAMES = ['headroom_retrieve', 'caveman_retrieve',
         'mcp__headroom__headroom_retrieve', 'mcp__caveman__caveman_retrieve']
BODY = json.dumps([{'id': i, 'value': 'Ω original ' * 20} for i in range(90)], indent=2)


def run_hook(tmp_path, script, payload):
    env = {**os.environ, 'HOME': str(tmp_path), 'CLAUDE_CONFIG_DIR': str(tmp_path / '.claude'),
           'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(tmp_path / 'snap'),
           'TOKEN_OPTIMIZER_RUNTIME': 'claude'}
    p = subprocess.run([sys.executable, str(SCRIPTS / script), '--quiet'],
                       input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.mark.parametrize('name', NAMES)
def test_recovered_original_never_archived_or_replaced(tmp_path, name):
    for n in range(2):
        out = run_hook(tmp_path, 'archive_result.py', {
            'tool_name': name, 'session_id': 'recovery', 'tool_use_id': f'call_{n}',
            'tool_input': {'handle': 'original'}, 'tool_response': BODY})
        assert out.strip() == '', out
    assert not list(tmp_path.rglob('manifest.jsonl'))


@pytest.mark.parametrize('name', NAMES[2:])
def test_legacy_manifest_cannot_deny_recovery(tmp_path, name):
    sys.path.insert(0, str(SCRIPTS))
    from refetch_fingerprint import tool_fingerprint
    d = tmp_path / 'snap/tool-archive/recovery'
    d.mkdir(parents=True)
    (d / 'old.json').write_text(json.dumps({'response': BODY}))
    (d / 'manifest.jsonl').write_text(json.dumps({
        'tool_name': name, 'tool_use_id': 'old', 'args_hash': tool_fingerprint(name, {'handle': 'original'}),
        'timestamp': datetime.now(timezone.utc).isoformat(), 'tokens_est': 10000}) + '\n')
    out = run_hook(tmp_path, 'refetch_guard.py', {'tool_name': name, 'session_id': 'recovery',
                                              'tool_input': {'handle': 'original'}})
    assert json.loads(out)['hookSpecificOutput'].get('permissionDecision') is None


def test_own_expand_is_not_compressed_or_deduped(tmp_path):
    import shlex
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS / "measure.py"))} expand original'
    for n in range(4):
        out = run_hook(tmp_path, 'bash_compress_hook.py', {'tool_name': 'Bash', 'session_id': 'recovery',
            'tool_input': {'command': command}, 'tool_response': {'stdout': BODY, 'stderr': '', 'exit_code': 0}})
        assert not out.strip(), out
    out = run_hook(tmp_path, 'archive_result.py', {'tool_name': 'Bash', 'session_id': 'recovery',
        'tool_use_id': 'expand', 'tool_input': {'command': command}, 'tool_response': {'stdout': BODY}})
    assert not out.strip()
    assert not list(tmp_path.rglob('manifest.jsonl'))


def test_ordinary_mcp_result_still_compresses(tmp_path):
    out = run_hook(tmp_path, 'archive_result.py', {'tool_name': 'mcp__ordinary__list_items',
        'session_id': 'control', 'tool_use_id': 'control', 'tool_response': BODY})
    replacement = json.loads(out)['hookSpecificOutput']['updatedMCPToolOutput']
    assert len(replacement) < len(BODY)
    assert 'Full result archived' in replacement

@pytest.mark.parametrize('name', ['memory_retrieve', 'docs_retrieve', 'mcp__memory__retrieve',
    'mcp__x__headroom_retrieve_extra', 'fake__caveman_retrieve', None, {}])
def test_name_lookalikes_do_not_get_veto(name):
    from recovery_output import is_recovery_tool
    assert not is_recovery_tool(name)


@pytest.mark.parametrize('suffix', ['; echo another', ' && echo another', ' | cat', ' --search another'])
def test_compound_and_search_commands_not_exempt(suffix):
    from recovery_output import is_expand_command
    assert not is_expand_command(f'python3 {SCRIPTS / "measure.py"} expand original{suffix}')


def test_unrelated_measure_script_not_exempt(tmp_path):
    from recovery_output import is_expand_command
    assert not is_expand_command(f'python3 {tmp_path / "measure.py"} expand original')
    assert not is_expand_command(f'echo {SCRIPTS / "measure.py"} expand original')
    assert not is_expand_command(f'python3 {SCRIPTS / "measure.py"} expand')


def test_actual_archive_expand_hook_round_trip(tmp_path):
    import shlex
    # Archive an ordinary tool, run the real recovery CLI, then replay its
    # output through both real PostToolUse hooks twice. No fake expand body.
    run_hook(tmp_path, 'archive_result.py', {'tool_name': 'mcp__ordinary__list_items',
        'session_id': 'source', 'tool_use_id': 'original', 'tool_response': BODY})
    before = (tmp_path / 'snap/tool-archive/source/manifest.jsonl').read_bytes()
    env = {**os.environ, 'HOME': str(tmp_path), 'CLAUDE_CONFIG_DIR': str(tmp_path / '.claude'),
           'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(tmp_path / 'snap'), 'TOKEN_OPTIMIZER_RUNTIME': 'claude'}
    argv = [sys.executable, str(SCRIPTS / 'measure.py'), 'expand', 'original']
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert BODY in result.stdout
    command = ' '.join(shlex.quote(a) for a in argv)
    for n in range(2):
        payload = {'tool_name': 'Bash', 'session_id': 'source', 'tool_use_id': f'recovered_{n}',
                   'tool_input': {'command': command},
                   'tool_response': {'stdout': result.stdout, 'stderr': result.stderr, 'exit_code': 0}}
        assert not run_hook(tmp_path, 'bash_compress_hook.py', payload).strip()
        assert not run_hook(tmp_path, 'archive_result.py', payload).strip()
    assert (tmp_path / 'snap/tool-archive/source/manifest.jsonl').read_bytes() == before


def test_own_expand_session_selector_is_protected():
    import shlex
    from recovery_output import is_expand_command
    assert is_expand_command(f'python3 {shlex.quote(str(SCRIPTS / "measure.py"))} expand original --session session')
    assert not is_expand_command(f'python3 {shlex.quote(str(SCRIPTS / "measure.py"))} expand --list')


def test_no_recovery_savings_events(tmp_path):
    import sqlite3
    name = 'mcp__caveman__caveman_retrieve'
    run_hook(tmp_path, 'archive_result.py', {'tool_name': name, 'session_id': 'recovery',
        'tool_use_id': 'original', 'tool_response': BODY})
    for db in tmp_path.rglob('*.db'):
        conn = sqlite3.connect(db)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'savings_events' in tables:
                assert conn.execute('SELECT count(*) FROM savings_events').fetchone()[0] == 0
            if 'compression_events' in tables:
                assert conn.execute('SELECT count(*) FROM compression_events').fetchone()[0] == 0
        finally:
            conn.close()

@pytest.mark.parametrize('root', ['plugins/token-optimizer', 'cowork/token-optimizer'])
def test_installed_mirrors_preserve_recovery(tmp_path, root):
    import shlex
    installed = SCRIPTS.parents[2] / root / 'skills/token-optimizer/scripts'
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(installed / "measure.py"))} expand original'
    # Reuse the real subprocess harness with the installed script path.
    for script, name in [('archive_result.py', 'mcp__caveman__caveman_retrieve'),
                         ('bash_compress_hook.py', 'Bash')]:
        out = run_hook(tmp_path, str(installed / script), {'tool_name': name,
            'session_id': 'installed', 'tool_use_id': 'original', 'tool_input': {'command': command},
            'tool_response': {'stdout': BODY, 'stderr': '', 'exit_code': 0} if name == 'Bash' else BODY})
        assert not out.strip(), out
    assert not list(tmp_path.rglob('manifest.jsonl'))


def test_consolidated_runner_keeps_recovery_original(tmp_path):
    runner = SCRIPTS.parents[2] / 'hooks/posttooluse_runner.py'
    out = run_hook(tmp_path, str(runner), {'hook_event_name': 'PostToolUse',
        'tool_name': 'mcp__caveman__caveman_retrieve', 'session_id': 'consolidated',
        'tool_use_id': 'original', 'tool_input': {'handle': 'original'}, 'tool_response': BODY})
    if out.strip():
        payload = json.loads(out)
        assert 'updatedMCPToolOutput' not in payload.get('hookSpecificOutput', {})
        assert 'updatedToolOutput' not in payload.get('hookSpecificOutput', {})
    assert not list(tmp_path.rglob('manifest.jsonl'))


def test_the_hint_token_optimizer_prints_is_recognized():
    # The model runs the pointer's command verbatim; that exact string must be exempt.
    from recovery_output import is_expand_command
    from refetch_fingerprint import expand_command
    assert is_expand_command(expand_command('original'))
    assert is_expand_command(expand_command('original') + ' --session session')
    assert not is_expand_command(expand_command('original') + '; echo another')
    assert not is_expand_command(expand_command('original') + ' | cat')


@pytest.mark.parametrize('path,shown', [
    ('/opt/to/scripts/measure.py', '/opt/to/scripts/measure.py'),
    ('/Users/First Last/Library/Application Support/to/measure.py',
     "'/Users/First Last/Library/Application Support/to/measure.py'"),
    ("/home/o'brien/to/measure.py", '"/home/o\'brien/to/measure.py"'),
])
def test_hint_path_is_pasteable(path, shown):
    from refetch_fingerprint import shell_path
    assert shell_path(path) == shown


def test_hint_with_a_space_in_the_install_path_round_trips(tmp_path, monkeypatch):
    import shlex
    import recovery_output
    import refetch_fingerprint
    spaced = tmp_path / 'First Last' / 'scripts' / 'measure.py'
    monkeypatch.setattr(refetch_fingerprint, 'measure_py_path', lambda: str(spaced))
    hint = refetch_fingerprint.expand_command('original')
    # One shell word for the path, and the recognizer accepts it.
    assert shlex.split(hint) == ['python3', str(spaced), 'expand', 'original']
    assert recovery_output.is_expand_command(hint)
    # The form older releases printed is still in live transcripts.
    assert recovery_output.is_expand_command(f'python3 {spaced} expand original')
    assert not recovery_output.is_expand_command(f'python3 {spaced} expand original && echo x')


def test_windows_shaped_hint_is_recognized(monkeypatch):
    import recovery_output
    import refetch_fingerprint
    win = 'C:\\Users\\First Last\\.claude\\plugins\\cache\\to\\scripts\\measure.py'
    monkeypatch.setattr(refetch_fingerprint, 'measure_py_path', lambda: win)
    monkeypatch.setattr(refetch_fingerprint.os, 'name', 'nt')
    hint = refetch_fingerprint.expand_command('original')
    assert hint == "python3 'C:/Users/First Last/.claude/plugins/cache/to/scripts/measure.py' expand original"
    assert recovery_output.is_expand_command(hint)
    assert recovery_output.is_expand_command(f'python3 {win} expand original')
    assert not recovery_output.is_expand_command(hint + ' | cat')


@pytest.mark.parametrize('tool', ['Bash', 'PowerShell'])
def test_expand_through_either_shell_tool_is_recovery_output(tool):
    """PowerShell is a first-class shell tool in the archive matcher; its `expand`
    output is the stored original coming back and must not be archived again."""
    sys.path.insert(0, str(SCRIPTS))
    from recovery_output import is_recovery_output
    from refetch_fingerprint import expand_command
    assert is_recovery_output(tool, {'command': expand_command('original')})
    assert not is_recovery_output(tool, {'command': expand_command('original') + '; echo more'})
    assert not is_recovery_output(tool, {'command': 'dir'})
    assert not is_recovery_output('Read', {'command': expand_command('original')})


@pytest.mark.parametrize('tool', ['Bash', 'PowerShell'])
def test_expand_via_shell_tool_is_not_archived_again(tmp_path, tool):
    import shlex
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS / "measure.py"))} expand original'
    out = run_hook(tmp_path, 'archive_result.py', {'tool_name': tool, 'session_id': 's',
        'tool_use_id': 't1', 'tool_input': {'command': command}, 'tool_response': {'stdout': BODY}})
    assert not out.strip(), out
    assert not list(tmp_path.rglob('manifest.jsonl'))
