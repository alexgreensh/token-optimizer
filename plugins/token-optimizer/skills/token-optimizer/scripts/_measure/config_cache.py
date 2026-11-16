"""Cache parsed local configuration, while leaving project fallback live."""
import copy
import contextvars
import threading
from contextlib import contextmanager

_cache = {}
_lock = threading.Lock()
_tier = contextvars.ContextVar('status_bar_pricing_tier', default=None)


def current_tier():
    return _tier.get()


@contextmanager
def pricing_snapshot(facade):
    token = _tier.set(facade._load_pricing_tier())
    try:
        yield
    finally:
        _tier.reset(token)


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
