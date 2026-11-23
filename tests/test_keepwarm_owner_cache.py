"""The Stop hook caches terminal eligibility for the current Claude process."""
import os
import sys
import time

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

from _measure import keepwarm_safety  # noqa: E402


def _counting_eligibility(monkeypatch, process, eligible=True):
    calls = []
    monkeypatch.setattr(keepwarm_safety.os, 'getppid', lambda: process['pid'])
    monkeypatch.setattr(keepwarm_safety, 'process_info', lambda pid: {
        'ppid': 1, 'command': 'claude', 'start': process['start'],
    } if pid == process['pid'] else None)
    monkeypatch.setattr(keepwarm_safety, 'owner_eligible', lambda pid: calls.append(pid) or eligible)
    return calls


def test_live_owner_eligibility_is_checked_once_per_session(tmp_path, monkeypatch):
    owner = {'pid': os.getpid(), 'start': 'fixture-start'}
    calls = _counting_eligibility(monkeypatch, owner)
    assert keepwarm_safety.session_owner(tmp_path, 'sess-a') == owner
    assert keepwarm_safety.session_owner(tmp_path, 'sess-a') == owner
    assert len(calls) == 1
    # A different session never borrows another session's owner.
    assert keepwarm_safety.session_owner(tmp_path, 'sess-b') == owner
    assert len(calls) == 2


def test_reused_owner_pid_is_looked_up_again(tmp_path, monkeypatch):
    owner = {'pid': os.getpid(), 'start': 'first-start'}
    calls = _counting_eligibility(monkeypatch, owner)
    assert keepwarm_safety.session_owner(tmp_path, 'sess') == owner
    owner['start'] = 'later-start'
    assert keepwarm_safety.session_owner(tmp_path, 'sess') == owner
    assert len(calls) == 2


def test_no_owner_is_cached_briefly_then_retried(tmp_path, monkeypatch):
    calls = _counting_eligibility(monkeypatch, {'pid': os.getpid(), 'start': 'headless'}, eligible=False)
    assert keepwarm_safety.session_owner(tmp_path, 'sess', now=1000.0) is None
    assert keepwarm_safety.session_owner(tmp_path, 'sess', now=1100.0) is None
    assert len(calls) == 1
    assert keepwarm_safety.session_owner(tmp_path, 'sess', now=1000.0 + keepwarm_safety._NO_OWNER_TTL + 1) is None
    assert len(calls) == 2


def test_corrupt_cache_falls_back_to_a_fresh_lookup(tmp_path, monkeypatch):
    owner = {'pid': os.getpid(), 'start': 's'}
    calls = _counting_eligibility(monkeypatch, owner)
    keepwarm_safety._owner_cache_path(tmp_path, 'sess').write_text('{not json')
    assert keepwarm_safety.session_owner(tmp_path, 'sess') == owner
    assert len(calls) == 1


def test_resumed_print_process_cannot_borrow_live_interactive_owner(tmp_path, monkeypatch):
    interactive_pid, print_pid = os.getpid(), os.getpid() + 1
    processes = {
        interactive_pid: {'ppid': 1, 'start': 'interactive-start', 'command': 'claude'},
        print_pid: {'ppid': 1, 'start': 'print-start', 'command': 'claude'},
    }
    monkeypatch.setattr(keepwarm_safety, 'process_info', processes.get)
    monkeypatch.setattr(keepwarm_safety, 'owner_eligible', lambda pid: pid == interactive_pid)
    monkeypatch.setattr(keepwarm_safety.os, 'getppid', lambda: interactive_pid)
    assert keepwarm_safety.session_owner(tmp_path, 'resumed') == {
        'pid': interactive_pid, 'start': 'interactive-start'}

    monkeypatch.setattr(keepwarm_safety.os, 'getppid', lambda: print_pid)
    assert keepwarm_safety.session_owner(tmp_path, 'resumed') is None


def test_deferred_lookup_uses_hook_parent_and_rejects_pid_reuse_after_stop(tmp_path, monkeypatch):
    before_stop = 1_700_000_000
    process = {'pid': os.getpid(), 'start': time.ctime(before_stop)}
    calls = _counting_eligibility(monkeypatch, process)
    monkeypatch.setattr(keepwarm_safety.os, 'getppid', lambda: 1)
    args = (tmp_path, 'sess', before_stop + 10)

    assert keepwarm_safety.session_owner(*args, parent_pid=process['pid']) == process
    assert keepwarm_safety.session_owner(*args, parent_pid=process['pid']) == process
    assert calls == [process['pid']]

    process['start'] = time.ctime(before_stop + 5)
    assert keepwarm_safety.session_owner(*args, parent_pid=process['pid']) == process
    assert calls == [process['pid']] * 2

    process['start'] = time.ctime(before_stop + 11)
    assert keepwarm_safety.session_owner(*args, parent_pid=process['pid']) is None
    assert calls == [process['pid']] * 2


def test_missing_session_id_never_caches(tmp_path, monkeypatch):
    calls = _counting_eligibility(monkeypatch, {'pid': os.getpid(), 'start': 'headless'}, eligible=False)
    keepwarm_safety.session_owner(tmp_path, None)
    keepwarm_safety.session_owner(tmp_path, None)
    assert len(calls) == 2 and not list(tmp_path.iterdir())


def test_no_process_is_spawned_to_find_an_owner_off_posix(monkeypatch):
    # Windows has no `ps`: the Stop hook must answer "no owner" without starting one (hot-path budget).
    class Off:
        name = 'nt'
        def __getattr__(self, attr):
            return getattr(os, attr)
    monkeypatch.setattr(keepwarm_safety, 'os', Off())
    def forbidden(*args, **kwargs):
        raise AssertionError('a process was started')
    monkeypatch.setattr(keepwarm_safety.subprocess, 'run', forbidden)
    assert keepwarm_safety.process_info(os.getpid()) is None
    assert keepwarm_safety.find_owner() is None
