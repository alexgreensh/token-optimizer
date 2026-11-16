"""Cache parsed local configuration, while leaving project fallback live."""
import copy
import threading

_cache = {}
_lock = threading.Lock()


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
