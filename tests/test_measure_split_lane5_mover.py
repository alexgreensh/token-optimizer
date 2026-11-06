"""Lane U5 mover tests: "already applied" means byte-equal to a clean-room rebuild.

Split from test_measure_split_lane5.py to keep both files under 1,000 lines. Covers
scripts/split_lanes/u5_move.py: the pinned frozen inputs (BASE commit + sha256,
pre-lane commit + per-file sha256), every class of hole the reviews found, and the
shallow-checkout shape CI runs in (no history, inputs fetched by exact SHA).
"""
import ast
import functools
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
LANE = '5'
split_tree_only = pytest.mark.skipif(bool(os.environ.get('MEASURE_SPLIT_SOURCE')),
                                     reason='structure of the split tree; BASE has no providers')


def packet():
    ownership = json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))
    return ownership['lanes'][LANE]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ------------- the mover's "already applied" means: byte-equal to a clean-room rebuild

SYNTHETIC_MEASURE = """\"\"\"synthetic measure facade\"\"\"
from __future__ import annotations
import functools
import os
from pathlib import Path
LIMIT = 7
TABLE = {"a": 1}


def alpha(x, limit=LIMIT):
    \"\"\"alpha\"\"\"
    try:
        return str(Path(x) / str(TABLE["b"]) / os.sep)
    except KeyError as exc:
        return str(exc)


LIMIT = 11


def beta(y, flag=False, *, extra=LIMIT):
    return alpha(y) if flag else extra


def gamma():
    global TABLE
    TABLE = {"a": 2}
    return TABLE


@functools.lru_cache(maxsize=None)
def delta(n=LIMIT):
    return n + LIMIT
"""


@functools.lru_cache(maxsize=1)
def mover():
    return _load('_u5_move_under_test', REPO / 'scripts/split_lanes/u5_move.py')


FACADE_REL = 'skills/token-optimizer/scripts/measure.py'
PROVIDER_REL = 'skills/token-optimizer/scripts/_measure/audit.py'
FRAGMENT_REL = 'scripts/mutations/_measure_lane5.json'
MANIFEST_REL = 'scripts/measure_split_manifest.json'
OWNERSHIP_REL = 'scripts/measure_split_ownership.json'
LOCK_REL = 'scripts/split_lanes/u5_pristine.json'
PRISTINE_RELS = (FACADE_REL, PROVIDER_REL, FRAGMENT_REL, MANIFEST_REL, OWNERSHIP_REL)
SYNTHETIC_PRE_LANE, SYNTHETIC_BASE = '1' * 40, '2' * 40


def _put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))


def _edit(path, old, new, count=1):
    text = path.read_bytes().decode('utf-8')
    assert text.count(old) == count, (path.name, old, text.count(old))
    _put(path, text.replace(old, new))


def synthetic_world(tmp_path, root='repo', base=SYNTHETIC_BASE):
    """A tiny unsplit facade, its frozen packet/manifest, the BASE file and a pristine copy."""
    import hashlib
    repo = tmp_path / root
    tool = mover()
    _put(repo / FACADE_REL, SYNTHETIC_MEASURE)
    _put(repo / PROVIDER_REL, tool.split.HEADER)
    lines = SYNTHETIC_MEASURE.splitlines()
    rows = []
    for node in ast.parse(SYNTHETIC_MEASURE).body:
        if isinstance(node, ast.FunctionDef):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            rows.append({'name': node.name, 'module': 'audit', 'lane': 5, 'kind': 'FunctionDef',
                         'sha256': hashlib.sha256('\n'.join(lines[first - 1:node.end_lineno]).encode()).hexdigest()})
    _put(repo / 'scripts/measure_split_manifest.json', json.dumps({
        'base': base, 'source_sha256': hashlib.sha256(SYNTHETIC_MEASURE.encode()).hexdigest(),
        'symbols': rows}))
    _put(repo / 'scripts/measure_split_ownership.json', json.dumps({'lanes': {'5': {
        'modules': ['audit'], 'dispatch': None, 'symbols': rows, 'inherited_canaries': [],
        'mutation_fragment': FRAGMENT_REL}}}))
    _put(repo / FRAGMENT_REL, json.dumps([{
        'mutation': 'alpha reads the wrong key', 'edit': 0, 'file': FACADE_REL,
        'old': 'str(TABLE["b"])', 'new': 'str(TABLE["c"])', 'symbol': 'alpha'}], indent=2) + '\n')
    pristine = tmp_path / 'pristine'
    for relpath in PRISTINE_RELS:
        _put(pristine / relpath, (repo / relpath).read_bytes().decode('utf-8'))
    frozen = tmp_path / 'frozen_measure.py'
    _put(frozen, SYNTHETIC_MEASURE)
    _put(repo / LOCK_REL, json.dumps({
        'pre_lane_commit': SYNTHETIC_PRE_LANE,
        'pre_lane_sha256': {rel: hashlib.sha256((pristine / rel).read_bytes()).hexdigest() for rel in sorted(PRISTINE_RELS)},
        'frozen_base_commit': base,
        'frozen_base_sha256': hashlib.sha256(SYNTHETIC_MEASURE.encode()).hexdigest()}, indent=2) + '\n')
    return repo, frozen, pristine


def _snapshot(repo):
    return tuple((repo / relpath).read_bytes() for relpath in (FACADE_REL, PROVIDER_REL, FRAGMENT_REL))


def test_mover_applies_then_revalidates_against_a_clean_room(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    first = tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert (first['moved_now'], first['already_moved'], first['clean_room_files']) == (4, 0, 3)
    provider = (repo / PROVIDER_REL).read_bytes().decode('utf-8')
    assert 'return str(exc)' in provider and '_measure_facade.exc' not in provider
    assert 'str(_measure_facade.TABLE["b"])' in provider
    fragment = json.loads((repo / FRAGMENT_REL).read_bytes())[0]
    assert fragment['old'] == 'str(_measure_facade.TABLE["b"])' and fragment['new'] == 'str(_measure_facade.TABLE["c"])'
    before = _snapshot(repo)
    second = tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert (second['moved_now'], second['already_moved'], second['clean_room_files']) == (0, 4, 3)
    assert _snapshot(repo) == before
    # beta captured the LATER LIMIT, exactly as BASE did
    assert "kwdefaults={'extra': LIMIT}" in (repo / FACADE_REL).read_bytes().decode('utf-8')


def _move_line_before(path, containing, anchor):
    """Move the one line containing `containing` above the line `anchor` (same text, new place)."""
    lines = path.read_bytes().decode('utf-8').splitlines(keepends=True)
    index = [i for i, line in enumerate(lines) if containing in line]
    assert len(index) == 1, (containing, index)
    moved = lines.pop(index[0])
    target = [i for i, line in enumerate(lines) if line.rstrip('\n') == anchor]
    assert len(target) == 1, (anchor, target)
    lines.insert(target[0], moved)
    _put(path, ''.join(lines))


def _append(path, text):
    _put(path, path.read_bytes().decode('utf-8') + text)


# One planted change per class of hole the reviews found, plus the neighbours of each class.
HOLES = [
    ('body edit: handler tuple', lambda r: _edit(r / PROVIDER_REL, 'except KeyError as exc:', 'except (KeyError, ValueError) as exc:'), PROVIDER_REL),
    ('body edit: qualification dropped', lambda r: _edit(r / PROVIDER_REL, 'str(_measure_facade.TABLE["b"])', 'str(TABLE["b"])'), PROVIDER_REL),
    ('body edit: placeholder default replaced', lambda r: _edit(r / PROVIDER_REL, 'def alpha(x, limit=None):', 'def alpha(x, limit=7):'), PROVIDER_REL),
    ('body edit: facade write rewritten as a local', lambda r: _edit(r / PROVIDER_REL, '_measure_facade.TABLE = {"a": 2}', 'TABLE = {"a": 2}'), PROVIDER_REL),
    ('default edit: positional', lambda r: _edit(r / FACADE_REL, "_measure_providers['audit'].alpha, __name__, defaults=(LIMIT,)",
                                                 "_measure_providers['audit'].alpha, __name__, defaults=(8,)"), FACADE_REL),
    ('default edit: keyword', lambda r: _edit(r / FACADE_REL, "kwdefaults={'extra': LIMIT}", "kwdefaults={'extra': 3}"), FACADE_REL),
    ('default edit: dropped', lambda r: _edit(r / FACADE_REL, 'defaults=(False,), ', ''), FACADE_REL),
    ('default edit: decorator argument', lambda r: _edit(r / FACADE_REL, 'lru_cache(maxsize=None)', 'lru_cache(maxsize=1)'), FACADE_REL),
    ('default edit: decorator removed', lambda r: _edit(r / FACADE_REL, ', decorators=(functools.lru_cache(maxsize=None),)', ''), FACADE_REL),
    ('binding moved across the initialization its default reads',
     lambda r: _move_line_before(r / FACADE_REL, "_measure_providers['audit'].beta", 'LIMIT = 11'), FACADE_REL),
    ('binding moved above a constant it captures',
     lambda r: _move_line_before(r / FACADE_REL, "_measure_providers['audit'].alpha", 'LIMIT = 7'), FACADE_REL),
    ('lane bindings reordered',
     lambda r: _move_line_before(r / FACADE_REL, "_measure_providers['audit'].gamma", "beta = _measure_compat.bind(_measure_providers['audit'].beta, __name__, defaults=(False,), kwdefaults={'extra': LIMIT})"), FACADE_REL),
    ('extra statement appended to the provider', lambda r: _append(r / PROVIDER_REL, '\nSTATE = []\n'), PROVIDER_REL),
    ('extra call appended to the provider', lambda r: _append(r / PROVIDER_REL, '\nprint("import-time work")\n'), PROVIDER_REL),
    ('extra statement appended to the facade', lambda r: _append(r / FACADE_REL, '\nSTATE = []\n'), FACADE_REL),
    ('constant changed outside every binding', lambda r: _edit(r / FACADE_REL, 'LIMIT = 7', 'LIMIT = 999'), FACADE_REL),
    ('constant changed between two bindings', lambda r: _edit(r / FACADE_REL, 'LIMIT = 11', 'LIMIT = 999'), FACADE_REL),
    ('mutation fragment edited', lambda r: _edit(r / FRAGMENT_REL, '"new": "str(_measure_facade.TABLE[\\"c\\"])"', '"new": "str(_measure_facade.TABLE[\\"d\\"])"'), FRAGMENT_REL),
]


@pytest.mark.parametrize('label,plant,relpath', HOLES, ids=[h[0] for h in HOLES])
def test_the_clean_room_alone_refuses_every_class_of_hole(tmp_path, monkeypatch, label, plant, relpath):
    """Diagnostics switched off: only the byte comparison with the rebuild stands guard."""
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    plant(repo)
    before = _snapshot(repo)
    for name in ('verify_unmoved', 'validate_completed', 'check_order'):
        monkeypatch.setattr(tool, name, lambda *args, **kwargs: None)
    with pytest.raises(tool.Refusal, match=re.escape(relpath) + ': differs from the clean-room result'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert _snapshot(repo) == before, 'a refusal must write nothing'


@pytest.mark.parametrize('check_only', [False, True], ids=['apply', 'check'])
@pytest.mark.parametrize('label,plant,relpath', HOLES, ids=[h[0] for h in HOLES])
def test_every_hole_is_refused_and_explained_with_the_diagnostics_on(tmp_path, label, plant, relpath, check_only):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    plant(repo)
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal) as raised:
        tool.run(repo, check_only=check_only, frozen_source=frozen, pristine=pristine)
    assert relpath + ': differs from the clean-room result' in str(raised.value)
    assert _snapshot(repo) == before, 'a refusal must write nothing'


DIAGNOSES = [
    ('except KeyError as exc:', 'except (KeyError, ValueError) as exc:', PROVIDER_REL, 'alpha: completed body drifted'),
    ("kwdefaults={'extra': LIMIT}", "kwdefaults={'extra': 3}", FACADE_REL, 'beta: completed binding drifted'),
    ('lru_cache(maxsize=None)', 'lru_cache(maxsize=1)', FACADE_REL, 'delta: completed binding drifted'),
]


@pytest.mark.parametrize('old,new,relpath,message', DIAGNOSES)
def test_the_refusal_names_the_drifted_definition(tmp_path, old, new, relpath, message):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    _edit(repo / relpath, old, new)
    with pytest.raises(tool.Refusal, match=message):
        tool.run(repo, frozen_source=frozen, pristine=pristine)


def test_the_refusal_names_a_binding_moved_across_initialization(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    _move_line_before(repo / FACADE_REL, "_measure_providers['audit'].beta", 'LIMIT = 11')
    with pytest.raises(tool.Refusal, match=r'top-level initialization order differs from the frozen source'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)


def test_a_not_yet_moved_body_that_is_not_the_frozen_one_is_refused(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    _edit(repo / FACADE_REL, 'return n + LIMIT', 'return n - LIMIT')
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal, match='delta: unmoved body differs from its frozen hash'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert _snapshot(repo) == before


def test_a_fresh_move_that_is_not_the_pristine_inputs_plus_the_move_is_refused(tmp_path):
    """Hand edits made before the first run would otherwise be laundered into the lane."""
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    _edit(repo / FACADE_REL, 'TABLE = {"a": 1}', 'TABLE = {"a": 1}\nEXTRA = 1')
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal, match=re.escape(FACADE_REL) + ': differs from the clean-room result'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert _snapshot(repo) == before


def test_a_binding_that_points_at_the_wrong_provider_symbol_is_refused(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    _edit(repo / FACADE_REL, "_measure_providers['audit'].gamma", "_measure_providers['audit'].beta")
    with pytest.raises(tool.Refusal, match='gamma'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)


def test_a_frozen_source_that_is_not_the_pinned_one_is_refused(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    _put(frozen, SYNTHETIC_MEASURE.replace('LIMIT = 7', 'LIMIT = 9'))
    with pytest.raises(tool.Refusal, match='does not hash to the pinned frozen_base_sha256'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)


def test_a_completed_tree_without_a_reachable_frozen_source_is_refused(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    with pytest.raises(tool.Refusal, match='cannot obtain the frozen BASE commit'):
        tool.run(repo, pristine=pristine)


def test_a_missing_pristine_input_is_refused(tmp_path):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    (pristine / PROVIDER_REL).unlink()
    with pytest.raises(tool.Refusal, match='pristine input'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)


def test_the_current_facade_is_never_accepted_as_the_frozen_source(tmp_path):
    """The U4 hole: rebuilding the frozen BASE from the tree under review blesses anything in it."""
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    _edit(repo / FACADE_REL, 'LIMIT = 11', 'LIMIT = 999')
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal, match='does not hash to the pinned frozen_base_sha256'):
        tool.run(repo, frozen_source=repo / FACADE_REL, pristine=pristine)
    undone = tmp_path / 'unsplit_from_the_current_text.py'
    _put(undone, SYNTHETIC_MEASURE.replace('LIMIT = 11', 'LIMIT = 999'))
    with pytest.raises(tool.Refusal, match='does not hash to the pinned frozen_base_sha256'):
        tool.run(repo, frozen_source=undone, pristine=pristine)
    assert _snapshot(repo) == before


def test_a_pristine_override_that_is_not_the_pinned_bytes_is_refused(tmp_path):
    """--pristine cannot launder a tampered tree: a copy of it does not match the pinned hashes."""
    import shutil
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    tool.run(repo, frozen_source=frozen, pristine=pristine)
    _edit(repo / FACADE_REL, 'LIMIT = 11', 'LIMIT = 999')
    laundered = tmp_path / 'laundered'
    for relpath in PRISTINE_RELS:
        (laundered / relpath).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / relpath, laundered / relpath)
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal, match=re.escape(FACADE_REL) + r' \(from .*\) does not match the sha256 pinned'):
        tool.run(repo, frozen_source=frozen, pristine=laundered)
    assert _snapshot(repo) == before


@pytest.mark.parametrize('label,damage,message', [
    ('missing', lambda lock, path: path.unlink(), 'is missing: the frozen inputs are not pinned'),
    ('not json', lambda lock, path: path.write_bytes(b'{'), 'is not valid JSON'),
    ('extra key', lambda lock, path: _put(path, json.dumps({**lock, 'note': 1})), 'must hold exactly'),
    ('short base id', lambda lock, path: _put(path, json.dumps({**lock, 'frozen_base_commit': 'e946a89a'})), 'frozen_base_commit must be a 40-hex'),
    ('symbolic pre-lane ref', lambda lock, path: _put(path, json.dumps({**lock, 'pre_lane_commit': 'HEAD~1'})), 'pre_lane_commit must be a 40-hex'),
    ('short digest', lambda lock, path: _put(path, json.dumps({**lock, 'frozen_base_sha256': 'abc'})), 'frozen_base_sha256 must be a 64-hex'),
    ('input not pinned', lambda lock, path: _put(path, json.dumps({**lock, 'pre_lane_sha256': {FACADE_REL: lock['pre_lane_sha256'][FACADE_REL]}})), 'must pin exactly'),
])
def test_a_missing_or_malformed_pin_refuses_before_anything_is_judged(tmp_path, label, damage, message):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    path = repo / LOCK_REL
    damage(json.loads(path.read_bytes()), path)
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal, match=message):
        tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert _snapshot(repo) == before


@pytest.mark.parametrize('relpath,old,new', [
    (MANIFEST_REL, SYNTHETIC_BASE, '3' * 40),
    (MANIFEST_REL, '"symbols"', '"note": 1, "symbols"'),
    (OWNERSHIP_REL, '"dispatch": null', '"dispatch": null, "note": 1'),
])
def test_a_packet_that_is_not_the_pinned_one_is_refused(tmp_path, relpath, old, new):
    repo, frozen, pristine = synthetic_world(tmp_path)
    tool = mover()
    _edit(repo / relpath, old, new)
    before = _snapshot(repo)
    with pytest.raises(tool.Refusal, match=re.escape(relpath) + ' differs from the sha256 pinned'):
        tool.run(repo, frozen_source=frozen, pristine=pristine)
    assert _snapshot(repo) == before


def _git(*args):
    import subprocess
    return subprocess.run(['git', '-C', str(REPO), *args], capture_output=True)


def real_lock():
    return json.loads((REPO / LOCK_REL).read_text(encoding='utf-8'))


@pytest.fixture(scope='session')
def history():
    """The two commits the real-tree tests read: the pinned BASE and the pinned pre-lane commit.

    CI checks out shallow, so they are fetched by exact SHA. If they cannot be had, a
    developer without network skips, but CI fails: a silent skip there counts as missing.
    """
    tool = mover()
    lock = real_lock()
    wanted = {'pinned BASE commit': lock['frozen_base_commit'], 'pinned pre-lane commit': lock['pre_lane_commit']}
    missing = {label: sha for label, sha in wanted.items() if not tool.ensure_commit(REPO, sha)}
    if missing:
        message = f'cannot obtain {missing} by fetching from origin'
        if os.environ.get('CI'):
            pytest.fail(message + ' (CI must not skip the real-tree checks)')
        pytest.skip(message + ' (offline developer checkout)')
    return wanted


def _real_copy(tmp_path):
    """The five inputs and the pins of the real tree, nothing else: the tree under review."""
    import shutil
    repo = tmp_path / 'real'
    for relpath in (*PRISTINE_RELS, LOCK_REL):
        (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relpath, repo / relpath)
    return repo


def _assert_only_the_facade_differs_from_the_clean_room(refused):
    """On the aggregate the shared facade also carries the other lanes, so it is the one
    file a clean-room rebuild may not reproduce (facade bytes: merge oracle, decisions-U9 D1)."""
    differing = [line for line in str(refused.value).splitlines() if 'differs from the clean-room result' in line]
    assert [line.split(':', 1)[0] for line in differing] == [FACADE_REL], str(refused.value)


@split_tree_only
def test_real_audit_domain_equals_a_clean_room_rebuild_of_the_pre_lane_inputs(tmp_path, history):
    tool = mover()
    with pytest.raises(tool.Refusal, match='not exactly the mechanical move') as refused:
        tool.run(_real_copy(tmp_path), check_only=True, git_repo=REPO)
    _assert_only_the_facade_differs_from_the_clean_room(refused)


KEEPWARM = '_KEEPWARM_PING_BUDGET_USD = 0.10'
REAL_HOLES = [
    ('default edit', lambda r: _edit(r / FACADE_REL, 'defaults=(30, _DETCAND_COACH_BUDGET_S,', 'defaults=(31, _DETCAND_COACH_BUDGET_S,'), FACADE_REL),
    ('default edit: literal', lambda r: _edit(r / FACADE_REL, 'defaults=(5000, None,)', 'defaults=(5001, None,)'), FACADE_REL),
    ('body edit', lambda r: _edit(r / PROVIDER_REL, '    if score >= 90:\n        return "S"', '    if score >= 90:\n        return "S+"'), PROVIDER_REL),
    ('placeholder default', lambda r: _edit(r / PROVIDER_REL, 'def _estimate_messages_until_compact(ctx_window, overhead, avg_msg_tokens=None, model=None):',
                                            'def _estimate_messages_until_compact(ctx_window, overhead, avg_msg_tokens=5000, model=None):'), PROVIDER_REL),
    ('binding moved across the initialization its default reads',
     lambda r: _move_line_before(r / FACADE_REL, "_measure_providers['audit']._deterministic_candidates_data", '_DETCAND_COACH_BUDGET_S = 8.0'), FACADE_REL),
    ('extra statement appended to the provider', lambda r: _append(r / PROVIDER_REL, '\nSTATE = []\n'), PROVIDER_REL),
    ('a $0.10 budget constant outside every binding becomes $999',
     lambda r: _edit(r / FACADE_REL, KEEPWARM, '_KEEPWARM_PING_BUDGET_USD = 999'), FACADE_REL),
]


@split_tree_only
@pytest.mark.parametrize('label,plant,relpath', REAL_HOLES, ids=[h[0] for h in REAL_HOLES])
def test_real_tree_holes_are_refused_by_the_clean_room(tmp_path, monkeypatch, label, plant, relpath, history):
    repo = _real_copy(tmp_path)
    plant(repo)
    tool = mover()
    for name in ('verify_unmoved', 'validate_completed', 'check_order'):
        monkeypatch.setattr(tool, name, lambda *args, **kwargs: None)
    with pytest.raises(tool.Refusal, match=re.escape(relpath) + ': differs from the clean-room result'):
        tool.run(repo, check_only=True, git_repo=REPO)


def _run_git(cwd, *args):
    import subprocess
    # autocrlf off: a Windows runner must not check these fixtures out as CRLF (the move compares raw bytes)
    return subprocess.run(['git', '-c', 'user.name=u5', '-c', 'user.email=u5@example.invalid',
                           '-c', 'commit.gpgsign=false', '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false',
                           *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_a_commit_missing_from_a_shallow_checkout_is_fetched_by_sha(tmp_path):
    """CI's shape, offline: shallow clone, history absent, exact-SHA fetch brings it in."""
    origin = tmp_path / 'origin'
    origin.mkdir()
    _run_git(origin, 'init', '-q')
    (origin / 'inputs.txt').write_bytes(b'pre-lane\n')
    _run_git(origin, 'add', '.')
    _run_git(origin, 'commit', '-q', '-m', 'pre-lane')
    pre_lane = _run_git(origin, 'rev-parse', 'HEAD').stdout.strip()
    (origin / 'inputs.txt').write_bytes(b'lane\n')
    _run_git(origin, 'commit', '-q', '-am', 'lane')
    _run_git(origin, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    shallow = tmp_path / 'shallow'
    _run_git(tmp_path, 'clone', '-q', '--depth=1', origin.as_uri(), str(shallow))
    tool = mover()
    import subprocess
    assert subprocess.run(['git', '-C', str(shallow), 'cat-file', '-e', pre_lane + '^{commit}'], capture_output=True).returncode != 0
    assert tool.read_pristine(pre_lane, shallow, 'inputs.txt') == b'pre-lane\n', 'read_pristine fetches what is missing'
    assert tool.ensure_commit(shallow, pre_lane)
    assert not tool.ensure_commit(shallow, '0' * 40), 'an unknown commit is reported, not invented'
    assert not tool.ensure_commit(shallow, 'HEAD~7'), 'only exact commit ids are fetched'


def test_a_full_checkout_is_never_made_shallow_by_the_fetch(tmp_path):
    origin = tmp_path / 'origin'
    origin.mkdir()
    _run_git(origin, 'init', '-q')
    (origin / 'a.txt').write_bytes(b'a\n')
    _run_git(origin, 'add', '.')
    _run_git(origin, 'commit', '-q', '-m', 'one')
    (origin / 'a.txt').write_bytes(b'b\n')
    _run_git(origin, 'commit', '-q', '-am', 'two')
    later = _run_git(origin, 'rev-parse', 'HEAD').stdout.strip()
    _run_git(origin, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    full = tmp_path / 'full'
    _run_git(tmp_path, 'clone', '-q', origin.as_uri(), str(full))
    (origin / 'a.txt').write_bytes(b'c\n')
    _run_git(origin, 'commit', '-q', '-am', 'three')
    newest = _run_git(origin, 'rev-parse', 'HEAD').stdout.strip()
    assert mover().ensure_commit(full, newest)
    assert _run_git(full, 'rev-parse', '--is-shallow-repository').stdout.strip() == 'false'
    assert later and not (full / '.git' / 'shallow').exists()


def packet_lane():
    return json.loads((REPO / OWNERSHIP_REL).read_text(encoding='utf-8'))['lanes']['5']


def test_the_frozen_inputs_are_pinned_in_the_tree_not_derived_from_history():
    import hashlib
    tool = mover()
    lock = real_lock()
    assert set(lock) == {'pre_lane_commit', 'pre_lane_sha256', 'frozen_base_commit', 'frozen_base_sha256'}
    assert re.fullmatch(r'[0-9a-f]{40}', lock['pre_lane_commit']) and re.fullmatch(r'[0-9a-f]{40}', lock['frozen_base_commit'])
    assert re.fullmatch(r'[0-9a-f]{64}', lock['frozen_base_sha256'])
    assert set(lock['pre_lane_sha256']) == set(PRISTINE_RELS)
    assert tool.load_lock(REPO, packet_lane()) == lock
    manifest = json.loads((REPO / MANIFEST_REL).read_text(encoding='utf-8'))
    assert (manifest['base'], manifest['source_sha256']) == (lock['frozen_base_commit'], lock['frozen_base_sha256'])
    for relpath in (MANIFEST_REL, OWNERSHIP_REL):
        assert hashlib.sha256((REPO / relpath).read_bytes()).hexdigest() == lock['pre_lane_sha256'][relpath]


# ------------------------------- no history: the shape of CI's shallow checkout

def _commit(origin, message):
    _run_git(origin, 'add', '-A')
    _run_git(origin, 'commit', '-q', '-m', message)
    return _run_git(origin, 'rev-parse', 'HEAD').stdout.strip()


def _present(repo, sha):
    import subprocess
    return subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', sha + '^{commit}'], capture_output=True).returncode == 0


@pytest.fixture
def mini_history(tmp_path):
    """Real git history shaped like the lane's: BASE, pre-lane, tooling, pins, result."""
    origin = tmp_path / 'origin'
    origin.mkdir()
    _run_git(origin, 'init', '-q')
    _put(origin / FACADE_REL, SYNTHETIC_MEASURE)
    base = _commit(origin, 'BASE')
    synthetic_world(tmp_path, root='origin', base=base)
    (origin / LOCK_REL).unlink()
    pre_lane = _commit(origin, 'pre-lane inputs')
    _put(origin / 'scripts/split_lanes/u5_move.py', '# the tool is added here\n')
    _commit(origin, 'tooling')
    tool = mover()
    lock = tool.pin(origin)
    pinned = _commit(origin, 'pins')
    _run_git(origin, 'tag', 'pinned')
    tool.run(origin)
    result = _commit(origin, 'result')
    _run_git(origin, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    return SimpleNamespace(origin=origin, base=base, pre_lane=pre_lane, pinned=pinned, result=result, lock=lock,
                           tmp=tmp_path)


def _shallow(history, name, ref=None):
    clone = history.tmp / name
    _run_git(history.tmp, 'clone', '-q', '--depth=1', *(['--branch', ref] if ref else []), history.origin.as_uri(), str(clone))
    assert not _present(clone, history.base) and not _present(clone, history.pre_lane), 'no history in a shallow checkout'
    return clone


def _lane_bytes(repo):
    return {rel: (repo / rel).read_bytes() for rel in (FACADE_REL, PROVIDER_REL, FRAGMENT_REL)}


def test_pin_records_exactly_what_git_says_about_the_two_frozen_inputs(mini_history):
    import hashlib
    lock = mini_history.lock
    assert (lock['pre_lane_commit'], lock['frozen_base_commit']) == (mini_history.pre_lane, mini_history.base)
    assert lock['frozen_base_sha256'] == hashlib.sha256(SYNTHETIC_MEASURE.encode()).hexdigest()
    tool = mover()
    for relpath, digest in lock['pre_lane_sha256'].items():
        assert hashlib.sha256(tool.read_pristine(mini_history.pre_lane, mini_history.origin, relpath)).hexdigest() == digest


def test_without_history_the_pinned_commits_are_fetched_by_sha_and_the_untouched_tree_passes(mini_history):
    clone = _shallow(mini_history, 'clone')
    report = mover().run(clone, check_only=True)
    assert (report['moved_now'], report['already_moved'], report['clean_room_files']) == (0, 4, 3)
    assert _present(clone, mini_history.base) and _present(clone, mini_history.pre_lane), 'fetched by exact SHA'


def test_without_history_a_fresh_move_reproduces_the_committed_result_byte_for_byte(mini_history):
    clone = _shallow(mini_history, 'fresh', ref='pinned')
    report = mover().run(clone)
    assert (report['moved_now'], report['already_moved']) == (4, 0)
    assert _lane_bytes(clone) == _lane_bytes(mini_history.origin)


@pytest.mark.parametrize('label,tamper', [
    ('a constant outside every binding', lambda r: _edit(r / FACADE_REL, 'LIMIT = 11', 'LIMIT = 999')),
    ('a constant the bindings capture', lambda r: _edit(r / FACADE_REL, 'LIMIT = 7', 'LIMIT = 8')),
    ('a statement added', lambda r: _append(r / FACADE_REL, '\nBUDGET = 999\n')),
])
def test_without_history_a_change_outside_the_bindings_is_refused(mini_history, label, tamper):
    """The reviewer's $0.10 -> $999 case: nothing but the pins may define what the tree should be."""
    clone = _shallow(mini_history, 'tampered')
    tamper(clone)
    before = _lane_bytes(clone)
    with pytest.raises(mover().Refusal, match=re.escape(FACADE_REL) + ': differs from the clean-room result'):
        mover().run(clone)
    assert _lane_bytes(clone) == before, 'a refusal must write nothing'


@pytest.mark.parametrize('tampered', [False, True], ids=['untouched tree', 'tampered tree'])
def test_when_the_frozen_base_cannot_be_obtained_nothing_is_judged_and_nothing_written(mini_history, tampered):
    clone = _shallow(mini_history, 'cut_off')
    _run_git(clone, 'remote', 'set-url', 'origin', (mini_history.tmp / 'gone').as_uri())
    if tampered:
        _edit(clone / FACADE_REL, 'LIMIT = 11', 'LIMIT = 999')
    before = _lane_bytes(clone)
    with pytest.raises(mover().Refusal, match='cannot obtain the frozen BASE commit ' + mini_history.base):
        mover().run(clone)
    assert _lane_bytes(clone) == before


def test_without_history_a_frozen_file_rebuilt_from_the_tampered_tree_is_refused(mini_history):
    clone = _shallow(mini_history, 'rebuilt')
    _edit(clone / FACADE_REL, 'LIMIT = 11', 'LIMIT = 999')
    impostor = mini_history.tmp / 'impostor.py'
    _put(impostor, SYNTHETIC_MEASURE.replace('LIMIT = 11', 'LIMIT = 999'))
    with pytest.raises(mover().Refusal, match='does not hash to the pinned frozen_base_sha256'):
        mover().run(clone, frozen_source=impostor)


# The same, on the real tree: a depth-1 clone of this checkout, pinned commits fetched by SHA.

@pytest.fixture(scope='session')
def real_shallow(history, tmp_path_factory):
    tool = mover()
    lock = real_lock()
    clone = tmp_path_factory.mktemp('real_shallow') / 'clone'
    with pytest.MonkeyPatch.context() as env:  # lets the local origin serve a commit by exact id
        env.setenv('GIT_CONFIG_COUNT', '1')
        env.setenv('GIT_CONFIG_KEY_0', 'uploadpack.allowAnySHA1InWant')
        env.setenv('GIT_CONFIG_VALUE_0', 'true')
        _run_git(clone.parent, 'clone', '-q', '--depth=1', '--no-checkout', REPO.as_uri(), str(clone))
        for sha in (lock['frozen_base_commit'], lock['pre_lane_commit']):
            assert not _present(clone, sha), 'a depth-1 clone carries no history'
            assert tool.ensure_commit(clone, sha), f'{sha} must be fetchable by exact SHA'
    return clone


@split_tree_only
def test_real_tree_in_a_shallow_clone_passes_untouched(tmp_path, real_shallow):
    tool = mover()
    with pytest.raises(tool.Refusal, match='not exactly the mechanical move') as refused:
        tool.run(_real_copy(tmp_path), check_only=True, git_repo=real_shallow)
    _assert_only_the_facade_differs_from_the_clean_room(refused)


@split_tree_only
def test_real_tree_in_a_shallow_clone_refuses_a_budget_changed_outside_the_bindings(tmp_path, real_shallow):
    repo = _real_copy(tmp_path)
    _edit(repo / FACADE_REL, KEEPWARM, '_KEEPWARM_PING_BUDGET_USD = 999')
    before = _lane_bytes(repo)
    with pytest.raises(mover().Refusal, match=re.escape(FACADE_REL) + ': differs from the clean-room result'):
        mover().run(repo, git_repo=real_shallow)
    assert _lane_bytes(repo) == before


@split_tree_only
def test_real_tree_with_no_way_to_obtain_the_frozen_base_is_refused(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    _run_git(empty, 'init', '-q')
    repo = _real_copy(tmp_path)
    _edit(repo / FACADE_REL, KEEPWARM, '_KEEPWARM_PING_BUDGET_USD = 999')
    before = _lane_bytes(repo)
    with pytest.raises(mover().Refusal, match='cannot obtain the frozen BASE commit'):
        mover().run(repo, git_repo=empty)
    assert _lane_bytes(repo) == before
