import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
import cursor_read_cache as cache
import cursor_hook_bridge as bridge
import cursor_bridge_checkpoint as checkpoint


def fields(path, root, sid='review2'):
    return {'tool_name': 'Read', 'tool_args': {'file_path': str(path)}, 'tool_use_id': 'id',
            'conversation_id': sid, 'workspace_roots': [str(root)], 'cwd': str(root)}


def setup_cache(monkeypatch, tmp_path, mode='block'):
    home = tmp_path / 'cursor'
    home.mkdir()
    monkeypatch.setattr(cache, '_home', lambda: home)
    monkeypatch.setattr(bridge, 'cursor_home', lambda: home)
    monkeypatch.setenv('HOME', str(tmp_path))
    config = home / 'token-optimizer/read-cache.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'mode': mode}))
    return home


def big_file(path):
    path.write_text('\n'.join('def function_%d(value):\n    return value + %d\n' % (i, i) for i in range(300)))


def test_compaction_clears_confirmed_and_pending_reads(monkeypatch, tmp_path):
    setup_cache(monkeypatch, tmp_path)
    path = tmp_path / 'large.py'; big_file(path)
    f = fields(path, tmp_path)
    cache.evaluate(f); cache.confirm_read(f)
    assert cache.evaluate(f)['emit']['permission'] == 'deny'
    bridge.handle_pre_compact({'conversation_id': 'review2'})
    cache.confirm_read(f)
    assert cache.evaluate(f)['emit'] is None


@pytest.mark.parametrize('name,content', [('small.py', 'x = 1\n'), ('data.json', '{"foo": 1}'), ('broken.py', 'def broken(:\n' * 500)])
def test_ineligible_structure_never_denies(monkeypatch, tmp_path, name, content):
    setup_cache(monkeypatch, tmp_path)
    path = tmp_path / name; path.write_text(content)
    f = fields(path, tmp_path)
    cache.evaluate(f); cache.confirm_read(f)
    result = cache.evaluate(f)
    assert result is None or result['emit'] is None or result['emit'].get('permission') != 'deny'


def test_relative_read_uses_workspace_and_contextignore(monkeypatch, tmp_path):
    setup_cache(monkeypatch, tmp_path)
    root = tmp_path / 'project'; root.mkdir()
    path = root / 'large.py'; big_file(path)
    monkeypatch.chdir(tmp_path)
    f = fields('large.py', root)
    assert cache.evaluate(f)['observed']['decision'] == 'first_read'
    cache.confirm_read(f)
    assert cache.evaluate(fields(path, root))['emit']['permission'] == 'deny'
    (root / '.contextignore').write_text('large.py\n')
    assert cache.evaluate(f) is None


def test_shell_failure_does_not_lose_restore(monkeypatch, tmp_path, capsys):
    home = setup_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, '_update_tally', lambda *a, **k: ({}, False))
    monkeypatch.setattr(bridge, '_claim_restore', lambda f: str(home / 'token-optimizer/cp'))
    (home / 'token-optimizer/cp').write_text('checkpoint body')
    import cursor_thrash
    monkeypatch.setattr(cursor_thrash, 'shell_thrash_nudge', lambda f: (_ for _ in ()).throw(RuntimeError('broken')))
    bridge.handle_post_tool_use({'conversation_id': 'review2', 'tool_name': 'Shell', 'tool_input': {'command': 'ls'}})
    assert 'checkpoint body' in capsys.readouterr().out


def test_checkpoint_uses_requested_home_with_preimported_measure(monkeypatch, tmp_path):
    import measure
    home = tmp_path / 'cursor'; home.mkdir()
    import cursor_transcript
    translated = tmp_path / 'translated'; translated.write_text('x')
    monkeypatch.setattr(cursor_transcript, 'translate_transcript', lambda *a: translated)
    old = measure.CHECKPOINT_DIR
    def capture(**kwargs):
        assert measure.CHECKPOINT_DIR == home / 'token-optimizer/checkpoints'
        assert measure.CHECKPOINT_EVENT_LOG == home / 'token-optimizer/checkpoint-events.jsonl'
        return measure.CHECKPOINT_DIR / 'cp.md'
    monkeypatch.setattr(measure, 'compact_capture', capture)
    assert checkpoint._capture_compact_checkpoint({'transcript_path': 'x', 'conversation_id': 'x'}, home)
    assert measure.CHECKPOINT_DIR == old


def test_restore_rejects_parent_symlink(monkeypatch, tmp_path):
    to_dir = tmp_path / 'to'; to_dir.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (to_dir / 'restore-context').symlink_to(outside, target_is_directory=True)
    root = str(tmp_path / 'project')
    digest = hashlib.sha1(root.encode()).hexdigest()
    (outside / (digest + '.md')).write_text('<!-- source-end: %s -->\nsecret' % time.time())
    assert checkpoint.restore_for_workspace({}, root, to_dir, None, lambda x: None, 60) is None


def test_compaction_epoch_rejects_inflight_stale_store(monkeypatch, tmp_path):
    setup_cache(monkeypatch, tmp_path)
    path = tmp_path / 'large.py'; big_file(path)
    f = fields(path, tmp_path)
    cache.evaluate(f); cache.confirm_read(f)
    old = cache._load_store('review2')
    cache.clear_session_reads('review2')
    cache._save_store('review2', old)
    assert cache.evaluate(f)['emit'] is None


def test_normalized_workspace_suppresses_active_sibling(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    to_dir = tmp_path / 'to'; folder = to_dir / 'restore-context'; folder.mkdir(parents=True)
    digest = hashlib.sha1(str(root).encode()).hexdigest()
    (folder / (digest + '.md')).write_text('<!-- source-end: %s -->\ncontinuity' % time.time())
    sessions = tmp_path / 'sessions'; sessions.mkdir()
    tally_path = sessions / 'sibling.json'; tally_path.write_text('{}')
    tally = {'conversation_id': 'sibling', 'workspace_roots': [str(root) + '/'], 'updated_at': time.time()}
    assert checkpoint.restore_for_workspace({'conversation_id': 'own'}, str(root) + '/.', to_dir, sessions, lambda p: tally, 60) is None
    assert checkpoint.restore_for_workspace({'conversation_id': 'own'}, str(root) + '/.', to_dir, None, lambda p: None, 60) == 'continuity'
