"""The Stop hook resolves the owning Claude process once per session, not per turn."""
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

from _measure import keepwarm_safety  # noqa: E402


def _counting_finder(monkeypatch, result):
    calls = []
    monkeypatch.setattr(keepwarm_safety, 'find_owner', lambda: calls.append(1) or result)
    return calls


def test_live_owner_is_found_once_per_session(tmp_path, monkeypatch):
    owner = {'pid': os.getpid(), 'start': 'fixture-start'}
    calls = _counting_finder(monkeypatch, owner)
    assert keepwarm_safety.session_owner(tmp_path, 'sess-a') == owner
    assert keepwarm_safety.session_owner(tmp_path, 'sess-a') == owner
    assert len(calls) == 1
    # A different session never borrows another session's owner.
    assert keepwarm_safety.session_owner(tmp_path, 'sess-b') == owner
    assert len(calls) == 2


def test_dead_owner_is_looked_up_again(tmp_path, monkeypatch):
    calls = _counting_finder(monkeypatch, {'pid': 2 ** 22 + 12345, 'start': 'gone'})
    keepwarm_safety.session_owner(tmp_path, 'sess')
    keepwarm_safety.session_owner(tmp_path, 'sess')
    assert len(calls) == 2


def test_no_owner_is_cached_briefly_then_retried(tmp_path, monkeypatch):
    calls = _counting_finder(monkeypatch, None)
    assert keepwarm_safety.session_owner(tmp_path, 'sess', now=1000.0) is None
    assert keepwarm_safety.session_owner(tmp_path, 'sess', now=1100.0) is None
    assert len(calls) == 1
    assert keepwarm_safety.session_owner(tmp_path, 'sess', now=1000.0 + keepwarm_safety._NO_OWNER_TTL + 1) is None
    assert len(calls) == 2


def test_corrupt_cache_falls_back_to_a_fresh_lookup(tmp_path, monkeypatch):
    owner = {'pid': os.getpid(), 'start': 's'}
    calls = _counting_finder(monkeypatch, owner)
    keepwarm_safety._owner_cache_path(tmp_path, 'sess').write_text('{not json')
    assert keepwarm_safety.session_owner(tmp_path, 'sess') == owner
    assert len(calls) == 1


def test_missing_session_id_never_caches(tmp_path, monkeypatch):
    calls = _counting_finder(monkeypatch, None)
    keepwarm_safety.session_owner(tmp_path, None)
    keepwarm_safety.session_owner(tmp_path, None)
    assert len(calls) == 2 and not list(tmp_path.iterdir())
