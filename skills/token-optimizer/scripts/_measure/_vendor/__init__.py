"""Private, license-preserving runtime dependencies; no runtime installation."""
import importlib.util as _util
import sys as _sys
import threading as _threading


def _lazy(name):
    """Return module ``name`` deferred to first attribute access.

    The module object is registered in sys.modules immediately, but its code
    runs only on the first attribute miss, serialized by a lock. importlib's
    LazyLoader is lock-free on Python 3.9/3.10, where a second thread could
    observe the module mid-exec and raise AttributeError.
    """
    module = _sys.modules.get(name)
    if module is not None:
        return module
    spec = _util.find_spec(name)
    loader = spec.loader
    module = _util.module_from_spec(spec)
    lock = _threading.Lock()
    loaded = False

    def _getattr(attr):
        nonlocal loaded
        if not loaded:
            with lock:
                if not loaded:
                    loader.exec_module(module)
                    loaded = True
                    module.__dict__.pop('__getattr__', None)
        try:
            return module.__dict__[attr]
        except KeyError:
            raise AttributeError(
                "module {!r} has no attribute {!r}".format(name, attr)) from None

    module.__dict__['__getattr__'] = _getattr
    _sys.modules[name] = module
    return module


# The Python 3.9/3.10 ``tomllib`` fallback. Importers bind it at module load
# (``from _measure._vendor import tomli as tomllib``), but Tomli itself, with
# its imports and regex compiles, runs only when a caller first parses TOML,
# so hooks that never read TOML pay nothing for it.
tomli = _lazy(__name__ + ".tomli")
