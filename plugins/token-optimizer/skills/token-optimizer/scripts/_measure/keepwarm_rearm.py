"""Publish and watch successive pauses in the same terminal session."""
from contextlib import contextmanager
import json
import os
import sys
import tempfile
import time


def _legacy_writer_alive(owner):
    from .lease_candidate_cleanup import owner_is_gone

    # Holding the OS lock proves an earlier new-protocol lease is orphaned,
    # including after PID reuse. An old process only held the pathname lease.
    if owner.get('expires_wall') == sys.float_info.max or owner.get('released'):
        return False
    pid = owner.get('pid')
    return type(pid) is int and 0 < pid < 2**31 and not owner_is_gone(pid)


def _displaced_legacy_writer(lease):
    # Old releases keep a private candidate until they leave their critical
    # section, even if another old writer took over their expired pathname.
    prefix = '.' + lease.path.name + '.candidate-'
    with os.scandir(lease.path.parent) as entries:
        for entry in entries:
            if (entry.name.startswith(prefix) and entry.name != prefix + lease.nonce
                    and entry.is_file(follow_symlinks=False)):
                try:
                    with open(entry.path, encoding='utf-8') as stream:
                        owner = json.load(stream)
                        if isinstance(owner, dict) and _legacy_writer_alive(owner):
                            return True
                except FileNotFoundError:
                    continue
                except ValueError:
                    # A partial candidate cannot yet have been linked as owner.
                    continue
    return False


@contextmanager
def os_lock(path, timeout):
    """One persistent inode. False means busy, None means unusable storage."""
    from hook_provenance import _acquire
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = _acquire(path, timeout, busy=False, nofollow=True)
    except OSError:
        fd = None
    if fd is None or fd is False:
        yield fd
        return
    try:
        yield True
    finally:
        os.close(fd)


def state_lock(directory, timeout=.075):
    return os_lock(directory / '.keepwarm-pauses-v2.lock', timeout)


@contextmanager
def writer_lock(facade, timeout):
    """The shared spending protocol used by 5.13.76 and 5.13.77."""
    from hook_runtime import LeaseLock

    path = facade.SNAPSHOT_DIR / facade._KEEPWARM_LOCK_NAME
    until = time.monotonic() + timeout
    with os_lock(path, timeout) as held:
        if not held:
            yield held
            return
        lease = LeaseLock(path.with_name(path.name + '.lease'), acquire_timeout=max(0, until - time.monotonic()),
                          cohort_throttle=False, reclaim_released=True,
                          may_reclaim_active=lambda owner: not _legacy_writer_alive(owner))
        with lease as leased:
            try:
                available = leased and not _displaced_legacy_writer(lease)
            except OSError:
                available = None
            yield lease if available else (None if lease.storage_error or available is None else False)


def verify_spend(lease):
    if hasattr(lease, '_owner_path') and not lease.path.samefile(lease._owner_path):
        raise OSError('reservation lease changed')


def publish_exclusive(path, content):
    """Readers see the complete record or no pathname. Never replace a claimant."""
    fd, temporary = tempfile.mkstemp(prefix='.keepwarm-publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
        os.link(temporary, path)
    finally:
        from hook_runtime import LeaseLock
        try:
            LeaseLock._retry_sharing_violation(lambda: os.unlink(temporary), attempts=3)
        except OSError:
            pass  # The published claim is already complete.


def _commit_arm(facade, record):
    """Caller holds the writer lock. Duplicate Stops share their ping history."""
    from . import keepwarm_safety
    from .mod_action_booking import _append

    for existing in reversed(facade.load_keepwarm_records(strict=True)):
        if existing.get('session_id') != record['session_id'] or existing.get('owner') != record.get('owner'):
            continue
        if keepwarm_safety.same_pause(existing, record):
            if 'terminal_turn' not in record or 'terminal_turn' in existing:
                return existing
            record = {**existing, 'terminal_turn': record['terminal_turn']}
            break
    _append(facade._keepwarm_sidecar_path(), record)
    return record


def _publish_stop(facade, record, path, now):
    from .keepwarm_safety import session_ended, terminal_turn

    result = None
    # Spending checks cache expiry against proved pings. Only expire the pause itself here.
    if now - record['ts'] <= facade._KEEPWARM_MAX_AGE_SECONDS:
        turn = terminal_turn(facade.Path(record['transcript_path']))
        ended = session_ended(facade.SNAPSHOT_DIR, record['session_id'], record['ts'])
        if ended is None or (not ended and turn is None):
            return None  # Retry an unreadable marker or incomplete transcript.
        if not ended and turn == record['terminal_turn']:
            result = _commit_arm(facade, record)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # The tick can acknowledge an already committed Stop.
    return result


def publish_arm(facade, record, retry=None):
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    pending = None
    if 'terminal_turn' in record:
        fd, name = tempfile.mkstemp(prefix='kw2-stop-', dir=facade.SNAPSHOT_DIR)
        pending = facade.Path(name)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, allow_nan=False)
    with facade._keepwarm_lock() as held:
        if held:
            if pending is not None:
                return _publish_stop(facade, record, pending, facade.time.time())
            return _commit_arm(facade, record)
        if held is False and pending is not None and retry is not None:
            retry.append((record, pending))


def retry_due_stop(facade, record, path):
    from .keepwarm_auto import BUCKETS

    if facade.time.time() - record['last_turn_ts'] >= BUCKETS[0]:
        # Fresh pauses can wait for the scheduler. A due one may have less
        # than a minute left, so let this worker claim it as the writer exits.
        with state_lock(facade.SNAPSHOT_DIR, 60) as held:
            if held:
                return _publish_stop(facade, record, path, facade.time.time())


def _import_legacy(facade, now):
    from codex_io import atomic_write_json
    from .mod_action_booking import _append

    directory = facade.SNAPSHOT_DIR
    marker = directory / '.kw2-imported'
    try:
        previous = {} if marker.is_symlink() else json.loads(marker.read_bytes())
    except (FileNotFoundError, ValueError):
        previous = {}  # 5.13.78 left an empty marker.
    stamps = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            legacy = entry.name == 'keepwarm_state.jsonl' or entry.name.startswith('keepwarm-pending-stop-')
            if legacy and entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                stamps[entry.name] = [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
    if stamps == previous:
        return
    imported = {(r['session_id'], r['ts']) for r in facade.load_keepwarm_records(strict=True)}
    for name, stamp in stamps.items():
        if not isinstance(previous, dict) or stamp != previous.get(name):
            rows = {(r['session_id'], r['ts']): r for r in facade.load_keepwarm_records(directory / name, strict=True)}
            for key, row in rows.items():
                if key not in imported and now - float(row['ts']) <= facade._KEEPWARM_MAX_AGE_SECONDS:
                    _append(facade._keepwarm_sidecar_path(), row)
                    imported.add(key)
    atomic_write_json(marker, stamps, replace_symlink=True)


def recover_pending(facade, now):
    with facade._keepwarm_lock() as held:
        if not held:
            return
        try:
            _import_legacy(facade, now)
        except OSError:
            pass  # Retry migration independently of current Stops.
        for path in facade.SNAPSHOT_DIR.glob('kw2-stop-*'):
            try:
                if not path.is_symlink():
                    row = json.loads(path.read_bytes())
                    if facade._valid_keepwarm_record(row):
                        _publish_stop(facade, row, path, now)
            except (OSError, KeyError, ValueError, TypeError):
                continue


def latest_terminal_arms(facade):
    # Proof for a previous pause can be appended after the next Stop.
    latest = {}
    for record in facade.load_keepwarm_records():
        sid = record['session_id']
        if record.get('owner') and (sid not in latest or float(record['ts']) >= float(latest[sid]['ts'])):
            latest[sid] = record
    return latest


def watch_terminal(facade, sid, fd, watch_delay):
    """Yield to a new worker or pick up its arm if it already saw our lock."""
    try:
        if not facade._keepwarm_valid_arg(sid):
            return 1
        os.fstat(fd)
        while True:
            record = latest_terminal_arms(facade).get(sid)
            delay = watch_delay(facade, record, fd)
            if delay is not None:
                if delay:
                    facade.time.sleep(delay)
                continue
            if os.name != 'posix':
                return 0
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
            latest = latest_terminal_arms(facade).get(sid)
            if not latest or latest == record:
                return 0
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
    finally:
        os.close(fd)
