"""Keep old global lookups and definition-time binding on the active facade."""
from __future__ import annotations

import importlib
import importlib.machinery
import sys
import threading
from types import ModuleType

_LOCK = threading.RLock()
_UNSWEPT = []  # deferred bindings a facade statement assigned; the next bind() sweeps them


def facade():
    """Resolve once per invocation, never once per global reference."""
    return sys.modules['measure']


def read_global(module, name):
    """Exception targets retain Python's global/builtin lookup after cleanup."""
    values = vars(module)
    if name in values:
        return values[name]
    builtins = values.get('__builtins__', __builtins__)
    if not isinstance(builtins, dict):
        builtins = vars(builtins)
    try:
        return builtins[name]
    except KeyError:
        raise NameError(f"name '{name}' is not defined", **({'name': name} if sys.version_info >= (3, 10) else {})) from None


def prepare(module):
    """Select the active facade and give this initialization fresh definitions.

    Ordinary reload creates fresh functions without changing functions already
    exported by a previous facade. Providers carry no business state. Only an
    initialization the UserPromptSubmit runner flags (HOOK_IMPORT, consumed
    here) loads DEFERRED_MODULES on first use; every other one is eager.
    """
    from . import DEFERRED_MODULES, DOMAIN_MODULES
    package = sys.modules[__package__]
    hook, package.HOOK_IMPORT = getattr(package, 'HOOK_IMPORT', False) is True, False
    sys.modules['measure'] = module
    values = vars(module)
    for key in [k for k in ('__getattr__', '__dir__') if getattr(values.get(k), '_measure_lazy', False)]:
        del values[key]  # a previous lazy initialization's lookups
    providers = Providers(n for n in DOMAIN_MODULES if hook and n in DEFERRED_MODULES)
    for name in DOMAIN_MODULES:
        if name in providers.deferred:
            _require(name)
        else:
            providers[name] = _fresh(name)
    return providers


def _fresh(name):
    qualified = __package__ + '.' + name
    return importlib.reload(sys.modules[qualified]) if qualified in sys.modules else importlib.import_module(qualified)


def _require(name):
    """A missing deferred provider fails the facade import, cached or not."""
    qualified = __package__ + '.' + name
    if importlib.machinery.PathFinder.find_spec(qualified, sys.modules[__package__].__path__) is None:
        raise ModuleNotFoundError(f'No module named {qualified!r}', name=qualified)


class Providers(dict):
    """Provider modules by domain; a deferred one loads and binds all its names on first use."""
    def __init__(self, deferred):
        super().__init__()
        self.deferred = frozenset(deferred)
        self.pending, self.namespace = {}, None

    def __missing__(self, name):
        if name in self.deferred:
            return _Domain(self, name)
        raise KeyError(name)

    def load(self, name):
        with _LOCK:
            if dict.__contains__(self, name):
                return dict.__getitem__(self, name)
            provider = self[name] = _fresh(name)
            for key, binding in list(self.pending.items()):
                if binding.domain == name:
                    value = binding.resolve()
                    if key not in self.namespace:  # a caller's assignment wins
                        self.namespace[key] = value
                    del self.pending[key]
            return provider


class _Domain:
    """A deferred domain not loaded yet: attributes name its definitions."""
    def __init__(self, providers, domain):
        self.providers, self.domain = providers, domain

    def __getattr__(self, attr):
        return _Binding(self.providers, self.domain, attr)


class _Binding:
    """One definition of a deferred domain, resolved on first use."""
    __slots__ = ('providers', 'domain', 'attr', 'options', 'value', 'namespace')

    def __init__(self, providers, domain, attr):
        self.providers, self.domain, self.attr, self.options, self.value, self.namespace = providers, domain, attr, None, None, None

    def resolve(self):
        if self.value is None:
            module_name, options = self.options
            self.value = bind(getattr(self.providers.load(self.domain), self.attr), module_name, **options)
        return self.value

    def __call__(self, *args, **kwargs):  # only before its sweep; adds a traceback frame
        if self.options is None:  # a direct provider call, e.g. the facade's __main__ guard
            return getattr(self.providers.load(self.domain), self.attr)(*args, **kwargs)
        return self.resolve()(*args, **kwargs)


def _sweep():
    """Move assigned deferred bindings out of the namespace; __getattr__ (PEP 562) loads them."""
    with _LOCK:
        waiting = []  # bindings whose statement has not assigned them yet
        while _UNSWEPT:
            binding = _UNSWEPT.pop()
            namespace, providers = binding.namespace, binding.providers
            if namespace.get(binding.attr) is not binding:
                waiting.append(binding)
            elif binding.value is not None or dict.__contains__(providers, binding.domain):
                namespace[binding.attr] = binding.resolve()
            else:
                del namespace[binding.attr]
                providers.pending[binding.attr] = binding
                if providers.namespace is None:
                    providers.namespace = namespace
                    namespace['__getattr__'], namespace['__dir__'] = _lookups(namespace, providers)
        _UNSWEPT[:] = waiting


def _lookups(namespace, providers):
    def __getattr__(name):
        binding = providers.pending.get(name)
        if binding is not None:
            providers.load(binding.domain)
            if name in namespace:
                return namespace[name]
        error = AttributeError(f"module {namespace.get('__name__')!r} has no attribute {name!r}"); error.name = name; raise error  # name= is 3.10+

    def __dir__():
        return sorted(set(namespace) | set(providers.pending))
    __getattr__._measure_lazy = __dir__._measure_lazy = True
    return __getattr__, __dir__


def bind(value, module_name, *, defaults=None, kwdefaults=None, decorators=()):
    """Bind captured defaults, identity and decorators at the old source site;
    a deferred definition is recorded for its facade (the caller's globals)."""
    if _UNSWEPT:
        _sweep()
    if isinstance(value, _Binding):
        value.options = (module_name, {'defaults': defaults, 'kwdefaults': kwdefaults, 'decorators': decorators})
        value.namespace = sys._getframe(1).f_globals
        _UNSWEPT.append(value)
        return value
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
