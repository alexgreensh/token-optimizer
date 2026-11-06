"""Deferred providers: opt-in for the UserPromptSubmit runner, eager for everyone else.

Perf attribution 2026-11-06: UserPromptSubmit ran code from 6 of 13 domains
but unmarshalled all 13 on every prompt. The runner sets _measure.HOOK_IMPORT
just before `import measure`; only that facade initialization defers
_measure.DEFERRED_MODULES to their first lookup. Every other import (library
callers, tests, the CLI) keeps the eager namespace exactly as before.

The review repros (REVIEW-sol-perf-fix.md, findings 1-3) are the `review`
tests: a caller's direct assignment survives the first load, the default
namespace keeps every name for vars(), wildcard import and deletion, and a
provider that disappears fails the facade reload even when it is cached.
"""
import ast
import importlib
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from helpers.measure_split import isolated_measure

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
pytestmark = pytest.mark.skipif(bool(os.environ.get('MEASURE_SPLIT_SOURCE')), reason='split tree only')
MODES = ('default', 'hook')


def _package(scripts=SCRIPTS):
    sys.path.insert(0, str(scripts))
    try:
        import _measure
        return _measure
    finally:
        sys.path.remove(str(scripts))


def _bindings():
    tree = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            found[node.targets[0].id] = (node.value.args[0].value.slice.value, node)
    return tree, found


def _env(tmp_path):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True, exist_ok=True)
    return {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
            'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}


def _copied_scripts(tmp_path):
    """A plugin-shaped copy: the facade's upward .claude-plugin/plugin.json walk
    stops at this root instead of reaching the real home (Windows temp is under it)."""
    root = tmp_path / 'plugin'
    tree = root / 'skills/token-optimizer/scripts'
    shutil.copytree(SCRIPTS, tree, ignore=shutil.ignore_patterns('__pycache__'))
    (root / '.claude-plugin').mkdir()
    shutil.copy2(REPO / '.claude-plugin/plugin.json', root / '.claude-plugin/plugin.json')
    return tree


@contextmanager
def _measure_for(tmp_path, mode, path=None):
    """The facade as a library caller (default) or the UPS runner (hook) imports it:
    the runner imports this tree's package, sets the flag, then imports measure."""
    saved = {k: v for k, v in sys.modules.items() if k == '_measure' or k.startswith('_measure.')}
    for key in saved:
        sys.modules.pop(key)
    package = _package(Path(path).parent if path else SCRIPTS)
    package.HOOK_IMPORT = mode == 'hook'
    try:
        with isolated_measure(path, env=_env(tmp_path)) as m:
            yield m
    finally:
        package.HOOK_IMPORT = False
        for key in [k for k in sys.modules if k == '_measure' or k.startswith('_measure.')]:
            sys.modules.pop(key)
        sys.modules.update(saved)


# ------------------------------------------------------------ review repros

@pytest.mark.parametrize('mode', MODES)
def test_review_caller_assignment_survives_the_first_load(tmp_path, mode):
    """Finding 1: a direct assignment before any lookup in its domain wins."""
    fake = lambda: {'model': 'review-sentinel'}  # noqa: E731
    with _measure_for(tmp_path, mode) as m:
        m._read_codex_config = fake
        assert m._codex_config_model() == 'review-sentinel'  # a real sibling consumer
        assert m._read_codex_config is fake


@pytest.mark.parametrize('mode', MODES)
def test_review_dashboard_assignment_order_is_kept(tmp_path, mode):
    """Finding 1, tests/test_dashboard_selfheal_singleflight.py ordering."""
    fake = lambda *a, **k: None  # noqa: E731
    with _measure_for(tmp_path, mode) as m:
        m.generate_standalone_dashboard = fake
        assert callable(m._dispatch_dashboard)
        assert m.generate_standalone_dashboard is fake


def test_review_default_namespace_keeps_every_name(tmp_path):
    """Finding 2: library callers see vars(), wildcard import and deletion as before."""
    _tree, bindings = _bindings()
    with _measure_for(tmp_path, 'default') as m:
        assert set(bindings) <= set(vars(m))
        assert not any(type(v).__name__ == '_Binding' for v in vars(m).values())
        assert '__getattr__' not in vars(m)
        star = {}
        exec('from measure import *', star)
        assert 'savings_report' in star and 'status_bar_payload' in star
        del m.savings_report
        assert not hasattr(m, 'savings_report')


@pytest.mark.parametrize('mode', MODES)
def test_review_cached_provider_that_disappears_fails_the_reload(tmp_path, mode):
    """Finding 3: a removed provider fails the facade reload even when cached."""
    tree = _copied_scripts(tmp_path)
    with _measure_for(tmp_path, mode, tree / 'measure.py') as m:
        m._codex_config_model()
        assert '_measure.audit' in sys.modules
        (tree / '_measure/audit.py').unlink()
        sys.modules['_measure'].HOOK_IMPORT = mode == 'hook'
        with pytest.raises(ModuleNotFoundError, match=r"_measure\.audit"):
            importlib.reload(m)
        sys.modules['_measure'].HOOK_IMPORT = False


# ------------------------------------------------------------ hook import

def test_no_module_level_facade_code_reads_a_deferred_name():
    """Until the next bind() call a deferred name holds a placeholder, so
    module-level code and binding arguments must only use eager names."""
    deferred = _package().DEFERRED_MODULES
    tree, bindings = _bindings()
    names = {name for name, (domain, _node) in bindings.items() if domain in deferred}
    assert names
    used = []
    for node in tree.body:
        is_binding = any(node is b for _d, b in bindings.values())
        parts = ([*node.value.args[1:], *(k.value for k in node.value.keywords)] if is_binding else [node])
        for part in parts:
            used += [(sub.id, node.lineno) for sub in ast.walk(part)
                     if isinstance(sub, ast.Name) and sub.id in names]
    assert used == []
    # Each deferred binding leaves the namespace at the next bind() call, so
    # the facade's last binding must belong to an eager domain.
    last = [domain for domain, _node in bindings.values()][-1]
    assert last not in deferred, last


def test_hook_import_defers_and_binds_like_eager(tmp_path):
    _tree, bindings = _bindings()
    with _measure_for(tmp_path, 'hook') as m:
        package = sys.modules['_measure']
        assert package.HOOK_IMPORT is False  # consumed by this initialization only
        providers = m._measure_providers  # this facade's own; sys.modules is shared across tests
        for domain in package.DOMAIN_MODULES:
            assert dict.__contains__(providers, domain) == (domain not in package.DEFERRED_MODULES), domain
        savings = [name for name, (domain, _n) in bindings.items() if domain == 'savings']
        assert savings[0] not in vars(m) and savings[0] in dir(m)
        live = getattr(m, savings[0])
        provider = sys.modules['_measure.savings']
        for name in savings:  # the whole domain bound at once, fresh for this facade
            assert vars(m)[name] is getattr(provider, name)
            assert getattr(m, name).__module__ == m.__name__
        assert live is getattr(provider, savings[0])
        assert not dict.__contains__(providers, 'audit')
        with pytest.raises(AttributeError, match=r"module 'measure' has no attribute 'no_such_name'"):
            m.no_such_name
        assert not dict.__contains__(providers, 'cli')
        assert providers.load('cli') is sys.modules['_measure.cli']
        assert not any(type(v).__name__ == '_Binding' for v in vars(m).values())
    with _measure_for(tmp_path, 'default') as m:  # the next initialization is eager again
        assert all(dict.__contains__(m._measure_providers, d) for d in sys.modules['_measure'].DOMAIN_MODULES)


@pytest.mark.parametrize('mode', MODES)
def test_missing_deferred_provider_still_fails_the_facade_import(tmp_path, mode):
    tree = _copied_scripts(tmp_path)
    (tree / '_measure/audit.py').unlink()
    with pytest.raises(ModuleNotFoundError, match=r"_measure\.audit"):
        with _measure_for(tmp_path, mode, tree / 'measure.py'):
            pass


def test_cli_and_main_runs_stay_eager(tmp_path):
    """The CLI never sets the flag: its __main__ guard calls the real cli.main,
    so a crash traceback keeps exactly its frames."""
    with _measure_for(tmp_path, 'default') as m:
        assert type(m._measure_providers['cli']) is type(sys)


def test_userpromptsubmit_runner_opts_in_and_resets():
    tree = ast.parse((REPO / 'hooks/userpromptsubmit_runner.py').read_text(encoding='utf-8'))
    flags = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(ast.unparse(t).endswith('.HOOK_IMPORT') for t in n.targets)]
    assert any(f.endswith('= True') for f in flags) and any(f.endswith('= False') for f in flags), flags


def test_review2_hook_to_default_reload_retires_the_lazy_handlers(tmp_path):
    """REVIEW-sol-perf-fix2 finding 1: an eager reload of the same module object
    after a hook-mode import leaves no lazy lookup behind."""
    with _measure_for(tmp_path, 'hook') as m:
        assert '__getattr__' in vars(m)
        importlib.reload(m)  # HOOK_IMPORT was consumed: this re-initialization is eager
        assert not m._measure_providers.deferred
        assert '__getattr__' not in vars(m) and '__dir__' not in vars(m)
        del m.savings_report
        assert not hasattr(m, 'savings_report')
