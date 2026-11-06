#!/usr/bin/env python3
"""Inventory, move, and resolve measure definitions without changing their scope.

The manifest records the frozen input. Runtime code never imports this tool.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import symtable
import sys
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
MANIFEST = REPO / 'scripts/measure_split_manifest.json'
DOMAINS = ('runtime', 'pricing', 'audit', 'dashboard', 'lifecycle', 'sessions',
           'trends', 'cache_policy', 'daemon', 'quality_archive', 'continuity',
           'savings', 'cli')
LANES = {'runtime': 1, 'pricing': 1, 'trends': 1, 'sessions': 2, 'cli': 2,
         'savings': 3, 'cache_policy': 4, 'audit': 5, 'quality_archive': 6,
         'continuity': 6, 'daemon': 7, 'dashboard': 8, 'lifecycle': 8}
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
RETAINED = frozenset({'_redirect_bytecode_out_of_repo'})
MIN_PYTHON = (3, 11)


def require_mover_python():
    if sys.version_info < MIN_PYTHON:
        raise RuntimeError('measure_split.py requires Python 3.11 or newer; runtime supports Python 3.9+')


HEADER = '"""Definition-only implementations; mutable state belongs to measure."""\nfrom __future__ import annotations\nfrom . import _compat\n'


def read_manifest(path=MANIFEST):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def resolve_symbol(scripts, name):
    scripts = Path(scripts)
    facade = scripts / 'measure.py'
    if name == '__main__':
        moved = _dispatch_moved(facade, facade.stat().st_mtime_ns, facade.stat().st_size)
        return scripts / '_measure/cli.py' if moved else facade
    if name in _definition_names(facade, facade.stat().st_mtime_ns, facade.stat().st_size):
        return facade
    owner = next(s['module'] for s in read_manifest()['symbols'] if s['name'] == name)
    path = scripts / '_measure' / (owner + '.py')
    if name not in _definition_names(path, path.stat().st_mtime_ns, path.stat().st_size):
        raise ValueError(f'{name}: missing implementation in {path}')
    return path


@lru_cache(maxsize=64)
def _definition_names(path, mtime_ns, size):
    return frozenset(n.name for n in ast.parse(path.read_text(encoding='utf-8')).body
                     if isinstance(n, DEFINITION))


@lru_cache(maxsize=32)
def _dispatch_moved(path, mtime_ns, size):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    guard = next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'")
    return (len(guard.body) == 1 and isinstance(guard.body[0], ast.Expr)
            and isinstance(guard.body[0].value, ast.Call)
            and ast.unparse(guard.body[0].value.func) == "_measure_providers['cli'].main")


class Qualifier(ast.NodeVisitor):
    """Use CPython's symbol tables; retain all untouched text byte for byte."""
    def __init__(self, source, table, globals_, *, tree=None):
        require_mover_python()
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.lines:
            self.offsets.append(self.offsets[-1] + len(line))
        self.table = table
        self.globals = globals_ | {'__file__', '__name__'}
        self.edits = []
        self.used_tables = set()
        self.shadow = set()
        self.in_comprehension = False
        self.class_comprehension = False
        self.root = None
        self.import_counter = 0
        self.exception_targets = {node.name for node in ast.walk(tree or ast.parse(source))
                                  if isinstance(node, ast.ExceptHandler) and node.name}

    def offset(self, line, column):
        # AST columns count UTF-8 bytes, string slicing counts characters.
        return self.offsets[line - 1] + len(self.lines[line - 1].encode('utf-8')[:column].decode('utf-8'))

    def replace(self, node, text):
        self.edits.append((self.offset(node.lineno, node.col_offset),
                           self.offset(node.end_lineno, node.end_col_offset), text))

    def child(self, node, name):
        for child in self.table.get_children():
            if (child.get_name() == name and child.get_lineno() == node.lineno
                    and child.get_id() not in self.used_tables):
                self.used_tables.add(child.get_id())
                return child
        raise ValueError(f'no symbol table for {name}:{node.lineno}')

    def scoped(self, node, name, body):
        previous = self.table
        self.table = self.child(node, name)
        for statement in body:
            self.visit(statement)
        self.table = previous

    def visit_Name(self, node):
        try:
            symbol = self.table.lookup(node.id)
        except KeyError:
            return
        if node.id in self.shadow:
            return
        class_scope = (self.table.get_type() == 'class' or self.class_comprehension and
                       self.table.get_name() in ('listcomp', 'setcomp', 'dictcomp', 'genexpr'))
        class_comp = self.in_comprehension and class_scope
        if (symbol.is_global() or class_comp) and node.id in self.globals:
            prefix = '_compat.facade().' if class_scope else '_measure_facade.'
            if isinstance(node.ctx, ast.Load) and node.id in self.exception_targets:
                self.replace(node, f'_compat.read_global({prefix[:-1]}, {node.id!r})')
            else:
                self.replace(node, prefix + node.id)

    def visit_Global(self, node):
        remaining = [name for name in node.names if name not in self.globals]
        self.replace(node, 'global ' + ', '.join(remaining) if remaining else 'pass')

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id == 'globals'
                and self.table.lookup('globals').is_global() and not node.args and not node.keywords):
            self.replace(node, '_measure_facade.__dict__')
        else:
            self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        self.generic_visit(node)
        if not node.name or not self.table.lookup(node.name).is_global():
            return
        start = self.offset(node.lineno, node.col_offset)
        end = self.offset(node.end_lineno, node.end_col_offset)
        body = self.source[start:end]
        contained = [edit for edit in self.edits if start <= edit[0] <= edit[1] <= end]
        for left, right, replacement in sorted(contained, reverse=True):
            body = body[:left-start] + replacement + body[right-start:]
        indent = ' ' * node.col_offset
        prefix = 'if True:\n' if indent else ''
        wrapper = ast.parse(prefix + indent + 'try:\n' + indent + '    pass\n' + indent + body).body[0]
        if indent:
            wrapper = wrapper.body[0]
        handler = wrapper.handlers[0]
        temporary = '_measure_exception_' + str(self.import_counter)
        while temporary in self.source:
            self.import_counter += 1
            temporary = '_measure_exception_' + str(self.import_counter)
        self.import_counter += 1
        handler.name = temporary
        target = ('_compat.facade().' if self.table.get_type() == 'class' else '_measure_facade.') + node.name
        # Python clears an exception target even when the suite exits early
        # or deletes the target itself. Keep that cleanup on the facade.
        handler.body = [*ast.parse(target + ' = ' + temporary).body,
                        ast.Try(body=handler.body, handlers=[], orelse=[],
                                finalbody=ast.parse(target + ' = None\ndel ' + target).body)]
        rendered = ast.unparse(ast.fix_missing_locations(handler))
        if isinstance(wrapper, getattr(ast, "TryStar", ())):
            rendered = rendered.replace('except ', 'except* ', 1)
        rendered = rendered.replace('\n', '\n' + ' ' * node.col_offset)
        self.edits = [edit for edit in self.edits if edit not in contained]
        self.edits.append((start, end, rendered))

    def visit_FunctionDef(self, node):
        parent = self.table
        global_binding = (node is not self.root and node.name in self.globals
                          and parent.lookup(node.name).is_global())
        # Nested defaults and decorators execute in the enclosing scope.
        for value in node.decorator_list + node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        self.scoped(node, node.name, node.body)
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            first = node.body[1] if len(node.body) > 1 else None
        if first is None:
            position = self.offsets[node.end_lineno]
            text = ' ' * (node.col_offset + 4) + '_measure_facade = _compat.facade()\n'
        elif first.lineno == node.lineno:
            position = self.offset(first.lineno, first.col_offset)
            text = '_measure_facade = _compat.facade(); '
        else:
            position = self.offset(first.lineno, first.col_offset)
            text = '_measure_facade = _compat.facade()\n' + ' ' * first.col_offset
        self.edits.append((position, position, text))
        if global_binding:
            position = self.offset(node.end_lineno, node.end_col_offset)
            indent = ' ' * node.col_offset
            binding = f'\n{indent}_measure_facade.{node.name} = _compat.bind({node.name}, _measure_facade.__name__)'
            if parent.get_type() == 'module':
                binding += f'\n{indent}_measure_facade.{node.name}.__qualname__ = {node.name!r}'
            binding += f'\n{indent}del {node.name}'
            self.edits.append((position, position, binding))

    visit_AsyncFunctionDef = visit_FunctionDef

    def imports(self, node):
        replacements = []
        changed = False
        for alias in node.names:
            name = alias.asname or alias.name.split('.')[0]
            global_binding = name in self.globals and self.table.lookup(name).is_global()
            if not global_binding:
                if isinstance(node, ast.Import):
                    replacements.append('import ' + ast.unparse(alias))
                else:
                    replacements.append('from ' + '.' * node.level + (node.module or '') + ' import ' + ast.unparse(alias))
                continue
            changed = True
            temporary = '_measure_import_' + str(self.import_counter)
            self.import_counter += 1
            if isinstance(node, ast.Import):
                # An unaliased dotted import binds the root package.
                if '.' in alias.name and alias.asname is None:
                    replacements += ['import ' + alias.name, f'_measure_facade.{name} = {name}', 'del ' + name]
                    continue
                replacements.append('import ' + alias.name + ' as ' + temporary)
            else:
                replacements.append('from ' + '.' * node.level + (node.module or '') + ' import ' + alias.name + ' as ' + temporary)
            replacements += [f'_measure_facade.{name} = {temporary}', 'del ' + temporary]
        if changed:
            self.replace(node, '; '.join(replacements))

    visit_Import = imports
    visit_ImportFrom = imports

    def visit_Lambda(self, node):
        for value in node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        self.scoped(node, 'lambda', [node.body])
        left = self.offset(node.body.lineno, node.body.col_offset)
        right = self.offset(node.body.end_lineno, node.body.end_col_offset)
        self.edits.extend(((left, left, '((_measure_facade := _compat.facade()), '),
                           (right, right, ')[1]')))

    def visit_ClassDef(self, node):
        for value in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(value)
        self.scoped(node, node.name, node.body)

    def comprehension(self, node, name):
        # The first iterable executes outside the comprehension scope.
        self.visit(node.generators[0].iter)
        previous = self.table
        previous_shadow, previous_comp = self.shadow, self.in_comprehension
        previous_class_comp = self.class_comprehension
        self.class_comprehension = previous_class_comp or previous.get_type() == 'class'
        try:
            self.table = self.child(node, name)
        except ValueError:
            # PEP 709 inlines list/set/dict comprehensions on Python 3.12+.
            if name == 'genexpr':
                raise
        self.shadow = self.shadow | {n.id for g in node.generators for n in ast.walk(g.target) if isinstance(n, ast.Name)}
        self.in_comprehension = True
        for index, generator in enumerate(node.generators):
            self.visit(generator.target)
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for field in ('elt', 'key', 'value'):
            if hasattr(node, field):
                self.visit(getattr(node, field))
        self.table = previous
        self.shadow, self.in_comprehension = previous_shadow, previous_comp
        self.class_comprehension = previous_class_comp

    def visit_ListComp(self, node): self.comprehension(node, 'listcomp')
    def visit_SetComp(self, node): self.comprehension(node, 'setcomp')
    def visit_DictComp(self, node): self.comprehension(node, 'dictcomp')
    def visit_GeneratorExp(self, node): self.comprehension(node, 'genexpr')


def extract_definition(source, name, module, *, tree=None, table=None):
    require_mover_python()
    if name in RETAINED:
        raise ValueError('retained facade definition: ' + name)
    tree = tree or ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, DEFINITION) and n.name == name)
    top = table or symtable.symtable(source, 'measure.py', 'exec')
    qualifier = Qualifier(source, top, {s.get_name() for s in top.get_symbols()
                                      if s.is_assigned() or s.is_imported()} |
                          {name for statement in ast.walk(tree) if isinstance(statement, ast.Global)
                           for name in statement.names}, tree=tree)
    import copy
    node = copy.deepcopy(node)
    qualifier.root = node
    decorators = node.decorator_list
    node.decorator_list = []
    defaults = list(node.args.defaults) if hasattr(node, 'args') else []
    kwdefaults = [(a.arg, v) for a, v in zip(node.args.kwonlyargs, node.args.kw_defaults) if v] if hasattr(node, 'args') else []
    # Provider defaults are placeholders. Originals bind in the facade.
    if hasattr(node, 'args'):
        node.args.defaults = []
        node.args.kw_defaults = [None] * len(node.args.kwonlyargs)
    qualifier.visit(node)
    for value in defaults + [v for _, v in kwdefaults]:
        qualifier.replace(value, 'None')
    start = qualifier.offset(node.lineno, node.col_offset)
    end = qualifier.offset(node.end_lineno, node.end_col_offset)
    moved = source[start:end]
    for left, right, value in sorted(qualifier.edits, reverse=True):
        if start <= left <= right <= end:
            moved = moved[:left-start] + value + moved[right-start:]
    def tuple_source(values):
        return '(' + ', '.join(ast.get_source_segment(source, v) for v in values) + (',' if values else '') + ')'
    keywords = []
    if defaults:
        keywords.append('defaults=' + tuple_source(defaults))
    if kwdefaults:
        keywords.append('kwdefaults={' + ', '.join(repr(k) + ': ' + ast.get_source_segment(source, v) for k, v in kwdefaults) + '}')
    if decorators:
        keywords.append('decorators=' + tuple_source(decorators))
    suffix = ', ' + ', '.join(keywords) if keywords else ''
    binding = f'{name} = _measure_compat.bind(_measure_providers[{module!r}].{name}, __name__{suffix})'
    return moved + '\n', binding


def move(scripts, names):
    require_mover_python()
    if RETAINED.intersection(names):
        raise ValueError('retained facade definitions: ' + ', '.join(sorted(RETAINED.intersection(names))))
    scripts = Path(scripts)
    path = scripts / 'measure.py'
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    table = symtable.symtable(source, 'measure.py', 'exec')
    owners = {s['name']: s['module'] for s in read_manifest()['symbols']}
    changes = []
    for name in names:
        node = next(n for n in tree.body if isinstance(n, DEFINITION) and n.name == name)
        moved, binding = extract_definition(source, name, owners[name], tree=tree, table=table)
        target = scripts / '_measure' / (owners[name] + '.py')
        existing = target.read_text(encoding='utf-8')
        if any(isinstance(n, DEFINITION) and n.name == name for n in ast.parse(existing).body):
            raise ValueError(f'duplicate implementation: {name}')
        target.write_bytes((existing.rstrip() + '\n\n\n' + moved).encode('utf-8'))
        changes.append((min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno, binding))
    lines = source.splitlines(keepends=True)
    for first, last, binding in sorted(changes, reverse=True):
        lines[first-1:last] = [binding + '\n']
    path.write_bytes(''.join(lines).encode('utf-8'))


def move_cli(scripts):
    """The dispatch retains module-global assignments and conditional exports."""
    require_mover_python()
    scripts = Path(scripts)
    path = scripts / 'measure.py'
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    guard = next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'")
    top = symtable.symtable(source, str(path), 'exec')
    qualifier = Qualifier(source, top, {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}, tree=tree)
    for statement in guard.body:
        qualifier.visit(statement)
    start = qualifier.offsets[guard.lineno]
    end = qualifier.offset(guard.end_lineno, guard.end_col_offset)
    body = source[start:end]
    for left, right, value in sorted(qualifier.edits, reverse=True):
        if start <= left <= right <= end:
            body = body[:left-start] + value + body[right-start:]
    target = scripts / '_measure/cli.py'
    existing = target.read_text(encoding='utf-8')
    if any(isinstance(node, DEFINITION) for node in ast.parse(existing).body):
        raise ValueError('CLI provider already has an implementation')
    provider = existing.rstrip() + '\n\n\ndef main():\n    _measure_facade = _compat.facade()\n' + body + '\n'
    replacement = 'if __name__ == "__main__":\n    _measure_providers[\'cli\'].main()'
    facade = source[:qualifier.offset(guard.lineno, guard.col_offset)] + replacement + source[end:]
    compile(provider, str(target), 'exec')
    compile(facade, str(path), 'exec')
    target.write_bytes(provider.encode('utf-8'))
    path.write_bytes(facade.encode('utf-8'))


def main():
    if sys.version_info < MIN_PYTHON:
        print('measure_split.py requires Python 3.11 or newer; runtime supports Python 3.9+', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scripts', type=Path, default=SCRIPTS)
    parser.add_argument('--move', nargs='+')
    parser.add_argument('--resolve')
    parser.add_argument('--move-cli', action='store_true')
    args = parser.parse_args()
    if args.move:
        move(args.scripts, args.move)
    elif args.move_cli:
        move_cli(args.scripts)
    elif args.resolve:
        print(resolve_symbol(args.scripts, args.resolve))


if __name__ == '__main__':
    raise SystemExit(main())
