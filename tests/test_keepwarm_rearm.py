"""A later Stop must publish its own pause while the first watcher is running."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_keepwarm_arm_dedup import SCRIPTS, _stop_transcript, m, session_owner
from test_stop_runner import _load_runner
from _measure import keepwarm_safety as safety
from process_identity import process_start


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='terminal watchers use flock')


@pytest.fixture
def stops(m, tmp_path, monkeypatch):
    mod, _ = m
    runner = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, 'measure', mod)
    transcript = _stop_transcript(tmp_path)
    template = json.loads(transcript.read_text())
    sid = 'abc123'
    payload = {'session_id': sid, 'hook_event_name': 'Stop', 'transcript_path': str(transcript)}
    spawned = []
    observed = []
    monkeypatch.setenv(safety.HOOK_PARENT_PID_ENV, str(os.getpid()))
    monkeypatch.setenv('TOKEN_OPTIMIZER_HOOK_PARENT_START', process_start(os.getpid()))
    monkeypatch.setattr(mod, '_daemon_snapshot_sandboxed', lambda: False)
    monkeypatch.setattr(mod, '_read_stdin_hook_input', lambda: payload)
    monkeypatch.setattr(mod, 'spawn_detached', lambda cmd, **kw: spawned.append((cmd[2:], kw)) or True)
    # Exercise the real flush singleton and the refresh-throttled worker path.
    monkeypatch.setattr(mod, '_session_refresh_due', lambda: False)
    monkeypatch.setattr(safety, 'fire_headless', lambda *a, **kw: pytest.fail('unexpected ping'))

    def submit(number, before_worker=lambda: None, extra_rows=()):
        cwd = tmp_path if number == 1 else tmp_path / 'release' / 'install'
        cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd)
        with transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'user', 'cwd': str(cwd), 'turn': number}) + '\n')
            stamp = datetime.now(timezone.utc).isoformat()
            stream.write(json.dumps(dict(template, cwd=str(cwd), turn=number, timestamp=stamp)) + '\n')
            for row in extra_rows:
                stream.write(json.dumps(dict(row, timestamp=stamp)) + '\n')
        turn = safety.terminal_turn(transcript)
        if number == 2:
            args = safety.flush_hook_args(mod, ['session-end-flush', '--trigger', 'stop', '--defer'])
            mod._dispatch_session_end_flush(args)
        else:
            runner._sub_session_end_flush_stop(payload)
        assert len(spawned) == number
        worker, options = spawned[-1]
        assert worker[0] == 'session-end-flush-worker' and options['cwd'] == str(cwd)
        before_worker()
        safety.run_flush_worker(mod, worker)
        return turn

    def observe(record):
        observed.append(record['terminal_turn'])
        return {'action': 'stop', 'reason': 'fixture'}

    monkeypatch.setattr(safety, '_headless_decision', lambda facade, record, *args: observe(record))
    return SimpleNamespace(mod=mod, submit=submit, observed=observed, sid=sid)


@pytest.mark.parametrize('exit_during_rearm', [False, True])
def test_three_stops_one_watcher(stops, monkeypatch, exit_during_rearm):
    turns = []
    seen = []
    active = []
    watch = safety.watch_terminal

    def singleton(*args):
        assert not active, 'two workers entered the watcher simultaneously'
        active.append(True)
        try:
            return watch(*args)
        finally:
            active.pop()

    def decide(facade, record, *args):
        seen.append(record['terminal_turn'])
        assert len(seen) <= 3
        if len(seen) < 3:
            turns.append(stops.submit(len(seen) + 1))
            assert facade.load_keepwarm_records()[-1]['terminal_turn'] == turns[-1]
            if not exit_during_rearm:
                return {'action': 'stop', 'reason': 'not-due', 'due_in_s': 60}
        return {'action': 'stop', 'reason': 'cache-expired'}

    monkeypatch.setattr(stops.mod, 'time', SimpleNamespace(time=time.time, sleep=lambda _: None))
    monkeypatch.setattr(safety, 'watch_terminal', singleton)
    monkeypatch.setattr(safety, '_headless_decision', decide)
    first = stops.submit(1)
    records = stops.mod.load_keepwarm_records()
    assert len(records) == 3
    assert seen == [r['terminal_turn'] for r in records]
    assert seen == [first, *turns]
    assert len({r['terminal_turn']['digest'] for r in records}) == 3


def test_stop_after_foreign_released_sidecar_lease(stops):
    first = stops.submit(1)
    lease = stops.mod.SNAPSHOT_DIR / (stops.mod._KEEPWARM_LOCK_NAME + '.lease')
    # A scheduler process completed its mutation just before this worker.
    subprocess.run([sys.executable, '-c',
                    'import sys; sys.path.insert(0, sys.argv[1]); from hook_runtime import lease_lock\n'
                    'with lease_lock(sys.argv[2], reclaim_released=True) as held: assert held',
                    str(SCRIPTS), str(lease)], check=True)
    assert json.loads(lease.read_text())['released'] == 1
    second = stops.submit(2)
    assert stops.observed == [first, second]


def test_stop_after_hook_wrapper_exits(stops, monkeypatch):
    # Use a real short-lived wrapper and real kernel start stamps. Only the
    # test parent's executable identity and terminal eligibility are synthetic.
    parent = os.getpid()
    owner_start = time.ctime(time.time() - 60)
    monkeypatch.setattr(stops.mod, '_posix_exe_path',
                        lambda pid: '/bin/claude' if pid == parent else sys.executable)
    monkeypatch.setattr(safety, 'process_info', lambda pid: (
        {'ppid': 1, 'start': owner_start, 'command': 'claude'} if pid == parent else
        {'ppid': parent, 'start': owner_start, 'command': sys.executable} if process_start(pid) else None))
    monkeypatch.setattr(safety, 'owner_eligible', lambda pid: pid == parent)
    monkeypatch.setattr(safety, 'session_owner', session_owner)
    first = stops.submit(1)
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as wrapper:
        monkeypatch.setenv(safety.HOOK_PARENT_PID_ENV, str(wrapper.pid))
        monkeypatch.setenv('TOKEN_OPTIMIZER_HOOK_PARENT_START', process_start(wrapper.pid))
        second = stops.submit(2, before_worker=lambda: wrapper.communicate(timeout=5))
        assert wrapper.returncode == 0
    assert stops.observed == [first, second]


def test_busy_proof_latch_starts_waiting_watcher(stops, monkeypatch):
    latch = safety._headless_latch(stops.mod)
    latch.parent.mkdir(exist_ok=True, parents=True)
    latch.write_text('pending fixture ' + str(os.getpid()))
    monkeypatch.setattr(stops.mod, 'time', SimpleNamespace(time=time.time, sleep=lambda _: latch.unlink()))
    turn = stops.submit(1)
    assert stops.observed == [turn]


def test_deferred_worker_checks_usage_outside_preflight_tail(stops):
    first = stops.submit(1)
    # Large tool output moves the usage-bearing assistant row outside the
    # hook's bounded byte scan. The worker's 400-line scan still has it.
    second = stops.submit(2, extra_rows=[
        {'type': 'user', 'message': {'content': 'x' * (1024 * 1024)}},
        {'type': 'assistant', 'message': {'content': 'done'}},
    ])
    assert stops.observed == [first, second]


def test_late_receipt_keeps_new_pause_current(stops, monkeypatch):
    stops.submit(1)
    second = stops.submit(2)
    previous, _ = stops.mod.load_keepwarm_records()
    safety._track_proven_ping(stops.mod, previous, previous['ts'])
    assert stops.mod.load_keepwarm_records()[-1]['pings_fired'] == [previous['ts']]
    monkeypatch.setattr(stops.mod, 'time', SimpleNamespace(
        time=time.time, sleep=lambda _: pytest.fail('watcher followed the resumed pause')))
    safety.watch_armed_terminal(stops.mod, stops.sid)
    assert stops.observed[-1] == second
    result = safety.scheduler_tick(stops.mod, time.time(), True, {}, None)
    assert [entry['session_id'] for entry in result['decisions']] == [stops.sid]


def test_tick_recovers_rearm_after_busy_writer(stops, monkeypatch):
    first = stops.submit(1)
    lock = stops.mod._keepwarm_lock
    attempts = []
    holder = lock()
    assert holder.__enter__()

    @contextmanager
    def release_after_attempt():
        with lock() as acquired:
            if not attempts:
                attempts.append(acquired)
                holder.__exit__(None, None, None)
            yield acquired

    monkeypatch.setattr(stops.mod, '_keepwarm_lock', release_after_attempt)
    try:
        second = stops.submit(2)
    finally:
        if not attempts:
            holder.__exit__(None, None, None)
    assert attempts == [False]
    from _measure import keepwarm_rearm
    keepwarm_rearm.recover_pending(stops.mod, time.time())
    safety.watch_armed_terminal(stops.mod, stops.sid)
    assert stops.observed == [first, second]


def test_detached_workers_share_one_live_watcher(m, tmp_path, monkeypatch):
    mod, _ = m
    runner = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, 'measure', mod)
    transcript = _stop_transcript(tmp_path)
    template = json.loads(transcript.read_text())
    payload = {'session_id': 'abc123', 'hook_event_name': 'Stop', 'transcript_path': str(transcript)}
    monkeypatch.setenv(safety.HOOK_PARENT_PID_ENV, str(os.getpid()))
    monkeypatch.setenv('TOKEN_OPTIMIZER_HOOK_PARENT_START', process_start(os.getpid()))
    # Run the real worker CLI with a synthetic owner and a non-spending clock
    # callback. A ready file acknowledges the exact turn seen under flock.
    entry = tmp_path / 'fixture_worker.py'
    entry.write_text(f'''import os, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(SCRIPTS)!r})
import measure
from _measure import cli, keepwarm_safety as safety
measure._session_refresh_due = lambda: False
measure._daemon_snapshot_sandboxed = lambda: False
safety.session_owner = lambda *a, **kw: {{"pid": int(kw["parent_pid"]), "start": "fixture"}}
safety.owner_alive = lambda owner: True
measure.time = SimpleNamespace(time=time.time, sleep=lambda _: time.sleep(0.02))
def observe(facade, record, *args):
    ready = Path(__file__).parent / record["terminal_turn"]["digest"]
    with ready.open("a") as stream:
        print(os.getpid(), file=stream)
    return {{"action": "stop", "reason": "not-due", "due_in_s": 60}}
safety._headless_decision = observe
def forbidden(*args, **kwargs):
    raise AssertionError("unexpected ping")
safety.fire_headless = forbidden
cli.main()
''')
    monkeypatch.setattr(mod, '__file__', str(entry))
    processes = []
    spawn = mod.spawn_detached

    def track(command, **options):
        proc = spawn(command, **options)
        assert proc is not None
        processes.append(proc)
        return proc

    monkeypatch.setattr(mod, 'spawn_detached', track)
    try:
        for turn in range(3):
            cwd = tmp_path if turn < 2 else tmp_path / 'release'
            cwd.mkdir(exist_ok=True)
            monkeypatch.chdir(cwd)
            with transcript.open('a') as stream:
                stream.write(json.dumps({'type': 'user', 'turn': turn}) + '\n')
                stream.write(json.dumps(dict(template, turn=turn, cwd=str(cwd))) + '\n')
            ready = tmp_path / safety.terminal_turn(transcript)['digest']
            runner._sub_session_end_flush_stop(payload)
            assert len(processes) == turn + 1
            if turn:
                assert processes[-1].wait(timeout=30) == 0
            until = time.monotonic() + 30
            while not ready.exists() or not ready.stat().st_size:
                assert processes[0].poll() is None and time.monotonic() < until
                time.sleep(.025)
            assert set(ready.read_text().split()) == {str(processes[0].pid)}
        assert len(mod.load_keepwarm_records()) == 3
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
