"""Publish and watch successive pauses in the same terminal session."""
from contextlib import contextmanager
import errno
import json
import os
import stat
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


def _acquire_writer(fd, timeout):
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return None
    until = time.monotonic() + timeout
    while True:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK) and not (
                    os.name == 'nt' and exc.errno == errno.EACCES):
                raise
        remaining = until - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


@contextmanager
def writer_lock(facade, timeout):
    """True while locked, False on contention, None on a storage error."""
    path = facade.SNAPSHOT_DIR / facade._KEEPWARM_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), flags, 0o600)
    except OSError:
        yield None
        return
    try:
        until = time.monotonic() + timeout
        try:
            held = _acquire_writer(fd, timeout)
        except (OSError, ImportError):
            held = None
        if not held:
            yield held
            return
        # Older processes only know the lease. Wait for their live writer,
        # even after expiry. Their clock-based reclaimer must also leave our
        # lease alone until we release it. The next writer recovers a dead PID.
        from hook_runtime import LeaseLock

        lease = LeaseLock(path.with_name(path.name + '.lease'),
                          acquire_timeout=max(0, until - time.monotonic()),
                          lease_seconds=sys.float_info.max, cohort_throttle=False,
                          reclaim_released=True, may_reclaim_active=lambda owner: not _legacy_writer_alive(owner))
        with lease as leased:
            try:
                available = leased and not _displaced_legacy_writer(lease)
            except OSError:
                available = None
            yield None if lease.storage_error else available
    finally:
        # Keep the inode in place. Closing the descriptor releases the OS lock.
        os.close(fd)


@contextmanager
def arm_lock(facade, transcript, turn, expires=None):
    """A detached Stop can retry a busy writer while its own pause is current."""
    from .keepwarm_safety import terminal_turn

    delay = 0.05
    while True:
        with facade._keepwarm_lock() as held:
            if held is not False or turn is None or facade.current_deadline() is not None:
                yield held
                return
        if expires is None or facade.time.time() >= expires:
            break
        time.sleep(delay)
        if terminal_turn(transcript) != turn:
            break
        delay = min(1, delay * 2)
    yield False


def publish_exclusive(path, content):
    """Readers see the complete record or no pathname. Never replace a claimant."""
    fd, temporary = tempfile.mkstemp(prefix='.keepwarm-publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _commit_arm(facade, record):
    """Caller holds the writer lock. Duplicate Stops share their ping history."""
    from . import keepwarm_safety
    from .mod_action_booking import _append

    for existing in reversed(facade.load_keepwarm_records()):
        if existing.get('session_id') != record['session_id'] or existing.get('owner') != record['owner']:
            continue
        if keepwarm_safety.same_pause(existing, record):
            if 'terminal_turn' not in record or 'terminal_turn' in existing:
                return existing
            record = {**existing, 'terminal_turn': record['terminal_turn']}
            break
    _append(facade._keepwarm_sidecar_path(), record)
    return record


def publish_arm(facade, record, transcript, turn):
    from .keepwarm_auto import CACHE_LIFETIME
    from .keepwarm_safety import terminal_turn

    pending = None
    expires = record['last_turn_ts'] + CACHE_LIFETIME
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if turn is not None:
        pending = facade.SNAPSHOT_DIR / ('keepwarm-pending-stop-' + os.urandom(16).hex())
        publish_exclusive(pending, json.dumps(record, allow_nan=False))
    with arm_lock(facade, transcript, turn, expires) as held:
        if not held:
            return None
        current = turn is None or terminal_turn(transcript) == turn
        result = _commit_arm(facade, record) if current else None
        if pending:
            pending.unlink(missing_ok=True)
        return result


def recover_pending(facade, now):
    """Retry a detached worker's Stop even if it exited waiting for storage."""
    from .keepwarm_auto import CACHE_LIFETIME

    for path in facade.SNAPSHOT_DIR.glob('keepwarm-pending-stop-*'):
        with facade._keepwarm_lock() as held:
            if not held:
                return
            try:
                if path.is_symlink():
                    continue
                record = json.loads(path.read_text(encoding='utf-8'))
                if (facade._valid_keepwarm_record(record) and record.get('terminal_turn')
                        and now < record['last_turn_ts'] + CACHE_LIFETIME
                        and facade.classify_keepwarm_record(record, now=now) == 'still_paused'):
                    _commit_arm(facade, record)
                path.unlink(missing_ok=True)
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
