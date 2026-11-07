"""Canonical fixture loading and source resolution for mixed split trees."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import re
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


def _fstring_inspection(segment):
    """The facade-prefix edits below, applied to one whole f-string's text."""
    segment = re.sub(r"_compat\.read_global\((?:_measure_facade|_compat\.facade\(\)),\s*(['\"])(\w+)\1\)",
                     r"\2", segment)
    segment = re.sub(r"\b_measure_facade\.__dict__\b", "globals()", segment)
    return re.sub(r"(?:\b_measure_facade|_compat\.facade\(\))\.", "", segment)


def _inspection_text(text):
    # Python 3.9 reports wrong positions for expressions INSIDE f-strings (exact
    # from 3.10), so each outermost f-string is rewritten as a whole from its
    # own (correct) span and the nodes inside it are skipped below.
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
    while True:
        tree = ast.parse(code)
        restored = None
        for handler in ast.walk(tree):
            if not (isinstance(handler, ast.ExceptHandler) and handler.name
                    and handler.name.startswith('_measure_exception_') and len(handler.body) == 2):
                continue
            binding, guarded = handler.body
            if not (isinstance(binding, ast.Assign) and len(binding.targets) == 1
                    and isinstance(binding.targets[0], ast.Attribute) and isinstance(binding.value, ast.Name)
                    and binding.value.id == handler.name and isinstance(guarded, ast.Try)
                    and not guarded.handlers and not guarded.orelse and len(guarded.finalbody) == 2):
                continue
            target = binding.targets[0]
            rendered_target = ast.unparse(target)
            if ast.unparse(target.value) not in ('_measure_facade', '_compat.facade()'):
                continue
            if [ast.unparse(n) for n in guarded.finalbody] != [rendered_target + ' = None', 'del ' + rendered_target]:
                continue
            restored = ast.ExceptHandler(type=handler.type, name=target.attr, body=guarded.body)
            rendered = ast.unparse(ast.fix_missing_locations(restored))
            if any(isinstance(parent, getattr(ast, "TryStar", ())) and handler in parent.handlers for parent in ast.walk(tree)):
                rendered = rendered.replace('except ', 'except* ', 1)
            rendered = rendered.replace('\n', '\n' + ' ' * handler.col_offset)
            _, _, position = positions(code)
            code = (code[:position(handler.lineno, handler.col_offset)] + rendered
                    + code[position(handler.end_lineno, handler.end_col_offset):])
            break
        if restored is None:
            break
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
    tree = ast.parse(code)
    in_fstring = set()
    for node in ast.walk(tree):  # breadth-first: an outer f-string comes first
        if isinstance(node, ast.JoinedStr) and id(node) not in in_fstring:
            in_fstring.update(id(n) for n in ast.walk(node))
            left, right = position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)
            edits.append((left, right, _fstring_inspection(code[left:right])))
    for node in ast.walk(tree):
        if id(node) in in_fstring:
            continue
        if (isinstance(node, ast.Call) and ast.unparse(node.func) == '_compat.read_global'
                and len(node.args) == 2 and ast.unparse(node.args[0]) in ('_measure_facade', '_compat.facade()')
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                and node.args[1].value.isidentifier()):
            edits.append((position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset),
                          node.args[1].value))
        elif isinstance(node, ast.Attribute):
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
    span = _definition_spans(source).get(value.__name__)
    if span is None:
        raise StopIteration  # what the uncached next() lookup raised
    first, last = span
    return '\n'.join(source.splitlines()[first-1:last]) + '\n'


@lru_cache(maxsize=4)
def _definition_spans(source):
    """First top-level definition of each name: (first line incl. decorators, end line).

    Spans, not the tree: an AST of the whole 66k-line view is large to keep."""
    spans = {}
    for node in ast.parse(source).body:
        if isinstance(node, _tool().DEFINITION) and node.name not in spans:
            spans[node.name] = (min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno)
    return spans


# Built views, keyed by the facade path and the content of every file a view
# is built from. One build parses ~1,250 bodies (~50 s on a fast Mac), and 54
# test files ask for the view, three of them at import, so without this every
# pytest process paid minutes per call. Content-keyed, so an edited fixture
# tree gets a fresh view. Bounded: a few trees per session at ~3 MB each.
_VIEWS = {}
_VIEW_LIMIT = 4


def _view_key(path):
    import hashlib
    digest = hashlib.sha256()
    for file in (path, *sorted((path.parent / '_measure').glob('*.py')), Path(_tool().MANIFEST)):
        digest.update(str(file).encode('utf-8') + b'\0')
        try:
            digest.update(file.read_bytes())
        except OSError:
            digest.update(b'\0<missing>')
        digest.update(b'\0')
    return str(path), digest.hexdigest()


def measure_source(path=None):
    """A whole-program inspection view, preserving old unqualified spellings.

    Executable extraction tests use definition_nodes instead. This view makes
    literal forbidden-pattern checks continue to inspect every implementation.
    """
    path = Path(path or SCRIPTS / 'measure.py')
    if path.name != 'measure.py':
        return path.read_text(encoding='utf-8')
    key = _view_key(path)
    view = _VIEWS.get(key)
    if view is None:
        view = _build_measure_source(path)
        while len(_VIEWS) >= _VIEW_LIMIT:
            _VIEWS.pop(next(iter(_VIEWS)))
        _VIEWS[key] = view
    return view


def _build_measure_source(path):
    source = path.read_text(encoding='utf-8')
    tool = _tool()
    # Each provider is read and parsed once per build, not once per symbol.
    provider_spans = {}
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
        if origin not in provider_spans:
            body = origin.read_text(encoding='utf-8')
            spans = {}
            for n in ast.parse(body).body:
                if isinstance(n, tool.DEFINITION) and n.name not in spans:
                    spans[n.name] = (n.lineno, n.end_lineno)
            provider_spans[origin] = (body.splitlines(), spans)
        body_lines, spans = provider_spans[origin]
        if name not in spans:
            raise StopIteration  # what the per-symbol next() lookup raised
        first, last = spans[name]
        text = '\n'.join(body_lines[first-1:last])
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
