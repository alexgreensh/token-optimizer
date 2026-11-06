#!/usr/bin/env python3
"""Lane U5: move the audit domain out of measure.py, deterministically.

Reads the lane 5 packet from scripts/measure_split_ownership.json, extracts each
named definition from measure.py with U0's scope-aware mover
(scripts/measure_split.py: extract_definition), appends it to
skills/token-optimizer/scripts/_measure/audit.py, and replaces it in place in
measure.py with the same binding statement U0's canaries use.

"Already applied" has one meaning, checked last and decisively: the lane is
rebuilt in a scratch directory (.scratch-u5/cleanroom) from the pristine pre-lane
inputs (measure.py, the audit provider, the mutation fragment file and the two
packet files), by this same script, and EVERY file the lane writes must be
byte-identical to the working tree. Any difference refuses and names the file
and first differing line. The finer checks below remain as diagnostics that give
a specific reason first.

Every frozen input is pinned, independently of the tree being judged, in
scripts/split_lanes/u5_pristine.json: the pre-lane commit and the sha256 of each
pristine input, and the unsplit BASE commit and the sha256 of its measure.py.
The BASE text is NEVER rebuilt from the current measure.py (that would bless any
change made outside the bindings): it is read with git cat-file / show, fetched
by exact SHA (`git fetch --depth=1 origin <sha>` in a shallow checkout) if absent,
hashed against the pin, and if it cannot be obtained the run refuses and writes
nothing. --frozen-source FILE and --pristine REF|DIR can only supply bytes that
hash to the same pins. The pins are rewritten only by --pin.

Idempotent, and never trusting: a definition already moved is not skipped on
sight. Its provider body and its facade binding (captured defaults, keyword
defaults, decorators) are re-derived from the frozen BASE source with the same
extraction and must match text for text, or the run refuses. The frozen source is
`git show <pinned base>:.../measure.py` (or --frozen-source FILE) and must hash
to the pinned frozen_base_sha256. It also refuses (writes nothing) when a
packet symbol is missing, defined twice, or defined in another provider, or when
a not-yet-moved definition differs from its frozen hash, or when the facade's
top-level order (definitions, bindings and every statement both trees share)
differs from the frozen source: a binding moved across the initialization its
captured defaults read would otherwise pass.
Mutation fragments for moved symbols get their `old`/`new` text rewritten to the
qualified provider text, so the same logical mutation still plants.

    python3 scripts/split_lanes/u5_move.py            # apply
    python3 scripts/split_lanes/u5_move.py --check    # report only, write nothing
    python3 scripts/split_lanes/u5_move.py --repo DIR # operate on another checkout
    python3 scripts/split_lanes/u5_move.py --frozen-source FILE  # BASE measure.py from a file (must match the pin)
    python3 scripts/split_lanes/u5_move.py --pristine REF|DIR    # pre-lane inputs from elsewhere (must match the pin)
    python3 scripts/split_lanes/u5_move.py --pin                 # (re)write u5_pristine.json (full history needed)
"""
from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import symtable
import sys
from pathlib import Path

LANE = 5
MODULE = 'audit'
HERE = Path(__file__).resolve()
TOOL_REPO = HERE.parents[2]


def _load_split():
    spec = importlib.util.spec_from_file_location('_u5_measure_split', TOOL_REPO / 'scripts/measure_split.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


split = _load_split()


class Refusal(Exception):
    """The tree is not in a state this lane can move safely. Nothing was written."""


def _read(path):
    """Exact text of a file: no newline translation, so results match on every OS."""
    return Path(path).read_bytes().decode('utf-8')


def _write(path, text):
    Path(path).write_bytes(text.encode('utf-8'))


# ---------------------------------------------------------------- the packet

def load_packet(repo):
    ownership = json.loads(_read(repo / 'scripts/measure_split_ownership.json'))
    lane = ownership['lanes'][str(LANE)]
    if lane['modules'] != [MODULE] or lane.get('dispatch') is not None:
        raise Refusal(f'lane {LANE} packet is not exactly the {MODULE} module: {lane["modules"]}')
    names = [row['name'] for row in lane['symbols']]
    if len(set(names)) != len(names):
        raise Refusal('lane packet lists a symbol twice')
    for row in lane['symbols']:
        if row['module'] != MODULE or row['lane'] != LANE:
            raise Refusal(f'{row["name"]}: packet row is not lane {LANE}/{MODULE}')
    manifest = json.loads(_read(repo / 'scripts/measure_split_manifest.json'))
    frozen = {row['name'] for row in manifest['symbols'] if row['module'] == MODULE}
    if frozen != set(names):
        raise Refusal(f'ownership packet and source manifest disagree: '
                      f'only-packet={sorted(set(names) - frozen)} only-manifest={sorted(frozen - set(names))}')
    return lane, names


# ------------------------------------------------------------- tree analysis

def _bindings(tree):
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == '_measure_compat.bind'):
            out.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    return out


def classify(repo, names):
    """Return (todo, done, facade_tree). Raises Refusal on anything ambiguous."""
    scripts = repo / 'skills/token-optimizer/scripts'
    facade_tree = ast.parse(_read(scripts / 'measure.py'))
    facade_defs, facade_binds = {}, _bindings(facade_tree)
    for node in facade_tree.body:
        if isinstance(node, split.DEFINITION):
            facade_defs.setdefault(node.name, []).append(node)
    provider_defs = {}
    for path in sorted((scripts / '_measure').glob('*.py')):
        if path.name.startswith('_'):
            continue
        for node in ast.parse(_read(path)).body:
            if isinstance(node, split.DEFINITION):
                provider_defs.setdefault(node.name, []).append(path.stem)
    todo, done, problems = [], [], []
    for name in names:
        in_facade = len(facade_defs.get(name, []))
        homes = provider_defs.get(name, [])
        bound = facade_binds.get(name, [])
        if in_facade == 1 and not homes and not bound:
            todo.append(name)
        elif in_facade == 0 and homes == [MODULE] and bound == [f"_measure_providers['{MODULE}'].{name}"]:
            done.append(name)
        elif in_facade == 0 and not homes and not bound:
            problems.append(f'{name}: missing (no body in measure.py or any provider)')
        else:
            problems.append(f'{name}: ambiguous state facade_bodies={in_facade} provider_bodies={homes} bindings={bound}')
    if problems:
        raise Refusal('\n'.join(problems))
    return todo, done, facade_tree


# ------------------------------------------------- the frozen source, trusted

def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def frozen_source_text(git_repo, lock, override=None):
    """The unsplit BASE measure.py named by the pin, hash-checked against the pin.

    Never derived from the working tree. Absent history is fetched by exact SHA;
    if it still cannot be had, refuse.
    """
    if override is not None:
        text = _read(override)
    else:
        sha = lock['frozen_base_commit']
        if not ensure_commit(git_repo, sha):
            raise Refusal(f'cannot obtain the frozen BASE commit {sha}: not in the object store and '
                          f'`git fetch --depth=1 origin {sha}` failed; nothing was judged and nothing was written '
                          f'(pass --frozen-source FILE holding the BASE measure.py)')
        try:
            out = subprocess.run(['git', '-C', str(git_repo), 'show', sha + ':skills/token-optimizer/scripts/measure.py'],
                                 capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise Refusal(f'cannot read the frozen source at {sha[:12]} from git ({exc}); '
                          f'pass --frozen-source FILE') from exc
        text = out.stdout.decode('utf-8')
    if _sha(text) != lock['frozen_base_sha256']:
        raise Refusal('the frozen source does not hash to the pinned frozen_base_sha256; '
                      'refusing to judge anything against it')
    return text


def _definition_text(lines, node):
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return '\n'.join(lines[first - 1:node.end_lineno])


def _is_binding(node):
    return (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == '_measure_compat.bind')


def _order_keys(text):
    """One key per top-level statement; a binding stands for the definition it replaced."""
    seen, keys = {}, []
    for node in ast.parse(text).body:
        if isinstance(node, split.DEFINITION):
            base = ('def', node.name)
        elif _is_binding(node):
            base = ('def', node.targets[0].id)
        else:
            base = ('stmt', ast.get_source_segment(text, node))
        seen[base] = seen.get(base, 0) + 1
        keys.append(base + (seen[base],))
    return keys


def check_order(frozen, current):
    """Refuse when the shared top-level sequence differs from the frozen one.

    Compares only keys present in both trees, so another lane's definition->binding
    replacement, and U0's own additions, do not matter, while a binding that moved
    across a constant, a state write or another definition does.
    """
    frozen_keys, current_keys = _order_keys(frozen), _order_keys(current)
    shared = set(frozen_keys) & set(current_keys)
    expected = [k for k in frozen_keys if k in shared]
    actual = [k for k in current_keys if k in shared]
    if expected == actual:
        return
    index = next(i for i, (a, b) in enumerate(zip(expected, actual)) if a != b)

    def show(key):
        return key[1] if key[0] == 'def' else 'statement ' + repr(key[1][:60])
    raise Refusal(f'top-level initialization order differs from the frozen source at position {index}: '
                  f'expected {show(expected[index])} but found {show(actual[index])}')


def verify_unmoved(repo, todo, frozen):
    """A definition still in the facade must be the frozen one, byte for byte."""
    scripts = repo / 'skills/token-optimizer/scripts'
    text = _read(scripts / 'measure.py')
    manifest = json.loads(_read(repo / 'scripts/measure_split_manifest.json'))
    rows = {row['name']: row for row in manifest['symbols']}
    lines = text.splitlines()
    nodes = {n.name: n for n in ast.parse(text).body if isinstance(n, split.DEFINITION)}
    problems = [f'{name}: unmoved body differs from its frozen hash (edited since the freeze)'
                for name in todo if _sha(_definition_text(lines, nodes[name])) != rows[name]['sha256']]
    if problems:
        raise Refusal('\n'.join(problems))


_FROZEN_PARSES = {}
_FROZEN_EXTRACTIONS = {}


def _frozen_parse(frozen):
    """Parse the frozen source once per process (it is immutable content)."""
    key = _sha(frozen)
    if key not in _FROZEN_PARSES:
        _FROZEN_PARSES[key] = (ast.parse(frozen), symtable.symtable(frozen, 'measure.py', 'exec'))
    return _FROZEN_PARSES[key]


def _frozen_extraction(frozen, name, tree, table):
    key = (_sha(frozen), name)
    if key not in _FROZEN_EXTRACTIONS:
        _FROZEN_EXTRACTIONS[key] = split.extract_definition(frozen, name, MODULE, tree=tree, table=table)
    return _FROZEN_EXTRACTIONS[key]


def validate_completed(repo, done, frozen):
    """Re-derive every completed body and binding from the frozen source.

    Body: the provider definition must equal the mover's output text for the
    frozen definition. Binding: the facade statement (provider symbol, captured
    defaults, keyword defaults, decorators) must equal the mover's binding.
    """
    scripts = repo / 'skills/token-optimizer/scripts'
    manifest = json.loads(_read(repo / 'scripts/measure_split_manifest.json'))
    rows = {row['name']: row for row in manifest['symbols']}
    frozen_tree, frozen_table = _frozen_parse(frozen)
    frozen_lines = frozen.splitlines()
    frozen_nodes = {n.name: n for n in frozen_tree.body if isinstance(n, split.DEFINITION)}
    provider_text = _read(scripts / '_measure' / (MODULE + '.py'))
    provider_lines = provider_text.splitlines()
    provider_nodes = {n.name: n for n in ast.parse(provider_text).body if isinstance(n, split.DEFINITION)}
    facade_text = _read(scripts / 'measure.py')
    facade_bindings = {}
    for node in ast.parse(facade_text).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == '_measure_compat.bind'):
            facade_bindings[node.targets[0].id] = ast.get_source_segment(facade_text, node)
    problems = []
    for name in done:
        node = frozen_nodes.get(name)
        if node is None:
            problems.append(f'{name}: absent from the frozen source')
            continue
        if _sha(_definition_text(frozen_lines, node)) != rows[name]['sha256']:
            problems.append(f'{name}: frozen definition does not hash to the manifest row')
            continue
        moved, binding = _frozen_extraction(frozen, name, frozen_tree, frozen_table)
        actual = '\n'.join(provider_lines[provider_nodes[name].lineno - 1:provider_nodes[name].end_lineno]) + '\n'
        if actual != moved:
            problems.append(f'{name}: completed body drifted from the frozen source plus allowed qualification')
        if facade_bindings.get(name) != binding:
            problems.append(f'{name}: completed binding drifted (provider symbol, captured defaults or decorators): '
                            f'{facade_bindings.get(name)!r} != {binding!r}')
    if problems:
        raise Refusal('\n'.join(problems))


# ----------------------------------------------------------------- the move

class _Recorder:
    """Capture the mover's text edits per definition so fragments can follow them."""
    def __init__(self):
        self.seen = []
        outer = self

        class Recording(split.Qualifier):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.seen.append(self)
        self.cls = Recording

    def __enter__(self):
        self.original = split.Qualifier
        split.Qualifier = self.cls
        return self

    def __exit__(self, *exc):
        split.Qualifier = self.original


def extract_all(repo, todo, facade_tree):
    scripts = repo / 'skills/token-optimizer/scripts'
    source = _read(scripts / 'measure.py')
    table = symtable.symtable(source, 'measure.py', 'exec')
    nodes = {n.name: n for n in facade_tree.body if isinstance(n, split.DEFINITION) and n.name in set(todo)}
    ordered = sorted(todo, key=lambda name: nodes[name].lineno)
    moves = []
    for name in ordered:
        node = nodes[name]
        with _Recorder() as recorder:
            moved, binding = split.extract_definition(source, name, MODULE, tree=facade_tree, table=table)
        qualifier = recorder.seen[-1]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        start = qualifier.offset(node.lineno, node.col_offset)
        end = qualifier.offset(node.end_lineno, node.end_col_offset)
        moves.append({'name': name, 'moved': moved, 'binding': binding, 'first': first, 'last': node.end_lineno,
                      'start': start, 'end': end, 'edits': list(qualifier.edits)})
    return source, moves


def rewrite_facade(source, moves):
    lines = source.splitlines(keepends=True)
    for move in sorted(moves, key=lambda m: m['first'], reverse=True):
        lines[move['first'] - 1:move['last']] = [move['binding'] + '\n']
    return ''.join(lines)


def rewrite_provider(existing, moves):
    text = existing
    for move in moves:
        text = text.rstrip() + '\n\n\n' + move['moved']
    return text


# ------------------------------------------------------- mutation fragments

_STRINGS = r'''("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')'''


def _qualified_text(source, move, a, b):
    """Original source[a:b] as it reads after the mover's edits (strictly inside)."""
    text = source[a:b]
    renamed = {}
    for left, right, value in sorted(move['edits'], reverse=True):
        if left < a < right or left < b < right:
            raise Refusal(f'{move["name"]}: a mutation fragment straddles a rewritten token')
        if not (a <= left and right <= b) or (left == right and left in (a, b)):
            continue
        original = source[left:right]
        if re.fullmatch(r'[A-Za-z_]\w*', original) and value == '_measure_facade.' + original:
            renamed[original] = value
        text = text[:left - a] + value + text[right - a:]
    return text, renamed


def _qualify_new(new, renamed):
    if not renamed:
        return new
    names = '|'.join(sorted(map(re.escape, renamed), key=len, reverse=True))
    pattern = re.compile(_STRINGS + r'|(?<![\w.])(' + names + r')(?!\w)')
    return pattern.sub(lambda m: m.group(0) if m.group(1) is not None else renamed[m.group(2)], new)


def rewrite_fragments(fragments, source, moves):
    """Qualify old/new for fragments whose symbol this run moves."""
    by_name = {m['name']: m for m in moves}
    changed = 0
    out = []
    for fragment in fragments:
        fragment = dict(fragment)
        move = by_name.get(fragment.get('symbol'))
        if move is not None:
            old = fragment['old']
            window = source[move['start']:move['end']]
            at = window.find(old)
            if at < 0:
                raise Refusal(f'{fragment["mutation"]!r}[{fragment["edit"]}]: old text not inside {move["name"]}')
            if source.find(old) != move['start'] + at:
                raise Refusal(f'{fragment["mutation"]!r}[{fragment["edit"]}]: old text first occurs outside {move["name"]}')
            a = move['start'] + at
            qualified_old, renamed = _qualified_text(source, move, a, a + len(old))
            qualified_new = _qualify_new(fragment['new'], renamed)
            if (qualified_old, qualified_new) != (old, fragment['new']):
                changed += 1
            fragment['old'], fragment['new'] = qualified_old, qualified_new
        out.append(fragment)
    return out, changed


def check_fragments(repo, fragments, provider_text):
    """Every fragment must plant inside its own symbol, in the provider, and compile."""
    problems = []
    for fragment in fragments:
        symbol, old = fragment.get('symbol'), fragment['old']
        label = f'{fragment["mutation"]!r}[{fragment["edit"]}]'
        if symbol is None:
            continue
        if symbol not in _provider_symbols(provider_text):
            continue
        at = provider_text.find(old)
        if at < 0:
            problems.append(f'{label}: old text not found in {MODULE}.py')
            continue
        block = _symbol_span(provider_text, symbol)
        if not (block[0] <= at < block[1]):
            problems.append(f'{label}: old text first occurs outside {symbol} in {MODULE}.py')
        mutated = provider_text.replace(old, fragment['new'], 1)
        try:
            compile(mutated, f'{MODULE}.py', 'exec')
        except SyntaxError as exc:
            problems.append(f'{label}: mutated provider does not compile: {exc}')
            continue
        stray = unqualified_globals(mutated)
        if stray:
            problems.append(f'{label}: mutated provider has unqualified globals {sorted(stray)}')
    return problems


def _provider_symbols(text):
    return {n.name for n in ast.parse(text).body if isinstance(n, split.DEFINITION)}


def _symbol_span(text, name):
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    node = next(n for n in ast.parse(text).body if isinstance(n, split.DEFINITION) and n.name == name)
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return offsets[first - 1], offsets[node.end_lineno]


def unqualified_globals(text):
    """Global names a provider function reads other than builtins and _compat.

    Provider bodies reach shared state, and even their own siblings, only through
    the facade, so any other global is a leak (a same-domain call would bypass a
    patch on the facade).
    """
    top = symtable.symtable(text, 'audit.py', 'exec')
    allowed = set(dir(builtins)) | {'_compat'}
    found = set()

    def walk(table):
        for child in table.get_children():
            for symbol in child.get_symbols():
                if symbol.is_global() and symbol.get_name() not in allowed and symbol.is_referenced():
                    found.add(symbol.get_name())
            walk(child)
    walk(top)
    return found


# --------------------------------------------------------------------- main

# ------------------------------------------------ the clean room: the oracle

FACADE = 'skills/token-optimizer/scripts/measure.py'
PROVIDER = f'skills/token-optimizer/scripts/_measure/{MODULE}.py'
PACKET_FILES = ('scripts/measure_split_ownership.json', 'scripts/measure_split_manifest.json')
SELF = 'scripts/split_lanes/u5_move.py'


def lane_files(lane):
    """Every file this lane writes."""
    return (FACADE, PROVIDER, lane['mutation_fragment'])


LOCK = 'scripts/split_lanes/u5_pristine.json'
_SHA = re.compile(r'[0-9a-f]{40}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
LOCK_KEYS = {'pre_lane_commit', 'pre_lane_sha256', 'frozen_base_commit', 'frozen_base_sha256'}


def _git(git_repo, *args, **kwargs):
    return subprocess.run(['git', '-C', str(git_repo), *args], capture_output=True, **kwargs)


def ensure_commit(git_repo, sha):
    """True when `sha` is available locally, fetching it explicitly if it is not.

    CI checks out shallow, so history-dependent inputs (the BASE commit, the
    pinned pre-lane commit) are fetched by exact SHA rather than assumed.
    """
    def present():
        return _git(git_repo, 'cat-file', '-e', sha + '^{commit}').returncode == 0
    if present():
        return True
    if not _SHA.fullmatch(sha):
        return False
    shallow = _git(git_repo, 'rev-parse', '--is-shallow-repository', text=True).stdout.strip() == 'true'
    # Never --depth a full clone: that would mark a shared object store shallow.
    _git(git_repo, 'fetch', '--no-tags', *(['--depth=1'] if shallow else []), 'origin', sha)
    return present()


def pristine_paths(lane):
    """Every pre-lane input the clean room starts from."""
    return (*lane_files(lane), *PACKET_FILES)


def load_lock(repo, lane):
    """The pins, checked for shape. Missing or malformed pins refuse: nothing is frozen without them."""
    path = Path(repo) / LOCK
    if not path.is_file():
        raise Refusal(f'{LOCK} is missing: the frozen inputs are not pinned (run --pin on a full checkout)')
    try:
        lock = json.loads(_read(path))
    except ValueError as exc:
        raise Refusal(f'{LOCK} is not valid JSON: {exc}') from exc
    if not isinstance(lock, dict) or set(lock) != LOCK_KEYS:
        raise Refusal(f'{LOCK} must hold exactly {sorted(LOCK_KEYS)}, got {sorted(lock) if isinstance(lock, dict) else lock!r}')
    for key in ('pre_lane_commit', 'frozen_base_commit'):
        if not isinstance(lock[key], str) or not _SHA.fullmatch(lock[key]):
            raise Refusal(f'{LOCK}: {key} must be a 40-hex commit id, got {lock[key]!r}')
    if not isinstance(lock['frozen_base_sha256'], str) or not _DIGEST.fullmatch(lock['frozen_base_sha256']):
        raise Refusal(f'{LOCK}: frozen_base_sha256 must be a 64-hex sha256')
    hashes = lock['pre_lane_sha256']
    if not isinstance(hashes, dict) or set(hashes) != set(pristine_paths(lane)):
        raise Refusal(f'{LOCK}: pre_lane_sha256 must pin exactly {sorted(pristine_paths(lane))}')
    for relpath, digest in hashes.items():
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise Refusal(f'{LOCK}: pre_lane_sha256[{relpath}] must be a 64-hex sha256')
    return lock


def _derive_pre_lane(git_repo):
    """The commit just before this script was added (only --pin asks, and needs full history)."""
    try:
        out = _git(git_repo, 'log', '--diff-filter=A', '--format=%H', '--', SELF, check=True, text=True).stdout.split()
        if not out:
            raise Refusal(f'{SELF} has no adding commit yet')
        return _git(git_repo, 'rev-parse', out[-1] + '^', check=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Refusal(f'cannot ask git for the pre-lane commit ({exc}); --pin needs a checkout with full history') from exc


def pin(git_repo):
    """Write the pins from git: the pre-lane commit's inputs and the BASE it names."""
    sha = _derive_pre_lane(git_repo)
    if not ensure_commit(git_repo, sha):
        raise Refusal(f'cannot obtain the pre-lane commit {sha}')
    ownership = json.loads(read_pristine(sha, git_repo, 'scripts/measure_split_ownership.json').decode('utf-8'))
    lane = ownership['lanes'][str(LANE)]
    hashes = {relpath: hashlib.sha256(read_pristine(sha, git_repo, relpath)).hexdigest() for relpath in pristine_paths(lane)}
    manifest = json.loads(read_pristine(sha, git_repo, 'scripts/measure_split_manifest.json').decode('utf-8'))
    base = manifest['base']
    if not ensure_commit(git_repo, base):
        raise Refusal(f'cannot obtain the manifest base commit {base}')
    text = _git(git_repo, 'show', base + ':' + FACADE, check=True).stdout.decode('utf-8')
    if _sha(text) != manifest['source_sha256']:
        raise Refusal('the manifest base does not hash to its own source_sha256; refusing to pin')
    lock = {'pre_lane_commit': sha, 'pre_lane_sha256': dict(sorted(hashes.items())),
            'frozen_base_commit': base, 'frozen_base_sha256': manifest['source_sha256']}
    _write(Path(git_repo) / LOCK, json.dumps(lock, indent=2) + '\n')
    return lock


def read_pristine(pristine, git_repo, relpath):
    if isinstance(pristine, Path):
        try:
            return (pristine / relpath).read_bytes()
        except OSError as exc:
            raise Refusal(f'pristine input {relpath} unreadable in {pristine}: {exc}') from exc
    ensure_commit(git_repo, str(pristine))
    try:
        return _git(git_repo, 'show', f'{pristine}:{relpath}', check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Refusal(f'pristine input {relpath} not readable at {pristine}: {exc}') from exc


def pristine_inputs(lane, lock, pristine, git_repo):
    """The pre-lane inputs, wherever they come from, proven equal to the pinned bytes."""
    source = lock['pre_lane_commit'] if pristine is None else pristine
    out = {}
    for relpath in pristine_paths(lane):
        data = read_pristine(source, git_repo, relpath)
        if hashlib.sha256(data).hexdigest() != lock['pre_lane_sha256'][relpath]:
            raise Refusal(f'pristine input {relpath} (from {source}) does not match the sha256 pinned in {LOCK}; '
                          f'refusing to rebuild the lane from anything but the pre-lane bytes')
        out[relpath] = data
    return out


def _first_difference(actual, expected):
    left, right = actual.decode('utf-8', 'replace').splitlines(), expected.decode('utf-8', 'replace').splitlines()
    for number, (a, b) in enumerate(zip(left, right), 1):
        if a != b:
            return number, a, b
    number = min(len(left), len(right)) + 1
    return number, (left[number - 1] if number <= len(left) else '<end of file>'), \
        (right[number - 1] if number <= len(right) else '<end of file>')


_CLEAN_ROOMS = {}


def clean_room(repo, lane, frozen, inputs):
    """Run this same move on the pristine pre-lane inputs; return the files it writes.

    The result depends only on the pristine bytes, the frozen source and this tool,
    so one process computes it once per distinct input set.
    """
    digest = hashlib.sha256()
    for relpath in sorted(inputs):
        digest.update(relpath.encode() + b'\0' + inputs[relpath] + b'\0')
    key = (digest.hexdigest(), _sha(frozen))
    if key not in _CLEAN_ROOMS:
        scratch = repo / '.scratch-u5' / 'cleanroom'
        shutil.rmtree(scratch, ignore_errors=True)
        for relpath, data in inputs.items():
            target = scratch / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        try:
            run(scratch, _frozen=frozen, _oracle=True)
            _CLEAN_ROOMS[key] = {relpath: (scratch / relpath).read_bytes() for relpath in lane_files(lane)}
        finally:
            if not os.environ.get('U5_KEEP_CLEANROOM'):
                shutil.rmtree(scratch, ignore_errors=True)
    return _CLEAN_ROOMS[key]


def compare_with_clean_room(final, expected):
    differences = []
    for relpath, wanted in expected.items():
        if final[relpath] != wanted:
            number, got, want = _first_difference(final[relpath], wanted)
            differences.append(f'{relpath}: differs from the clean-room result at line {number} '
                               f'(working tree {got[:90]!r} vs clean room {want[:90]!r})')
    if differences:
        raise Refusal('the lane is not exactly the mechanical move of the pristine inputs:\n'
                      + '\n'.join(differences))


# --------------------------------------------------------------------- main

def diagnose(repo, todo, done, frozen):
    """Why the finer checks object (messages only; the clean room is the authority)."""
    reasons = []
    for label, check in (('unmoved bodies', lambda: verify_unmoved(repo, todo, frozen)),
                         ('completed bodies and bindings', lambda: validate_completed(repo, done, frozen)),
                         ('top-level order', lambda: check_order(frozen, _read(repo / FACADE)))):
        try:
            check()
        except Refusal as exc:
            reasons.append(f'[{label}] {exc}')
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never hide the real refusal
            reasons.append(f'[{label}] could not be evaluated: {type(exc).__name__}: {exc}')
    return reasons


def run(repo, *, check_only=False, frozen_source=None, pristine=None, git_repo=None, _frozen=None, _oracle=False):
    repo = Path(repo).resolve()
    git_repo = Path(git_repo).resolve() if git_repo else repo
    scripts = repo / 'skills/token-optimizer/scripts'
    lane, names = load_packet(repo)
    lock = None if _oracle else load_lock(repo, lane)
    if lock is not None:
        # The packet this run reads must be the pinned one, and name the pinned BASE.
        for relpath in PACKET_FILES:
            if hashlib.sha256((repo / relpath).read_bytes()).hexdigest() != lock['pre_lane_sha256'][relpath]:
                raise Refusal(f'{relpath} differs from the sha256 pinned in {LOCK}; re-pin only on purpose (--pin)')
        manifest = json.loads(_read(repo / 'scripts/measure_split_manifest.json'))
        if (manifest['base'], manifest['source_sha256']) != (lock['frozen_base_commit'], lock['frozen_base_sha256']):
            raise Refusal(f'the manifest names a BASE other than the one pinned in {LOCK}')
    todo, done, facade_tree = classify(repo, names)
    frozen = _frozen if _frozen is not None else frozen_source_text(git_repo, lock, frozen_source)
    if _oracle:
        # The clean room checks its own pristine inputs eagerly.
        verify_unmoved(repo, todo, frozen)
        validate_completed(repo, done, frozen)
        check_order(frozen, _read(repo / FACADE))
    report = {'packet': len(names), 'moved_now': len(todo), 'already_moved': len(done), 'validated_completed': len(done)}
    fragment_path = repo / lane['mutation_fragment']
    provider_path = scripts / '_measure' / (MODULE + '.py')
    facade_path = scripts / 'measure.py'
    try:
        fragments = json.loads(_read(fragment_path))
        provider_before = _read(provider_path)
        if todo:
            source, moves = extract_all(repo, todo, facade_tree)
            new_facade = rewrite_facade(source, moves)
            new_provider = rewrite_provider(provider_before, moves)
            new_fragments, changed = rewrite_fragments(fragments, source, moves)
            compile(new_facade, str(facade_path), 'exec')
            compile(new_provider, str(provider_path), 'exec')
            stray = unqualified_globals(new_provider)
            if stray:
                raise Refusal(f'moved bodies leak unqualified globals: {sorted(stray)}')
        else:
            new_facade, new_provider, new_fragments, changed = None, provider_before, fragments, 0
        report.update(fragments=len(fragments), fragments_rewritten=changed)
        final = {relpath: (repo / relpath).read_bytes() for relpath in lane_files(lane)}
        if todo:
            final[FACADE] = new_facade.encode('utf-8')
            final[PROVIDER] = new_provider.encode('utf-8')
            final[lane['mutation_fragment']] = (json.dumps(new_fragments, indent=2) + '\n').encode('utf-8')
        if not _oracle:
            # The decisive check: the state this run leaves must equal a from-scratch rebuild.
            if pristine is not None and not isinstance(pristine, Path) and os.path.isdir(str(pristine)):
                pristine = Path(pristine)
            expected = clean_room(repo, lane, frozen, pristine_inputs(lane, lock, pristine, git_repo))
            compare_with_clean_room(final, expected)
            report['clean_room_files'] = len(expected)
        problems = check_fragments(repo, new_fragments, new_provider)
        if problems:
            raise Refusal('\n'.join(problems))
    except Exception as exc:  # noqa: BLE001 - every failure explains itself, then refuses
        if _oracle:
            raise
        reasons = diagnose(repo, todo, done, frozen)
        detail = exc if isinstance(exc, Refusal) else f'{type(exc).__name__}: {exc}'
        raise Refusal('\n'.join([*reasons, str(detail)])) from exc
    if not check_only and todo:
        _write(provider_path, new_provider)
        _write(facade_path, new_facade)
        fragment_path.write_bytes(final[lane['mutation_fragment']])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=TOOL_REPO)
    parser.add_argument('--check', action='store_true', help='validate and report; write nothing')
    parser.add_argument('--frozen-source', type=Path, help='unsplit BASE measure.py (default: git show of the pinned base); must match the pin')
    parser.add_argument('--pin', action='store_true', help='rewrite the pins in u5_pristine.json from git (full history) and exit')
    parser.add_argument('--pristine', help='git ref or directory holding the pre-lane inputs (default: the pinned commit); must match the pins')
    args = parser.parse_args(argv)
    pristine = Path(args.pristine) if args.pristine and os.path.isdir(args.pristine) else args.pristine
    try:
        if args.pin:
            print(json.dumps(pin(args.repo), indent=2, sort_keys=True))
            return 0
        report = run(args.repo, check_only=args.check, frozen_source=args.frozen_source, pristine=pristine)
    except Refusal as exc:
        print(f'REFUSED (nothing written):\n{exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
