"""Publish and watch successive pauses in the same terminal session."""
from contextlib import contextmanager
import os
import time


@contextmanager
def arm_lock(facade, transcript, turn):
    """A detached Stop can retry a busy writer while its own pause is current."""
    with facade._keepwarm_lock() as held:
        if held or turn is None or facade.current_deadline() is not None:
            yield held
            return

    from hook_runtime import LeaseLock
    from .keepwarm_safety import terminal_turn

    path = facade.SNAPSHOT_DIR / (facade._KEEPWARM_LOCK_NAME + '.lease')
    retry = LeaseLock(path, cohort_throttle=False, reclaim_released=True)
    # Give the current generation time to release or expire. Each acquisition
    # keeps its normal short timeout, so a resumed turn can abandon the wait.
    until = time.monotonic() + retry.lease_seconds + retry.reclaim_grace
    while terminal_turn(transcript) == turn:
        with retry as held:
            if held:
                yield True
                return
        if time.monotonic() >= until or not path.is_file():
            break
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
