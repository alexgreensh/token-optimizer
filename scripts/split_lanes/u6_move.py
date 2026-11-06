#!/usr/bin/env python3
"""Lane U6: move the quality_archive and continuity definitions out of measure.py.

Deterministic and re-runnable against whatever measure.py the branch holds:

* Symbols come BY NAME from lane 6 of scripts/measure_split_ownership.json.
* Each body is extracted with U0's scope-aware mover (scripts/measure_split.py:
  extract_definition), from one parse of the unmodified facade, and appended to
  its destination provider in facade source order.
* Each definition (decorators included) is replaced in place by the
  `_measure_compat.bind(...)` statement U0's facade pattern uses.
* Lane 6's mutation fragments are re-expressed against the qualified provider
  text. Every translated fragment is proven: planting it in the provider gives
  byte-for-byte what moving the BASE-planted facade gives.

Inputs are pinned in this file by commit SHA + sha256 (FROZEN_BASE,
LANE_INPUTS), fetched by SHA when the clone is shallow, and verified; the run
refuses before any write when they cannot be obtained or do not match. A fresh
tree must equal the pinned inputs exactly.

Completed state is decided by replay: the pinned inputs (facade, provider
stubs and mutation fragments at the lane's cut point) are copied into a
scratch dir, the move runs there, and every file this lane writes must be
byte-identical to the working tree, or the run refuses naming the paths. The
finer checks below remain as diagnostics.

Everything is checked against the pinned frozen BASE facade, also verified
by the manifest's whole-file and per-symbol hashes. Idempotent: an already-moved tree returns success only when each
provider file is byte-for-byte what the move writes from frozen BASE (bodies,
placeholder defaults, order) and each facade binding, captured defaults and
decorators included, matches exactly. Any body or default drift refuses.
Refuses on a missing symbol, a duplicate body, a body in the wrong provider,
a half-moved tree, or a fragment it cannot prove.

Every body also passes a symbol-table audit (audit_scopes): except-as names,
for/with/comprehension targets, walrus, imports and nested defs stay local,
and no facade name stays unqualified. A failure refuses the whole move.

Run with the same interpreter every time (python3.14): CPython's symbol
tables drive the qualification.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import subprocess
import symtable
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LANE = '6'
FACADE = 'skills/token-optimizer/scripts/measure.py'

# Pinned, immutable inputs (Astra U4 finding 1). Never derived from the working
# tree: obtained by SHA (git cat-file, or git fetch --depth=1 origin <sha> in a
# shallow clone) and verified by sha256, or the run refuses before any write.
# Re-pin here, in the script commit, only when the lane is re-cut on a new U0.
FROZEN_BASE = {  # unsplit facade the manifest was frozen from
    'sha': '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f',
    'sha256': 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc',
}
LANE_INPUTS = {  # the U0 commit this lane is cut from
    'sha': '8aa1464db203b78bded10b87f8ecbd2b497f7fad',
    'facade_sha256': '82de61c808d96ce8ea7318d332ba2e57c2267cb8d1d8eb20817db7a2255b8fde',
    'stubs_sha256': 'f342617652472c24428bce56cc329d0d0ce3a800b8a86032ff588c2ba3a35989',
    'fragments_sha256': 'ac5c081230fcc17b499134aac5be634214dae6281adacd7e8df7eed34c4e404e',
}
MODULES = ('quality_archive', 'continuity')


def _tool():
    spec = importlib.util.spec_from_file_location('_u6_measure_split', REPO / 'scripts/measure_split.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


TOOL = _tool()
DEFINITION = TOOL.DEFINITION


class MoveError(RuntimeError):
    pass


def lane_symbols(ownership):
    packet = json.loads(Path(ownership).read_text(encoding='utf-8'))['lanes'][LANE]
    if tuple(packet['modules']) != MODULES:
        raise MoveError(f'lane {LANE} modules changed: {packet["modules"]}')
    symbols = [(row['name'], row['module']) for row in packet['symbols']]
    names = [name for name, _ in symbols]
    if len(set(names)) != len(names):
        raise MoveError('duplicate symbol in the lane packet')
    if any(module not in MODULES for _, module in symbols):
        raise MoveError('lane packet assigns a symbol outside the lane modules')
    return dict(symbols)


def _defs(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, DEFINITION):
            out.setdefault(node.name, []).append(node)
    return out


def _bindings(tree):
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            out.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    return out


def _qualifier(source, tree, table):
    # Same globals rule as TOOL.extract_definition, computed once.
    globals_ = ({s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
                | {name for statement in ast.walk(tree) if isinstance(statement, ast.Global)
                   for name in statement.names})
    return globals_


def qualified(source, tree, table, globals_, name):
    """Mirror TOOL.extract_definition, also returning the edits and the span.

    The caller asserts the text equals TOOL.extract_definition's output, so the
    two can never drift apart silently.
    """
    node = next(n for n in tree.body if isinstance(n, DEFINITION) and n.name == name)
    qualifier = TOOL.Qualifier(source, table, globals_, tree=tree)
    node = copy.deepcopy(node)
    qualifier.root = node
    node.decorator_list = []
    defaults = list(node.args.defaults) if hasattr(node, 'args') else []
    kwdefaults = [v for v in node.args.kw_defaults if v] if hasattr(node, 'args') else []
    if hasattr(node, 'args'):
        node.args.defaults = []
        node.args.kw_defaults = [None] * len(node.args.kwonlyargs)
    qualifier.visit(node)
    for value in defaults + kwdefaults:
        qualifier.replace(value, 'None')
    start = qualifier.offset(node.lineno, node.col_offset)
    end = qualifier.offset(node.end_lineno, node.end_col_offset)
    edits = sorted(e for e in qualifier.edits if start <= e[0] <= e[1] <= end)
    text = source[start:end]
    for left, right, value in reversed(edits):
        text = text[:left-start] + value + text[right-start:]
    return text, start, end, edits


def map_span(source, start, edits, left, right):
    """Qualified text of source[left:right]; insertions on its edges stay outside."""
    for a, b, _ in edits:
        if a < left < b or a < right < b:
            raise MoveError('fragment boundary splits a qualified token')
    text = source[left:right]
    for a, b, value in sorted(edits, reverse=True):
        if left <= a and b <= right and not (a == b and a in (left, right)):
            text = text[:a-left] + value + text[b-left:]
    return text


def _provider_body(text, name):
    node = next(n for n in ast.parse(text).body if isinstance(n, DEFINITION) and n.name == name)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets[node.lineno-1], offsets[node.end_lineno-1] + len(lines[node.end_lineno-1].rstrip('\n'))


def translate_fragments(source, tree, table, globals_, owners, fragments, bindings):
    """Return lane-6 fragments rewritten for the moved tree, each one proven."""
    out = []
    for fragment in fragments:
        name = fragment.get('symbol')
        if name not in owners:
            out.append(fragment)
            continue
        old, new = fragment['old'], fragment['new']
        node = next(n for n in tree.body if isinstance(n, DEFINITION) and n.name == name)
        text, start, end, edits = qualified(source, tree, table, globals_, name)
        at = source.find(old)
        if at < start or at >= end:
            raise MoveError(f'{fragment["mutation"]}: old text does not start inside {name}')
        mutated = source[:at] + new + source[at+len(old):]
        if at + len(old) > end:
            # Facade text after the body (#246 plants a module-level call).
            inside = source[at:end]
            if not new.startswith(inside) or at != end - len(inside):
                raise MoveError(f'{fragment["mutation"]}: cannot split facade-tail fragment')
            binding = bindings[name]
            row = {k: v for k, v in fragment.items() if k != 'symbol'}
            row['file'] = fragment['file']
            row['old'] = binding + old[len(inside):]
            row['new'] = binding + new[len(inside):]
            out.append((row, 'facade', mutated))
            continue
        mtree = ast.parse(mutated)
        mtable = symtable.symtable(mutated, 'measure.py', 'exec')
        mglobals = _qualifier(mutated, mtree, mtable)
        mtext, mstart, _, medits = qualified(mutated, mtree, mtable, mglobals, name)
        if mstart != start:
            raise MoveError(f'{fragment["mutation"]}: mutation moved the definition start')
        row = dict(fragment)
        row['old'] = map_span(source, start, edits, at, at + len(old))
        row['new'] = map_span(mutated, start, medits, at, at + len(new))
        if row['old'] == row['new'] or not row['old']:
            raise MoveError(f'{fragment["mutation"]}: translation plants nothing')
        out.append((row, 'provider', mtext))
    return out


def prove_fragments(rows, providers, owners, facade_text, scripts):
    final = []
    for item in rows:
        if isinstance(item, dict):
            final.append(item)
            continue
        row, where, expected = item
        if where == 'provider':
            name = row['symbol']
            text = providers[owners[name]]
            left, right = _provider_body(text, name)
            at = text.find(row['old'])
            if at < left or at + len(row['old']) > right:
                raise MoveError(f'{row["mutation"]}: translated old is not first found inside {name}')
            planted = text.replace(row['old'], row['new'], 1)
            pleft, pright = _provider_body(planted, name)
            if planted[pleft:pright] != expected:
                raise MoveError(f'{row["mutation"]}: planted provider differs from moving the planted facade')
        else:
            if facade_text.count(row['old']) != 1:
                raise MoveError(f'{row["mutation"]}: facade-tail fragment is not unique')
            planted = facade_text.replace(row['old'], row['new'], 1)
            compile(planted, str(scripts / 'measure.py'), 'exec')
        final.append(row)
    return final


def _function_table(text, name):
    module = symtable.symtable(text, name + '.py', 'exec')
    found = [child for child in module.get_children() if child.get_name() == name]
    if len(found) != 1:
        raise MoveError(f'{name}: expected one top-level scope, found {len(found)}')
    return found[0]


def audit_scopes(original, moved, name, facade_globals):
    """Binding-level proof that qualification kept every scope's meaning.

    Compares CPython symbol tables of the original definition and the moved
    one, scope by scope. Every local binding (parameters, assignments,
    except-as names, for/with/comprehension targets, walrus, imports, nested
    defs) must stay local; no facade-global name may become local or stay an
    unqualified global; closures keep their free variables. The only new
    names are the facade handle and the mover's import temporaries.
    """
    errors = []

    def walk(o, n, path):
        if (o.get_type(), o.get_name()) != (n.get_type(), n.get_name()):
            errors.append(f'{path}: scope shape changed {o.get_name()} -> {n.get_name()}')
            return
        os_ = {s.get_name(): s for s in o.get_symbols()}
        ns_ = {s.get_name(): s for s in n.get_symbols()}
        o_local = {k for k, s in os_.items() if s.is_local()}
        n_local = {k for k, s in ns_.items() if s.is_local()}
        temporaries = {k for k in n_local if k == '_measure_facade' or k.startswith(('_measure_import_', '_measure_exception_'))}
        rebound_globals = {k for k in n_local - o_local
                           if k in os_ and os_[k].is_declared_global()
                           and (ns_[k].is_imported() or ns_[k].is_namespace())}
        lost = o_local - n_local
        gained = n_local - o_local - temporaries - rebound_globals
        if lost:
            errors.append(f'{path}: local binding rewritten to facade: {sorted(lost)}')
        if gained:
            errors.append(f'{path}: global turned into a local binding: {sorted(gained)}')
        o_free = {k for k, s in os_.items() if s.is_free()}
        n_free = {k for k, s in ns_.items() if s.is_free()}
        if o_free - n_free or n_free - o_free - {'_measure_facade'}:
            errors.append(f'{path}: closure variables changed {sorted(o_free ^ n_free)}')
        n_global = {k for k, s in ns_.items() if s.is_global() and (s.is_referenced() or s.is_assigned())}
        o_global = {k for k, s in os_.items() if s.is_global()}
        stray = n_global - {'_compat'} - (o_global - facade_globals)
        if stray:
            errors.append(f'{path}: unqualified facade/global names left: {sorted(stray)}')
        if any(s.is_declared_global() for s in ns_.values()):
            errors.append(f'{path}: global declaration survived qualification')
        oc, nc = o.get_children(), n.get_children()
        if len(oc) != len(nc):
            errors.append(f'{path}: child scope count {len(oc)} -> {len(nc)}')
            return
        for oo, nn in zip(oc, nc):
            walk(oo, nn, f'{path}/{oo.get_name()}')

    walk(_function_table(original, name), _function_table(moved, name), name)
    return errors


def _definition_text(source, node):
    lines = source.splitlines(keepends=True)
    return ''.join(lines[node.lineno-1:node.end_lineno])


def audit_tree(base_measure, scripts, owners):
    """Audit an already-moved tree against the BASE facade it came from."""
    source = Path(base_measure).read_text(encoding='utf-8')
    tree = ast.parse(source)
    table = symtable.symtable(source, 'measure.py', 'exec')
    globals_ = _qualifier(source, tree, table)
    errors, handlers = [], 0
    providers = {m: (Path(scripts) / '_measure' / (m + '.py')).read_text(encoding='utf-8') for m in MODULES}
    for node in tree.body:
        if isinstance(node, DEFINITION) and node.name in owners:
            text = providers[owners[node.name]]
            moved = next(n for n in ast.parse(text).body if isinstance(n, DEFINITION) and n.name == node.name)
            errors += audit_scopes(_definition_text(source, node), _definition_text(text, moved), node.name, globals_)
            handlers += sum(1 for h in ast.walk(node) if isinstance(h, ast.ExceptHandler) and h.name)
    return errors, handlers


def state(scripts, owners):
    facade = ast.parse((scripts / 'measure.py').read_text(encoding='utf-8'))
    in_facade = _defs(facade)
    bound = _bindings(facade)
    provider_defs = {}
    for module in TOOL.DOMAINS:
        for name, nodes in _defs(ast.parse((scripts / '_measure' / (module + '.py')).read_text(encoding='utf-8'))).items():
            provider_defs.setdefault(name, []).extend([module] * len(nodes))
    unmoved, moved = [], []
    for name, module in owners.items():
        here = len(in_facade.get(name, []))
        there = provider_defs.get(name, [])
        if here > 1 or len(there) > 1 or (here and there):
            raise MoveError(f'duplicate symbol {name}: facade={here}, providers={there}')
        if not here and not there:
            raise MoveError(f'missing symbol {name}')
        if there:
            want = f"_measure_providers[{module!r}].{name}"
            if there != [module] or bound.get(name) != [want]:
                raise MoveError(f'{name}: moved body/binding inconsistent ({there}, {bound.get(name)})')
            moved.append(name)
        else:
            if name in bound:
                raise MoveError(f'{name}: facade has both a body and a binding')
            unmoved.append(name)
    return unmoved, moved


def frozen_hash(lines, row):
    return hashlib.sha256('\n'.join(lines[row['start']-1:row['end']]).encode()).hexdigest()


def load_pinned_frozen(manifest=None, frozen_path=None, pins=None):
    """The pinned unsplit facade, verified by sha256; never the working tree."""
    pins = pins or FROZEN_BASE
    if frozen_path is not None:
        text = Path(frozen_path).read_text(encoding='utf-8')
    else:
        ensure_commit(pins['sha'])
        text = _show(pins['sha'], FACADE)
    if _sha256(text) != pins['sha256']:
        raise MoveError(f'frozen BASE has sha256 {_sha256(text)}, pinned {pins["sha256"]}')
    if manifest is not None and manifest['source_sha256'] != pins['sha256']:
        raise MoveError('manifest source_sha256 differs from the pinned frozen BASE')
    return text


def expected_moves(frozen, owners, rows):
    """What a correct move of each lane symbol produces, derived from frozen BASE only."""
    tree = ast.parse(frozen)
    table = symtable.symtable(frozen, 'measure.py', 'exec')
    globals_ = _qualifier(frozen, tree, table)
    lines = frozen.splitlines()
    nodes = {n.name: n for n in tree.body if isinstance(n, DEFINITION) and n.name in owners}
    out = {}
    for order, name in enumerate(sorted(owners, key=lambda k: rows[k]['start'])):
        row, node = rows[name], nodes.get(name)
        if node is None:
            raise MoveError(f'{name}: not defined in frozen BASE')
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        if (first, node.end_lineno) != (row['start'], row['end']) or frozen_hash(lines, row) != row['sha256']:
            raise MoveError(f'{name}: frozen BASE span/hash differs from the manifest')
        moved, binding = TOOL.extract_definition(frozen, name, owners[name], tree=tree, table=table)
        if qualified(frozen, tree, table, globals_, name)[0] + '\n' != moved:
            raise MoveError(f'{name}: local qualifier drifted from U0 extract_definition')
        scope_errors = audit_scopes(_definition_text(frozen, node), moved, name, globals_)
        if scope_errors:
            raise MoveError('scope audit failed: ' + '; '.join(scope_errors))
        out[name] = {'moved': moved, 'binding': binding, 'order': order,
                     'definition': '\n'.join(lines[row['start']-1:row['end']])}
    return out


def _statement_tokens(tree):
    """One token per top-level statement; a definition and its binding share one.

    Definitions and `_measure_compat.bind` statements become ('def', name), so
    any lane's in-place replacement keeps its token. Everything else is its
    AST without positions. Repeats are numbered so each token is unique.
    """
    tokens, seen = [], {}
    for node in tree.body:
        if isinstance(node, DEFINITION):
            key = ('def', node.name)
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
              and ast.unparse(node.value.func) == '_measure_compat.bind'):
            key = ('def', node.targets[0].id)
        else:
            key = ('stmt', ast.dump(node, include_attributes=False))
        seen[key] = seen.get(key, 0) + 1
        tokens.append(key + (seen[key],))
    return tokens


def position_errors(frozen_tree, current_tree, owners):
    """Each owned symbol keeps its place in the frozen initialization order.

    Statements present on only one side (U0 bootstrap lines, a moved CLI
    guard) are ignored; every remaining statement, other lanes' bindings
    included, must keep the same relative order around each owned symbol.
    """
    frozen, current = _statement_tokens(frozen_tree), _statement_tokens(current_tree)
    common = set(frozen) & set(current)
    want = [t for t in frozen if t in common]
    have = [t for t in current if t in common]
    errors = []
    for name in owners:
        token = ('def', name, 1)
        if token not in common:
            errors.append(f'{name}: missing from the facade statement order')
            continue
        i, j = want.index(token), have.index(token)
        if (want[i-1:i], want[i+1:i+2]) != (have[j-1:j], have[j+1:j+2]) or i != j:
            errors.append(f'{name}: position drift: binding no longer sits between its frozen neighbours')
    return errors


def validate_moved(facade_text, provider_texts, owners, expected, stubs, frozen_tree):
    """Completed state must be byte-for-byte what the move writes from frozen BASE,
    with every binding still between its frozen neighbours."""
    facade_tree = ast.parse(facade_text)
    seen = []
    for node in facade_tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'
                and node.targets[0].id in owners):
            name = node.targets[0].id
            if ast.get_source_segment(facade_text, node) != expected[name]['binding']:
                raise MoveError(f'{name}: binding drift (body reference, defaults or decorators changed)')
            seen.append(name)
    if seen != sorted(owners, key=lambda k: expected[k]['order']):
        raise MoveError('binding order drift: facade bindings are not at their frozen positions')
    drift = position_errors(frozen_tree, facade_tree, owners)
    if drift:
        raise MoveError('; '.join(drift[:5]))
    for module in MODULES:
        names = sorted((n for n in owners if owners[n] == module), key=lambda k: expected[k]['order'])
        want = stubs[module].rstrip() + '\n' + ''.join('\n\n' + expected[n]['moved'] for n in names)
        text = provider_texts[module]
        if text != want:
            have = {n.name: _definition_text(text, n) for n in ast.parse(text).body if isinstance(n, DEFINITION)}
            drifted = [n for n in names if have.get(n) != expected[n]['moved']]
            raise MoveError(f'{module}: body drift in {drifted[:5] or "provider header/order"}')


WRITTEN = ('measure.py', '_measure/quality_archive.py', '_measure/continuity.py')
PROVIDER_DIR = 'skills/token-optimizer/scripts/_measure/'
FRAGMENTS = 'scripts/mutations/_measure_lane6.json'


def _git(*args):
    done = subprocess.run(['git', '-C', str(REPO), *args], capture_output=True)
    if done.returncode:
        raise MoveError(f'git {" ".join(args)} failed: {done.stderr.decode("utf-8", "replace").strip()}')
    return done.stdout.decode('utf-8')


def ensure_commit(sha):
    """Make `sha` readable, fetching exactly that object when the clone is shallow (CI)."""
    probe = ['git', '-C', str(REPO), 'cat-file', '-e', sha + '^{commit}']
    if subprocess.run(probe, capture_output=True).returncode:
        _git('fetch', '--no-tags', '--depth=1', 'origin', sha)
        if subprocess.run(probe, capture_output=True).returncode:
            raise MoveError(f'commit {sha} is not available even after an explicit fetch')
    return sha


def _show(sha, path):
    return _git('show', f'{sha}:{path}')


def _sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def stubs_digest(providers):
    digest = hashlib.sha256()
    for domain in TOOL.DOMAINS:
        digest.update(domain.encode() + b'\0' + providers[domain].encode('utf-8') + b'\0')
    return digest.hexdigest()


def lane_inputs(pins=None):
    """The pinned lane inputs: facade, every provider stub, mutation fragments."""
    pins = pins or LANE_INPUTS
    ensure_commit(pins['sha'])
    inputs = {'ref': pins['sha'], 'measure.py': _show(pins['sha'], FACADE),
              'providers': {d: _show(pins['sha'], f'{PROVIDER_DIR}{d}.py') for d in TOOL.DOMAINS},
              'fragments': _show(pins['sha'], FRAGMENTS)}
    checks = (('facade', _sha256(inputs['measure.py']), pins['facade_sha256']),
              ('provider stubs', stubs_digest(inputs['providers']), pins['stubs_sha256']),
              ('mutation fragments', _sha256(inputs['fragments']), pins['fragments_sha256']))
    for label, have, want in checks:
        if have != want:
            raise MoveError(f'pinned lane input {label} at {pins["sha"]} has sha256 {have}, pinned {want}')
    return inputs


def replay(scripts, fragments_path, owners, rows, frozen, inputs, scratch):
    """Run the move on the frozen inputs in `scratch`; return the differing lane files."""
    import shutil
    scratch = Path(scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    copy_scripts = scratch / 'scripts'
    (copy_scripts / '_measure').mkdir(parents=True)
    (copy_scripts / 'measure.py').write_text(inputs['measure.py'], encoding='utf-8', newline='\n')
    for domain, text in inputs['providers'].items():
        (copy_scripts / '_measure' / (domain + '.py')).write_text(text, encoding='utf-8', newline='\n')
    copy_fragments = scratch / 'fragments.json'
    copy_fragments.write_text(inputs['fragments'], encoding='utf-8', newline='\n')
    unmoved, moved = state(copy_scripts, owners)
    if moved or len(unmoved) != len(owners):
        raise MoveError('frozen inputs are not an unmoved lane tree')
    _move(copy_scripts, owners, rows, frozen, copy_fragments,
          {m: inputs['providers'][m] for m in MODULES})
    pairs = [(Path(scripts) / rel, copy_scripts / rel) for rel in WRITTEN]
    pairs.append((Path(fragments_path), copy_fragments))
    return [Path(have).as_posix() for have, want in pairs if have.read_bytes() != want.read_bytes()], scratch


def run(scripts, owners, rows, frozen, fragments_path, check=False, inputs=None, scratch=None):
    """Fresh tree: move. Moved tree: succeed only if a replay on frozen inputs is byte-identical."""
    scripts, fragments_path = Path(scripts), Path(fragments_path)
    unmoved, moved = state(scripts, owners)
    if moved and unmoved:
        raise MoveError(f'half-moved tree: {len(moved)} moved, {len(unmoved)} not; refusing')
    if inputs is None:
        raise MoveError('pinned lane inputs are required')
    stubs = {m: inputs['providers'][m] for m in MODULES}
    if unmoved:
        # A fresh tree must be exactly the pinned inputs, so nothing outside the
        # lane's own definitions can ride along with the move.
        pairs = [(scripts / 'measure.py', inputs['measure.py']), (fragments_path, inputs['fragments'])]
        pairs += [(scripts / '_measure' / (d + '.py'), inputs['providers'][d]) for d in TOOL.DOMAINS]
        differing = [Path(path).as_posix() for path, want in pairs if Path(path).read_bytes() != want.encode('utf-8')]
        if differing:
            raise MoveError(f'fresh tree differs from the pinned lane inputs {inputs.get("ref", "")}: '
                            + ', '.join(differing))
        return _move(scripts, owners, rows, frozen, fragments_path, stubs, check=check)
    if scratch is None:
        raise MoveError('moved tree: a scratch dir is required to replay the move')
    differing, where = replay(scripts, fragments_path, owners, rows, frozen, inputs, scratch)
    if differing:
        diagnostics = []
        try:
            validate_moved((scripts / 'measure.py').read_text(encoding='utf-8'),
                           {m: (scripts / '_measure' / (m + '.py')).read_text(encoding='utf-8') for m in MODULES},
                           owners, expected_moves(frozen, owners, rows), stubs, ast.parse(frozen))
        except MoveError as exc:
            diagnostics.append(str(exc))
        raise MoveError(f'completed state differs from a replay of the move on frozen inputs '
                        f'{inputs.get("ref", "")}: {", ".join(differing)} (replay at {Path(where).as_posix()})'
                        + ('; diagnostic: ' + '; '.join(diagnostics) if diagnostics else ''))
    return {'moved': 0, 'already_moved': len(moved), 'replay_identical': len(WRITTEN) + 1}


def _move(scripts, owners, rows, frozen, fragments_path, stubs, check=False):
    expected = expected_moves(frozen, owners, rows)
    frozen_tree = ast.parse(frozen)
    fragments = json.loads(fragments_path.read_text(encoding='utf-8'))
    path = scripts / 'measure.py'
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    table = symtable.symtable(source, 'measure.py', 'exec')
    globals_ = _qualifier(source, tree, table)
    nodes = sorted((n for n in tree.body if isinstance(n, DEFINITION) and n.name in owners),
                   key=lambda n: n.lineno)
    if len(nodes) != len(owners):
        raise MoveError('symbol count mismatch')
    if [n.name for n in nodes] != sorted(owners, key=lambda k: expected[k]['order']):
        raise MoveError('facade definition order differs from frozen BASE')
    drift = position_errors(frozen_tree, tree, owners)
    if drift:
        raise MoveError('; '.join(drift[:5]))
    lines_now = source.splitlines()
    appended = {module: [] for module in MODULES}
    changes = []
    bindings = {}
    for node in nodes:
        module = owners[node.name]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        if '\n'.join(lines_now[first-1:node.end_lineno]) != expected[node.name]['definition']:
            raise MoveError(f'{node.name}: facade definition differs from frozen BASE')
        moved_text, binding = TOOL.extract_definition(source, node.name, module, tree=tree, table=table)
        if (moved_text, binding) != (expected[node.name]['moved'], expected[node.name]['binding']):
            raise MoveError(f'{node.name}: extraction here differs from extraction of frozen BASE')
        appended[module].append(moved_text)
        bindings[node.name] = binding
        changes.append((first, node.end_lineno, binding))
    providers = {}
    for module in MODULES:
        target = scripts / '_measure' / (module + '.py')
        existing = target.read_text(encoding='utf-8')
        if existing != stubs[module]:
            raise MoveError(f'{module}: destination is not the definition-only stub')
        text = existing.rstrip() + '\n'
        for moved_text in appended[module]:
            text += '\n\n' + moved_text
        compile(text, str(target), 'exec')
        providers[module] = text
    lines = source.splitlines(keepends=True)
    for first, last, binding in sorted(changes, reverse=True):
        lines[first-1:last] = [binding + '\n']
    facade = ''.join(lines)
    compile(facade, str(path), 'exec')
    rows_out = translate_fragments(source, tree, table, globals_, owners, fragments, bindings)
    final = prove_fragments(rows_out, providers, owners, facade, scripts)
    # The result must pass the completed-state validator before anything is written.
    validate_moved(facade, providers, owners, expected, stubs, frozen_tree)
    if check:
        return {'moved': len(nodes), 'dry_run': True}
    for module in MODULES:
        (scripts / '_measure' / (module + '.py')).write_text(providers[module], encoding='utf-8', newline='\n')
    path.write_text(facade, encoding='utf-8', newline='\n')
    fragments_path.write_text(json.dumps(final, indent=2) + '\n', encoding='utf-8', newline='\n')
    return {'moved': len(nodes), 'quality_archive': len(appended['quality_archive']),
            'continuity': len(appended['continuity']),
            'fragments_translated': sum(1 for r in rows_out if not isinstance(r, dict))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scripts', type=Path, default=REPO / 'skills/token-optimizer/scripts')
    parser.add_argument('--ownership', type=Path, default=REPO / 'scripts/measure_split_ownership.json')
    parser.add_argument('--fragments', type=Path, default=REPO / 'scripts/mutations/_measure_lane6.json')
    parser.add_argument('--manifest', type=Path, default=REPO / 'scripts/measure_split_manifest.json')
    parser.add_argument('--frozen', type=Path, help='unsplit measure.py file; must match the pinned sha256')
    parser.add_argument('--scratch', type=Path, default=REPO / '.scratch-u6/replay',
                        help='where the completed-state replay runs')
    parser.add_argument('--check', action='store_true', help='prove everything, write nothing')
    parser.add_argument('--audit-against', type=Path, metavar='BASE_MEASURE',
                        help='scope-audit the moved tree against an unsplit measure.py')
    args = parser.parse_args(argv)
    if args.audit_against:
        errors, handlers = audit_tree(args.audit_against, args.scripts, lane_symbols(args.ownership))
        print(json.dumps({'audited': len(lane_symbols(args.ownership)), 'named_except_handlers': handlers,
                          'errors': errors}, sort_keys=True))
        return 1 if errors else 0
    try:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        owners = lane_symbols(args.ownership)
        rows = {row['name']: row for row in manifest['symbols'] if row['name'] in owners}
        frozen = load_pinned_frozen(manifest, args.frozen)
        inputs = lane_inputs()
        result = run(args.scripts, owners, rows, frozen, args.fragments, check=args.check,
                     inputs=inputs, scratch=args.scratch)
        print(json.dumps(result, sort_keys=True))
    except MoveError as exc:
        print(f'u6_move: refused: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
