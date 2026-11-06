#!/usr/bin/env python3
"""Lane U8: move the dashboard and lifecycle definitions out of measure.py.

Symbols come BY NAME from lane 8's packet in scripts/measure_split_ownership.json.
Each body is cut from measure.py by its AST span, qualified by U0's scope-aware
mover (measure_split.extract_definition) and appended in source order to its
provider; the definition is replaced in place by U0's binding statement. Lane
8's mutation fragments are regenerated in the same pass: each mutation is
planted in the original definition, both versions go through the same mover,
and the differing lines (plus context until the first match falls inside the
symbol) become the new old/new.

There are exactly two accepted states:
  fresh     every lane file byte-equals the frozen input (INPUT_COMMIT,
            pinned by sha256) -> perform the move. BASE_COMMIT's measure.py
            is pinned too, and every lane slice must hash to its packet sha.
  complete  the move is replayed from the frozen inputs in a scratch copy
            under .scratch-u8/, and every file this lane writes must
            byte-equal the replay -> success, nothing written.
Anything else refuses, writing nothing, naming each differing path. Finer
checks (bodies, bindings, positions, providers, fragments) then run only as
diagnostics for the refusal message.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import symtable
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import measure_split as split  # noqa: E402

LANE = '8'
MODULES = ('dashboard', 'lifecycle')
OWNERSHIP = REPO / 'scripts/measure_split_ownership.json'
FRAGMENTS = REPO / 'scripts/mutations/_measure_lane8.json'
SCRATCH = REPO / '.scratch-u8'
INHERITED = tuple(json.loads(OWNERSHIP.read_text(encoding='utf-8'))['lanes'][LANE]['inherited_canaries'])
# The U0 commit this lane is cut from, and the exact bytes this lane reads.
INPUT_COMMIT = '8aa1464db203b78bded10b87f8ecbd2b497f7fad'
INPUTS = {
    'skills/token-optimizer/scripts/measure.py': '82de61c808d96ce8ea7318d332ba2e57c2267cb8d1d8eb20817db7a2255b8fde',
    'skills/token-optimizer/scripts/_measure/dashboard.py': 'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
    'skills/token-optimizer/scripts/_measure/lifecycle.py': 'db266db17988892909e234a477af64f8fcf25c351a322a195c3c739242d7b246',
    'scripts/mutations/_measure_lane8.json': '03b770fb582092f7ce9b25813969016270390207cd31d3c84783fc3b309f247c',
}
# Final BASE (release 5.13.64): the unsplit source every lane slice comes from.
BASE_COMMIT = '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f'
BASE_MEASURE_SHA256 = 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc'


class Refusal(Exception):
    pass


def lane_rows(ownership):
    rows = ownership['lanes'][LANE]['symbols']
    names = [row['name'] for row in rows]
    if len(names) != len(set(names)):
        raise Refusal('lane packet lists a symbol twice')
    if set(ownership['lanes'][LANE]['modules']) != set(MODULES):
        raise Refusal(f"lane packet modules {ownership['lanes'][LANE]['modules']} != {MODULES}")
    bad = [row['name'] for row in rows if row['module'] not in MODULES]
    if bad:
        raise Refusal(f'lane packet assigns symbols outside {MODULES}: {bad}')
    owners = {row['name']: row['module'] for row in split.read_manifest()['symbols']}
    drift = [row['name'] for row in rows if owners.get(row['name']) != row['module']]
    if drift:
        raise Refusal(f'ownership and source manifests disagree on: {drift}')
    return rows


def definitions(tree):
    found = {}
    for node in tree.body:
        if isinstance(node, split.DEFINITION):
            found.setdefault(node.name, []).append(node)
    return found


def facade_bindings(tree):
    bound = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            provider = node.value.args[0]
            # _measure_providers['lifecycle'].name
            bound.setdefault(node.targets[0].id, []).append(provider.value.slice.value)
    return bound


def plan(scripts, rows):
    """Classify every lane symbol as pending or done; refuse anything else."""
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    lines = source.splitlines()
    facade_defs = definitions(tree)
    bound = facade_bindings(tree)
    provider_defs = {m: definitions(ast.parse((scripts / '_measure' / (m + '.py')).read_text(encoding='utf-8')))
                     for m in MODULES}
    lane_names = {row['name'] for row in rows}
    problems, pending, done = [], [], []
    for module, defs in provider_defs.items():
        for name in defs:
            if name not in lane_names:
                problems.append(f'{module}.py has a definition outside lane 8: {name}')
    for row in rows:
        name, module = row['name'], row['module']
        in_facade = facade_defs.get(name, [])
        in_provider = provider_defs[module].get(name, [])
        elsewhere = [m for m in MODULES if m != module and name in provider_defs[m]]
        binding = bound.get(name, [])
        if len(in_facade) == 1 and not in_provider and not elsewhere and not binding:
            node = in_facade[0]
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            digest = hashlib.sha256('\n'.join(lines[first-1:node.end_lineno]).encode()).hexdigest()
            if digest != row['sha256']:
                problems.append(f'{name}: source differs from the frozen packet hash')
            pending.append((first, name, module))
        elif not in_facade and len(in_provider) == 1 and not elsewhere and binding == [module]:
            done.append(name)
        else:
            problems.append(f'{name}: facade defs={len(in_facade)}, {module}.py defs={len(in_provider)}, '
                            f'other module={elsewhere}, bindings={binding}')
    if problems:
        raise Refusal('\n'.join(problems))
    return source, tree, sorted(pending), done


def _positions(text):
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def at(line, column):
        # AST columns count UTF-8 bytes.
        return offsets[line-1] + len(lines[line-1].encode('utf-8')[:column].decode('utf-8'))
    return at


def _line_end(text, position):
    """A definition's span includes the newline ending its last line."""
    return position + 1 if text[position:position+1] == '\n' else position


class Extractor:
    """U0's extract_definition, fed small modules with the facade's global set.

    The mover qualifies a name when it is global in the function's scope and
    bound at module level. A synthetic module that binds exactly the same
    module-level names and holds the unchanged definitions gives the same
    answer as the 64K-line file, without walking it once per symbol.
    """
    def __init__(self, source, tree):
        table = symtable.symtable(source, 'measure.py', 'exec')
        known = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
        known |= {n for statement in ast.walk(tree) if isinstance(statement, ast.Global) for n in statement.names}
        self.header = ''.join(f'{name} = None\n' for name in sorted(known))

    def extract(self, items):
        out = {}
        for offset in range(0, len(items), 12):
            batch = items[offset:offset + 12]
            synthetic = self.header + ''.join('\n\n' + text + '\n' for _, _, text in batch)
            tree, table = ast.parse(synthetic), symtable.symtable(synthetic, 'measure.py', 'exec')
            for name, module, _ in batch:
                out[name] = split.extract_definition(synthetic, name, module, tree=tree, table=table)
        return out


def _span(source, node):
    at = _positions(source)
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return at(first, 0), _line_end(source, at(node.end_lineno, node.end_col_offset))


def requalify_fragments(source, tree, fragments, bodies, texts, extractor):
    """Rewrite each fragment of a symbol moved now into its qualified spelling.

    The mutation is planted in the original definition and both versions go
    through the same mover; the differing whole lines become old/new. Edits of
    one mutation are derived in order, each against the source with the
    earlier edits already planted, exactly as the harness applies them.
    Context lines are added until the first match in the provider (what the
    harness replaces) falls inside the symbol.
    """
    nodes = definitions(tree)
    rewritten = 0
    by_mutation = {}
    for fragment in fragments:
        by_mutation.setdefault(fragment['mutation'], []).append(fragment)
    for edits in by_mutation.values():
        current, providers, spans, qualified_now = source, dict(texts), {}, {}
        for fragment in sorted(edits, key=lambda f: f['edit']):
            name = fragment.get('symbol')
            if name not in bodies:
                continue
            module = bodies[name][0]
            start, end = spans.get(name) or _span(source, nodes[name][0])
            where = current.find(fragment['old'])
            if where < 0 or not (start <= where and where + len(fragment['old']) <= end):
                raise Refusal(f"fragment {fragment['mutation']!r}: first match is outside {name}")
            mutated = current[:where] + fragment['new'] + current[where + len(fragment['old']):]
            end += len(fragment['new']) - len(fragment['old'])
            qualified = qualified_now.get(name, bodies[name][1])
            try:
                planted = extractor.extract([(name, module, mutated[start:end].rstrip('\n'))])[name][0]
            except SyntaxError as exc:
                raise Refusal(f"fragment {fragment['mutation']!r}: planted source does not parse: {exc}") from exc
            if planted == qualified:
                raise Refusal(f"fragment {fragment['mutation']!r}: edit lies outside the moved body")
            prefix = 0
            while prefix < min(len(qualified), len(planted)) and qualified[prefix] == planted[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < min(len(qualified), len(planted)) - prefix
                   and qualified[-1-suffix] == planted[-1-suffix]):
                suffix += 1
            left = qualified.rfind('\n', 0, prefix) + 1
            right = qualified.find('\n', len(qualified) - suffix)
            right = len(qualified) if right < 0 else right + 1
            provider = providers[module]
            home = provider.find(qualified)
            if home < 0 or provider.find(qualified, home + 1) >= 0:
                raise Refusal(f"fragment {fragment['mutation']!r}: cannot locate {name} in {module}.py")
            while True:
                old = qualified[left:right]
                hit = provider.find(old)
                if home <= hit and hit + len(old) <= home + len(qualified):
                    break
                if right < len(qualified):
                    following = qualified.find('\n', right)
                    right = len(qualified) if following < 0 else following + 1
                elif left > 0:
                    left = qualified.rfind('\n', 0, left - 1) + 1
                else:
                    raise Refusal(f"fragment {fragment['mutation']!r}: no unique context inside {name}")
            new = planted[left:len(planted) - (len(qualified) - right)]
            fragment['old'], fragment['new'] = old, new
            providers[module] = provider[:hit] + new + provider[hit + len(old):]
            current, spans[name], qualified_now[name] = mutated, (start, end), planted
            rewritten += 1
    return rewritten


def check_fragments(texts, fragments, rows):
    """Planted in order per mutation, each first match must sit inside its symbol."""
    lane = {row['name']: row['module'] for row in rows}
    planted, current = dict(texts), None
    for fragment in sorted(fragments, key=lambda f: (f['mutation'], f['edit'])):
        if fragment['mutation'] != current:
            planted, current = dict(texts), fragment['mutation']
        name = fragment.get('symbol')
        if name not in lane:
            raise Refusal(f"fragment {fragment['mutation']!r} names a symbol outside lane 8: {name}")
        module = lane[name]
        text = planted[module]
        node = definitions(ast.parse(text)).get(name)
        if not node:
            continue  # still in the facade; nothing moved
        at = _positions(text)
        start, end = at(node[0].lineno, 0), _line_end(text, at(node[0].end_lineno, node[0].end_col_offset))
        where = text.find(fragment['old'])
        if where < 0 or not (start <= where and where + len(fragment['old']) <= end):
            raise Refusal(f"fragment {fragment['mutation']!r} edit {fragment['edit']}: no match inside {name}")
        planted[module] = text[:where] + fragment['new'] + text[where + len(fragment['old']):]


def _inspection():
    """U0's inverse of the qualification, as the source-inspection tests use it."""
    path = REPO / 'tests/helpers/measure_split.py'
    spec = importlib.util.spec_from_file_location('_u8_measure_split_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_done(scripts, rows, done, facade_tree, facade_source):
    """A completed symbol must be exactly the mover's output for its frozen source.

    The provider body is mapped back to its original spelling (U0's inverse)
    and must hash to the frozen packet sha256. That reconstruction is then
    moved again, inside a module with BASE's frozen global set, and the result
    must equal the current body and the current facade binding, defaults and
    decorators included, byte for byte.
    """
    if not done:
        return 0
    inverse = _inspection()
    manifest = split.read_manifest()
    declared = set()
    for symbol in manifest['symbols']:
        for declaration in symbol.get('global_declarations', []):
            declared |= {n.id if isinstance(n, ast.Name) else n for statement in ast.parse(declaration['text'].strip()).body
                         for n in getattr(statement, 'names', [])}
    header = ''.join(f'{name} = None\n' for name in sorted(set(manifest['bindings']) | declared))
    by_name = {row['name']: row for row in rows}
    bindings = {n.targets[0].id: ast.get_source_segment(facade_source, n) for n in facade_tree.body
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value.func) == '_measure_compat.bind'}
    problems, checks = [], []
    for module in MODULES:
        text = (scripts / '_measure' / (module + '.py')).read_text(encoding='utf-8')
        lines = text.splitlines()
        for node in ast.parse(text).body:
            if not isinstance(node, split.DEFINITION) or node.name not in done:
                continue
            row = by_name[node.name]
            body = '\n'.join(lines[node.lineno-1:node.end_lineno])
            plain = inverse._inspection_text(body)
            first_body = ast.parse(plain).body[0].body[0].lineno - 1
            restored = (row['signature'] + '\n' + '\n'.join(plain.splitlines()[first_body:])).splitlines(keepends=True)
            for declaration in row.get('global_declarations', []):
                index = declaration['line']
                if '\n' not in declaration['text'] and index < len(restored) and restored[index].strip() == 'pass':
                    indent = len(restored[index]) - len(restored[index].lstrip())
                    restored[index] = ' ' * indent + declaration['text'] + '\n'
            original = ''.join(restored)
            if hashlib.sha256(original.encode()).hexdigest() != row['sha256']:
                problems.append(f'{node.name}: moved body drifted from its frozen source')
                continue
            checks.append((node.name, module, original, body))
    # U0's extractor walks the whole module per call; small batches keep this fast.
    for offset in range(0, len(checks), 12):
        batch = checks[offset:offset + 12]
        synthetic = header + ''.join('\n\n' + original + '\n' for _, _, original, _ in batch)
        tree, table = ast.parse(synthetic), symtable.symtable(synthetic, 'measure.py', 'exec')
        problems.extend(_compare_batch(synthetic, tree, table, batch, bindings))
    if len(checks) + sum('drifted from its frozen source' in p for p in problems) != len(done):
        problems.append('completed symbols missing from their provider modules')
    if problems:
        raise Refusal('\n'.join(problems))
    return len(checks)


def _compare_batch(synthetic, tree, table, batch, bindings):
    problems = []
    for name, module, _, body in batch:
        moved, binding = split.extract_definition(synthetic, name, module, tree=tree, table=table)
        if moved.rstrip('\n') != body:
            problems.append(f'{name}: moved body is not the mover output for its frozen source')
        if bindings.get(name) != binding:
            problems.append(f'{name}: facade binding (defaults/decorators) drifted')
    return problems


def _identities(entries):
    """Number repeated identities so duplicates stay distinguishable."""
    seen, out = {}, []
    for identity in entries:
        seen[identity] = seen.get(identity, 0) + 1
        out.append((identity, seen[identity]))
    return out


def frozen_sequence():
    manifest = split.read_manifest()
    by_end = {s['end']: s['name'] for s in manifest['symbols']}
    entries = []
    for statement in manifest['statements']:
        if statement['kind'] in ('FunctionDef', 'AsyncFunctionDef', 'ClassDef'):
            entries.append(('def', by_end[statement['end']]))
        else:
            entries.append(('stmt', statement['sha256']))
    return _identities(entries)


def facade_sequence(source, tree):
    lines = source.splitlines()
    entries = []
    for node in tree.body:
        if isinstance(node, split.DEFINITION):
            entries.append(('def', node.name))
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
              and ast.unparse(node.value.func) == '_measure_compat.bind'):
            entries.append(('def', node.targets[0].id))
        else:
            text = '\n'.join(lines[node.lineno-1:node.end_lineno])
            entries.append(('stmt', hashlib.sha256(text.encode()).hexdigest()))
    return _identities(entries)


def verify_positions(source, tree, rows):
    """Each lane binding keeps its frozen neighbours, not just its lane order.

    Neighbours are the nearest statements present in both the frozen BASE
    sequence and this facade, so other lanes' replacements and U0's own
    bootstrap edits are tolerated without absolute line numbers.
    """
    lane = {('def', row['name']) for row in rows}
    frozen, current = frozen_sequence(), facade_sequence(source, tree)
    common = ({key for key in frozen if key[0] not in lane} & {key for key in current if key[0] not in lane})

    def anchors(sequence):
        result, previous = {}, None
        pending = []
        for key in sequence:
            if key[0] in lane:
                pending.append(key)
            elif key in common:
                for owned in pending:
                    result[owned] = [previous, key]
                pending, previous = [], key
        for owned in pending:
            result[owned] = [previous, None]
        return result
    expected, actual = anchors(frozen), anchors(current)
    order = [key for key in frozen if key[0] in lane]
    problems = [f'{key[0][1]}: binding moved across frozen initialization (neighbours {actual.get(key)} != {expected[key]})'
                for key in order if actual.get(key) != expected[key]]
    if [key for key in current if key[0] in lane] != order:
        problems.append('lane bindings are out of their frozen order')
    if problems:
        raise Refusal('\n'.join(problems))
    return len(order)


def verify_providers(texts, rows):
    """Diagnostic: a provider is its header plus exactly its owned definitions."""
    order = {}
    # U0's canaries were appended first; this lane appends the rest in source order.
    for row in sorted(rows, key=lambda r: (r['name'] not in INHERITED, r['start'])):
        order.setdefault(row['module'], []).append(row['name'])
    problems = []
    for module, text in texts.items():
        tree = ast.parse(text)
        body = tree.body
        header = [ast.unparse(n) for n in body[:3]]
        if header != [ast.unparse(n) for n in ast.parse(split.HEADER).body]:
            problems.append(f'{module}.py: header differs from U0\'s provider header')
        extra = [n.lineno for n in body[3:] if not isinstance(n, split.DEFINITION)]
        if extra:
            problems.append(f'{module}.py: executable statements outside definitions at lines {extra}')
        names = [n.name for n in body[3:] if isinstance(n, split.DEFINITION)]
        if sorted(names) != sorted(order[module]) or len(names) != len(set(names)):
            problems.append(f'{module}.py: definitions differ from the owned set')
        elif names != order[module]:
            problems.append(f'{module}.py: definitions are out of source order')
    if problems:
        raise Refusal('\n'.join(problems))
    return sum(len(v) for v in order.values())


def _apply(scripts, fragments_path, rows, *, write):
    """The move itself, from a fresh state."""
    source, tree, pending, done = plan(scripts, rows)
    if set(done) != set(INHERITED):
        raise Refusal(f'not a fresh state: moved {sorted(done)}, expected only U0 canaries {INHERITED}')
    fragments = json.loads(fragments_path.read_text(encoding='utf-8'))
    report = {'lane': 8, 'symbols': len(rows), 'moved_now': len(pending), 'lines_removed': 0,
              'fragments': len(fragments)}
    providers = {m: scripts / '_measure' / (m + '.py') for m in MODULES}
    texts = {m: path.read_text(encoding='utf-8') for m, path in providers.items()}
    extractor = Extractor(source, tree)
    nodes = definitions(tree)
    lines = source.splitlines(keepends=True)
    items = [(name, module, ''.join(lines[first-1:nodes[name][0].end_lineno]).rstrip('\n'))
             for first, name, module in pending]
    owner = {name: module for name, module, _ in items}
    bodies = {name: (owner[name], moved, binding) for name, (moved, binding) in extractor.extract(items).items()}
    for _, name, module in pending:  # original order inside each module
        texts[module] = texts[module].rstrip() + '\n\n\n' + bodies[name][1]
    for first, name, _ in sorted(pending, reverse=True):
        report['lines_removed'] += nodes[name][0].end_lineno - first + 1
        lines[first-1:nodes[name][0].end_lineno] = [bodies[name][2] + '\n']
    facade = ''.join(lines)
    compile(facade, str(scripts / 'measure.py'), 'exec', dont_inherit=True)
    verify_positions(facade, ast.parse(facade), rows)
    for module in MODULES:
        compile(texts[module], str(providers[module]), 'exec', dont_inherit=True)
    verify_providers(texts, rows)
    report['fragments_rewritten'] = requalify_fragments(source, tree, fragments, bodies, texts, extractor)
    check_fragments(texts, fragments, rows)
    if write:
        # Bytes, not text mode: the repo is LF (.gitattributes) on every OS,
        # and the replay compares raw bytes.
        fragments_path.write_bytes((json.dumps(fragments, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
        for module in MODULES:
            providers[module].write_bytes(texts[module].encode('utf-8'))
        (scripts / 'measure.py').write_bytes(facade.encode('utf-8'))
    return report


def _have_commit(sha):
    return subprocess.run(['git', 'cat-file', '-e', sha + '^{commit}'], cwd=REPO, capture_output=True).returncode == 0


def _check_lane_slices(base_source):
    """Every lane definition in the packet is a byte slice of the pinned BASE."""
    lines = base_source.splitlines()
    rows = json.loads(OWNERSHIP.read_text(encoding='utf-8'))['lanes'][LANE]['symbols']
    bad = [row['name'] for row in rows
           if hashlib.sha256('\n'.join(lines[row['start']-1:row['end']]).encode()).hexdigest() != row['sha256']]
    if bad:
        raise Refusal(f'lane packet slices differ from pinned BASE: {bad[:10]}')


def frozen_inputs():
    """The exact bytes this lane starts from: pinned commit + sha256, never the facade.

    A shallow checkout fetches only that commit. If it still cannot be read,
    or any byte differs from its pin, refuse: nothing is derived from the
    working tree to fill the gap.
    """
    for commit in (INPUT_COMMIT, BASE_COMMIT):
        if not _have_commit(commit):
            subprocess.run(['git', 'fetch', '--no-tags', '--depth=1', 'origin', commit],
                           cwd=REPO, capture_output=True, timeout=600)
    base = subprocess.run(['git', 'show', f'{BASE_COMMIT}:skills/token-optimizer/scripts/measure.py'],
                          cwd=REPO, capture_output=True)
    if base.returncode:
        raise Refusal(f'frozen inputs unavailable ({BASE_COMMIT[:12]}:measure.py); refusing')
    digest = hashlib.sha256(base.stdout).hexdigest()
    if digest != BASE_MEASURE_SHA256 or digest != split.read_manifest()['source_sha256']:
        raise Refusal('frozen BASE measure.py does not match its pinned sha256 and the source manifest')
    _check_lane_slices(base.stdout.decode('utf-8'))
    data = {}
    for relative, digest in INPUTS.items():
        result = subprocess.run(['git', 'show', f'{INPUT_COMMIT}:{relative}'], cwd=REPO, capture_output=True)
        if result.returncode:
            raise Refusal(f'frozen inputs unavailable ({INPUT_COMMIT[:12]}:{relative}); '
                          'cannot prove the completed state, refusing')
        if hashlib.sha256(result.stdout).hexdigest() != digest:
            raise Refusal(f'frozen input {relative} does not match its pinned sha256')
        data[relative] = result.stdout
    return data


def _lane_files(scripts, fragments_path):
    """Working paths, keyed like INPUTS."""
    keys = list(INPUTS)
    return {keys[0]: scripts / 'measure.py', keys[1]: scripts / '_measure/dashboard.py',
            keys[2]: scripts / '_measure/lifecycle.py', keys[3]: fragments_path}


def replay(scripts, fragments_path, rows, inputs):
    """Re-run the move on the frozen inputs; return the working paths that differ."""
    SCRATCH.mkdir(exist_ok=True)
    work = SCRATCH / f'replay-{uuid.uuid4().hex}'
    try:
        copy = _lane_files(work / 'scripts', work / 'fragments.json')
        for relative, path in copy.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(inputs[relative])
        _apply(work / 'scripts', copy[list(INPUTS)[3]], rows, write=True)
        working = _lane_files(scripts, fragments_path)
        return [str(working[relative]) for relative in INPUTS
                if not working[relative].is_file() or working[relative].read_bytes() != copy[relative].read_bytes()]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def diagnose(scripts, fragments_path, rows):
    """The finer checks, run only to explain a refusal."""
    notes = []
    try:
        source, tree, pending, done = plan(scripts, rows)
    except Refusal as exc:
        return [str(exc)]
    texts = {m: (scripts / '_measure' / (m + '.py')).read_text(encoding='utf-8') for m in MODULES}
    checks = (lambda: verify_done(scripts, rows, set(done), tree, source),
              lambda: verify_positions(source, tree, rows),
              lambda: verify_providers(texts, rows),
              lambda: check_fragments(texts, json.loads(fragments_path.read_text(encoding='utf-8')), rows))
    for check in checks:
        try:
            check()
        except Refusal as exc:
            notes.append(str(exc))
    return notes


def move(scripts, *, fragments=None, dry_run=False):
    scripts = Path(scripts)
    fragments = Path(fragments or FRAGMENTS)
    rows = lane_rows(json.loads(OWNERSHIP.read_text(encoding='utf-8')))
    inputs = frozen_inputs()
    working = _lane_files(scripts, fragments)
    fresh = all(working[k].is_file() and working[k].read_bytes() == inputs[k] for k in INPUTS)
    if fresh:
        report = _apply(scripts, fragments, rows, write=not dry_run)
        report['state'] = 'fresh'
        return report
    try:
        differing = replay(scripts, fragments, rows, inputs)
    except Refusal as exc:
        raise Refusal(f'replay of the frozen inputs failed: {exc}') from exc
    if differing:
        lines = ['completed state differs from the replay of the frozen inputs:']
        lines += [f'  differs: {path}' for path in differing]
        lines += ['diagnostics:'] + ['  ' + note for note in diagnose(scripts, fragments, rows)
                                     for note in note.splitlines()]
        raise Refusal('\n'.join(lines))
    return {'lane': 8, 'symbols': len(rows), 'state': 'complete', 'moved_now': 0,
            'replayed_files_identical': len(INPUTS), 'input_commit': INPUT_COMMIT}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scripts', type=Path, default=split.SCRIPTS)
    parser.add_argument('--fragments', type=Path, default=FRAGMENTS)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        report = move(args.scripts, fragments=args.fragments, dry_run=args.dry_run)
    except Refusal as exc:
        print('refused, nothing written:\n' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
