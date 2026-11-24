"""Exercise interleaved Stop, compaction, and ping workers on isolated storage."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from types import SimpleNamespace

import pytest

from test_keepwarm_arm_dedup import SCRIPTS, m, _stop_transcript
from test_keepwarm_auto import attempt, ready_install
from _measure import keepwarm_auto as policy, keepwarm_safety as safety, mod_action_booking


def proved(*args):
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        'usage': {'cache_read_input_tokens': 120000, 'cache_creation_input_tokens': 0}}))


@pytest.mark.parametrize('already_reserved', [7, 8])
def test_reservation_uses_locked_clock(m, monkeypatch, already_reserved):
    facade, before, record = ready_install(m, monkeypatch)
    locked_at = before + 2
    clock = [before]
    lock = mod_action_booking._lock
    with lock(facade) as held:
        assert held
        for n in range(already_reserved):
            row = attempt(n, sid=f'other-{n}', ts=before - 10 if n < 7 else before + 1)
            mod_action_booking._append(facade._keepwarm_ledger_path(), row)

    @contextmanager
    def acquire(facade):
        with lock(facade) as held:
            clock[0] = locked_at
            yield held

    monkeypatch.setattr(mod_action_booking, '_lock', acquire)
    monkeypatch.setattr(facade, 'time', SimpleNamespace(time=lambda: clock[0]))
    result = safety.fire_headless(facade, record, now=before, runner=proved)
    rows = facade._keepwarm_load_ledger_rows()
    if already_reserved == 8:
        assert result == {'fired': False, 'reason': 'cap-day'}
        assert len(rows) == 8
    else:
        assert result['proof']
        assert rows[-2]['status'] == 'firing' and rows[-2]['ts'] == locked_at


def test_slow_compaction_preserves_new_stop(m, tmp_path, monkeypatch):
    import hook_runtime

    facade, _, first = ready_install(m, monkeypatch)
    transcript = _stop_transcript(tmp_path)
    reading, attempted = Event(), Event()
    lock = facade._keepwarm_lock
    # Move the old lease's wall clock past expiry while compaction is paused.
    clock = SimpleNamespace(time=lambda: time.time() + (30 if reading.is_set() else 0),
                            monotonic=time.monotonic, sleep=time.sleep)
    monkeypatch.setattr(hook_runtime, 'time', clock)

    def classify(*args, **kwargs):
        reading.set()
        assert attempted.wait(5)
        return 'still_paused'

    @contextmanager
    def observed_lock():
        is_contender = reading.is_set()
        with lock() as held:
            yield held
        if is_contender:
            attempted.set()

    monkeypatch.setattr(facade, 'classify_keepwarm_record', classify)
    monkeypatch.setattr(facade, '_keepwarm_lock', observed_lock)
    with ThreadPoolExecutor(1) as pool:
        compact = pool.submit(facade.compact_keepwarm_sidecar)
        assert reading.wait(5)
        second = facade.write_keepwarm_arm_record('new-stop', transcript, parent_pid=2)
        assert compact.result(timeout=5) == (1, 0)
    assert second is not None
    assert facade.load_keepwarm_records() == [first, second]


def test_ledger_and_sidecar_share_lock_until_process_exit(m):
    facade, _ = m
    worker = """
import os, sys
sys.path.insert(0, sys.argv[1])
import measure
from _measure.mod_action_booking import _lock
with _lock(measure) as held:
    print(held, flush=True)
    sys.stdin.read(1)
    os._exit(0)
"""
    with subprocess.Popen([sys.executable, '-c', worker, str(SCRIPTS)], text=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
        try:
            assert child.stdout.readline().strip() == 'True'
            with facade._keepwarm_lock() as held:
                assert held is False
        finally:
            child.communicate('x', timeout=5)
    assert child.returncode == 0
    with facade._keepwarm_lock() as held:
        assert held is True


def test_reparented_wrapper_cannot_pin_unrelated_owner(monkeypatch):
    wrapper, reused, unrelated = 101, 102, 103
    parents = {wrapper: reused, reused: unrelated, unrelated: 1}
    stamps = {wrapper: 'wrapper', reused: 'replacement-shell', unrelated: 'older-claude'}

    def start(pid):
        if pid == reused:
            parents[wrapper] = 1
        return stamps.get(pid)

    monkeypatch.setattr(safety, 'process_start', start)
    monkeypatch.setattr(safety, 'process_parent', parents.get)
    facade = SimpleNamespace(_posix_exe_path=lambda pid: 'claude' if pid == unrelated else 'sh')
    _, stamp = safety._stop_parent(facade, wrapper)
    assert stamp is None


@pytest.mark.skipif(os.name != 'posix', reason='terminal watcher')
@pytest.mark.parametrize('phase', ['classification', 'reservation', 'ledger'])
def test_due_watcher_retries_competing_ping(m, tmp_path, monkeypatch, phase):
    facade, _, record = ready_install(m, monkeypatch)
    latch = safety._headless_latch(facade)
    calls = []

    def claim():
        latch.write_text(f'pending competing {os.getpid()}')

    def classify(*args, **kwargs):
        if phase == 'classification' and not calls:
            claim()
        calls.append('classify')
        return 'still_paused'

    monkeypatch.setattr(facade, 'classify_keepwarm_record', classify)
    original = safety.fire_headless

    def fire(*args, **kwargs):
        if 'reserve' not in calls:
            if phase == 'reservation':
                claim()
            if phase == 'ledger':
                with mod_action_booking._lock(facade) as held:
                    assert held
                    calls.append('reserve')
                    return original(*args, **kwargs, runner=proved)
        calls.append('reserve')
        return original(*args, **kwargs, runner=proved)

    monkeypatch.setattr(safety, 'fire_headless', fire)
    with (tmp_path / 'watch').open('w') as fd:
        delay = safety._watch_delay(facade, record, fd.fileno())
        assert delay is not None and 0 < delay <= 5
        assert facade._keepwarm_load_ledger_rows() == []
        latch.unlink(missing_ok=True)
        assert safety._watch_delay(facade, record, fd.fileno()) == 0
    assert [r['status'] for r in facade._keepwarm_load_ledger_rows()] == ['firing', 'ok']


def test_watcher_follows_refresh_that_beats_its_reservation(m, tmp_path, monkeypatch):
    facade, _, record = ready_install(m, monkeypatch)
    fire = safety.fire_headless

    def competing_refresh(*args, **kwargs):
        assert fire(*args, **kwargs, runner=proved)['proof']
        return fire(*args, **kwargs, runner=lambda *a: pytest.fail('duplicate ping'))

    monkeypatch.setattr(safety, 'fire_headless', competing_refresh)
    with (tmp_path / 'watch').open('w') as fd:
        delay = safety._watch_delay(facade, record, fd.fileno())
        assert delay is not None and 0 < delay <= 60
    assert [r['status'] for r in facade._keepwarm_load_ledger_rows()] == ['firing', 'ok']


@pytest.mark.parametrize('state', ['ended', 'expired', 'refreshed'])
def test_busy_latch_respects_session_and_cache_lifetime(m, tmp_path, monkeypatch, state):
    facade, now, record = ready_install(m, monkeypatch)
    safety._headless_latch(facade).write_text(f'pending suspended {os.getpid()}')
    if state == 'ended':
        assert safety.mark_ended(facade.SNAPSHOT_DIR, record['session_id'])
    else:
        record['last_turn_ts'] = now - policy.CACHE_LIFETIME - 10
        if state == 'refreshed':
            row = attempt(1, sid=record['session_id'], ts=now - 310, proof=True)
            assert facade._keepwarm_ledger_append(row)
    with (tmp_path / 'watch').open('w') as fd:
        delay = safety._watch_delay(facade, record, fd.fileno())
    if state == 'refreshed':
        assert delay is not None and 0 < delay <= 60
    else:
        assert delay is None


def test_read_only_writer_stops_retrying(m, tmp_path, monkeypatch):
    from _measure.keepwarm_rearm import arm_lock

    facade, _ = m
    transcript = _stop_transcript(tmp_path)
    turn = safety.terminal_turn(transcript)
    facade.SNAPSHOT_DIR.mkdir(exist_ok=True, parents=True)
    lease = facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + '.lease')
    lease.write_text('{}')
    opened = os.open
    failures = []

    def read_only(path, flags, *args, **kwargs):
        if Path(path).parent == facade.SNAPSHOT_DIR and flags & os.O_CREAT:
            failures.append(path)
            assert len(failures) < 4, 'permanent storage error retried'
            raise OSError(errno.EROFS, 'read-only fixture')
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', read_only)
    with arm_lock(facade, transcript, turn) as held:
        assert not held
    assert len(failures) == 1


def test_damaged_session_id_skipped_before_watcher_lookup(m, monkeypatch):
    from _measure.keepwarm_rearm import latest_terminal_arms

    facade, _, healthy = ready_install(m, monkeypatch)
    for invalid in (['broken'], {'broken': True}, 1, ''):
        damaged = dict(healthy, session_id=invalid)
        assert facade._keepwarm_append_line(facade._keepwarm_sidecar_path(), json.dumps(damaged) + '\n')
    assert latest_terminal_arms(facade) == {healthy['session_id']: healthy}
    assert facade.load_keepwarm_records() == [healthy]
