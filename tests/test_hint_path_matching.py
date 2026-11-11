"""Hint follows match on canonical paths: symlinked, relative and ~ hints credit."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/token-optimizer/scripts'))
from session_store import SessionStore  # noqa: E402
from hint_paths import normalize_hint_path  # noqa: E402


def _store(tmp_path):
    return SessionStore('hint-path-test', snapshot_dir=tmp_path / 'snap')


def test_symlinked_hint_is_credited_when_read_by_real_path(tmp_path):
    real = tmp_path / 'real' / 'auth.py'
    real.parent.mkdir()
    real.write_text('x = 1\n')
    link_dir = tmp_path / 'link'
    link_dir.symlink_to(real.parent, target_is_directory=True)
    store = _store(tmp_path)
    try:
        store.record_hint_serve([str(link_dir / 'auth.py')])
        # The Read hook hands over Path.resolve(), i.e. the real path.
        assert store.claim_hint_follow(str(real.resolve())) is True
        assert store.claim_hint_follow(str(real.resolve())) is False  # once only
    finally:
        store.close()


def test_real_hint_is_credited_when_read_through_a_symlink(tmp_path):
    real = tmp_path / 'real' / 'm.py'
    real.parent.mkdir()
    real.write_text('y = 2\n')
    link = tmp_path / 'm_link.py'
    link.symlink_to(real)
    store = _store(tmp_path)
    try:
        store.record_hint_serve([str(real)])
        assert store.claim_hint_follow(str(link)) is True
    finally:
        store.close()


def test_relative_hint_is_credited(tmp_path, monkeypatch):
    target = tmp_path / 'pkg' / 'util.py'
    target.parent.mkdir()
    target.write_text('z = 3\n')
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path)
    try:
        store.record_hint_serve([os.path.join('pkg', '..', 'pkg', 'util.py')])
        assert store.claim_hint_follow(str(target.resolve())) is True
    finally:
        store.close()


def test_home_relative_hint_is_credited(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))  # Windows expands "~" from this, not HOME
    target = tmp_path / 'home' / 'proj' / 'a.py'
    target.parent.mkdir(parents=True)
    target.write_text('a = 1\n')
    store = _store(tmp_path)
    try:
        store.record_hint_serve(['~/proj/a.py'])
        assert store.claim_hint_follow(str(target.resolve())) is True
    finally:
        store.close()


def test_unrelated_file_is_not_credited(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_hint_serve([str(tmp_path / 'a.py')])
        assert store.claim_hint_follow(str(tmp_path / 'b.py')) is False
    finally:
        store.close()


def test_row_stored_raw_before_normalization_still_matches(tmp_path):
    store = _store(tmp_path)
    try:
        store._connect().execute(
            "INSERT INTO hint_serves (file_path, served_at, credited) VALUES (?, strftime('%s','now'), 0)",
            ('/plain/old.py',))
        assert store.claim_hint_follow('/plain/old.py') is True
    finally:
        store.close()


def test_normalize_empty_and_idempotent(tmp_path):
    assert normalize_hint_path('') == '' and normalize_hint_path(None) == ''
    once = normalize_hint_path(str(tmp_path / 'x' / '..' / 'y'))
    assert normalize_hint_path(once) == once
