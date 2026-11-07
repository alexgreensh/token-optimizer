"""Private, license-preserving runtime dependencies; no runtime installation."""
import importlib.util
import os as _os
import sys as _sys


def _lazy(name):
    """Return module ``name`` deferred to first attribute access.

    The module object is registered in sys.modules immediately, but its code
    runs only on the first attribute miss. No lock is held across that exec:
    the shim's own __getattr__ keeps resolving parser attributes lazily, and
    a fork child detects the inherited in-flight exec by pid and binds the
    parser module directly instead of re-entering dead import state.
    """
    module = _sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    pid = _os.getpid()

    def _getattr(attr):
        if _os.getpid() == pid:
            spec.loader.exec_module(module)
            try:
                return module.__dict__[attr]
            except KeyError:
                pass
            # The shim installed its own lazy __getattr__ during exec; it
            # resolves parser attributes on demand. Anything else missing is
            # a genuine AttributeError from that resolver.
            module_getattr = module.__dict__.get('__getattr__')
            if module_getattr is not None and module_getattr is not _getattr:
                return module_getattr(attr)
        else:
            # Fork child: the parent's first exec of ``name`` was in flight,
            # so its import lock is inherited held by a thread that does not
            # exist here and exec_module could never finish. Bind the parser
            # module's public names directly; _tomli's own lock is free.
            impl = importlib.import_module('_tomli')
            for key in ('TOMLDecodeError', '__version__', 'load', 'loads', '__all__'):
                try:
                    module.__dict__[key] = getattr(impl, key)
                except AttributeError:
                    pass
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
