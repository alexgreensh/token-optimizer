"""Lane U7 proof: the daemon packet runs from _measure/daemon.py, unchanged in behaviour.

Static ownership and scope checks, live facade lookups through patched helpers, captured defaults,
state that stays on measure, BASE-against-candidate error paths and the lane's mutation fragments.
The deterministic mover script is proved in test_measure_split_lane7_replay.py.
"""
import ast
import builtins
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import shutil
import subprocess
import symtable
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure, measure_source
from _lane7_helpers import (  # noqa: F401,F811  (fixtures are used by name)
    DEFINITION,
    FRAGMENTS,
    lf_fixtures,
    mover,
    NAMES,
    OWNERSHIP,
    PACKET,
    PROVIDER,
    PROVIDER_REL,
    provider_tree,
    REPO,
    SCRIPTS,
    _fixture_env,
)


@pytest.fixture
def m(tmp_path, monkeypatch):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    with isolated_measure(env=_fixture_env(home)) as module:
        yield module


# ---- ownership -----------------------------------------------------------------------------------

def test_packet_is_the_full_daemon_lane():
    assert PACKET['modules'] == ['daemon']
    assert len(NAMES) == len(set(NAMES)) == 126
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))
    owned = {row['name'] for row in manifest['symbols'] if row['module'] == 'daemon'}
    assert owned == set(NAMES)


def test_every_packet_body_lives_once_in_the_provider_and_is_bound_once_in_the_facade():
    bodies = [n.name for n in provider_tree().body if isinstance(n, DEFINITION)]
    assert sorted(bodies) == sorted(NAMES)
    facade = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    left = [n.name for n in facade.body if isinstance(n, DEFINITION) and n.name in set(NAMES)]
    assert left == []
    bound = {}
    for node in facade.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            bound.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    for name in NAMES:
        assert bound[name] == [f"_measure_providers['daemon'].{name}"], name


def test_provider_carries_definitions_only_no_domain_state():
    for node in provider_tree().body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom):
            assert node.module == '__future__' or (node.level == 1 and node.module is None
                                                  and [a.name for a in node.names] == ['_compat'])
            continue
        assert isinstance(node, DEFINITION), ast.unparse(node)[:80]


KNOWN_SIZE_CONFLICT = []


def test_facade_budget_and_file_ceiling():
    assert len(PROVIDER.read_text(encoding='utf-8').splitlines()) <= 9000
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        import check_measure_split
        errors, counts = check_measure_split.check(REPO)
    finally:
        sys.path.remove(str(REPO / 'scripts'))
    # Resolved on the aggregate: aa356ec6's pass-1 ceiling (9,000) covers the daemon packet,
    # so the guard reports no size errors at all. Anything that appears again still fails.
    assert errors == KNOWN_SIZE_CONFLICT, errors
    assert counts['moved'] >= len(NAMES)


# ---- scope: every old module global is reached through the facade --------------------------------

def _global_references(table, found):
    for symbol in table.get_symbols():
        if symbol.is_referenced() and symbol.is_global() and table.get_type() != 'module':
            found.add(symbol.get_name())
    for child in table.get_children():
        _global_references(child, found)


def test_no_unqualified_facade_global_remains_in_the_provider():
    source = PROVIDER.read_text(encoding='utf-8')
    found = set()
    _global_references(symtable.symtable(source, 'daemon.py', 'exec'), found)
    allowed = {name for name in dir(builtins)} - {'__name__'} | {'_compat'}
    leaked = sorted(found - allowed)
    assert leaked == []
    assert not found & {'__file__', '__name__'}
    assert not [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Global)]


def _bound_names(function):
    """Every name a body binds without a Name node, or as a plain local."""
    def stores(node):
        return [n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))]
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.ExceptHandler) and node.name:
            found.add(node.name)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            found.update(stores(node.target))
        elif isinstance(node, ast.comprehension):
            found.update(stores(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    found.update(stores(item.optional_vars))
        elif isinstance(node, ast.NamedExpr):
            found.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.update((a.asname or a.name.split('.')[0]) for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            found.update(p.arg for p in a.posonlyargs + a.args + a.kwonlyargs)
            found.update(p.arg for p in (a.vararg, a.kwarg) if p)
            if node is not function and not isinstance(node, ast.Lambda):
                found.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.add(node.id)
    found.discard('_measure_facade')
    return found


def test_local_bindings_are_never_rewritten_to_facade_attributes():
    """except-as names, loop/with/comprehension/walrus targets and plain locals stay local,
    even when they share a spelling with a module-level name in measure.py."""
    facade_names = {s.get_name() for s in symtable.symtable(
        (SCRIPTS / 'measure.py').read_text(encoding='utf-8'), 'measure.py', 'exec').get_symbols()
        if s.is_assigned() or s.is_imported()}
    shared, written, handlers = set(), set(), 0
    for function in provider_tree().body:
        if not isinstance(function, ast.FunctionDef):
            continue
        spelled = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                   and isinstance(n.value, ast.Name) and n.value.id == '_measure_facade'}
        written |= {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == '_measure_facade'
                    and isinstance(n.ctx, (ast.Store, ast.Del))}
        local = _bound_names(function)
        handlers += sum(isinstance(n, ast.ExceptHandler) and bool(n.name) for n in ast.walk(function))
        assert not local & spelled, (function.name, sorted(local & spelled))
        shared |= local & facade_names
    # Only the two caches this lane owns the writes of are ever stored on the facade.
    assert written == {'_PROJECT_JSONL_STAT_CACHE', '_PROJECT_JSONL_STAT_CACHE_TS'}
    # The check is only meaningful if the collisions it guards against really exist here.
    # On the aggregate the facade's module-level names are the split remainders: the real
    # collisions are import spellings, not the old unsplit except/loop names.
    assert handlers >= 8 and {'contextmanager', 'signal'} <= shared


def test_moved_bodies_restore_to_their_frozen_hashes():
    """Undoing only the qualification must give back the BASE bytes of every body."""
    restored = measure_source()
    tree = ast.parse(restored)
    lines = restored.splitlines()
    by_name = {n.name: n for n in tree.body if isinstance(n, DEFINITION)}
    manifest = {row['name']: row for row in json.loads(
        (REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))['symbols']}
    bad = []
    for name in NAMES:
        node = by_name[name]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        digest = hashlib.sha256('\n'.join(lines[first - 1:node.end_lineno]).encode()).hexdigest()
        if digest != manifest[name]['sha256']:
            bad.append(name)
    assert bad == []


def test_signatures_and_definition_time_defaults_match_the_frozen_source(m):
    manifest = {row['name']: row for row in json.loads(
        (REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))['symbols']}
    for name in NAMES:
        namespace = dict(vars(m))
        exec('from __future__ import annotations\n' + manifest[name]['signature'] + '\n    pass', namespace)
        original, exported = namespace[name], getattr(m, name)
        shape = lambda fn: [(p.name, p.kind, p.default, p.annotation) for p in inspect.signature(fn).parameters.values()]
        assert shape(exported) == shape(original), name
        assert exported.__defaults__ == original.__defaults__, name
        assert exported.__kwdefaults__ == original.__kwdefaults__, name
        assert exported.__annotations__ == original.__annotations__, name
        assert exported.__module__ == m.__name__
        assert exported.__qualname__ == name
        assert Path(exported.__code__.co_filename) == PROVIDER


def test_defaults_are_captured_once_not_read_live(m, monkeypatch):
    assert m.kill_stale_sessions.__defaults__ == (12, False, False)
    original = m._reclaim_posix_daemon_port.__defaults__
    assert original[0] == m.DAEMON_PORT
    monkeypatch.setattr(m, 'DAEMON_PORT', m.DAEMON_PORT + 1)
    assert m._reclaim_posix_daemon_port.__defaults__ == original


# ---- live facade lookups -------------------------------------------------------------------------

def test_same_domain_helper_patch_is_effective_and_undone(m, monkeypatch):
    fake = Mock(return_value='claude-sentinel')
    with monkeypatch.context() as patch:
        patch.setattr(m, '_posix_comm_name', fake)
        assert m._posix_is_claude_host('/usr/bin/anything', '') is True
        assert fake.call_count == 1
    assert m._posix_is_claude_host('/usr/bin/anything', '') is False
    assert fake.call_count == 1


def test_same_domain_matcher_patch_is_effective_and_undone(m, monkeypatch):
    fake = Mock(return_value=True)
    with monkeypatch.context() as patch:
        patch.setattr(m, '_command_matches_process', fake)
        assert m._posix_is_claude_host('worker', 'node worker.js') is True
        fake.assert_called_once_with('node worker.js', 'claude')
    assert m._posix_is_claude_host('worker', 'node worker.js') is False


def test_cross_domain_helper_patch_is_effective_and_undone(m, monkeypatch):
    fake = Mock(return_value=7)
    with monkeypatch.context() as patch:
        patch.setattr(m, '_int_env', fake)
        assert m._daemon_pulse_probe_seconds() == 7
        fake.assert_called_once_with('TOKEN_OPTIMIZER_DAEMON_PULSE_PROBE', 60)
    assert m._daemon_pulse_probe_seconds() == 60


def test_rebound_path_constant_redirects_the_next_call(m, monkeypatch, tmp_path):
    first, second = tmp_path / 'a.plist', tmp_path / 'b.plist'
    second.write_text('x', encoding='utf-8')
    monkeypatch.setattr(m, 'PLIST_PATH', first)
    assert m._daemon_service_installed('Darwin') is False
    monkeypatch.setattr(m, 'PLIST_PATH', second)
    assert m._daemon_service_installed('Darwin') is True


@pytest.mark.parametrize('code,expected', [(0, True), (1, False)])
def test_imported_subprocess_and_constants_are_the_facades(m, monkeypatch, code, expected):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(m, 'subprocess', SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(m, 'WINDOWS_TASK_NAME', 'sentinel-task')
    monkeypatch.setattr(m, '_NO_WINDOW', 0x08000000)
    assert m._daemon_service_installed('Windows') is expected
    assert calls == [(['schtasks', '/Query', '/TN', 'sentinel-task'],
                      {'capture_output': True, 'timeout': 5, 'creationflags': 0x08000000})]


def test_replaced_platform_module_reaches_the_moved_code(m, monkeypatch):
    monkeypatch.setattr(m, 'platform', SimpleNamespace(system=lambda: 'MacOSX'))
    assert m._normalized_platform() == 'Darwin'
    monkeypatch.setattr(m, 'platform', SimpleNamespace(system=lambda: 'windows'))
    assert m._normalized_platform() == 'Windows'


def test_facade_file_is_the_anchor_for_the_generated_server_and_version(m, monkeypatch, tmp_path):
    scripts = tmp_path / 'plugin' / 'cache' / 'fixture-market' / 'skills' / 'token-optimizer' / 'scripts'
    scripts.mkdir(parents=True)
    (tmp_path / 'plugin' / 'cache' / 'fixture-market' / '.claude-plugin').mkdir()
    (tmp_path / 'plugin' / 'cache' / 'fixture-market' / '.claude-plugin' / 'plugin.json').write_text(
        json.dumps({'version': '9.8.7'}), encoding='utf-8')
    fake_file = scripts / 'measure.py'
    monkeypatch.setattr(m, '__file__', str(fake_file))
    assert m._read_plugin_version() == '9.8.7'
    script = m._generate_daemon_script()
    assert repr(str(fake_file.resolve())) in script
    assert repr('fixture-market') in script


def test_global_cache_writes_land_on_the_facade_not_the_provider(m, monkeypatch, tmp_path):
    (tmp_path / 'projects' / 'p').mkdir(parents=True)
    (tmp_path / 'projects' / 'p' / 'a.jsonl').write_text('{}\n', encoding='utf-8')
    monkeypatch.setattr(m, 'CLAUDE_DIR', tmp_path)
    # stamp after the walk, TTL check, TTL check after a jump, stamp after the second walk
    clock = iter([100.0, 100.5, 500.0, 500.5])
    monkeypatch.setattr(m, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(m, '_PROJECT_JSONL_STAT_CACHE', None)
    monkeypatch.setattr(m, '_PROJECT_JSONL_STAT_CACHE_TS', 0.0)
    first = m._scan_project_jsonl_stats()
    assert [row[0].name for row in first] == ['a.jsonl']
    assert m._PROJECT_JSONL_STAT_CACHE is first
    assert m._PROJECT_JSONL_STAT_CACHE_TS == 100.0
    assert m._scan_project_jsonl_stats() is first          # inside the TTL window
    second = m._scan_project_jsonl_stats()                 # monotonic jumped past the TTL
    assert second is not first and m._PROJECT_JSONL_STAT_CACHE is second
    assert m._PROJECT_JSONL_STAT_CACHE_TS == 500.5
    owner = sys.modules['_measure.daemon']
    assert not hasattr(owner, '_PROJECT_JSONL_STAT_CACHE')
    assert not hasattr(owner, '_PROJECT_JSONL_STAT_CACHE_TS')


def test_generated_daemon_source_is_untouched_by_qualification(m):
    script = m._generate_daemon_script()
    compile(script, 'dashboard-server.py', 'exec')
    assert '_measure_facade' not in script and '_compat' not in script
    assert 'class Handler' in script
    assert script == m._generate_daemon_script()


def test_pure_helpers_keep_their_results(m):
    assert m._parse_elapsed_time('2-03:04:05') == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert m._parse_elapsed_time('04:05') == 245
    assert m._parse_elapsed_time('9') == 0
    assert [m._format_elapsed(s) for s in (59, 3600, 90000)] == ['0m', '1h 0m', '1d 1h']
    assert m._posix_comm_name('-Zsh') == 'zsh' and m._posix_comm_name('tmux: server') == 'tmux'
    assert m._posix_option_args(['claude', '-p', '--', 'x']) == ['-p']
    assert m._posix_is_headless(['-pc']) is True and m._posix_is_headless(['--resume']) is False


# ---- error paths: real `except OSError as <name>` branches, BASE against candidate ----------------

def _os_error_text(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return str(exc)
    raise AssertionError('the blocker must make mkdir fail')


def _failing_state_dir(work):
    work.mkdir()
    blocker = work / 'blocker'
    blocker.write_text('a file where the state directory would have to be', encoding='utf-8')
    return blocker / 'state'


def _error_path_outcomes(module, work):
    """Drive both handlers; return everything a caller or a user could observe."""
    state = _failing_state_dir(work)
    outcome = {}
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        outcome['tombstone_return'] = module._write_uninstall_tombstone(state)
    outcome['tombstone_stderr'] = captured.getvalue()
    saved = (module.SNAPSHOT_DIR, module.DAEMON_INSTALL_FAILED_BREADCRUMB,
             module._daemon_install_failed_marker_present)
    module.SNAPSHOT_DIR = state
    module.DAEMON_INSTALL_FAILED_BREADCRUMB = state / '.daemon-install-failed'
    module._daemon_install_failed_marker_present = lambda: False
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            outcome['marker_return'] = module._write_daemon_install_failed_marker('policy-denied')
    finally:
        (module.SNAPSHOT_DIR, module.DAEMON_INSTALL_FAILED_BREADCRUMB,
         module._daemon_install_failed_marker_present) = saved
    outcome['marker_stderr'] = captured.getvalue()
    outcome['tree'] = sorted(str(p.relative_to(work)) for p in work.rglob('*'))
    return outcome


def test_except_as_branches_keep_the_base_messages(m, tmp_path):
    work = tmp_path / 'work'
    outcome = _error_path_outcomes(m, work)
    state = work / 'blocker' / 'state'
    reason = _os_error_text(state)
    assert outcome == {
        'tombstone_return': None,
        'tombstone_stderr': ('[Token Optimizer] Warning: could not write uninstall tombstone '
                             f'at {state / ".daemon-thrash"}: {reason}\n'
                             '  If the daemon is still running after uninstall, remove the '
                             'LaunchAgent/task/unit manually.\n'),
        'marker_return': None,
        'marker_stderr': ('[Token Optimizer] Warning: could not persist the daemon install-failed marker '
                          f'({state / ".daemon-install-failed"}): {reason}. Failed installs will keep '
                          'retrying on the normal throttle instead of stopping permanently.\n'),
        'tree': ['blocker'],
    }


def _base_scripts(mover, tmp_path):
    """The frozen BASE tree from git. A shallow checkout (CI) fetches that one commit by SHA; if that
    fails the test fails, because a lane proof that silently skips is a proof that is missing."""
    base = mover.BASE
    try:
        mover.ensure_commit(REPO, base)
    except mover.MoveRefused as exc:
        pytest.fail(f'the frozen BASE {base} is neither in this checkout nor fetchable: {exc}')
    # The plugin manifest comes with the scripts: BASE's version discovery walks up from the imported file
    # looking for .claude-plugin/plugin.json and must stop at this fixture root, never above it.
    archive = subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', base,
                              'skills/token-optimizer/scripts', '.claude-plugin/plugin.json'],
                             capture_output=True, check=True).stdout
    destination = tmp_path / 'base'
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            relative = Path(member.name)
            assert not relative.is_absolute() and '..' not in relative.parts
            if member.isdir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                (destination / relative).parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source:
                    (destination / relative).write_bytes(source.read())
    assert (destination / '.claude-plugin' / 'plugin.json').is_file()
    facade = destination / 'skills/token-optimizer/scripts/measure.py'
    found = hashlib.sha256(facade.read_bytes()).hexdigest()
    assert found == mover.BASE_FACADE_SHA256, f'the BASE measure.py is not the pinned one: {found}'
    return destination / 'skills/token-optimizer/scripts'


def test_the_pinned_base_is_the_one_the_frozen_manifest_names(mover):
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))
    assert OWNERSHIP['base'] == manifest['base'] == mover.BASE
    assert manifest['provisional'] is False
    assert manifest['source_sha256'] == mover.BASE_FACADE_SHA256      # U0's own record of the BASE facade hash


def test_error_paths_are_identical_in_base_and_candidate(mover, tmp_path):
    base = _base_scripts(mover, tmp_path)
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    outcomes = []
    for label, path in (('base', base / 'measure.py'), ('candidate', SCRIPTS / 'measure.py')):
        work = tmp_path / 'work'
        if work.exists():
            shutil.rmtree(work)
        with isolated_measure(path=path, name='measure_u7_' + label, env=_fixture_env(home)) as module:
            # version discovery ended at the manifest that ships with this tree, not somewhere above it
            shipped = path.parents[3] / '.claude-plugin' / 'plugin.json' if label == 'base' else \
                REPO / '.claude-plugin' / 'plugin.json'
            assert module._read_plugin_version() == json.loads(shipped.read_text(encoding='utf-8'))['version']
            if label == 'candidate':
                assert Path(module._write_uninstall_tombstone.__code__.co_filename) == PROVIDER
            else:
                assert Path(module._write_uninstall_tombstone.__code__.co_filename) == path
            outcomes.append(_error_path_outcomes(module, work))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0]['tombstone_stderr'] and outcomes[0]['marker_stderr']


# ---- mutation fragments --------------------------------------------------------------------------

def test_every_lane_fragment_plants_inside_its_symbol_and_nowhere_else(monkeypatch):
    spec = importlib.util.spec_from_file_location('_u7_guards', REPO / 'scripts/check_regression_guards.py')
    guards = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, '_u7_guards', guards)
    spec.loader.exec_module(guards)
    fragments = json.loads(FRAGMENTS.read_text(encoding='utf-8'))
    assert len(fragments) == 13
    text = PROVIDER.read_text(encoding='utf-8')
    for fragment in fragments:
        assert fragment['symbol'] in NAMES
        changed = guards.resolve_targets(REPO, fragment['file'], [fragment])
        assert [Path(key) for key in changed] == [Path(PROVIDER_REL)], fragment['mutation']
        before, after = changed[next(iter(changed))]
        assert before == text and after != before
        mutated = ast.parse(after)
        assert {n.name for n in mutated.body if isinstance(n, DEFINITION)} == set(NAMES)
        differing = [n.name for n in provider_tree().body if isinstance(n, DEFINITION)
                     and ast.unparse(n) != ast.unparse(next(x for x in mutated.body if isinstance(x, DEFINITION)
                                                            and x.name == n.name))]
        assert differing == [fragment['symbol']], fragment['mutation']


def test_no_test_in_this_file_can_skip():
    """A proof that silently skips in CI is a proof that is missing (orchestrator rule, lane-1 review).

    Covers every file of the lane's proof: this one, the replay file and the shared helpers.
    """
    here = Path(__file__).parent
    for name in ('test_measure_split_lane7.py', 'test_measure_split_lane7_replay.py', '_lane7_helpers.py'):
        text = (here / name).read_text(encoding='utf-8')
        for banned in ('pytest.' + 'skip', 'skip' + 'if', 'import' + 'orskip', 'xfa' + 'il'):
            assert banned not in text, (name, banned)
