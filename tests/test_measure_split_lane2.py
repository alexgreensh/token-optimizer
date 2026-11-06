"""Lane U2 (sessions + CLI dispatch): ownership, live lookups, and CLI scope."""
import ast
import builtins
import json
import os
import subprocess
import symtable
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from helpers.measure_split import isolated_measure, source_path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
PACKAGE = SCRIPTS / '_measure'
LANE = json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))['lanes']['2']
SYMBOLS = {s['name']: s['module'] for s in LANE['symbols']}
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _tree(path):
    return ast.parse(path.read_text(encoding='utf-8'))


@pytest.fixture(scope='module', autouse=True)
def _u2_scratch(tmp_path_factory):
    """Derivations and the reference re-run live in tmp, never in the checkout.
    Shared by the module so the foundation reference is built once."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('U2_SCRATCH', str(tmp_path_factory.mktemp('u2-scratch')))
        yield


@pytest.fixture
def m(tmp_path):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home,
                               'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        yield module


def test_packet_is_exactly_the_sessions_and_cli_lane():
    assert LANE['modules'] == ['sessions', 'cli']
    assert len(SYMBOLS) == 100
    assert set(SYMBOLS.values()) == {'sessions'}
    assert LANE['inherited_canaries'] == []


def test_every_lane_symbol_has_one_body_in_its_provider_and_a_facade_binding():
    facade = _tree(SCRIPTS / 'measure.py')
    facade_defs = {n.name for n in facade.body if isinstance(n, DEFINITION)}
    bindings = {}
    for node in facade.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            bindings[node.targets[0].id] = ast.unparse(node.value.args[0])
    provider = [n.name for n in _tree(PACKAGE / 'sessions.py').body if isinstance(n, DEFINITION)]
    assert sorted(provider) == sorted(SYMBOLS)
    assert not facade_defs & set(SYMBOLS)
    for name in SYMBOLS:
        assert bindings[name] == f"_measure_providers['sessions'].{name}"
        assert source_path(name).name == 'sessions.py'


def test_cli_dispatch_lives_in_cli_main_behind_the_original_guard():
    facade = _tree(SCRIPTS / 'measure.py')
    guards = [n for n in facade.body if isinstance(n, ast.If)
              and ast.unparse(n.test) == "__name__ == '__main__'"]
    assert len(guards) == 1
    assert ast.unparse(guards[0].body) == "_measure_providers['cli'].main()"
    assert guards[0] is facade.body[-1]
    cli = [n.name for n in _tree(PACKAGE / 'cli.py').body if isinstance(n, DEFINITION)]
    assert cli == ['main']
    assert source_path('__main__').name == 'cli.py'


def test_every_cli_spelling_is_dispatched_from_cli_main():
    spellings = json.loads((REPO / 'tests/fixtures/measure_split/cases.json').read_text(encoding='utf-8'))['spellings']
    assert len(spellings) == 146
    tree = _tree(PACKAGE / 'cli.py')
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert [s for s in spellings if s not in literals] == []


@pytest.mark.parametrize('module', ['sessions', 'cli'])
def test_providers_hold_no_state_and_no_unresolved_globals(module):
    path = PACKAGE / f'{module}.py'
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in tree.body:
        assert (isinstance(node, DEFINITION)
                or isinstance(node, ast.ImportFrom) and node.module in ('__future__', None)
                or isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)), node.lineno
    unresolved = set()

    def walk(table):
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if (symbol.is_referenced() and symbol.is_global() and not symbol.is_assigned()
                    and name not in ('_compat', '_measure_facade') and not hasattr(builtins, name)):
                unresolved.add((table.get_name(), name))
        for child in table.get_children():
            walk(child)
    for child in symtable.symtable(source, str(path), 'exec').get_children():
        walk(child)
    assert unresolved == set()


def test_provider_modules_carry_no_copied_mutable_state(m):
    import _measure.sessions as sessions
    import _measure.cli as cli
    allowed = {'annotations', '_compat'}
    for provider, names in ((sessions, set(SYMBOLS)), (cli, {'main'})):
        public = {k for k in vars(provider) if not (k.startswith('__') and k.endswith('__'))}
        assert public == allowed | names


def _main_scope_handlers(tree):
    """Named except handlers in cli.main's own scope (not nested defs/lambdas)."""
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    found, pending = [], list(main.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.ExceptHandler) and node.name:
            found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def test_dispatch_except_handlers_keep_base_global_lifetime():
    """BASE binds `except E as e` at module scope as a global for the handler's
    life and deletes it after. U0's mover keeps exactly that on the facade."""
    handlers = _main_scope_handlers(_tree(PACKAGE / 'cli.py'))
    assert len(handlers) >= 14
    for handler in handlers:
        temporary = handler.name
        assert temporary.startswith('_measure_exception_'), handler.lineno
        first, guarded = handler.body[0], handler.body[1]
        assert ast.unparse(first.value) == temporary, handler.lineno
        target = ast.unparse(first.targets[0])
        assert target.startswith('_measure_facade.'), handler.lineno
        assert isinstance(guarded, ast.Try) and len(handler.body) == 2, handler.lineno
        assert ast.unparse(guarded.finalbody) == f'{target} = None\ndel {target}', handler.lineno
        # The suite reads the facade target, never the temporary.
        reads = {n.id for s in guarded.body for n in ast.walk(s) if isinstance(n, ast.Name)}
        assert temporary not in reads, handler.lineno


def _scope_names(function):
    """Facade attributes read in this scope, excluding nested scopes."""
    attrs, targets, pending = set(), set(), list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) == '_measure_facade':
            attrs.add(node.attr)
        if isinstance(node, ast.comprehension):
            targets |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        pending.extend(ast.iter_child_nodes(node))
    return attrs, targets


def _function_tables(table):
    if table.get_type() == 'function':
        yield table
    for child in table.get_children():
        yield from _function_tables(child)


@pytest.mark.parametrize('module', ['sessions', 'cli'])
def test_local_bindings_are_never_read_through_the_facade(module):
    """Parameters, for/with/comprehension/walrus targets, except names and
    nonlocals stay local. A function-scope name read as `_measure_facade.X`
    while X is also bound locally would split one variable into two."""
    path = PACKAGE / f'{module}.py'
    source = path.read_text(encoding='utf-8')
    tables = {(t.get_name(), t.get_lineno()): t
              for t in _function_tables(symtable.symtable(source, str(path), 'exec'))}
    checked = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        table = tables[(node.name, node.lineno)]
        local = {s.get_name() for s in table.get_symbols()
                 if s.is_local() or s.is_parameter() or s.is_free()} - {'_measure_facade'}
        attrs, comp_targets = _scope_names(node)
        if module == 'cli' and node.name == 'main':
            # Every dispatch binding is a facade global except: handler names,
            # comprehension targets (inlined on 3.12+), and the mover's
            # temporaries for nested defs and dotted imports, which are mirrored
            # onto the facade and deleted at once.
            handlers = {n.name for n in ast.walk(node) if isinstance(n, ast.ExceptHandler) and n.name}
            nested = {n.name for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n is not node}
            dotted = {a.name.split('.')[0] for n in ast.walk(node) if isinstance(n, ast.Import)
                      for a in n.names if '.' in a.name and a.asname is None}
            comps = {x.id for n in ast.walk(node) if isinstance(n, ast.comprehension)
                     for x in ast.walk(n.target) if isinstance(x, ast.Name)}
            temporaries = {n for n in local if n.startswith(('_measure_import_', '_measure_exception_'))}
            assert local - temporaries <= handlers | nested | dotted | comps
            for name in nested | dotted:
                assert f'_measure_facade.{name} = ' in source, name
            continue
        assert not attrs & (local | comp_targets), (node.name, attrs & (local | comp_targets))
        checked += 1
    assert checked >= (100 if module == 'sessions' else 6)


def _error_env(home, snapshot):
    (home / '.claude').mkdir(parents=True, exist_ok=True)
    return {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home),
            'LOCALAPPDATA': str(home / 'AppData/Local'), 'APPDATA': str(home / 'AppData/Roaming'),
            'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache'),
            'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_STATE_HOME': str(home / '.local/state'),
            'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
            'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(snapshot), 'TOKEN_OPTIMIZER_RUNTIME': 'claude',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}


# Runs the real public __main__ (runpy) with one named function raising on
# entry, so the same failure is injected into BASE and candidate unmodified.
_RAISE_IN = '''import runpy, sys
script, target = sys.argv[1], sys.argv[2]
def trace(frame, event, arg):
    if event == "call" and frame.f_code.co_name == target:
        raise RuntimeError("fixture boom")
sys.argv = [script, *sys.argv[3:]]
sys.path.insert(0, script.rsplit("/", 1)[0] if "/" in script else ".")
sys.settrace(trace)
runpy.run_path(script, run_name="__main__")
'''


def _error_cases(tmp_path):
    """Two real dispatch except branches: consent write failure, api-manage fail-open."""
    blocker = tmp_path / 'blocker'
    blocker.write_text('')
    driver = tmp_path / 'raise_in.py'
    driver.write_text(_RAISE_IN)
    consent = (_error_env(tmp_path / 'home-consent', blocker / 'data'),
               lambda script: [str(script), 'daemon-consent', '--set', 'yes'])
    manage = (_error_env(tmp_path / 'home-manage', tmp_path / 'home-manage/data'),
              lambda script: ['-I', str(driver), str(script).replace(os.sep, '/'), '_manage_mcp',
                              'api-manage', 'mcp/disable', 'fixture'])
    return blocker, {'consent': consent, 'api-manage': manage}


def _run(script, env, argv):
    result = subprocess.run([sys.executable, *argv(script)], env=env, cwd=env['HOME'],
                            input=b'', capture_output=True, timeout=120)
    return result.returncode, result.stdout, result.stderr


def test_dispatch_error_handlers_print_base_output(tmp_path):
    """Expected bytes captured from BASE before the move (same on e946a89a and 55da0b11)."""
    blocker, cases = _error_cases(tmp_path)
    errno_text = os.strerror(20) if os.name != 'nt' else None
    code, out, err = _run(SCRIPTS / 'measure.py', *cases['consent'])
    assert (code, err) == (1, b'')
    if errno_text:
        assert out == (f"[Error] Could not write consent file: [Errno 20] {errno_text}: "
                       f"'{blocker / 'data'}'\n").encode()
    else:
        assert out.startswith(b'[Error] Could not write consent file: ')
    code, out, err = _run(SCRIPTS / 'measure.py', *cases['api-manage'])
    assert (code, out, err) == (0, b'{"ok": false, "message": "error: fixture boom", "manage": null}', b'')


def test_dispatch_error_handlers_match_live_base(tmp_path):
    base = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))['base']
    # CI clones are shallow: fetch the BASE commit explicitly, never skip.
    assert _mover().ensure_commit(base), f'cannot fetch BASE {base}'
    archive = tmp_path / 'base.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), base,
                    'skills/token-optimizer/scripts'], cwd=REPO, check=True)
    import tarfile
    with tarfile.open(archive) as stream:
        stream.extractall(tmp_path / 'base', **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    base_script = tmp_path / 'base/skills/token-optimizer/scripts/measure.py'
    assert not (base_script.parent / '_measure').exists()
    _, cases = _error_cases(tmp_path)
    for name, (env, argv) in cases.items():
        assert _run(SCRIPTS / 'measure.py', env, argv) == _run(base_script, env, argv), name


def test_same_domain_helper_patch_and_undo(m, monkeypatch):
    fake = Mock(return_value='sentinel-owner')
    with monkeypatch.context() as patch:
        patch.setattr(m, '_canonical_session_uuid', fake)
        assert m._usage_owner_id('/x/not-a-uuid.jsonl') == 'sentinel-owner'
        assert fake.call_count == 1
    assert m._usage_owner_id('/x/not-a-uuid.jsonl') == '/x/not-a-uuid.jsonl'


def test_moved_body_calls_live_facade_helper(m, monkeypatch):
    fake = Mock(return_value=('sentinel-uuid', None))
    monkeypatch.setattr(m, '_extract_session_uuid', fake)
    assert m._canonical_session_uuid('/x/anything.jsonl') == 'sentinel-uuid'
    fake.assert_called_once_with('anything')


def test_facade_caller_reaches_patched_moved_helper(m, monkeypatch):
    claude_models = m.PRICING_TIERS['anthropic']['claude_models']
    real_key = next(iter(claude_models))
    assert m._is_priced_model('fixture-unpriced-model', tier='anthropic') is False
    fake = Mock(return_value=real_key)
    with monkeypatch.context() as patch:
        patch.setattr(m, '_claude_price_key', fake)
        assert m._is_priced_model('fixture-unpriced-model', tier='anthropic') is True
        assert fake.call_count == 1
    assert m._is_priced_model('fixture-unpriced-model', tier='anthropic') is False


def test_moved_bodies_read_rebound_constants_and_modules(m, monkeypatch):
    import sqlite3
    monkeypatch.setattr(m, '_COUNTED_PRICING_VERSION', 'X')
    monkeypatch.setattr(m, '_USAGE_PARSER_VERSION', 'Y')
    assert m._upgrade_drain_key() == 'countedX-usageY'
    conn = sqlite3.connect(':memory:')
    m._upgrade_drain_mark_done(conn)
    assert m._upgrade_meta_get(conn, m._UPGRADE_DRAIN_META) == 'countedX-usageY'
    broken = Mock(side_effect=sqlite3.OperationalError('fixture'))
    monkeypatch.setattr(m, 'sqlite3', Mock(Error=sqlite3.Error))
    assert m._upgrade_meta_set(Mock(execute=broken), 'k', 'v') is None


def test_hermes_rollup_globals_written_by_dispatch_are_read_from_facade(m):
    text = (PACKAGE / 'sessions.py').read_text(encoding='utf-8')
    assert '_measure_facade.__dict__.get("_HERMES_ROLLUP_CONTEXT", {})' in text
    assert '_measure_facade.__dict__.get("_HERMES_ROLLUP_SKILLS", {})' in text
    cli = (PACKAGE / 'cli.py').read_text(encoding='utf-8')
    assert '_measure_facade._HERMES_ROLLUP_CONTEXT = ' in cli
    assert '_measure_facade._HERMES_ROLLUP_SKILLS = ' in cli


def test_exports_keep_facade_identity(m):
    for name in SYMBOLS:
        value = getattr(m, name)
        assert value.__module__ == 'measure', name
        assert Path(value.__code__.co_filename).name == 'sessions.py', name


def test_lane_mutation_fragments_resolve_to_one_site():
    fragments = json.loads((REPO / LANE['mutation_fragment']).read_text(encoding='utf-8'))
    assert len(fragments) == 8
    for edit in fragments:
        assert edit['symbol'] in SYMBOLS
        path = source_path(edit['symbol'])
        assert path.name == 'sessions.py'
        assert path.read_text(encoding='utf-8').count(edit['old']) == 1, edit['mutation']


MOVE_SCRIPT = REPO / 'scripts/split_lanes/u2_move.py'
STATE = 'U2: 0 definitions to move, 100 already moved, dispatch moved'
BINDING = "_collect_drain_mode = _measure_compat.bind(_measure_providers['sessions']._collect_drain_mode, __name__)\n"
INIT = '_UPGRADE_DRAIN_META = "upgrade_drain_done"\n'


def _mover():
    import importlib.util
    spec = importlib.util.spec_from_file_location('_u2_move_under_test', MOVE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _foundation():
    """History, else the record; then fetched explicitly when this clone is shallow."""
    mover = _mover()
    foundation = mover.resolve_foundation()
    assert foundation, 'cannot resolve the foundation commit'
    assert mover.ensure_commit(foundation), f'cannot fetch foundation {foundation}'
    return foundation


def _run_mover(*extra, scripts=None, check=True):
    argv = [sys.executable, str(MOVE_SCRIPT), *(['--check'] if check else []), *extra]
    if scripts:
        argv += ['--scripts', str(scripts)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=1800)


def test_frozen_record_names_the_manifest_base_and_every_output():
    record = json.loads((REPO / 'scripts/split_lanes/u2_frozen.json').read_text(encoding='utf-8'))
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))
    assert (record['base'], record['source_sha256']) == (manifest['base'], manifest['source_sha256'])
    assert set(record['symbols']) == set(SYMBOLS)
    assert len({n for n, r in record['symbols'].items() if 'defaults=' in r['binding']}) == 26
    assert set(record['outputs']) == {'measure.py', '_measure/sessions.py', '_measure/cli.py',
                                      'scripts/mutations/_measure_lane2.json'}
    assert record['positions']
    assert len(record['foundation']) == 40


def test_completed_state_is_byte_identical_to_a_foundation_rerun():
    """The authoritative definition of "already applied"."""
    foundation = _foundation()
    result = _run_mover()
    # On the aggregate the shared facade and this lane's own frozen record carry
    # all eight lanes, so the byte check refuses naming exactly those two files;
    # the lane-owned outputs still match the foundation re-run (oracle, D1).
    assert result.returncode != 0, result.stdout
    assert 'refusing: lane output differs from the foundation re-run' in result.stderr, result.stderr
    differing = {Path(line.split(' != ')[0].strip()).name
                 for line in result.stderr.splitlines() if ' != ' in line}
    assert differing == {'measure.py', 'u2_frozen.json'}, result.stderr


def test_completed_state_diagnostics_pass_without_history():
    result = _run_mover('--no-reference', '--frozen', 'record')
    # The record is the lane's own output set: on the aggregate only the shared
    # facade differs from it; every lane-owned file still matches the record.
    assert result.returncode != 0, result.stdout
    differing = [line.strip() for line in result.stderr.splitlines() if 'differs from the recorded move' in line]
    assert differing == ['refusing: lane output file differs from the recorded move: measure.py'], result.stderr


def _drifted_copy(tmp_path, relative, edits):
    import shutil
    copy = tmp_path / 'scripts'
    shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    path = copy / relative
    text = path.read_text(encoding='utf-8')
    for old, new in edits:
        if old is None:
            text += new
            continue
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_bytes(text.encode('utf-8'))  # LF exactly, on every platform
    return copy


# One case per hole class found by review (bodies/defaults U7, positions U6,
# extra provider code U2), plus two extra binding/dispatch drifts.
HOLES = {
    'body-edit': ('_measure/sessions.py', [(
        '    return (int(message_count or 0), int(input_tokens or 0), int(output_tokens or 0))',
        '    return (int(message_count or 0), int(output_tokens or 0), int(input_tokens or 0))')],
        'moved body differs from frozen BASE derivation: _session_rank'),
    'default-edit': ('measure.py', [(
        "_measure_providers['sessions']._find_all_jsonl_files, __name__, defaults=(30,))",
        "_measure_providers['sessions']._find_all_jsonl_files, __name__, defaults=(31,))")],
        'facade binding (defaults/decorators) differs from frozen BASE: _find_all_jsonl_files'),
    'binding-moved-across-init': ('measure.py', [(BINDING, ''), (INIT, BINDING + INIT)],
        'binding position differs from frozen BASE (initialization order) near: _collect_drain_mode'),
    'extra-provider-statement': ('_measure/sessions.py', [(None, '\nraise RuntimeError("review-only import failure")\n')],
        'lane output file differs from the recorded move: _measure/sessions.py'),
    # Astra U4 finding 1: a constant outside any binding must not be blessed.
    'constant-outside-bindings': ('measure.py', [(
        '_UPGRADE_DRAIN_MAX_ATTEMPTS = 3\n', '_UPGRADE_DRAIN_MAX_ATTEMPTS = 999\n')],
        'lane output file differs from the recorded move: measure.py'),
    'added-default': ('measure.py', [(
        "_session_rank = _measure_compat.bind(_measure_providers['sessions']._session_rank, __name__)",
        "_session_rank = _measure_compat.bind(_measure_providers['sessions']._session_rank, __name__, defaults=(0,))")],
        'facade binding (defaults/decorators) differs from frozen BASE: _session_rank'),
    'dispatch-edit': ('_measure/cli.py', [(
        '    _measure_facade.args = _measure_facade.sys.argv[1:]',
        '    _measure_facade.args = _measure_facade.sys.argv[2:]')],
        'moved CLI dispatch differs from frozen BASE derivation'),
}


def _assert_refused_without_writes(copy, extra, messages):
    before = {p: p.read_bytes() for p in copy.rglob('*.py')}
    record = (REPO / 'scripts/split_lanes/u2_frozen.json').read_bytes()
    for check in (True, False):
        result = _run_mover(*extra, scripts=copy, check=check)
        assert result.returncode != 0, result.stdout
        for message in messages:
            assert message in result.stderr, result.stderr
    assert {p: p.read_bytes() for p in copy.rglob('*.py')} == before
    assert (REPO / 'scripts/split_lanes/u2_frozen.json').read_bytes() == record


@pytest.mark.parametrize('hole', list(HOLES))
def test_completed_state_refuses_against_the_foundation_rerun(tmp_path, hole):
    _foundation()
    relative, edits, reason = HOLES[hole]
    copy = _drifted_copy(tmp_path, relative, edits)
    _assert_refused_without_writes(copy, ('--frozen', 'record'), [
        'refusing: lane output differs from the foundation re-run',
        str((copy / relative).resolve()), 'diagnosis: refusing: ' + reason])


@pytest.mark.parametrize('hole', list(HOLES))
def test_completed_state_diagnostics_refuse_without_history(tmp_path, hole):
    relative, edits, reason = HOLES[hole]
    copy = _drifted_copy(tmp_path, relative, edits)
    _assert_refused_without_writes(copy, ('--no-reference', '--frozen', 'record'), ['refusing: ' + reason])


def test_fresh_state_refuses_a_definition_moved_across_init(tmp_path):
    """The same position guard runs before a fresh move writes anything."""
    foundation = _foundation()
    archive = tmp_path / 'foundation.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), foundation,
                    'skills/token-optimizer/scripts'], cwd=REPO, check=True)
    import tarfile
    with tarfile.open(archive) as stream:
        stream.extractall(tmp_path / 'f', **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    copy = tmp_path / 'f/skills/token-optimizer/scripts'
    source = (copy / 'measure.py').read_text(encoding='utf-8')
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_collect_drain_mode')
    lines = source.splitlines(keepends=True)
    block = ''.join(lines[node.lineno - 1:node.end_lineno])
    assert source.count(block) == 1 and source.count(INIT) == 1
    (copy / 'measure.py').write_bytes(source.replace(block, '').replace(INIT, block + INIT).encode('utf-8'))
    _assert_refused_without_writes(copy, ('--frozen', 'git'), [
        'refusing: binding position differs from frozen BASE (initialization order) near: _collect_drain_mode'])


def _shallow_git(tmp_path, with_origin=True):
    """A depth-1 bare clone of this checkout: no history, like a CI checkout."""
    clone = tmp_path / 'shallow.git'
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(['git', 'init', '-q', '--bare', str(clone)], check=True)
    subprocess.run(['git', '-C', str(clone), 'remote', 'add', 'origin', REPO.as_uri()], check=True)
    subprocess.run(['git', '-C', str(clone), 'fetch', '-q', '--no-tags', '--depth=1', 'origin', head], check=True)
    mover = _mover()
    for pin in (mover.PINNED_BASE['commit'], mover.PINNED_FOUNDATION['commit']):
        assert subprocess.run(['git', '-C', str(clone), 'cat-file', '-e', pin + '^{commit}'],
                              capture_output=True).returncode, 'fixture must start without the pinned commits'
    if not with_origin:
        subprocess.run(['git', '-C', str(clone), 'remote', 'remove', 'origin'], check=True)
    return clone


def test_no_history_fetches_pins_and_refuses_a_changed_constant(tmp_path, monkeypatch):
    """Shallow clone: the pinned BASE/foundation are fetched explicitly, a fresh
    reference is built from them, and a changed constant outside the lane's
    bindings is refused without writes."""
    monkeypatch.setenv('U2_GIT_REPO', str(_shallow_git(tmp_path)))
    relative, edits, reason = HOLES['constant-outside-bindings']
    copy = _drifted_copy(tmp_path, relative, edits)
    before = {p: p.read_bytes() for p in copy.rglob('*.py')}
    result = _run_mover('--frozen', 'record', '--reference', str(tmp_path / 'fresh-reference'), scripts=copy)
    assert result.returncode != 0
    assert 'refusing: lane output differs from the foundation re-run' in result.stderr, result.stderr
    assert str((copy / 'measure.py').resolve()) in result.stderr
    assert 'diagnosis: refusing: ' + reason in result.stderr
    assert (tmp_path / 'fresh-reference/.u2-reference.json').exists()
    assert {p: p.read_bytes() for p in copy.rglob('*.py')} == before


def test_unobtainable_pins_refuse_without_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('U2_GIT_REPO', str(_shallow_git(tmp_path, with_origin=False)))
    copy = _drifted_copy(tmp_path, '_measure/cli.py', [])
    before = {p: p.read_bytes() for p in copy.rglob('*.py')}
    result = _run_mover('--reference', str(tmp_path / 'ref'), scripts=copy)
    assert result.returncode != 0
    assert f"refusing: foundation commit {_mover().PINNED_FOUNDATION['commit']} is not available" in result.stderr
    assert {p: p.read_bytes() for p in copy.rglob('*.py')} == before
    assert not (tmp_path / 'ref').exists()


def test_frozen_inputs_are_pinned_in_the_script_not_the_tree():
    mover = _mover()
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))
    assert (manifest['base'], manifest['source_sha256']) == (
        mover.PINNED_BASE['commit'], mover.PINNED_BASE['measure_sha256'])
    assert mover.resolve_foundation() == mover.PINNED_FOUNDATION['commit']
    with pytest.raises(SystemExit):
        mover.resolve_foundation('0' * 40)


def _module_reference():
    """The module's cached foundation reference (built by the authoritative check)."""
    result = _run_mover()
    # On the aggregate the byte check refuses on the shared facade and the lane's
    # own record; the foundation reference is still built and cached for reuse.
    assert 'refusing: lane output differs from the foundation re-run' in result.stderr, result.stderr
    refs = sorted(Path(os.environ['U2_SCRATCH'], 'reference').glob('*-*'))
    refs = [r for r in refs if (r / '.u2-reference.json').is_file()]
    assert len(refs) == 1, refs
    return refs[0], result


def test_clean_cached_reference_is_a_verified_no_op():
    _, first = _module_reference()
    again = _run_mover()
    assert (again.returncode, again.stdout, again.stderr) == (first.returncode, first.stdout, first.stderr)


def test_unchanged_stamp_cannot_bless_a_damaged_cache_and_tree(tmp_path):
    """Astra U2-final finding 1: the same drift in the candidate and in the
    cached reference, with the stamp untouched, must still refuse."""
    import shutil
    reference, _ = _module_reference()
    cache = tmp_path / 'cached-reference'
    shutil.copytree(reference, cache)
    stamp = (cache / '.u2-reference.json').read_bytes()
    planted = '\nraise RuntimeError("review-only cached reference drift")\n'
    provider = cache / 'skills/token-optimizer/scripts/_measure/sessions.py'
    provider.write_bytes(provider.read_bytes() + planted.encode('utf-8'))
    copy = _drifted_copy(tmp_path, '_measure/sessions.py', [(None, planted)])
    assert (copy / '_measure/sessions.py').read_bytes() == provider.read_bytes()
    before = {p: p.read_bytes() for p in [*copy.rglob('*.py'), *cache.rglob('*')] if p.is_file()}
    for check in (True, False):
        result = _run_mover('--frozen', 'record', '--reference', str(cache), scripts=copy, check=check)
        assert result.returncode != 0, result.stdout
        assert 'refusing: cached reference' in result.stderr and 'is damaged' in result.stderr, result.stderr
        assert 'skills/token-optimizer/scripts/_measure/sessions.py' in result.stderr
        assert 'byte-identical to a fresh re-run' not in result.stdout
    assert (cache / '.u2-reference.json').read_bytes() == stamp
    assert {p: p.read_bytes() for p in [*copy.rglob('*.py'), *cache.rglob('*')] if p.is_file()} == before


def test_output_pins_match_the_committed_lane_outputs():
    mover = _mover()
    import hashlib
    mismatched = [rel for rel, digest in mover.PINNED_OUTPUTS.items()
                  if hashlib.sha256((REPO / rel).read_bytes()).hexdigest() != digest]
    # Only the shared facade and the lane's own record differ on the aggregate:
    # they carry all eight lanes (decisions-U9 Dev-2). Lane-owned outputs keep their pins.
    assert sorted(mismatched) == ['scripts/split_lanes/u2_frozen.json',
                                'skills/token-optimizer/scripts/measure.py'], mismatched
