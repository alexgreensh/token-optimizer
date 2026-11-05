#!/usr/bin/env python3
"""Reject lost/duplicated definitions, facade regrowth and unshipped providers."""
from __future__ import annotations
import ast
from pathlib import Path
import sys

import measure_split as split


def check(root=split.REPO):
    root = Path(root)
    scripts = root / 'skills/token-optimizer/scripts'
    package = scripts / '_measure'
    manifest = split.read_manifest(root / 'scripts/measure_split_manifest.json')
    records = {row['name']: row for row in manifest['symbols']}
    if 'dispatch' in manifest:
        records['__main__'] = manifest['dispatch']
    expected_files = {'__init__.py', '_compat.py'} | {name + '.py' for name in split.DOMAINS}
    actual_files = {path.name for path in package.glob('*.py')}
    errors = []
    if actual_files != expected_files:
        errors.append(f'package files differ: missing={expected_files-actual_files}, extra={actual_files-expected_files}')
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    facade = ast.parse(source)
    found = {}
    bindings = {}
    if split.resolve_symbol(scripts, '__main__').name == 'measure.py':
        found['__main__'] = ['measure']
    for node in facade.body:
        if isinstance(node, split.DEFINITION):
            found.setdefault(node.name, []).append('measure')
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
              and ast.unparse(node.value.func) == '_measure_compat.bind'):
            name = node.targets[0].id
            bindings[name] = ast.unparse(node.value.args[0])
    moved = set()
    for path in sorted(package.glob('*.py')):
        text = path.read_text(encoding='utf-8')
        limit = 200 if path.name == '_compat.py' else 9000
        if len(text.splitlines()) > limit:
            errors.append(f'{path.name}: exceeds {limit} lines')
        if path.name.startswith('_'):
            continue
        domain_tree = ast.parse(text)
        for node in ast.walk(domain_tree):
            if isinstance(node, ast.Import) and any(alias.name == 'measure' or alias.name.startswith('_measure.') for alias in node.names):
                errors.append(f'{path.name}: direct domain/facade import at line {node.lineno}')
            if isinstance(node, ast.ImportFrom) and (node.module == 'measure' or node.module and node.module.startswith('_measure') or
                    node.level and (node.module not in (None, '_compat') or any(alias.name != '_compat' for alias in node.names))):
                errors.append(f'{path.name}: direct domain/facade import at line {node.lineno}')
        for node in domain_tree.body:
            if isinstance(node, split.DEFINITION):
                name = '__main__' if node.name == 'main' and path.stem == 'cli' else node.name
                found.setdefault(name, []).append(path.stem)
                moved.add(name)
                if name not in records or records[name]['module'] != path.stem:
                    errors.append(f'{path.name}: unowned definition {node.name}')
            elif isinstance(node, ast.ImportFrom):
                allowed = (node.module == '__future__' or
                           node.level == 1 and node.module is None and [a.name for a in node.names] == ['_compat'])
                if not allowed or any(a.name == '*' for a in node.names):
                    errors.append(f'{path.name}: direct domain/facade import')
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                errors.append(f'{path.name}: provider carries module-level state or work at line {node.lineno}')
    for name in records:
        if len(found.get(name, [])) != 1:
            errors.append(f'{name}: expected one body, found {found.get(name, [])}')
        if name in moved and name not in bindings and name != '__main__':
            errors.append(f'{name}: moved body has no facade binding')
        if name in bindings and name not in moved:
            errors.append(f'{name}: binding has no moved body')
    for name in found.keys() - records.keys():
        errors.append(f'new implementation outside frozen ownership: {name}')
    removed_lines = sum(records[name]['end']-records[name]['start']+1 for name in moved)
    ceiling = manifest['source_lines']-removed_lines+len(moved)*2+300
    remaining = set(records)-moved-{'_redirect_bytecode_out_of_repo'}
    if not remaining:
        ceiling = min(ceiling, 8500)
    if len(source.splitlines()) > ceiling:
        errors.append(f'facade exceeds mixed-tree ceiling {ceiling}')
    return errors, {'definitions': len(manifest['symbols']), 'dispatch_ranges': int('dispatch' in manifest),
                    'moved': len(moved), 'remaining': len(remaining),
                    'facade_lines': len(source.splitlines()), 'facade_ceiling': ceiling}


if __name__ == '__main__':
    errors, counts = check()
    print(counts)
    for error in errors:
        print(error, file=sys.stderr)
    raise SystemExit(bool(errors))
