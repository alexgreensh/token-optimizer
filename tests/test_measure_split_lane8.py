"""Lane U8: dashboard and lifecycle bodies live in _measure, behaviour stays on measure."""
import __future__
import ast
import functools
import importlib.util
import inspect
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
MODULES = ('dashboard', 'lifecycle')
PACKET = json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))['lanes']['8']
ROWS = PACKET['symbols']


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def m(tmp_path):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home,
                               'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        yield module


def _provider_tree(module):
    return ast.parse((SCRIPTS / '_measure' / (module + '.py')).read_text(encoding='utf-8'))


def test_every_lane_symbol_has_one_body_in_its_module_and_one_binding():
    facade = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    facade_defs = {n.name for n in facade.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    bindings = {n.targets[0].id: ast.unparse(n.value.args[0]) for n in facade.body
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value.func) == '_measure_compat.bind'}
    provided = {}
    for module in MODULES:
        for node in _provider_tree(module).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                provided.setdefault(node.name, []).append(module)
    assert len(ROWS) == 204
    for row in ROWS:
        name = row['name']
        assert name not in facade_defs, name
        assert provided.get(name) == [row['module']], name
        assert bindings[name] == f"_measure_providers[{row['module']!r}].{name}", name
    assert set(provided) == {row['name'] for row in ROWS}


@functools.lru_cache(maxsize=1)
def _facade_has_future_annotations():
    tree = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    return any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
               and any(a.name == 'annotations' for a in n.names) for n in tree.body)


def test_providers_hold_no_state_no_globals_and_match_facade_future_flags():
    for module in MODULES:
        tree = _provider_tree(module)
        for node in tree.body:
            assert (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    or isinstance(node, ast.ImportFrom) and node.level == 1
                    and [a.name for a in node.names] == ['_compat']
                    or isinstance(node, ast.ImportFrom) and node.module == '__future__'), (module, node.lineno)
        assert not any(isinstance(node, ast.Global) for node in ast.walk(tree)), module
        # Bodies must compile under the same future flags as measure.py did.
        provider_future = any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
                              and any(a.name == 'annotations' for a in n.names) for n in tree.body)
        assert provider_future == _facade_has_future_annotations(), module


def test_exports_keep_base_signatures_defaults_and_annotation_objects(m):
    """Each export matches its frozen BASE signature evaluated in the facade."""
    for row in ROWS:
        name = row['name']
        namespace = dict(m.__dict__)
        flags = __future__.annotations.compiler_flag if _facade_has_future_annotations() else 0
        exec(compile(row['signature'] + '\n    pass\n', 'base-signature', 'exec', flags=flags,
                     dont_inherit=True), namespace)
        expected, actual = namespace[name], getattr(m, name)
        assert inspect.signature(actual) == inspect.signature(expected), name
        assert str(inspect.signature(actual)) == str(inspect.signature(expected)), name
        assert inspect.unwrap(actual).__defaults__ == inspect.unwrap(expected).__defaults__, name
        assert inspect.unwrap(actual).__kwdefaults__ == inspect.unwrap(expected).__kwdefaults__, name
        assert hasattr(actual, '__wrapped__') == hasattr(expected, '__wrapped__'), name
        assert actual.__module__ == m.__name__ and actual.__qualname__ == name
        assert Path(inspect.unwrap(actual).__code__.co_filename).name == row['module'] + '.py'
    expected_return = 'Path' if _facade_has_future_annotations() else m.Path
    assert m._codex_config_path.__annotations__['return'] == expected_return


def test_moved_code_keeps_the_facade_future_flags(m):
    # compile() inside _run_post_flush_extensions inherits these flags.
    flag = __future__.annotations.compiler_flag
    for name in ('_run_post_flush_extensions', 'generate_dashboard', 'setup_hook'):
        moved = bool(inspect.unwrap(getattr(m, name)).__code__.co_flags & flag)
        assert moved == _facade_has_future_annotations(), name


def test_same_and_cross_domain_patches_reach_moved_bodies_and_revert(m, tmp_path, monkeypatch):
    home = tmp_path / 'codex-home'
    home.mkdir()
    sentinel = home / 'sentinel.toml'
    originals = {name: getattr(m, name) for name in ('_codex_config_path', 'runtime_home',
                                                     '_is_running_from_plugin_cache')}
    with monkeypatch.context() as patch:
        # dashboard -> dashboard helper
        fake_path = Mock(return_value=sentinel)
        patch.setattr(m, '_codex_config_path', fake_path)
        patch.setattr(m, 'runtime_home', lambda: home)
        assert m._safe_codex_config_path() == sentinel
        assert fake_path.call_count == 1
        # dashboard -> runtime (another lane's helper)
        patch.undo()
        runtime = Mock(return_value=home)
        patch.setattr(m, 'runtime_home', runtime)
        assert m._codex_config_path() == home / 'config.toml'
        assert runtime.call_count == 1
        # lifecycle -> lifecycle helper
        patch.setattr(m, '_is_running_from_plugin_cache', Mock(return_value=True))
        assert m._get_measure_py_path() == '${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py'
    assert {name: getattr(m, name) for name in originals} == originals
    assert m._codex_config_path() != home / 'config.toml'
    patched = Mock(return_value=False)
    monkeypatch.setattr(m, '_is_running_from_plugin_cache', patched)
    monkeypatch.setattr(m, '_is_running_from_synced_plugin', patched)
    assert m._get_measure_py_path() == str(Path(m.__file__).resolve())
    assert patched.call_count == 2


def test_facade_file_os_and_path_stay_live_for_moved_bodies(m, tmp_path, monkeypatch):
    fake = tmp_path / 'elsewhere' / 'scripts' / 'measure.py'
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(m, '_is_running_from_plugin_cache', lambda: False)
    monkeypatch.setattr(m, '_is_running_from_synced_plugin', lambda: False)
    monkeypatch.setattr(m, '__file__', str(fake))
    assert m._get_measure_py_path() == str(fake.resolve())
    assert m._get_statusline_path().endswith('statusline.js')
    seen = []

    class FakePath(type(Path())):
        def __new__(cls, *args):
            seen.append(args)
            return super().__new__(cls, *args)
    monkeypatch.setattr(m, 'Path', FakePath)
    assert m._get_measure_py_path() == str(fake.resolve())
    assert seen and seen[0] == (str(fake),)


def test_context_manager_locks_keep_decorator_and_cleanup(m):
    for name in ('_config_lock', '_skill_mgmt_lock', '_codex_config_lock',
                 '_dashboard_selfheal_build_lock', '_settings_lock'):
        lock = getattr(m, name)
        assert hasattr(lock, '__wrapped__'), name
        assert Path(lock.__wrapped__.__code__.co_filename).parent.name == '_measure', name
    with pytest.raises(RuntimeError, match='inside'):
        with m._config_lock() as first:
            raise RuntimeError('inside')
    # The exceptional exit released the lease, so the next writer gets it too.
    with m._config_lock() as second:
        assert second == first


def test_lane_mutation_fragments_resolve_inside_moved_symbols():
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        guards = _load('_lane8_regression_guards', REPO / 'scripts/check_regression_guards.py')
    finally:
        sys.path.remove(str(REPO / 'scripts'))
    fragments = json.loads((REPO / PACKET['mutation_fragment']).read_text(encoding='utf-8'))
    assert len(fragments) == 20
    names = {f['mutation'] for f in fragments}
    mutations = [entry for _origin, entry in guards._load_partitioned() if entry[0] in names]
    assert {entry[0] for entry in mutations} == names
    owners = {row['name']: row['module'] for row in ROWS}
    for name, _tests, relpath, edits in mutations:
        changed = guards.resolve_targets(REPO, relpath, edits)
        lane_edits = [e for e in edits if e.get('symbol') in owners]
        for edit in lane_edits:
            # The resolver returns native relative paths (backslashes on Windows).
            target = Path('skills/token-optimizer/scripts/_measure') / (owners[edit['symbol']] + '.py')
            assert target in {Path(key) for key in changed}, (name, target, list(changed))


def _scope_attributes(node):
    """`_measure_facade.X` reads in this scope, not in nested scopes."""
    found = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            # Decorators, defaults and annotations still evaluate here.
            if not isinstance(child, ast.Lambda):
                stack.extend(getattr(child, 'decorator_list', []))
            continue
        if (isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
                and child.value.id == '_measure_facade'):
            found.append(child)
        stack.extend(ast.iter_child_nodes(child))
    return found


def test_local_bindings_are_never_rewritten_to_facade_attributes():
    """except-as names, loop/with/walrus targets, imports and params stay local."""
    import symtable
    checked = handlers = 0
    for module in MODULES:
        text = (SCRIPTS / '_measure' / (module + '.py')).read_text(encoding='utf-8')
        tree = ast.parse(text)
        top = symtable.symtable(text, module + '.py', 'exec')
        tables = {}

        def index(table):
            tables.setdefault((table.get_name(), table.get_lineno()), []).append(table)
            for child in table.get_children():
                index(child)
        index(top)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            table = next(t for t in tables[(node.name, node.lineno)] if t.get_type() == 'function')
            local = {s.get_name() for s in table.get_symbols()
                     if (s.is_local() or s.is_parameter()) and not s.is_global()} - {'_measure_facade'}
            for attribute in _scope_attributes(node):
                assert attribute.attr not in local, (module, node.name, attribute.lineno, attribute.attr)
            checked += 1
        handlers += sum(1 for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and h.name)
    assert checked >= 204 and handlers >= 36


def _ensure_commit(sha):
    """CI checks out shallow: fetch the one frozen commit explicitly, never skip."""
    import subprocess
    if subprocess.run(['git', 'cat-file', '-e', sha + '^{commit}'], cwd=REPO, capture_output=True).returncode == 0:
        return
    fetched = subprocess.run(['git', 'fetch', '--no-tags', '--depth=1', 'origin', sha],
                             cwd=REPO, capture_output=True, timeout=600)
    assert fetched.returncode == 0, ('cannot fetch frozen commit ' + sha + ': '
                                     + fetched.stderr.decode('utf-8', 'replace')[-400:])


def _base_scripts(tmp_path):
    import io
    import subprocess
    import tarfile
    base = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))['base']
    _ensure_commit(base)
    # The plugin manifest stops BASE's _read_plugin_version parent walk inside the fixture.
    result = subprocess.run(['git', 'archive', '--format=tar', base, 'skills/token-optimizer/scripts',
                             '.claude-plugin'], cwd=REPO, capture_output=True, check=True)
    target = tmp_path / 'base'
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(target, filter='data') if hasattr(tarfile, 'data_filter') else archive.extractall(target)
    return target / 'skills/token-optimizer/scripts/measure.py'


def _setup_hook_on_malformed_settings(module, settings, capsys, monkeypatch):
    settings.write_text('{not json', encoding='utf-8', newline='\n')
    monkeypatch.setattr(module, 'SETTINGS_PATH', settings)
    monkeypatch.setattr(module, 'detect_runtime', lambda: 'claude')
    with pytest.raises(SystemExit) as raised:
        module.setup_hook()
    out = capsys.readouterr()
    return raised.value.code, out.out, out.err, settings.read_text(encoding='utf-8')


def test_except_branch_error_path_matches_base(tmp_path, capsys, monkeypatch):
    """A real `except ... as e` branch: same message, exit code and untouched file."""
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    env = {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
           'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}
    settings = home / '.claude' / 'settings.json'
    try:
        json.loads('{not json')
    except json.JSONDecodeError as exc:
        reason = str(exc)
    with isolated_measure(env=env) as candidate:
        assert Path(inspect.unwrap(candidate.setup_hook).__code__.co_filename).name == 'lifecycle.py'
        with monkeypatch.context() as patch:
            actual = _setup_hook_on_malformed_settings(candidate, settings, capsys, patch)
    assert actual == (1, f'[Error] Could not read {settings}: {reason}\n', '', '{not json')
    base_path = _base_scripts(tmp_path)
    with isolated_measure(base_path, env=env) as base:
        assert Path(base.setup_hook.__code__.co_filename) == base_path
        with monkeypatch.context() as patch:
            expected = _setup_hook_on_malformed_settings(base, settings, capsys, patch)
    assert actual == expected


def _copy_scripts(tmp_path):
    copy = tmp_path / 'scripts'
    (copy / '_measure').mkdir(parents=True)
    shutil.copyfile(SCRIPTS / 'measure.py', copy / 'measure.py')
    for path in (SCRIPTS / '_measure').glob('*.py'):
        shutil.copyfile(path, copy / '_measure' / path.name)
    return copy


def _replace_once(path, old, new):
    text = path.read_text(encoding='utf-8')
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding='utf-8', newline='\n')


@pytest.fixture
def windows_newlines(monkeypatch):
    """Simulate Windows text mode on any host: default newline writes become CRLF."""
    real = Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        return real(self, data, encoding=encoding, errors=errors, newline='\r\n' if newline is None else newline)
    monkeypatch.setattr(Path, 'write_text', write_text)


def _tool(name):
    tool = _load(name, REPO / 'scripts/split_lanes/u8_move.py')
    _ensure_commit(tool.INPUT_COMMIT)
    tool.frozen_inputs()
    return tool


def _lane_copy(tmp_path):
    copy = _copy_scripts(tmp_path)
    fragments = tmp_path / 'fragments.json'
    shutil.copyfile(REPO / PACKET['mutation_fragment'], fragments)
    return copy, fragments


def _assert_refused(tool, copy, fragments, differing, diagnostic):
    files = [copy / 'measure.py', copy / '_measure/dashboard.py', copy / '_measure/lifecycle.py', fragments]
    before = {path: path.read_bytes() for path in files}
    with pytest.raises(tool.Refusal) as refused:
        tool.move(copy, fragments=fragments)
    message = str(refused.value)
    assert message.startswith('completed state differs from the replay of the frozen inputs:'), message
    listed = [line.split('differs: ', 1)[1] for line in message.splitlines() if line.startswith('  differs: ')]
    assert listed == [str(path) for path in differing], message
    assert diagnostic in message, message
    assert {path: path.read_bytes() for path in files} == before
    assert not list((REPO / '.scratch-u8').glob('replay-*'))


def test_completed_state_replays_identically_and_fresh_state_is_exact(tmp_path, windows_newlines):
    tool = _tool('_lane8_replay_ok')
    report = tool.move(SCRIPTS)
    assert report['state'] == 'complete' and report['replayed_files_identical'] == 4
    # A fresh state must be byte-identical to the frozen inputs; anything else replays.
    copy, fragments = _lane_copy(tmp_path)
    facade = copy / 'measure.py'
    facade.write_text(facade.read_text(encoding='utf-8') + '\n\ndef _get_statusline_path():\n    return None\n',
                      encoding='utf-8', newline='\n')
    _assert_refused(tool, copy, fragments, [facade], "_get_statusline_path: facade defs=1")


def test_completed_state_refuses_a_moved_body_edit(tmp_path, windows_newlines):
    tool = _tool('_lane8_replay_body')
    copy, fragments = _lane_copy(tmp_path)
    lifecycle = copy / '_measure/lifecycle.py'
    _replace_once(lifecycle, '.resolve().parent / "statusline.js")', '.resolve().parent / "statusline.mjs")')
    _assert_refused(tool, copy, fragments, [lifecycle],
                    '_get_statusline_path: moved body drifted from its frozen source')


def test_completed_state_refuses_a_captured_default_edit(tmp_path, windows_newlines):
    tool = _tool('_lane8_replay_default')
    copy, fragments = _lane_copy(tmp_path)
    facade = copy / 'measure.py'
    _replace_once(facade, "._settings_write_atomic_silent, __name__, defaults=(None,))",
                  "._settings_write_atomic_silent, __name__, defaults=(True,))")
    _assert_refused(tool, copy, fragments, [facade],
                    '_settings_write_atomic_silent: facade binding (defaults/decorators) drifted')


def test_completed_state_refuses_a_binding_moved_across_initialization(tmp_path, windows_newlines):
    tool = _tool('_lane8_replay_position')
    copy, fragments = _lane_copy(tmp_path)
    facade = copy / 'measure.py'
    constant = '_CONFIG_LOCK_PATH = CONFIG_DIR / ".config.lock"\n'
    binding = ("_config_lock = _measure_compat.bind(_measure_providers['lifecycle']._config_lock, "
               "__name__, decorators=(contextmanager,))\n")
    _replace_once(facade, constant + '\n\n' + binding, binding + '\n\n' + constant)
    _assert_refused(tool, copy, fragments, [facade], '_config_lock: binding moved across frozen initialization')
    source = facade.read_text(encoding='utf-8')
    with pytest.raises(tool.Refusal, match='_config_lock: binding moved'):
        tool.verify_positions(source, ast.parse(source), ROWS)


def test_completed_state_refuses_an_extra_provider_statement(tmp_path, windows_newlines):
    tool = _tool('_lane8_replay_extra')
    copy, fragments = _lane_copy(tmp_path)
    dashboard = copy / '_measure/dashboard.py'
    dashboard.write_text(dashboard.read_text(encoding='utf-8') + '\nraise RuntimeError("review-only import failure")\n',
                         encoding='utf-8', newline='\n')
    _assert_refused(tool, copy, fragments, [dashboard], 'dashboard.py: executable statements outside definitions')


def test_completed_state_refuses_a_changed_constant_outside_lane_bindings(tmp_path, windows_newlines):
    """The frozen BASE is the pinned commit, so a non-lane edit cannot be blessed."""
    tool = _tool('_lane8_replay_constant')
    copy, fragments = _lane_copy(tmp_path)
    facade = copy / 'measure.py'
    _replace_once(facade, '\n_SETTINGS_WRITE_TRIES = 2\n', '\n_SETTINGS_WRITE_TRIES = 999\n')
    _assert_refused(tool, copy, fragments, [facade], 'completed state differs')


def test_refuses_without_writes_when_the_frozen_commit_cannot_be_obtained(tmp_path, monkeypatch):
    tool = _load('_lane8_no_history', REPO / 'scripts/split_lanes/u8_move.py')
    copy, fragments = _lane_copy(tmp_path)
    files = [copy / 'measure.py', copy / '_measure/dashboard.py', copy / '_measure/lifecycle.py', fragments]
    before = {path: path.read_bytes() for path in files}
    calls = []
    real_run = tool.subprocess.run

    def no_history(argv, **kwargs):
        calls.append(argv[:2])
        if argv[:2] == ['git', 'fetch']:
            return tool.subprocess.CompletedProcess(argv, 128, b'', b'fatal: no such object')
        if argv[:2] in (['git', 'cat-file'], ['git', 'show']):
            return tool.subprocess.CompletedProcess(argv, 128, b'', b'fatal: not a valid object')
        return real_run(argv, **kwargs)
    monkeypatch.setattr(tool.subprocess, 'run', no_history)
    with pytest.raises(tool.Refusal, match='frozen inputs unavailable'):
        tool.move(copy, fragments=fragments)
    assert ['git', 'fetch'] in calls  # it tried the shallow-safe fetch first
    assert {path: path.read_bytes() for path in files} == before
    # A wrong pin also refuses, even when git can serve the commit.
    monkeypatch.setattr(tool.subprocess, 'run', real_run)
    _ensure_commit(tool.INPUT_COMMIT)
    monkeypatch.setitem(tool.INPUTS, 'skills/token-optimizer/scripts/measure.py', '0' * 64)
    with pytest.raises(tool.Refusal, match='does not match its pinned sha256'):
        tool.move(copy, fragments=fragments)
    assert {path: path.read_bytes() for path in files} == before


def test_base_fixture_version_lookup_stays_inside_the_fixture(tmp_path):
    """BASE's _read_plugin_version walks parents; the fixture must stop that walk."""
    base_path = _base_scripts(tmp_path)
    found = next((parent for parent in base_path.resolve().parents
                  if (parent / '.claude-plugin' / 'plugin.json').is_file()), None)
    assert found is not None and found.is_relative_to(tmp_path.resolve()), found
