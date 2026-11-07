"""Private, license-preserving runtime dependencies; no runtime installation."""
import importlib.util as _util
import sys as _sys


def _lazy(name):
    """Return module ``name`` with its code deferred to first attribute access."""
    module = _sys.modules.get(name)
    if module is not None:
        return module
    spec = _util.find_spec(name)
    spec.loader = _util.LazyLoader(spec.loader)
    module = _util.module_from_spec(spec)
    _sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The Python 3.9/3.10 ``tomllib`` fallback. Importers bind it at module load
# (``from _measure._vendor import tomli as tomllib``), but Tomli itself, with
# its imports and regex compiles, runs only when a caller first parses TOML,
# so hooks that never read TOML pay nothing for it.
tomli = _lazy(__name__ + ".tomli")
