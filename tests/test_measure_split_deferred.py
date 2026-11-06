"""Deferred providers: loaded on first use, otherwise identical to eager binding.

Perf attribution 2026-11-06: UserPromptSubmit ran code from 6 of 13 domains
but unmarshalled all 13 on every prompt. _measure.DEFERRED_MODULES load on the
first lookup of any of their facade names.
"""
import ast
import os
import shutil
import sys
from pathlib import Path

import pytest
from helpers.measure_split import isolated_measure

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
pytestmark = pytest.mark.skipif(bool(os.environ.get('MEASURE_SPLIT_SOURCE')), reason='split tree only')


def _deferred():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import _measure
        return _measure.DEFERRED_MODULES, _measure.DOMAIN_MODULES
    finally:
        sys.path.remove(str(SCRIPTS))


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
    (home / '.claude').mkdir(parents=True)
    return {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
            'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}


def test_no_module_level_facade_code_reads_a_deferred_name():
    """Until the next bind() call a deferred name holds a placeholder, so
    module-level code and binding arguments must only use eager names."""
    deferred, _ = _deferred()
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


def test_deferred_domains_load_on_first_use_and_bind_like_eager(tmp_path):
    deferred, domains = _deferred()
    _tree, bindings = _bindings()
    with isolated_measure(env=_env(tmp_path)) as m:
        for domain in domains:
            assert (('_measure.' + domain) in sys.modules) == (domain not in deferred), domain
        savings = [name for name, (domain, _n) in bindings.items() if domain == 'savings']
        assert savings[0] not in vars(m) and savings[0] in dir(m)
        live = getattr(m, savings[0])
        provider = sys.modules['_measure.savings']
        for name in savings:  # the whole domain bound at once, fresh for this facade
            assert vars(m)[name] is getattr(provider, name)
            assert getattr(m, name).__module__ == m.__name__
        assert live is getattr(provider, savings[0])
        assert '_measure.audit' not in sys.modules
        with pytest.raises(AttributeError, match=r"module 'measure' has no attribute 'no_such_name'"):
            m.no_such_name
        assert '_measure.cli' not in sys.modules
        assert m._measure_providers.load('cli') is sys.modules['_measure.cli']
        assert '__getattr__' in vars(m) and not any(type(v).__name__ == '_Binding' for v in vars(m).values())
        assert sorted(set(dir(m))) == dir(m)


def test_deleted_deferred_name_stays_deleted(tmp_path):
    _deferred_modules, _ = _deferred()
    _tree, bindings = _bindings()
    name = next(n for n, (domain, _node) in bindings.items() if domain == 'audit')
    with isolated_measure(env=_env(tmp_path)) as m:
        getattr(m, name)
        delattr(m, name)
        assert not hasattr(m, name)


def test_missing_deferred_provider_still_fails_the_facade_import(tmp_path):
    tree = tmp_path / 'scripts'
    shutil.copytree(SCRIPTS, tree, ignore=shutil.ignore_patterns('__pycache__'))
    (tree / '_measure/audit.py').unlink()
    with pytest.raises(ModuleNotFoundError, match=r"_measure\.audit"):
        with isolated_measure(tree / 'measure.py', env=_env(tmp_path)):
            pass
