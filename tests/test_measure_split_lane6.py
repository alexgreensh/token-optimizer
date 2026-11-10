"""Lane U6: quality_archive and continuity moved behind the measure facade.

Every assertion goes through the public facade (`measure.X`), the surface the
existing tests patch. The provider files must hold only definitions.
"""
import ast
import importlib.util
import inspect
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure, source_path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
PACKET = json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))['lanes']['6']
OWNERS = {row['name']: row['module'] for row in PACKET['symbols']}
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _env(home):
    return {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
            'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}


@pytest.fixture(scope='module')
def m(tmp_path_factory):
    home = tmp_path_factory.mktemp('lane6') / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    with isolated_measure(env=_env(home)) as module:
        yield module


def _top_level(path):
    return ast.parse(path.read_text(encoding='utf-8')).body


def test_every_lane6_symbol_has_one_body_in_its_provider_and_one_facade_binding():
    facade = _top_level(SCRIPTS / 'measure.py')
    assert not [n.name for n in facade if isinstance(n, DEFINITION) and n.name in OWNERS]
    bindings = {}
    for node in facade:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            bindings.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    bodies = {}
    for module in PACKET['modules']:
        for node in _top_level(SCRIPTS / '_measure' / (module + '.py')):
            if isinstance(node, DEFINITION):
                bodies.setdefault(node.name, []).append(module)
    assert OWNERS and len(OWNERS) == len(PACKET['symbols'])
    for name, module in OWNERS.items():
        assert bodies.get(name) == [module], name
        assert bindings.get(name) == [f"_measure_providers[{module!r}].{name}"], name
        assert source_path(name).name == module + '.py'
    assert set(bodies) == set(OWNERS)


@pytest.mark.parametrize('module', PACKET['modules'])
def test_providers_hold_definitions_only_within_budget(module):
    path = SCRIPTS / '_measure' / (module + '.py')
    text = path.read_text(encoding='utf-8')
    assert len(text.splitlines()) <= 9000
    for node in ast.parse(text).body:
        if isinstance(node, DEFINITION):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        assert ast.unparse(node) in ('from __future__ import annotations', 'from . import _compat'), node.lineno


def test_providers_have_no_domain_global_caches(m):
    for module in PACKET['modules']:
        provider = m._measure_providers[module]
        for name, value in vars(provider).items():
            if name.startswith('__') or name in ('_compat', 'annotations'):
                continue
            assert inspect.isfunction(value) or inspect.isclass(value), (module, name)
            assert name in OWNERS, (module, name)
        # Mutable state stays on the facade only.
        assert not hasattr(provider, '_EXTERNAL_MEMORY_CACHE')
        assert not hasattr(provider, 'QUALITY_CACHE_DIR')


def test_exports_are_the_provider_definitions(m):
    lines = {}
    for module in PACKET['modules']:
        for node in _top_level(SCRIPTS / '_measure' / (module + '.py')):
            if isinstance(node, ast.FunctionDef):
                lines[node.name] = (module, node.lineno)
    for name, module in OWNERS.items():
        value = getattr(m, name)
        assert value.__module__ == m.__name__, name
        assert value.__qualname__ == name, name
        if inspect.isfunction(value):
            code = value.__code__
            assert (Path(code.co_filename).name, code.co_firstlineno) == (module + '.py', lines[name][1]), name


def test_exception_classes_keep_one_identity(m):
    assert issubclass(m._HookTimeout, BaseException)
    assert m._HookTimeout is m._measure_providers['quality_archive']._HookTimeout
    assert issubclass(m._SkipTranscriptQuality, Exception)
    with pytest.raises(m._SkipTranscriptQuality):
        raise m._measure_providers['continuity']._SkipTranscriptQuality()


def test_same_domain_helper_patch_is_seen_and_reverts(m, monkeypatch):
    cached = {'model_context_window': 1000000, 'compact_window': 500000,
              'compact_window_reduced': True, 'model_fill_pct': 30.0,
              'compact_window_source': 'fixture'}
    original = m._format_context_fill(60.0, cached)
    fake = Mock(return_value='SENTINEL-SRC')
    with monkeypatch.context() as patch:
        patch.setattr(m, '_safe_note_text', fake)
        patched = m._format_context_fill(60.0, cached)
    assert 'SENTINEL-SRC' in patched and 'SENTINEL-SRC' not in original
    assert fake.call_count == 1
    assert m._format_context_fill(60.0, cached) == original


def test_cross_module_helper_and_rebound_state_are_live(m, monkeypatch, tmp_path):
    # continuity._once_per_session_marker -> quality_archive.sanitize_session_id
    fake = Mock(return_value='sentinel-sid')
    with monkeypatch.context() as patch:
        patch.setattr(m, 'sanitize_session_id', fake)
        patch.setattr(m, 'QUALITY_CACHE_DIR', tmp_path)
        assert m._once_per_session_marker('tag x!', 'abcdef-1') == tmp_path / 'once-tagx-sentinel-sid.json'
        fake.return_value = 'unknown'
        assert m._once_per_session_marker('tag', 'abcdef-1') is None
    assert fake.call_count == 2
    assert m._once_per_session_marker('tag', 'abcdef-1') == m.QUALITY_CACHE_DIR / 'once-tag-abcdef-1.json'


def test_replaced_os_module_reaches_moved_code(m, monkeypatch):
    fake_os = Mock(wraps=m.os)
    fake_os.environ = {'LANE6_INT': ' 41 ', 'LANE6_FLOAT': '2.5'}
    monkeypatch.setattr(m, 'os', fake_os)
    assert m._int_env('LANE6_INT', 7) == 41
    assert m._float_env('LANE6_FLOAT', 1.0) == 2.5
    assert m._int_env('LANE6_MISSING', 7) == 7


def test_global_cache_write_lands_on_the_facade(m, monkeypatch):
    monkeypatch.setattr(m, '_EXTERNAL_MEMORY_CACHE', None)
    monkeypatch.setenv('TO_EXTERNAL_MEMORY', '1')
    assert m._external_memory_present() is True
    assert m._EXTERNAL_MEMORY_CACHE is True
    # The memo is read back from the facade on the next call.
    monkeypatch.setenv('TO_EXTERNAL_MEMORY', '0')
    assert m._external_memory_present() is True
    m._EXTERNAL_MEMORY_CACHE = None
    assert m._external_memory_present() is False


def test_definition_time_defaults_are_captured_not_live(m, monkeypatch):
    assert m._recent_compact_boundary.__defaults__ == (m._COMPACT_BOUNDARY_FRESH_S, None)
    assert m._advice_replay.__defaults__ == (m._ADVICE_POST_COMPACT_CONTEXT,)
    assert m._checkpoint_restore_credited_recently.__defaults__ == (m._NUDGE_FOLLOWTHROUGH_WINDOW_SECONDS,)
    assert m.compact_capture.__defaults__ == (None, None, 'auto', None, None, None, False, None)
    assert m._record_checkpoint_metadata.__kwdefaults__ == {'fill_pct': None, 'quality_score': None}
    before = m._recent_compact_boundary.__defaults__
    monkeypatch.setattr(m, '_COMPACT_BOUNDARY_FRESH_S', object())
    assert m._recent_compact_boundary.__defaults__ == before
    params = inspect.signature(m.compact_capture).parameters
    assert params['trigger'].default == 'auto'


def test_import_time_settings_use_moved_env_readers(tmp_path):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    env = _env(home) | {'TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS': '3',
                        'TOKEN_OPTIMIZER_CHECKPOINT_COOLDOWN_SECONDS': '11'}
    with isolated_measure(env=env) as module:
        assert module._QUALITY_CACHE_RETENTION_DAYS == 3
        assert module._CHECKPOINT_COOLDOWN_SECONDS == 11
        first = module._int_env
    with isolated_measure(env=_env(home)) as module:
        assert module._QUALITY_CACHE_RETENTION_DAYS == 7
        assert module._CHECKPOINT_COOLDOWN_SECONDS == 90
        assert module._int_env is not first


def test_every_moved_lambda_resolves_the_facade_per_call():
    lambdas = []
    for module in PACKET['modules']:
        tree = ast.parse((SCRIPTS / '_measure' / (module + '.py')).read_text(encoding='utf-8'))
        lambdas += [n for n in ast.walk(tree) if isinstance(n, ast.Lambda)]
    assert lambdas
    for node in lambdas:
        assert isinstance(node.body, ast.Subscript), node.lineno
        assert ast.unparse(node.body.value.elts[0]) == '(_measure_facade := _compat.facade())', node.lineno


def _guards():
    spec = importlib.util.spec_from_file_location('_lane6_guards', REPO / 'scripts/check_regression_guards.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lane6_mutation_fragments_plant_in_the_moved_tree():
    guards = _guards()
    fragments = json.loads((REPO / 'scripts/mutations/_measure_lane6.json').read_text(encoding='utf-8'))
    names = {row['mutation'] for row in fragments}
    assert len(fragments) == 14 and len(names) == 13
    mutations = {m[0]: m for m in guards.collect()}
    for name in names:
        _, _, relpath, edits, _ = mutations[name]
        changed = guards.resolve_targets(REPO, relpath, edits)
        assert changed, name
        for target, (before, after) in changed.items():
            assert before != after
            if target.endswith('measure.py'):
                # Only the #246 module-level-call mutation plants in the facade.
                assert '_TOOL_CALL_WARN, _TOOL_CALL_CRITICAL = _scaled_tool_call_thresholds()' in after
            else:
                assert Path(target).stem in PACKET['modules'], (name, target)


def _mover():
    spec = importlib.util.spec_from_file_location('_lane6_mover', REPO / 'scripts/split_lanes/u6_move.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SYNTHETIC = '''import os
G = None
E = OSError
def helper():
    return 1
def keeps(paths):
    try:
        os.stat(paths[0])
    except E as G:
        return str(G)
    for G in paths:
        pass
    with open(paths[0]) as G:
        pass
    if (G := helper()):
        return [G for G in paths]
    return G
def declared():
    global G
    try:
        pass
    except E as G:
        pass
    return G
'''


def test_scope_audit_keeps_except_for_with_walrus_targets_local():
    mover = _mover()
    tree = ast.parse(_SYNTHETIC)
    import symtable
    table = symtable.symtable(_SYNTHETIC, 'measure.py', 'exec')
    globals_ = mover._qualifier(_SYNTHETIC, tree, table)
    moved, _ = mover.TOOL.extract_definition(_SYNTHETIC, 'keeps', 'quality_archive', tree=tree, table=table)
    node = next(n for n in tree.body if getattr(n, 'name', None) == 'keeps')
    original = mover._definition_text(_SYNTHETIC, node)
    assert mover.audit_scopes(original, moved, 'keeps', globals_) == []
    # The local G of every binding form stays a plain name; only E/os/helper qualify.
    assert '_measure_facade.G' not in moved
    assert 'except _measure_facade.E as G:' in moved and '_measure_facade.helper()' in moved


def test_scope_audit_rejects_each_broken_rewrite():
    mover = _mover()
    tree = ast.parse(_SYNTHETIC)
    import symtable
    table = symtable.symtable(_SYNTHETIC, 'measure.py', 'exec')
    globals_ = mover._qualifier(_SYNTHETIC, tree, table)
    node = next(n for n in tree.body if getattr(n, 'name', None) == 'keeps')
    original = mover._definition_text(_SYNTHETIC, node)
    good, _ = mover.TOOL.extract_definition(_SYNTHETIC, 'keeps', 'quality_archive', tree=tree, table=table)
    # A local turned into a facade attribute (Astra U0 finding 2, function form).
    lost = good.replace('return str(G)', 'return str(_measure_facade.G)').replace('    return G\n', '    return _measure_facade.G\n')
    lost = lost.replace('for G in paths', 'for _measure_facade.G in paths').replace('as G:\n        pass', 'as _measure_facade.G:\n        pass')
    lost = lost.replace('(G := ', '(_G := ').replace('except _measure_facade.E as G:', 'except _measure_facade.E as _G:')
    lost = lost.replace('[G for G in paths]', '[_G for _G in paths]')
    assert any('local binding rewritten' in e for e in mover.audit_scopes(original, lost, 'keeps', globals_))
    # A same-module helper left unqualified bypasses the facade.
    bypass = good.replace('_measure_facade.helper()', 'helper()')
    assert any('unqualified' in e for e in mover.audit_scopes(original, bypass, 'keeps', globals_))
    # A declared global bound by except-as. U0-FINAL's qualifier routes it through a
    # temporary and keeps the facade binding; the audit accepts that rendering.
    declared_node = next(n for n in tree.body if getattr(n, 'name', None) == 'declared')
    original_declared = mover._definition_text(_SYNTHETIC, declared_node)
    declared, _ = mover.TOOL.extract_definition(_SYNTHETIC, 'declared', 'quality_archive', tree=tree, table=table)
    assert '_measure_facade.G = _measure_exception_' in declared
    assert mover.audit_scopes(original_declared, declared, 'declared', globals_) == []
    # The pre-fix rendering (global removed, except-as G left local) is still caught.
    buggy = ('def declared():\n    _measure_facade = _compat.facade()\n    pass\n    try:\n        pass\n'
             '    except _measure_facade.E as G:\n        pass\n    return _measure_facade.G\n')
    errors = mover.audit_scopes(original_declared, buggy, 'declared', globals_)
    assert any('global turned into a local' in e for e in errors), errors


def _base_scripts(tmp_path):
    """BASE (unsplit) measure.py beside today's siblings.

    Shallow CI clones fetch exactly the BASE commit object; a missing object is
    a failure, never a skip.
    """
    import shutil
    import subprocess
    mover = _mover()
    base = mover.FROZEN_BASE['sha']
    mover.ensure_commit(base)
    shown = subprocess.run(['git', '-C', str(REPO), 'show', f'{base}:skills/token-optimizer/scripts/measure.py'],
                           capture_output=True)
    assert shown.returncode == 0, shown.stderr
    assert mover._sha256(shown.stdout.decode('utf-8')) == mover.FROZEN_BASE['sha256']
    # Repo layout inside the fixture root, with the pinned BASE plugin manifest:
    # BASE's version discovery walks up from measure.py and must stop here.
    root = tmp_path / 'base root'
    scripts = root / 'skills/token-optimizer/scripts'
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns('__pycache__', '_measure'))
    (scripts / 'measure.py').write_bytes(shown.stdout)
    manifest = subprocess.run(['git', '-C', str(REPO), 'show', f'{base}:.claude-plugin/plugin.json'],
                              capture_output=True)
    assert manifest.returncode == 0, manifest.stderr
    (root / '.claude-plugin').mkdir()
    (root / '.claude-plugin' / 'plugin.json').write_bytes(manifest.stdout)
    return scripts / 'measure.py'


def _inspect_error(module, target, capsys):
    out = []
    for as_json in (True, False):
        assert module.jsonl_inspect(str(target), as_json) is None
        out.append(capsys.readouterr())
    return out


def test_except_branch_output_matches_base(m, tmp_path, capsys):
    """jsonl_inspect's `except (PermissionError, OSError) as e` branch, BASE vs candidate."""
    target = tmp_path / 'not a file é.jsonl'
    target.mkdir()
    try:
        open(target, 'r', encoding='utf-8')
    except (PermissionError, OSError) as exc:
        expected_error = str(exc)
    candidate = _inspect_error(m, target, capsys)
    assert candidate[0].out == json.dumps({'error': expected_error}) + '\n'
    assert candidate[1].out == f'[Error] Cannot read file: {expected_error}\n'
    assert candidate[0].err == candidate[1].err == ''
    base_path = _base_scripts(tmp_path)
    home = tmp_path / 'base home'
    (home / '.claude').mkdir(parents=True)
    assert m._read_plugin_version() == json.loads((REPO / '.claude-plugin/plugin.json').read_text(encoding='utf-8'))['version']
    shipped = base_path.parents[3] / '.claude-plugin' / 'plugin.json'
    with isolated_measure(base_path, env=_env(home)) as base:
        assert not hasattr(base, '_measure_providers')
        assert Path(base.jsonl_inspect.__code__.co_filename) == base_path
        # Version discovery ended at the manifest shipped with this fixture, not above it.
        assert base._read_plugin_version() == json.loads(shipped.read_text(encoding='utf-8'))['version']
        assert _inspect_error(base, target, capsys) == candidate


def test_real_tree_replay_refuses_a_changed_constant_and_blesses_only_the_pinned_move(tmp_path):
    """Astra U4 finding 1: a constant outside the bindings ($0.10 -> $999 class) must refuse.

    Inputs come only from the pinned commits (fetched by SHA when shallow), never
    from the working tree. One replay serves both halves of the proof.
    """
    import shutil
    mover = _mover()
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))
    owners = mover.lane_symbols(REPO / 'scripts/measure_split_ownership.json')
    rows = {row['name']: row for row in manifest['symbols'] if row['name'] in owners}
    frozen = mover.load_pinned_frozen(manifest)
    inputs = mover.lane_inputs()
    copy = tmp_path / 'tree'
    shutil.copytree(SCRIPTS, copy / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    fragments = copy / 'fragments.json'
    shutil.copyfile(REPO / 'scripts/mutations/_measure_lane6.json', fragments)
    facade = copy / 'scripts/measure.py'
    text = facade.read_text(encoding='utf-8')
    constant = '_COMPACT_RESTORE_ANCHOR_MAX_AGE_S = 1800\n'
    assert text.count(constant) == 1
    facade.write_text(text.replace(constant, '_COMPACT_RESTORE_ANCHOR_MAX_AGE_S = 999\n'), encoding='utf-8', newline='\n')
    before = _snapshot(copy)
    scratch = tmp_path / 'replay'
    with pytest.raises(mover.MoveError, match='differs from a replay') as refused:
        mover.run(copy / 'scripts', owners, rows, frozen, fragments, inputs=inputs, scratch=scratch)
    # The planted facade change and the two intentional integration changes
    # are the only files allowed to differ from the historical replay.
    changed = {facade.as_posix(), (copy / 'scripts/_measure/quality_archive.py').as_posix(),
               (copy / 'scripts/_measure/continuity.py').as_posix()}
    details = str(refused.value).split(': ', 1)[1].split(' (replay at ', 1)[0]
    assert set(details.split(', ')) == changed
    assert _snapshot(copy) == before
    for rel in mover.WRITTEN:
        if rel == 'measure.py':
            continue
        assert (SCRIPTS / rel).read_bytes() != (scratch / 'scripts' / rel).read_bytes(), rel
    assert (REPO / 'scripts/mutations/_measure_lane6.json').read_bytes() == (scratch / 'fragments.json').read_bytes()
    base_path = _base_scripts(tmp_path)
    errors, handlers = mover.audit_tree(base_path, SCRIPTS, owners)
    # Added resume pricing uses its own read-only connection and callback;
    # verbosity calibration now records a signed delta using the builtin sum.
    assert set(errors) == {
        "_log_resume_lean_savings: global turned into a local binding: ['conn']",
        "_log_resume_lean_savings: child scope count 0 -> 1",
        "_calibrate_prior_verbosity_nudges: unqualified facade/global names left: ['sum']",
    }
    moved_handlers = sum(1 for module in PACKET['modules']
                         for node in ast.walk(ast.parse((SCRIPTS / '_measure' / (module + '.py')).read_text(encoding='utf-8')))
                         if isinstance(node, ast.ExceptHandler) and node.name)
    assert handlers == moved_handlers > 0


@pytest.mark.parametrize('which', ['base-unobtainable', 'base-wrong-hash', 'inputs-unobtainable', 'inputs-wrong-hash'])
def test_unobtainable_or_mismatched_pins_refuse_before_any_write(tmp_path, monkeypatch, which):
    mover = _mover()
    real_git = mover._git
    def offline(*args):
        if args[0] == 'fetch':
            raise mover.MoveError('offline: fetch refused by the test')
        return real_git(*args)
    monkeypatch.setattr(mover, '_git', offline)
    bogus = '0' * 40
    if which == 'base-unobtainable':
        monkeypatch.setitem(mover.FROZEN_BASE, 'sha', bogus)
    elif which == 'base-wrong-hash':
        monkeypatch.setitem(mover.FROZEN_BASE, 'sha256', '1' * 64)
    elif which == 'inputs-unobtainable':
        monkeypatch.setitem(mover.LANE_INPUTS, 'sha', bogus)
    else:
        monkeypatch.setitem(mover.LANE_INPUTS, 'facade_sha256', '1' * 64)
    scripts = tmp_path / 'scripts'
    (scripts / '_measure').mkdir(parents=True)
    (scripts / 'measure.py').write_text('X = 1\n', encoding='utf-8', newline='\n')
    fragments = tmp_path / 'fragments.json'
    fragments.write_text('[]\n', encoding='utf-8', newline='\n')
    before = _snapshot(tmp_path)
    assert mover.main(['--scripts', str(scripts), '--fragments', str(fragments),
                       '--scratch', str(tmp_path / 'replay')]) == 1
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / 'replay').exists()


_FROZEN = '''import os
LIMIT = 5
def helper(x):
    return x + 1
def alpha(a, b=LIMIT):
    """Doc."""
    return helper(a) + b + len(os.sep)
LIMIT = 7
class Boom(Exception):
    pass
def tail():
    return LIMIT
'''
_OWNERS = {'alpha': 'quality_archive', 'Boom': 'continuity'}


def _synthetic(tmp_path, mover, frozen=_FROZEN):
    import hashlib
    scripts = tmp_path / 'scripts'
    (scripts / '_measure').mkdir(parents=True)
    (scripts / 'measure.py').write_text(frozen, encoding='utf-8', newline='\n')
    for domain in mover.TOOL.DOMAINS:
        (scripts / '_measure' / (domain + '.py')).write_text(mover.TOOL.HEADER, encoding='utf-8', newline='\n')
    lines = frozen.splitlines()
    rows = {n.name: {'name': n.name, 'start': n.lineno, 'end': n.end_lineno,
                     'sha256': hashlib.sha256('\n'.join(lines[n.lineno-1:n.end_lineno]).encode()).hexdigest()}
            for n in ast.parse(frozen).body if getattr(n, 'name', None) in _OWNERS}
    fragments = tmp_path / 'fragments.json'
    fragments.write_text('[]\n', encoding='utf-8', newline='\n')
    return scripts, rows, fragments


def _run(mover, scripts, rows, fragments, frozen=_FROZEN, facade_input=_FROZEN):
    inputs = {'ref': 'synthetic', 'measure.py': facade_input, 'fragments': '[]\n',
              'providers': {d: mover.TOOL.HEADER for d in mover.TOOL.DOMAINS}}
    return mover.run(scripts, _OWNERS, rows, frozen, fragments, inputs=inputs,
                     scratch=scripts.parent / 'replay')


def test_synthetic_move_then_rerun_is_a_validated_no_op(tmp_path):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    assert _run(mover, scripts, rows, fragments)['moved'] == 2
    facade = (scripts / 'measure.py').read_text(encoding='utf-8')
    assert "alpha = _measure_compat.bind(_measure_providers['quality_archive'].alpha, __name__, defaults=(LIMIT,))" in facade
    provider = (scripts / '_measure/quality_archive.py').read_text(encoding='utf-8')
    assert 'def alpha(a, b=None):' in provider and '_measure_facade.helper(a)' in provider
    assert _run(mover, scripts, rows, fragments) == {'moved': 0, 'already_moved': 2, 'replay_identical': 4}


@pytest.mark.parametrize('target, old, new, message', [
    ('_measure/quality_archive.py', 'helper(a) + b', 'helper(a) - b', 'body drift'),
    ('_measure/quality_archive.py', 'def alpha(a, b=None):', 'def alpha(a, b=6):', 'body drift'),
    ('_measure/quality_archive.py', '_measure_facade.helper(a)', 'helper(a)', 'body drift'),
    ('_measure/continuity.py', '    pass', '    x = 1', 'body drift'),
    ('measure.py', 'defaults=(LIMIT,)', 'defaults=(6,)', 'binding drift'),
    ('measure.py', 'defaults=(LIMIT,)', 'defaults=(LIMIT,), decorators=(staticmethod,)', 'binding drift'),
])
def test_completed_state_refuses_body_and_default_drift(tmp_path, target, old, new, message):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    _run(mover, scripts, rows, fragments)
    path = scripts / target
    text = path.read_text(encoding='utf-8')
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding='utf-8', newline='\n')
    with pytest.raises(mover.MoveError, match=message):
        _run(mover, scripts, rows, fragments)


def test_completed_state_refuses_moved_binding_position(tmp_path):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    _run(mover, scripts, rows, fragments)
    path = scripts / 'measure.py'
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    alpha = next(i for i, line in enumerate(lines) if line.startswith('alpha = '))
    boom = next(i for i, line in enumerate(lines) if line.startswith('Boom = '))
    lines[alpha], lines[boom] = lines[boom], lines[alpha]
    path.write_text(''.join(lines), encoding='utf-8', newline='\n')
    with pytest.raises(mover.MoveError, match='binding order drift'):
        _run(mover, scripts, rows, fragments)


def test_move_refuses_unfrozen_facade_or_frozen_hash(tmp_path):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    path = scripts / 'measure.py'
    changed = _FROZEN.replace('+ b +', '+ b * 2 +')
    path.write_text(changed, encoding='utf-8', newline='\n')
    with pytest.raises(mover.MoveError, match='differs from the pinned lane inputs'):
        _run(mover, scripts, rows, fragments)
    with pytest.raises(mover.MoveError, match='facade definition differs from frozen BASE'):
        _run(mover, scripts, rows, fragments, facade_input=changed)
    path.write_text(_FROZEN, encoding='utf-8', newline='\n')
    with pytest.raises(mover.MoveError, match='frozen BASE span/hash'):
        _run(mover, scripts, rows, fragments, frozen=_FROZEN.replace('return x + 1', 'return x + 1  # edited').replace('"""Doc."""', '"""Doc!"""'))
    assert path.read_text(encoding='utf-8') == _FROZEN


def _snapshot(root):
    root = Path(root)
    files = [p for p in sorted(root.rglob('*')) if p.is_file() and 'replay' not in p.relative_to(root).parts]
    return {p: p.read_bytes() for p in files}


def _move_line(path, prefix, anchor, after):
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    line = lines.pop(next(i for i, text in enumerate(lines) if text.startswith(prefix)))
    at = next(i for i, text in enumerate(lines) if text.startswith(anchor))
    lines.insert(at + 1 if after else at, line)
    path.write_text(''.join(lines), encoding='utf-8', newline='\n')


@pytest.mark.parametrize('anchor, after', [
    ('LIMIT = 5', False),   # above the constant its captured default reads: NameError at import
    ('LIMIT = 7', True),    # below a later reassignment: silently captures 7
])
def test_completed_state_refuses_binding_moved_across_initialization(tmp_path, anchor, after):
    """Astra U6 finding 1: lane order alpha < Boom is preserved; the position is not."""
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    _run(mover, scripts, rows, fragments)
    _move_line(scripts / 'measure.py', 'alpha = ', anchor, after)
    facade = (scripts / 'measure.py').read_text(encoding='utf-8')
    assert facade.index('alpha = ') < facade.index('Boom = ')
    before = _snapshot(tmp_path)
    with pytest.raises(mover.MoveError, match='alpha: position drift'):
        _run(mover, scripts, rows, fragments)
    assert _snapshot(tmp_path) == before


def test_fresh_move_refuses_definition_moved_across_initialization(tmp_path):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    facade = scripts / 'measure.py'
    lines = _FROZEN.splitlines(keepends=True)
    start = next(i for i, text in enumerate(lines) if text.startswith('def alpha'))
    block = lines[start:start + 3]
    del lines[start:start + 3]
    lines[1:1] = block          # alpha now defined above LIMIT = 5
    facade.write_text(''.join(lines), encoding='utf-8', newline='\n')
    before = _snapshot(tmp_path)
    with pytest.raises(mover.MoveError, match='differs from the pinned lane inputs'):
        _run(mover, scripts, rows, fragments)
    with pytest.raises(mover.MoveError, match='alpha: position drift'):
        _run(mover, scripts, rows, fragments, facade_input=''.join(lines))
    assert _snapshot(tmp_path) == before


def test_position_check_allows_other_lanes_and_bootstrap_lines(tmp_path):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    facade = scripts / 'measure.py'
    text = _FROZEN.replace('def helper(x):\n    return x + 1\n',
                           "helper = _measure_compat.bind(_measure_providers['runtime'].helper, __name__)\n")
    text = text.replace('import os\n', 'import os\n_measure_providers = {}\n')
    facade.write_text(text, encoding='utf-8', newline='\n')
    assert _run(mover, scripts, rows, fragments, facade_input=text)['moved'] == 2
    assert _run(mover, scripts, rows, fragments, facade_input=text) == {
        'moved': 0, 'already_moved': 2, 'replay_identical': 4}


@pytest.mark.parametrize('hole, target, edit', [
    ('body edit', '_measure/quality_archive.py', lambda t: t.replace('helper(a) + b', 'helper(a) - b')),
    ('default edit', 'measure.py', lambda t: t.replace('defaults=(LIMIT,)', 'defaults=(6,)')),
    ('binding moved across init', 'measure.py', None),
    ('extra provider statement', '_measure/quality_archive.py',
     lambda t: t + '\nraise RuntimeError("review-only import failure")\n'),
])
def test_replay_refuses_every_completed_state_hole(tmp_path, hole, target, edit):
    """Root fix: completed state means byte-identical to replaying the move on frozen inputs."""
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    _run(mover, scripts, rows, fragments)
    path = scripts / target
    if edit is None:
        _move_line(path, 'alpha = ', 'LIMIT = 5', False)
    else:
        text = path.read_text(encoding='utf-8')
        changed = edit(text)
        assert changed != text
        path.write_text(changed, encoding='utf-8', newline='\n')
    before = _snapshot(tmp_path)
    with pytest.raises(mover.MoveError, match='differs from a replay') as refused:
        _run(mover, scripts, rows, fragments)
    assert path.as_posix() in str(refused.value)
    assert _snapshot(tmp_path) == before


def test_fresh_move_refuses_a_changed_constant_outside_the_bindings(tmp_path):
    """No-history fresh-path twin of the U4 case: LIMIT 5 -> 999 before the move."""
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    (scripts / 'measure.py').write_text(_FROZEN.replace('LIMIT = 5', 'LIMIT = 999'), encoding='utf-8', newline='\n')
    before = _snapshot(tmp_path)
    with pytest.raises(mover.MoveError, match='differs from the pinned lane inputs'):
        _run(mover, scripts, rows, fragments)
    assert _snapshot(tmp_path) == before


def test_completed_synthetic_tree_refuses_a_changed_constant_outside_the_bindings(tmp_path):
    mover = _mover()
    scripts, rows, fragments = _synthetic(tmp_path, mover)
    _run(mover, scripts, rows, fragments)
    facade = scripts / 'measure.py'
    facade.write_text(facade.read_text(encoding='utf-8').replace('LIMIT = 7', 'LIMIT = 999'), encoding='utf-8', newline='\n')
    before = _snapshot(tmp_path)
    with pytest.raises(mover.MoveError, match='differs from a replay') as refused:
        _run(mover, scripts, rows, fragments)
    assert facade.as_posix() in str(refused.value)
    assert _snapshot(tmp_path) == before
