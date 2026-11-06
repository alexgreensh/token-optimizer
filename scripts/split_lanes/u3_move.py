#!/usr/bin/env python3
"""Lane U3: move the savings definitions from measure.py into _measure/savings.py.

Deterministic and re-runnable. Symbols come BY NAME from the lane-3 packet in
scripts/measure_split_ownership.json; every source slice is checked against the
frozen hash in scripts/measure_split_manifest.json. Qualification and the
in-place facade binding are U0's (scripts/measure_split.py move()), so this lane
adds no new rewriting rules.

The script also rewrites lane 3's mutation fragments
(scripts/mutations/_measure_lane3.json) to the qualified provider text. Each
rewritten fragment is proven equivalent: moving the mutated measure.py must
produce exactly the provider the fragment plants.

Refuses (exit 1, nothing written) on: a packet symbol that is missing, defined
twice, defined in both the facade and the provider, hash-drifted, bound without
a body or a body without a binding, or an unowned definition in savings.py.
A fully moved tree is a verified no-op.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import io
import os
import re
import shutil
import subprocess
import hashlib
import importlib.util
import json
import symtable
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LANE = '3'
MODULE = 'savings'
OWNERSHIP = REPO / 'scripts/measure_split_ownership.json'
FRAGMENTS = REPO / 'scripts/mutations/_measure_lane3.json'
PROVIDER_REL = 'skills/token-optimizer/scripts/_measure/savings.py'


def _tool():
    spec = importlib.util.spec_from_file_location('_u3_measure_split', REPO / 'scripts/measure_split.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


split = _tool()


class Refusal(Exception):
    pass


class IterableLambdaQualifier(split.Qualifier):
    """U0's Qualifier, except for a lambda inside a comprehension iterable.

    The shared mover wraps every lambda body as ((_measure_facade := ...), body)[1].
    CPython rejects an assignment expression anywhere in a comprehension's
    iterable, lambdas included (SyntaxError), e.g. sorted(..., key=lambda m: ...)
    as a comprehension source. Only there, global names use the direct
    `_compat.facade().NAME` spelling U0 already emits for class scopes: still a
    live lookup at call time, and the inspection view unqualifies it the same way.
    Everything else is U0's rule, unchanged. Reported to U0 for the shared tool.
    """
    iterable_lambdas = None
    direct = 0
    used = 0

    def _collect(self):
        found = set()
        for node in ast.walk(self.root):
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for generator in node.generators:
                    found |= {id(n) for n in ast.walk(generator.iter) if isinstance(n, ast.Lambda)}
        return found

    def visit_Lambda(self, node):
        if self.iterable_lambdas is None:
            self.iterable_lambdas = self._collect()
        if id(node) not in self.iterable_lambdas:
            return super().visit_Lambda(node)
        IterableLambdaQualifier.used += 1
        for value in node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        self.direct += 1
        try:
            self.scoped(node, 'lambda', [node.body])
        finally:
            self.direct -= 1

    def _redirect(self, count):
        if self.direct and len(self.edits) > count:
            left, right, text = self.edits[-1]
            if text.startswith('_measure_facade.'):
                self.edits[-1] = (left, right, '_compat.facade().' + text[len('_measure_facade.'):])
            elif text.startswith('_compat.read_global(_measure_facade, '):
                self.edits[-1] = (left, right, '_compat.read_global(_compat.facade(), '
                                  + text[len('_compat.read_global(_measure_facade, '):])

    def visit_Name(self, node):
        count = len(self.edits)
        super().visit_Name(node)
        self._redirect(count)

    def visit_Call(self, node):
        count = len(self.edits)
        super().visit_Call(node)
        if self.direct and len(self.edits) == count + 1 and self.edits[-1][2] == '_measure_facade.__dict__':
            left, right, _ = self.edits[-1]
            self.edits[-1] = (left, right, '_compat.facade().__dict__')


# extract_definition/move resolve Qualifier from this private copy of the tool.
split.Qualifier = IterableLambdaQualifier


def lane_symbols():
    packet = json.loads(OWNERSHIP.read_text(encoding='utf-8'))['lanes'][LANE]
    if packet['modules'] != [MODULE]:
        raise Refusal(f'lane {LANE} packet modules changed: {packet["modules"]}')
    manifest = {row['name']: row for row in split.read_manifest()['symbols']}
    names = []
    for row in packet['symbols']:
        name = row['name']
        if row['module'] != MODULE or row['lane'] != int(LANE):
            raise Refusal(f'{name}: packet assigns it to {row["module"]}/lane {row["lane"]}')
        frozen = manifest.get(name)
        if frozen is None or frozen['module'] != MODULE or frozen['sha256'] != row['sha256']:
            raise Refusal(f'{name}: ownership packet and source manifest disagree')
        names.append(name)
    if len(set(names)) != len(names):
        raise Refusal('lane packet lists a symbol twice')
    return names, manifest


def definitions(tree):
    found = {}
    for node in tree.body:
        if isinstance(node, split.DEFINITION):
            found.setdefault(node.name, []).append(node)
    return found


def bindings(tree):
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            found.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    return found


def slice_sha(lines, node):
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return hashlib.sha256('\n'.join(lines[first - 1:node.end_lineno]).encode()).hexdigest()


def classify(scripts, names, manifest):
    """Return the packet symbols still in the facade, in source order."""
    facade_text = (scripts / 'measure.py').read_text(encoding='utf-8')
    provider_text = (scripts / '_measure' / (MODULE + '.py')).read_text(encoding='utf-8')
    facade_tree, provider_tree = ast.parse(facade_text), ast.parse(provider_text)
    in_facade, in_provider = definitions(facade_tree), definitions(provider_tree)
    bound = bindings(facade_tree)
    lines = facade_text.splitlines()
    errors, pending = [], []
    for name in names:
        here, there = in_facade.get(name, []), in_provider.get(name, [])
        binding = bound.get(name, [])
        if len(here) + len(there) == 0:
            errors.append(f'{name}: missing from measure.py and {MODULE}.py')
        elif len(here) > 1 or len(there) > 1 or (here and there):
            errors.append(f'{name}: duplicate implementation (facade={len(here)}, provider={len(there)})')
        elif here:
            if binding:
                errors.append(f'{name}: facade has both a body and a binding')
            elif slice_sha(lines, here[0]) != manifest[name]['sha256']:
                errors.append(f'{name}: source drifted from the frozen manifest hash')
            else:
                pending.append(here[0])
        elif binding != [f"_measure_providers[{MODULE!r}].{name}"]:
            errors.append(f'{name}: moved body without its single facade binding ({binding})')
    owned = set(names)
    for name in in_provider:
        if name not in owned:
            errors.append(f'{MODULE}.py: unowned definition {name}')
    if errors:
        raise Refusal('\n'.join(errors))
    return facade_text, [node.name for node in sorted(pending, key=lambda n: n.lineno)]


def _inspection():
    """U0's inverse of the mover (tests/helpers/measure_split.py), loaded read-only."""
    spec = importlib.util.spec_from_file_location('_u3_inspection', REPO / 'tests/helpers/measure_split.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._inspection_text


def validate_completed(scripts, names, manifest):
    """Prove the moved state is exactly what the mover makes from frozen source.

    1. Unqualify each moved body, restore its frozen signature and `global`
       lines, and require the frozen manifest sha256 (body drift).
    2. Put those frozen definitions back in place of their bindings and run
       the mover again. Every provider body and every facade binding statement,
       captured defaults included, must match byte for byte (qualification,
       placeholder or default drift).
    3. savings.py must be exactly U0's header plus those bodies, in source order.
    Returns a list of errors; empty means proven.
    """
    unqualify = _inspection()
    facade_text = (scripts / 'measure.py').read_text(encoding='utf-8')
    provider_path = scripts / '_measure' / (MODULE + '.py')
    provider_text = provider_path.read_text(encoding='utf-8')
    facade_nodes = {}
    for node in ast.parse(facade_text).body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'
                and node.targets[0].id in names):
            facade_nodes[node.targets[0].id] = node
    provider_lines = provider_text.splitlines()
    provider_nodes = {n.name: n for n in ast.parse(provider_text).body if isinstance(n, split.DEFINITION)}
    errors, frozen_text = [], {}
    for name in names:
        impl = provider_nodes[name]
        text = unqualify('\n'.join(provider_lines[impl.lineno - 1:impl.end_lineno]))
        first_body = ast.parse(text).body[0].body[0].lineno - 1
        restored = (manifest[name]['signature'] + '\n' + '\n'.join(text.splitlines()[first_body:])).splitlines(keepends=True)
        for declaration in manifest[name].get('global_declarations', []):
            index = declaration['line']
            if '\n' not in declaration['text'] and index < len(restored) and restored[index].strip() == 'pass':
                indent = len(restored[index]) - len(restored[index].lstrip())
                restored[index] = ' ' * indent + declaration['text'] + ('\n' if restored[index].endswith('\n') else '')
        text = ''.join(restored)
        if hashlib.sha256(text.encode()).hexdigest() != manifest[name]['sha256']:
            errors.append(f'{name}: moved body no longer matches the frozen source')
        frozen_text[name] = text
    if errors:
        return errors
    lines = facade_text.splitlines(keepends=True)
    for name in sorted(names, key=lambda n: facade_nodes[n].lineno, reverse=True):
        node = facade_nodes[name]
        lines[node.lineno - 1:node.end_lineno] = [frozen_text[name] + '\n']
    rebuilt = ''.join(lines)
    tree = ast.parse(rebuilt)
    table = symtable.symtable(rebuilt, 'measure.py', 'exec')
    expected_provider = split.HEADER.rstrip()
    for name in names:
        body, binding = split.extract_definition(rebuilt, name, MODULE, tree=tree, table=table)
        if ast.get_source_segment(provider_text, provider_nodes[name]) + '\n' != body:
            return [f'{name}: moved body differs from re-moving the frozen source']
        if ast.get_source_segment(facade_text, facade_nodes[name]) != binding:
            return [f'{name}: facade binding (captured defaults) differs from the frozen definition']
        expected_provider = expected_provider.rstrip() + '\n\n\n' + body  # as move() appends
    if provider_text != expected_provider:
        return [f'{MODULE}.py: extra or reordered content beyond the moved bodies']
    return check_positions(facade_text, names)


def _statement_tokens(text):
    """Top-level order: every definition or binding (any lane) by name, others by sha."""
    lines = text.splitlines()
    tokens = []
    for node in ast.parse(text).body:
        if isinstance(node, split.DEFINITION):
            tokens.append(('def', node.name))
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
              and ast.unparse(node.value.func) == '_measure_compat.bind'):
            tokens.append(('def', node.targets[0].id))
        else:
            tokens.append(('stmt', hashlib.sha256('\n'.join(lines[node.lineno - 1:node.end_lineno]).encode()).hexdigest()))
    return tokens


def check_positions(facade_text, names):
    """Each lane binding keeps its frozen place among ALL top-level statements.

    BASE order comes from the frozen manifest: definitions by name, every other
    statement (constants, state initialization, imports) by source sha256.
    Tokens whose count differs between BASE and the facade (U0 bootstrap
    additions, a later lane's dispatch move) drop out on both sides; whatever
    remains must be the identical sequence. A binding moved across an
    initialization or another definition changes that sequence and refuses.
    Also pins each lane binding between its nearest surviving BASE neighbours.
    """
    manifest = split.read_manifest()
    by_end = {row['end']: row['name'] for row in manifest['symbols']}
    base = [('def', by_end[row['end']]) if row['kind'] in ('FunctionDef', 'AsyncFunctionDef', 'ClassDef')
            else ('stmt', row['sha256']) for row in manifest['statements']]
    current = _statement_tokens(facade_text)
    from collections import Counter
    base_count, current_count = Counter(base), Counter(current)
    keep = {token for token in base_count if base_count[token] == current_count.get(token)}
    base_kept = [token for token in base if token in keep]
    current_kept = [token for token in current if token in keep]
    errors = []
    missing = [name for name in names if ('def', name) not in keep]
    if missing:
        errors.append('lane bindings missing from the facade order: ' + ', '.join(missing[:5]))
    if base_kept != current_kept:
        index = next(i for i, (a, b) in enumerate(zip(base_kept + [None], current_kept + [None])) if a != b)
        near = {token[1] for token in base_kept[max(0, index - 2):index + 3] if token and token[0] == 'def'}
        errors.append(f'facade statement order differs from the frozen BASE at kept position {index} '
                      f'(near {sorted(near)[:4]}): a binding moved across initialization or another definition')
    statements = sum(1 for token in base_kept if token[0] == 'stmt')
    if statements < 0.95 * sum(1 for token in base if token[0] == 'stmt'):
        errors.append(f'only {statements} BASE top-level statements still anchor the facade order')
    return errors


# ---------------------------------------------------------------- fragments

def _qualified_span(source, tree, table, name, left, right):
    """Provider text for source[left:right] inside `name`, using U0's Qualifier.

    Mirrors measure_split.extract_definition's setup. Edits wholly inside the
    span apply; an edit straddling a span edge returns None (caller falls back).
    """
    import copy
    node = next(n for n in tree.body if isinstance(n, split.DEFINITION) and n.name == name)
    qualifier = split.Qualifier(source, table, {s.get_name() for s in table.get_symbols()
                                                if s.is_assigned() or s.is_imported()} |
                                {n for st in ast.walk(tree) if isinstance(st, ast.Global) for n in st.names},
                                tree=tree)
    node = copy.deepcopy(node)
    qualifier.root = node
    node.decorator_list = []
    defaults = list(node.args.defaults) + [v for v in node.args.kw_defaults if v]
    node.args.defaults = []
    node.args.kw_defaults = [None] * len(node.args.kwonlyargs)
    qualifier.visit(node)
    for value in defaults:
        qualifier.replace(value, 'None')
    text = source[left:right]
    for l, r, value in sorted(qualifier.edits, reverse=True):
        if l == r and (l == left or l == right):
            continue  # insertion at an edge belongs outside the fragment
        if left <= l <= r <= right:
            text = text[:l - left] + value + text[r - left:]
        elif l < right and r > left:
            return None
    return text


def _diff_fragment(p0, p1, anchor):
    prefix = 0
    while prefix < min(len(p0), len(p1)) and p0[prefix] == p1[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(p0), len(p1)) - prefix
           and p0[len(p0) - 1 - suffix] == p1[len(p1) - 1 - suffix]):
        suffix += 1
    start = p0.rfind('\n', 0, prefix) + 1
    if anchor is not None:
        start = min(start, anchor)
    end0, end1 = len(p0) - suffix, len(p1) - suffix
    stop = p0.find('\n', end0)
    grow = (len(p0) if stop < 0 else stop + 1) - end0
    return p0[start:end0 + grow], p1[start:end1 + grow]


def _node_span(source, node):
    lines = source.splitlines(keepends=True)
    def offset(line, column):
        # AST columns count UTF-8 bytes; string slicing counts characters.
        return sum(map(len, lines[:line - 1])) + len(lines[line - 1].encode('utf-8')[:column].decode('utf-8'))
    return offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)


def _mutation_groups(fragments, pending):
    """Lane-3 fragments of each logical mutation, in the order they plant."""
    groups = {}
    for index, fragment in enumerate(fragments):
        if fragment.get('symbol') in pending:
            groups.setdefault(fragment['mutation'], []).append(index)
    for indexes in groups.values():
        indexes.sort(key=lambda i: fragments[i]['edit'])
    return groups


def rewrite_fragments(source, pending):
    """Qualified fragments for lane-3 symbols that are about to move.

    Edits of one mutation apply in order, as the harness plants them, so each
    is derived on the source already carrying the previous edits: an edit can
    change the scope of a name elsewhere in the same body (a new local
    assignment), which changes how the mover qualifies it.

    Returns (fragments, report, expected) where expected[mutation][symbol] is
    the provider text that moving the fully mutated measure.py produces.
    """
    fragments = json.loads(FRAGMENTS.read_text(encoding='utf-8'))
    base_tree = ast.parse(source)
    base_table = symtable.symtable(source, 'measure.py', 'exec')
    report, expected = [], {}
    for mutation, indexes in _mutation_groups(fragments, pending).items():
        current, tree, table = source, base_tree, base_table
        for index in indexes:
            fragment = fragments[index]
            label = f'{mutation}#{fragment["edit"]}'
            symbol, old, new = fragment['symbol'], fragment['old'], fragment['new']
            start, end = _node_span(current, definitions(tree)[symbol][0])
            first = current.find(old)
            if first < 0 or not (start <= first and first + len(old) <= end):
                raise Refusal(f'{label}: first match of old is not inside {symbol}')
            mutated = current[:first] + new + current[first + len(old):]
            m_tree = ast.parse(mutated)
            m_table = symtable.symtable(mutated, 'measure.py', 'exec')
            p0, b0 = split.extract_definition(current, symbol, MODULE, tree=tree, table=table)
            p1, b1 = split.extract_definition(mutated, symbol, MODULE, tree=m_tree, table=m_table)
            if b0 != b1:
                raise Refusal(f'{label}: mutation changes the facade binding')
            candidates = []
            if old in p0:
                candidates.append(('verbatim', old, new))
            q_old = _qualified_span(current, tree, table, symbol, first, first + len(old))
            q_new = _qualified_span(mutated, m_tree, m_table, symbol, first, first + len(new))
            if q_old is not None and q_new is not None:
                candidates.append(('qualified', q_old, q_new))
            # Last resort: the exact provider difference, widened to whole lines
            # and back to where the original fragment starts.
            anchor = next((p0.find(a) for _, a, _ in candidates if a in p0), None)
            candidates.append(('diff', *_diff_fragment(p0, p1, anchor)))
            chosen = next(((how, a, b) for how, a, b in candidates
                           if a in p0 and p0.replace(a, b, 1) == p1), None)
            if chosen is None:
                raise Refusal(f'{label}: no equivalent provider fragment')
            how, a, b = chosen
            report.append((mutation, fragment['edit'], symbol, how))
            fragment.update({'file': PROVIDER_REL, 'old': a, 'new': b})
            current, tree, table = mutated, m_tree, m_table
        expected[mutation] = {fragments[i]['symbol']: split.extract_definition(
            current, fragments[i]['symbol'], MODULE, tree=tree, table=table)[0] for i in indexes}
    return fragments, report, expected


def verify_fragments(scripts, expected=None):
    """Replay each lane-3 mutation on the real savings.py, edits in order.

    Each edit's first match must sit inside its own symbol. With `expected`
    (computed at move time), every touched body must equal what moving the
    mutated measure.py produces.
    """
    provider = (scripts / '_measure' / (MODULE + '.py')).read_text(encoding='utf-8')
    fragments = json.loads(FRAGMENTS.read_text(encoding='utf-8'))
    owned = set(definitions(ast.parse(provider)))
    errors = []
    for mutation, indexes in _mutation_groups(fragments, owned).items():
        text = provider
        for index in indexes:
            fragment = fragments[index]
            label = f'{mutation}#{fragment["edit"]}'
            if fragment.get('file') != PROVIDER_REL:
                errors.append(f'{label}: file still {fragment.get("file")}')
            nodes = definitions(ast.parse(text))
            start, end = _node_span(text, nodes[fragment['symbol']][0])
            first = text.find(fragment['old'])
            if first < 0 or not (start <= first and first + len(fragment['old']) <= end):
                errors.append(f'{label}: old not inside {fragment["symbol"]} in {MODULE}.py')
                break
            text = text[:first] + fragment['new'] + text[first + len(fragment['old']):]
        else:
            compile(text, PROVIDER_REL, 'exec')
            if expected is not None:
                nodes = definitions(ast.parse(text))
                for symbol, body in expected[mutation].items():
                    if ast.get_source_segment(text, nodes[symbol][0]) + '\n' != body:
                        errors.append(f'{mutation}: planted {symbol} differs from moving the mutated facade')
    return errors


# ---------------------------------------------------------------- main

# Frozen lane input: the unmoved U0 tree this lane was cut from. Pinned here,
# obtained from git objects (never rebuilt from the working tree), sha-checked.
# --freeze-input rewrites this block for a new U0 head.
# >>> frozen-input
FROZEN_COMMIT = '8aa1464db203b78bded10b87f8ecbd2b497f7fad'
FROZEN_SHA256 = {
    'skills/token-optimizer/scripts/measure.py': '82de61c808d96ce8ea7318d332ba2e57c2267cb8d1d8eb20817db7a2255b8fde',
    'skills/token-optimizer/scripts/_measure/savings.py': 'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
    'scripts/mutations/_measure_lane3.json': '78ebcc35e56f863d95320caf046a2572c528fde59f748d11a35ca92a690c3df1',
}
# <<< frozen-input
# Final BASE the U0 manifest was frozen from (lineage check, not a move input).
BASE_COMMIT = '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f'
BASE_MEASURE_SHA256 = 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc'

# U3_MOVE_SCRATCH lets tests keep the re-run inside their own tmp dir.
SCRATCH = Path(os.environ.get('U3_MOVE_SCRATCH') or REPO / '.scratch-u3' / 'frozen-run')


def _write(path, text):
    """LF on every platform: the repo is LF and the replay compares raw bytes."""
    Path(path).write_bytes(text.encode('utf-8'))


@contextlib.contextmanager
def _lf_writes():
    """U0's move() writes with Path.write_text; keep its output LF on Windows too."""
    original = Path.write_text
    def write_text(self, data, encoding=None, errors=None, newline=None):
        return original(self, data, encoding=encoding, errors=errors, newline='\n' if newline is None else newline)
    Path.write_text = write_text
    try:
        yield
    finally:
        Path.write_text = original


def _checked_out(path):
    """Working-tree bytes as git stores them. *.py is eol=lf in .gitattributes,
    so .py stays raw; the .json fragment file may be CRLF on a Windows
    autocrlf checkout, which is a checkout artifact, not drift."""
    data = Path(path).read_bytes()
    return data if Path(path).suffix == '.py' else data.replace(b'\r\n', b'\n')


def _git(*args, check=True):
    return subprocess.run(['git', *args], cwd=REPO, capture_output=True, check=check)


def frozen_inputs():
    """{repo path: bytes} at FROZEN_COMMIT, from git objects; fetch if absent."""
    if _git('cat-file', '-e', FROZEN_COMMIT + '^{commit}', check=False).returncode:
        fetched = _git('fetch', '--quiet', '--depth=1', '--no-tags', 'origin', FROZEN_COMMIT, check=False)
        if fetched.returncode or _git('cat-file', '-e', FROZEN_COMMIT + '^{commit}', check=False).returncode:
            raise Refusal(f'frozen lane input {FROZEN_COMMIT} cannot be obtained '
                          f'(git fetch --depth=1 origin {FROZEN_COMMIT} failed): nothing verified, nothing written\n'
                          + fetched.stderr.decode('utf-8', 'replace').strip())
    manifest = split.read_manifest()
    if manifest['base'] != BASE_COMMIT or manifest['source_sha256'] != BASE_MEASURE_SHA256:
        raise Refusal(f'U0 manifest is not frozen at the pinned final BASE {BASE_COMMIT}')
    if _git('cat-file', '-e', BASE_COMMIT + '^{commit}', check=False).returncode:
        _git('fetch', '--quiet', '--depth=1', '--no-tags', 'origin', BASE_COMMIT, check=False)
    base = _git('show', f'{BASE_COMMIT}:skills/token-optimizer/scripts/measure.py', check=False)
    if base.returncode or _sha(base.stdout) != BASE_MEASURE_SHA256:
        raise Refusal(f'final BASE {BASE_COMMIT} measure.py cannot be obtained or fails its pinned sha256')
    out = {}
    for path, digest in FROZEN_SHA256.items():
        shown = _git('show', f'{FROZEN_COMMIT}:{path}', check=False)
        if shown.returncode or _sha(shown.stdout) != digest:
            raise Refusal(f'frozen input {FROZEN_COMMIT}:{path} is missing or fails its pinned sha256')
        out[path] = shown.stdout
    if out['skills/token-optimizer/scripts/_measure/savings.py'] != split.HEADER.encode('utf-8'):
        raise Refusal('frozen provider is not the U0 empty header')
    return out


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


@contextlib.contextmanager
def _fragments_at(path):
    global FRAGMENTS
    previous, FRAGMENTS = FRAGMENTS, Path(path)
    try:
        yield
    finally:
        FRAGMENTS = previous


def rebuild_input(scripts, names, manifest):
    """The pre-move facade, rebuilt from the moved tree.

    Each binding is replaced, where it stands, by its frozen definition
    (moved body unqualified with U0's inverse, frozen signature and `global`
    lines restored, frozen sha256 required). Returns (text, errors).
    """
    unqualify = _inspection()
    facade_text = (scripts / 'measure.py').read_text(encoding='utf-8')
    provider_text = (scripts / '_measure' / (MODULE + '.py')).read_text(encoding='utf-8')
    provider_lines = provider_text.splitlines()
    provider_nodes = {n.name: n for n in ast.parse(provider_text).body if isinstance(n, split.DEFINITION)}
    facade_nodes = {n.targets[0].id: n for n in ast.parse(facade_text).body
                    if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                    and ast.unparse(n.value.func) == '_measure_compat.bind' and n.targets[0].id in names}
    errors, frozen = [], {}
    for name in names:
        impl = provider_nodes[name]
        text = unqualify('\n'.join(provider_lines[impl.lineno - 1:impl.end_lineno]))
        first_body = ast.parse(text).body[0].body[0].lineno - 1
        restored = (manifest[name]['signature'] + '\n' + '\n'.join(text.splitlines()[first_body:])).splitlines(keepends=True)
        for declaration in manifest[name].get('global_declarations', []):
            index = declaration['line']
            if '\n' not in declaration['text'] and index < len(restored) and restored[index].strip() == 'pass':
                indent = len(restored[index]) - len(restored[index].lstrip())
                restored[index] = ' ' * indent + declaration['text'] + ('\n' if restored[index].endswith('\n') else '')
        frozen[name] = ''.join(restored)
        if _sha(frozen[name]) != manifest[name]['sha256']:
            errors.append(f'{name}: moved body no longer matches the frozen source')
    lines = facade_text.splitlines(keepends=True)
    for name in sorted(names, key=lambda n: facade_nodes[n].lineno, reverse=True):
        node = facade_nodes[name]
        lines[node.lineno - 1:node.end_lineno] = [frozen[name] + '\n']
    return ''.join(lines), errors


def _move_in_scratch(input_measure, fragments_bytes):
    """Run this lane's own move on the frozen inputs. Cached by content."""
    key = _sha(b'\0'.join([input_measure.encode('utf-8'), split.HEADER.encode('utf-8'), fragments_bytes,
                           Path(__file__).read_bytes(), (REPO / 'scripts/measure_split.py').read_bytes(),
                           OWNERSHIP.read_bytes(), (REPO / 'scripts/measure_split_manifest.json').read_bytes(),
                           (REPO / 'tests/helpers/measure_split.py').read_bytes(), sys.version.encode()]))
    out = SCRATCH / key
    outputs = ('measure.py', MODULE + '.py', FRAGMENTS.name)
    if all((out / name).is_file() for name in outputs) and (out / 'receipt.json').is_file():
        receipt = json.loads((out / 'receipt.json').read_text(encoding='utf-8'))
        if all(_sha((out / name).read_bytes()) == receipt[name] for name in outputs):
            return out
    work = SCRATCH / (key + '.work')
    if work.exists():
        shutil.rmtree(work)
    (work / 'scripts/_measure').mkdir(parents=True)
    _write(work / 'scripts/measure.py', input_measure)
    _write(work / 'scripts/_measure' / (MODULE + '.py'), split.HEADER)
    (work / FRAGMENTS.name).write_bytes(fragments_bytes)
    with _fragments_at(work / FRAGMENTS.name), contextlib.redirect_stdout(io.StringIO()):
        move_fresh(work / 'scripts')
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copyfile(work / 'scripts/measure.py', out / 'measure.py')
    shutil.copyfile(work / 'scripts/_measure' / (MODULE + '.py'), out / (MODULE + '.py'))
    shutil.copyfile(work / FRAGMENTS.name, out / FRAGMENTS.name)
    _write(out / 'receipt.json', json.dumps({name: _sha((out / name).read_bytes()) for name in outputs}))
    shutil.rmtree(work)
    return out


def verify_completed(scripts, names, manifest):
    """'Already applied' means: re-running this lane's move on the frozen
    inputs (git objects at FROZEN_COMMIT) reproduces every file the lane
    writes, byte for byte. The rebuild from the working tree is diagnostic only."""
    frozen = frozen_inputs()
    out = _move_in_scratch(frozen['skills/token-optimizer/scripts/measure.py'].decode('utf-8'),
                           frozen['scripts/mutations/_measure_lane3.json'])
    targets = {'measure.py': scripts / 'measure.py', MODULE + '.py': scripts / '_measure' / (MODULE + '.py'),
               FRAGMENTS.name: FRAGMENTS}
    errors, diffs = [], SCRATCH / 'diffs'
    for name, actual in targets.items():
        expected = (out / name).read_bytes()
        if _checked_out(actual) != expected:
            diffs.mkdir(parents=True, exist_ok=True)
            diff_path = diffs / (name + '.diff')
            _write(diff_path, ''.join(difflib.unified_diff(
                expected.decode('utf-8').splitlines(keepends=True),
                actual.read_text(encoding='utf-8').splitlines(keepends=True),
                f'frozen-rerun/{name}', Path(actual).as_posix())))
            errors.append(f'{actual} differs from the frozen re-run; diff: {diff_path}')
    if errors:  # diagnostics: say what drifted
        rebuilt, rebuild_errors = rebuild_input(scripts, names, manifest)
        errors += rebuild_errors
        if not rebuild_errors and rebuilt.encode('utf-8') != frozen['skills/token-optimizer/scripts/measure.py']:
            errors.append('rebuilt pre-move facade differs from the frozen input (a binding moved, or a '
                          'non-lane statement changed)')
        errors += check_positions((scripts / 'measure.py').read_text(encoding='utf-8'), names)
        if not rebuild_errors:
            errors += validate_completed(scripts, names, manifest) + verify_fragments(scripts)
    return errors


def move_fresh(scripts):
    """Move every pending lane symbol in `scripts` (all of them, never a part)."""
    names, manifest = lane_symbols()
    source, pending = classify(scripts, names, manifest)
    fragments, report, expected = rewrite_fragments(source, set(pending))
    facade, provider = scripts / 'measure.py', scripts / '_measure' / (MODULE + '.py')
    before = {facade: facade.read_bytes(), provider: provider.read_bytes(), FRAGMENTS: FRAGMENTS.read_bytes()}
    try:
        IterableLambdaQualifier.used = 0
        with _lf_writes():
            split.move(scripts, pending)
        print(f'iterable-lambda qualification applied to {IterableLambdaQualifier.used} lambda(s)')
        _write(FRAGMENTS, json.dumps(fragments, indent=2, ensure_ascii=False) + '\n')
        _, left = classify(scripts, names, manifest)
        errors = (['still in facade: ' + ', '.join(left)] if left else []) + verify_fragments(scripts, expected)
        errors = errors or validate_completed(scripts, names, manifest)
        compile(provider.read_text(encoding='utf-8'), str(provider), 'exec')
        if errors:
            raise Refusal('\n'.join(errors))
    except BaseException:
        for path, data in before.items():
            path.write_bytes(data)
        raise
    for mutation, edit, symbol, how in report:
        print(f'  fragment {how:9} {symbol}: {mutation} #{edit}')
    print(f'verified {len(expected)} mutations: planted provider == moved mutated facade')
    print(f'moved {len(pending)} definitions into _measure/{MODULE}.py')


def freeze_input(commit):
    """Pin a new U0 head (unmoved lane, empty provider) in this script's frozen-input block."""
    global FROZEN_COMMIT, FROZEN_SHA256
    commit = _git('rev-parse', commit + '^{commit}').stdout.decode().strip()
    digests = {path: _sha(_git('show', f'{commit}:{path}').stdout) for path in FROZEN_SHA256}
    FROZEN_COMMIT, FROZEN_SHA256 = commit, digests
    frozen = frozen_inputs()
    work = SCRATCH / ('freeze-' + commit[:12])
    (work / '_measure').mkdir(parents=True, exist_ok=True)
    (work / 'measure.py').write_bytes(frozen['skills/token-optimizer/scripts/measure.py'])
    (work / '_measure' / (MODULE + '.py')).write_bytes(frozen['skills/token-optimizer/scripts/_measure/savings.py'])
    names, manifest = lane_symbols()
    if len(classify(work, names, manifest)[1]) != len(names):
        raise Refusal(f'{commit} is not an unmoved lane-{LANE} tree')
    me = Path(__file__)
    text = me.read_text(encoding='utf-8')
    block = ("# >>> frozen-input\nFROZEN_COMMIT = " + repr(commit) + "\nFROZEN_SHA256 = {\n"
             + ''.join(f'    {path!r}: {digest!r},\n' for path, digest in digests.items())
             + "}\n# <<< frozen-input")
    _write(me, re.sub(r'# >>> frozen-input\n.*?# <<< frozen-input', lambda _: block, text, count=1, flags=re.S))
    print(f'froze lane-{LANE} input at {commit}')
    return 0


def run(scripts, dry_run=False):
    names, manifest = lane_symbols()
    source, pending = classify(scripts, names, manifest)
    print(f'lane {LANE}: {len(names)} packet symbols, {len(pending)} still in measure.py, '
          f'{len(names) - len(pending)} already in {MODULE}.py')
    if pending and len(pending) != len(names):
        raise Refusal(f'partially moved tree ({len(names) - len(pending)} of {len(names)} moved): '
                      'restore measure.py, savings.py and the lane-3 fragments, then re-run')
    if not pending:
        errors = verify_completed(scripts, names, manifest)
        if errors:
            raise Refusal('\n'.join(errors))
        print('verified: re-running the move on the frozen inputs reproduces measure.py, '
              f'{MODULE}.py and {FRAGMENTS.name} byte for byte')
        print('no-op')
        return 0
    frozen = frozen_inputs()
    if (source.encode('utf-8') != frozen['skills/token-optimizer/scripts/measure.py']
            or _checked_out(FRAGMENTS) != frozen['scripts/mutations/_measure_lane3.json']):
        raise Refusal('unmoved tree differs from the frozen lane input (measure.py or the lane-3 '
                      'fragments); on a new U0 head run --freeze-input first')
    if dry_run:
        print('dry run: nothing written')
        return 0
    move_fresh(scripts)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scripts', type=Path, default=split.SCRIPTS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--freeze-input', metavar='COMMIT',
                        help='pin an unmoved U0 commit as this lane\'s frozen input (rewrites the pinned block)')
    args = parser.parse_args()
    try:
        if args.freeze_input:
            return freeze_input(args.freeze_input)
        return run(args.scripts.resolve(), args.dry_run)
    except Refusal as exc:
        print(f'u3_move refused:\n{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
