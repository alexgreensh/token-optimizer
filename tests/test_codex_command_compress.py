"""codex_command_compress: eligibility, rewrite/exec round-trip, and the two
security invariants this port hardens -- an injected tool_input.shell is
ignored, and reads outside the working directory are never auto-allowed."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
import codex_command_compress as compression
import codex_install as installer
import plugin_env


@pytest.fixture(autouse=True)
def _enable_and_isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_env, 'is_v5_flag_enabled', lambda *a, **k: True)
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _payload(command, **tool_input_extra):
    return {'tool_name': 'Bash',
            'tool_input': {'command': command, **tool_input_extra},
            'cwd': os.getcwd(), 'model': 'gpt-6-astra',
            'session_id': '11111111-1111-1111-1111-111111111111'}


def _plan_from(rewritten_command):
    """Pull the base64 plan out of a rewritten `--run <b64>` invocation."""
    import shlex
    parts = shlex.split(rewritten_command)
    idx = parts.index('--run')
    return json.loads(base64.b64decode(parts[idx + 1]))


# --------------------------------------------------------------------------- #
# Eligibility (ported)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('command', ['git branch new-branch', 'find . -delete', 'rg --pre=evil x',
    'git log --output=written', 'python -m arbitrary', 'Get-Content file; Remove-Item file',
    'Get-Content $(evil)', 'Get-ChildItem | Remove-Item'])
def test_rewrite_does_not_allow_write_or_expression_commands(command):
    assert not compression.eligible(command)


def test_eligible_accepts_inspection_commands():
    for command in ('git status', 'git log --oneline', 'rg pattern', 'ls -la',
                    'wc -l file', 'tail -n 5 file', 'tail -f log', 'Get-Content file'):
        assert compression.eligible(command), command


# --------------------------------------------------------------------------- #
# Hole (a): model-controlled tool_input.shell must never steer execution
# --------------------------------------------------------------------------- #

def test_injected_tool_input_shell_is_ignored():
    marker = Path.cwd() / 'shell-ran'
    evil = Path.cwd() / 'evil-shell'
    evil.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    evil.chmod(0o755)
    result = compression.rewrite(_payload('git status', shell=str(evil)))
    assert result is not None
    output = result['hookSpecificOutput']
    # The injected field is stripped, not echoed back into the tool call.
    assert 'shell' not in output['updatedInput']
    # And the plan carries no shell at all: --run re-derives the default.
    plan = _plan_from(output['updatedInput']['command'])
    assert 'shell' not in plan
    assert str(evil) not in json.dumps(plan)


def test_forged_plan_shell_is_ignored_at_exec():
    """A hand-crafted --run plan cannot name its interpreter either."""
    marker = Path.cwd() / 'forged-shell-ran'
    evil = Path.cwd() / 'evil-shell'
    evil.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    evil.chmod(0o755)
    Path('file.txt').write_text('a\nb\n')
    rc = compression.run({'command': 'wc -l file.txt', 'shell': str(evil)})
    assert rc == 0
    assert not marker.exists()


def test_rewrite_uses_default_shell_not_requested():
    result = compression.rewrite(_payload('git status', shell='definitely-not-a-shell'))
    assert result is not None
    rewritten = result['hookSpecificOutput']['updatedInput']['command']
    assert 'definitely-not-a-shell' not in rewritten
    assert sys.executable in rewritten


# --------------------------------------------------------------------------- #
# Hole (b): arbitrary-path / sensitive reads must NOT be auto-allowed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('command', [
    'tail ~/.ssh/id_rsa',
    'ls ~/.ssh',
    'tail ~/.aws/credentials',
    'tail /etc/passwd',
    'wc -l /var/log/syslog',
    'tail .env',
    'tail sub/.env.local',
    'tail ../outside.txt',
    'tail ~/key.pem',
    'tail server.key',
    'rg -f ~/.ssh/config pattern',
    'rg --hidden secret',
    'rg --no-ignore token',
    'rg pattern ~/.kube/config',
    'grep -r password .',
    'grep -rin password src',
    'ls *.pem',
    'Get-Content C:\\Windows\\system32\\config\\sam',
    "Get-Content -LiteralPath 'C:\\secret.txt'",
    'Get-ChildItem -Recurse',
    'Get-ChildItem -Force',
])
def test_sensitive_or_outside_reads_are_not_rewritten_or_allowed(command):
    """Codex must see the literal command and apply its own consent: no
    updatedInput wrapper, no permissionDecision from us."""
    assert compression.rewrite(_payload(command)) is None


@pytest.mark.parametrize('command', [
    'git status', 'git log --oneline', 'ls', 'ls src',
    'wc -l file.txt', 'tail -n 5 file.txt', 'tail file.txt',
    'rg pattern src', 'rg -e pattern file.txt', 'Get-Content file.txt',
])
def test_confined_inspection_gets_allow_and_rewrite(command):
    Path('file.txt').write_text('x\n')
    Path('src').mkdir(exist_ok=True)
    (Path('src') / 'a.py').write_text('pass\n')
    result = compression.rewrite(_payload(command))
    assert result is not None, command
    output = result['hookSpecificOutput']
    assert output['permissionDecision'] == 'allow'
    assert output['hookEventName'] == 'PreToolUse'
    assert '--run' in output['updatedInput']['command']


def test_run_revalidates_at_exec_time(tmp_path, capsys):
    """--run re-checks eligibility AND confinement: a plan forged for a
    sensitive read is refused even though it decodes fine."""
    assert compression.run({'command': 'rm -rf x'}) == 2
    assert compression.run({'command': 'tail /etc/passwd'}) == 2
    assert compression.run({'command': 'tail ~/.ssh/id_rsa'}) == 2
    assert compression.run({'command': 'tail .env'}) == 2
    err = capsys.readouterr().err
    assert 'not eligible' in err


def test_run_malformed_plan_fails_clean():
    script = SCRIPTS / 'codex_command_compress.py'
    result = subprocess.run([sys.executable, str(script), '--run', 'not-base64!!!'],
                            capture_output=True, timeout=30)
    assert result.returncode == 2


# --------------------------------------------------------------------------- #
# Exec path round-trip (posix end-to-end)
# --------------------------------------------------------------------------- #

def _dirty_git_repo(path):
    subprocess.run(['git', 'init', '-q'], cwd=path, check=True)
    files = []
    for i in range(300):
        f = path / f'file_{i}.py'
        f.write_text('x = 1\n')
        files.append(str(f))
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t',
                    'add', '-A'], cwd=path, check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t',
                    'commit', '-q', '-m', 'base'], cwd=path, check=True)
    for i in range(300):
        (path / f'file_{i}.py').write_text('x = 2\n')


@pytest.mark.skipif(os.name == 'nt', reason='posix shell path')
def test_rewritten_command_executes_and_compresses(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _dirty_git_repo(repo)
    monkeypatch.chdir(repo)
    shell = shutil.which('bash') or shutil.which('sh')
    result = compression.rewrite(_payload('git status', shell='/nonexistent/injected'))
    assert result is not None
    rewritten = result['hookSpecificOutput']['updatedInput']['command']
    env = {**os.environ, 'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(tmp_path / 'data')}
    proc = subprocess.run([shell, '-c', rewritten], capture_output=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    raw = subprocess.run(['git', 'status'], cwd=repo, capture_output=True).stdout
    assert len(proc.stdout) < len(raw)
    assert b'full command output saved' in proc.stdout
    archives = list((tmp_path / 'data/codex-command-output').glob('*.txt'))
    assert len(archives) == 1 and b'file_0.py' in archives[0].read_bytes()


@pytest.mark.skipif(os.name == 'nt', reason='posix shell path')
def test_rewritten_command_failure_streams_verbatim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shell = shutil.which('bash') or shutil.which('sh')
    result = compression.rewrite(_payload('wc -l missing-file.txt'))
    assert result is not None
    rewritten = result['hookSpecificOutput']['updatedInput']['command']
    proc = subprocess.run([shell, '-c', rewritten], capture_output=True, timeout=30)
    assert proc.returncode != 0
    assert proc.stderr
    assert b'full command output saved' not in proc.stdout


# --------------------------------------------------------------------------- #
# Item 19: legacy launcher decode diagnostic + version-normalized signature
# --------------------------------------------------------------------------- #

def _legacy_base64_command(code: str) -> str:
    """Construct a command in the retired (pre-#183) base64 -c shape."""
    blob = base64.b64encode(code.encode()).decode()
    return (
        'python -c "'
        + "exec(__import__('base64').b64decode('" + blob + "'))"
        + '" token-optimizer/scripts/windows-launcher'
    )


def test_decode_launcher_reads_legacy_base64_commands_only(tmp_path):
    """--decode-launcher remains the audit channel for hooks.json entries
    baked by the retired base64 launcher. Launcher-file commands carry no
    payload: they are self-auditing plain source, so decode returns None."""
    code = "import os\nos.environ['TOKEN_OPTIMIZER_RUNTIME'] = 'codex'\n"
    assert installer.decode_launcher_command(_legacy_base64_command(code)) == code
    assert installer.decode_launcher_command('echo hello') is None
    command = installer._windows_launcher_command(
        tmp_path / '5.9.0', 'hooks/run.py', ['hooks/stop_runner.py'], {'TO_TEST': 'v'})
    assert installer.decode_launcher_command(command) is None


def test_launcher_signature_normalizes_version_only(tmp_path):
    cmd_a = installer._windows_launcher_command(tmp_path / '5.9.0', 'hooks/run.py', ['a'], {'K': '1'})
    cmd_b = installer._windows_launcher_command(tmp_path / '5.10.0', 'hooks/run.py', ['a'], {'K': '1'})
    assert cmd_a != cmd_b  # baked version differs
    assert installer._launcher_signature(cmd_a) == installer._launcher_signature(cmd_b)
    # Different env / script / args / parent → different signature → review.
    changed_env = installer._windows_launcher_command(tmp_path / '5.9.0', 'hooks/run.py', ['a'], {'K': '2'})
    changed_script = installer._windows_launcher_command(tmp_path / '5.9.0', 'hooks/other.py', ['a'], {'K': '1'})
    other_parent = installer._windows_launcher_command(tmp_path / 'elsewhere' / '5.9.0', 'hooks/run.py', ['a'], {'K': '1'})
    for other in (changed_env, changed_script, other_parent):
        assert installer._launcher_signature(other) != installer._launcher_signature(cmd_a)


def test_launcher_signature_rejects_tampered_commands(tmp_path):
    """Any deviation from the generated command text -- a swapped blob in a
    legacy command, an edited env value or appended payload in a
    launcher-file command -- cannot pass as equivalent: signatures compare
    verbatim outside the normalized --baked-root version leaf, so tampering
    triggers the trust review it should."""
    legacy = _legacy_base64_command("print('a')")
    tampered_legacy = _legacy_base64_command("print('evil')")
    assert installer._launcher_signature(tampered_legacy) != installer._launcher_signature(legacy)

    command = installer._windows_launcher_command(tmp_path / '5.9.0', 'hooks/run.py', [], {'K': '1'})
    assert installer._launcher_signature(command.replace('K=1', 'K=2')) \
        != installer._launcher_signature(command)
    assert installer._launcher_signature(command + ' & echo hi') \
        != installer._launcher_signature(command)
    # A swapped baked-root leaf that is not semver-shaped is not normalized.
    assert installer._launcher_signature(command.replace('5.9.0', 'tampered')) \
        != installer._launcher_signature(command)
    # Non-semver roots and foreign commands compare verbatim.
    assert installer._launcher_signature('echo hi') == 'echo hi'


def test_install_preserves_equivalent_version_resolver_but_not_changed_logic(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer, '_repo_root', lambda: tmp_path / '5.13.8')
    old = installer._managed_hooks(enable_prompt_hooks=True)
    monkeypatch.setattr(installer, '_repo_root', lambda: tmp_path / '5.13.11')
    merged = installer._merge_hooks({'hooks': old}, enable_prompt_hooks=True)['hooks']
    assert merged == old
    old['Stop'][0]['hooks'][0]['command'] += ' changed'
    merged = installer._merge_hooks({'hooks': old}, enable_prompt_hooks=True)['hooks']
    assert not merged['Stop'][0]['hooks'][0]['command'].endswith(' changed')


def test_decode_launcher_cli(tmp_path):
    script = SCRIPTS / 'codex_install.py'
    legacy = _legacy_base64_command("import runpy\n")
    ok = subprocess.run([sys.executable, str(script), '--decode-launcher', legacy],
                        capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0 and 'runpy' in ok.stdout
    # Launcher-file shape: nothing embedded; the CLI points at the plain
    # source instead of failing.
    command = installer._windows_launcher_command(tmp_path / '5.9.0', 'hooks/run.py', [], {})
    self_audit = subprocess.run([sys.executable, str(script), '--decode-launcher', command],
                                capture_output=True, text=True, timeout=30)
    assert self_audit.returncode == 0 and 'nothing embedded' in self_audit.stdout
    bad = subprocess.run([sys.executable, str(script), '--decode-launcher', 'echo hi'],
                         capture_output=True, text=True, timeout=30)
    assert bad.returncode == 1


def test_default_shell_skips_unreadable_path_entries(tmp_path, monkeypatch):
    """An unreadable PATH entry (e.g. another user's private bin dir on a
    shared machine) must not crash shell resolution: a PermissionError from
    stat is treated like any other non-match, and resolution moves on."""
    blocked = tmp_path / 'blocked'
    blocked.mkdir()
    (blocked / 'bash').write_text('#!/bin/sh\n', encoding='utf-8')
    blocked.chmod(0o000)
    monkeypatch.setenv('PATH', str(blocked))
    try:
        assert compression._default_shell() is None
    finally:
        blocked.chmod(0o700)


# --------------------------------------------------------------------------- #
# Item 23: empty Codex hooks manifest
# --------------------------------------------------------------------------- #

def test_codex_manifest_does_not_load_claude_hook_bundle():
    manifest = json.loads((SCRIPTS.parents[2] / '.codex-plugin/plugin.json').read_text())
    assert manifest['hooks'] == './hooks/codex-hooks.json'
    assert json.loads((SCRIPTS.parents[2] / manifest['hooks']).read_text())['hooks'] == {}


# --------------------------------------------------------------------------- #
# Torture gauntlet: subprocess timeout + spawn failure handling
# --------------------------------------------------------------------------- #

def test_run_handles_spawn_failure(monkeypatch, capsys, tmp_path):
    """HIGH: subprocess.run spawn failure (OSError) must return 127, not
    crash with a Python traceback that hides the user's command."""
    import codex_command_compress
    # Set up a git repo so 'git status' is eligible
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    plan = {'command': 'git status', 'session_id': None, 'model': None}
    monkeypatch.setattr(codex_command_compress, '_default_shell', lambda: '/bin/sh')
    def fake_run(*args, **kwargs):
        raise OSError('No such file or directory')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    rc = codex_command_compress.run(plan)
    assert rc == 127, f"expected 127 on spawn failure, got {rc}"
    captured = capsys.readouterr()
    assert 'failed to spawn' in captured.err.lower()


def test_run_handles_timeout(monkeypatch, capsys, tmp_path):
    """HIGH: subprocess.run timeout must return 124 and stream captured
    output, not hang indefinitely or crash."""
    import codex_command_compress
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    plan = {'command': 'git status', 'session_id': None, 'model': None}
    monkeypatch.setattr(codex_command_compress, '_default_shell', lambda: '/bin/sh')
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get('timeout', 6))
    monkeypatch.setattr(subprocess, 'run', fake_run)
    rc = codex_command_compress.run(plan)
    assert rc == 124, f"expected 124 on timeout, got {rc}"
    captured = capsys.readouterr()
    assert 'timed out' in captured.err.lower()
