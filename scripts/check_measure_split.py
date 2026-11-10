#!/usr/bin/env python3
"""Reject lost/duplicated definitions, facade regrowth and unshipped providers."""
from __future__ import annotations
import ast
from pathlib import Path
import sys

import measure_split as split


PASS1_PROVIDER_CEILING = 9000
# Pure helper modules: imported by domain modules, never facade providers.
HELPER_NAMES = ('dashboard_history', 'weekly_activity', 'headline', 'structural_ledger', 'structural_archives', 'structural_listing', 'structural_report', 'structural_seed', 'structural_value', 'counted_position', 'counted_pricing_v5', 'transcript_backfill', 'counted_subagents', 'lean_resume_threshold', 'output_reread', 'session_effect', 'rework_metrics', 'counted_orphans', 'counted_cap', 'archive_void_evidence', 'cost_per_message_trend', 'lean_resume_receivers', 'routing_period', 'fixture_context', 'session_best', 'savings_story', 'transcript_product', 'periphery_exposure', 'structure_funnel', 'setup_attachment_counter', 'first_read_outcomes', 'setup_budget_monitor', 'setup_connector_ledger', 'first_call_context', 'first_read_revive', 'mcp_archive_tripwire', 'compaction_survival', 'archive_void_codex', 'compression_fixture_void', 'feature_health', 'coverage_report', 'file_refetch', 'holdout_report', 'setup_step')
HELPERS = {name + '.py' for name in HELPER_NAMES}

def check(root=split.REPO):
    root = Path(root)
    scripts = root / 'skills/token-optimizer/scripts'
    package = scripts / '_measure'
    manifest = split.read_manifest(root / 'scripts/measure_split_manifest.json')
    records = {row['name']: row for row in manifest['symbols']}
    if 'dispatch' in manifest:
        records['__main__'] = manifest['dispatch']
    expected_files = {'__init__.py', '_compat.py', 'dashboard_history.py', 'weekly_activity.py', 'headline.py'} | HELPERS | {name + '.py' for name in split.DOMAINS}
    actual_files = {path.name for path in package.glob('*.py')}
    errors = []
    if actual_files != expected_files:
        errors.append(f'package files differ: missing={expected_files-actual_files}, extra={actual_files-expected_files}')
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    facade = ast.parse(source)
    bootstrap = [node for node in facade.body if isinstance(node, ast.FunctionDef)
                 and node.name == '_redirect_bytecode_out_of_repo']
    calls = [node.lineno for node in ast.walk(facade) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == '_redirect_bytecode_out_of_repo']
    compat_imports = [node.lineno for node in facade.body if isinstance(node, ast.ImportFrom)
                      and node.module == '_measure']
    if len(bootstrap) != 1 or not calls or not compat_imports or bootstrap[0].end_lineno >= min(calls + compat_imports):
        errors.append('retained bytecode bootstrap must precede its early call')
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
        # FILE-SIZE-POLICY.md pass 1: _measure/ domain modules may reach the 9,000-line plan
        # ceiling until pass 2 brings each under 1,000. _compat stays small.
        limit = 200 if path.name == '_compat.py' else PASS1_PROVIDER_CEILING
        if len(text.splitlines()) > limit:
            errors.append(f'{path.name}: exceeds {limit} lines')
        if path.name.startswith('_') or path.name in HELPERS:
            continue
        domain_tree = ast.parse(text)
        for node in ast.walk(domain_tree):
            if isinstance(node, ast.Import) and any(alias.name == 'measure' or alias.name.startswith('_measure.') for alias in node.names):
                errors.append(f'{path.name}: direct domain/facade import at line {node.lineno}')
            if isinstance(node, ast.ImportFrom) and (node.module == 'measure' or node.module and node.module.startswith('_measure') or
                    node.level and (node.module not in (None, '_compat') or any(alias.name not in ('_compat',) + HELPER_NAMES for alias in node.names))):
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
                           node.level == 1 and node.module is None and
                           all(a.name in ('_compat',) + HELPER_NAMES for a in node.names))
                if not allowed or any(a.name == '*' for a in node.names):
                    errors.append(f'{path.name}: direct domain/facade import')
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                errors.append(f'{path.name}: provider carries module-level state or work at line {node.lineno}')
    for name in records:
        if len(found.get(name, [])) != 1:
            errors.append(f'{name}: expected one body, found {found.get(name, [])}')
        if name in moved and name not in bindings and name != '__main__':
            errors.append(f'{name}: moved body has no facade binding')
        if name in bindings:
            expected = f"_measure_providers[{records[name]['module']!r}].{name}"
            if bindings[name] != expected:
                errors.append(f'{name}: wrong facade binding, expected {expected}')
        if name in bindings and name not in moved:
            errors.append(f'{name}: binding has no moved body')
    for name in found.keys() - records.keys():
        errors.append(f'new implementation outside frozen ownership: {name}')
    removed_lines = sum(records[name]['end']-records[name]['start']+1 for name in moved if name in records)
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
