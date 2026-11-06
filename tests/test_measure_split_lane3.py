"""Lane U3: the savings domain lives in _measure/savings.py behind the facade.

These run on the split tree. They prove ownership, unchanged frozen source,
live facade lookups (patches, constants, imported modules, global writes),
captured defaults, a stateless provider, and that lane 3's mutation fragments
still plant inside their own moved bodies.
"""
import ast
import hashlib
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure, measure_source

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
PROVIDER = SCRIPTS / '_measure/savings.py'
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _packet():
    return json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))['lanes']['3']


def _names():
    return [row['name'] for row in _packet()['symbols']]


def _defs(path):
    return [n.name for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n, DEFINITION)]


@pytest.fixture
def m(tmp_path):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home,
                               'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        yield module


def test_every_lane3_symbol_has_one_body_in_savings_and_one_binding():
    names = _names()
    assert len(names) == 134 and len(set(names)) == 134
    facade = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    facade_defs = {n.name for n in facade.body if isinstance(n, DEFINITION)}
    bound = {}
    for node in facade.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            bound.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    provider = _defs(PROVIDER)
    assert sorted(provider) == sorted(names)
    assert not facade_defs & set(names)
    for name in names:
        assert bound[name] == [f"_measure_providers['savings'].{name}"], name


def test_provider_keeps_original_source_order():
    order = {row['name']: row['start'] for row in _packet()['symbols']}
    provider = _defs(PROVIDER)
    assert provider == sorted(provider, key=order.__getitem__)


def test_inspection_view_reproduces_every_frozen_base_slice():
    """Unqualifying the moved bodies gives back the exact frozen BASE text."""
    source = measure_source(SCRIPTS / 'measure.py')
    lines = source.splitlines()
    nodes = {n.name: n for n in ast.parse(source).body if isinstance(n, DEFINITION)}
    for row in _packet()['symbols']:
        node = nodes[row['name']]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        text = '\n'.join(lines[first - 1:node.end_lineno])
        assert hashlib.sha256(text.encode()).hexdigest() == row['sha256'], row['name']


def test_provider_is_definition_only_with_no_copied_state():
    tree = ast.parse(PROVIDER.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, DEFINITION):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        assert isinstance(node, ast.ImportFrom), ast.dump(node)[:120]
        assert (node.module, node.level, [a.name for a in node.names]) in (
            ('__future__', 0, ['annotations']), (None, 1, ['_compat']))
    for node in ast.walk(tree):
        # A remaining `global` would bind provider-module state.
        assert not isinstance(node, ast.Global), node.lineno


def test_facade_exports_are_fresh_provider_functions_with_frozen_signatures(m):
    provider = sys.modules['_measure.savings']
    for row in _packet()['symbols']:
        live = getattr(m, row['name'])
        assert Path(live.__code__.co_filename).resolve() == PROVIDER.resolve(), row['name']
        assert live.__module__ == m.__name__ and live.__qualname__ == row['name']
        assert getattr(provider, row['name']) is live
        namespace = dict(m.__dict__)
        exec(compile(row['signature'] + '\n    pass\n', 'frozen-signature', 'exec'), namespace)
        frozen = inspect.signature(namespace[row['name']])
        actual = inspect.signature(live)
        assert [(p.name, p.kind, p.default) for p in frozen.parameters.values()] == \
               [(p.name, p.kind, p.default) for p in actual.parameters.values()], row['name']


def test_provider_module_holds_no_business_attributes(m):
    provider = sys.modules['_measure.savings']
    extra = {name for name, value in vars(provider).items()
             if not name.startswith('__') and name != '_compat' and not inspect.isfunction(value)}
    assert extra == {'annotations'} or not extra
    assert not hasattr(provider, '_subagent_pool_sidecar_memo')


def test_same_domain_patch_and_undo_reach_moved_caller(m, monkeypatch):
    pool = {'transformation_usd': 100.0, 'crosscheck': {'transformation_usd': 101.0}}
    assert m._crosscheck_verdict(pool) is True
    fake = Mock(return_value=(False, None))
    with monkeypatch.context() as patch:
        patch.setattr(m, '_crosscheck_agrees', fake)
        assert m._crosscheck_verdict(pool) is False
        fake.assert_called_once_with(100.0, 101.0)
    assert m._crosscheck_verdict(pool) is True
    assert fake.call_count == 1


def test_cross_domain_caller_sees_patched_savings_helper(m, monkeypatch):
    import datetime as real
    calls = []
    monkeypatch.setattr(m, '_install_date', lambda: calls.append(1) or '2020-01-01')
    expected = max(90, min(3650, (real.datetime.now() - real.datetime(2020, 1, 1)).days + 2))
    assert expected not in (90, 3650)
    assert m._upgrade_drain_days() == expected and calls == [1]
    today = real.datetime.now().date().isoformat()
    monkeypatch.setattr(m, '_install_date', lambda: today)
    assert m._upgrade_drain_days() == 90


def test_moved_body_reads_rebound_facade_paths(m, tmp_path, monkeypatch):
    snapshots = tmp_path / 'snapshots'
    snapshots.mkdir()
    (snapshots / 'snapshot_before.json').write_bytes(json.dumps({'timestamp': '2026-02-03T04:05:06'}).encode())
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', snapshots)
    assert m._install_date() == '2026-02-03'


def test_moved_body_reads_live_constants_and_imported_modules(m, monkeypatch):
    pool = {'transformation_usd': 4.0, 'crosscheck': {'transformation_usd': 4.0}}
    monkeypatch.setattr(m, '_TRANSFORMATION_FLAT_BAND', 0.05)
    assert m._crosscheck_flat(pool, 100.0) is True
    monkeypatch.setattr(m, '_TRANSFORMATION_FLAT_BAND', 0.01)
    assert m._crosscheck_flat(pool, 100.0) is False
    assert m._crosscheck_agrees(10.0, 10.5) == (True, 4.8)
    fake_math = type('FakeMath', (), {'isfinite': staticmethod(lambda value: False)})
    monkeypatch.setattr(m, 'math', fake_math)
    assert m._crosscheck_agrees(10.0, 10.5) == (False, None)


def test_defaults_are_captured_at_the_original_facade_position(m, monkeypatch):
    captured = m._read_subagent_pool_sidecar.__defaults__
    assert captured == (m._SUBAGENT_POOL_TTL,)
    monkeypatch.setattr(m, '_SUBAGENT_POOL_TTL', -1)
    assert m._read_subagent_pool_sidecar.__defaults__ == captured
    assert m._update_counted_cumulative.__defaults__ == (
        m._COUNTED_MAX_SESSIONS_PER_PASS, m._COUNTED_PASS_DEADLINE_SECONDS, True)
    assert m._winsorized_mean_session.__defaults__ == (m._BASELINE_WINSOR_PCT,)


def test_sidecar_memo_global_writes_land_on_the_facade(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', tmp_path)
    monkeypatch.setattr(m, '_subagent_pool_sidecar_memo', None)
    key = (30, 0.5, 'claude', 'anthropic')
    m._write_subagent_pool_sidecar(key, {'saved_usd': 1.25})
    assert (tmp_path / 'subagent_pool.json').exists()
    assert m._read_subagent_pool_sidecar(key) == {'saved_usd': 1.25}
    memo = m._subagent_pool_sidecar_memo
    assert isinstance(memo, tuple) and m._subagent_pool_key_str(key) in memo[1]
    assert not hasattr(sys.modules['_measure.savings'], '_subagent_pool_sidecar_memo')
    sentinel = (memo[0], {m._subagent_pool_key_str(key): {'saved_usd': 9.0, '_cached_ts': memo[1][m._subagent_pool_key_str(key)]['_cached_ts']}})
    monkeypatch.setattr(m, '_subagent_pool_sidecar_memo', sentinel)
    assert m._read_subagent_pool_sidecar(key) == {'saved_usd': 9.0}


def _guards():
    spec = importlib.util.spec_from_file_location('_lane3_guards', REPO / 'scripts/check_regression_guards.py')
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / 'scripts'))
    spec.loader.exec_module(module)
    return module


def test_lane3_mutation_fragments_plant_inside_their_moved_bodies():
    guards = _guards()
    fragments = json.loads((REPO / 'scripts/mutations/_measure_lane3.json').read_text(encoding='utf-8'))
    assert len(fragments) == 19
    names = {f['mutation'] for f in fragments}
    owned = set(_names())
    provider_rel = PROVIDER.relative_to(REPO).as_posix()
    mutations = [mut for mut in guards.collect() if mut[0] in names]
    assert {mut[0] for mut in mutations} == names
    for name, _tests, relpath, edits, _origin in mutations:
        changed = {Path(k).as_posix(): v for k, v in guards.resolve_targets(REPO, relpath, edits).items()}
        assert provider_rel in changed, name
        original, mutated = changed[provider_rel]
        compile(mutated, provider_rel, 'exec')
        before = {n.name: ast.get_source_segment(original, n) for n in ast.parse(original).body if isinstance(n, DEFINITION)}
        after = {n.name: ast.get_source_segment(mutated, n) for n in ast.parse(mutated).body if isinstance(n, DEFINITION)}
        touched = {symbol for symbol in before if before[symbol] != after.get(symbol)}
        expected = {e['symbol'] for e in edits if e.get('symbol') in owned}
        assert touched == expected, name


@pytest.fixture(scope='session', autouse=True)
def _frozen_run_scratch(tmp_path_factory):
    """The script's frozen re-run and diffs stay in this session's tmp dir."""
    previous = os.environ.get('U3_MOVE_SCRATCH')
    os.environ['U3_MOVE_SCRATCH'] = str(tmp_path_factory.mktemp('u3-frozen-run'))
    yield
    if previous is None:
        os.environ.pop('U3_MOVE_SCRATCH', None)
    else:
        os.environ['U3_MOVE_SCRATCH'] = previous


def _mover(tmp_path, measure_text, provider_text):
    import subprocess
    scripts = tmp_path / 'scripts'
    (scripts / '_measure').mkdir(parents=True)
    (scripts / 'measure.py').write_bytes(measure_text.encode('utf-8'))  # LF on Windows too
    (scripts / '_measure/savings.py').write_bytes(provider_text.encode('utf-8'))
    proc = subprocess.run([sys.executable, str(REPO / 'scripts/split_lanes/u3_move.py'),
                           '--scripts', str(scripts)], capture_output=True, text=True, timeout=1800)
    return proc, scripts


def test_lane_script_is_a_verified_noop_on_the_split_tree(tmp_path):
    measure_text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    provider_text = PROVIDER.read_text(encoding='utf-8')
    proc, scripts = _mover(tmp_path, measure_text, provider_text)
    # The aggregate facade also carries the other lanes, so the frozen re-run
    # refuses it alone; savings.py and the fragment still reproduce byte for byte
    # (facade half: merge oracle, decisions-U9 D1), and nothing may be written.
    assert proc.returncode == 1, proc.stderr
    assert 'u3_move refused' in proc.stderr
    differing = [line for line in proc.stderr.splitlines() if 'differs from the frozen re-run' in line]
    assert len(differing) == 1 and 'measure.py differs from the frozen re-run' in differing[0], proc.stderr
    assert 'savings.py' not in proc.stderr and '_measure_lane3.json' not in proc.stderr
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == measure_text
    assert (scripts / '_measure/savings.py').read_text(encoding='utf-8') == provider_text


@pytest.mark.parametrize('damage', ['duplicate', 'missing', 'unowned'])
def test_lane_script_refuses_inconsistent_trees(tmp_path, damage):
    measure_text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    provider_text = PROVIDER.read_text(encoding='utf-8')
    if damage == 'duplicate':
        provider_text += '\n\ndef _usd(value):\n    return value\n'
    elif damage == 'missing':
        tree = ast.parse(provider_text)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_usd')
        lines = provider_text.splitlines(keepends=True)
        provider_text = ''.join(lines[:node.lineno - 1] + lines[node.end_lineno:])
    else:
        provider_text += '\n\ndef _not_in_lane_three():\n    return 0\n'
    proc, scripts = _mover(tmp_path, measure_text, provider_text)
    assert proc.returncode == 1
    assert 'u3_move refused' in proc.stderr
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == measure_text


def _function_bindings(node):
    """Names a function binds locally, by every binding form, minus `global`s."""
    declared = {name for n in ast.walk(node) if isinstance(n, (ast.Global, ast.Nonlocal)) for name in n.names}
    bound = set()
    for n in ast.walk(node):
        if isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)  # assignment, for/with/comprehension targets, walrus
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split('.')[0] for a in n.names}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and n is not node:
            if not isinstance(n, ast.Lambda):
                bound.add(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bound.add(n.rest)
    return bound - declared


def test_local_bindings_are_never_read_through_the_facade():
    """Astra U0 finding 2: except-as names and other non-Name bindings stay local."""
    base = {n.name: n for n in ast.parse(measure_source(SCRIPTS / 'measure.py')).body if isinstance(n, DEFINITION)}
    provider = {n.name: n for n in ast.parse(PROVIDER.read_text(encoding='utf-8')).body if isinstance(n, DEFINITION)}
    handlers = 0
    for name in _names():
        local = _function_bindings(base[name])
        handlers += sum(1 for n in ast.walk(base[name]) if isinstance(n, ast.ExceptHandler) and n.name)
        through_facade = {n.attr for n in ast.walk(provider[name]) if isinstance(n, ast.Attribute) and (
            isinstance(n.value, ast.Name) and n.value.id == '_measure_facade'
            or isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == '_compat.facade')}
        assert not through_facade & local, (name, sorted(through_facade & local))
        moved_handlers = sorted(n.name for n in ast.walk(provider[name]) if isinstance(n, ast.ExceptHandler) and n.name)
        assert moved_handlers == sorted(n.name for n in ast.walk(base[name]) if isinstance(n, ast.ExceptHandler) and n.name)
    assert handlers > 0


def test_bootstrap_redirect_stays_in_the_facade():
    """Astra U0 finding 4: the pre-import bytecode redirect is never extracted."""
    assert '_redirect_bytecode_out_of_repo' not in _names()
    assert '_redirect_bytecode_out_of_repo' in _defs(SCRIPTS / 'measure.py')
    assert '_redirect_bytecode_out_of_repo' not in _defs(PROVIDER)


def _base_function(m, name):
    """The frozen BASE body (sha-checked above), defined against this facade."""
    source = measure_source(SCRIPTS / 'measure.py')
    node = next(n for n in ast.parse(source).body if isinstance(n, DEFINITION) and n.name == name)
    namespace = dict(m.__dict__)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'base-' + name, 'exec'), namespace)
    function = namespace[name]
    assert Path(function.__code__.co_filename).name == 'base-' + name
    return function


def test_except_as_error_path_matches_base_sqlite_error(m, tmp_path):
    bogus = tmp_path / 'not a database é.db'
    bogus.write_bytes(b'this is not sqlite' * 64)
    empty = tmp_path / 'empty.db'
    import sqlite3
    sqlite3.connect(empty).close()
    base = _base_function(m, '_session_cost_open_db')
    for path in (bogus, empty):
        expected = base(path)
        actual = m._session_cost_open_db(path)
        assert expected[0] is None and actual[0] is None
        assert actual == expected and actual[1]  # the str(exc) message, byte for byte


def test_except_as_error_path_matches_base_runway_failure(m, monkeypatch, capsys):
    class FixedClock:
        @staticmethod
        def now():
            import datetime as real
            return real.datetime(2026, 11, 5, 12, 0, 0)
    monkeypatch.setattr(m, 'datetime', FixedClock)
    monkeypatch.setattr(m, '_dashboard_savings_data', lambda **kw: {'total_tokens': 0, 'total_cost_usd': 0.0, 'by_category': {}})
    monkeypatch.setattr(m, '_cache_ttl_waste_cached', lambda **kw: {'available': False})
    monkeypatch.setattr(m, 'keepwarm_cache_health_block', lambda **kw: {'fixture': True})
    calls = []
    def failing_runway(**kw):
        calls.append(kw)
        raise LookupError('fixture runway failure')
    monkeypatch.setattr(m, 'runway_snapshot', failing_runway)
    base = _base_function(m, '_live_savings_payload')
    expected = base(days=7)
    base_err = capsys.readouterr().err
    actual = m._live_savings_payload(days=7)
    actual_err = capsys.readouterr().err
    assert actual == expected and actual['runway'] is None
    assert actual_err == base_err and 'runway snapshot failed (LookupError)' in actual_err
    assert calls == [{'days': 7}, {'days': 7}]


_DRIFTS = {
    # body text changes: caught by the frozen sha256
    'body': ('provider', 'event\'s own date via the system tz database). Never raises.',
             'event\'s own date via the system tz database). Never raises!'),
    # Astra U3 finding 1, reproduction 2: a changed moved body
    'body-usd': ('provider', '    v = float(v or 0)\n', '    v = float(v or 0) + 100\n'),
    # Astra U3 finding 1, reproduction 3: a changed captured default
    'binding-default-sidecar': ('facade', '_read_subagent_pool_sidecar, __name__, defaults=(_SUBAGENT_POOL_TTL,))',
                                '_read_subagent_pool_sidecar, __name__, defaults=(999,))'),
    # same unqualified text, different qualification: caught by re-moving
    'qualification': ('provider', 'dt.astimezone(_measure_facade.timezone.utc)',
                      'dt.astimezone(_compat.facade().timezone.utc)'),
    # captured default in the facade binding
    'binding-default': ('facade', "_measure_providers['savings']._counted_walk_transcript, __name__, defaults=(False,))",
                        "_measure_providers['savings']._counted_walk_transcript, __name__, defaults=(True,))"),
    # provider placeholder default (the frozen signature restores the same text)
    'placeholder-default': ('provider', 'def _counted_walk_transcript(path, collect_markers=None):',
                            'def _counted_walk_transcript(path, collect_markers=False):'),
    # anything beyond header + moved bodies
    'stray-content': ('provider', 'from . import _compat\n', 'from . import _compat\n_STRAY_CACHE = {}\n'),
    # Astra U2 finding 1: extra executable code appended to the provider
    'appended-statement': ('provider', None, "\n\nprint('stray provider side effect')\n"),
}


_REASONS = {
    'body': 'no longer matches the frozen source', 'body-usd': 'no longer matches the frozen source',
    'qualification': 'moved body differs from re-moving the frozen source',
    'placeholder-default': 'moved body differs from re-moving the frozen source',
    'binding-default': 'facade binding (captured defaults) differs',
    'binding-default-sidecar': 'facade binding (captured defaults) differs',
    'stray-content': 'extra or reordered content beyond the moved bodies',
    'appended-statement': 'extra or reordered content beyond the moved bodies',
}
# The root check: body edits cannot even rebuild the frozen input; every other
# drift rebuilds it, re-runs the move in .scratch-u3 and byte-compares.
_ROOT = {}  # every drift: the frozen re-run (git objects at the pinned commit) differs


def _assert_root_refusal(stderr, root='differs from the frozen re-run; diff: '):
    assert root in stderr, stderr
    if root.endswith('diff: '):
        for line in stderr.splitlines():
            if root in line:
                assert Path(line.split(root, 1)[1]).is_file()


@pytest.mark.parametrize('drift', sorted(_DRIFTS))
def test_completed_state_refuses_body_binding_and_default_drift(tmp_path, drift):
    """Astra lane review: the idempotent path proves every moved body and binding."""
    where, old, new = _DRIFTS[drift]
    measure_text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    provider_text = PROVIDER.read_text(encoding='utf-8')
    if where == 'facade':
        assert measure_text.count(old) == 1
        measure_text = measure_text.replace(old, new)
    elif old is None:
        provider_text += new
    else:
        assert provider_text.count(old) == 1
        provider_text = provider_text.replace(old, new)
    proc, scripts = _mover(tmp_path, measure_text, provider_text)
    assert proc.returncode == 1, proc.stdout
    assert 'u3_move refused' in proc.stderr and 'no-op' not in proc.stdout
    assert _REASONS[drift] in proc.stderr, proc.stderr
    _assert_root_refusal(proc.stderr, _ROOT.get(drift, 'differs from the frozen re-run; diff: '))
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == measure_text
    assert (scripts / '_measure/savings.py').read_text(encoding='utf-8') == provider_text


def _bindings_across_their_default():
    """Lift `_read_subagent_pool_sidecar`'s binding above `_SUBAGENT_POOL_TTL = ...`,
    the constant its captured default reads. The lane-3 bindings in between move
    with it, in order, so lane-3 order is unchanged; only their place among
    non-lane statements changes."""
    text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    body = ast.parse(text).body
    owned = set(_names())
    def bound(node):
        return (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind' and node.targets[0].id in owned)
    index = next(i for i, n in enumerate(body) if bound(n) and n.targets[0].id == '_read_subagent_pool_sidecar')
    assert '_SUBAGENT_POOL_TTL' in ast.unparse(body[index].value)
    init = next(j for j in range(index - 1, -1, -1)
                if '_SUBAGENT_POOL_TTL' in {t.id for t in getattr(body[j], 'targets', []) if isinstance(t, ast.Name)})
    group = [body[k] for k in range(init + 1, index + 1) if bound(body[k])]
    lines = text.splitlines(keepends=True)
    moved = [''.join(lines[n.lineno - 1:n.end_lineno]) for n in group]
    for node in reversed(group):
        del lines[node.lineno - 1:node.end_lineno]
    lines[body[init].lineno - 1:body[init].lineno - 1] = moved
    return text, ''.join(lines), [n.targets[0].id for n in group]


def _move_line_above(text, node, target):
    lines = text.splitlines(keepends=True)
    moved = lines[node.lineno - 1:node.end_lineno]
    del lines[node.lineno - 1:node.end_lineno]
    lines[target.lineno - 1:target.lineno - 1] = moved
    return ''.join(lines)


def _lane_order(source):
    owned = set(_names())
    return [n.targets[0].id for n in ast.parse(source).body if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == '_measure_compat.bind'
            and n.targets[0].id in owned]


def test_completed_state_refuses_binding_moved_across_its_default_initialization(tmp_path):
    """Astra lane-6 finding 1: position among non-lane statements, not just lane order."""
    text, moved, group = _bindings_across_their_default()
    assert group[-1] == '_read_subagent_pool_sidecar' and len(group) == 3
    assert _lane_order(moved) == _lane_order(text) and moved != text
    # The moved binding now evaluates its captured default before the constant exists.
    position = {n.targets[0].id if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) else None: i
                for i, n in enumerate(ast.parse(moved).body)}
    assert position['_read_subagent_pool_sidecar'] < position['_SUBAGENT_POOL_TTL']
    proc, scripts = _mover(tmp_path, moved, PROVIDER.read_text(encoding='utf-8'))
    assert proc.returncode == 1, proc.stdout
    assert 'statement order differs from the frozen BASE' in proc.stderr, proc.stderr
    assert 'rebuilt pre-move facade differs from the frozen input' in proc.stderr, proc.stderr
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == moved


def test_completed_state_refuses_binding_swapped_with_a_non_lane_statement(tmp_path):
    text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    body = ast.parse(text).body
    owned = set(_names())
    index = next(i for i, n in enumerate(body) if i and isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                 and ast.unparse(n.value.func) == '_measure_compat.bind' and n.targets[0].id in owned
                 and not (isinstance(body[i - 1], ast.Assign) and isinstance(body[i - 1].value, ast.Call)
                          and ast.unparse(body[i - 1].value.func) == '_measure_compat.bind'))
    moved = _move_line_above(text, body[index], body[index - 1])
    proc, scripts = _mover(tmp_path, moved, PROVIDER.read_text(encoding='utf-8'))
    assert proc.returncode == 1, proc.stdout
    assert 'statement order differs from the frozen BASE' in proc.stderr, proc.stderr
    assert 'rebuilt pre-move facade differs from the frozen input' in proc.stderr, proc.stderr
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == moved


def test_completed_state_refuses_astra_u2_raise_appended_to_provider(tmp_path):
    provider_text = PROVIDER.read_text(encoding='utf-8') + 'raise RuntimeError("review-only import failure")\n'
    proc, scripts = _mover(tmp_path, (SCRIPTS / 'measure.py').read_text(encoding='utf-8'), provider_text)
    assert proc.returncode == 1 and 'no-op' not in proc.stdout
    _assert_root_refusal(proc.stderr)
    assert (scripts / '_measure/savings.py').read_text(encoding='utf-8') == provider_text


def test_completed_state_refuses_a_duplicated_facade_binding(tmp_path):
    text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    line = next(l for l in text.splitlines(keepends=True) if l.startswith("_usd = _measure_compat.bind("))
    moved = text.replace(line, line + line, 1)
    proc, scripts = _mover(tmp_path, moved, PROVIDER.read_text(encoding='utf-8'))
    assert proc.returncode == 1 and 'no-op' not in proc.stdout
    assert '_usd: moved body without its single facade binding' in proc.stderr, proc.stderr
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == moved



def test_completed_state_refuses_a_changed_constant_outside_lane_bindings(tmp_path):
    """Astra U4 finding 1: a non-lane change (a dollar threshold) must not be
    blessed by rebuilding the frozen input from the facade."""
    text = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    assert text.count('\n_TRANSFORMATION_FLAT_BAND = 0.05\n') == 1
    changed = text.replace('\n_TRANSFORMATION_FLAT_BAND = 0.05\n', '\n_TRANSFORMATION_FLAT_BAND = 999.0\n')
    proc, scripts = _mover(tmp_path, changed, PROVIDER.read_text(encoding='utf-8'))
    assert proc.returncode == 1 and 'no-op' not in proc.stdout
    _assert_root_refusal(proc.stderr)
    assert 'measure.py differs from the frozen re-run' in proc.stderr
    assert 'rebuilt pre-move facade differs from the frozen input' in proc.stderr
    assert (scripts / 'measure.py').read_text(encoding='utf-8') == changed


def _lane_module():
    spec = importlib.util.spec_from_file_location('_lane3_mover_under_test', REPO / 'scripts/split_lanes/u3_move.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_tree(tmp_path):
    scripts = tmp_path / 'scripts'
    (scripts / '_measure').mkdir(parents=True)
    for relative in ('measure.py', '_measure/savings.py'):
        (scripts / relative).write_bytes((SCRIPTS / relative).read_bytes())
    return scripts


def test_unobtainable_frozen_commit_refuses_without_writes(tmp_path, capsys):
    mover = _lane_module()
    mover.FROZEN_COMMIT = 'f' * 40  # no such object here, and not fetchable from origin
    scripts = _copy_tree(tmp_path)
    before = {p: p.read_bytes() for p in scripts.rglob('*.py')}
    with pytest.raises(mover.Refusal, match='cannot be obtained'):
        mover.run(scripts)
    assert {p: p.read_bytes() for p in scripts.rglob('*.py')} == before


def test_tampered_frozen_pin_refuses_without_writes(tmp_path):
    mover = _lane_module()
    path = 'skills/token-optimizer/scripts/measure.py'
    mover.FROZEN_SHA256 = dict(mover.FROZEN_SHA256, **{path: '0' * 64})
    scripts = _copy_tree(tmp_path)
    before = {p: p.read_bytes() for p in scripts.rglob('*.py')}
    with pytest.raises(mover.Refusal, match='fails its pinned sha256'):
        mover.run(scripts)
    assert {p: p.read_bytes() for p in scripts.rglob('*.py')} == before


def test_frozen_input_is_pinned_in_the_script_not_the_tree():
    source = (REPO / 'scripts/split_lanes/u3_move.py').read_text(encoding='utf-8')
    assert "FROZEN_COMMIT = '" in source and 'rebuild_input' in source
    assert not list((REPO / 'scripts/split_lanes').glob('u3_*.json'))
