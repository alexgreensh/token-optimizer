"""Execute generated marketplace hooks under native cmd.exe, not CRT quoting."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
import codex_install as ci


REPO = Path(__file__).resolve().parents[1]


def _install_test_launcher(base: Path) -> Path:
    """Place the canonical launcher where install() would: next to the
    version dirs, at the stable path the generated command invokes."""
    target = base / 'windows-launcher.py'
    shutil.copyfile(REPO / 'hooks' / 'windows-launcher.py', target)
    return target


@pytest.mark.skipif(sys.platform != 'win32', reason='requires native cmd.exe')
@pytest.mark.parametrize('upgrade', [False, True])
def test_marketplace_command_runs_with_stdin_and_environment(tmp_path, monkeypatch, upgrade):
    base = tmp_path / "plugin space & (test) ! apostrophe'"
    root = base / '5.9.0'
    for version in ('5.9.0', '5.10.0', 'latest'):
        hooks = base / version / 'hooks'
        hooks.mkdir(parents=True)
        (hooks / 'run.py').write_text(
            "import json, os, sys\n"
            "print(json.dumps(dict(root=os.environ['TOKEN_OPTIMIZER_RUNTIME_ROOT'], "
            "runtime=os.environ['TOKEN_OPTIMIZER_RUNTIME'], extra=os.environ['TO_TEST'], "
            "args=sys.argv[1:], stdin=sys.stdin.read())))\n", encoding='utf-8')
    _install_test_launcher(base)
    monkeypatch.setattr(ci, '_repo_root', lambda: root)
    # Metachar coverage stays within what a real generated command can carry:
    # Windows forbids '"' in file names and the generator's own args/env
    # never contain one (list2cmdline escapes it as \", which cmd.exe does
    # NOT parse), and exactly ONE '%' may appear in the whole /C line -- cmd
    # expands %name% pairs across the line even inside quotes, so a second
    # '%' would pair with the first and eat the text between them.
    argument = 'space & pipe| percent% bang! caret^'
    env_value = 'space & pipe| bang! caret^'
    command = ci._hook_command('hooks/test.py', argument, extra_env={'TO_TEST': env_value})
    assert ci._is_token_optimizer_group({'command': command})
    if upgrade:
        (root / 'hooks/run.py').unlink()
        (root / 'hooks').rmdir()
        root.rmdir()
    # A list would apply CRT escaping (backslash-double-quote), which cmd.exe
    # does not understand. Pass the raw /C command line used by a shell.
    result = subprocess.run(
        f'cmd.exe /d /s /c "{command}"', input='{"test": true}',
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert Path(data['root']) == base / '5.10.0'
    assert data['runtime'] == 'codex'
    assert data['extra'] == env_value
    assert data['args'] == ['hooks/test.py', argument]
    assert data['stdin'] == '{"test": true}'


def test_legacy_windows_groups_are_replaced(monkeypatch):
    # The real generated 5.13.x legacy shape: set-assignment + a runner path
    # built through the FOR variable. Both anchors together mark it as ours.
    old = {'hooks': [{'command': 'for /f "delims=" %R in (\'resolver\') do '
                                 '@set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\old\\%R" && '
                                 'python "C:\\old\\%R\\hooks\\run.py" hooks/stop_runner.py'}]}
    own = {'hooks': [{'command': 'echo my hook'}]}
    monkeypatch.setattr(ci, '_managed_hooks', lambda **kwargs: {'Stop': [own]})
    merged = ci._merge_hooks({'hooks': {'SessionStart': [old, own]}})
    assert merged['hooks']['SessionStart'] == [own]
    assert ci._remove_hooks({'hooks': {'SessionStart': [old, own]}}) == {
        'hooks': {'SessionStart': [own]}}


def test_foreign_hook_referencing_our_env_var_is_untouched(monkeypatch):
    # A user's own hook that merely mentions TOKEN_OPTIMIZER_RUNTIME_ROOT
    # (without the generated runner signature) must never be swept up by
    # reinstall/uninstall -- the env-var name alone is not ownership.
    foreign = {'hooks': [{'command': 'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\mine" && python my_runner.py'}]}
    own = {'hooks': [{'command': 'echo my hook'}]}
    monkeypatch.setattr(ci, '_managed_hooks', lambda **kwargs: {'Stop': [own]})
    merged = ci._merge_hooks({'hooks': {'SessionStart': [foreign, own]}})
    assert merged['hooks']['SessionStart'] == [foreign, own]
    assert ci._remove_hooks({'hooks': {'SessionStart': [foreign, own]}}) == {
        'hooks': {'SessionStart': [foreign, own]}}


@pytest.mark.skipif(sys.platform != 'win32', reason='requires native cmd.exe')
def test_quiet_command_executes_without_output(tmp_path, monkeypatch):
    root = tmp_path / '5.0.0'
    hooks = root / 'hooks'
    hooks.mkdir(parents=True)
    marker = root / 'ran'
    (hooks / 'run.py').write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch(); print('noise')",
        encoding='utf-8')
    _install_test_launcher(tmp_path)
    monkeypatch.setattr(ci, '_repo_root', lambda: root)
    command = ci._hook_command('hooks/stop_runner.py', redirect_quiet=True)
    result = subprocess.run(f'cmd.exe /d /s /c "{command}"',
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert result.stdout == result.stderr == ''
