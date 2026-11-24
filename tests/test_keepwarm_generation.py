"""Cross-version pause isolation, reservation contention and accounting reads."""
import builtins
from contextlib import nullcontext
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_keepwarm_arm_dedup import _stop_transcript, m
from test_keepwarm_auto import attempt, decide, ready_install
from test_keepwarm_writer_races import proved
from _measure import keepwarm_rearm as rearm, keepwarm_safety as safety, mod_action_booking


@pytest.mark.parametrize('winerror,recovers,attempts', [(32, True, 2), (33, True, 2),
                                                       (32, False, 3), (5, False, 1)])
def test_windows_ledger_read(m, monkeypatch, winerror, recovers, attempts):
    facade, now, record = ready_install(m, monkeypatch)
    ledger = facade._keepwarm_ledger_path()
    assert facade._keepwarm_ledger_append(attempt(1, sid='other', ts=now - 10))
    original = ledger.read_bytes()
    from _measure import keepwarm_economics
    monkeypatch.setattr(keepwarm_economics, 'verified_summary', lambda *args: {'pings': 0, 'net_usd': 0})
    real_open = builtins.open
    reads, calls = [], []

    def busy(path, *args, **kwargs):
        if Path(path) == ledger:
            reads.append(path)
            if not recovers or len(reads) == 1:
                exc = PermissionError('injected Windows file error')
                exc.winerror = winerror
                raise exc
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', busy)
    result = safety.fire_headless(facade, record, now=now, runner=lambda *a: calls.append(a) or proved())
    assert len(reads) == attempts
    assert result['fired'] is recovers and len(calls) == int(recovers)
    if not recovers:
        assert result['reason'] == ('accounting-unavailable' if winerror == 5 else 'ledger-locked')
        assert ledger.read_bytes() == original


@pytest.mark.parametrize('code,expected,reads', [(32, 'headless-busy', 3), (5, 'accounting-unavailable', 1)])
def test_windows_proof_marker_contention(m, monkeypatch, code, expected, reads):
    facade, _ = m
    count = []
    def denied(*args, **kwargs):
        count.append(1)
        exc = PermissionError('fixture marker')
        exc.winerror = code
        raise exc
    monkeypatch.setattr(Path, 'open', denied)
    assert safety._headless_latch_reason(facade) == expected and len(count) == reads


def test_delayed_outcome_explains_windows_ci_result(m, monkeypatch):
    facade, now, record = ready_install(m, monkeypatch)
    clock = [now]
    monkeypatch.setattr(facade, 'time', SimpleNamespace(time=lambda: clock[0]))

    def slow_failure(*args):
        clock[0] += 2
        return SimpleNamespace(returncode=1, stdout='', stderr='HTTP 429: Too Many Requests')

    assert safety.fire_headless(facade, record, now=now, runner=slow_failure)['reason'] == 'rate-limited'
    rows = facade._keepwarm_load_ledger_rows()
    assert decide(facade, now=now + 1, headless=True, rows=rows)['reason'] == 'accounting-unavailable'
    assert decide(facade, now=clock[0], headless=True, rows=rows)['reason'] == 'failed-proof'


@pytest.mark.parametrize('takeover', [False, True])
def test_spending_lease_does_not_block_an_arm(m, tmp_path, monkeypatch, takeover):
    from hook_runtime import LeaseLock
    facade, now, first = ready_install(m, monkeypatch)
    path = facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + '.lease')
    import hook_runtime
    clock = [now - 365 * 86400]
    monkeypatch.setattr(hook_runtime, 'time', SimpleNamespace(time=lambda: clock[0],
                        monotonic=time.monotonic, sleep=time.sleep))
    for i in range(7):
        assert facade._keepwarm_ledger_append(attempt(i, sid=f'other-{i}', ts=now - 60))
    with LeaseLock(path, lease_seconds=.1, reclaim_released=True) as held:
        assert held
        assert facade.write_keepwarm_arm_record('second', _stop_transcript(tmp_path), parent_pid=2)
        # Old writer has checked slot eight and may append even after lease expiry and clock rollback.
        clock[0] = now
        if takeover:
            with LeaseLock(path, reclaim_released=True) as replacement:
                assert replacement
        result = safety.fire_headless(facade, first, now=now, runner=lambda *a: pytest.fail('ninth ping'))
        assert result == {'fired': False, 'reason': 'ledger-locked'}
        mod_action_booking._append(facade._keepwarm_ledger_path(), attempt(7, sid='legacy', ts=now))
    assert safety.fire_headless(facade, first, now=now, runner=proved)['reason'] == 'cap-day'
    assert len(facade._keepwarm_load_ledger_rows()) == 8


@pytest.mark.parametrize('failure', ['partial-transcript', 'end-marker', 'end-inbox'])
def test_pending_survives_unavailable_evidence(m, tmp_path, monkeypatch, failure):
    facade, _ = m
    transcript = _stop_transcript(tmp_path)
    expected = safety.terminal_turn(transcript)
    now = time.time()
    with monkeypatch.context() as busy:
        busy.setattr(facade, '_keepwarm_lock', lambda: nullcontext(False))
        assert facade.write_keepwarm_arm_record('waiting', transcript, parent_pid=2) is None
    with monkeypatch.context() as unavailable:
        if failure == 'partial-transcript':
            with transcript.open('ab') as stream:
                stream.write(b'{"type":"system"}')
        elif failure == 'end-inbox':
            listdir = os.listdir
            def busy_directory(path):
                if Path(path).name == 'kw2-ends':
                    raise PermissionError('temporary inbox read denial')
                return listdir(path)
            unavailable.setattr(os, 'listdir', busy_directory)
        else:
            read = Path.read_text
            def denied(path, *args, **kwargs):
                if path == safety._end_path(facade.SNAPSHOT_DIR, 'waiting'):
                    raise PermissionError('temporarily unreadable')
                return read(path, *args, **kwargs)
            unavailable.setattr(Path, 'read_text', denied)
        rearm.recover_pending(facade, now)
        assert facade.load_keepwarm_records() == []
        assert len(list(facade.SNAPSHOT_DIR.glob('kw2-stop-*'))) == 1
    if failure == 'partial-transcript':
        with transcript.open('ab') as stream:
            stream.write(b'\n')
    for _ in range(2):
        rearm.recover_pending(facade, now)
    record, = facade.load_keepwarm_records()
    assert record['terminal_turn'] == expected and record['session_id'] == 'waiting'
    assert not list(facade.SNAPSHOT_DIR.glob('kw2-stop-*'))


def test_end_while_state_busy_retires_staged_stop(m, tmp_path, monkeypatch):
    facade, _ = m
    with monkeypatch.context() as busy:
        busy.setattr(facade, '_keepwarm_lock', lambda: nullcontext(False))
        assert facade.write_keepwarm_arm_record('ended', _stop_transcript(tmp_path), parent_pid=2) is None
    with facade._keepwarm_lock() as held:
        assert held and safety.mark_ended(facade.SNAPSHOT_DIR, 'ended')
    rearm.recover_pending(facade, time.time())
    assert not facade.load_keepwarm_records()
    assert not list(facade.SNAPSHOT_DIR.glob('kw2-stop-*'))


def test_stop_publication_needs_no_link_rename_or_second_owner_probe(m, tmp_path, monkeypatch):
    facade, _ = m
    def unsupported(*args, **kwargs):
        raise OSError(errno.ENOTSUP, 'fixture denies link or rename')
    monkeypatch.setattr(os, 'link', unsupported)
    monkeypatch.setattr(os, 'replace', unsupported)
    monkeypatch.setattr(safety, 'owner_alive', lambda *args: pytest.fail('duplicate owner probe'))
    record = facade.write_keepwarm_arm_record('direct', _stop_transcript(tmp_path), parent_pid=2)
    assert record and facade.load_keepwarm_records() == [record]
    with monkeypatch.context() as busy:
        busy.setattr(facade, '_keepwarm_lock', lambda: nullcontext(False))
        assert facade.write_keepwarm_arm_record('waiting', _stop_transcript(tmp_path), parent_pid=2) is None
    rearm.recover_pending(facade, time.time())
    assert [r['session_id'] for r in facade.load_keepwarm_records()] == ['direct', 'waiting']


def test_old_rows_migrate_once_off_the_stop_path(m, tmp_path, monkeypatch):
    facade, now, first = ready_install(m, monkeypatch)
    legacy = facade.SNAPSHOT_DIR / 'keepwarm_state.jsonl'
    expired = dict(first, ts=now - facade._KEEPWARM_MAX_AGE_SECONDS - 1)
    live = dict(first, session_id='migrated')
    legacy.write_text((json.dumps(expired) + '\n') * 25000 + json.dumps(live) + '\n')
    read = builtins.open
    opens = []
    def observe(path, *args, **kwargs):
        if path == legacy:
            opens.append(path)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', observe)
    assert facade.write_keepwarm_arm_record('new', _stop_transcript(tmp_path), parent_pid=2)
    assert opens == []
    for _ in range(2):
        rearm.recover_pending(facade, now)
    assert opens == [legacy]
    assert facade.load_keepwarm_records()[-1] == live
    assert len(facade.load_keepwarm_records()) == 3


def test_temp_cleanup_after_proof_publication_does_not_cancel_ping(m, monkeypatch):
    facade, now, record = ready_install(m, monkeypatch)
    unlink = os.unlink
    denials = []
    def sharing(path, *args, **kwargs):
        if Path(path).name.startswith('.keepwarm-publish-') and not denials:
            denials.append(path)
            exc = PermissionError('scanner has private name open')
            exc.winerror = 32
            raise exc
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', sharing)
    assert safety.fire_headless(facade, record, now=now, runner=proved)['proof']
    assert len(denials) == 1 and not safety._headless_latch(facade).exists()
    assert [r['status'] for r in facade._keepwarm_load_ledger_rows()] == ['firing', 'ok']


def test_old_client_recovers_crashed_new_spending_lease(m, monkeypatch):
    from hook_runtime import LeaseLock
    from test_keepwarm_arm_dedup import SCRIPTS
    import hook_runtime
    facade, _ = m
    worker = """
import os, sys
sys.path.insert(0, sys.argv[1])
import measure
from _measure.mod_action_booking import _lock
with _lock(measure) as held:
    assert held
    os._exit(19)
"""
    result = subprocess.run([sys.executable, '-c', worker, str(SCRIPTS)], timeout=15)
    assert result.returncode == 19
    clock = time.time() + 86400
    monkeypatch.setattr(hook_runtime, 'time', SimpleNamespace(time=lambda: clock,
                        sleep=time.sleep, monotonic=time.monotonic))
    with LeaseLock(facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + '.lease'),
                   reclaim_released=True) as acquired:
        assert acquired


@pytest.mark.parametrize('exception', [False, True])
def test_state_lock_releases_after_exit(m, exception):
    facade, _ = m
    with pytest.raises(ValueError) if exception else nullcontext():
        with facade._keepwarm_lock() as held, facade._keepwarm_lock() as contender:
            assert held is True and contender is False
            if exception:
                raise ValueError('fixture')
    with facade._keepwarm_lock() as held:
        assert held is True
