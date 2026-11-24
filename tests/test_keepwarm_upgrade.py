"""Keep-warm regressions at the dispatcher, storage and reservation boundaries."""
import builtins
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from test_keepwarm_arm_dedup import _stop_transcript, m
from test_keepwarm_auto import attempt, history, ready_install
from test_keepwarm_writer_races import proved
from test_consent_gate_race_window import race_env
from _measure import keepwarm_auto as policy, keepwarm_rearm as rearm, keepwarm_safety as safety


@pytest.mark.parametrize('failure', ['unreadable', 'rollback'])
def test_accounting_failure_refuses_reservation(m, monkeypatch, failure):
    facade, now, record = ready_install(m, monkeypatch)
    history(facade, stamp=now - 60, at_risk=30, returned=30)
    for index in range(8):
        assert facade._keepwarm_ledger_append(attempt(index, sid=f'other-{index}', ts=now))
    path = facade._keepwarm_ledger_path()
    original = path.read_bytes()
    real_open = builtins.open

    def deny_read(file, mode='r', *args, **kwargs):
        if Path(file) == path and 'r' in mode:
            raise PermissionError('ledger readable only after repair')
        return real_open(file, mode, *args, **kwargs)

    if failure == 'unreadable':
        monkeypatch.setattr(builtins, 'open', deny_read)
    else:
        monkeypatch.setattr(facade, 'time', SimpleNamespace(time=lambda: now - 3))
    calls = []
    result = safety.fire_headless(facade, record, now=now, runner=lambda *a: calls.append(a) or proved())
    assert result == {'fired': False, 'reason': 'accounting-unavailable'}
    assert calls == [] and path.read_bytes() == original


@pytest.mark.parametrize('stamp', ['original', ''])
def test_dispatch_parent_reused_before_walk(m, monkeypatch, tmp_path, stamp):
    facade, _ = m
    monkeypatch.setenv(safety.HOOK_PARENT_PID_ENV, '123')
    monkeypatch.setenv('TOKEN_OPTIMIZER_HOOK_PARENT_START', stamp)
    monkeypatch.setattr(safety, 'process_start', lambda pid: 'replacement')
    monkeypatch.setattr(facade, '_posix_exe_path', lambda pid: '/bin/claude')
    payload = {'session_id': 'test', 'transcript_path': str(_stop_transcript(tmp_path))}
    args = safety.stop_flush_args(facade, ['session-end-flush', '--trigger', 'stop'], payload)
    assert '--transcript-path' in args and '--keepwarm-parent-pid' not in args


@pytest.mark.skipif(os.name != 'posix', reason='terminal hook')
@pytest.mark.parametrize('reparent', [False, True])
def test_dispatcher_checks_parent_before_export(race_env, monkeypatch, reparent):
    import process_identity
    run = race_env.run
    pid = [123]
    captured = []
    monkeypatch.setattr(run.os, 'getppid', lambda: pid[0])
    monkeypatch.setenv('TOKEN_OPTIMIZER_HOOK_PARENT_START', 'inherited')
    race_env.write_config({'v5_welcome_shown': True, 'enterprise_consent_shown': True})

    def read_start(parent):
        assert parent == 123
        if reparent:
            pid[0] = 1
        return 'original'

    def dispatch(command, **kwargs):
        captured.append(kwargs['env'])
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(process_identity, 'process_start', read_start)
    monkeypatch.setattr(run.subprocess, 'Popen', dispatch)
    assert race_env.dispatch(['run.py', 'hooks/stop_runner.py']) == 0
    child, = captured
    assert child[safety.HOOK_PARENT_PID_ENV] == '123'
    assert child.get('TOKEN_OPTIMIZER_HOOK_PARENT_START') == (None if reparent else 'original')


@pytest.mark.parametrize('legacy_takeover', [False, True])
def test_upgrade_drains_live_legacy_compactor(m, tmp_path, monkeypatch, legacy_takeover):
    from hook_runtime import LeaseLock

    facade, _, first = ready_install(m, monkeypatch)
    lease = LeaseLock(facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + '.lease'),
                      lease_seconds=0.1, reclaim_released=True)
    assert lease.acquire()
    path = facade._keepwarm_sidecar_path()
    old_snapshot = path.read_bytes()
    real_lock = facade._keepwarm_lock
    observations = []

    @contextmanager
    def interleave():
        with real_lock() as held:
            yield held
        if not observations:
            observations.append(held)
            # The legacy compactor resumes its atomic replacement and releases.
            replacement = path.with_suffix('.old')
            replacement.write_bytes(old_snapshot)
            replacement.replace(path)
            lease.release()

    try:
        time.sleep(.4)  # A live legacy writer can outlast its lease and grace.
        if legacy_takeover:
            # A second old writer can already have replaced the first lease.
            with LeaseLock(lease.path, reclaim_released=True) as acquired:
                assert acquired
        monkeypatch.setattr(facade, '_keepwarm_lock', interleave)
        second = facade.write_keepwarm_arm_record('second', _stop_transcript(tmp_path), parent_pid=2)
        assert second and facade.load_keepwarm_records() == [first, second]
    finally:
        lease.release()


def test_scheduler_recovers_stop_after_worker_dies_waiting(m, tmp_path, monkeypatch):
    facade, now, first = ready_install(m, monkeypatch)
    path = _stop_transcript(tmp_path)
    with monkeypatch.context() as waiting:
        def terminate():
            raise SystemExit('detached worker interrupted at lock')
        waiting.setattr(facade, '_keepwarm_lock', terminate)
        with pytest.raises(SystemExit):
            facade.write_keepwarm_arm_record('pending', path, parent_pid=2)
    tick = safety.scheduler_tick(facade, now + 1, False, {}, proved)
    assert {row['session_id'] for row in tick['decisions']} == {first['session_id'], 'pending'}
    assert len([r for r in facade.load_keepwarm_records() if r['session_id'] == 'pending']) == 1


def test_eligible_stop_outlasts_thirty_seconds_of_contention(m, monkeypatch, tmp_path):
    facade, _ = m
    path = _stop_transcript(tmp_path)
    elapsed = [0.0]
    start = time.time()
    def advance(seconds):
        elapsed[0] += seconds
        assert elapsed[0] < 40
    monkeypatch.setattr(rearm, 'time', SimpleNamespace(monotonic=lambda: elapsed[0], sleep=advance))
    monkeypatch.setattr(facade, 'time', SimpleNamespace(time=lambda: start + elapsed[0]))
    @contextmanager
    def lock():
        yield elapsed[0] >= 31
    monkeypatch.setattr(facade, '_keepwarm_lock', lock)
    record = facade.write_keepwarm_arm_record('waiting', path, parent_pid=2)
    assert record and facade.load_keepwarm_records() == [record]
    assert elapsed[0] >= 31
    assert not list(facade.SNAPSHOT_DIR.glob('keepwarm-pending-stop-*'))


def test_pending_proof_is_complete_on_first_visibility(m, monkeypatch):
    facade, now, record = ready_install(m, monkeypatch)
    latch = safety._headless_latch(facade)
    opened, linked = os.open, os.link
    reasons = []

    def open_file(path, flags, *args, **kwargs):
        fd = opened(path, flags, *args, **kwargs)
        if Path(path) == latch and flags & os.O_CREAT:
            reasons.append(safety.headless_disabled(facade))
        return fd

    def link_file(source, dest, *args, **kwargs):
        linked(source, dest, *args, **kwargs)
        if Path(dest) == latch:
            reasons.append(safety.headless_disabled(facade))

    monkeypatch.setattr(os, 'open', open_file)
    monkeypatch.setattr(os, 'link', link_file)
    assert safety.fire_headless(facade, record, now=now, runner=proved)['proof']
    assert reasons == ['headless-busy']


def test_watch_waits_for_own_refresh_through_cache_expiry(m, monkeypatch, tmp_path):
    facade, now, record = ready_install(m, monkeypatch)
    clock = [now]
    record['last_turn_ts'] = now - 3570
    monkeypatch.setattr(facade, 'time', SimpleNamespace(time=lambda: clock[0]))
    with (tmp_path / 'watch').open('w') as watch:
        def refresh(*args):
            clock[0] += 16
            delay = safety._watch_delay(facade, record, watch.fileno())
            assert delay is not None and 0 < delay <= 5
            clock[0] = now + facade._KEEPWARM_PING_TIMEOUT_SECONDS + 1
            assert safety._watch_delay(facade, record, watch.fileno()) is None
            clock[0] = now + 20
            return proved()
        assert safety.fire_headless(facade, record, now=now, runner=refresh)['proof']
        assert 0 < safety._watch_delay(facade, record, watch.fileno()) <= 60


@pytest.mark.skipif(os.name != 'posix', reason='terminal watcher uses flock')
def test_pruning_preserves_locked_watcher_inode(m, monkeypatch):
    import fcntl
    facade, _ = m
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def watch(facade, sid, fd):
        try:
            path, = facade.SNAPSHOT_DIR.glob('keepwarm-owner-watch-*')
            now = time.time()
            os.utime(fd, (now - 15 * policy.DAY,) * 2)
            policy.prune_markers(facade, now)
            with path.open('a') as contender, pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    monkeypatch.setattr(safety, 'watch_terminal', watch)
    safety.watch_armed_terminal(facade, 'returning')


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_arm_cannot_hide_later_stop(m, monkeypatch, bad):
    facade, _, record = ready_install(m, monkeypatch)
    path = facade._keepwarm_sidecar_path()
    path.write_text(json.dumps(dict(record, ts=bad)) + '\n' + json.dumps(record) + '\n')
    assert facade.load_keepwarm_records() == [record]
    assert rearm.latest_terminal_arms(facade) == {record['session_id']: record}
