"""Upgrade overlap and the last refresh window, using isolated records and fake pings."""
import builtins
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import time

import pytest

from test_keepwarm_arm_dedup import _stop_transcript, m
from test_keepwarm_auto import attempt, ready_install
from test_keepwarm_writer_races import proved
from _measure import keepwarm_rearm as rearm, keepwarm_safety as safety


def test_import_finds_late_legacy_writes(m, monkeypatch):
    facade, now, original = ready_install(m, monkeypatch)
    legacy = facade.SNAPSHOT_DIR / 'keepwarm_state.jsonl'
    legacy.write_text(json.dumps(original) + '\n')
    rearm.recover_pending(facade, now)
    with legacy.open('a') as stream:
        print(json.dumps(dict(original, session_id='late-state')), file=stream)
    pending = legacy.with_name('keepwarm-pending-stop-late')
    pending.write_text(json.dumps(dict(original, session_id='late-pending')))
    for _ in range(2):
        rearm.recover_pending(facade, now)
    assert sorted(r['session_id'] for r in facade.load_keepwarm_records()) == sorted(
        [original['session_id'], 'late-state', 'late-pending'])


def test_import_retries_denied_directory(m, monkeypatch):
    facade, now, original = ready_install(m, monkeypatch)
    path = facade.SNAPSHOT_DIR / 'keepwarm-pending-stop-before-upgrade'
    pending = dict(original, session_id='waiting')
    path.write_text(json.dumps(pending))
    with monkeypatch.context() as busy:
        def deny_directory(read):
            def listing(directory):
                if Path(directory) == path.parent:
                    raise PermissionError('fixture blocks the first scan')
                return read(directory)
            return listing
        for method in ('scandir', 'listdir'):
            busy.setattr(os, method, deny_directory(getattr(os, method)))
        rearm.recover_pending(facade, now)
    for _ in range(2):
        rearm.recover_pending(facade, now)
    assert facade.load_keepwarm_records() == [original, pending]


def transcript_at(tmp_path, stamp):
    path = _stop_transcript(tmp_path)
    row = json.loads(path.read_text())
    row['timestamp'] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    path.write_text(json.dumps(row) + '\n')
    return path


def worker(facade, sid, path, now):
    safety.run_flush_worker(facade, ['--trigger', 'stop', '--session-id', sid, '--transcript-path',
                            str(path), '--keepwarm-parent-pid', '123', '--keepwarm-parent-start',
                            'fixture', '--keepwarm-stop-at', str(now)])


def test_staged_second_refresh_uses_proved_clock(m, tmp_path, monkeypatch):
    facade, now, _ = ready_install(m, monkeypatch)
    sid = 'second-refresh'
    path = transcript_at(tmp_path, now - 6900)
    assert facade._keepwarm_ledger_append(attempt(1, sid=sid, ts=now - 3360, proof=True))
    with monkeypatch.context() as busy:
        busy.setattr(facade, '_keepwarm_lock', lambda: nullcontext(False))
        assert facade.write_keepwarm_arm_record(sid, path, parent_pid=123) is None
    rearm.recover_pending(facade, now)
    record = rearm.latest_terminal_arms(facade).get(sid)
    assert record is not None
    assert safety.fire_headless(facade, record, now=now, runner=proved)['proof']
    assert [r['status'] for r in facade._keepwarm_load_ledger_rows()] == ['ok', 'firing', 'ok']


@pytest.mark.skipif(os.name != 'posix', reason='detached terminal worker')
def test_due_worker_retries_after_flush(m, tmp_path, monkeypatch):
    facade, now, _ = ready_install(m, monkeypatch)
    path = transcript_at(tmp_path, now - 3560)
    events = []
    def finish_flush(args):
        events.append('flush')
        assert list(facade.SNAPSHOT_DIR.glob('kw2-stop-*'))
        time.sleep(.025)  # The initial publication already waited 75 ms.
        holder.close()
    def refresh(facade, sid):
        record = rearm.latest_terminal_arms(facade)[sid]
        assert safety.fire_headless(facade, record, now=now, runner=proved)['proof']
        events.append('refresh')
    monkeypatch.setattr(facade, '_run_session_end_flush_worker', finish_flush)
    monkeypatch.setattr(facade, '_daemon_snapshot_sandboxed', lambda: False)
    monkeypatch.setattr(safety, 'watch_armed_terminal', refresh)
    with ExitStack() as holder:
        assert holder.enter_context(facade._keepwarm_lock())
        worker(facade, 'due', path, now)
    assert events == ['flush', 'refresh']
    assert not list(facade.SNAPSHOT_DIR.glob('kw2-stop-*'))


@pytest.mark.skipif(os.name != 'posix', reason='detached terminal worker')
def test_small_context_reads_bounded_suffix_before_flush(m, tmp_path, monkeypatch):
    facade, _ = m
    path = transcript_at(tmp_path, time.time() - 60)
    row = json.loads(path.read_text())
    row['message']['usage'] = {'cache_read_input_tokens': 500}
    with path.open('w') as stream:
        stream.write((json.dumps({'type': 'system', 'text': 'x' * 16000}) + '\n') * 256)
        print(json.dumps(row), file=stream)
    reads, at_flush = [], []
    class Meter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.stream.close()
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __iter__(self):
            for line in self.stream:
                reads.append(len(line))
                yield line
        def read(self, *args):
            data = self.stream.read(*args)
            reads.append(len(data))
            return data
    def meter(opener):
        def open_file(file, *args, **kwargs):
            stream = opener(file, *args, **kwargs)
            return Meter(stream) if file == path or file == str(path) else stream
        return open_file
    for module in (builtins, io):
        monkeypatch.setattr(module, 'open', meter(module.open))
    monkeypatch.setattr(facade, '_run_session_end_flush_worker', lambda args: at_flush.append(sum(reads)))
    worker(facade, 'small', path, time.time())
    assert len(at_flush) == 1 and 0 < at_flush[0] < 2 * 1024 * 1024
    assert not facade.load_keepwarm_records()
