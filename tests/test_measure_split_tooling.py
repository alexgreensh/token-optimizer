"""The mechanical mover must respect Python scopes and original binding time."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))


def tooling():
    import measure_split
    return measure_split


def test_scope_rewrite_keeps_locals_strings_and_comprehension_iterable():
    source = '''STATE = 4
helper = None
def work(value, captured=STATE):
    """helper STATE"""
    local = [STATE for STATE in range(STATE)]
    def nested(STATE):
        return helper(STATE)
    return helper(value), local, nested(2), captured
'''
    moved, binding = tooling().extract_definition(source, 'work', 'runtime')
    assert 'helper STATE' in moved
    assert '_measure_facade.helper(value)' in moved
    assert 'for STATE in range(_measure_facade.STATE)' in moved
    assert 'helper(STATE)' in moved
    assert 'defaults=(STATE,)' in binding
    compile(moved, 'runtime.py', 'exec')


def test_global_writes_and_globals_dictionary_use_facade():
    source = '''CACHE = None
def work():
    global CACHE
    CACHE = globals().get("CACHE", 1)
    del CACHE
'''
    moved, _ = tooling().extract_definition(source, 'work', 'runtime')
    assert '_measure_facade.CACHE = _measure_facade.__dict__.get' in moved
    assert 'del _measure_facade.CACHE' in moved
    assert 'global CACHE' not in moved


def test_module_level_builtin_uses_are_not_facade_bindings():
    source = '''VALUE = int("3")
try:
    float(VALUE)
except TypeError:
    pass
def work(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
'''
    moved, _ = tooling().extract_definition(source, 'work', 'runtime')
    assert 'return int(float(value))' in moved
    assert 'except (TypeError, ValueError):' in moved


def test_explicit_global_import_writes_back_to_facade():
    from types import SimpleNamespace
    source = '''CACHE = None
def work():
    global CACHE
    from json import loads as CACHE
    return CACHE("123")
'''
    active = SimpleNamespace(CACHE=None)
    moved, _ = tooling().extract_definition(source, 'work', 'runtime')
    namespace = {'_compat': SimpleNamespace(facade=lambda: active)}
    exec(moved, namespace)
    assert namespace['work']() == 123
    assert active.CACHE('456') == 456


def test_defaults_and_decorators_are_bound_at_original_position():
    source = '''VALUE = 7
def decorate(fn): return fn
@decorate
def work(arg=VALUE, *, other=VALUE):
    return arg, other, VALUE
'''
    moved, binding = tooling().extract_definition(source, 'work', 'runtime')
    assert '@decorate' not in moved
    assert 'arg=None' in moved and 'other=None' in moved
    assert 'defaults=(VALUE,)' in binding
    assert "kwdefaults={'other': VALUE}" in binding
    assert 'decorators=(decorate,)' in binding


def test_class_and_comprehension_scopes_are_distinct():
    source = '''VALUE = 3
class Example:
    VALUE = 5
    values = [VALUE for _ in range(VALUE)]
    def method(self):
        return VALUE
'''
    moved, _ = tooling().extract_definition(source, 'Example', 'runtime')
    assert 'range(VALUE)' in moved
    assert '_compat.facade().VALUE for _' in moved
    assert 'return _measure_facade.VALUE' in moved
    compile(moved, 'runtime.py', 'exec')


def test_class_bodies_have_no_copied_facade_state():
    from types import SimpleNamespace
    source = '''VALUE = 3
class Example:
    VALUE = 5
    values = [VALUE for _ in range(VALUE)]
    def method(self):
        return VALUE
'''
    moved, _ = tooling().extract_definition(source, 'Example', 'runtime')
    namespace = {'_compat': SimpleNamespace(facade=lambda: SimpleNamespace(VALUE=3))}
    exec(moved, namespace)
    cls = namespace['Example']
    assert cls.values == [3] * 5
    assert cls().method() == 3
    assert '_measure_facade' not in cls.__dict__


def test_returned_lambda_resolves_the_facade_at_each_invocation():
    from types import SimpleNamespace
    source = '''STATE = 1
def work():
    return lambda: STATE
'''
    active = SimpleNamespace(STATE=1)
    moved, _ = tooling().extract_definition(source, 'work', 'runtime')
    namespace = {'_compat': SimpleNamespace(facade=lambda: active)}
    exec(moved, namespace)
    closure = namespace['work']()
    active = SimpleNamespace(STATE=2)
    assert closure() == 2


def test_source_resolver_finds_both_canary_and_unmoved():
    m = tooling()
    root = REPO / 'skills/token-optimizer/scripts'
    assert m.resolve_symbol(root, '_safe_int').name == 'runtime.py'
    assert m.resolve_symbol(root, '_normalize_model_name').name == 'measure.py'


def test_inventory_assigns_every_definition_and_statement():
    m = tooling()
    manifest = m.read_manifest()
    assert len(manifest['symbols']) > 1200
    assert len({s['name'] for s in manifest['symbols']}) == len(manifest['symbols'])
    assert all(s['module'] in m.DOMAINS for s in manifest['symbols'])
    assert manifest['provisional'] is True
    assert manifest['statements']


def test_inspection_view_preserves_literals_and_resolves_fstrings_and_lambdas():
    from helpers.measure_split import _inspection_text
    source = '''def work():
    _measure_facade = _compat.facade()
    return f"{_measure_facade.VALUE}", "_measure_facade.VALUE", lambda: ((_measure_facade := _compat.facade()), _measure_facade.VALUE)[1], _measure_facade.__dict__.get("VALUE")
'''
    resolved = _inspection_text(source)
    assert 'f"{VALUE}"' in resolved
    assert '"_measure_facade.VALUE"' in resolved
    assert 'lambda: VALUE' in resolved
    assert 'globals().get("VALUE")' in resolved


def test_cli_move_preserves_public_bytes_and_module_globals(tmp_path):
    import os
    import shutil
    import subprocess
    scripts = tmp_path / 'payload/skills/token-optimizer/scripts'
    shutil.copytree(REPO / 'skills/token-optimizer/scripts', scripts,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.codex').mkdir()
    env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home),
           'CLAUDE_CONFIG_DIR': str(home), 'CODEX_HOME': str(home / '.codex'),
           'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(home / 'data'),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
    commands = (['--version'], ['pricing-tier'], ['--runtime', 'codex', 'codex-status-line'])
    def run(argv):
        result = subprocess.run([sys.executable, str(scripts / 'measure.py'), *argv], env=env,
                                cwd=home, input=b'', capture_output=True, timeout=30)
        return result.returncode, result.stdout, result.stderr
    before = [run(argv) for argv in commands]
    tooling().move_cli(scripts)
    assert tooling().resolve_symbol(scripts, '__main__').name == 'cli.py'
    assert [run(argv) for argv in commands] == before
    from helpers.measure_split import measure_source
    resolved = measure_source(scripts / 'measure.py')
    import ast
    ast.parse(resolved)
    assert 'elif args[0] == "compact-restore":' in resolved
    assert 'elif args[0] == "keepwarm-scheduler":' in resolved
