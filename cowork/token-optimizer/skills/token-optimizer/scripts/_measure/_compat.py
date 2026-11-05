"""Keep old global lookups and definition-time binding on the active facade."""
from __future__ import annotations

import importlib
import sys
from types import ModuleType


def facade():
    """Resolve once per invocation, never once per global reference."""
    return sys.modules['measure']


def prepare(module):
    """Select the active facade and give this initialization fresh definitions.

    Ordinary reload creates fresh functions without changing functions already
    exported by a previous facade. Providers carry no business state.
    """
    from . import DOMAIN_MODULES
    sys.modules['measure'] = module
    providers = {}
    for name in DOMAIN_MODULES:
        qualified = __package__ + '.' + name
        if qualified in sys.modules:
            provider = importlib.reload(sys.modules[qualified])
        else:
            provider = importlib.import_module(qualified)
        providers[name] = provider
    return providers


def bind(value, module_name, *, defaults=None, kwdefaults=None, decorators=()):
    """Bind captured defaults, identity and decorators at the old source site."""
    value.__module__ = module_name
    if defaults is not None:
        value.__defaults__ = defaults
    if kwdefaults is not None:
        value.__kwdefaults__ = kwdefaults
    for decorator in reversed(decorators):
        value = decorator(value)
    return value


class Namespace:
    """The source-extraction seam uses the test's supplied fake globals."""
    def __init__(self, values):
        object.__setattr__(self, '_values', values)

    def __getattribute__(self, name):
        values = object.__getattribute__(self, '_values')
        if name == '__dict__':
            return values
        try:
            return values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        object.__getattribute__(self, '_values')[name] = value

    def __delattr__(self, name):
        del object.__getattribute__(self, '_values')[name]


def namespace(values):
    return Namespace(values)
