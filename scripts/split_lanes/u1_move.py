#!/usr/bin/env python3
"""Lane U1: move the runtime, pricing and trends definitions out of measure.py.

Deterministic and re-runnable against any measure.py that still carries the
frozen lane-1 packet (scripts/measure_split_ownership.json, lane "1"):

* every lane symbol is located BY NAME with ast, never by line number;
* the moved text is U0's scope-aware qualification (measure_split.extract_definition),
  so bodies keep resolving old globals through the live facade;
* each definition is replaced in place by U0's binding statement;
* the lane-1 mutation fragments are re-derived for the qualified text, and
  each one is proven to plant exactly the qualified form of the original mutation.

"Already applied" means exactly one thing: the move is replayed on the lane's
inputs (the files it writes, at the U0 commit it was cut from) in
.scratch-u1/replay/, and every written file - measure.py, the three providers,
the lane-1 mutation fragments - is byte-identical in the working tree. An
untouched tree receives the replay's bytes; any other state refuses, naming the
differing files and the replay to diff against. A missing, duplicated or
half-moved symbol refuses before any file is written.

The finer checks below run inside the replay and as refusal diagnostics.
Success is only reported against the frozen BASE (manifest `base`, checked
against `source_sha256` and every per-symbol `sha256`): each body still in the
facade must equal its frozen text, and each completed body and its whole
binding (defaults, decorators) must equal the qualification of that frozen
text byte for byte, and every lane definition or binding must keep its frozen
position relative to all surrounding statements. Any drift refuses.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import shutil
import subprocess
import symtable
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import measure_split as split  # noqa: E402  (U0's reviewed mover)

LANE = '1'
OWNERSHIP = REPO / 'scripts/measure_split_ownership.json'
FRAGMENTS = REPO / 'scripts/mutations/_measure_lane1.json'
# The pre-import bytecode redirect stays in the facade (plan section 2, bootstrap allowlist).
BOOTSTRAP = frozenset({'_redirect_bytecode_out_of_repo'})
BIND = '_measure_compat.bind'


class Refused(Exception):
    """The input is not a lane-1 tree this script can move safely."""


class _LaneQualifier(split.Qualifier):
    """U0's qualifier, except a lambda that reads no facade global stays verbatim.

    U0 wraps every lambda body in `((_measure_facade := _compat.facade()), body)[1]`.
    Python rejects that walrus inside a comprehension iterable, e.g.
    `for k, v in sorted(mix.items(), key=lambda kv: -kv[1])`. When the body
    has no qualified name the wrapper only binds an unused local, so leaving the
    original lambda text is the exact original behaviour. A lambda that does
    read a facade global keeps U0's wrapper unchanged.
    """
    def visit_Lambda(self, node):
        for value in node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        before = len(self.edits)
        self.scoped(node, 'lambda', [node.body])
        if len(self.edits) == before:
            return
        left = self.offset(node.body.lineno, node.body.col_offset)
        right = self.offset(node.body.end_lineno, node.body.end_col_offset)
        self.edits.extend(((left, left, '((_measure_facade := _compat.facade()), '),
                           (right, right, ')[1]')))


# extract_definition looks Qualifier up at call time; only this process is affected.
split.Qualifier = _LaneQualifier


def lane_packet(ownership=OWNERSHIP):
    packet = json.loads(Path(ownership).read_text(encoding='utf-8'))['lanes'][LANE]
    owners = {row['name']: row['module'] for row in split.read_manifest()['symbols']}
    symbols = []
    for row in packet['symbols']:
        if owners.get(row['name']) != row['module'] or split.LANES[row['module']] != int(LANE):
            raise Refused(f"{row['name']}: ownership and source manifest disagree")
        if row['name'] not in BOOTSTRAP:
            symbols.append((row['name'], row['module']))
    if len({name for name, _ in symbols}) != len(symbols):
        raise Refused('lane packet lists a symbol twice')
    return packet, symbols


def _definitions(tree):
    found = {}
    for node in tree.body:
        if isinstance(node, split.DEFINITION):
            found.setdefault(node.name, []).append(node)
    return found


def _bindings(tree):
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == BIND):
            found.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    return found


def classify(scripts, symbols):
    """Return (to_move, already_moved); refuse anything ambiguous."""
    scripts = Path(scripts)
    facade_tree = ast.parse((scripts / 'measure.py').read_text(encoding='utf-8'))
    defs, binds = _definitions(facade_tree), _bindings(facade_tree)
    providers = {}
    for module in sorted({module for _, module in symbols}):
        providers[module] = _definitions(ast.parse((scripts / '_measure' / (module + '.py')).read_text(encoding='utf-8')))
    to_move, moved, errors = [], [], []
    for name, module in symbols:
        in_facade = len(defs.get(name, []))
        in_provider = len(providers[module].get(name, []))
        binding = binds.get(name, [])
        expected = f"_measure_providers['{module}'].{name}"
        if in_facade == 1 and in_provider == 0 and not binding:
            to_move.append((name, module))
        elif in_facade == 0 and in_provider == 1 and binding == [expected]:
            moved.append((name, module))
        elif in_facade == 0 and in_provider == 0:
            errors.append(f'{name}: missing (no body in measure.py or _measure/{module}.py)')
        else:
            errors.append(f'{name}: inconsistent (facade bodies={in_facade}, provider bodies={in_provider}, bindings={binding})')
    if errors:
        raise Refused('\n'.join(errors))
    return to_move, moved


def _span(source, name):
    """Character span of a top-level definition, decorators included."""
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, split.DEFINITION) and n.name == name)
    lines = source.splitlines(keepends=True)
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return sum(map(len, lines[:first - 1])), sum(map(len, lines[:node.end_lineno]))


def _same_line_tail(source, node):
    """A comment after the last statement, on its line, belongs to the moved body.

    U0's slice stops at the statement's end column and the facade line is
    replaced whole, so without this the comment would vanish from both files.
    """
    line = source.splitlines(keepends=True)[node.end_lineno - 1]
    rest = line.encode('utf-8')[node.end_col_offset:].decode('utf-8').rstrip('\r\n')
    if not rest.strip():
        return ''
    if not rest.lstrip().startswith('#'):
        raise Refused(f'{node.name}: code follows the definition on its last line')
    return rest


def _map_span(original, qualified, start, end):
    """Map [start, end) of the unqualified body onto its qualified text.

    Qualification inserts prefixes in front of names, so an insertion at the
    start belongs to the fragment and one at the end belongs to what follows.
    """
    left = right = None
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, qualified, autojunk=False).get_opcodes():
        if left is None and (i1 <= start < i2 or (tag == 'insert' and i1 == start)):
            left = j1 if tag != 'equal' else j1 + start - i1
        if tag == 'equal' and i1 < end <= i2:
            right = j1 + end - i1
        elif tag != 'equal' and i1 < end <= i2:
            right = j2
    if left is None or right is None:
        raise Refused('fragment span could not be mapped onto qualified text')
    return left, right


def requalify_fragments(source, fragments, to_move, provider_texts):
    """Re-derive old/new for fragments whose symbol moves in this run."""
    modules = dict(to_move)
    out = []
    for fragment in fragments:
        name = fragment.get('symbol')
        if name not in modules:
            out.append(fragment)
            continue
        old, new = fragment['old'], fragment['new']
        start, end = _span(source, name)
        at = source.find(old)
        if at < 0 or not (start <= at and at + len(old) <= end):
            raise Refused(f"{fragment['mutation']}: first match of its fragment is not inside {name}")
        mutated = source[:at] + new + source[at + len(old):]
        module = modules[name]
        tree = ast.parse(source)
        q_orig, _ = split.extract_definition(source, name, module, tree=tree,
                                             table=symtable.symtable(source, 'measure.py', 'exec'))
        q_mut, _ = split.extract_definition(mutated, name, module, tree=ast.parse(mutated),
                                            table=symtable.symtable(mutated, 'measure.py', 'exec'))
        body = source[start:end]
        a, b = _map_span(body, q_orig, at - start, at - start + len(old))
        suffix = len(q_orig) - b
        if q_orig[:a] != q_mut[:a] or q_orig[b:] != q_mut[len(q_mut) - suffix:]:
            raise Refused(f"{fragment['mutation']}: mutation changes qualification outside its fragment")
        q_old, q_new = q_orig[a:b], q_mut[a:len(q_mut) - suffix]
        if q_orig.replace(q_old, q_new, 1) != q_mut:
            raise Refused(f"{fragment['mutation']}: qualified fragment does not reproduce the mutation")
        text = provider_texts[module]
        where = text.find(q_old)
        body_at = text.find(q_orig.rstrip('\n'))
        if where < 0 or body_at < 0 or not (body_at <= where < body_at + len(q_orig)):
            raise Refused(f"{fragment['mutation']}: first match in _measure/{module}.py is outside {name}")
        out.append({**fragment, 'old': q_old, 'new': q_new})
    return out


def check_bootstrap(scripts):
    """The retained bootstrap must be a facade body that precedes its early call."""
    scripts = Path(scripts)
    tree = ast.parse((scripts / 'measure.py').read_text(encoding='utf-8'))
    for name in BOOTSTRAP:
        bodies = [n for n in tree.body if isinstance(n, split.DEFINITION) and n.name == name]
        # The early call sits in a module-level `if __name__ in ("__main__",):` block.
        calls = [c for statement in tree.body if not isinstance(statement, split.DEFINITION)
                 for c in ast.walk(statement)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name]
        if len(bodies) != 1 or not calls or bodies[0].end_lineno >= calls[0].lineno:
            raise Refused(f'{name}: must stay in measure.py, defined once before its early call')
        for module in LANE_MODULES:
            if name in _definitions(ast.parse((scripts / '_measure' / (module + '.py')).read_text(encoding='utf-8'))):
                raise Refused(f'{name}: retained bootstrap found in _measure/{module}.py')


LANE_MODULES = ('runtime', 'pricing', 'trends')


FROZEN_PATH = 'skills/token-optimizer/scripts/measure.py'
# Pinned here, independent of every file the lane or U0 can edit. The re-cut on
# U0-FINAL updates these two lines deliberately; nothing derives them at runtime.
FROZEN_BASE_COMMIT = '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f'
FROZEN_BASE_SHA256 = 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc'
# The only top-level statements U0 inserted into the facade before lanes start.
U0_SCAFFOLD = frozenset({
    'from _measure import _compat as _measure_compat\n',
    '_measure_providers = _measure_compat.prepare(sys.modules[__name__])\n',
})
_EXPECTED = {}


def _ensure_commit(commit):
    """Have `commit` locally, fetching it by SHA (depth 1) into a shallow clone if needed."""
    if subprocess.run(['git', '-C', str(REPO), 'cat-file', '-e', commit + '^{commit}'], capture_output=True).returncode == 0:
        return
    args = ['fetch', '--no-tags', '--quiet', '--depth=1', 'origin', commit]
    proc = subprocess.run(['git', '-C', str(REPO), *args], capture_output=True, timeout=300)
    if proc.returncode or subprocess.run(['git', '-C', str(REPO), 'cat-file', '-e', commit + '^{commit}'],
                                         capture_output=True).returncode:
        raise Refused(f"frozen input {commit[:12]} is unavailable (git {' '.join(args)}: "
                      f"{proc.stderr.decode('utf-8', 'replace').strip() or 'not found'}); refusing, nothing written")


def frozen_source(path=None):
    """The pinned unsplit BASE measure.py, verified by its pinned sha256."""
    manifest = split.read_manifest()
    if (manifest['base'], manifest['source_sha256']) != (FROZEN_BASE_COMMIT, FROZEN_BASE_SHA256):
        raise Refused('the source manifest names a different frozen BASE than this script pins')
    if path is not None:
        data = Path(path).read_bytes()
    else:
        _ensure_commit(FROZEN_BASE_COMMIT)
        proc = subprocess.run(['git', '-C', str(REPO), 'show', f'{FROZEN_BASE_COMMIT}:{FROZEN_PATH}'], capture_output=True)
        if proc.returncode:
            raise Refused(f'frozen BASE {FROZEN_BASE_COMMIT[:12]} has no {FROZEN_PATH}; refusing, nothing written')
        data = proc.stdout
    if hashlib.sha256(data).hexdigest() != FROZEN_BASE_SHA256:
        raise Refused('frozen source does not match the pinned sha256; refusing, nothing written')
    return data.decode('utf-8')


def check_statements(facade_source, frozen):
    """The facade is the frozen BASE except for definitions moved in place.

    Every top-level statement must match frozen BASE exactly and in order:
    definitions are compared by name (body or binding, any lane), everything else
    by exact text, and only U0's scaffolding may be inserted. Any definition still
    a body must equal its frozen per-symbol hash. A changed constant, a dropped or
    added statement, or a reordering refuses.
    """
    manifest = split.read_manifest()
    names = {row['name'] for row in manifest['symbols']}
    hashes = {row['name']: row['sha256'] for row in manifest['symbols']}
    frozen_keys = [key[:2] for key in _statement_keys(frozen, names)]
    lines = facade_source.splitlines(keepends=True)
    current, errors = [], []
    for key, node in zip(_statement_keys(facade_source, names), ast.parse(facade_source).body):
        text = ''.join(lines[node.lineno - 1:node.end_lineno])
        if key[0] == 'stmt' and text in U0_SCAFFOLD:
            continue
        if isinstance(node, split.DEFINITION) and key[0] == 'def':
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            body = '\n'.join(facade_source.splitlines()[first - 1:node.end_lineno])
            if hashlib.sha256(body.encode()).hexdigest() != hashes[node.name]:
                errors.append(f'{node.name}: facade body differs from its frozen hash (line {node.lineno})')
        current.append((key[:2], node.lineno))
    if [key for key, _ in current] != frozen_keys:
        for index, ((key, line), expected) in enumerate(zip(current, frozen_keys)):
            if key != expected:
                errors.append(f'facade statement at line {line} is not the frozen BASE statement #{index + 1} '
                              '(changed, added, removed or reordered)')
                break
        else:
            errors.append(f'facade has {len(current)} statements outside U0 scaffolding, frozen BASE has {len(frozen_keys)}')
    if errors:
        raise Refused('\n'.join(errors))


def _frozen_texts(frozen, symbols):
    """name -> exact frozen definition text, each checked against its manifest sha256."""
    hashes = {row['name']: row['sha256'] for row in split.read_manifest()['symbols']}
    lines = frozen.splitlines()
    nodes = _definitions(ast.parse(frozen))
    out = {}
    for name, _ in symbols:
        if len(nodes.get(name, [])) != 1:
            raise Refused(f'{name}: not exactly one body in the frozen source')
        node = nodes[name][0]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        text = '\n'.join(lines[first - 1:node.end_lineno])
        if hashlib.sha256(text.encode()).hexdigest() != hashes[name]:
            raise Refused(f'{name}: frozen text does not match its manifest sha256')
        out[name] = text
    return out


def expected_state(frozen, symbols):
    """name -> (provider body text, facade binding) qualified from the frozen source."""
    key = hashlib.sha256(frozen.encode()).hexdigest()
    if key not in _EXPECTED:
        _frozen_texts(frozen, symbols)
        tree = ast.parse(frozen)
        table = symtable.symtable(frozen, 'measure.py', 'exec')
        nodes = {n.name: n for n in tree.body if isinstance(n, split.DEFINITION)}
        expected = {}
        for name, module in symbols:
            text, binding = split.extract_definition(frozen, name, module, tree=tree, table=table)
            expected[name] = ((text[:-1] + _same_line_tail(frozen, nodes[name])).rstrip('\n'), binding)
        _EXPECTED[key] = expected
    return _EXPECTED[key]


def _full_lines(source, node):
    lines = source.splitlines(keepends=True)
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return ''.join(lines[first - 1:node.end_lineno]).rstrip('\n')


def _statement_keys(source, names):
    """Top-level statement identities that survive in-place moves by any lane.

    A manifest definition is ('def', name) whether it is still a body or already
    a binding; every other statement is keyed by its exact text.
    """
    out = []
    lines = source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        if isinstance(node, split.DEFINITION) and node.name in names:
            out.append(('def', node.name, node.lineno))
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == BIND
              and node.targets[0].id in names):
            out.append(('def', node.targets[0].id, node.lineno))
        else:
            text = ''.join(lines[node.lineno - 1:node.end_lineno])
            out.append(('stmt', hashlib.sha256(text.encode()).hexdigest(), node.lineno))
    return out


def check_positions(facade_source, frozen, symbols):
    """Each lane symbol keeps its frozen place among the surrounding statements.

    Anchors are statements that occur exactly once in both the frozen BASE and the
    current facade (constants, state initialization, other lanes' definitions or
    bindings). Inserted scaffolding is tolerated. A lane binding that crosses any
    anchor, e.g. moves above the initialization its defaults or body rely on,
    refuses: it would capture or see different state at import.
    """
    names = {row['name'] for row in split.read_manifest()['symbols']}
    frozen_keys, current_keys = _statement_keys(frozen, names), _statement_keys(facade_source, names)
    count_f, count_c = {}, {}
    for key in frozen_keys:
        count_f[key[:2]] = count_f.get(key[:2], 0) + 1
    for key in current_keys:
        count_c[key[:2]] = count_c.get(key[:2], 0) + 1
    shared = [key[:2] for key in frozen_keys if count_f[key[:2]] == 1 and count_c.get(key[:2]) == 1]
    index_f = {key[:2]: i for i, key in enumerate(frozen_keys)}
    index_c = {key[:2]: i for i, key in enumerate(current_keys)}
    frozen_line = {key[:2]: key[2] for key in frozen_keys}
    errors = []
    for name, _ in symbols:
        own = ('def', name)
        if own not in shared:
            errors.append(f'{name}: not exactly one definition/binding in both the frozen BASE and the facade')
            continue
        f, c = index_f[own], index_c[own]
        crossed = [a for a in shared if a != own and (index_f[a] < f) != (index_c[a] < c)]
        if crossed:
            errors.append(f'{name}: binding moved across {len(crossed)} surrounding statement(s), '
                          f'e.g. frozen line {frozen_line[crossed[0]]}')
    if errors:
        raise Refused('\n'.join(errors))


def validate(scripts, frozen, symbols):
    """Refuse unless every symbol is exactly frozen (unmoved) or exactly its qualified move."""
    scripts = Path(scripts)
    texts = _frozen_texts(frozen, symbols)
    expected = expected_state(frozen, symbols)
    to_move, moved = classify(scripts, symbols)
    facade_source = (scripts / 'measure.py').read_text(encoding='utf-8')
    facade = ast.parse(facade_source)
    defs = _definitions(facade)
    errors = []
    for check in (check_statements, lambda source, base: check_positions(source, base, symbols)):
        try:
            check(facade_source, frozen)
        except Refused as exc:
            errors.append(str(exc))
    for name, _ in to_move:
        if _full_lines(facade_source, defs[name][0]) != texts[name]:
            errors.append(f'{name}: facade body drifted from the frozen BASE')
    bindings = {n.targets[0].id: ast.get_source_segment(facade_source, n) for n in facade.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == BIND}
    providers = {}
    for name, module in moved:
        if module not in providers:
            text = (scripts / '_measure' / (module + '.py')).read_text(encoding='utf-8')
            providers[module] = (text, _definitions(ast.parse(text)))
        text, nodes = providers[module]
        body, binding = expected[name]
        if _full_lines(text, nodes[name][0]) != body:
            errors.append(f'{name}: moved body drifted from the qualified frozen BASE')
        if bindings.get(name) != binding:
            errors.append(f'{name}: facade binding drifted (defaults/decorators/target): {bindings.get(name)!r}')
    if errors:
        raise Refused('\n'.join(errors))
    return to_move, moved


def _move_in_place(scripts, fragments_path, frozen, log):
    """The move itself. Only ever runs on a replay copy of the lane's inputs."""
    scripts = Path(scripts)
    check_bootstrap(scripts)
    _, symbols = lane_packet()
    to_move, moved = validate(scripts, frozen, symbols)
    log(f'lane {LANE}: {len(symbols)} owned symbols, {len(moved)} already moved, {len(to_move)} to move')
    if not to_move:
        raise Refused('lane inputs already contain the moved state; they must be the unmoved U0 tree')
    facade_path = scripts / 'measure.py'
    source = facade_path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    table = symtable.symtable(source, 'measure.py', 'exec')
    nodes = {n.name: n for n in tree.body if isinstance(n, split.DEFINITION)}
    # Source order makes the provider layout independent of packet order.
    ordered = sorted(to_move, key=lambda item: nodes[item[0]].lineno)
    appended = {module: [] for _, module in ordered}
    changes = []
    for index, (name, module) in enumerate(ordered, 1):
        node = nodes[name]
        text, binding = split.extract_definition(source, name, module, tree=tree, table=table)
        text = text[:-1] + _same_line_tail(source, node) + '\n'
        appended[module].append(text)
        changes.append((min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno, binding))
        if index % 25 == 0:
            log(f'  qualified {index}/{len(ordered)}')
    lines = source.splitlines(keepends=True)
    for first, last, binding in sorted(changes, reverse=True):
        lines[first - 1:last] = [binding + '\n']
    facade = ''.join(lines)
    provider_texts = {}
    for module, texts in appended.items():
        path = scripts / '_measure' / (module + '.py')
        existing = path.read_text(encoding='utf-8')
        provider_texts[module] = existing.rstrip() + ''.join('\n\n\n' + text.rstrip('\n') for text in texts) + '\n'
    compile(facade, str(facade_path), 'exec', dont_inherit=True)
    for module, text in provider_texts.items():
        compile(text, str(scripts / '_measure' / (module + '.py')), 'exec', dont_inherit=True)
    fragments = json.loads(Path(fragments_path).read_text(encoding='utf-8'))
    rewritten = requalify_fragments(source, fragments, to_move, provider_texts)
    for module, text in provider_texts.items():
        (scripts / '_measure' / (module + '.py')).write_text(text, encoding='utf-8', newline='\n')
    facade_path.write_text(facade, encoding='utf-8', newline='\n')
    if rewritten != fragments:
        Path(fragments_path).write_text(json.dumps(rewritten, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    after, done = validate(scripts, frozen, symbols)
    if after or len(done) != len(symbols):
        raise Refused('post-condition failed: some lane symbols did not move')
    changed = sum(1 for a, b in zip(fragments, rewritten) if a != b)
    log(f'moved {len(to_move)} definitions; requalified {changed} mutation fragments')
    return {'moved_now': len(to_move), 'already_moved': len(moved), 'fragments_requalified': changed}


WRITTEN = ('skills/token-optimizer/scripts/measure.py',
           *(f'skills/token-optimizer/scripts/_measure/{module}.py' for module in LANE_MODULES),
           'scripts/mutations/_measure_lane1.json')
REPLAY_ROOT = REPO / '.scratch-u1/replay'
_REPLAYS = {}


def _git(*args):
    proc = subprocess.run(['git', '-C', str(REPO), *args], capture_output=True)
    if proc.returncode:
        raise Refused(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def lane_base():
    """The U0 commit this lane was cut from: the parent of the commit adding this script.

    Independent of remote refs, so a moving U0 branch cannot change it; after the
    re-cut (script commit cherry-picked onto the new U0 head) it is that head.
    """
    for attempt in range(2):
        added = _git('log', '--diff-filter=A', '--format=%H', 'HEAD', '--', 'scripts/split_lanes/u1_move.py').decode().split()
        parent = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--verify', '-q', added[-1] + '^'],
                                capture_output=True) if added else None
        if parent is not None and parent.returncode == 0:
            return parent.stdout.decode().strip()
        if attempt == 0:
            # A shallow clone shows its root as adding every file: deepen HEAD once.
            head = _git('rev-parse', 'HEAD').decode().strip()
            subprocess.run(['git', '-C', str(REPO), 'fetch', '--no-tags', '--quiet', '--depth=64', 'origin', head],
                           capture_output=True, timeout=300)
    raise Refused('cannot find the U0 commit this lane was cut from (parent of the commit adding '
                  'scripts/split_lanes/u1_move.py); pass --lane-base. Refusing, nothing written')


def replay(base, frozen, log=print, inputs=None):
    """Run this lane's move on the frozen inputs in scratch; return (inputs, outputs, root).

    Inputs are the files the lane writes, read from the U0 commit the lane was cut
    from. The move validates them against the frozen BASE first (manifest hashes,
    exact bodies, positions), so the replay cannot start from a drifted tree.
    """
    if inputs is None:
        _ensure_commit(base)
        inputs = {relative: _git('show', f'{base}:{relative}') for relative in WRITTEN}
    key = (base, hashlib.sha256(frozen.encode()).hexdigest(),
           hashlib.sha256(b''.join(hashlib.sha256(inputs[r]).digest() for r in WRITTEN)).hexdigest())
    if key in _REPLAYS:
        return _REPLAYS[key]
    root = REPLAY_ROOT / (base[:12] + '-' + key[2][:8])
    if root.exists():
        shutil.rmtree(root)
    for relative, data in inputs.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    _move_in_place(root / 'skills/token-optimizer/scripts', root / WRITTEN[-1], frozen, log)
    outputs = {relative: (root / relative).read_bytes() for relative in WRITTEN}
    _REPLAYS[key] = (inputs, outputs, root)
    return _REPLAYS[key]


def run(scripts=split.SCRIPTS, fragments_path=FRAGMENTS, *, dry_run=False, log=print, frozen=None, base=None):
    """Apply the lane, or prove it is already applied, by byte-comparing a replay.

    Already applied means exactly: every file this lane writes is byte-identical
    to the replayed move. An untouched tree (byte-identical to the inputs) gets
    the replay's outputs. Anything else refuses, naming the files that differ and
    the replay to diff them against; the finer validators add diagnostics.
    """
    scripts, fragments_path = Path(scripts), Path(fragments_path)
    frozen = frozen if frozen is not None else frozen_source()
    base = base or lane_base()
    inputs, outputs, root = replay(base, frozen, log)
    targets = {WRITTEN[0]: scripts / 'measure.py', WRITTEN[-1]: fragments_path}
    targets.update({f'skills/token-optimizer/scripts/_measure/{module}.py': scripts / '_measure' / (module + '.py')
                    for module in LANE_MODULES})
    current = {relative: path.read_bytes() for relative, path in targets.items()}
    _, symbols = lane_packet()
    if current == outputs:
        log(f'lane {LANE}: already applied; byte-identical to the replay at {root}')
        return {'moved_now': 0, 'already_moved': len(symbols)}
    if current == inputs:
        moved_now = len(symbols) - len(classify_inputs(inputs, symbols))
        if not dry_run:
            for relative, path in targets.items():
                path.write_bytes(outputs[relative])
        log(f'lane {LANE}: applied the replay from {root}')
        return {'moved_now': moved_now, 'already_moved': len(symbols) - moved_now, **({'dry_run': True} if dry_run else {})}
    differing = [f'{relative} (replay: {root / relative})' for relative in WRITTEN if current[relative] != outputs[relative]]
    findings = []
    for check in (lambda: check_bootstrap(scripts), lambda: validate(scripts, frozen, symbols)):
        try:
            check()
        except Refused as exc:
            findings.append(str(exc))
    diagnostics = '\n'.join(findings) or 'finer validators found nothing more specific'
    raise Refused('working tree differs from the replayed move in:\n  ' + '\n  '.join(differing)
                  + '\ndiagnostics: ' + diagnostics)


def classify_inputs(inputs, symbols):
    """Symbols already moved in the U0 inputs (the inherited canaries)."""
    names = set()
    for module in LANE_MODULES:
        tree = ast.parse(inputs[f'skills/token-optimizer/scripts/_measure/{module}.py'].decode('utf-8'))
        names |= set(_definitions(tree))
    return [name for name, _ in symbols if name in names]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scripts', type=Path, default=split.SCRIPTS)
    parser.add_argument('--fragments', type=Path, default=FRAGMENTS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--frozen-source', type=Path, help='unsplit BASE measure.py (default: git show of manifest base)')
    parser.add_argument('--lane-base', help='U0 commit the lane was cut from (default: parent of the commit adding this script)')
    args = parser.parse_args()
    try:
        frozen = frozen_source(args.frozen_source)
        print(json.dumps(run(args.scripts, args.fragments, dry_run=args.dry_run, frozen=frozen, base=args.lane_base)))
    except Refused as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
