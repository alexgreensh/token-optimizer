#!/usr/bin/env python3
"""Lane U4: move the cache_policy definitions out of measure.py, mechanically.

Deterministic and re-runnable. Given the current measure.py it extracts exactly
the symbols named in the lane 4 packet of scripts/measure_split_ownership.json
(BY NAME, never by line number), writes them to _measure/cache_policy.py using
U0's scope-aware extractor (scripts/measure_split.py), and replaces each
original definition in place with the binding statement U0's facade pattern
uses. Nothing else in measure.py is touched.

States, per symbol:
    pending  top-level def in measure.py, absent from cache_policy.py
    done     bound in measure.py, defined once in cache_policy.py
Pending symbols move; done symbols are left exactly as they are, so an
interrupted or partial state resumes. Everything else (missing, duplicate,
half-moved, hash drift, module-level state in the provider) is refused and
nothing is written. When every symbol is already done the script reports so and
exits 0 without writing.

    python3.14 scripts/split_lanes/u4_move.py            # apply
    python3.14 scripts/split_lanes/u4_move.py --check    # report state, write nothing
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import symtable
import sys
from pathlib import Path

LANE = 4
MODULE = 'cache_policy'
REPO = Path(__file__).resolve().parents[2]
BIND_FUNCTION = '_measure_compat.bind'
# Owned by the facade for good: bytecode redirection runs before the bridge exists.
RETAINED = frozenset({'_redirect_bytecode_out_of_repo'})


class Refusal(Exception):
    """The tree is not in a state this lane's move can safely act on."""


def _read(path):
    """File text exactly as stored: bytes decoded, never the platform's newline translation."""
    return Path(path).read_bytes().decode('utf-8')


def _write(path, text):
    """Write text as LF-exact UTF-8 bytes (the repo requires LF; Windows text mode would write CRLF)."""
    Path(path).write_bytes(text.encode('utf-8'))


def load_tool(root):
    path = Path(root) / 'scripts/measure_split.py'
    spec = importlib.util.spec_from_file_location('_u4_measure_split_tool', path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def lane_packet(root):
    ownership = json.loads(_read(Path(root) / 'scripts/measure_split_ownership.json'))
    packet = ownership['lanes'][str(LANE)]
    if packet['modules'] != [MODULE]:
        raise Refusal(f'lane {LANE} packet owns {packet["modules"]}, expected {[MODULE]}')
    names = [row['name'] for row in packet['symbols']]
    if len(names) != len(set(names)):
        raise Refusal('duplicate names in the lane packet: ' + ', '.join(sorted({n for n in names if names.count(n) > 1})))
    for retained in sorted(RETAINED.intersection(names)):
        raise Refusal(f'{retained} is retained in measure.py and may never be moved')
    for row in packet['symbols']:
        if row['module'] != MODULE or row['lane'] != LANE:
            raise Refusal(f'{row["name"]}: packet row is {row["module"]}/lane {row["lane"]}')
    return packet


def bound_names(tree, module):
    """name -> provider attribute named by a facade binding for this module."""
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == BIND_FUNCTION):
            target = node.value.args[0]
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Subscript)
                    and ast.unparse(target.value.value) == '_measure_providers'
                    and ast.literal_eval(target.value.slice) == module):
                found.setdefault(node.targets[0].id, []).append(target.attr)
    return found


def classify(packet, facade_tree, provider_tree, tool):
    definitions = {}
    for node in facade_tree.body:
        if isinstance(node, tool.DEFINITION):
            definitions.setdefault(node.name, []).append(node)
    provider = {}
    for node in provider_tree.body:
        if isinstance(node, tool.DEFINITION):
            provider.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ImportFrom) and (node.module == '__future__' or (
                node.level == 1 and node.module is None and [a.name for a in node.names] == ['_compat'])):
            continue
        elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise Refusal(f'{MODULE}.py carries module-level state or work at line {node.lineno}')
    bindings = bound_names(facade_tree, MODULE)
    pending, done, problems = [], [], []
    for row in packet['symbols']:
        name = row['name']
        in_facade, in_provider, bound = definitions.get(name, []), provider.get(name, []), bindings.get(name, [])
        if len(in_facade) > 1 or len(in_provider) > 1 or len(bound) > 1:
            problems.append(f'{name}: duplicate (facade defs {len(in_facade)}, provider defs {len(in_provider)}, bindings {len(bound)})')
        elif in_facade and not in_provider and not bound:
            pending.append(name)
        elif not in_facade and len(in_provider) == 1 and bound == [name]:
            done.append(name)
        elif not in_facade and not in_provider and not bound:
            problems.append(f'{name}: missing from measure.py and from {MODULE}.py')
        else:
            problems.append(f'{name}: half-moved (facade defs {len(in_facade)}, provider defs {len(in_provider)}, '
                            f'bindings {bound})')
    owned = {row['name'] for row in packet['symbols']}
    for extra in sorted(provider.keys() - owned):
        problems.append(f'{extra}: defined in {MODULE}.py but not owned by lane {LANE}')
    for extra in sorted(set(bindings) - owned):
        problems.append(f'{extra}: bound from {MODULE} but not in the lane {LANE} packet')
    return pending, done, problems, definitions


def verify_source(packet, definitions, pending, lines):
    """The frozen packet hash must match the slice about to move."""
    rows = {row['name']: row for row in packet['symbols']}
    drift = []
    for name in pending:
        node = definitions[name][0]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        digest = hashlib.sha256('\n'.join(lines[first - 1:node.end_lineno]).encode()).hexdigest()
        if digest != rows[name]['sha256']:
            drift.append(name)
    if drift:
        raise Refusal('source drifted from the lane packet hash, nothing written: ' + ', '.join(drift))


def trailing_comment(source_lines, node):
    """Text after the last statement on its final line, if it is only a comment.

    U0's extractor slices to the end of the last statement and the in-place
    replacement drops the whole line, so a same-line trailing comment would be
    lost. A mechanical move keeps it.
    """
    last = source_lines[node.end_lineno - 1].rstrip('\n')
    tail = last.encode('utf-8')[node.end_col_offset:].decode('utf-8')
    if not tail.strip():
        return ''
    if not tail.lstrip().startswith('#'):
        raise Refusal(f'{node.name}: unsupported text after the final statement: {tail!r}')
    return tail


def leading_text_guard(source_lines, node):
    """Text sharing the def line, or comments between a decorator and the def, would be lost."""
    head = source_lines[node.lineno - 1].encode('utf-8')[:node.col_offset].decode('utf-8')
    if head.strip():
        raise Refusal(f'{node.name}: code before the def on its first line: {head!r}')
    if node.decorator_list:
        decorators_end = max(d.end_lineno for d in node.decorator_list)
        between = [line for line in source_lines[decorators_end:node.lineno - 1] if line.strip()]
        if between:
            raise Refusal(f'{node.name}: comments or text between decorator and def would be lost: {between!r}')
        for decorator in node.decorator_list:
            line = source_lines[decorator.end_lineno - 1].rstrip('\n')
            tail = line.encode('utf-8')[decorator.end_col_offset:].decode('utf-8')
            if tail.strip():
                raise Refusal(f'{node.name}: trailing text on a decorator line would be lost: {tail!r}')


def binding_violations(source):
    """Uses rewritten to a facade attribute although the enclosing scope binds the name locally.

    except-as targets, with/for targets, walrus targets, match captures,
    imports and nested definitions are all symbol-table locals. A rewrite of
    one of their uses would raise AttributeError on the error path. This is an
    independent check of the extractor's output, using CPython's symbol tables.
    """
    tree = ast.parse(source)
    top = symtable.symtable(source, MODULE + '.py', 'exec')
    found = []

    def child_table(table, node):
        name = 'lambda' if isinstance(node, ast.Lambda) else node.name
        for candidate in table.get_children():
            if candidate.get_name() == name and candidate.get_lineno() == node.lineno:
                return candidate
        raise Refusal(f'no symbol table for {name}:{node.lineno}')

    def walk(node, table, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = function or ('<lambda>' if isinstance(child, ast.Lambda) else child.name)
                walk(child, child_table(table, child), inner)
                continue
            if (isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
                    and child.value.id == '_measure_facade'):
                try:
                    symbol = table.lookup(child.attr)
                except KeyError:
                    symbol = None
                if symbol is not None and symbol.is_local() and not symbol.is_global():
                    found.append((function, child.attr, child.lineno))
            walk(child, table, function)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk(node, child_table(top, node), node.name)
    return found


def build(root, packet, definitions, pending, facade_source, provider_source, tool):
    tree = ast.parse(facade_source)
    table = symtable.symtable(facade_source, 'measure.py', 'exec')
    source_lines = facade_source.splitlines(keepends=True)
    ordered = sorted(pending, key=lambda name: definitions[name][0].lineno)
    regions = []
    texts = {}
    # extract_definition walks its whole tree on every call only to collect `global`
    # names. Hand it the target node plus one Global carrying the same names: the
    # output is identical (tested against the full-tree call), the cost is not.
    global_names = sorted({name for statement in ast.walk(tree) if isinstance(statement, ast.Global)
                           for name in statement.names})
    for name in ordered:
        node = definitions[name][0]
        leading_text_guard(source_lines, node)
        pruned = ast.Module(body=[node, ast.Global(names=global_names)], type_ignores=[])
        moved, binding = tool.extract_definition(facade_source, name, MODULE, tree=pruned, table=table)
        tail = trailing_comment(source_lines, node)
        if tail:
            moved = moved.rstrip('\n') + tail + '\n'
        texts[name] = moved.rstrip('\n')
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        regions.append((first, node.end_lineno, binding))
    new_lines = list(facade_source.splitlines(keepends=True))
    for first, last, binding in sorted(regions, reverse=True):
        new_lines[first - 1:last] = [binding + '\n']
    return merge_provider(provider_source, texts, tree, definitions, tool), ''.join(new_lines), regions


def merge_provider(provider_source, new_texts, facade_tree, definitions, tool):
    """The provider in canonical form: header, then every definition in the order of its facade position.

    This is also the form a from-scratch move produces, so a half-finished tree
    resumes into exactly the file a full rebuild would write. An existing
    provider that is not already canonical (stray comments, spacing, order) is refused.
    """
    provider_tree = ast.parse(provider_source)
    lines = provider_source.splitlines()
    defs = [n for n in provider_tree.body if isinstance(n, tool.DEFINITION)]
    position = {n.targets[0].id: n.lineno for n in facade_tree.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == BIND_FUNCTION}
    texts = {n.name: '\n'.join(lines[n.lineno - 1:n.end_lineno]) for n in defs}
    prefix = '\n'.join(lines[:defs[0].lineno - 1]) if defs else provider_source
    if defs:
        canonical = prefix.rstrip() + ''.join('\n\n\n' + texts[n.name] for n in defs) + '\n'
        if canonical != provider_source or [n.name for n in defs] != sorted(texts, key=position.__getitem__):
            raise Refusal(f'{MODULE}.py is not in canonical form (comment, spacing or definition order differs)')
    for name in new_texts:
        position[name] = definitions[name][0].lineno
    texts.update(new_texts)
    return prefix.rstrip() + ''.join('\n\n\n' + texts[name] for name in sorted(texts, key=position.__getitem__)) + '\n'


def validate(root, packet, pending, old_facade, new_facade, new_provider, regions, tool):
    compile(new_provider, f'{MODULE}.py', 'exec')
    compile(new_facade, 'measure.py', 'exec')
    provider_tree, facade_tree = ast.parse(new_provider), ast.parse(new_facade)
    verify_positions(root, new_facade, facade_tree, tool)
    broken = binding_violations(new_provider)
    if broken:
        raise Refusal('local bindings were rewritten to facade attributes: ' + ', '.join(
            f'{function}:{name}@{line}' for function, name, line in broken))
    defined = [n.name for n in provider_tree.body if isinstance(n, tool.DEFINITION)]
    owned = [row['name'] for row in packet['symbols']]
    if sorted(defined) != sorted(owned):
        raise Refusal('provider definitions differ from the lane packet after the move')
    left_behind = [n.name for n in facade_tree.body if isinstance(n, tool.DEFINITION) and n.name in set(owned)]
    if left_behind:
        raise Refusal('definitions still in measure.py after the move: ' + ', '.join(left_behind))
    bindings = bound_names(facade_tree, MODULE)
    if sorted(bindings) != sorted(owned) or any(bindings[name] != [name] for name in owned):
        raise Refusal('facade bindings differ from the lane packet after the move')
    # Everything outside the replaced regions must be byte-identical.
    old_lines = old_facade.splitlines(keepends=True)
    removed = set()
    for first, last, _ in regions:
        removed.update(range(first - 1, last))
    kept_old = [line for index, line in enumerate(old_lines) if index not in removed]
    binding_lines = {binding + '\n' for _, _, binding in regions}
    kept_new = []
    remaining = dict.fromkeys(binding_lines, 0)
    for line in new_facade.splitlines(keepends=True):
        if line in remaining and remaining[line] == 0:
            remaining[line] = 1
            continue
        kept_new.append(line)
    if kept_old != kept_new:
        raise Refusal('measure.py changed outside the replaced definitions')


# ---------------------------------------------------------------- completed-state proof

def _positions(text):
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def position(line, column):
        return offsets[line - 1] + len(lines[line - 1].encode('utf-8')[:column].decode('utf-8'))
    return lines, offsets, position


def _is_facade_call(node):
    return isinstance(node, ast.Call) and ast.unparse(node.func) == '_compat.facade' and not node.args


def restore_original(provider_text, row):
    """Undo exactly the qualification the extractor adds; the result must hash to the frozen source.

    Reverses: the injected facade statement, facade attribute access,
    ``globals()`` rewriting, the lambda wrapper, ``global`` -> ``pass``, and the
    placeholder defaults / removed decorators (restored from the frozen packet
    signature). Anything it does not recognise stays in the text and is caught
    by the hash and by the forward round trip.
    """
    code = provider_text
    while True:
        tree = ast.parse(code)
        wrapper = next((n for n in ast.walk(tree) if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Tuple)
                        and len(n.value.elts) == 2 and isinstance(n.value.elts[0], ast.NamedExpr)
                        and isinstance(n.value.elts[0].target, ast.Name)
                        and n.value.elts[0].target.id == '_measure_facade' and _is_facade_call(n.value.elts[0].value)
                        and isinstance(n.slice, ast.Constant) and n.slice.value == 1), None)
        if wrapper is None:
            break
        _, _, position = _positions(code)
        body = ast.get_source_segment(code, wrapper.value.elts[1])
        code = code[:position(wrapper.lineno, wrapper.col_offset)] + body + code[position(wrapper.end_lineno, wrapper.end_col_offset):]
    lines, offsets, position = _positions(code)
    edits = []
    for node in ast.walk(ast.parse(code)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == '_measure_facade' and _is_facade_call(node.value)):
            left, right = position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)
            if code[right:right + 1] == ';':
                right += 2
            elif not lines[node.lineno - 1][:node.col_offset].strip():
                left, right = offsets[node.lineno - 1], offsets[node.end_lineno]
            edits.append((left, right, ''))
        elif isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id == '_measure_facade':
                if node.attr == '__dict__':
                    edits.append((position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset), 'globals()'))
                else:
                    edits.append((position(value.lineno, value.col_offset), position(value.end_lineno, value.end_col_offset) + 1, ''))
            elif _is_facade_call(value):
                edits.append((position(value.lineno, value.col_offset), position(value.end_lineno, value.end_col_offset) + 1, ''))
    for left, right, replacement in sorted(edits, reverse=True):
        code = code[:left] + replacement + code[right:]
    leftovers = [n.id for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Name) and n.id in ('_measure_facade', '_compat')]
    if leftovers:
        raise Refusal(f'{row["name"]}: qualification the extractor does not produce ({leftovers[0]})')
    tree = ast.parse(code)
    function = tree.body[0]
    body_start = function.body[0].lineno - 1
    restored = row['signature'].splitlines() + code.splitlines()[body_start:]
    for declaration in row['global_declarations']:
        if '\n' in declaration['text']:
            raise Refusal(f'{row["name"]}: multi-line global declaration is not supported')
        index = declaration['line']
        if index >= len(restored) or restored[index].strip() != 'pass' and not restored[index].strip().startswith('global'):
            raise Refusal(f'{row["name"]}: cannot restore the global declaration at line {index}')
        restored[index] = restored[index][:len(restored[index]) - len(restored[index].lstrip())] + declaration['text']
    return '\n'.join(restored)


def _module_names(facade_source, facade_tree, root):
    """Names the extractor treats as facade globals, independent of which lane moved what."""
    top = symtable.symtable(facade_source, 'measure.py', 'exec')
    names = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    for node in ast.walk(facade_tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    manifest = json.loads(_read(Path(root) / 'scripts/measure_split_manifest.json'))
    for symbol in manifest['symbols']:
        for declaration in symbol.get('global_declarations', []):
            names.update(ast.parse(declaration['text']).body[0].names)
    return sorted(name for name in names if name.isidentifier())


def verify_completed(root, packet, done, facade_source, facade_tree, provider_source, provider_tree, tool):
    """Every finished symbol must still be the frozen source plus the allowed qualification.

    Per symbol: (1) reversing the qualification reproduces the frozen source
    (packet sha256); (2) extracting that frozen source again yields exactly
    the body in the provider and exactly the binding statement in the facade,
    captured defaults and decorators included. A changed body, a bypassed
    facade lookup, a changed default or decorator, or a changed signature all
    make one of the two comparisons fail.
    """
    if not done:
        return
    rows = {row['name']: row for row in packet['symbols']}
    provider_lines = provider_source.splitlines()
    facade_lines = facade_source.splitlines()
    bindings = {n.targets[0].id: n for n in facade_tree.body
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value.func) == BIND_FUNCTION and isinstance(n.targets[0], ast.Name)}
    nodes = {n.name: n for n in provider_tree.body if isinstance(n, tool.DEFINITION)}
    names = _module_names(facade_source, facade_tree, root)
    prelude = ''.join(' = '.join(names[i:i + 80]) + ' = None\n' for i in range(0, len(names), 80))
    problems = []
    for name in done:
        node, row = nodes[name], rows[name]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        body = '\n'.join(provider_lines[first - 1:node.end_lineno])
        try:
            original = restore_original(body, row)
        except (Refusal, SyntaxError, IndexError) as exc:
            problems.append(f'{name}: moved body cannot be reversed to the frozen source ({exc})')
            continue
        if hashlib.sha256(original.encode()).hexdigest() != row['sha256']:
            problems.append(f'{name}: moved body drifted from the frozen source (sha256 mismatch after reversing the qualification)')
            continue
        source = prelude + original + '\n'
        moved, binding = tool.extract_definition(source, name, MODULE)
        tail = trailing_comment(source.splitlines(keepends=True), next(
            n for n in ast.parse(source).body if isinstance(n, tool.DEFINITION) and n.name == name))
        expected = moved.rstrip('\n') + tail
        if body != expected:
            problems.append(f'{name}: moved body differs from the extractor output for the frozen source')
        actual = ast.get_source_segment(facade_source, bindings[name])
        if actual != binding:
            problems.append(f'{name}: binding (defaults, decorators or provider reference) differs from the frozen source: '
                            f'expected {binding!r}, found {actual!r}')
    if problems:
        raise Refusal('completed definitions no longer match the frozen source, nothing written:\n  ' + '\n  '.join(problems))


# ---------------------------------------------------------------- position proof

_DEF_KINDS = ('FunctionDef', 'AsyncFunctionDef', 'ClassDef')


def frozen_sequence(root):
    """Top-level items of the frozen source, in order: definitions by name, other statements by hash."""
    manifest = json.loads(_read(Path(root) / 'scripts/measure_split_manifest.json'))
    by_end = {(row['end'], row['kind']): row['name'] for row in manifest['symbols']}
    guard = manifest['dispatch']['start'] - 1
    seen, sequence = {}, []
    for row in manifest['statements']:
        if row['kind'] in _DEF_KINDS:
            sequence.append(('def', by_end[(row['end'], row['kind'])]))
        elif row['kind'] == 'If' and row['start'] == guard:
            sequence.append(('dispatch',))
        else:
            seen[row['sha256']] = seen.get(row['sha256'], 0) + 1
            sequence.append(('stmt', row['sha256'], seen[row['sha256']]))
    return sequence


def current_sequence(facade_source, facade_tree, tool):
    """The same keys for the facade as it is now: a binding stands for the definition it replaced."""
    lines = facade_source.splitlines()
    seen, sequence = {}, []
    for node in facade_tree.body:
        if isinstance(node, tool.DEFINITION):
            key = ('def', node.name)
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and ast.unparse(node.value.func) == BIND_FUNCTION):
            key = ('def', node.targets[0].id)
        elif isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            key = ('dispatch',)
        else:
            digest = hashlib.sha256('\n'.join(lines[node.lineno - 1:node.end_lineno]).encode()).hexdigest()
            seen[digest] = seen.get(digest, 0) + 1
            key = ('stmt', digest, seen[digest])
        sequence.append((key, node.lineno, lines[node.lineno - 1].strip()[:70]))
    return sequence


def verify_positions(root, facade_source, facade_tree, tool):
    """Every frozen item still present must keep its place relative to every other one.

    A binding moved across a constant, a state initialisation, another lane's
    binding or an unmoved definition changes which names its captured defaults
    can see and which value they capture, even when the order of this lane's
    own bindings is untouched. Comparing against the frozen top-level sequence
    (not line numbers) tolerates other lanes' in-place replacements and the
    bridge statements U0 added, and refuses any displacement.
    """
    index = {key: i for i, key in enumerate(frozen_sequence(root))}
    anchored = [(index[key], key, line, text) for key, line, text in current_sequence(facade_source, facade_tree, tool)
                if key in index]
    for earlier, later in zip(anchored, anchored[1:]):
        if later[0] <= earlier[0]:
            def label(item):
                return item[1][1] if item[1][0] == 'def' else f'line {item[2]}: {item[3]}'
            raise Refusal(f'position: {label(later)} (line {later[2]}) now follows {label(earlier)} (line {earlier[2]}) '
                          'but precedes it in the frozen source; a binding or statement moved across an initialisation')
    return len(anchored)


# ---------------------------------------------------------------- rebuild from the pinned frozen inputs

SCRATCH = '.scratch-u4'
FRAGMENT = 'scripts/mutations/_measure_lane4.json'
FACADE_REL = 'skills/token-optimizer/scripts/measure.py'
PROVIDER_REL = 'skills/token-optimizer/scripts/_measure/cache_policy.py'
PROVIDER_DIR = 'skills/token-optimizer/scripts/_measure/'
TOOL_FILES = ('scripts/measure_split.py', 'scripts/measure_split_ownership.json', 'scripts/measure_split_manifest.json')

# The frozen inputs, pinned by commit and sha256. Nothing here is derived from the
# working tree: the move input is U0-FINAL's tree (final BASE 55da0b11 plus U0's
# facade bridge and empty provider modules). Fetched by sha, checked by sha256,
# and the move is refused when any file cannot be obtained or does not match.
# Refresh deliberately with:  u4_move.py --print-pin <U0 commit>
PIN = {
    'base': {
        'commit': '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f',
        'path': 'skills/token-optimizer/scripts/measure.py',
        'sha256': 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc',
    },
    'input': {
        'commit': '8aa1464db203b78bded10b87f8ecbd2b497f7fad',
        'files': {
            'scripts/measure_split.py':
                'a25e3b66514ffdcc1ed640f7e763af38db1b8b4518b6917dd1295f1a5c11cd73',
            'scripts/measure_split_manifest.json':
                'e2708beba9bbd5671b6e492b564ba19006c4e0b27ba7b9fe38eda8d06e73e73f',
            'scripts/measure_split_ownership.json':
                '04574f5d338a160a3018eaa436b7b1e7f0ddaf03fcd456c5858d7dc787fc9d37',
            'scripts/mutations/_measure_lane4.json':
                '37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570',
            'skills/token-optimizer/scripts/_measure/__init__.py':
                '57114e197b9b1879fb97d0985b77cd215b27b1d8f6fa223488ebd16c25e5c961',
            'skills/token-optimizer/scripts/_measure/_compat.py':
                '6a910e5da6a8b25813aa391d490aeee10fd47847a7e625cd2391ff109fcae130',
            'skills/token-optimizer/scripts/_measure/audit.py':
                '90e657411ab28d2ce4837bfb84dc6084f9ba40d6e6dc9f8be8f2f3d81a256e9a',
            'skills/token-optimizer/scripts/_measure/cache_policy.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/cli.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/continuity.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/daemon.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/dashboard.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/lifecycle.py':
                'db266db17988892909e234a477af64f8fcf25c351a322a195c3c739242d7b246',
            'skills/token-optimizer/scripts/_measure/pricing.py':
                '530843bc43c3c15b7a83aa5c035b1fb474862233fce4cc4419071d95c2089102',
            'skills/token-optimizer/scripts/_measure/quality_archive.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/runtime.py':
                'b93b727066de52d1810ee30c0f8132c26fa2e36e21eef6805c34806998967f42',
            'skills/token-optimizer/scripts/_measure/savings.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/sessions.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/_measure/trends.py':
                'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
            'skills/token-optimizer/scripts/measure.py':
                '82de61c808d96ce8ea7318d332ba2e57c2267cb8d1d8eb20817db7a2255b8fde',
        },
    },
}


def _git(objects, *args):
    try:
        return subprocess.run(['git', '-C', str(objects), *args], capture_output=True)
    except OSError as exc:
        raise Refusal(f'cannot run git to obtain the pinned frozen inputs: {exc}')


_OBTAINED = {}
_REBUILT = {}


def _pin_key(pin, objects):
    return (str(Path(objects).resolve()), json.dumps(pin, sort_keys=True))


def obtain_frozen(pin, objects):
    """{path: text} for every pinned file, each verified against its pinned sha256.

    Objects come from the local repository, else from `git fetch --depth=1 origin
    <sha>` (works in a shallow CI checkout). Anything missing or different refuses.
    """
    key = _pin_key(pin, objects)
    if key in _OBTAINED:
        return _OBTAINED[key]
    commit, wanted = pin['input']['commit'], pin['input']['files']
    if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise Refusal(f'the pinned commit must be a full sha, got {commit!r}')
    exists = ['cat-file', '-e', f'{commit}^{{commit}}']
    if _git(objects, *exists).returncode != 0:
        fetched = _git(objects, 'fetch', '--depth=1', '--no-tags', 'origin', commit)
        if fetched.returncode != 0 or _git(objects, *exists).returncode != 0:
            raise Refusal(f'cannot obtain the pinned frozen inputs: commit {commit[:12]} is not in {objects} and '
                          f'`git fetch --depth=1 origin {commit[:12]}` failed: '
                          + fetched.stderr.decode('utf-8', 'replace').strip()[:200])
    files, bad = {}, []
    for path, digest in sorted(wanted.items()):
        blob = _git(objects, 'cat-file', 'blob', f'{commit}:{path}')
        if blob.returncode != 0:
            bad.append(f'{path}: not in {commit[:12]}')
        elif hashlib.sha256(blob.stdout).hexdigest() != digest:
            bad.append(f'{path}: sha256 {hashlib.sha256(blob.stdout).hexdigest()[:16]}... is not the pinned {digest[:16]}...')
        else:
            files[path] = blob.stdout.decode('utf-8')
    if bad:
        raise Refusal('the pinned frozen inputs do not match, nothing written:\n  ' + '\n  '.join(bad))
    _OBTAINED[key] = files
    return files


def print_pin(objects, commit):
    """The PIN literal for a U0 commit: its measure.py, provider modules, fragment and split tooling."""
    commit = _git(objects, 'rev-parse', f'{commit}^{{commit}}').stdout.decode().strip()
    names = _git(objects, 'ls-tree', '-r', '--name-only', commit, '--', PROVIDER_DIR).stdout.decode().split()
    wanted = sorted([FACADE_REL, FRAGMENT, *TOOL_FILES, *names])
    files = {}
    for path in wanted:
        blob = _git(objects, 'cat-file', 'blob', f'{commit}:{path}')
        if blob.returncode != 0:
            raise Refusal(f'{path} is not in {commit[:12]}')
        files[path] = hashlib.sha256(blob.stdout).hexdigest()
    manifest = json.loads(_git(objects, 'cat-file', 'blob', f'{commit}:scripts/measure_split_manifest.json').stdout)
    base = _git(objects, 'cat-file', 'blob', f'{manifest["base"]}:{FACADE_REL}')
    if base.returncode != 0 or hashlib.sha256(base.stdout).hexdigest() != manifest['source_sha256']:
        raise Refusal('the BASE named by the manifest is not available here or does not match its source_sha256')
    return {'base': {'commit': manifest['base'], 'path': FACADE_REL, 'sha256': manifest['source_sha256']},
            'input': {'commit': commit, 'files': files}}


def _difference(path, expected, actual):
    import difflib
    delta = list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), 'rebuilt', 'working tree', lineterm='', n=0))
    return f'{path}: differs from the rebuild of the frozen inputs\n    ' + '\n    '.join(line[:200] for line in delta[:14])


def rebuild(root, pin, objects, scratch=None):
    """Run this lane's move on the pinned frozen inputs, in a scratch dir built from nothing else."""
    import shutil
    frozen = obtain_frozen(pin, objects)
    key = _pin_key(pin, objects)
    if key in _REBUILT:
        return _REBUILT[key]
    scratch = Path(scratch if scratch is not None else Path(root) / SCRATCH) / 'rebuild'
    if scratch.exists():
        shutil.rmtree(scratch)
    try:
        for relative, text in frozen.items():
            target = scratch / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode('utf-8'))
        with open(os.devnull, 'w') as sink:
            run(scratch, rebuilding=True, out=sink)
        rebuilt = {FACADE_REL: _read(scratch / FACADE_REL),
                   PROVIDER_REL: _read(scratch / PROVIDER_REL),
                   FRAGMENT: frozen.get(FRAGMENT, '')}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            scratch.parent.rmdir()
        except OSError:
            pass
    _REBUILT[key] = rebuilt
    return rebuilt


def rebuild_and_compare(root, pin, objects, facade_text, provider_text, fragment_text, scratch=None):
    """Byte-compare every file this lane writes against the rebuild of the pinned frozen inputs."""
    rebuilt = rebuild(root, pin, objects, scratch)
    problems = [_difference(path, rebuilt[path], actual)
                for path, actual in ((FACADE_REL, facade_text), (PROVIDER_REL, provider_text))
                if rebuilt[path] != actual]
    if rebuilt[FRAGMENT] != fragment_text:
        problems.append(f'{FRAGMENT}: this lane does not write the mutation fragment but it differs from the pinned one')
    if problems:
        raise Refusal('the working tree is not the rebuild of the pinned frozen inputs '
                      f'({pin["input"]["commit"][:8]}), nothing written:\n  ' + '\n  '.join(problems))
    return f'pinned {pin["input"]["commit"][:8]}, BASE {pin["base"]["commit"][:8]}'


def atomic_write(path, text):
    temporary = path.with_name(path.name + '.u4tmp')
    _write(temporary, text)
    os.replace(temporary, path)


def run(root=REPO, *, check=False, out=None, pin=None, objects=None, scratch=None, rebuilding=False):
    """Move (or verify) lane 4.

    "Already applied" means exactly one thing: running this same move on the
    PINNED frozen inputs (commit + sha256, never read from the working tree), in
    a scratch directory, reproduces every file this lane writes (measure.py and
    cache_policy.py) byte for byte, and leaves the mutation fragment alone. The
    finer checks (hash, binding, position, local binding audit) run first and
    give precise messages; the rebuild is the authority and also catches
    whatever they were not written to look for. A fresh move is held to the same
    standard before anything is written.
    """
    out = out or sys.stdout
    root = Path(root)
    scripts = root / 'skills/token-optimizer/scripts'
    facade_path, provider_path = scripts / 'measure.py', scripts / '_measure' / (MODULE + '.py')
    tool = load_tool(root)
    packet = lane_packet(root)
    facade_source = _read(facade_path)
    provider_source = _read(provider_path)
    if '\r' in facade_source or '\r' in provider_source:
        raise Refusal('CRLF line endings: the move assumes LF files')
    facade_tree, provider_tree = ast.parse(facade_source), ast.parse(provider_source)
    pending, done, problems, definitions = classify(packet, facade_tree, provider_tree, tool)
    total = len(packet['symbols'])
    if problems:
        raise Refusal('refusing, nothing written:\n  ' + '\n  '.join(problems))
    verify_positions(root, facade_source, facade_tree, tool)
    verify_completed(root, packet, done, facade_source, facade_tree, provider_source, provider_tree, tool)
    final_facade, final_provider, regions = facade_source, provider_source, []
    if pending:
        verify_source(packet, definitions, pending, facade_source.splitlines())
        if check:
            print(f'lane {LANE}: {len(pending)}/{total} pending, {len(done)} done, no hash drift', file=out)
            return 1
        final_provider, final_facade, regions = build(root, packet, definitions, pending, facade_source,
                                                      provider_source, tool)
        validate(root, packet, pending, facade_source, final_facade, final_provider, regions, tool)
    else:
        broken = binding_violations(provider_source)
        if broken:
            raise Refusal('local bindings were rewritten to facade attributes: ' + ', '.join(
                f'{function}:{name}@{line}' for function, name, line in broken))
    label = 'rebuild skipped'
    if not rebuilding:
        fragment = root / FRAGMENT
        label = rebuild_and_compare(root, PIN if pin is None else pin, root if objects is None else objects,
                                    final_facade, final_provider,
                                    _read(fragment) if fragment.exists() else '', scratch)
    if not pending:
        print(f'lane {LANE}: already applied, {len(done)}/{total} symbols in {MODULE}.py, byte-identical to the '
              f'rebuild of the frozen inputs ({label}), nothing to do', file=out)
        return 0
    atomic_write(provider_path, final_provider)
    atomic_write(facade_path, final_facade)
    before, after = len(facade_source.splitlines()), len(final_facade.splitlines())
    print(f'lane {LANE}: moved {len(pending)} definitions to {MODULE}.py ({len(done)} were already done) '
          f'({len(final_provider.splitlines())} lines); measure.py {before} -> {after} lines; '
          f'byte-identical to the rebuild of the frozen inputs ({label})', file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=REPO, help='repository root (default: this checkout)')
    parser.add_argument('--check', action='store_true', help='report state and write nothing')
    parser.add_argument('--objects', type=Path, default=None,
                        help='git repository that holds (or can fetch) the pinned commit (default: --root)')
    parser.add_argument('--print-pin', metavar='COMMIT', default=None,
                        help='print the PIN literal for a U0 commit and exit (deliberate re-pin)')
    args = parser.parse_args(argv)
    try:
        if args.print_pin:
            print(json.dumps(print_pin(args.objects or args.root, args.print_pin), indent=4))
            return 0
        return run(args.root, check=args.check, objects=args.objects)
    except Refusal as exc:
        print(f'u4_move: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
