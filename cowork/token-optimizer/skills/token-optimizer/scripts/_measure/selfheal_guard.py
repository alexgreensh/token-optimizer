"""Bound the detached dashboard self-heal so it can never run away.

The self-heal child rebuilds the dashboard after a hook-path regen ran out of
time. On very large histories that rebuild used to run for tens of minutes and
grow to several GB, pushing the machine into swap. The child now:
  * runs at lower CPU priority,
  * exits itself once it passes a wall-clock or peak-memory ceiling,
  * leaves a backoff marker so hooks stop relaunching it for a day.
A user-opened dashboard is not affected: only the background heal is bounded.
"""
import os
import sys
import threading
import time

BACKOFF_NAME = "dashboard.heal.backoff"
_DEFAULT_MAX_SECONDS = 600
_DEFAULT_MAX_MB = 1536
_DEFAULT_BACKOFF_SECONDS = 24 * 3600
_POLL_SECONDS = 2.0


def _env_number(name, default):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def max_seconds():
    return _env_number("TOKEN_OPTIMIZER_SELFHEAL_MAX_SECONDS", _DEFAULT_MAX_SECONDS)


def max_mb():
    return _env_number("TOKEN_OPTIMIZER_SELFHEAL_MAX_MB", _DEFAULT_MAX_MB)


def backoff_seconds():
    return _env_number("TOKEN_OPTIMIZER_SELFHEAL_BACKOFF_SECONDS", _DEFAULT_BACKOFF_SECONDS)


def peak_rss_mb():
    """Peak resident memory of this process in MB, or None where unknown."""
    try:
        import resource
    except ImportError:  # Windows: the wall-clock cap still applies.
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def backoff_active(snapshot_dir, now=None):
    marker = snapshot_dir / BACKOFF_NAME
    try:
        age = (time.time() if now is None else now) - marker.stat().st_mtime
    except OSError:
        return False
    # File clocks can run a moment ahead of time.time() (seen on Windows).
    return -60 <= age < backoff_seconds()


def clear_backoff(snapshot_dir):
    try:
        (snapshot_dir / BACKOFF_NAME).unlink()
    except OSError:
        pass


def _write_backoff(snapshot_dir, reason):
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / BACKOFF_NAME).write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass


def check(snapshot_dir, started, now=None, rss_mb=None):
    """Return the reason to stop, or None. Split out so tests need no thread."""
    elapsed = (time.monotonic() if now is None else now) - started
    if elapsed > max_seconds():
        return "time %ds > %ds" % (elapsed, max_seconds())
    rss = peak_rss_mb() if rss_mb is None else rss_mb
    if rss is not None and rss > max_mb():
        return "memory %dMB > %dMB" % (rss, max_mb())
    return None


def start(snapshot_dir, exit_fn=os._exit):
    """Arm the ceilings for this process. Call once, in the self-heal child only."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    started = time.monotonic()

    def watch():
        while True:
            time.sleep(_POLL_SECONDS)
            reason = check(snapshot_dir, started)
            if reason:
                _write_backoff(snapshot_dir, reason)
                exit_fn(3)
                return

    thread = threading.Thread(target=watch, name="to-selfheal-guard", daemon=True)
    thread.start()
    return thread
