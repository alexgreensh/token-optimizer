"""Cache parsed local configuration, while leaving project fallback live.

Also scopes what one status-bar refresh may skip (see skip_cache_scan)."""
import copy
import contextvars
import threading
from contextlib import contextmanager

_cache = {}
_lock = threading.Lock()
_tier = contextvars.ContextVar('status_bar_pricing_tier', default=None)
_skip_scan = contextvars.ContextVar('status_bar_skip_cache_scan', default=False)


def current_tier():
    return _tier.get()


@contextmanager
def pricing_snapshot(facade):
    token = _tier.set(facade._load_pricing_tier())
    try:
        yield
    finally:
        _tier.reset(token)


def cache_scan_skipped():
    return _skip_scan.get()


@contextmanager
def skip_cache_scan():
    """Inside a status-bar refresh, skip the 30-day cache-waste transcript scan.

    Its only output is the cache_drop opportunity block, which the status-bar
    payload and savings_headline never read; the dashboard and CLI still scan.
    """
    token = _skip_scan.set(True)
    try:
        yield
    finally:
        _skip_scan.reset(token)


def clear():
    with _lock:
        _cache.clear()


def read(path, json_module):
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with _lock:
            if key not in _cache:
                value = json_module.loads(path.read_text(encoding='utf-8'))
                _cache.clear()
                _cache[key] = value if isinstance(value, dict) else {}
            return copy.deepcopy(_cache[key])
    except (OSError, ValueError, TypeError):
        clear()
        return {}
