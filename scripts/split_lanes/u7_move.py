#!/usr/bin/env python3
"""Lane U7: move the daemon packet from measure.py into _measure/daemon.py.

Deterministic and re-runnable against whatever measure.py is checked out. The
packet is read from scripts/measure_split_ownership.json (lane 7), never from a
line number, and every body is extracted BY NAME with U0's scope-aware
`measure_split.extract_definition`. Each moved definition is replaced in place
by the binding statement U0's facade pattern uses.

The lane's mutation fragments (scripts/mutations/_measure_lane7.json) are
rewritten in the same run. A fragment's old/new text is a literal replacement
on the unqualified source, so after the move it would no longer match. Each is
re-derived by extracting the function a second time with the mutation applied
to the ORIGINAL source: the qualified old/new pair is exactly the difference
between the two moved bodies, so the mutated daemon.py equals what the mover
would have produced from the mutated measure.py. Nothing is hand-qualified.

One repair on top of U0's extractor: it slices a body to the end of its last
statement, which drops a comment trailing that line. The script re-attaches it
(and refuses any other trailing text), so every body round-trips to its frozen
hash.

"Already applied" means exactly one thing: a fresh run of this move on a copy of
the frozen inputs (the commit the lane branched from, read with git or from a
directory) reproduces every file the lane writes, the facade, the provider and
the fragment file, byte for byte. Any difference is refused with the file and the
first differing line. This is a lane-branch check: a tree that also carries other
lanes' edits to measure.py is the integrator's to regenerate, not to verify here.
The finer checks (bodies restored to their frozen hashes, provider signatures,
full binding text, binding position among every other statement, fragment
placement) stay as diagnostics that name what differs, and they also run on
freshly written files, which are rolled back if they fail.

States per symbol:
  pending  one top-level definition in measure.py, no binding, none in daemon.py
  done     no definition in measure.py, one facade binding, one in daemon.py
Anything else (missing, duplicate, half-moved, source drifted from the frozen
hash, a fragment that no longer lands inside its symbol) is refused before a
byte is written. All pending, or all done (a verified no-op). The three files
are written together and rolled back if any write fails.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
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

LANE = '7'
MODULE = 'daemon'
FRAGMENTS = 'scripts/mutations/_measure_lane7.json'
FACADE = 'skills/token-optimizer/scripts/measure.py'
PROVIDER = f'skills/token-optimizer/scripts/_measure/{MODULE}.py'
# Every file this lane writes. "Already applied" means these equal a fresh move.
LANE_FILES = (FACADE, PROVIDER, FRAGMENTS)
# Everything the move reads: the frozen foundation the lane branched from.
FROZEN_PATHS = (FACADE, 'skills/token-optimizer/scripts/_measure', 'scripts/measure_split.py',
                'scripts/measure_split_manifest.json', 'scripts/measure_split_ownership.json', FRAGMENTS,
                'tests/helpers/measure_split.py')
DEFAULT_ROOT = Path(__file__).resolve().parents[2]
# The U0 commit this lane branched from: the final U0 tip (U0-FINAL), whose facade is on release BASE below.
# Recorded here, not guessed from history, so that a shallow checkout (GitHub CI) can fetch exactly this
# commit by SHA. Reachable from cloud/split-u7 because the lane branch descends from it.
FOUNDATION = '8aa1464db203b78bded10b87f8ecbd2b497f7fad'
# The unsplit release the whole split is measured against (5.13.64) and the sha256 of its measure.py. The
# BASE-versus-candidate proofs read it from git (fetched by SHA if absent) and must find exactly this hash.
BASE = '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f'
BASE_FACADE_SHA256 = 'e675f01b22b12923f412c0404e102faaba94e55fe616987f385b61e5c696bcfc'
# sha256 of every file the move reads, as they are at FOUNDATION (raw bytes; the repo is LF). The frozen
# inputs are what these hashes say, whatever else is checked out or passed with --foundation. Regenerate
# with:  python3.14 scripts/split_lanes/u7_move.py --print-pins --foundation <sha>
PINS = {
    'scripts/measure_split.py':
        'a25e3b66514ffdcc1ed640f7e763af38db1b8b4518b6917dd1295f1a5c11cd73',
    'scripts/measure_split_manifest.json':
        'e2708beba9bbd5671b6e492b564ba19006c4e0b27ba7b9fe38eda8d06e73e73f',
    'scripts/measure_split_ownership.json':
        '04574f5d338a160a3018eaa436b7b1e7f0ddaf03fcd456c5858d7dc787fc9d37',
    'scripts/mutations/_measure_lane7.json':
        '67748660689542e1b94a65d9aa4e29f99a6e16dd03758c3a7ed97419f9ae9a33',
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
    'tests/helpers/measure_split.py':
        'ba108f0161d3bf0405ef969cf1c7a336aa4b185a98f7c14aa170ef36b732db4b',
}


def read_exact(path):
    """The file's bytes as text, with no newline translation: the repository is LF and replays compare bytes."""
    return Path(path).read_bytes().decode('utf-8')


def write_exact(path, text):
    """Write text as UTF-8 bytes with LF kept as LF (write_text would turn it into CRLF on Windows)."""
    Path(path).write_bytes(text.encode('utf-8'))


class MoveRefused(Exception):
    pass


def load_tool(root):
    spec = importlib.util.spec_from_file_location('_u7_measure_split', root / 'scripts/measure_split.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def lane_packet(root, tool):
    ownership = json.loads(read_exact(root / 'scripts/measure_split_ownership.json'))
    packet = ownership['lanes'][LANE]
    if packet['modules'] != [MODULE]:
        raise MoveRefused(f'lane {LANE} owns {packet["modules"]}, expected [{MODULE!r}]')
    frozen = {row['name']: row for row in tool.read_manifest(root / 'scripts/measure_split_manifest.json')['symbols']}
    names = []
    for row in packet['symbols']:
        name = row['name']
        if name in names:
            raise MoveRefused(f'{name}: listed twice in the lane packet')
        if name not in frozen or frozen[name]['module'] != MODULE or row['module'] != MODULE:
            raise MoveRefused(f'{name}: packet and source manifest disagree on the owner')
        if frozen[name]['sha256'] != row['sha256']:
            raise MoveRefused(f'{name}: packet and source manifest disagree on the frozen hash')
        names.append(name)
    return names, frozen


def classify(tool, facade_tree, provider_tree, names):
    facade_defs, bindings, provider_defs = {}, {}, {}
    for node in facade_tree.body:
        if isinstance(node, tool.DEFINITION):
            facade_defs.setdefault(node.name, []).append(node)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == '_measure_compat.bind'):
            expected = f"_measure_providers[{MODULE!r}].{node.targets[0].id}"
            if ast.unparse(node.value.args[0]) == expected:
                bindings.setdefault(node.targets[0].id, []).append(node)
    for node in provider_tree.body:
        if isinstance(node, tool.DEFINITION):
            provider_defs.setdefault(node.name, []).append(node)
    pending, done, errors = [], [], []
    for name in names:
        counts = (len(facade_defs.get(name, [])), len(bindings.get(name, [])), len(provider_defs.get(name, [])))
        if counts == (1, 0, 0):
            pending.append(name)
        elif counts == (0, 1, 1):
            done.append(name)
        else:
            errors.append(f'{name}: facade definitions/bindings/provider bodies = {counts}')
    return pending, done, errors, facade_defs


def extract_recorded(tool, source, name, tree=None, table=None):
    """U0's extractor, plus the exact edit list it applied (source offsets)."""
    seen = []
    base = tool.Qualifier

    class Recording(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self)

    tool.Qualifier = Recording
    try:
        moved, binding = tool.extract_definition(source, name, MODULE, tree=tree, table=table)
    finally:
        tool.Qualifier = base
    qualifier = seen[0]
    # The extractor slices to the end of the last statement, so a comment trailing
    # that line would be lost together with the facade lines it replaces.
    node = qualifier.root
    end = qualifier.offset(node.end_lineno, node.end_col_offset)
    tail = source[end:qualifier.offsets[node.end_lineno]].rstrip('\r\n')
    if tail.strip():
        if not tail.lstrip().startswith('#'):
            raise MoveRefused(f'{name}: unexpected text after the last statement: {tail!r}')
        # A fixed extractor keeps the comment itself; never append it twice.
        if not moved.rstrip('\n').endswith(tail.strip()):
            moved = moved[:-1] + tail + '\n'
    return moved, binding, qualifier


def qualified_fragment(tool, source, node, qualifier, moved, name, fragment):
    """Return the (old, new) pair that applies to the moved body."""
    old, new = fragment['old'], fragment['new']
    start = qualifier.offset(node.lineno, node.col_offset)
    end = qualifier.offset(node.end_lineno, node.end_col_offset)
    first = source.find(old)
    if first < 0 or not start <= first < end or first + len(old) > end:
        raise MoveRefused(f'{name}: fragment {fragment["mutation"]!r}#{fragment["edit"]} is not first found inside '
                          f'its symbol (offset {first}, symbol {start}-{end})')
    edits = [e for e in qualifier.edits if start <= e[0] <= e[1] <= end]
    low, high = first, first + len(old)
    for left, right, _text in edits:
        for boundary in (low, high):
            if left < boundary < right:
                raise MoveRefused(f'{name}: fragment boundary falls inside a qualified expression')

    def moved_offset(point, *, include_insertion_at_point):
        shift = 0
        for left, right, text in edits:
            if right <= point and (include_insertion_at_point or not left == right == point):
                shift += len(text) - (right - left)
        return point - start + shift

    i = moved_offset(low, include_insertion_at_point=True)
    j = moved_offset(high, include_insertion_at_point=False)
    mutated = source[:low] + new + source[high:]
    mutated_moved, _binding, _q = extract_recorded(tool, mutated, name)
    delta = len(mutated_moved) - len(moved)
    if moved[:i] != mutated_moved[:i] or moved[j:] != mutated_moved[j + delta:]:
        raise MoveRefused(f'{name}: fragment changes more than its own span in the moved body')
    return moved[i:j], mutated_moved[i:j + delta], mutated_moved


def symbol_span(tool, text, name):
    node = next(n for n in ast.parse(text).body if isinstance(n, tool.DEFINITION) and n.name == name)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets[node.lineno - 1], offsets[node.end_lineno]


def verify_fragment(tool, provider, fragment):
    """The harness plants old->new on the first occurrence in the symbol's file."""
    name, old = fragment['symbol'], fragment['old']
    index = provider.find(old)
    start, end = symbol_span(tool, provider, name)
    if index < 0 or not start <= index < end or index + len(old) > end:
        raise MoveRefused(f'{name}: fragment {fragment["mutation"]!r}#{fragment["edit"]} does not land in its symbol '
                          f'in {MODULE}.py (offset {index}, symbol {start}-{end})')


def _tokens(entries):
    """(kind, key, occurrence) per top-level statement, in file order."""
    seen, tokens = {}, []
    for key in entries:
        seen[key] = seen.get(key, 0) + 1
        tokens.append(key + (seen[key],))
    return tokens


def position_errors(tool, root, facade_source, facade_tree, names):
    """Each binding must sit where its definition sat, relative to every other statement.

    Statements are compared by identity, never by line number: a definition and the
    binding that replaced it share one token (D, name), the dispatch guard is one
    token, anything else is its text hash. For each of this lane's tokens, every
    token present in both the frozen and the current file must fall on the same side
    of it. That covers state and constants (a captured default must still see its
    initialization and not a later reassignment), other bindings, and the lane's own
    order. Other lanes' mechanical replacements keep their token; statements added or
    edited by U0 exist on one side only and are not compared.
    """
    manifest = tool.read_manifest(root / 'scripts/measure_split_manifest.json')
    owner = {row['end']: row['name'] for row in manifest['symbols']}
    guard_end = (manifest.get('dispatch') or {}).get('end')
    described, frozen_keys = {}, []
    for row in manifest['statements']:
        if row['kind'] in ('FunctionDef', 'AsyncFunctionDef', 'ClassDef') and row['end'] in owner:
            key = ('D', owner[row['end']])
        elif row['kind'] == 'If' and row['end'] == guard_end:
            key = ('G', '')
        else:
            key = ('H', row['sha256'])
        frozen_keys.append(key)
        described.setdefault(key, f'{row["kind"]} at frozen line {row["start"]}' if key[0] != 'D' else key[1])
    lines = facade_source.splitlines()
    current_keys = []
    for node in facade_tree.body:
        if isinstance(node, tool.DEFINITION):
            current_keys.append(('D', node.name))
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == '_measure_compat.bind'):
            current_keys.append(('D', node.targets[0].id))
        elif isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            current_keys.append(('G', ''))
        else:
            current_keys.append(('H', hashlib.sha256('\n'.join(lines[node.lineno - 1:node.end_lineno]).encode()).hexdigest()))
    frozen_at = {token: index for index, token in enumerate(_tokens(frozen_keys))}
    current_at = {token: index for index, token in enumerate(_tokens(current_keys))}
    common = [token for token in frozen_at if token in current_at]
    errors = []
    for name in names:
        mine = ('D', name, 1)
        if mine not in frozen_at or mine not in current_at:
            continue
        crossed = [token for token in common if token != mine
                   and (frozen_at[token] < frozen_at[mine]) != (current_at[token] < current_at[mine])]
        if crossed:
            shown = ', '.join(described[token[:2]] for token in crossed[:3])
            errors.append(f'{name}: binding moved across {len(crossed)} frozen statement(s), e.g. {shown}')
    return errors


def load_helper(root):
    """U0's source-inspection helper: it reverses only the permitted qualification."""
    path = root / 'tests/helpers/measure_split.py'
    if not path.is_file():
        raise MoveRefused(f'cannot verify completed bodies: {path} is missing')
    spec = importlib.util.spec_from_file_location('_u7_measure_split_helper', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    if not hasattr(helper, 'measure_source'):
        raise MoveRefused('cannot verify completed bodies: the helper has no measure_source')
    return helper


def verify_applied(root, tool=None, names=None, frozen=None):
    """Every moved body and its full binding must match the frozen source.

    Body: with the qualification reversed, the slice hashes to the frozen SHA-256.
    Signature: the provider keeps the frozen parameters, annotations and kind, with
    None placeholders for defaults and no decorators. Binding: its text equals what
    the extractor generates for the frozen signature, so defaults, keyword defaults,
    decorators and every other argument are checked, not just the target symbol.
    Position: see position_errors, each binding keeps its place among every other
    statement (state initialization, constants, other bindings).
    """
    tool = tool or load_tool(root)
    if names is None:
        names, frozen = lane_packet(root, tool)
    scripts = root / 'skills/token-optimizer/scripts'
    facade_path, provider_path = scripts / 'measure.py', scripts / '_measure' / (MODULE + '.py')
    facade_source = read_exact(facade_path)
    provider_tree = ast.parse(read_exact(provider_path))
    facade_tree = ast.parse(facade_source)
    bindings = {n.targets[0].id: n for n in facade_tree.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == '_measure_compat.bind'}
    provider = {n.name: n for n in provider_tree.body if isinstance(n, tool.DEFINITION)}
    try:
        restored = load_helper(root).measure_source(facade_path)
        restored_tree = ast.parse(restored)
    except MoveRefused:
        raise
    except Exception as exc:
        raise MoveRefused(f'cannot restore the moved bodies for verification: {exc!r}') from exc
    restored_lines = restored.splitlines()
    restored_defs = {n.name: n for n in restored_tree.body if isinstance(n, tool.DEFINITION)}
    errors = []
    for name in names:
        row = frozen[name]
        node = restored_defs.get(name)
        if node is None:
            errors.append(f'{name}: body cannot be restored from the facade')
        else:
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            digest = hashlib.sha256('\n'.join(restored_lines[first - 1:node.end_lineno]).encode()).hexdigest()
            if digest != row['sha256']:
                errors.append(f'{name}: body differs from the frozen source')
        moved, binding = tool.extract_definition(row['signature'] + '\n    pass\n', name, MODULE)
        expected = ast.parse(moved).body[0]
        actual = provider.get(name)
        if actual is None or (type(actual), ast.dump(actual.args), ast.dump(actual.returns) if actual.returns else None,
                              actual.decorator_list) != (type(expected), ast.dump(expected.args),
                                                         ast.dump(expected.returns) if expected.returns else None, []):
            errors.append(f'{name}: provider signature differs from the frozen signature')
        node = bindings.get(name)
        if node is None or ast.get_source_segment(facade_source, node) != binding:
            errors.append(f'{name}: facade binding differs from the frozen signature (defaults, decorators or arguments)')
    errors += position_errors(tool, root, facade_source, facade_tree, names)
    if errors:
        raise MoveRefused(f'{len(errors)} completed definition(s) differ from the frozen source:\n  '
                          + '\n  '.join(errors[:20]) + ('\n  ...' if len(errors) > 20 else ''))
    return len(names)


def foundation_rev(root):
    """The commit the lane branched from: merge-base of HEAD and U0's branch."""
    explicit = os.environ.get('U7_FOUNDATION')
    if explicit:
        return explicit
    git = shutil.which('git')
    if git:
        proc = subprocess.run([git, '-C', str(root), 'merge-base', 'HEAD', 'origin/cloud/split-u0'],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise MoveRefused('cannot find the frozen foundation commit (merge-base of HEAD and origin/cloud/split-u0); '
                      'pass --foundation REV or DIR, or set U7_FOUNDATION')


def ensure_commit(root, rev):
    """Make a commit's objects readable: already there in a full clone, fetched by SHA in a shallow one.

    A shallow checkout (GitHub CI) has no history, so a rev that is not present is fetched explicitly,
    one commit deep. If that fails the caller is told why; nothing is skipped or guessed.
    """
    git = shutil.which('git')
    if not git:
        raise MoveRefused('git is not available')

    def present():
        return subprocess.run([git, '-C', str(root), 'cat-file', '-e', f'{rev}^{{commit}}'],
                              capture_output=True).returncode == 0
    if present():
        return
    print(f'u7_move: {rev} is not in this checkout (shallow clone?); fetching it by SHA', file=sys.stderr)
    fetched = subprocess.run([git, '-C', str(root), 'fetch', '--depth=1', '--no-tags', '--quiet', 'origin', rev],
                             capture_output=True, text=True)
    if not present():
        raise MoveRefused(f'cannot read the foundation {rev}: it is not in this checkout and fetching it from '
                          f'origin failed ({(fetched.stderr or fetched.stdout).strip() or "no output"})')


def fetch_frozen(root, foundation, destination):
    """Copy the frozen inputs, from a git revision or a directory, into destination. Unverified."""
    if isinstance(foundation, Path):
        for rel in FROZEN_PATHS:
            source, target = foundation / rel, destination / rel
            if not source.exists():
                raise MoveRefused(f'the foundation {foundation} lacks {rel}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(source, target)
        return
    git = shutil.which('git')
    if not git:
        raise MoveRefused('cannot rebuild the frozen inputs: git is not available')
    ensure_commit(root, foundation)
    proc = subprocess.run([git, '-C', str(root), 'archive', '--format=tar', foundation, *FROZEN_PATHS],
                          capture_output=True)
    if proc.returncode:
        raise MoveRefused(f'cannot read the foundation {foundation}: {proc.stderr.decode(errors="replace").strip()}')
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as stream:
        for member in stream:
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts:
                raise MoveRefused(f'unsafe archive member {member.name}')
            if member.isdir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                (destination / relative).parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source:
                    (destination / relative).write_bytes(source.read())


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def pins_for(directory):
    """sha256 of every file the move reads, keyed by repository path."""
    directory = Path(directory)
    found = {}
    for rel in FROZEN_PATHS:
        path = directory / rel
        if path.is_dir():
            files = [f for f in path.rglob('*') if f.is_file() and '__pycache__' not in f.parts and f.suffix != '.pyc']
        else:
            files = [path] if path.is_file() else []
        for file in files:
            found[file.relative_to(directory).as_posix()] = _sha256(file.read_bytes())
    return dict(sorted(found.items()))


def verify_pins(directory, pins):
    """Refuse unless the frozen inputs are exactly the pinned ones (no more, no fewer, no different)."""
    actual = pins_for(directory)
    problems = []
    for rel in sorted(set(pins) | set(actual), key=lambda r: (r != FACADE, r)):
        if rel not in actual:
            problems.append(f'{rel}: missing from the frozen inputs')
        elif rel not in pins:
            problems.append(f'{rel}: is not one of the pinned frozen inputs')
        elif actual[rel] != pins[rel]:
            problems.append(f'{rel}: sha256 {actual[rel][:16]}... is not the pinned {pins[rel][:16]}...')
    if problems:
        raise MoveRefused('the frozen inputs are not the pinned ones; nothing was written:\n  ' + '\n  '.join(problems))


def materialize(root, foundation, destination, pins):
    """Obtain the frozen inputs and prove they are the pinned ones, or refuse. Never trusts the checkout."""
    fetch_frozen(root, foundation, destination)
    verify_pins(destination, pins)


def rebuild_expected(root, foundation, scratch_parent, pins):
    """Run this lane's own move on a copy of the frozen inputs; return what it writes."""
    foundation = foundation_rev(root) if foundation is None else foundation
    parent = Path(scratch_parent) if scratch_parent else root / '.scratch-u7'
    parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='u7-rebuild-', dir=parent))
    try:
        materialize(root, foundation, scratch, pins)
        found = examine(scratch)
        if found['state'] != 'pending':
            raise MoveRefused(f'the foundation {foundation} already has the packet applied; it cannot be the frozen input')
        apply(scratch, found['result'])
        return {rel: read_exact(scratch / rel) for rel in LANE_FILES}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def planned_texts(result):
    """What apply() would write for a pending packet."""
    return {FACADE: result['facade'], PROVIDER: result['provider'],
            FRAGMENTS: json.dumps(result['fragments'], indent=2, ensure_ascii=False) + '\n'}


def disk_texts(root):
    return {rel: (root / rel).read_bytes().decode('utf-8') for rel in LANE_FILES}


def byte_differences(have_texts, expected):
    """One entry per lane file whose bytes differ from the fresh move. Exact: CRLF is not LF."""
    found = []
    for rel in LANE_FILES:
        want, have = expected[rel], have_texts[rel]
        if want == have:
            continue
        a, b = want.splitlines(), have.splitlines()
        if a == b:
            found.append(f'{rel}: differs from a fresh move of the frozen inputs in line endings only '
                         f'(the repository requires LF)')
            continue
        line = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        left = a[line][:100] if line < len(a) else '<end of file>'
        right = b[line][:100] if line < len(b) else '<end of file>'
        found.append(f'{rel}: differs from a fresh move of the frozen inputs at line {line + 1} '
                     f'(expected {left!r}, found {right!r}; {len(a)} vs {len(b)} lines)')
    return found


def examine(root, tool=None):
    """Read the lane's files, refuse any state that is not cleanly pending or cleanly done, and for a pending
    packet compute the move from them. Writes nothing."""
    tool = tool or load_tool(root)
    scripts = root / 'skills/token-optimizer/scripts'
    names, frozen = lane_packet(root, tool)
    facade_path = scripts / 'measure.py'
    provider_path = scripts / '_measure' / (MODULE + '.py')
    fragments_path = root / FRAGMENTS
    source = read_exact(facade_path)
    existing = read_exact(provider_path)
    fragments = json.loads(read_exact(fragments_path))
    facade_tree, provider_tree = ast.parse(source), ast.parse(existing)
    pending, done, errors, facade_defs = classify(tool, facade_tree, provider_tree, names)
    if errors:
        raise MoveRefused('refusing, symbols not in a clean state:\n  ' + '\n  '.join(errors))
    if pending and done:
        raise MoveRefused(f'refusing a half-applied packet: {len(done)} done, {len(pending)} pending')
    stray = [n.name for n in provider_tree.body if isinstance(n, tool.DEFINITION) and n.name not in set(names)]
    if stray:
        raise MoveRefused(f'{MODULE}.py carries definitions outside the lane packet: {stray}')
    outside = sorted({f['symbol'] for f in fragments} - set(names))
    if outside:
        raise MoveRefused(f'{FRAGMENTS} targets symbols outside lane {LANE}: {outside}')
    if not pending:
        return {'state': 'done', 'tool': tool, 'names': names, 'frozen': frozen, 'source': source,
                'existing': existing, 'fragments': fragments, 'done': len(done)}
    lines = source.splitlines()
    for name in pending:
        node = facade_defs[name][0]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        digest = hashlib.sha256('\n'.join(lines[first - 1:node.end_lineno]).encode()).hexdigest()
        if digest != frozen[name]['sha256']:
            raise MoveRefused(f'{name}: facade source differs from the frozen hash (re-freeze the manifest first)')
    pending.sort(key=lambda name: facade_defs[name][0].lineno)
    table = symtable.symtable(source, 'measure.py', 'exec')
    provider = existing
    changes, bodies = [], {}
    for name in pending:
        moved, binding, qualifier = extract_recorded(tool, source, name, tree=facade_tree, table=table)
        bodies[name] = (moved, qualifier)
        provider = provider.rstrip() + '\n\n\n' + moved
        node = facade_defs[name][0]
        changes.append((min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno, binding))
    rewritten = []
    for fragment in fragments:
        name = fragment['symbol']
        moved, qualifier = bodies[name]
        old, new, mutated_moved = qualified_fragment(tool, source, facade_defs[name][0], qualifier, moved, name, fragment)
        if provider.replace(old, new, 1) != provider.replace(moved, mutated_moved, 1):
            raise MoveRefused(f'{name}: planted fragment is not the mutated mover output')
        rewritten.append({**fragment, 'old': old, 'new': new})
    for fragment in rewritten:
        verify_fragment(tool, provider, fragment)
    out = source.splitlines(keepends=True)
    removed = 0
    for first, last, binding in sorted(changes, reverse=True):
        removed += last - first + 1
        out[first - 1:last] = [binding + '\n']
    facade = ''.join(out)
    compile(provider, str(provider_path), 'exec')
    compile(facade, str(facade_path), 'exec')
    return {'state': 'pending', 'result': {'facade': facade, 'provider': provider, 'fragments': rewritten,
            'moved': pending, 'already': 0, 'removed_lines': removed}}


def plan(root, tool=None, foundation=None, scratch_parent=None, pins=None):
    """What to do to the lane's files, proven against a fresh move of the PINNED frozen inputs.

    Both states are compared with that fresh move: a pending packet must plan exactly its bytes, an applied
    one must already hold them. The frozen inputs come from the foundation commit (fetched by SHA if need
    be) or a directory, and must hash to the pins; if they cannot be obtained or do not, nothing is written.
    """
    pins = PINS if pins is None else pins
    found = examine(root, tool)
    expected = rebuild_expected(root, foundation, scratch_parent, pins)
    if found['state'] == 'pending':
        problems = byte_differences(planned_texts(found['result']), expected)
        if problems:
            raise MoveRefused('the planned move is not what a fresh move of the pinned frozen inputs produces; '
                              'nothing was written:\n  ' + '\n  '.join(problems))
        return found['result']
    tool, names, frozen = found['tool'], found['names'], found['frozen']
    existing, fragments = found['existing'], found['fragments']
    # Authoritative: a fresh move of the frozen inputs must reproduce every lane file byte for byte.
    # The finer checks below name what differs; they never replace it.
    problems = byte_differences(disk_texts(root), expected)
    verified = 0
    try:
        verified = verify_applied(root, tool, names, frozen)
    except MoveRefused as exc:
        problems.append(str(exc))
    for fragment in fragments:
        try:
            verify_fragment(tool, existing, fragment)
        except MoveRefused as exc:
            problems.append(str(exc))
    if problems:
        raise MoveRefused('the applied tree is not what a fresh move of the frozen inputs produces:\n  '
                          + '\n  '.join(problems))
    return {'facade': found['source'], 'provider': existing, 'fragments': fragments, 'moved': [],
            'already': found['done'], 'verified': verified, 'rebuilt': list(LANE_FILES), 'removed_lines': 0}


def apply(root, result):
    scripts = root / 'skills/token-optimizer/scripts'
    targets = [(scripts / '_measure' / (MODULE + '.py'), result['provider']),
               (root / FRAGMENTS, json.dumps(result['fragments'], indent=2, ensure_ascii=False) + '\n'),
               (scripts / 'measure.py', result['facade'])]
    before = [(path, path.read_bytes()) for path, _ in targets]
    try:
        for path, text in targets:
            write_exact(path, text)
        verify_applied(root)
    except BaseException:
        for path, data in before:
            path.write_bytes(data)
        raise


def print_pins(root, foundation):
    """Print the FOUNDATION and PINS lines for a foundation, to paste into this script. Pins nothing itself."""
    foundation = Path(foundation).resolve() if Path(foundation).is_dir() else foundation
    parent = root / '.scratch-u7'
    parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='u7-pins-', dir=parent))
    try:
        fetch_frozen(root, foundation, scratch)
        pins = pins_for(scratch)
        rev = foundation if isinstance(foundation, Path) else subprocess.run(
            ['git', '-C', str(root), 'rev-parse', foundation + '^{commit}'], capture_output=True, text=True,
            check=True).stdout.strip()
    except MoveRefused as exc:
        print(f'u7_move: {exc}', file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"FOUNDATION = '{rev}'")
    print('PINS = {')
    for rel, digest in pins.items():
        print(f"    '{rel}':\n        '{digest}',")
    print('}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT, help='repository root (default: this checkout)')
    parser.add_argument('--check', action='store_true', help='report the state, write nothing')
    parser.add_argument('--foundation', help='git revision or directory holding the frozen inputs (default: '
                        '$U7_FOUNDATION, else FOUNDATION in this script for this checkout, else the merge-base '
                        'of HEAD and origin/cloud/split-u0)')
    parser.add_argument('--scratch', type=Path, help='parent for the throwaway rebuild (default: <root>/.scratch-u7)')
    parser.add_argument('--print-pins', action='store_true',
                        help='print FOUNDATION and PINS for --foundation REV (or the recorded FOUNDATION), then stop')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    foundation = args.foundation or os.environ.get('U7_FOUNDATION') or (FOUNDATION if root == DEFAULT_ROOT else None)
    if foundation and Path(foundation).is_dir():
        foundation = Path(foundation).resolve()
    if args.print_pins:
        return print_pins(root, foundation or FOUNDATION)
    try:
        result = plan(root, foundation=foundation, scratch_parent=args.scratch)
    except MoveRefused as exc:
        print(f'u7_move: {exc}', file=sys.stderr)
        return 2
    if result['moved']:
        print(f'u7_move: {len(result["moved"])} definitions to move, {result["removed_lines"]} facade lines replaced, '
              f'{len(result["fragments"])} mutation fragments to qualify')
        if args.check:
            return 1
        try:
            apply(root, result)
        except MoveRefused as exc:
            print(f'u7_move: the written tree failed verification and was rolled back: {exc}', file=sys.stderr)
            return 2
        print(f'u7_move: moved {len(result["moved"])} definitions into _measure/{MODULE}.py and verified them')
    else:
        print(f'u7_move: already applied ({result["already"]} definitions in _measure/{MODULE}.py; '
              f'a fresh move of the frozen inputs reproduces {len(result["rebuilt"])} files byte for byte; '
              f'{result["verified"]} bodies and bindings match the frozen source; '
              f'{len(result["fragments"])} fragments land in their symbols), no change')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
