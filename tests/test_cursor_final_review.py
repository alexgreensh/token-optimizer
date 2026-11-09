import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
import cursor_read_cache as cache
import cursor_bridge_payload as payload
import cursor_state


@pytest.fixture
def ready(tmp_path, monkeypatch):
    home = tmp_path / '.cursor'; home.mkdir()
    monkeypatch.setattr(cache, '_home', lambda: home)
    monkeypatch.setenv('HOME', str(tmp_path))
    cfg = home / 'token-optimizer/read-cache.json'; cfg.parent.mkdir()
    cfg.write_text('{"mode":"block"}')
    path = tmp_path / 'large.py'
    path.write_text('\n'.join('def f%d(x):\n    return x + %d\n' % (i, i) for i in range(300)))
    fields = {'tool_name':'Read', 'tool_args':{'file_path':str(path)}, 'conversation_id':'conversation-123',
              'agent_id':'parent', 'tool_use_id':'tool-id', 'workspace_roots':[str(tmp_path)]}
    return home, path, fields


def test_subagent_store_isolated_and_missing_identity_fails_open(ready):
    _, _, parent = ready
    cache.evaluate(parent); cache.confirm_read(parent)
    assert cache.evaluate(parent)['emit']['permission'] == 'deny'
    child = dict(parent, agent_id='child')
    assert cache.evaluate(child)['emit'] is None
    cache.confirm_read(child)
    assert cache.evaluate(child)['emit']['permission'] == 'deny'
    unknown = dict(parent); unknown.pop('agent_id')
    assert cache.evaluate(unknown) is None
    assert payload.decode_payload({'conversation_id':'conversation-123', 'agent_id':'child'})['agent_id'] == 'child'


def test_compaction_epoch_replace_failure_invalidates_store(ready, monkeypatch):
    _, _, fields = ready
    cache.evaluate(fields); cache.confirm_read(fields)
    original = cache.os.replace
    def replace(src, dest):
        if str(dest).endswith('.epoch'): raise PermissionError('locked')
        return original(src, dest)
    monkeypatch.setattr(cache.os, 'replace', replace)
    cache.clear_session_reads(fields['conversation_id'])
    assert cache.evaluate(fields)['emit'] is None


def test_contextignore_uses_resolved_root_and_directory_patterns(ready, tmp_path):
    _, path, fields = ready
    folder = tmp_path / 'project'; folder.mkdir()
    sub = folder / 'secrets'; sub.mkdir()
    secret = sub / 'large.py'; secret.write_bytes(path.read_bytes())
    (folder / '.contextignore').write_text('secrets/\n')
    alias = tmp_path / 'alias'; alias.symlink_to(folder, target_is_directory=True)
    fields = dict(fields, tool_args={'file_path':str(alias / 'secrets/large.py')}, workspace_roots=[str(alias)])
    assert cache.evaluate(fields) is None


def test_distinct_unsafe_conversation_ids_do_not_collide():
    assert cache._store_path('conv.1') != cache._store_path('conv1')
    assert cache._store_path('x' * 64 + 'a') != cache._store_path('x' * 64 + 'b')


def test_cursor_state_override_uses_selected_home(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN_OPTIMIZER_CURSOR_HOME', str(tmp_path / 'alternate'))
    assert cursor_state.state_vscdb_path_for_home(tmp_path / 'alternate') == tmp_path / 'alternate/User/globalStorage/state.vscdb'


def test_compaction_temp_creation_failure_invalidates_store(ready, monkeypatch):
    _, _, fields = ready
    cache.evaluate(fields); cache.confirm_read(fields)
    original = cache.tempfile.mkstemp
    def make(*args, **kwargs):
        if kwargs.get('prefix') == '.epoch-': raise OSError('disk full')
        return original(*args, **kwargs)
    monkeypatch.setattr(cache.tempfile, 'mkstemp', make)
    cache.clear_session_reads(fields['conversation_id'])
    assert cache.evaluate(fields)['emit'] is None


def test_shell_home_flag_cannot_consume_dry_run(tmp_path):
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    script = 'source ./install.sh; measure_py=skills/token-optimizer/scripts/measure.py; _cursor_prepare --home --dry-run'
    result = subprocess.run(['bash', '-c', script], cwd=repo, env={**os.environ, '_TO_INSTALL_SH_TEST_MODE':'1'}, capture_output=True, text=True)
    assert result.returncode != 0
    assert '--home requires a directory path' in result.stderr + result.stdout
    assert not (repo / '--dry-run').exists()


def test_installer_rejects_symlinked_home(tmp_path):
    import cursor_install
    target = tmp_path / 'target'; target.mkdir()
    alias = tmp_path / 'alias'; alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        cursor_install.install(home=alias, dry_run=True)


def test_oversized_ignore_file_and_blank_agent_fail_open(ready, tmp_path):
    _, _, fields = ready
    assert cache.evaluate(dict(fields, agent_id='  ')) is None
    (tmp_path / '.contextignore').write_text('# comment\n' * 1001)
    assert cache.evaluate(fields) is None
    assert payload.decode_payload({'agent_id': '  '})['agent_id'] == ''
