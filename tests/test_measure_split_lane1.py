"""Lane U1 (runtime, pricing, trends): ownership, live facade lookups, mutation fragments.

`MEASURE_SPLIT_SOURCE=<BASE measure.py>` runs only the `test_error_path_*` cases
against the unsplit BASE: their literal expectations were captured there and must
hold byte for byte on both trees.
"""
import ast
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import FunctionType, ModuleType
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
LANE_MODULES = ('runtime', 'pricing', 'trends')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def mover(tmp_path_factory):
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        module = _load('_lane1_mover', REPO / 'scripts/split_lanes/u1_move.py')
        # Replays are fixtures too: keep them inside this run's tmp, not the checkout.
        module.REPLAY_ROOT = tmp_path_factory.mktemp('lane1-replay')
        yield module
    finally:
        sys.path.remove(str(REPO / 'scripts'))


@pytest.fixture(scope='module')
def guards():
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        yield _load('_lane1_guards', REPO / 'scripts/check_regression_guards.py')
    finally:
        sys.path.remove(str(REPO / 'scripts'))


BASE_SOURCE = os.environ.get('MEASURE_SPLIT_SOURCE')


@pytest.fixture(autouse=True)
def _base_mode(request):
    if BASE_SOURCE and not request.node.name.startswith('test_error_path_'):
        pytest.skip('split-tree assertion; BASE mode runs only the error-path parity cases')


@pytest.fixture
def m(tmp_path):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home,
                               'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        yield module


def _assert_subject(function, provider):
    """Prove which tree the parity case actually exercised."""
    origin = Path(function.__code__.co_filename)
    if BASE_SOURCE:
        assert origin == Path(BASE_SOURCE)
    else:
        assert origin == SCRIPTS / '_measure' / provider


def _top_level(path):
    return ast.parse(Path(path).read_text(encoding='utf-8')).body


# --- ownership and budgets -------------------------------------------------

def test_every_lane_symbol_has_one_body_in_its_owned_provider(mover):
    _, symbols = mover.lane_packet()
    to_move, moved = mover.classify(SCRIPTS, symbols)
    assert to_move == []
    assert len(moved) == len(symbols) == 179
    assert {module for _, module in symbols} == set(LANE_MODULES)


def test_bootstrap_redirect_stays_in_the_facade(mover):
    names = [n.name for n in _top_level(SCRIPTS / 'measure.py') if isinstance(n, ast.FunctionDef)]
    assert names.count('_redirect_bytecode_out_of_repo') == 1
    for module in LANE_MODULES:
        assert '_redirect_bytecode_out_of_repo' not in {
            n.name for n in _top_level(SCRIPTS / '_measure' / (module + '.py')) if isinstance(n, ast.FunctionDef)}


def _bootstrap_positions(source):
    tree = ast.parse(source)
    body = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_redirect_bytecode_out_of_repo')
    call = next(c for s in tree.body if not isinstance(s, ast.FunctionDef) for c in ast.walk(s)
                if isinstance(c, ast.Call) and getattr(c.func, 'id', None) == '_redirect_bytecode_out_of_repo')
    providers = next(n for n in tree.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == '_measure_providers')
    return body, call, providers


def test_bootstrap_body_precedes_its_early_call_and_guard_goes_red(mover, tmp_path):
    """Needs no git history: runs in the shallow CI checkout too."""
    source = (SCRIPTS / 'measure.py').read_text(encoding='utf-8')
    body, call, providers = _bootstrap_positions(source)
    assert body.end_lineno < call.lineno < providers.lineno
    mover.check_bootstrap(SCRIPTS)
    lines = source.splitlines(keepends=True)
    bootstrap = ''.join(lines[body.lineno - 1:body.end_lineno])
    renamed = source.replace('def _redirect_bytecode_out_of_repo():', 'def _redirect_bytecode_moved_away():', 1)
    after_call = source.replace(bootstrap, '', 1) + '\n\n' + bootstrap
    cases = {'body renamed away': (renamed, None), 'body after its early call': (after_call, None),
             'bootstrap copied into a provider': (source, 'runtime')}
    for case, (facade_text, provider) in cases.items():
        scripts = _tree_copy(tmp_path / case.replace(' ', '-'))
        (scripts / 'measure.py').write_text(facade_text, encoding='utf-8', newline='\n')
        if provider:
            path = scripts / '_measure' / (provider + '.py')
            path.write_text(path.read_text(encoding='utf-8') + '\n\n' + bootstrap, encoding='utf-8', newline='\n')
        with pytest.raises(mover.Refused, match='_redirect_bytecode_out_of_repo: (must stay in measure.py|retained bootstrap found)'):
            mover.check_bootstrap(scripts)


def test_bootstrap_drift_refuses_through_the_replay(mover, frozen, base, tmp_path):
    scripts = _tree_copy(tmp_path)
    facade = scripts / 'measure.py'
    facade.write_text(facade.read_text(encoding='utf-8').replace(
        'def _redirect_bytecode_out_of_repo():', 'def _redirect_bytecode_moved_away():', 1), encoding='utf-8', newline='\n')
    before = facade.read_bytes()
    with pytest.raises(mover.Refused, match='differs from the replayed move') as refused:
        mover.run(scripts, dry_run=True, log=_quiet, frozen=frozen, base=base)
    assert '_redirect_bytecode_out_of_repo: must stay in measure.py' in str(refused.value)
    assert facade.read_bytes() == before


def test_split_guard_accepts_the_lane_tree():
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        errors, counts = _load('_lane1_check', REPO / 'scripts/check_measure_split.py').check(REPO)
    finally:
        sys.path.remove(str(REPO / 'scripts'))
    # FILE-SIZE-POLICY.md (orchestrator, 2026-11-06): pass-1 domain modules may exceed
    # U0's 1,000-line cap until U9 adds .file-size-allow. Tolerate exactly that message
    # for this lane's own modules, and nothing else.
    allowed = {f'{module}.py: exceeds 1000 lines' for module in LANE_MODULES
               if len((SCRIPTS / '_measure' / (module + '.py')).read_text(encoding='utf-8').splitlines()) > 1000}
    assert set(errors) <= allowed, sorted(set(errors) - allowed)
    assert counts['facade_lines'] <= counts['facade_ceiling']


def test_lane_providers_carry_no_module_state():
    for module in LANE_MODULES:
        for node in _top_level(SCRIPTS / '_measure' / (module + '.py')):
            assert isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)), (module, node.lineno)
            assert not isinstance(node, ast.Global)
        for node in ast.walk(ast.parse((SCRIPTS / '_measure' / (module + '.py')).read_text(encoding='utf-8'))):
            # A remaining `global` would write provider state instead of the facade's.
            assert not isinstance(node, ast.Global), (module, node.lineno)


def test_imported_providers_hold_only_definitions(m):
    allowed = (FunctionType, type, ModuleType)
    for module in LANE_MODULES:
        provider = sys.modules['_measure.' + module]
        for name, value in vars(provider).items():
            if name.startswith('__') or name == 'annotations':
                continue
            assert isinstance(value, allowed), (module, name, type(value))


@pytest.fixture(scope='module')
def frozen(mover):
    """Pinned unsplit BASE. The mover fetches it by SHA into a shallow clone; no skip."""
    return mover.frozen_source()


@pytest.fixture(scope='module')
def base(mover, frozen):
    """The U0 commit this lane was cut from (the mover deepens a shallow clone)."""
    return mover.lane_base()


def _quiet(*_):
    pass


def test_rerun_is_a_no_op_because_tree_matches_the_replay_byte_for_byte(mover, frozen, base):
    inputs, outputs, root = mover.replay(base, frozen, _quiet)
    assert set(outputs) == set(mover.WRITTEN) and root.is_relative_to(mover.REPLAY_ROOT)
    # The aggregate facade also carries the other lanes, so run() refuses naming
    # it alone; every lane-owned file must still equal the replay byte for byte
    # (the facade half is proven by the merge oracle, decisions-U9 D1).
    for relative in mover.WRITTEN[1:]:
        assert (REPO / relative).read_bytes() == outputs[relative], relative
    with pytest.raises(mover.Refused) as refused:
        mover.run(dry_run=True, log=_quiet, frozen=frozen, base=base)
    listed = [line.split(' (replay: ')[0].strip() for line in str(refused.value).splitlines()
              if '(replay: ' in line]
    assert listed == [mover.WRITTEN[0]]


def test_untouched_inputs_receive_exactly_the_replay(mover, frozen, base, tmp_path):
    scripts = tmp_path / 'skills/token-optimizer/scripts'
    inputs, outputs, _ = mover.replay(base, frozen, _quiet)
    for relative, data in inputs.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(data)
    fragments = tmp_path / mover.WRITTEN[-1]
    assert mover.run(scripts, fragments, log=_quiet, frozen=frozen, base=base) == {'moved_now': 176, 'already_moved': 3}
    for relative in mover.WRITTEN:
        assert (tmp_path / relative).read_bytes() == outputs[relative], relative


def _edit(path, old, new):
    text = path.read_text(encoding='utf-8')
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding='utf-8', newline='\n')


DRIFTS = {
    'completed body': ('_measure/pricing.py', '        return m.capitalize()\n', '        return m.upper()\n',
                       '_friendly_model: moved body drifted'),
    'lost qualification': ('_measure/runtime.py', 'return _measure_facade.detect_runtime() == "codex" or',
                           'return detect_runtime() == "codex" or', '_use_codex_session_adapter: moved body drifted'),
    'inherited canary body': ('_measure/pricing.py', '    full_input = int(input_tokens or 0)',
                              '    full_input = 1 + int(input_tokens or 0)', '_get_model_cost: moved body drifted'),
    'captured default': ('measure.py', "get_session_baselines, __name__, defaults=(10,))",
                         "get_session_baselines, __name__, defaults=(11,))", 'get_session_baselines: facade binding drifted'),
    'canary default': ('measure.py', "defaults=(0, 0, None, None, None, False,))",
                       "defaults=(0, 0, None, None, None, True,))", '_get_model_cost: facade binding drifted'),
    'decorator': ('measure.py',
                  "_tripwire_lock = _measure_compat.bind(_measure_providers['trends']._tripwire_lock, "
                  "__name__, decorators=(contextmanager,))",
                  "_tripwire_lock = _measure_compat.bind(_measure_providers['trends']._tripwire_lock, "
                  "__name__)", '_tripwire_lock: facade binding drifted'),
}


@pytest.mark.parametrize('kind', sorted(DRIFTS))
def test_completed_state_drift_diagnostics(mover, frozen, tmp_path, kind):
    relative, old, new, message = DRIFTS[kind]
    scripts = _tree_copy(tmp_path)
    _edit(scripts / relative, old, new)
    _, symbols = mover.lane_packet()
    with pytest.raises(mover.Refused, match=message):
        mover.validate(scripts, frozen, symbols)


def _move_binding_above(path, name, anchor):
    """Move one binding line above a non-lane statement; lane order is unchanged."""
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    binding = next(i for i, line in enumerate(lines) if line.startswith(name + ' = _measure_compat.bind('))
    target = max(i for i, line in enumerate(lines[:binding]) if line.startswith(anchor))
    assert all(not line.startswith(('def ', 'class ')) and '_measure_compat.bind(' not in line
               for line in lines[target:binding]), 'precondition: only non-lane statements are crossed'
    moved = lines.pop(binding)
    lines.insert(target, moved)
    path.write_text(''.join(lines), encoding='utf-8', newline='\n')


# One negative case per hole class reviewers found: each must refuse via the
# replay byte comparison, and must write nothing.
HOLES = {
    'body edit': (lambda s: _edit(s / '_measure/pricing.py', '        return m.capitalize()\n', '        return m.upper()\n'),
                  'skills/token-optimizer/scripts/_measure/pricing.py', '_friendly_model: moved body drifted'),
    'default edit': (lambda s: _edit(s / 'measure.py', 'get_session_baselines, __name__, defaults=(10,))',
                                     'get_session_baselines, __name__, defaults=(11,))'),
                     'skills/token-optimizer/scripts/measure.py', 'get_session_baselines: facade binding drifted'),
    'binding moved across init': (lambda s: _move_binding_above(s / 'measure.py', '_apply_bundled_prices',
                                                                '_PROMO_MANAGED_OPENAI_KEYS = '),
                                  'skills/token-optimizer/scripts/measure.py',
                                  '_apply_bundled_prices: binding moved across 1 surrounding statement'),
    'extra provider statement': (lambda s: (s / '_measure/trends.py').write_text(
                                     (s / '_measure/trends.py').read_text(encoding='utf-8') + '\n_SHADOW_CACHE = {}\n',
                                     encoding='utf-8', newline='\n'),
                                 'skills/token-optimizer/scripts/_measure/trends.py', None),
}


@pytest.mark.parametrize('kind', sorted(HOLES))
def test_completed_state_hole_refuses_without_writes(mover, frozen, base, tmp_path, kind):
    tamper, relative, diagnostic = HOLES[kind]
    scripts = _tree_copy(tmp_path)
    fragments = tmp_path / 'fragments.json'
    shutil.copyfile(REPO / mover.WRITTEN[-1], fragments)
    tamper(scripts)
    files = [scripts / 'measure.py', fragments] + [scripts / '_measure' / (m + '.py') for m in LANE_MODULES]
    before = {path: path.read_bytes() for path in files}
    with pytest.raises(mover.Refused, match='differs from the replayed move') as refused:
        mover.run(scripts, fragments, log=_quiet, frozen=frozen, base=base)
    assert relative in str(refused.value)
    # The aggregate facade differs from the single-lane replay by construction, so
    # the differing list always heads with it, then the planted hole.
    replay_root = mover.replay(base, frozen, _quiet)[2]
    expected = [f'  {mover.WRITTEN[0]} (replay: {replay_root / mover.WRITTEN[0]})']
    if relative != mover.WRITTEN[0]:
        expected.append(f'  {relative} (replay: {replay_root / relative})')
    assert [line for line in str(refused.value).splitlines() if '(replay: ' in line] == expected
    if diagnostic:
        assert diagnostic in str(refused.value)
    assert {path: path.read_bytes() for path in files} == before


def test_unmoved_body_drift_refuses(mover, frozen, tmp_path):
    scripts = _tree_copy(tmp_path)
    _, symbols = mover.lane_packet()
    text = mover._frozen_texts(frozen, symbols)['_parse_semver']
    facade, provider = scripts / 'measure.py', scripts / '_measure/runtime.py'
    binding = next(line for line in facade.read_text(encoding='utf-8').splitlines()
                   if line.startswith('_parse_semver = _measure_compat.bind('))
    _edit(facade, binding + '\n', text + '\n')
    body, _ = mover.expected_state(frozen, symbols)['_parse_semver']
    _edit(provider, '\n\n\n' + body + '\n', '\n')
    to_move, _ = mover.classify(scripts, symbols)
    assert to_move == [('_parse_semver', 'runtime')]
    _edit(facade, text + '\n', text + '  # drift\n')
    with pytest.raises(mover.Refused, match='_parse_semver: facade body differs from its frozen hash'):
        mover.validate(scripts, frozen, symbols)


def test_frozen_source_must_match_the_pinned_hash(mover, frozen, tmp_path):
    assert mover.FROZEN_BASE_COMMIT == json.loads((REPO / 'scripts/measure_split_manifest.json').read_text())['base']
    copy = tmp_path / 'measure.py'
    copy.write_text(frozen + '\n', encoding='utf-8', newline='\n')
    with pytest.raises(mover.Refused, match='pinned sha256'):
        mover.frozen_source(copy)


PROMO = '_PROMO_MANAGED_OPENAI_KEYS = frozenset({"gpt-5.6-sol"})'
PROMO_DRIFT = '_PROMO_MANAGED_OPENAI_KEYS = frozenset({"gpt-5.6-sol", "fixture-model"})'


def test_changed_constant_outside_bindings_refuses_without_writes(mover, frozen, base, tmp_path):
    """Reviewer case: a non-lane statement edited in the working tree must refuse."""
    scripts = _tree_copy(tmp_path)
    fragments = tmp_path / 'fragments.json'
    shutil.copyfile(REPO / mover.WRITTEN[-1], fragments)
    _edit(scripts / 'measure.py', PROMO, PROMO_DRIFT)
    files = [scripts / 'measure.py', fragments] + [scripts / '_measure' / (m + '.py') for m in LANE_MODULES]
    before = {path: path.read_bytes() for path in files}
    with pytest.raises(mover.Refused, match='differs from the replayed move') as refused:
        mover.run(scripts, fragments, log=_quiet, frozen=frozen, base=base)
    assert 'is not the frozen BASE statement' in str(refused.value)
    assert {path: path.read_bytes() for path in files} == before


def test_changed_constant_in_the_replay_inputs_refuses(mover, frozen, base):
    """The lane base itself is checked against the pinned BASE, never trusted as frozen."""
    inputs, _, _ = mover.replay(base, frozen, _quiet)
    facade = inputs[mover.WRITTEN[0]].decode('utf-8')
    assert facade.count(PROMO) == 1
    tampered = dict(inputs, **{mover.WRITTEN[0]: facade.replace(PROMO, PROMO_DRIFT).encode('utf-8')})
    with pytest.raises(mover.Refused, match='is not the frozen BASE statement'):
        mover.replay(base, frozen, _quiet, inputs=tampered)


def _tree_copy(tmp_path):
    scripts = tmp_path / 'scripts'
    (scripts / '_measure').mkdir(parents=True)
    shutil.copyfile(SCRIPTS / 'measure.py', scripts / 'measure.py')
    for path in (SCRIPTS / '_measure').glob('*.py'):
        shutil.copyfile(path, scripts / '_measure' / path.name)
    return scripts


def test_missing_body_refuses(mover, tmp_path):
    scripts = _tree_copy(tmp_path)
    path = scripts / '_measure/trends.py'
    path.write_text(path.read_text(encoding='utf-8').replace('def _sql_quote(', 'def _sql_quote_gone(', 1), encoding='utf-8', newline='\n')
    _, symbols = mover.lane_packet()
    with pytest.raises(mover.Refused, match='_sql_quote: missing'):
        mover.classify(scripts, symbols)


def test_duplicate_body_refuses(mover, tmp_path):
    scripts = _tree_copy(tmp_path)
    facade = scripts / 'measure.py'
    facade.write_text(facade.read_text(encoding='utf-8') + '\n\ndef _parse_semver(value):\n    return value\n', encoding='utf-8', newline='\n')
    _, symbols = mover.lane_packet()
    with pytest.raises(mover.Refused, match='_parse_semver: inconsistent'):
        mover.classify(scripts, symbols)


# --- live facade lookups (KTD1) --------------------------------------------

def test_same_domain_pricing_helper_patch_and_undo(m, monkeypatch):
    original = m._pricing_tier_label('anthropic')
    with monkeypatch.context() as patch:
        patch.setattr(m, 'detect_runtime', lambda: 'codex')
        assert m._pricing_tier_label('anthropic') == 'OpenAI API pricing for recognized Codex models'
    assert m._pricing_tier_label('anthropic') == original


def test_moved_pricing_reads_live_cost_helper(m, monkeypatch):
    fake = Mock(return_value=4.5)
    monkeypatch.setattr(m, '_get_model_cost', fake)
    assert m._model_rate_per_mtok('claude-sonnet-5-5') == 4.5
    fake.assert_called_once_with('claude-sonnet-5-5', 1_000_000, 0)


def test_moved_pricing_reads_live_config_and_shared_tiers(m, monkeypatch):
    monkeypatch.setattr(m, '_read_config_flag', lambda key, default: 'fixture-tier')
    assert m._load_pricing_tier() == 'anthropic'
    monkeypatch.setitem(m.PRICING_TIERS, 'fixture-tier', {'label': 'Fixture'})
    assert m._load_pricing_tier() == 'fixture-tier'
    assert m._pricing_tier_label('fixture-tier') == 'Fixture'


def test_runtime_adapter_switch_follows_patched_detector(m, monkeypatch):
    monkeypatch.setattr(m, 'detect_runtime', lambda: 'codex')
    assert m._use_codex_session_adapter() is True
    monkeypatch.setattr(m, 'detect_runtime', lambda: 'claude')
    assert m._use_codex_session_adapter() is False


def test_trends_connection_follows_rebound_db_path(m, monkeypatch, tmp_path):
    target = tmp_path / 'rebound.db'
    monkeypatch.setattr(m, 'TRENDS_DB', target)
    conn = m._connect_trends_db()
    try:
        assert Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve() == target.resolve()
    finally:
        conn.close()


def test_trends_uses_patched_sqlite_module(m, monkeypatch):
    calls = []

    class Conn:
        def execute(self, sql):
            calls.append(sql)

    monkeypatch.setattr(m, 'sqlite3', type('FakeSqlite', (), {'connect': staticmethod(lambda path: Conn())}))
    assert isinstance(m._connect_trends_db(), Conn)
    assert calls[0] == 'PRAGMA journal_mode=WAL'


def test_global_cache_write_lands_on_the_facade(m, monkeypatch):
    monkeypatch.setattr(m, '_cached_estimate_fn', None)
    assert m._estimate_tokens('abcd' * 10) > 0
    assert m._cached_estimate_fn is not None
    monkeypatch.setattr(m, '_cached_estimate_fn', lambda text: 99)
    assert m._estimate_tokens('anything') == 99
    for module in LANE_MODULES:
        assert '_cached_estimate_fn' not in vars(sys.modules['_measure.' + module])


def test_runtime_spawn_breadcrumb_uses_patched_log_dir(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, 'DAEMON_LOG_DIR', tmp_path / 'logs')
    m._log_spawn_failure('fixture spawn failed')
    assert 'fixture spawn failed' in (tmp_path / 'logs/spawn-failures.log').read_text(encoding='utf-8')


def test_install_roots_use_the_facade_file(m, monkeypatch, tmp_path):
    fake = tmp_path / 'a/b/c/scripts/measure.py'
    monkeypatch.setattr(m, '__file__', str(fake))
    assert m.Path(fake).resolve().parents[3] in m._this_install_roots()


def test_cross_domain_unmoved_caller_sees_patched_moved_helper(m, monkeypatch):
    # _friendly_model (pricing) calls _strip_provider_prefixes (pricing) through the facade.
    monkeypatch.setattr(m, '_strip_provider_prefixes', lambda value: 'opus')
    assert m._friendly_model('anything') == 'Opus'


def test_tripwire_lock_keeps_contextmanager_and_cleanup(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', tmp_path)
    assert callable(m._tripwire_lock.__wrapped__)
    with m._tripwire_lock() as acquired:
        assert acquired
    with pytest.raises(RuntimeError):
        with m._tripwire_lock():
            raise RuntimeError('boom')
    with m._tripwire_lock() as acquired:
        assert acquired


def test_skip_migration_identity_is_shared(m):
    assert m._SkipMigration.__module__ == m.__name__
    assert issubclass(m._SkipMigration, Exception)
    source = (SCRIPTS / '_measure/trends.py').read_text(encoding='utf-8')
    assert 'except _SkipMigration' not in source


def test_exports_point_at_lane_providers(m, mover):
    _, symbols = mover.lane_packet()
    for name, module in symbols:
        value = getattr(m, name)
        code = getattr(value, '__wrapped__', value)
        if isinstance(code, type):
            assert code.__module__ == m.__name__
            continue
        assert Path(code.__code__.co_filename).name == module + '.py', name
        assert code.__module__ == m.__name__, name


# --- mutation fragments ----------------------------------------------------

def test_lane1_fragments_plant_inside_their_owned_bodies(guards, mover):
    fragments = json.loads((REPO / 'scripts/mutations/_measure_lane1.json').read_text(encoding='utf-8'))
    _, symbols = mover.lane_packet()
    owners = dict(symbols)
    assert len(fragments) == 9
    for fragment in fragments:
        name = fragment['symbol']
        changed = guards.resolve_targets(REPO, guards.MEASURE, [dict(fragment)])
        assert list(changed) == [f"skills/token-optimizer/scripts/_measure/{owners[name]}.py"], fragment['mutation']
        original, mutated = changed[next(iter(changed))]
        node = next(n for n in ast.parse(original).body if getattr(n, 'name', None) == name)
        lines = original.splitlines(keepends=True)
        start = sum(map(len, lines[:node.lineno - 1]))
        end = sum(map(len, lines[:node.end_lineno]))
        at = original.find(fragment['old'])
        assert start <= at < end, fragment['mutation']
        compile(mutated, 'mutated.py', 'exec')


def test_no_other_lane_fragment_targets_lane1_symbols(mover):
    _, symbols = mover.lane_packet()
    names = {name for name, _ in symbols}
    for lane in range(9):
        if lane == 1:
            continue
        for fragment in json.loads((REPO / f'scripts/mutations/_measure_lane{lane}.json').read_text(encoding='utf-8')):
            assert fragment.get('symbol') not in names, (lane, fragment['mutation'])


# --- error paths: identical on BASE and the split tree ----------------------
# Handler names (`except ... as e`) are function locals. Reading them through the
# facade would turn each handler into AttributeError, so these drive real except
# branches and compare against output captured on the unsplit BASE.

def test_error_path_baseline_write_failure_prints_the_os_error(m, monkeypatch, tmp_path, capsys):
    _assert_subject(m.set_pretool_baseline, 'pricing.py')

    class FullDisk(type(m.Path())):
        def mkdir(self, *args, **kwargs):
            raise OSError(28, 'No space left on device (fixture)')

    monkeypatch.setattr(m, 'SNAPSHOT_DIR', FullDisk(tmp_path / 'snap'))
    capsys.readouterr()
    assert m.set_pretool_baseline(['--opus', '0.9']) is None
    captured = capsys.readouterr()
    assert captured.out == '  [Error] Could not write baseline: [Errno 28] No space left on device (fixture)\n'
    assert captured.err == ''


def test_error_path_bad_prices_file_records_the_exception_text(m, monkeypatch, tmp_path):
    _assert_subject(m._apply_bundled_prices, 'pricing.py')
    monkeypatch.delenv('TOKEN_OPTIMIZER_BUNDLED_PRICES', raising=False)
    monkeypatch.setattr(m, 'BUNDLED_PRICES_STATUS', dict(m.BUNDLED_PRICES_STATUS))
    for text, error in (('{"schema": 2}', 'unsupported prices schema'),
                        ('not json', 'Expecting value: line 1 column 1 (char 0)')):
        path = tmp_path / 'prices.json'
        path.write_text(text, encoding='utf-8', newline='\n')
        assert m._apply_bundled_prices(str(path)) is False
        assert m.BUNDLED_PRICES_STATUS == {'loaded': False, 'path': str(path), 'generated_at': None, 'error': error}


def test_error_path_db_retry_fails_open_on_locks_and_reraises_other_errors(m, monkeypatch):
    _assert_subject(getattr(m._retry_db_operation, '__wrapped__', m._retry_db_operation), 'trends.py')
    sleeps = []
    monkeypatch.setattr(m.time, 'sleep', sleeps.append)
    calls = []

    def locked():
        calls.append('locked')
        raise m.sqlite3.OperationalError('database is locked')

    assert m._retry_db_operation(None, locked) is None
    assert calls == ['locked'] * 3
    assert sleeps == [0.1, 0.2]
    failure = m.sqlite3.OperationalError('no such table: fixture')

    def broken():
        raise failure

    with pytest.raises(m.sqlite3.OperationalError) as raised:
        m._retry_db_operation(None, broken)
    assert raised.value is failure
    assert sleeps == [0.1, 0.2]
