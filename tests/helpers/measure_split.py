"""Canonical fixture loading and source resolution for mixed split trees."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
_MISSING = object()


def exec_measure(module):
    """Execute a custom spec as the sole canonical facade.

    The surrounding fixture scope restores its predecessor, including when
    tests explicitly reload or pop the canonical entry.
    """
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
        path = Path(path or SCRIPTS / 'measure.py')
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


def measure_source(path=None):
    """A whole-program inspection view, preserving old unqualified spellings.

    Executable extraction tests use definition_nodes instead. This view makes
    literal forbidden-pattern checks continue to inspect every implementation.
    """
    import io
    import tokenize
    path = Path(path or SCRIPTS / 'measure.py')
    source = path.read_text(encoding='utf-8')
    if path.name != 'measure.py':
        return source
    tool = _tool()
    records = {row['name']: row for row in tool.read_manifest()['symbols']}
    lines = source.splitlines(keepends=True)
    changes = []
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            continue
        name = node.targets[0].id
        origin = source_path(name, path)
        body = origin.read_text(encoding='utf-8')
        impl = next(n for n in ast.parse(body).body if isinstance(n, tool.DEFINITION) and n.name == name)
        text = '\n'.join(body.splitlines()[impl.lineno-1:impl.end_lineno])
        # Token positions avoid touching strings or embedded generated scripts.
        token_lines = text.splitlines(keepends=True)
        offsets = [0]
        for line in token_lines:
            offsets.append(offsets[-1] + len(line))
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        edits = []
        for index, token in enumerate(tokens[:-1]):
            if token.type == tokenize.NAME and token.string == '_measure_facade' and tokens[index+1].string == '.':
                end = tokens[index+1].end
                edits.append((offsets[token.start[0]-1]+token.start[1], offsets[end[0]-1]+end[1], ''))
        for left, right, replacement in reversed(edits):
            text = text[:left] + replacement + text[right:]
        text = '\n'.join(line for line in text.splitlines() if '_measure_facade = _compat.facade()' not in line)
        # Restore definition-time signatures for textual inspection.
        first_body = ast.parse(text).body[0].body[0].lineno - 1
        text = records[name]['signature'] + '\n' + '\n'.join(text.splitlines()[first_body:]) + '\n'
        changes.append((node.lineno, node.end_lineno, text))
    for first, last, replacement in reversed(changes):
        lines[first-1:last] = [replacement]
    return ''.join(lines)
