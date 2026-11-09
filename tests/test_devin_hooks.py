"""Automatic Devin compression: gates, binding, hook mode and config lifecycle.

Devin reads hook exit 2 as "block", permission-checks the REWRITTEN command,
and loads global hooks from the "hooks" key of ~/.config/devin/config.json.
These tests pin the contract that makes the rewrite safe: only commands the
user's own Devin rules already run without a prompt are rewritten, only the
exact bound wrapper is approved, once, and every other hook and key in the
user's config survives install, refresh, uninstall and purge.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Devin adapter requires POSIX')

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

INTERPRETER = '/usr/bin/python3'
ENTRY = '/home/u/.config/devin/token-optimizer/plugin/devin_compress.py'
USER_HOOK = {'matcher': '^read$', 'hooks': [{'type': 'command', 'command': 'echo user-hook'}]}


def _rules(allow=(), ask=(), deny=()):
    return {'allow': list(allow), 'ask': list(ask), 'deny': list(deny)}


# ── Rule matcher ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('rule,expected', [
    ('Exec(git log)', ['git', 'log']),
    ('exec(git)', ['git']),
    ('Exec', []),
    ('Exec()', []),
    ('Exec(npm *)', []),
    ('Write(/tmp)', None),
    ('Fetch(domain:*)', None),
    ('mcp__x__*', None),
])
def test_rule_prefix(rule, expected):
    import devin_hooks as h
    assert h._rule_prefix(rule) == expected


@pytest.mark.parametrize('command,rules,expected', [
    ('git log -n 5', _rules(allow=['Exec(git log)']), True),
    ('git log -n 5', _rules(allow=['Exec(git)']), True),
    ('grep -rn foo .', _rules(allow=['Exec(grep)']), True),
    # No allow rule: Devin would prompt, so no rewrite.
    ('git log -n 5', _rules(), False),
    ('git log -n 5', _rules(allow=['Exec(git status)']), False),
    # A wildcard allow is never treated as proof.
    ('git log -n 5', _rules(allow=['Exec(git *)']), False),
    # Any deny/ask on the command, on everything, or on the interpreter wins.
    ('git log -n 5', _rules(allow=['Exec(git)'], ask=['Exec(git log)']), False),
    ('git log -n 5', _rules(allow=['Exec(git)'], deny=['Exec(git)']), False),
    ('git log -n 5', _rules(allow=['Exec(git)'], ask=['Exec']), False),
    ('git log -n 5', _rules(allow=['Exec(git)'], ask=['Exec(python3)']), False),
    ('git log -n 5', _rules(allow=['Exec(git)'], deny=['Exec(' + INTERPRETER + ')']), False),
    # Unrelated deny rules do not block.
    ('git log -n 5', _rules(allow=['Exec(git)'], deny=['Exec(rm)', 'Write(/etc)']), True),
    # Shell syntax, env prefixes, writes and non-whitelisted commands never.
    ('git log | head', _rules(allow=['Exec(git)']), False),
    ('git log && rm x', _rules(allow=['Exec(git)']), False),
    ('git log $(id)', _rules(allow=['Exec(git)']), False),
    ('FOO=1 git log', _rules(allow=['Exec(git)']), False),
    ('git push', _rules(allow=['Exec(git)']), False),
    ('rm -rf build', _rules(allow=['Exec(rm)']), False),
    ('bash -c ls', _rules(allow=['Exec(bash)']), False),
    ('', _rules(allow=['Exec']), False),
])
def test_eligible(command, rules, expected):
    import devin_hooks as h
    assert h.eligible(command, rules, INTERPRETER) is expected


def test_eligible_respects_compression_switch(monkeypatch):
    import devin_hooks as h
    monkeypatch.setenv('TOKEN_OPTIMIZER_BASH_COMPRESS', '0')
    assert h.eligible('git log', _rules(allow=['Exec(git)']), INTERPRETER) is False


def test_project_rules_are_read_up_to_repo_root(tmp_path):
    import devin_hooks as h
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    (repo / '.devin').mkdir()
    (repo / '.devin/config.json').write_text(json.dumps({'permissions': {'ask': ['Exec(git log)']}}))
    (tmp_path / '.devin').mkdir()
    (tmp_path / '.devin/config.json').write_text(json.dumps({'permissions': {'allow': ['Exec(git)']}}))
    nested = repo / 'a/b'
    nested.mkdir(parents=True)
    user = tmp_path / 'user.json'
    user.write_text(json.dumps({'permissions': {'allow': ['Exec(git)']}}))
    files = h.rule_files(user, nested)
    assert repo / '.devin/config.json' in files
    assert tmp_path / '.devin/config.json' not in files  # walk stops at the repo root
    rules = h.load_rules(files)
    assert h.eligible('git log', rules, INTERPRETER) is False


@pytest.mark.parametrize('content', ['{not json', '[]', '{"permissions": []}', '{"permissions": {"allow": "Exec"}}'])
def test_unreadable_rule_file_decides_nothing(tmp_path, content):
    import devin_hooks as h
    bad = tmp_path / 'config.json'
    bad.write_text(content)
    with pytest.raises(h.RulesUnknown):
        h.load_rules([bad])


# ── Wrapper strings ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('argv', [['git', 'log', '-n', '5'], ['grep', '-rn', 'two words', '.'],
                                  ['find', '.', '-name', "it's.py"], ['ls', '-la', 'dir with space']])
def test_wrapper_round_trip(argv):
    import devin_hooks as h
    wrapper = h.build_wrapper(INTERPRETER, ENTRY, 'session-1', argv)
    assert h.parse_wrapper(wrapper, INTERPRETER, ENTRY) == argv


@pytest.mark.parametrize('mutate', [
    lambda w: w + ' ',
    lambda w: w.replace(' run ', ' prepare '),
    lambda w: w.replace(INTERPRETER, '/tmp/python3'),
    lambda w: w.replace(ENTRY, '/tmp/devin_compress.py'),
    lambda w: w.replace(' -I ', ' '),
    lambda w: w.replace("'two words'", '"two words"'),
    lambda w: w + ' ; id',
    lambda w: 'sudo ' + w,
])
def test_wrapper_parse_refuses_anything_not_byte_exact(mutate):
    import devin_hooks as h
    wrapper = h.build_wrapper(INTERPRETER, ENTRY, 's', ['grep', '-rn', 'two words', '.'])
    assert h.parse_wrapper(mutate(wrapper), INTERPRETER, ENTRY) is None


def test_session_label_is_sanitized():
    import devin_hooks as h
    assert h.session_label('../../x') == h.DEFAULT_SESSION
    assert h.session_label(None) == h.DEFAULT_SESSION
    assert h.session_label('abc-123') == 'abc-123'


# ── One-time binding ────────────────────────────────────────────────────────

def test_binding_is_single_use_and_tool_use_id_bound(tmp_path):
    import devin_hooks as h
    path = tmp_path / h.BINDINGS
    wrapper = h.build_wrapper(INTERPRETER, ENTRY, 's', ['git', 'log'])
    assert h.bind(path, 'toolu_1', wrapper)
    assert not h.consume(path, 'toolu_2', wrapper)          # other tool call
    assert not h.consume(path, 'toolu_1', wrapper + ' x')   # other command (and the binding is spent)
    assert h.bind(path, 'toolu_1', wrapper)
    assert h.consume(path, 'toolu_1', wrapper)
    assert not h.consume(path, 'toolu_1', wrapper)          # second use
    assert oct(path.stat().st_mode & 0o777) == '0o600'


@pytest.mark.parametrize('bad_id', [None, '', 'a b', '../x', 'x' * 300, 42])
def test_binding_refuses_malformed_ids(tmp_path, bad_id):
    import devin_hooks as h
    path = tmp_path / h.BINDINGS
    assert not h.bind(path, bad_id, 'cmd')
    assert not h.consume(path, bad_id, 'cmd')


def test_binding_expires(tmp_path, monkeypatch):
    import devin_hooks as h
    import time
    path = tmp_path / h.BINDINGS
    assert h.bind(path, 'toolu_1', 'cmd')
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real() + h.BINDING_SECONDS + 5)
    assert not h.consume(path, 'toolu_1', 'cmd')


# ── Config merge helpers ────────────────────────────────────────────────────

def test_merge_and_remove_keep_user_hooks_and_keys():
    import devin_hooks as h
    entries = h.owned_entries(INTERPRETER, ENTRY)
    config = {'version': 1, 'permissions': {'allow': ['Exec(git)']}, 'hooks': {'PreToolUse': [USER_HOOK]}}
    merged = h.merge_hooks(config, entries)
    assert merged['hooks']['PreToolUse'] == [USER_HOOK, entries['PreToolUse']]
    assert h.merge_hooks(merged, entries) == merged  # idempotent
    removed, preserved = h.remove_hooks(merged, entries)
    assert removed == config and preserved == []


def test_hook_command_never_blocks_when_payload_is_missing(tmp_path):
    import devin_hooks as h
    command = h.hook_command(sys.executable, str(tmp_path / 'missing.py'), 'PreToolUse')
    run = subprocess.run(['/bin/sh', '-c', command], input=b'{}', capture_output=True, timeout=30)
    assert run.returncode == 0 and run.stdout == b''


# ── Installed hook mode ─────────────────────────────────────────────────────

def _devin_home():
    import devin_state
    return devin_state.home()


def _write_config(config):
    path = _devin_home() / 'config.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + '\n')
    path.chmod(0o600)
    return path


def _install_auto(config=None, allow=('Exec(git)', 'Exec(grep)', 'Exec(ls)')):
    import devin_install as d
    path = _write_config(config if config is not None else {'permissions': {'allow': list(allow)}})
    d.install(mode='auto')
    return path


def _entries():
    import devin_install as d
    return json.loads((d.state.data_dir() / 'hooks.json').read_text())['entries']


def _call(event, command, tool_use_id='toolu_test', cwd=None, tool='exec', raw=None):
    entry = _entries()['PreToolUse' if event == 'pre' else 'PermissionRequest']
    payload = raw if raw is not None else json.dumps({
        'hook_event_name': 'PreToolUse' if event == 'pre' else 'PermissionRequest',
        'tool_name': tool, 'tool_input': {'command': command},
        'tool_use_id': tool_use_id, 'session_id': 'sess-1'}).encode()
    run = subprocess.run(['/bin/sh', '-c', entry['hooks'][0]['command']], input=payload,
                         cwd=cwd or _devin_home(), capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout) if run.stdout.strip() else None


def test_hook_round_trip_rewrites_and_approves_once(tmp_path):
    _install_auto()
    decision = _call('pre', 'git log -n 3', cwd=tmp_path)
    wrapper = decision['hookSpecificOutput']['updatedInput']['command']
    assert ' run --session sess-1 -- git log -n 3' in wrapper
    assert decision['hookSpecificOutput'].get('permissionDecision') is None  # never self-approves here
    assert _call('perm', wrapper, cwd=tmp_path)['decision'] == 'approve'
    assert _call('perm', wrapper, cwd=tmp_path) is None                     # single use
    assert _call('perm', wrapper, tool_use_id='toolu_other', cwd=tmp_path) is None


def test_prompted_command_still_prompts(tmp_path):
    """A command Devin would ask about is never rewritten and never approved."""
    _install_auto(allow=('Exec(grep)',))
    assert _call('pre', 'git log -n 3', cwd=tmp_path) is None           # no allow rule: untouched
    assert _call('perm', 'git log -n 3', cwd=tmp_path) is None          # no decision on the raw command
    assert _call('perm', 'rm -rf build', cwd=tmp_path) is None
    project = tmp_path / 'proj'
    (project / '.git').mkdir(parents=True)
    (project / '.devin').mkdir()
    (project / '.devin/config.json').write_text(json.dumps({'permissions': {'ask': ['Exec(grep)']}}))
    assert _call('pre', 'grep -rn x .', cwd=project) is None           # project ask rule wins


def test_forged_wrapper_without_binding_still_prompts(tmp_path):
    import devin_hooks as h
    _install_auto()
    command = _entries()['PreToolUse']['hooks'][0]['command']
    interpreter, _, entry = shlex.split(command)[:3]
    forged = h.build_wrapper(interpreter, entry, 'sess-1', ['git', 'log'])
    assert _call('perm', forged, cwd=tmp_path) is None


@pytest.mark.parametrize('raw', [b'', b'not json', b'[]', b'{"tool_name": "exec"}', b'x' * (1024 * 1024 + 10)],
                         ids=['empty', 'text', 'array', 'no-input', 'oversize'])
def test_hook_mode_silent_on_garbage(raw):
    _install_auto()
    assert _call('pre', None, raw=raw) is None
    assert _call('perm', None, raw=raw) is None


def test_hook_ignores_other_tools():
    _install_auto()
    assert _call('pre', 'git log', tool='read') is None


@pytest.mark.parametrize('breakage', ['edited_payload', 'missing_payload', 'no_consent', 'no_attestation'])
def test_hook_mode_never_exits_2_on_a_broken_install(breakage):
    import devin_install as d
    _install_auto()
    entries = _entries()
    plugin = d.state.data_dir() / 'plugin'
    if breakage == 'edited_payload':
        (plugin / 'devin_state.py').write_text('raise SystemExit(2)\n')
    elif breakage == 'missing_payload':
        (plugin / 'devin_compress.py').unlink()
    elif breakage == 'no_consent':
        (d.state.data_dir() / 'consent.json').unlink()
    else:
        record = json.loads((d.state.data_dir() / 'hooks.json').read_text())
        record['org_policy'] = None
        (d.state.data_dir() / 'hooks.json').write_text(json.dumps(record))
    for event in ('PreToolUse', 'PermissionRequest'):
        payload = json.dumps({'tool_name': 'exec', 'tool_input': {'command': 'git log'},
                              'tool_use_id': 'toolu_1', 'session_id': 's'}).encode()
        run = subprocess.run(['/bin/sh', '-c', entries[event]['hooks'][0]['command']], input=payload,
                             capture_output=True, timeout=60)
        assert run.returncode == 0 and run.stdout == b'', (breakage, run)


def test_runner_refuses_ineligible_argv_and_runs_nothing(tmp_path):
    import devin_install as d
    _install_auto()
    victim = tmp_path / 'keep.txt'
    victim.write_text('x')
    run = subprocess.run([sys.executable, '-I', str(d.state.data_dir() / 'plugin/devin_compress.py'),
                          'run', '--session', 's', '--', 'rm', str(victim)], capture_output=True, timeout=60)
    assert run.returncode == 126 and victim.exists()


def test_runner_compresses_and_recovery_returns_the_original(tmp_path):
    import devin_install as d
    _install_auto()
    tmp_path = tmp_path / 'logs'
    tmp_path.mkdir()
    for index in range(30):
        (tmp_path / ('worker%02d.log' % index)).write_text(
            ''.join('worker %d: request ok, latency normal\n' % (i % 5) for i in range(100)))
    raw = subprocess.run(['grep', '-rn', 'worker', '.'], cwd=tmp_path, capture_output=True).stdout
    decision = _call('pre', 'grep -rn worker .', cwd=tmp_path)
    wrapper = decision['hookSpecificOutput']['updatedInput']['command']
    run = subprocess.run(shlex.split(wrapper), cwd=tmp_path, capture_output=True, timeout=60)
    assert run.returncode == 0 and len(run.stdout) < len(raw) // 2
    pointer = run.stdout.decode().splitlines()[0]
    assert pointer.startswith('[Token Optimizer capture ID: capture-')
    recover = pointer.split('recover with ', 1)[1].split(';', 1)[0]
    again = subprocess.run(shlex.split(recover), capture_output=True, timeout=60)
    assert again.returncode == 0
    body = again.stdout.split(b'[Begin untrusted recovered stdout]\n', 1)[1].rsplit(b'\n[End untrusted recovered stdout]', 1)[0]
    assert body == raw
    index = json.loads((d.state.data_dir() / 'auto-captures.json').read_text())
    assert len(index) == 1


def test_runner_keeps_exit_code_and_raw_failure_output(tmp_path):
    _install_auto()
    decision = _call('pre', 'git log -n 1', cwd=tmp_path)  # tmp_path is not a repository
    wrapper = decision['hookSpecificOutput']['updatedInput']['command']
    raw = subprocess.run(['git', 'log', '-n', '1'], cwd=tmp_path, capture_output=True)
    run = subprocess.run(shlex.split(wrapper), cwd=tmp_path, capture_output=True, timeout=60)
    assert run.returncode == raw.returncode != 0
    assert run.stdout == raw.stdout and run.stderr == raw.stderr


@pytest.mark.parametrize('command', ['git status --short', 'git status -sb', 'git status --porcelain=v2',
                                     'git status -z', 'git log -z', 'git diff --name-only -z'])
def test_machine_format_git_is_never_rewritten(tmp_path, command):
    _install_auto()
    assert _call('pre', command, cwd=tmp_path) is None


def test_long_git_status_is_rewritten(tmp_path):
    _install_auto()
    assert _call('pre', 'git status', cwd=tmp_path) is not None


# ── config.json lifecycle cells ─────────────────────────────────────────────

def _quiet(fn, *args, **kwargs):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def test_cell_absent_config_created_private_then_unwired():
    import devin_install as d
    path = _devin_home() / 'config.json'
    _devin_home().mkdir(parents=True, exist_ok=True)
    assert not path.exists()
    _quiet(d.install, mode='auto')
    assert set(json.loads(path.read_text())['hooks']) == {'PreToolUse', 'PermissionRequest'}
    assert oct(path.stat().st_mode & 0o777) == '0o600'
    _quiet(d.uninstall)
    assert json.loads(path.read_text()) == {}


def test_cell_user_hooks_and_keys_survive_every_command():
    import devin_install as d
    original = {'version': 1, 'theme_mode': 'dark', 'permissions': {'allow': ['Exec(git)']},
                'hooks': {'PreToolUse': [USER_HOOK], 'Stop': [USER_HOOK]}}
    path = _write_config(original)
    before = path.read_bytes()
    path.chmod(0o640)
    _quiet(d.install, mode='auto')
    once = path.read_bytes()
    _quiet(d.install, mode='auto')
    assert path.read_bytes() == once                       # reinstall is idempotent
    _quiet(d.install)
    assert path.read_bytes() == once                       # a plain refresh keeps automatic mode
    assert oct(path.stat().st_mode & 0o777) == '0o640'     # mode kept
    config = json.loads(once)
    assert config['hooks']['PreToolUse'][0] == USER_HOOK and config['hooks']['Stop'] == [USER_HOOK]
    _quiet(d.uninstall)
    assert path.read_bytes() == before                     # byte-identical round trip


def test_cell_manual_flag_unwires_and_keeps_skill():
    import devin_install as d
    path = _install_auto({'permissions': {'allow': ['Exec(git)']}, 'hooks': {'PreToolUse': [USER_HOOK]}})
    _quiet(d.install, mode='manual')
    assert json.loads(path.read_text())['hooks'] == {'PreToolUse': [USER_HOOK]}
    assert not (d.state.data_dir() / 'hooks.json').exists()
    assert (d.skill_dir() / 'SKILL.md').is_file()
    _quiet(d.install)
    assert 'PermissionRequest' not in json.loads(path.read_text())['hooks']  # stays manual


def test_cell_edited_entry_is_kept_and_named():
    import devin_install as d
    path = _install_auto()
    config = json.loads(path.read_text())
    config['hooks']['PreToolUse'][-1]['hooks'][0]['timeout'] = 30
    path.write_text(json.dumps(config, indent=2))
    out = _quiet(d.uninstall)
    assert 'Kept edited Token Optimizer hook entries' in out and 'PreToolUse' in out
    after = json.loads(path.read_text())
    assert after['hooks']['PreToolUse'][-1]['hooks'][0]['timeout'] == 30
    assert 'PermissionRequest' not in after['hooks']


def test_cell_python_change_replaces_only_the_stale_entries():
    import devin_hooks as h
    import devin_install as d
    path = _install_auto({'permissions': {'allow': ['Exec(git)']}, 'hooks': {'PreToolUse': [USER_HOOK]}})
    record_path = d.state.data_dir() / 'hooks.json'
    record = json.loads(record_path.read_text())
    old = h.owned_entries('/old/python3.11', str(d.state.data_dir() / 'plugin/devin_compress.py'))
    record['entries'] = old
    record_path.write_text(json.dumps(record))
    config = json.loads(path.read_text())
    config['hooks'] = h.merge_hooks({'hooks': {'PreToolUse': [USER_HOOK]}}, old)['hooks']
    path.write_text(json.dumps(config, indent=2))
    _quiet(d.install)
    hooks_now = json.loads(path.read_text())['hooks']
    assert hooks_now['PreToolUse'][0] == USER_HOOK
    assert len(hooks_now['PreToolUse']) == 2 and len(hooks_now['PermissionRequest']) == 1
    assert '/old/python3.11' not in path.read_text()
    assert json.loads(record_path.read_text())['stale'] == []


@pytest.mark.parametrize('shape', ['jsonc', 'array', 'hooks_not_object', 'symlink'])
def test_cell_uneditable_config_refuses_wiring_but_installs_skill(tmp_path, shape):
    import devin_install as d
    home = _devin_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'config.json'
    if shape == 'symlink':
        real = tmp_path / 'real-config.json'
        real.write_text('{}')
        path.symlink_to(real)
    else:
        path.write_text({'jsonc': '{\n  // comment\n  "version": 1\n}\n', 'array': '[]',
                         'hooks_not_object': '{"hooks": []}'}[shape])
    before = path.read_bytes()
    with pytest.raises(OSError, match='automatic hooks were NOT wired'):
        _quiet(d.install, mode='auto')
    assert path.read_bytes() == before
    assert (d.skill_dir() / 'SKILL.md').is_file()
    assert not (d.state.data_dir() / 'hooks.json').exists() or shape == 'hooks_not_object'


def test_cell_plain_uninstall_refuses_when_config_became_unparseable():
    import devin_install as d
    path = _install_auto()
    path.write_text('{ // broken\n')
    with pytest.raises(OSError, match='nothing was uninstalled'):
        _quiet(d.uninstall)
    assert (d.skill_dir() / 'SKILL.md').is_file() and (d.state.data_dir() / 'hooks.json').exists()


def test_cell_purge_unwires_and_removes_hook_state(tmp_path):
    import devin_install as d
    path = _install_auto({'permissions': {'allow': ['Exec(grep)']}, 'hooks': {'Stop': [USER_HOOK]}})
    log = tmp_path / 'a.log'
    log.write_text('line ok\n' * 3000)
    decision = _call('pre', 'grep -n line a.log', cwd=tmp_path)
    subprocess.run(shlex.split(decision['hookSpecificOutput']['updatedInput']['command']), cwd=tmp_path,
                   capture_output=True, timeout=60)
    _quiet(d.uninstall, purge=True)
    assert json.loads(path.read_text()) == {'permissions': {'allow': ['Exec(grep)']}, 'hooks': {'Stop': [USER_HOOK]}}
    for leaf in d.HOOK_STATE_LEAVES:
        assert not (d.state.data_dir() / leaf).exists(), leaf


def test_cell_purge_continues_when_config_is_unparseable():
    import devin_install as d
    path = _install_auto()
    path.write_text('{ // broken\n')
    out = _quiet(d.uninstall, purge=True)
    assert 'could not be removed' in out
    assert not (d.skill_dir() / 'SKILL.md').exists()
    assert path.read_text() == '{ // broken\n'


def test_cell_dry_runs_do_not_touch_config():
    import devin_install as d
    path = _write_config({'permissions': {'allow': ['Exec(git)']}})
    before = path.read_bytes()
    out = _quiet(d.install, dry_run=True, mode='auto')
    assert 'Would wire automatic compression hooks' in out and path.read_bytes() == before
    _quiet(d.install, mode='auto')
    wired = path.read_bytes()
    out = _quiet(d.uninstall, dry_run=True)
    assert 'Would remove automatic compression hooks' in out and path.read_bytes() == wired


def test_cell_upgrade_from_5_13_43_layout():
    """The previous release had no hook module or flat Tomli parser; a refresh must accept it."""
    import devin_install as d
    _write_config({'permissions': {'allow': ['Exec(git)']}})
    _quiet(d.install)
    manifest = json.loads(d.manifest_path().read_text())
    for leaf in ('plugin/devin_hooks.py', 'plugin/_tomli.py', 'plugin/credential_url_patterns.py'):
        (d.state.data_dir() / leaf).unlink()
        del manifest['files'][str(d.state.data_dir() / leaf)]
    d.manifest_path().write_text(json.dumps(manifest))
    _quiet(d.install, mode='auto')
    assert all((d.state.data_dir() / leaf).is_file() for leaf in ('plugin/devin_hooks.py', 'plugin/_tomli.py', 'plugin/credential_url_patterns.py'))
    from devin_verify import verify_install
    assert verify_install()


def test_installer_cli_flags():
    import devin_install as d
    with pytest.raises(SystemExit):
        d.main(['install', '--auto', '--manual'])
    with pytest.raises(SystemExit):
        d.main(['uninstall', '--auto'])


def test_doctor_probe_proves_the_round_trip(tmp_path):
    import devin_install as d
    _install_auto(allow=('Exec(grep)',))
    run = subprocess.run([sys.executable, str(d.state.data_dir() / 'plugin/devin_doctor.py'), '--probe'],
                         capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    auto = result['automatic']
    assert result['status'] == 'automatic' and auto['mode'] == 'automatic'
    probe = auto['probe']
    assert probe['passed'] and probe['second_request_still_prompts'] and probe['other_tool_call_still_prompts']
    assert probe['delivered_bytes'] < probe['raw_bytes']


def test_doctor_flags_missing_hook_python():
    import devin_install as d
    path = _install_auto()
    record_path = d.state.data_dir() / 'hooks.json'
    text = path.read_text()
    command = _entries()['PreToolUse']['hooks'][0]['command']
    interpreter = shlex.split(command)[0]
    path.write_text(text.replace(interpreter, '/gone/python3.99'))
    record_path.write_text(record_path.read_text().replace(interpreter, '/gone/python3.99'))
    run = subprocess.run([sys.executable, str(d.state.data_dir() / 'plugin/devin_doctor.py')],
                         capture_output=True, timeout=120)
    result = json.loads(run.stdout)
    assert run.returncode == 1 and result['automatic']['mode'] == 'broken'
    assert 'hook Python is gone' in result['automatic']['error']


# ── Review round 1 regressions ──────────────────────────────────────────────

@pytest.mark.parametrize('command', ['ls *.txt', 'ls ~', 'ls a # c', 'grep x src/*.py', 'ls file?.txt',
                                     'ls [ab].txt', 'grep --include=*.py x .'])
def test_shell_expansion_is_never_rewritten(command):
    import devin_hooks as h
    assert h.eligible(command, _rules(allow=['Exec(ls)', 'Exec(grep)']), INTERPRETER) is False


@pytest.mark.parametrize('command', ["find . -name '*.py'", 'grep -rn "a*b" .', r'ls a\*b'])
def test_quoted_specials_stay_eligible(command):
    import devin_hooks as h
    assert h.eligible(command, _rules(allow=['Exec(ls)', 'Exec(grep)', 'Exec(find)']), INTERPRETER) is True


def test_empty_argument_is_never_rewritten():
    import devin_hooks as h
    assert h.eligible('grep -n "" f', _rules(allow=['Exec(grep)']), INTERPRETER) is False


def test_same_program_ask_rule_blocks_other_flag_spellings():
    import devin_hooks as h
    assert h.eligible('grep -rn x .', _rules(allow=['Exec(grep)'], ask=['Exec(grep -r)']), INTERPRETER) is False


def test_project_allow_is_never_proof_but_project_ask_blocks(tmp_path):
    import devin_hooks as h
    user = tmp_path / 'home/.config/devin/config.json'
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({'permissions': {'allow': ['Exec(grep)']}}))
    repo = tmp_path / 'clone'
    (repo / '.git').mkdir(parents=True)
    (repo / '.devin').mkdir()
    (repo / '.devin/config.json').write_text(json.dumps({'permissions': {'allow': ['Exec(git)']}}))
    rules = h.load_effective_rules(user, repo)
    assert h.eligible('git log', rules, INTERPRETER) is False
    assert h.eligible('grep -rn x .', rules, INTERPRETER) is True
    (repo / '.devin/config.json').write_text(json.dumps({'permissions': {'ask': ['Exec(grep)']}}))
    assert h.eligible('grep -rn x .', h.load_effective_rules(user, repo), INTERPRETER) is False


def test_user_config_local_rules_are_read(tmp_path):
    import devin_hooks as h
    user = tmp_path / 'config.json'
    user.write_text(json.dumps({'permissions': {'allow': ['Exec(grep)']}}))
    (tmp_path / 'config.local.json').write_text(json.dumps({'permissions': {'deny': ['Exec(grep)']}}))
    assert h.eligible('grep -rn x .', h.load_effective_rules(user, tmp_path), INTERPRETER) is False


def test_rule_walk_stops_at_home(tmp_path, monkeypatch):
    import devin_hooks as h
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    start = tmp_path / 'home/a/b'
    start.mkdir(parents=True)
    files = h._project_rule_files(start)
    assert all(str(f).startswith(str(tmp_path / 'home')) for f in files)


def _deep_clone(tmp_path, depth):
    repo = tmp_path / 'clone'
    (repo / '.git').mkdir(parents=True, exist_ok=True)
    (repo / '.devin').mkdir(exist_ok=True)
    (repo / '.devin/config.json').write_text(json.dumps({'permissions': {'deny': ['Exec(git)']}}))
    cwd = repo.joinpath(*(['d'] * depth))
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def test_rule_walk_that_runs_out_decides_nothing(tmp_path):
    """A deny rule above the walk limit must never be read as no rules."""
    import devin_hooks as h
    user = tmp_path / 'home/.config/devin/config.json'
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({'permissions': {'allow': ['Exec(git)']}}))
    shallow = _deep_clone(tmp_path, 3)
    assert h.eligible('git log', h.load_effective_rules(user, shallow), INTERPRETER) is False
    deep = _deep_clone(tmp_path, h.MAX_WALK + 1)
    with pytest.raises(h.RulesUnknown):
        h.load_effective_rules(user, deep)
    with pytest.raises(h.RulesUnknown):
        h.load_effective_rules(user, shallow, str(deep))


def test_hook_leaves_command_alone_past_the_walk_limit(tmp_path):
    import devin_hooks as h
    _install_auto(allow=('Exec(git)',))
    deep = _deep_clone(tmp_path, h.MAX_WALK + 1)
    entry = _entries()['PreToolUse']
    payload = json.dumps({'tool_name': 'exec', 'tool_use_id': 'toolu_deep', 'session_id': 's', 'cwd': str(deep),
                          'tool_input': {'command': 'git log'}}).encode()
    run = subprocess.run(['/bin/sh', '-c', entry['hooks'][0]['command']], input=payload, cwd=deep,
                         capture_output=True, timeout=60)
    assert run.returncode == 0
    assert 'updatedInput' not in run.stdout.decode()


def test_reinstall_keeps_hook_order():
    import devin_hooks as h
    entries = h.owned_entries(INTERPRETER, ENTRY)
    config = {'hooks': {'PreToolUse': [entries['PreToolUse'], USER_HOOK]}}
    assert h.merge_hooks(config, entries)['hooks']['PreToolUse'] == [entries['PreToolUse'], USER_HOOK]


def test_rewrite_keeps_other_input_fields(tmp_path):
    _install_auto()
    entry = _entries()['PreToolUse']
    payload = json.dumps({'tool_name': 'exec', 'tool_use_id': 'toolu_f', 'session_id': 's',
                          'tool_input': {'command': 'git status', 'timeout': 30, 'run_in_background': False}}).encode()
    run = subprocess.run(['/bin/sh', '-c', entry['hooks'][0]['command']], input=payload, cwd=tmp_path,
                         capture_output=True, timeout=60)
    updated = json.loads(run.stdout)['hookSpecificOutput']['updatedInput']
    assert updated['timeout'] == 30 and updated['run_in_background'] is False and 'run --session' in updated['command']


@pytest.mark.parametrize('why', ['no_allow_rule', 'no_attestation'])
def test_runner_enforces_the_full_gate(tmp_path, why):
    """Run directly (no hook): a command the rules do not allow is never run."""
    import devin_install as d
    _install_auto(allow=('Exec(grep)',) if why == 'no_allow_rule' else ('Exec(find)', 'Exec(grep)'))
    if why == 'no_attestation':
        record = d.state.data_dir() / 'hooks.json'
        data = json.loads(record.read_text())
        data['org_policy'] = None
        record.write_text(json.dumps(data))
    target = tmp_path / 'w1'
    target.mkdir()
    (target / 'f').write_text('x')
    run = subprocess.run([sys.executable, '-I', str(d.state.data_dir() / 'plugin/devin_compress.py'),
                          'run', '--session', 's', '--', 'find', str(target), '-delete'],
                         cwd=tmp_path, capture_output=True, timeout=60)
    assert run.returncode == 126 and (target / 'f').exists()


def test_runner_forwards_sigterm_to_the_command(tmp_path):
    import signal
    import time
    import devin_install as d
    _install_auto(allow=('Exec(tail)',))
    log = tmp_path / 'live.log'
    log.write_text('start\n')
    wrapper = subprocess.Popen([sys.executable, '-I', str(d.state.data_dir() / 'plugin/devin_compress.py'),
                                'run', '--session', 's', '--', 'tail', '-f', str(log)],
                               cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    wrapper.send_signal(signal.SIGTERM)
    wrapper.wait(timeout=30)
    time.sleep(0.5)
    leftover = subprocess.run(['pgrep', '-f', 'tail -f ' + str(log)], capture_output=True)
    assert leftover.stdout.strip() == b''


def test_auto_capture_index_survives_concurrent_runs(tmp_path):
    import devin_install as d
    _install_auto(allow=('Exec(grep)',))
    work = tmp_path / 'w'
    work.mkdir()
    for index in range(30):
        (work / ('m%02d.log' % index)).write_text('match line ok\n' * 50)
    entry = str(d.state.data_dir() / 'plugin/devin_compress.py')
    procs = [subprocess.Popen([sys.executable, '-I', entry, 'run', '--session', 's', '--', 'grep', '-rn', 'match', '.'],
                              cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(16)]
    results = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
    assert all(code == 0 for _, _, code in results)
    index = json.loads((d.state.data_dir() / 'auto-captures.json').read_text())
    captures = sorted(p.name for p in (d.state.data_dir() / 'captures').iterdir() if p.name.startswith('capture-'))
    compressed = sum(out.startswith(b'[Token Optimizer capture ID:') for out, _, _ in results)
    detail = [err[:300] for _, err, _ in results if err]
    indexed = {e[0] for e in index}
    detail += [(name, sorted(os.listdir(d.state.data_dir() / 'captures' / name)))
               for name in captures if name not in indexed]
    detail.append(('compressed', compressed))
    # The invariant: every capture an automatic run keeps is indexed for
    # retention. A run that falls back to raw output (fail-open) keeps none.
    assert sorted(e[0] for e in index) == captures, detail
    assert len(captures) == compressed and compressed >= 12, (compressed, detail)


def test_uninstall_removes_the_config_backup():
    import devin_install as d
    _install_auto()
    _quiet(d.install, mode='auto')
    _quiet(d.uninstall)
    assert not (d.state.data_dir() / 'devin-config-backup.json').exists()


# ── State checks race os.replace unless made under the writers' lock ────────
# A stat racing os.replace can return the replaced inode with st_nlink 0
# (Linux and macOS both do), which private_path() refuses as a hard-link
# hazard. Each file below is only ever replaced under one lock, so every check
# of it must sit inside that lock. These tests report st_nlink 0 for a file
# whenever its lock is not held by this process, as a rival writer could be
# replacing it at that moment.

def _replaced_outside(monkeypatch, lock, files, held_by=None):
    import fcntl
    held = set()
    real_flock, real_lstat = fcntl.flock, Path.lstat
    held_by = held_by if held_by is not None else fcntl.LOCK_SH | fcntl.LOCK_EX

    def flock(fd, operation):
        real_flock(fd, operation)
        fd = fd if isinstance(fd, int) else fd.fileno()
        if operation & held_by:
            held.add(fd)
        elif operation & fcntl.LOCK_UN:
            held.discard(fd)

    def holding():
        try:
            inode = os.stat(lock).st_ino
        except FileNotFoundError:
            return False
        for fd in list(held):
            try:
                if os.fstat(fd).st_ino == inode:
                    return True
            except OSError:
                pass
            held.discard(fd)
        return False

    def lstat(self):
        info = real_lstat(self)
        if self in files and not holding():
            info = os.stat_result((info.st_mode, info.st_ino, info.st_dev, 0, info.st_uid,
                                   info.st_gid, info.st_size, info.st_atime, info.st_mtime, info.st_ctime))
        return info

    monkeypatch.setattr(fcntl, 'flock', flock)
    monkeypatch.setattr(Path, 'lstat', lstat)


def _hook_in_process(monkeypatch, cwd, tool_use_id='toolu_race'):
    import io
    import types
    import devin_compress as c
    payload = json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': 'exec',
                          'tool_input': {'command': 'grep -rn match .'}, 'cwd': str(cwd),
                          'tool_use_id': tool_use_id, 'session_id': 'sess-1'}).encode()
    monkeypatch.setattr(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(payload)))
    assert c._hook('pre-tool-use') == 0
    import devin_state as s
    bindings = s.data_dir() / 'hook-bindings.json'
    return tool_use_id in (json.loads(bindings.read_text()) if bindings.exists() else {})


def test_hook_checks_bindings_only_under_their_lock(tmp_path, monkeypatch, capfd):
    import fcntl
    import devin_state as s
    _install_auto(allow=('Exec(grep)',))
    bindings = s.data_dir() / 'hook-bindings.json'
    assert _hook_in_process(monkeypatch, tmp_path, 'toolu_first')  # the file now exists
    _replaced_outside(monkeypatch, s.data_dir() / 'hook-bindings.json.lock', {bindings}, fcntl.LOCK_EX)
    assert _hook_in_process(monkeypatch, tmp_path)


def test_hook_checks_install_state_only_under_the_install_lock(tmp_path, monkeypatch, capfd):
    import devin_state as s
    _install_auto(allow=('Exec(grep)',))
    data = s.data_dir()
    _replaced_outside(monkeypatch, data / 'install.lock', {data / 'consent.json', data / 'hooks.json'})
    assert _hook_in_process(monkeypatch, tmp_path)


@pytest.mark.parametrize('leaf', ['hooks.json', 'consent.json'])
def test_runner_checks_install_state_only_under_the_install_lock(tmp_path, monkeypatch, capfd, leaf):
    import signal
    import devin_compress as c
    import devin_state as s
    _install_auto(allow=('Exec(grep)',))
    work = tmp_path / 'w'
    work.mkdir()
    for index in range(30):
        (work / ('m%02d.log' % index)).write_text('match line ok\n' * 50)
    monkeypatch.chdir(work)
    # Never let a fallback exec replace the test process.
    monkeypatch.setattr(c, '_exec_original', lambda argv: 'exec_original')
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    capfd.readouterr()
    _replaced_outside(monkeypatch, s.data_dir() / 'install.lock', {s.data_dir() / leaf})
    try:
        code = c._run(['--session', 's', '--', 'grep', '-rn', 'match', '.'])
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)
    out = capfd.readouterr().out
    assert code == 0 and out.startswith('[Token Optimizer capture ID:'), (code, out[:200])
    index = json.loads((s.data_dir() / 'auto-captures.json').read_text())
    assert [e[0] for e in index] == [p.name for p in (s.data_dir() / 'captures').iterdir()]


@pytest.mark.parametrize('command', ['prepare', 'seal', 'recover', 'filter'])
def test_manual_commands_check_consent_only_under_the_install_lock(tmp_path, monkeypatch, capfd, command):
    import signal
    import devin_compress as c
    import devin_state as s
    _install_auto()
    capture = s.prepare_capture()
    (capture / 'stdout').write_bytes(b'repeated log line\n' * 1000)
    if command in ('recover', 'filter'):
        c.seal_capture(capture, 0)
    capfd.readouterr()
    argv = {'prepare': ['prepare'],
            'seal': ['seal', '--capture', str(capture), '--exit-code', '0'],
            'recover': ['recover', '--capture-id', capture.name, '--exit-code', '0'],
            'filter': ['--stdout', str(capture / 'stdout'), '--stderr', str(capture / 'stderr'),
                       '--exit-code', '0', '--session', 's']}[command]
    monkeypatch.setenv('TOKEN_OPTIMIZER_RUNTIME', 'devin')
    pipe = signal.getsignal(signal.SIGPIPE)
    _replaced_outside(monkeypatch, s.data_dir() / 'install.lock', {s.data_dir() / 'consent.json'})
    try:
        code = c.main(argv)
    finally:
        signal.signal(signal.SIGPIPE, pipe)
    assert code == 0, capfd.readouterr().err


# ── A stuck installer never freezes a hook or a run ─────────────────────────

_HOLD_INSTALL_LOCK = r'''
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
print('locked', flush=True)
time.sleep(float(sys.argv[2]))
'''


def _hold_install_lock(seconds):
    import devin_state as s
    holder = subprocess.Popen([sys.executable, '-I', '-c', _HOLD_INSTALL_LOCK,
                               str(s.data_dir() / 'install.lock'), str(seconds)],
                              stdout=subprocess.PIPE)
    assert holder.stdout.readline() == b'locked\n'
    return holder


def _grep_workdir(tmp_path):
    work = tmp_path / 'w'
    work.mkdir()
    for index in range(30):
        (work / ('m%02d.log' % index)).write_text('match line ok\n' * 50)
    return work


def test_hook_passes_the_command_through_while_an_install_holds_its_lock(tmp_path):
    import time
    _install_auto(allow=('Exec(grep)',))
    holder = _hold_install_lock(60)
    try:
        started = time.monotonic()
        decision = _call('pre', 'grep -rn match .', cwd=_grep_workdir(tmp_path))  # asserts exit 0
        elapsed = time.monotonic() - started
    finally:
        holder.kill(); holder.wait()
    assert decision is None and elapsed < 5, (decision, elapsed)


def test_runner_refuses_at_once_while_an_install_holds_its_lock(tmp_path):
    """The runner's gate cannot check the attestation while an install holds
    install.lock; it refuses (126, nothing run) instead of waiting."""
    import time
    import devin_install as d
    _install_auto(allow=('Exec(grep)',))
    work = _grep_workdir(tmp_path)
    entry = str(d.state.data_dir() / 'plugin/devin_compress.py')
    holder = _hold_install_lock(60)
    try:
        started = time.monotonic()
        run = subprocess.run([sys.executable, '-I', entry, 'run', '--session', 's', '--', 'grep', '-rn', 'match', '.'],
                             cwd=work, capture_output=True, timeout=30)
        elapsed = time.monotonic() - started
    finally:
        holder.kill(); holder.wait()
    assert elapsed < 5, elapsed
    assert run.returncode == 126 and run.stdout == b'', run
    assert b'an install is in progress' in run.stderr
    captures = d.state.data_dir() / 'captures'
    assert not captures.exists() or list(captures.iterdir()) == []


def test_hook_proceeds_once_the_install_releases_its_lock_mid_wait(tmp_path):
    import time
    _install_auto(allow=('Exec(grep)',))
    holder = _hold_install_lock(0.8)
    try:
        started = time.monotonic()
        decision = _call('pre', 'grep -rn match .', cwd=_grep_workdir(tmp_path))
        elapsed = time.monotonic() - started
    finally:
        holder.wait()
    assert decision is not None and 'updatedInput' in decision['hookSpecificOutput']
    assert 0.5 < elapsed < 5, elapsed


def test_runner_proceeds_once_the_install_releases_its_lock_mid_wait(tmp_path):
    import time
    import devin_install as d
    _install_auto(allow=('Exec(grep)',))
    work = _grep_workdir(tmp_path)
    entry = str(d.state.data_dir() / 'plugin/devin_compress.py')
    holder = _hold_install_lock(0.8)
    try:
        started = time.monotonic()
        run = subprocess.run([sys.executable, '-I', entry, 'run', '--session', 's', '--', 'grep', '-rn', 'match', '.'],
                             cwd=work, capture_output=True, timeout=30)
        elapsed = time.monotonic() - started
    finally:
        holder.wait()
    assert run.returncode == 0 and run.stdout.startswith(b'[Token Optimizer capture ID:'), run
    assert 0.5 < elapsed < 5, elapsed


def _busy_at(monkeypatch, module, entry):
    """install_settled() raises InstallBusy on its entry-th use, as a stuck
    install would after the bounded wait."""
    import contextlib
    import devin_state as s
    real, calls = module.install_settled, []

    @contextlib.contextmanager
    def settled():
        calls.append(1)
        if len(calls) == entry:
            raise s.InstallBusy('a Token Optimizer install is still in progress')
        with real():
            yield
    monkeypatch.setattr(module, 'install_settled', settled)
    return calls


@pytest.mark.parametrize('entry,expected', [(1, 'refused'), (2, 'exec_original'), (3, 'raw')])
def test_runner_takes_its_existing_path_at_every_busy_site(tmp_path, monkeypatch, capfd, entry, expected):
    """Gate: refuse (126, nothing run). Preparation: run the original command
    unfiltered. Seal/filter: deliver the captured bytes raw and remove them."""
    import signal
    import devin_compress as c
    import devin_state as s
    _install_auto(allow=('Exec(grep)',))
    work = _grep_workdir(tmp_path)
    monkeypatch.chdir(work)
    monkeypatch.setattr(c, '_exec_original', lambda argv: 'exec_original')
    raw = subprocess.run(['grep', '-rn', 'match', '.'], cwd=work, capture_output=True).stdout
    capfd.readouterr()
    _busy_at(monkeypatch, c, entry)
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        code = c._run(['--session', 's', '--', 'grep', '-rn', 'match', '.'])
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)
    out = capfd.readouterr()
    captures = s.data_dir() / 'captures'
    left = list(captures.iterdir()) if captures.exists() else []
    if expected == 'refused':
        assert code == 126 and 'an install is in progress' in out.err
    elif expected == 'exec_original':
        assert code == 'exec_original'
    else:
        assert code == 0 and out.out.encode() == raw and out.err == ''
    assert left == []


@pytest.mark.parametrize('command', ['prepare', 'seal', 'recover', 'filter'])
def test_manual_commands_refuse_while_an_install_is_busy(tmp_path, monkeypatch, capfd, command):
    import signal
    import devin_compress as c
    import devin_state as s
    _install_auto()
    capture = s.prepare_capture()
    (capture / 'stdout').write_bytes(b'repeated log line\n' * 1000)
    if command in ('recover', 'filter'):
        c.seal_capture(capture, 0)
    argv = {'prepare': ['prepare'],
            'seal': ['seal', '--capture', str(capture), '--exit-code', '0'],
            'recover': ['recover', '--capture-id', capture.name, '--exit-code', '0'],
            'filter': ['--stdout', str(capture / 'stdout'), '--stderr', str(capture / 'stderr'),
                       '--exit-code', '0', '--session', 's']}[command]
    monkeypatch.setenv('TOKEN_OPTIMIZER_RUNTIME', 'devin')
    capfd.readouterr()
    _busy_at(monkeypatch, c, 1)
    pipe = signal.getsignal(signal.SIGPIPE)
    try:
        code = c.main(argv)
    finally:
        signal.signal(signal.SIGPIPE, pipe)
    err = capfd.readouterr().err
    assert code == 2 and 'install is still in progress' in err, err
