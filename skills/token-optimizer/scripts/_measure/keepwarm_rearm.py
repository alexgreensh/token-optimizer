"""Publish and watch successive pauses in the same terminal session."""
from contextlib import contextmanager
import errno
import os
import stat
import time


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
        try:
            held = _acquire_writer(fd, timeout)
        except (OSError, ImportError):
            held = None
        yield held
    finally:
        # Keep the inode in place. Closing the descriptor releases the OS lock.
        os.close(fd)


@contextmanager
def arm_lock(facade, transcript, turn):
    """A detached Stop can retry a busy writer while its own pause is current."""
    from .keepwarm_safety import terminal_turn

    until = time.monotonic() + 30
    delay = 0.05
    while True:
        with facade._keepwarm_lock() as held:
            if held is not False or turn is None or facade.current_deadline() is not None:
                yield held
                return
        remaining = until - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        if terminal_turn(transcript) != turn:
            break
        delay = min(1, delay * 2)
    yield False


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
