"""Canonical fixture loading and source resolution for mixed split trees."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import sys
from functools import lru_cache

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
_MISSING = object()


def exec_measure(module):
    """Execute a custom spec as the sole canonical facade.

    The surrounding fixture scope restores its predecessor, including when
    tests explicitly reload or pop the canonical entry.
    """
    package = sys.modules.get('_measure')
    if package is not None and Path(package.__file__).parent.parent != Path(module.__spec__.origin).parent:
        origin = Path(module.__spec__.origin)
        requires_package = any(isinstance(node, ast.ImportFrom) and node.module == '_measure'
                               for node in ast.parse(origin.read_text(encoding='utf-8')).body)
        if requires_package:
            if not (origin.parent / '_measure').is_dir():
                raise ImportError('fixture facade is missing its _measure package')
            for name in tuple(sys.modules):
                if name == '_measure' or name.startswith('_measure.'):
                    sys.modules.pop(name, None)
    sys.modules[module.__name__] = module
    sys.modules['measure'] = module
    module.__spec__.loader.exec_module(module)
    return module


def exec_source_module(module):
    """Other fixture modules retain their existing loader behavior."""
    if Path(module.__spec__.origin).name == 'measure.py':
        return exec_measure(module)
    module.__spec__.loader.exec_module(module)
    return module


def exec_facade_source(source, path, values=None):
    """Existing dispatch-injection tests execute inside a real module object."""
    from types import ModuleType
    module = ModuleType('__main__')
    module.__dict__.update(values or {})
    module.__file__ = str(path)
    previous_main, previous_measure = sys.modules.get('__main__'), sys.modules.get('measure')
    sys.modules['__main__'] = sys.modules['measure'] = module
    try:
        exec(compile(source, str(path), 'exec'), module.__dict__)
    finally:
        for name, previous in (('__main__', previous_main), ('measure', previous_measure)):
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@contextmanager
def isolated_measure(path=None, *, name='measure', env=None):
    before = dict(sys.modules)
    environment = dict(os.environ)
    search = list(sys.path)
    try:
        if env:
            os.environ.update({key: str(value) for key, value in env.items()})
        path = Path(path or os.environ.get('MEASURE_SPLIT_SOURCE') or SCRIPTS / 'measure.py')
        sys.path.insert(0, str(path.parent))
        # These siblings intentionally capture environment at import.
        for sibling in ('plugin_env', 'runtime_env'):
            sys.modules.pop(sibling, None)
        module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(name, path))
        yield exec_measure(module)
    finally:
        for key in tuple(sys.modules):
            if key == name or key == 'measure' or key in ('plugin_env', 'runtime_env') or key.startswith('_measure'):
                if key in before:
                    sys.modules[key] = before[key]
                else:
                    sys.modules.pop(key, None)
        os.environ.clear()
        os.environ.update(environment)
        sys.path[:] = search


@lru_cache(maxsize=1)
def _tool():
    path = REPO / 'scripts/measure_split.py'
    spec = importlib.util.spec_from_file_location('_measure_split_tool', path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def source_path(name, path=None):
    return _tool().resolve_symbol(Path(path or SCRIPTS / 'measure.py').parent, name)


def definition_nodes(names, namespace, path=None):
    """Compile actual moved bodies with the same facade seam and fake globals."""
    tool = _tool()
    manifest = {row['name']: row for row in tool.read_manifest()['symbols']}
    nodes = []
    for name in names:
        origin = source_path(name, path)
        node = next(n for n in ast.parse(origin.read_text(encoding='utf-8')).body
                    if isinstance(n, tool.DEFINITION) and n.name == name)
        if origin.name != 'measure.py':
            original = ast.parse(manifest[name]['signature'] + '\n    pass').body[0]
            if hasattr(node, 'args'):
                node.args = original.args
            node.decorator_list = original.decorator_list
            class NamespaceCalls(ast.NodeTransformer):
                def visit_Call(self, call):
                    if ast.unparse(call.func) == '_compat.facade':
                        return ast.copy_location(ast.parse('_compat.namespace(globals())', mode='eval').body, call)
                    return self.generic_visit(call)
            node = NamespaceCalls().visit(node)
            sys.path.insert(0, str(origin.parent.parent))
            from _measure import _compat
            namespace['_compat'] = _compat
        nodes.append(node)
    return ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))


def _inspection_text(text):
    # AST positions also cover expressions inside f-strings on Python 3.11.
    prefix = 'if True:\n' if text[:1].isspace() else ''
    code = prefix + text
    def positions(value):
        lines = value.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        def position(line, column):
            return offsets[line-1] + len(lines[line-1].encode('utf-8')[:column].decode('utf-8'))
        return lines, offsets, position
    # Remove only the tuple wrapper the mover adds to lambda invocations.
    while True:
        tree = ast.parse(code)
        wrapper = next((node for node in ast.walk(tree) if isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2
                        and isinstance(node.value.elts[0], ast.NamedExpr)
                        and isinstance(node.value.elts[0].target, ast.Name)
                        and node.value.elts[0].target.id == '_measure_facade'), None)
        if wrapper is None:
            break
        _, _, position = positions(code)
        original = ast.get_source_segment(code, wrapper.value.elts[1])
        code = code[:position(wrapper.lineno, wrapper.col_offset)] + original + code[position(wrapper.end_lineno, wrapper.end_col_offset):]
    lines, offsets, position = positions(code)
    edits = []
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id == '_measure_facade' and node.attr == '__dict__':
                edits.append((position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset), 'globals()'))
            elif (isinstance(value, ast.Name) and value.id == '_measure_facade' or
                    isinstance(value, ast.Call) and ast.unparse(value.func) == '_compat.facade'):
                edits.append((position(value.lineno, value.col_offset),
                              position(value.end_lineno, value.end_col_offset)+1, ''))
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and node.targets[0].id == '_measure_facade'
              and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == '_compat.facade'):
            left, right = position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)
            if code[right:right+1] == ';':
                right += 2
            elif not lines[node.lineno-1][:node.col_offset].strip():
                left, right = offsets[node.lineno-1], offsets[node.end_lineno]
            edits.append((left, right, ''))
    for left, right, replacement in sorted(edits, reverse=True):
        code = code[:left] + replacement + code[right:]
    return code[len(prefix):].rstrip('\n')


def inspect_source(value):
    """Keep introspection guards on the complete implementation."""
    import inspect
    if inspect.ismodule(value):
        path = Path(getattr(value, '__file__', ''))
        return measure_source(path) if path.name == 'measure.py' else inspect.getsource(value)
    code = getattr(inspect.unwrap(value), '__code__', None)
    if code is None:
        return inspect.getsource(value)
    path = Path(code.co_filename)
    if path.parent.name == '_measure':
        path = path.parent.parent / 'measure.py'
    if path.name != 'measure.py':
        return inspect.getsource(value)
    source = measure_source(path)
    node = next(node for node in ast.parse(source).body
                if isinstance(node, _tool().DEFINITION) and node.name == value.__name__)
    first = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
    return '\n'.join(source.splitlines()[first-1:node.end_lineno]) + '\n'


def measure_source(path=None):
    """A whole-program inspection view, preserving old unqualified spellings.

    Executable extraction tests use definition_nodes instead. This view makes
    literal forbidden-pattern checks continue to inspect every implementation.
    """
    path = Path(path or SCRIPTS / 'measure.py')
    source = path.read_text(encoding='utf-8')
    if path.name != 'measure.py':
        return source
    tool = _tool()
    records = {row['name']: row for row in tool.read_manifest()['symbols']}
    lines = source.splitlines(keepends=True)
    changes = []
    for node in ast.parse(source).body:
        if (isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
                and source_path('__main__', path).name == 'cli.py'):
            cli = source_path('__main__', path).read_text(encoding='utf-8')
            implementation = next(n for n in ast.parse(cli).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
            body = '\n'.join(cli.splitlines()[implementation.lineno:implementation.end_lineno])
            changes.append((node.lineno, node.end_lineno, 'if __name__ == "__main__":\n' + _inspection_text(body) + '\n'))
            continue
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            continue
        name = node.targets[0].id
        origin = source_path(name, path)
        body = origin.read_text(encoding='utf-8')
        impl = next(n for n in ast.parse(body).body if isinstance(n, tool.DEFINITION) and n.name == name)
        text = '\n'.join(body.splitlines()[impl.lineno-1:impl.end_lineno])
        text = _inspection_text(text)
        # Restore definition-time signatures for textual inspection.
        first_body = ast.parse(text).body[0].body[0].lineno - 1
        text = records[name]['signature'] + '\n' + '\n'.join(text.splitlines()[first_body:]) + '\n'
        restored = text.splitlines(keepends=True)
        for declaration in records[name].get('global_declarations', []):
            index = declaration['line']
            if '\n' not in declaration['text'] and index < len(restored) and restored[index].strip() == 'pass':
                indent = len(restored[index])-len(restored[index].lstrip())
                restored[index] = ' ' * indent + declaration['text'] + '\n'
        text = ''.join(restored)
        changes.append((node.lineno, node.end_lineno, text))
    for first, last, replacement in reversed(changes):
        lines[first-1:last] = [replacement]
    return ''.join(lines)
