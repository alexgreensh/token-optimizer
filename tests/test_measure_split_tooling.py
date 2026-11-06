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


@pytest.mark.parametrize('body', ['result = str(exc)', 'result = str(exc); del exc',
    'result = str(exc) + """\nunchanged literal\n"""',
    'result = f"""\nunchanged {exc}\n"""',
    'def read_error():\n            return str(exc)\n        result = read_error()'])
@pytest.mark.parametrize('star', [False, True])
def test_cli_exception_target_is_global_and_deleted_after_handler(tmp_path, body, star):
    from types import SimpleNamespace
    scripts = tmp_path / 'scripts'
    package = scripts / '_measure'
    package.mkdir(parents=True)
    source = ('if __name__ == "__main__":\n'
              '    exc = "old"\n'
              '    try:\n        raise ValueError("expected error")\n'
              '    except ValueError as exc:\n        ' + body + '\n'
              '    deleted = "exc" not in globals()\n'
              '    try:\n        exc\n'
              '    except NameError as missing:\n        missing_type = type(missing).__name__\n')
    if star:
        source = source.replace('raise ValueError("expected error")',
                                'raise ExceptionGroup("fixture", [ValueError("expected error")])')
        source = source.replace('except ValueError as exc:', 'except* ValueError as exc:')
    (scripts / 'measure.py').write_text(source, encoding='utf-8')
    (package / 'cli.py').write_text(tooling().HEADER, encoding='utf-8')
    before = {'__name__': '__main__'}
    exec(source, before)
    tooling().move_cli(scripts)
    active = SimpleNamespace(__name__='__main__')
    spec = importlib.util.spec_from_file_location('fixture_compat',
        REPO / 'skills/token-optimizer/scripts/_measure/_compat.py')
    compat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compat)
    compat.facade = lambda: active
    namespace = {'_compat': compat}
    # Import-free execution of the provider's actual main definition.
    import ast
    tree = ast.parse((package / 'cli.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<moved-cli>', 'exec'), namespace)
    namespace['main']()
    assert active.result == before['result']
    assert active.missing_type == before['missing_type'] == 'NameError'
    assert active.deleted == before['deleted'] is True
    assert not hasattr(active, 'exc')
    from helpers.measure_split import _inspection_text
    inspected = _inspection_text((package / 'cli.py').read_text(encoding='utf-8'))
    assert '_measure_exception_' not in inspected
    assert '_compat.read_global' not in inspected


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
    assert m.resolve_symbol(root, '_normalize_model_name').name == 'sessions.py'
    assert m.resolve_symbol(root, '_redirect_bytecode_out_of_repo').name == 'measure.py'


def test_inventory_assigns_every_definition_and_statement():
    m = tooling()
    manifest = m.read_manifest()
    assert len(manifest['symbols']) > 1200
    assert len({s['name'] for s in manifest['symbols']}) == len(manifest['symbols'])
    assert all(s['module'] in m.DOMAINS for s in manifest['symbols'])
    assert manifest['provisional'] is False
    assert manifest['base'] == '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f'
    assert manifest['statements']


def test_retained_bootstrap_is_owned_but_never_extractable(tmp_path):
    import measure_split_inventory as inventory
    m = tooling()
    manifest = m.read_manifest()
    lanes = inventory.ownership(manifest, REPO)['lanes']
    for packet in lanes.values():
        assert all(row['name'] != '_redirect_bytecode_out_of_repo' for row in packet['symbols'])
    assert any(row['name'] == '_redirect_bytecode_out_of_repo'
               for row in lanes['1']['retained_symbols'])
    with pytest.raises(ValueError, match='retained'):
        m.move(tmp_path, ['_redirect_bytecode_out_of_repo'])


def test_guard_rejects_a_binding_to_the_wrong_provider(tmp_path):
    import shutil
    import check_measure_split
    root = tmp_path / 'root'
    scripts = root / 'skills/token-optimizer/scripts'
    shutil.copytree(REPO / 'skills/token-optimizer/scripts', scripts,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (root / 'scripts').mkdir()
    shutil.copy2(REPO / 'scripts/measure_split_manifest.json', root / 'scripts')
    path = scripts / 'measure.py'
    source = path.read_text(encoding='utf-8')
    assert "_measure_providers['runtime']._safe_int" in source
    path.write_text(source.replace("_measure_providers['runtime']._safe_int",
                                   "_measure_providers['pricing']._safe_int"), encoding='utf-8')
    errors, _ = check_measure_split.check(root)
    assert any('wrong facade binding' in error for error in errors), errors


@pytest.mark.parametrize('lines, rejected', [(9000, False), (9001, True)])
def test_guard_applies_the_pass1_provider_ceiling(tmp_path, lines, rejected):
    """FILE-SIZE-POLICY.md pass 1: _measure/*.py may reach 9,000 lines, never 9,001."""
    import shutil
    import check_measure_split
    root = tmp_path / 'root'
    scripts = root / 'skills/token-optimizer/scripts'
    shutil.copytree(REPO / 'skills/token-optimizer/scripts', scripts,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (root / 'scripts').mkdir()
    shutil.copy2(REPO / 'scripts/measure_split_manifest.json', root / 'scripts')
    path = scripts / '_measure/runtime.py'
    text = path.read_text(encoding='utf-8')
    text += '\n' * (lines - len(text.splitlines()))
    path.write_text(text, encoding='utf-8')
    assert len(path.read_text(encoding='utf-8').splitlines()) == lines
    errors, _ = check_measure_split.check(root)
    assert any('runtime.py: exceeds 9000 lines' in error for error in errors) is rejected, errors
    assert not any('exceeds 1000 lines' in error for error in errors), errors


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


def test_mover_writes_lf_on_every_platform():
    """Windows text mode would write CRLF into facade and providers; every write pins LF."""
    import ast
    tree = ast.parse((REPO / 'scripts/measure_split.py').read_text(encoding='utf-8'))
    writes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == 'write_text']
    assert len(writes) >= 4
    for node in writes:
        newline = {kw.arg: kw.value for kw in node.keywords}.get('newline')
        assert isinstance(newline, ast.Constant) and newline.value == '\n', f'line {node.lineno}'
    opened = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'open']
    assert all('w' not in ast.unparse(node.args[1]) for node in opened if len(node.args) > 1), 'text open() for writing'
