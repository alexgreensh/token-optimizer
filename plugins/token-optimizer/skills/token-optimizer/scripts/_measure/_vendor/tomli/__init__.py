# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Taneli Hukkinen
# Licensed to PSF under a Contributor Agreement.

# Compatibility shim: the canonical flat copy ships as the _tomli payload
# module so Devin's closed set can use it without _measure imports. This
# package exists only so `from _measure._vendor import tomli` keeps working;
# _vendor's lazy loader still defers the real parser to first TOML access.
import importlib as _importlib

_EXPORTS = ("TOMLDecodeError", "__version__", "load", "loads")


def __getattr__(attr):
    if attr not in _EXPORTS:
        raise AttributeError(
            "module '_measure._vendor.tomli' has no attribute {!r}".format(attr))
    mod = _importlib.import_module("_tomli")
    try:
        value = getattr(mod, attr)
    except AttributeError:
        # _tomli is still executing its own first import on this thread (an
        # import-time callback re-entered the facade). Hand back a resolver
        # that delegates once the parser module finishes; it is not cached,
        # so the real attribute binds on the next access.
        def value(*args, **kwargs):
            target = getattr(_importlib.import_module("_tomli"), attr, None)
            if target is None:
                return None
            globals()[attr] = target
            return target(*args, **kwargs)
        return value
    globals()[attr] = value
    return value


__all__ = ("loads", "load", "TOMLDecodeError")
