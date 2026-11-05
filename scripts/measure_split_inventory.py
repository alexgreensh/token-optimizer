#!/usr/bin/env python3
"""Freeze source ownership and generate the CLI/hook proof checklist from BASE."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import base64
import re
import shlex
from pathlib import Path
import subprocess
import symtable

import measure_split as split


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def freeze(source_root, owners, base, *, provisional=True):
    path = source_root / 'skills/token-optimizer/scripts/measure.py'
    text = path.read_text(encoding='utf-8')
    expected = subprocess.check_output(['git', 'show', base + ':skills/token-optimizer/scripts/measure.py'], cwd=split.REPO)
    if expected != path.read_bytes():
        raise ValueError('inventory source differs from the named BASE commit')
    tree, lines = ast.parse(text), text.splitlines()
    if any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
           and ast.unparse(n.value.func) == '_measure_compat.bind' for n in tree.body):
        raise ValueError('freeze must read unsplit BASE, never candidate exports')
    assigned = {row['name']: row['module'] for row in owners['symbols']}
    symbols = []
    for node in tree.body:
        if not isinstance(node, split.DEFINITION):
            continue
        if node.name not in assigned:
            raise ValueError('assign changed BASE symbol before freeze: ' + node.name)
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        source = '\n'.join(lines[first-1:node.end_lineno])
        symbols.append({'name': node.name, 'module': assigned[node.name], 'lane': split.LANES[assigned[node.name]],
                        'start': first, 'end': node.end_lineno, 'kind': type(node).__name__,
                        'signature': '\n'.join(lines[first-1:node.body[0].lineno-1]),
                        'global_declarations': [{'line': declaration.lineno-first,
                                                 'text': ast.get_source_segment(text, declaration)}
                                                for declaration in ast.walk(node) if isinstance(declaration, ast.Global)],
                        'sha256': hashlib.sha256(source.encode()).hexdigest()})
    dispatch = next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'")
    return {'schema': 1, 'provisional': provisional, 'base': base,
            'source_sha256': hashlib.sha256(text.encode()).hexdigest(), 'source_lines': len(lines),
            'symbols': symbols,
            'dispatch': {'module': 'cli', 'lane': 2, 'start': dispatch.lineno + 1,
                         'end': dispatch.end_lineno,
                         'sha256': hashlib.sha256('\n'.join(lines[dispatch.lineno:dispatch.end_lineno]).encode()).hexdigest()},
            'bindings': sorted(s.get_name() for s in symtable.symtable(text, str(path), 'exec').get_symbols()
                               if s.is_assigned() or s.is_imported()),
            'statements': [{'start': n.lineno, 'end': n.end_lineno, 'kind': type(n).__name__,
                            'sha256': hashlib.sha256('\n'.join(lines[n.lineno-1:n.end_lineno]).encode()).hexdigest()}
                           for n in tree.body]}


def dispatch_inventory(source):
    tree = ast.parse(source)
    guard = next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'")
    commands, branches = set(), []
    for node in ast.walk(guard):
        if not isinstance(node, ast.Compare):
            continue
        lhs = ast.unparse(node.left)
        values = []
        for value in node.comparators:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                values.append(value.value)
            elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                values.extend(n.value for n in value.elts if isinstance(n, ast.Constant) and isinstance(n.value, str))
        if lhs == 'args[0]':
            commands.update(values)
        if values:
            branches.append({'line': node.lineno, 'expression': ast.unparse(node), 'lhs': lhs, 'values': values})
    return sorted(commands), branches


def required_dispatch_cases(source):
    """Derive literal subcommands/flags with their owning first argument."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    def strings(compare):
        values = []
        for value in compare.comparators:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                values.append(value.value)
            elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                values.extend(n.value for n in value.elts if isinstance(n, ast.Constant) and isinstance(n.value, str))
        return values
    def owner(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If):
                values = [value for compare in ast.walk(node.test) if isinstance(compare, ast.Compare)
                          and ast.unparse(compare.left) == 'args[0]' for value in strings(compare)]
                if values:
                    return values
        return []
    required = []
    positions = {'sub': 1, 'subcmd': 1, 'action': 1, 'endpoint': 1, 'feature_name': 2}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        commands = owner(node)
        if not commands:
            continue
        lhs = ast.unparse(node.left)
        values = strings(node)
        index = positions.get(lhs)
        if isinstance(node.left, ast.Subscript) and isinstance(node.left.value, ast.Name) and node.left.value.id == 'args' and isinstance(node.left.slice, ast.Constant):
            index = node.left.slice.value
        if index is not None and isinstance(index, int) and index >= 0 and values:
            for command in commands:
                for value in values:
                    argv = [command]
                    while len(argv) <= index:
                        argv.append('fixture')
                    argv[index] = value
                    if command in ('skill', 'mcp', 'codex-skill', 'codex-mcp', 'api-manage') and len(argv) < 3:
                        argv.append('fixture')
                    required.append({'expression': ast.unparse(node), 'argv': argv, 'line': node.lineno})
        if (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                and node.left.value.startswith('--') and any(ast.unparse(value) == 'args' for value in node.comparators)):
            flag = node.left.value
            value_flags = {'--days': '7', '--context-size': '200000', '--topic': 'fixture',
                           '--trigger': 'auto', '--budget-seconds': '1', '--strategy': 'halves',
                           '--milestone': 'fixture', '--focus': 'skills', '--set': 'yes',
                           '--file': 'CLAUDE.md', '--coord-path': '.', '--port': '9000',
                           '--session': 'fixture-session', '--session-id': 'fixture-session'}
            for command in commands:
                argv = [command, flag]
                if flag in value_flags:
                    argv.append(value_flags[flag])
                required.append({'expression': ast.unparse(node), 'argv': argv, 'line': node.lineno})
    # This delegated parser owns its dispatch outside the main guard.
    for sub in ('status', 'enable', 'disable', 'scan', '__unknown__'):
        required.append({'expression': 'subagent-cache delegated subcommand: ' + sub,
                         'argv': ['subagent-cache', sub], 'line': None})
    return required


def cases(source_root, base, *, provisional=True):
    commands, branches = dispatch_inventory((source_root / 'skills/token-optimizer/scripts/measure.py').read_text())
    rows = []
    def add(argv, entry='cli', runtime='claude', **extra):
        key = entry + '-' + ('-'.join(argv) or 'no-args')
        if extra.get('hook_event'):
            key += '-' + extra['hook_event'] + '-' + extra.get('input_variant', 'success')
            key += '-' + extra.get('host_variant', 'claude')
        if extra.get('fixture') and extra['fixture'] != 'synthetic-current':
            key += '-' + extra['fixture']
        key = key.replace('/', '_').replace(' ', '_')
        if any(row['id'] == key for row in rows):
            return
        rows.append({'id': key, 'entry': entry, 'argv': argv, 'runtime': runtime, 'stdin_base64': '',
                     'fixture': 'synthetic-current', 'expected_exit': 'capture-from-BASE',
                     'stdout': key + '/stdout', 'stderr': key + '/stderr',
                     'side_effects': key + '/effects.json', **extra})
    add([])
    add(['unknown-split-command'])
    for command in commands:
        runtime = next((name for name in ('codex', 'hermes', 'copilot', 'cursor', 'antigravity', 'grok', 'devin')
                        if command.startswith(name + '-')), 'claude')
        for mode in ([], ['--json'], ['--quiet'], ['--help']):
            add([command, *mode], runtime=runtime)
    for command, subcommands in {
        'keepwarm-scheduler': ('status', 'install', 'uninstall'),
        'skill': ('archive', 'restore'), 'mcp': ('enable', 'disable'),
        'codex-skill': ('enable', 'disable'), 'codex-mcp': ('enable', 'disable'),
        'subagent-cache': ('status', 'enable', 'disable', 'scan'),
        'cohorts': ('status', 'promote', 'evaluate'),
        'consent': ('--show', '--reset', '--grant'),
        'daemon-consent': ('--get', '--set'),
        'pricing-tier': ('anthropic', 'bedrock', 'vertex-global', 'vertex-regional'),
        'v5': ('status', 'enable', 'disable', 'welcome', 'info'), 'resume-lean': ('--list', '--json', '--help'),
        'snapshot': ('before', 'after'), 'expand': ('--list', 'fixture-tool'),
        'compact-capture': ('--trigger',),
    }.items():
        for subcommand in subcommands:
            argv = [command, subcommand]
            if command == 'compact-capture': argv += ['auto']
            if command in ('skill', 'mcp', 'codex-skill', 'codex-mcp'): argv += ['fixture']
            if command == 'daemon-consent' and subcommand == '--set': argv += ['yes']
            if command == 'v5' and subcommand in ('enable', 'disable', 'info'): argv += ['quality_nudges']
            add(argv)
            add([*argv, '--json'])
    add([], entry='exports')
    for runtime in ('codex', 'hermes', 'copilot', 'cursor', 'antigravity', 'grok', 'devin'):
        add([runtime], entry='adapter', runtime=runtime)
    for requirement in required_dispatch_cases((source_root / 'skills/token-optimizer/scripts/measure.py').read_text()):
        add(requirement['argv'])
    for function in ('_collect_trends_data', '_collect_management_data', '_collect_health_data',
                     '_collect_quality_for_dashboard', 'runway_snapshot', '_get_savings_summary',
                     '_generate_daemon_script'):
        add([function], entry='api', fixture='seeded-current' if function != '_generate_daemon_script' else 'synthetic-current')
    for kind in ('daemon', 'dashboard'):
        for case in ('health', 'token', 'authorized', 'unauthorized', 'malformed'):
            add([kind, case], entry='http', fixture='seeded-current')
    add(['dashboard', 'savings'], entry='http', fixture='seeded-current')
    for variant in ('synthetic-current', 'fresh-cache', 'stale-cache'):
        add([], entry='node', fixture=variant)
    for variant in ('seeded-current', 'legacy', 'busy-db'):
        add(['status-bar', '--json', '--session', 'fixture-session'], fixture=variant)
        add(['savings', '--json'], fixture=variant)
    for variant in ('locked-settings', 'readonly-settings'):
        add(['v5', 'enable', 'quality_nudges', '--json'], fixture=variant)
    add(['expand', 'fixture-tool'], fixture='seeded-current')
    add(['compact-restore', '--compact'], fixture='seeded-current',
        hook_event='SessionStart', input_variant='success', host_variant='claude')
    hook_inventory = []
    for relative in ('hooks/hooks.json', 'plugins/token-optimizer/hooks/hooks.json', 'cowork/token-optimizer/hooks/hooks.json'):
        path = source_root / relative
        if not path.exists():
            continue
        hooks = json.loads(path.read_text()).get('hooks', {})
        for event, groups in hooks.items():
            for group in groups:
                for hook in group.get('hooks', []):
                    hook_inventory.append({'source': relative, 'event': event, 'command': hook.get('command'),
                                           'timeout': hook.get('timeout'), 'async': hook.get('async')})
                    matches = list(re.finditer(r'(?:skills/token-optimizer/scripts|hooks)/[A-Za-z0-9_-]+\.py', hook.get('command', '')))
                    matches = [match for match in matches if match.group() != 'hooks/run.py']
                    if matches:
                        match = matches[-1]
                        tail = hook['command'][match.end():].split(';', 1)[0].strip().strip('"')
                        argv = [match.group(), *shlex.split(tail)]
                        host = 'codex' if relative.startswith('plugins/') else 'cowork' if relative.startswith('cowork/') else 'claude'
                        for variant in ('success', 'malformed', 'empty', 'deadline'):
                            add(argv, entry='hook', runtime='codex' if host == 'codex' else 'claude',
                                hook_event=event, input_variant=variant, host_variant=host,
                                stdin_base64=base64.b64encode(b'{broken' if variant == 'malformed' else b'').decode())
    return {'schema': 1, 'provisional': provisional, 'base': base, 'spellings': commands,
            'dispatch_branches': branches,
            'required_dispatch_cases': required_dispatch_cases((source_root / 'skills/token-optimizer/scripts/measure.py').read_text()),
            'hooks': hook_inventory, 'cases': rows}


def ownership(manifest, root):
    symbols = {row['name']: row for row in manifest['symbols']}
    tests = {}
    source_sites, loader_sites = [], []
    for path in sorted((root / 'tests').glob('test_*.py')):
        text = path.read_text(encoding='utf-8')
        names = set()
        tree = ast.parse(text)
        helpers = {alias.asname or alias.name: alias.name for node in tree.body
                   if isinstance(node, ast.ImportFrom) and node.module == 'helpers.measure_split'
                   for alias in node.names}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper = helpers.get(node.func.id, node.func.id)
                site = {'file': str(path.relative_to(root)), 'line': node.lineno, 'helper': helper}
                if helper in ('measure_source', 'definition_nodes', 'inspect_source'):
                    source_sites.append(site)
                if helper in ('exec_measure', '_split_exec_module', 'exec_source_module',
                              'exec_facade_source', 'isolated_measure'):
                    loader_sites.append(site)
            if isinstance(node, ast.Attribute) and node.attr in symbols:
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in symbols:
                names.add(node.value)
        lanes = sorted({symbols[name]['lane'] for name in names})
        tests[str(path.relative_to(root))] = {'owner': lanes[0] if len(lanes) == 1 else 0,
                                            'lanes': lanes, 'symbols': sorted(names)}
    lanes = {}
    canaries = {'_safe_int', '_get_model_cost', '_bundled_prices_path', '_read_codex_config', '_settings_lock'}
    for lane in range(1, 9):
        lanes[str(lane)] = {
            'modules': [name for name, owner in split.LANES.items() if owner == lane],
            'symbols': [row for row in manifest['symbols'] if row['lane'] == lane and row['name'] not in split.RETAINED],
            'retained_symbols': [row for row in manifest['symbols'] if row['lane'] == lane and row['name'] in split.RETAINED],
            'dispatch': manifest['dispatch'] if lane == 2 else None,
            'inherited_canaries': sorted(name for name in canaries if symbols[name]['lane'] == lane),
            'owned_existing_tests': [path for path, record in tests.items() if record['owner'] == lane],
            'new_test_file': f'tests/test_measure_split_lane{lane}.py',
            'mutation_fragment': f'scripts/mutations/_measure_lane{lane}.json',
        }
    return {'schema': 1, 'base': manifest['base'], 'tests': tests, 'lanes': lanes,
            'source_resolution_sites': source_sites, 'canonical_loader_sites': loader_sites,
            'mutation_fragments': {f'scripts/mutations/_measure_lane{lane}.json': lane for lane in range(9)},
            'mutation_index_owner': 0,
            'shared_owner': 0,
            'worker_instruction': 'Other workers are active. Do not revert their edits. Edit only assigned definitions, destination modules, test files, and mutation fragments; send shared-file changes to the integrator.',
            'rule': 'Only the assigned owner edits a shared test; send other changes to the integrator.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--owners', type=Path, default=split.MANIFEST)
    parser.add_argument('--final', action='store_true')
    args = parser.parse_args()
    manifest = freeze(args.source, json.loads(args.owners.read_text()), args.base, provisional=not args.final)
    write_json(split.MANIFEST, manifest)
    write_json(split.REPO / 'scripts/measure_split_ownership.json', ownership(manifest, split.REPO))
    write_json(split.REPO / 'tests/fixtures/measure_split/cases.json',
               cases(args.source, args.base, provisional=manifest['provisional']))
    print(f"froze {len(manifest['symbols'])} definitions at {args.base}, provisional={manifest['provisional']}")


if __name__ == '__main__':
    main()
