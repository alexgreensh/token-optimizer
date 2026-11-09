#!/usr/bin/env python3
"""Lane U2: move the sessions definitions and the CLI dispatch out of measure.py.

Deterministic and re-runnable against any measure.py that still carries this
lane's symbols. It reads the lane packet by name, never by line number, and
drives U0's scope-aware mover (scripts/measure_split.py) for the actual moves.

"Already applied" means exactly one thing: every file this lane writes
(measure.py, _measure/sessions.py, _measure/cli.py, the lane mutation fragment
and scripts/split_lanes/u2_frozen.json) is byte-identical to a fresh re-run of
this script on the foundation inputs (the U0 commit the lane was cut from:
measure.py, the U0 provider stubs and the mutation fragment), done in scratch
under .scratch-u2/reference/ (cached only if all five files still match
PINNED_OUTPUTS; the stamp alone is never trusted). Any difference refuses
and names both paths.
A fresh move ends with the same comparison.

The frozen inputs are pinned in this file (PINNED_BASE, PINNED_FOUNDATION:
commit + sha256 of every input), never derived from the current facade or any
editable tree file. Missing commits are fetched explicitly
(`git fetch --depth=1 origin <sha>`, which works in shallow CI); if one cannot
be obtained or a hash differs, the script refuses before writing.

The finer checks stay as diagnostics and as pre-write guards. Before anything
is written the script refuses when a packet symbol is missing, defined twice,
or present in both facade and provider; when an unmoved body or the dispatch
differs from its manifest hash; when the dispatch has a module-scope walrus,
match capture or global/nonlocal; or when any lane slot sits between different
non-lane statements than in the frozen BASE. Each moved body, facade binding
(captured defaults included) and the dispatch is compared with the mover's
derivation from the manifest BASE (git), or from u2_frozen.json in a clone
without that commit (``--no-reference``: diagnostic only, not authoritative).

Lane-local additions, neither edits U0's tool:

1. U0's final mover handles module-level ``except E as name`` itself (the
   facade target lives for the handler and is deleted after, as in BASE), so
   this script uses ``move_cli`` unchanged. Module-scope walrus or match
   captures in the dispatch have no faithful facade spelling and are refused.
2. The lane's mutation fragment (scripts/mutations/_measure_lane2.json) gets
   the same namespace qualification the mover applied to the code it targets,
   computed from the mover's own edit list.

Usage: python3 scripts/split_lanes/u2_move.py [--check] [--frozen auto|git|record]
       [--foundation SHA] [--reference DIR] [--no-reference]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import symtable
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import measure_split as split  # noqa: E402

LANE = '2'
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
OWNERSHIP = REPO / 'scripts/measure_split_ownership.json'
RECORD = REPO / 'scripts/split_lanes/u2_frozen.json'
MEASURE_REL = 'skills/token-optimizer/scripts/measure.py'
CLI_GUARD = "_measure_providers['cli'].main"
GUARD_TEXT = 'if __name__ == "__main__":\n    _measure_providers[\'cli\'].main()'
# Frozen inputs, pinned here and never read from editable tree files. Updated
# only on a deliberate re-cut (new U0 foundation / new BASE), in this commit.
PINNED_BASE = {'commit': '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f',
               'measure_sha256': 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc'}
PINNED_FOUNDATION = {
    'commit': 'aa356ec654a1f6fa2e736f1269a412ef5422069b',
    'inputs_sha256': {
        'skills/token-optimizer/scripts/measure.py': '82de61c808d96ce8ea7318d332ba2e57c2267cb8d1d8eb20817db7a2255b8fde',
        'skills/token-optimizer/scripts/_measure/__init__.py': '57114e197b9b1879fb97d0985b77cd215b27b1d8f6fa223488ebd16c25e5c961',
        'skills/token-optimizer/scripts/_measure/_compat.py': '6a910e5da6a8b25813aa391d490aeee10fd47847a7e625cd2391ff109fcae130',
        'skills/token-optimizer/scripts/_measure/sessions.py': 'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
        'skills/token-optimizer/scripts/_measure/cli.py': 'a0b0af34292099e4d8268fb3cb977679db580f5e52b45c552ada5d60a40dd962',
        'scripts/mutations/_measure_lane2.json': '8f46c8271938ffe222f0a1786140e78b88d87cbccf8d75c2f228d6c8b8c6ed95',
        'scripts/measure_split.py': '53398444aa02f9eecc09137953d6d13e7b1f97a47c3c724090885ba96dc666e8',
        'scripts/measure_split_manifest.json': 'e2708beba9bbd5671b6e492b564ba19006c4e0b27ba7b9fe38eda8d06e73e73f',
        'scripts/measure_split_ownership.json': '04574f5d338a160a3018eaa436b7b1e7f0ddaf03fcd456c5858d7dc787fc9d37',
    },
}
# Expected bytes of every lane-written file for the pinned foundation. A cached
# reference is trusted only when all five match; a fresh re-run must produce
# exactly these before its stamp is written. Update on a deliberate re-cut.
PINNED_OUTPUTS = {
    'skills/token-optimizer/scripts/measure.py': '757f3d33888e7dc6e50702fabd3cf52d1aca7271d499d8b1ea95cae2fec30a85',
    'skills/token-optimizer/scripts/_measure/sessions.py': 'e958f4462d03b2448b55dab8b286daa5e6c89380cdac44040e90295df8c0f2c3',
    'skills/token-optimizer/scripts/_measure/cli.py': '13efa8227ca3a3e68260da5bb61f069f2a208da05b6d8920598d77f332f51501',  # cursor-coach/cursor-audit dispatch added post-freeze (feat/cursor-parity); re-pinned for intentional evolution
    'scripts/mutations/_measure_lane2.json': '8575771aa875f7ea96ee651a5361721e238710c624dc49fea218ed3fa1e256ac',
    'scripts/split_lanes/u2_frozen.json': 'fff3c038830fcf85431facaa25ca8e465b21875f890d906c3915e7e8a160caac',
}
# Read-only inputs this lane depends on; the working tree must still carry the pinned bytes.
SHARED_INPUTS = ('scripts/measure_split.py', 'scripts/measure_split_manifest.json',
                 'scripts/measure_split_ownership.json')
# Scratch for derivations and the reference re-run (tests point it at tmp).
SCRATCH = Path(os.environ.get('U2_SCRATCH') or REPO / '.scratch-u2')
# Git history lives in the real checkout even when this script runs from a
# scratch copy of the foundation (the reference re-run below).
GIT_REPO = Path(os.environ.get('U2_GIT_REPO') or REPO)
# Every file this lane writes: facade + providers (relative to the scripts
# dir), then the mutation fragment and the frozen record (relative to REPO).
SCRIPT_OUTPUTS = ('measure.py', '_measure/sessions.py', '_measure/cli.py')
REPO_OUTPUTS = ('scripts/mutations/_measure_lane2.json', 'scripts/split_lanes/u2_frozen.json')


class Refusal(SystemExit):
    def __init__(self, message):
        super().__init__('refusing: ' + message)


def write_lf(path, text):
    """Raw UTF-8 bytes with LF newlines on every platform (the repo is eol=lf)."""
    Path(path).write_bytes(text.encode('utf-8'))


class lf_text_writes:
    """U0's mover writes with Path.write_text, which emits CRLF on Windows.
    Pin newline='\\n' for the duration of a move so outputs are byte-stable."""
    def __enter__(self):
        self.original = Path.write_text
        original = self.original

        def write_text(path, data, encoding=None, errors=None, newline=None):
            return original(path, data, encoding=encoding, errors=errors, newline='\n')
        Path.write_text = write_text

    def __exit__(self, *exc):
        Path.write_text = self.original


class RecordingQualifier(split.Qualifier):
    last = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        RecordingQualifier.last = self


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def packet():
    lane = json.loads(OWNERSHIP.read_text(encoding='utf-8'))['lanes'][LANE]
    owners = {s['name']: s['module'] for s in split.read_manifest()['symbols']}
    for symbol in lane['symbols']:
        if owners.get(symbol['name']) != symbol['module'] or symbol['module'] not in lane['modules']:
            raise Refusal(f"{symbol['name']} owner differs between packet and manifest")
    return lane


def top_definitions(path):
    counts = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, split.DEFINITION):
            counts[node.name] = counts.get(node.name, 0) + 1
    return counts


def refuse_unqualifiable_bindings(guard):
    """Module-scope bindings that are not plain ``Name`` targets.

    Plain targets (assignment, for, with, del, augmented) are module globals in
    BASE, so binding and reads both move to the facade together. ``except as``
    is handled by U0's mover. A walrus or a match capture at module
    scope has no faithful facade spelling, so refuse rather than guess.
    """
    pending = list(guard.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue  # their bodies are their own scopes
        if isinstance(node, ast.NamedExpr):
            raise Refusal(f'module-scope walrus in the dispatch at line {node.lineno}')
        if (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name
                or isinstance(node, ast.MatchMapping) and node.rest):
            raise Refusal(f'module-scope match capture in the dispatch at line {node.lineno}')
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise Refusal(f'{type(node).__name__.lower()} at dispatch module scope, line {node.lineno}')
        pending.extend(ast.iter_child_nodes(node))


def main_guard(source):
    guards = [n for n in ast.parse(source).body if isinstance(n, ast.If)
              and ast.unparse(n.test) == "__name__ == '__main__'"]
    if len(guards) != 1:
        raise Refusal(f'expected one main guard, found {len(guards)}')
    return guards[0]


def dispatch_state(scripts):
    """'facade' (unmoved), 'moved', or refuse on a half-moved state."""
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    guard = main_guard(source)
    one_line = (len(guard.body) == 1 and isinstance(guard.body[0], ast.Expr)
                and isinstance(guard.body[0].value, ast.Call)
                and ast.unparse(guard.body[0].value.func) == CLI_GUARD)
    has_main = top_definitions(scripts / '_measure/cli.py').get('main', 0)
    if one_line and has_main == 1:
        return 'moved'
    if not one_line and not has_main:
        refuse_unqualifiable_bindings(guard)
        return 'facade'
    raise Refusal('CLI dispatch is half-moved or cli.main is duplicated')


def plan(scripts, lane):
    facade = top_definitions(scripts / 'measure.py')
    providers = {m: top_definitions(scripts / '_measure' / (m + '.py')) for m in lane['modules']}
    pending, done = [], []
    for symbol in lane['symbols']:
        name, module = symbol['name'], symbol['module']
        here, there = facade.get(name, 0), providers[module].get(name, 0)
        if here > 1 or there > 1 or (here and there):
            raise Refusal(f'duplicate definition of {name} (facade={here}, {module}={there})')
        if not here and not there:
            raise Refusal(f'{name} is missing from measure.py and _measure/{module}.py')
        (pending if here else done).append(name)
    return pending, done


# --- frozen expectations ---------------------------------------------------

def ensure_commit(sha):
    """True once `sha` is a local commit; fetches it explicitly from origin when
    this clone is shallow (CI checks out without history)."""
    def present():
        return subprocess.run(['git', 'cat-file', '-e', sha + '^{commit}'], cwd=GIT_REPO,
                              capture_output=True).returncode == 0
    try:
        if present():
            return True
        subprocess.run(['git', 'fetch', '--no-tags', '--depth=1', 'origin', sha], cwd=GIT_REPO,
                       capture_output=True, timeout=300)
        return present()
    except (OSError, subprocess.SubprocessError):
        return False


def frozen_source():
    """The manifest BASE's measure.py, or None when it cannot be obtained."""
    manifest = split.read_manifest()
    if (manifest['base'], manifest['source_sha256']) != (PINNED_BASE['commit'], PINNED_BASE['measure_sha256']):
        raise Refusal('manifest BASE differs from the BASE pinned in this script')
    if not ensure_commit(PINNED_BASE['commit']):
        return None
    try:
        text = subprocess.run(['git', 'show', PINNED_BASE['commit'] + ':' + MEASURE_REL], cwd=GIT_REPO,
                              capture_output=True, check=True).stdout.decode('utf-8')
    except (OSError, subprocess.CalledProcessError):
        return None
    if sha(text) != PINNED_BASE['measure_sha256']:
        raise Refusal('pinned BASE measure.py does not match its pinned sha256')
    return text


def definition_texts(source, tree=None):
    """Original definition texts exactly as the manifest hashed them (one parse)."""
    lines = source.splitlines()  # the manifest hashed splitlines() slices
    out = {}
    for node in (tree or ast.parse(source)).body:
        if isinstance(node, split.DEFINITION):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out[node.name] = '\n'.join(lines[first - 1:node.end_lineno])
    return out


def check_unmoved(scripts, lane, pending, cli):
    """Bodies still in the facade must be the frozen ones."""
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    records = {s['name']: s for s in lane['symbols']}
    texts = definition_texts(source) if pending else {}
    drift = [n for n in pending if sha(texts[n]) != records[n]['sha256']]
    if drift:
        raise Refusal('unmoved body differs from frozen BASE: ' + ', '.join(drift))
    if cli == 'facade':
        guard = main_guard(source)
        lines = source.splitlines()
        if sha('\n'.join(lines[guard.lineno:guard.end_lineno])) != lane['dispatch']['sha256']:
            raise Refusal('unmoved CLI dispatch differs from frozen BASE')


def derive(frozen, lane):
    """What the mover makes of the frozen BASE: provider text and binding per symbol."""
    tree = ast.parse(frozen)
    table = symtable.symtable(frozen, 'measure.py', 'exec')
    texts = definition_texts(frozen, tree)
    expected = {}
    for symbol in lane['symbols']:
        name, module = symbol['name'], symbol['module']
        if sha(texts[name]) != symbol['sha256']:
            raise Refusal(f'frozen BASE {name} does not match its manifest hash')
        moved, binding = split.extract_definition(frozen, name, module, tree=tree, table=table)
        expected[name] = {'provider_sha256': sha(moved[:-1]), 'binding': binding}
    guard = main_guard(frozen)
    lines = frozen.splitlines()
    if sha('\n'.join(lines[guard.lineno:guard.end_lineno])) != lane['dispatch']['sha256']:
        raise Refusal('frozen BASE dispatch does not match its manifest hash')
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='u2-derive-') as work:
        work = Path(work)
        (work / '_measure').mkdir()
        write_lf(work / 'measure.py', frozen)
        write_lf(work / '_measure/cli.py', split.HEADER)
        with lf_text_writes():
            split.move_cli(work)
        main_text = provider_segments(work / '_measure/cli.py')['main']
    return {'symbols': expected, 'dispatch': {'provider_sha256': sha(main_text)},
            'positions': lane_runs(top_sequence(frozen, tree), lane_slots(lane))}


def top_sequence(source, tree=None):
    """Top-level statements as position tokens shared by BASE and any split state.

    A definition and any lane's in-place binding of it are the same slot
    (['def', name]); the main guard, moved or not, is ['dispatch']; every other
    statement is identified by its exact text (['stmt', sha256]). Absolute line
    numbers never matter, so other lanes' replacements do not disturb it.
    """
    lines = source.splitlines()
    tokens = []
    for node in (tree or ast.parse(source)).body:
        if isinstance(node, split.DEFINITION):
            tokens.append(['def', node.name])
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
              and ast.unparse(node.value.func) == '_measure_compat.bind'
              and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            tokens.append(['def', node.targets[0].id])
        elif isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            tokens.append(['dispatch'])
        else:
            tokens.append(['stmt', sha('\n'.join(lines[node.lineno - 1:node.end_lineno]))])
    return tokens


def lane_slots(lane):
    return {('def', s['name']) for s in lane['symbols']} | {('dispatch',)}


def lane_runs(tokens, slots):
    """Each maximal run of lane slots with the non-lane neighbours around it."""
    runs, index = [], 0
    while index < len(tokens):
        if tuple(tokens[index]) not in slots:
            index += 1
            continue
        start = index
        while index < len(tokens) and tuple(tokens[index]) in slots:
            index += 1
        runs.append({'before': tokens[start - 1] if start else None,
                     'run': tokens[start:index],
                     'after': tokens[index] if index < len(tokens) else None})
    return runs


def check_positions(scripts, lane, expected):
    """Every lane slot sits between the same non-lane statements as in BASE."""
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    actual = lane_runs(top_sequence(source), lane_slots(lane))
    if actual == expected['positions']:
        return
    for want, have in zip(expected['positions'], actual + [None] * len(expected['positions'])):
        if want != have:
            names = ', '.join(t[1] if len(t) > 1 else t[0] for t in want['run'])
            raise Refusal('binding position differs from frozen BASE (initialization order) near: ' + names)
    raise Refusal('binding position differs from frozen BASE (extra lane run)')


def provider_segments(path):
    text = path.read_text(encoding='utf-8')
    lines = text.split('\n')
    return {n.name: '\n'.join(lines[n.lineno - 1:n.end_lineno])
            for n in ast.parse(text).body if isinstance(n, split.DEFINITION)}


def facade_bindings(source):
    out = {}
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            out[node.targets[0].id] = ast.get_source_segment(source, node)
    return out


def check_moved(scripts, lane, done, cli, expected):
    """Every completed body and binding must equal the frozen derivation."""
    providers = {m: provider_segments(scripts / '_measure' / (m + '.py')) for m in lane['modules']}
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    bindings = facade_bindings(source)
    modules = {s['name']: s['module'] for s in lane['symbols']}
    body_drift, binding_drift = [], []
    for name in done:
        want = expected['symbols'][name]
        if sha(providers[modules[name]][name]) != want['provider_sha256']:
            body_drift.append(name)
        if bindings.get(name) != want['binding']:
            binding_drift.append(name)
    if body_drift:
        raise Refusal('moved body differs from frozen BASE derivation: ' + ', '.join(body_drift))
    if binding_drift:
        raise Refusal('facade binding (defaults/decorators) differs from frozen BASE: ' + ', '.join(binding_drift))
    if cli == 'moved':
        if sha(providers['cli']['main']) != expected['dispatch']['provider_sha256']:
            raise Refusal('moved CLI dispatch differs from frozen BASE derivation')
        if not source.rstrip('\n').endswith(GUARD_TEXT):
            raise Refusal('facade main guard is not the one-line cli.main() call')


def load_record():
    record = json.loads(RECORD.read_text(encoding='utf-8'))
    manifest = split.read_manifest()
    if (record.get('base'), record.get('source_sha256')) != (manifest['base'], manifest['source_sha256']):
        raise Refusal(f'{RECORD.name} was derived from another BASE than the manifest')
    if not {'symbols', 'dispatch', 'positions', 'outputs'} <= record.keys():
        raise Refusal(f'{RECORD.name} predates the position check; regenerate it')
    return record


def expectations(mode):
    if mode in ('auto', 'git'):
        frozen = frozen_source()
        if frozen is not None:
            return derive(frozen, packet()), frozen
        if mode == 'git':
            raise Refusal('manifest BASE commit is not in this clone')
    if not RECORD.exists():
        raise Refusal(f'no frozen BASE in this clone and no {RECORD.name}')
    return load_record(), None


# --- mutation fragment -------------------------------------------------------

def _qualified_region(source, name, module, start, end):
    """Apply the mover's in-region rewrites of `name` to source[start:end]."""
    tree = ast.parse(source)
    table = symtable.symtable(source, 'measure.py', 'exec')
    original = split.Qualifier
    split.Qualifier = RecordingQualifier
    try:
        split.extract_definition(source, name, module, tree=tree, table=table)
    finally:
        split.Qualifier = original
    edits = RecordingQualifier.last.edits
    text = source[start:end]
    # Pure insertions (function preamble, lambda wrapper, nested bindings) are
    # left out; a fragment needing one fails the uniqueness check below.
    for left, right, value in sorted(edits, reverse=True):
        if right > left and start <= left and right <= end:
            text = text[:left - start] + value + text[right - start:]
    return text


def qualify_fragments(source, lane, fragment_path):
    """Return the lane fragment with qualified old/new for still-unmoved targets."""
    owners = {s['name']: s['module'] for s in lane['symbols']}
    fragments = json.loads(fragment_path.read_text(encoding='utf-8'))
    facade = {n.name for n in ast.parse(source).body if isinstance(n, split.DEFINITION)}
    out = []
    for edit in fragments:
        edit = dict(edit)
        name = edit.get('symbol')
        if name in owners and name in facade and edit.get('file') == MEASURE_REL:
            if source.count(edit['old']) != 1:
                raise Refusal(f"fragment old is not unique in measure.py: {edit['old'][:60]!r}")
            start = source.index(edit['old'])
            old = _qualified_region(source, name, owners[name], start, start + len(edit['old']))
            mutated = source[:start] + edit['new'] + source[start + len(edit['old']):]
            new = _qualified_region(mutated, name, owners[name], start, start + len(edit['new']))
            edit['old'], edit['new'] = old, new
        out.append(edit)
    return out


def verify_fragments(scripts, lane, fragments):
    owned = {s['name'] for s in lane['symbols']}
    for edit in fragments:
        if edit.get('symbol') not in owned:
            continue
        path = split.resolve_symbol(scripts, edit['symbol'])
        count = path.read_text(encoding='utf-8').count(edit['old'])
        if count != 1:
            raise Refusal(f"fragment {edit['mutation']!r} edit {edit['edit']}: "
                          f"old occurs {count}x in {path.name}")


# --- authoritative completed-state check: re-run from the foundation ---------

def resolve_foundation(explicit=None):
    """The pinned foundation. Never history, never a tree file."""
    if explicit and explicit != PINNED_FOUNDATION['commit']:
        raise Refusal('--foundation differs from the foundation pinned in this script')
    return PINNED_FOUNDATION['commit']


def check_shared_inputs():
    """The U0 files this lane reads must still be the pinned foundation bytes."""
    drift = [rel for rel in SHARED_INPUTS
             if hashlib.sha256((REPO / rel).read_bytes()).hexdigest() != PINNED_FOUNDATION['inputs_sha256'][rel]]
    if drift:
        raise Refusal('lane input differs from the pinned foundation: ' + ', '.join(drift))


def build_reference(foundation, target):
    """Foundation inputs + this script, moved in scratch. Returns the reference root."""
    script = Path(__file__).read_bytes()
    stamp = {'foundation': foundation, 'script_sha256': hashlib.sha256(script).hexdigest(),
             'inputs_sha256': PINNED_FOUNDATION['inputs_sha256']}
    stamp_path = target / '.u2-reference.json'
    if target.exists():
        if stamp_path.exists() and json.loads(stamp_path.read_text(encoding='utf-8')) == stamp:
            # The stamp proves nothing about the bytes: verify them every time.
            damaged = reference_drift(target)
            if damaged:
                raise Refusal(f'cached reference {target} is damaged (differs from pinned outputs: '
                              + ', '.join(damaged) + '); remove it and re-run')
            return target
        raise Refusal(f'reference dir {target} exists for another foundation/script; remove it first')
    if not ensure_commit(foundation):
        raise Refusal(f'foundation commit {foundation} is not available, even after fetching it')
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=target.parent, prefix=target.name + '.building-'))
    archive = subprocess.run(['git', 'archive', '--format=tar', foundation,
                              'skills/token-optimizer/scripts', 'scripts'], cwd=GIT_REPO,
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            if not (member.isfile() or member.isdir()) or member.name.startswith('/') or '..' in Path(member.name).parts:
                raise Refusal('unexpected member in foundation archive: ' + member.name)
        stream.extractall(work, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    for rel, digest in PINNED_FOUNDATION['inputs_sha256'].items():
        if hashlib.sha256((work / rel).read_bytes()).hexdigest() != digest:
            raise Refusal(f'foundation {foundation[:12]} {rel} does not match its pinned sha256')
    (work / 'scripts/split_lanes').mkdir(parents=True, exist_ok=True)
    (work / 'scripts/split_lanes/u2_move.py').write_bytes(script)
    (work / 'scripts/split_lanes/u2_frozen.json').unlink(missing_ok=True)
    run = subprocess.run([sys.executable, str(work / 'scripts/split_lanes/u2_move.py'), '--no-reference',
                          '--foundation', foundation],
                         cwd=work, env={**os.environ, 'U2_GIT_REPO': str(GIT_REPO)},
                         capture_output=True, text=True)
    if run.returncode:
        raise Refusal(f'foundation re-run failed in {work}:\n{run.stderr or run.stdout}')
    drift = reference_drift(work)
    if drift:
        raise Refusal(f'fresh foundation re-run in {work} differs from PINNED_OUTPUTS: '
                      + ', '.join(drift) + ' (stale pins after a re-cut?)')
    stamp_path_work = work / '.u2-reference.json'
    write_lf(stamp_path_work, json.dumps(stamp) + '\n')
    os.replace(work, target)
    return target


def reference_drift(reference):
    """Reference files whose bytes differ from PINNED_OUTPUTS."""
    return [rel for rel, digest in PINNED_OUTPUTS.items()
            if not (reference / rel).is_file()
            or hashlib.sha256((reference / rel).read_bytes()).hexdigest() != digest]


def reference_mismatches(scripts, reference):
    """Byte-compare every lane-written file with the foundation re-run."""
    pairs = [(scripts / rel, reference / 'skills/token-optimizer/scripts' / rel) for rel in SCRIPT_OUTPUTS]
    pairs += [(REPO / rel, reference / rel) for rel in REPO_OUTPUTS]
    return [(have, want) for have, want in pairs
            if not have.exists() or not want.exists() or have.read_bytes() != want.read_bytes()]


def output_hashes(scripts):
    """Byte hashes of every lane-written file except the record itself."""
    paths = {rel: scripts / rel for rel in SCRIPT_OUTPUTS}
    paths[REPO_OUTPUTS[0]] = REPO / REPO_OUTPUTS[0]
    return {rel: hashlib.sha256(path.read_bytes()).hexdigest() for rel, path in paths.items()}


def check_output_hashes(scripts):
    """Shallow-clone stand-in for the re-run: files unchanged since the recorded move."""
    if not RECORD.exists():
        raise Refusal(f'no {RECORD.name} to compare lane outputs with')
    want = load_record().get('outputs') or {}
    have = output_hashes(scripts)
    changed = sorted(rel for rel in have if want.get(rel) != have[rel])
    if changed or set(want) != set(have):
        raise Refusal('lane output file differs from the recorded move: ' + ', '.join(changed or sorted(set(want) ^ set(have))))


def diagnose(scripts, lane, mode):
    """The finer checks, used only to explain an authoritative mismatch."""
    try:
        expected, _ = expectations(mode)
        check_positions(scripts, lane, expected)
        pending, done = plan(scripts, lane)
        check_moved(scripts, lane, done, dispatch_state(scripts), expected)
        check_output_hashes(scripts)
    except SystemExit as exc:
        return str(exc.code)
    return 'finer checks found nothing more specific'


def check_against_reference(scripts, lane, args):
    foundation = resolve_foundation(args.foundation)
    key = foundation[:12] + '-' + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    target = Path(args.reference).resolve() if args.reference else SCRATCH / 'reference' / key
    reference = build_reference(foundation, target)
    mismatches = reference_mismatches(scripts, reference)
    if mismatches:
        lines = '\n'.join(f'  {have} != {want}' for have, want in mismatches)
        raise Refusal(f'lane output differs from the foundation re-run ({foundation[:12]}):\n{lines}\n'
                      f'diagnosis: {diagnose(scripts, lane, args.frozen)}')
    print(f'U2: every lane-written file is byte-identical to a fresh re-run on foundation {foundation[:12]}')


# --- entry point -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scripts', type=Path, default=SCRIPTS)
    parser.add_argument('--check', action='store_true', help='validate state only; write nothing')
    parser.add_argument('--frozen', choices=('auto', 'git', 'record'), default='auto',
                        help='source for the finer diagnostic checks (default: git, else the record)')
    parser.add_argument('--foundation', help='must equal the pinned foundation commit (kept for explicitness)')
    parser.add_argument('--reference', help='reuse/build the foundation re-run in this directory')
    parser.add_argument('--no-reference', action='store_true',
                        help='skip the authoritative foundation re-run (used by the re-run itself)')
    args = parser.parse_args()
    scripts = args.scripts.resolve()
    real_tree = scripts == SCRIPTS.resolve()
    check_shared_inputs()
    lane = packet()
    fragment_path = REPO / lane['mutation_fragment']
    pending, done = plan(scripts, lane)
    cli = dispatch_state(scripts)
    check_unmoved(scripts, lane, pending, cli)
    print(f'U2: {len(pending)} definitions to move, {len(done)} already moved, dispatch {cli}')

    if not pending and cli == 'moved':
        # "Already applied" means exactly: byte-identical to a fresh re-run of
        # this script on the foundation inputs. The finer checks only explain.
        if args.no_reference:
            expected, _ = expectations(args.frozen)
            check_positions(scripts, lane, expected)
            check_moved(scripts, lane, done, cli, expected)
            check_output_hashes(scripts)
            print('U2: finer checks pass (diagnostic only; no foundation re-run)')
        else:
            check_against_reference(scripts, lane, args)
        return 0

    expected, frozen = expectations(args.frozen)
    check_positions(scripts, lane, expected)
    if done:
        check_moved(scripts, lane, done, cli, expected)
    if args.check:
        print('U2: unmoved bodies, positions and any moved bodies match the frozen BASE')
        return 0
    if real_tree and frozen is None:
        raise Refusal('moving the real tree needs the manifest BASE commit to write the record')
    foundation = resolve_foundation(args.foundation) if real_tree else None
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    fragments = qualify_fragments(source, lane, fragment_path) if real_tree else None
    if pending:
        order = {n.name: n.lineno for n in ast.parse(source).body if isinstance(n, split.DEFINITION)}
        with lf_text_writes():
            split.move(scripts, sorted(pending, key=order.__getitem__))
    if cli == 'facade':
        with lf_text_writes():
            split.move_cli(scripts)
    pending_after, done_after = plan(scripts, lane)
    if pending_after or dispatch_state(scripts) != 'moved':
        raise SystemExit('move incomplete: ' + ', '.join(pending_after))
    check_positions(scripts, lane, expected)
    check_moved(scripts, lane, done_after, 'moved', expected)
    if real_tree:
        verify_fragments(scripts, lane, fragments)
        text = json.dumps(fragments, indent=2, ensure_ascii=False) + '\n'
        if text != fragment_path.read_text(encoding='utf-8'):
            write_lf(fragment_path, text)
        manifest = split.read_manifest()
        record = {**expected, 'base': manifest['base'], 'source_sha256': manifest['source_sha256'],
                  'outputs': output_hashes(scripts), 'foundation': foundation}
        text = json.dumps(record, indent=1, sort_keys=True, ensure_ascii=False) + '\n'
        if not RECORD.exists() or RECORD.read_text(encoding='utf-8') != text:
            write_lf(RECORD, text)
    print(f'U2: moved {len(pending)} definitions'
          + (' and the CLI dispatch' if cli == 'facade' else '')
          + '; bodies, bindings, positions and the dispatch match the frozen BASE')
    if real_tree and not args.no_reference:
        check_against_reference(scripts, lane, args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
