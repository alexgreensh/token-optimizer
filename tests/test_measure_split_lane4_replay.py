"""Lane U4 (cache_policy): the move script, its pinned frozen inputs and every refusal.

Runs scripts/split_lanes/u4_move.py on disposable copies of the tree and proves it
moves exactly the lane 4 packet, is a no-op on a finished tree, and refuses any
tree that is not the rebuild of the pinned frozen inputs. Structural tests are
skipped when MEASURE_SPLIT_SOURCE points at an unsplit BASE.
"""
import ast
import hashlib
import importlib.util
import inspect
import json
import os
import subprocess

import pytest
from helpers._lane4_helpers import (DEFINITION, REPO, SCRIPTS, SYMBOLS, _files, _load_script, _read, _run,
                                    _write, copy_root, structural)


# ---------------------------------------------------------------- the move script

@structural
def test_script_is_a_no_op_on_a_finished_tree(copy_root, capsys):
    root = copy_root()
    before = [path.read_bytes() for path in _files(root)]
    assert _run(root) == 0
    assert _run(root, check=True) == 0
    assert [path.read_bytes() for path in _files(root)] == before
    assert capsys.readouterr().out.count('already applied, 181/181') == 2


def _break_binding(root):
    facade, _ = _files(root)
    lines = _read(facade).splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.startswith('_keepwarm_sidecar_path = _measure_compat.bind('))
    del lines[index]
    _write(facade, ''.join(lines))


def _duplicate_body(root):
    _, provider = _files(root)
    text = _read(provider)
    _write(provider, text + '\n\ndef _keepwarm_sidecar_path():\n    pass\n')


def _unowned_body(root):
    _, provider = _files(root)
    _write(provider, _read(provider) + '\n\ndef _stray():\n    pass\n')


def _module_state(root):
    _, provider = _files(root)
    _write(provider, _read(provider) + '\n\n_CACHE = {}\n')


def _unowned_binding(root):
    facade, _ = _files(root)
    _write(facade, _read(facade) + "\n_other = _measure_compat.bind(_measure_providers['cache_policy']._other, __name__)\n")


def _body_back_in_facade(root):
    facade, _ = _files(root)
    _write(facade, _read(facade) + '\n\ndef _keepwarm_sidecar_path():\n    return 1\n')


@structural
@pytest.mark.parametrize('damage, message', [
    (_break_binding, 'half-moved'), (_duplicate_body, 'duplicate'), (_unowned_body, 'not owned'),
    (_module_state, 'module-level state'), (_unowned_binding, 'not in the lane 4 packet'),
    (_body_back_in_facade, 'half-moved')])
def test_script_refuses_any_inconsistent_tree_and_writes_nothing(copy_root, damage, message):
    root = copy_root()
    damage(root)
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match=message):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


def test_script_refuses_a_missing_symbol(copy_root):
    root = copy_root()
    _, provider = _files(root)
    _break_binding(root)
    tree = ast.parse(_read(provider))
    node = next(n for n in tree.body if isinstance(n, DEFINITION) and n.name == '_keepwarm_sidecar_path')
    lines = _read(provider).splitlines(keepends=True)
    del lines[node.lineno - 1:node.end_lineno]
    _write(provider, ''.join(lines))
    with pytest.raises(_load_script().Refusal, match='missing from measure.py'):
        _run(root)


VERBATIM = {
    '_keepwarm_sidecar_path': '''\
def _keepwarm_sidecar_path():
    return SNAPSHOT_DIR / _KEEPWARM_SIDECAR_NAME
''',

    '_keepwarm_read_tail': '''\
def _keepwarm_read_tail(filepath, max_lines=_KEEPWARM_TAIL_LINES):
    """Return up to the last `max_lines` raw lines of a file, newest-last.

    Bounded-memory deque scan so a huge transcript costs only one pass and a fixed
    window. Returns [] on any read error (the arm-writer fails open to "write
    nothing"). Pure read; no side effects.
    """
    from collections import deque
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=max_lines))
    except (OSError, ValueError):
        return []
''',

    '_keepwarm_tripwire_off': '''\
def _keepwarm_tripwire_off():
    """True iff the keep-warm tripwire has demoted the feature to 'off'.

    Thin wrapper over keepwarm_tripwire_mode so the gate stays readable. A
    missing sidecar (no breach yet) is NOT off; a corrupt sidecar reads as off
    (fail-closed, inside keepwarm_tripwire_mode). Defined below the gate but
    resolved at call time. Never raises.
    """
    try:
        return keepwarm_tripwire_mode() == "off"
    except Exception:
        return True  # fail-closed: if we cannot read the verdict, refuse
''',

    '_keepwarm_tripwire_lock': '''\
@contextmanager
def _keepwarm_tripwire_lock():
    """Bounded portable lease serializing tripwire recompute+write.

    Contenders skip the mutation after 75ms rather than blocking a hook.
    """
    lock_path = SNAPSHOT_DIR / f"{_KEEPWARM_TRIPWIRE_LOCK_NAME}.lease"
    with lease_lock(
        lock_path,
        deadline=current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired
''',
}


def _replace_once(path, old, new):
    text = _read(path)
    assert old in text, old
    _write(path, text.replace(old, new, 1))


BODY_DRIFT = [
    ('constant changed in a body', 'acquire_timeout=0.075', 'acquire_timeout=0.5', '_keepwarm_lock'),
    ('facade lookup bypassed', 'if _measure_facade._valid_keepwarm_record(rec):', 'if _valid_keepwarm_record(rec):',
     'load_keepwarm_records'),
    ('trailing comment dropped', 'return True  # fail-closed: if we cannot read the verdict, refuse', 'return True',
     '_keepwarm_tripwire_off'),
    ('parameter added', 'def _keepwarm_sidecar_path():', 'def _keepwarm_sidecar_path(extra=None):',
     '_keepwarm_sidecar_path'),
    ('placeholder default changed', 'def _keepwarm_read_tail(filepath, max_lines=None):',
     'def _keepwarm_read_tail(filepath, max_lines=5):', '_keepwarm_read_tail'),
]

DEFAULT_DRIFT = [
    ('default expression changed', 'defaults=(_KEEPWARM_TAIL_LINES,)', 'defaults=(_KEEPWARM_TAIL_LINES + 1,)',
     '_keepwarm_read_tail'),
    ('literal default changed', "._cache_ttl_waste_cached, __name__, defaults=(30, False,)",
     "._cache_ttl_waste_cached, __name__, defaults=(31, False,)", '_cache_ttl_waste_cached'),
    ('default removed', ', defaults=(_KEEPWARM_SCHEDULER_LOG_MAX_BYTES,)', '', '_keepwarm_rotate_log'),
    ('decorator removed', "._keepwarm_lock, __name__, decorators=(contextmanager,))", "._keepwarm_lock, __name__)",
     '_keepwarm_lock'),
]


@structural
@pytest.mark.parametrize('label, old, new, symbol', BODY_DRIFT, ids=[row[0] for row in BODY_DRIFT])
def test_completed_body_drift_is_refused_and_nothing_is_written(copy_root, label, old, new, symbol):
    root = copy_root()
    _replace_once(_files(root)[1], old, new)
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match=symbol):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
@pytest.mark.parametrize('label, old, new, symbol', DEFAULT_DRIFT, ids=[row[0] for row in DEFAULT_DRIFT])
def test_completed_default_or_decorator_drift_is_refused_and_nothing_is_written(copy_root, label, old, new, symbol):
    root = copy_root()
    _replace_once(_files(root)[0], old, new)
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match=symbol):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
def test_check_mode_also_verifies_completed_symbols(copy_root):
    """--check must not report success for a tree whose finished bodies or bindings drifted."""
    body, binding = copy_root('body'), copy_root('binding')
    _replace_once(_files(body)[1], 'acquire_timeout=0.075', 'acquire_timeout=0.5')
    _replace_once(_files(binding)[0], "._cache_ttl_waste_cached, __name__, defaults=(30, False,)",
                  "._cache_ttl_waste_cached, __name__, defaults=(31, False,)")
    with pytest.raises(_load_script().Refusal, match='_keepwarm_lock'):
        _run(body, check=True)
    with pytest.raises(_load_script().Refusal, match='_cache_ttl_waste_cached'):
        _run(binding, check=True)


@structural
def test_every_completed_body_reverses_to_its_frozen_hash():
    """The reversal is independent of the extractor: it only strips the allowed qualification."""
    script = _load_script()
    text = _read(SCRIPTS / '_measure/cache_policy.py')
    lines = text.splitlines()
    for node in ast.parse(text).body:
        if isinstance(node, DEFINITION):
            body = '\n'.join(lines[node.lineno - 1:node.end_lineno])
            original = script.restore_original(body, SYMBOLS[node.name])
            assert hashlib.sha256(original.encode()).hexdigest() == SYMBOLS[node.name]['sha256'], node.name


def _top_level_block(path, name):
    """(start, end) 0-based line slice of a top-level definition, decorators included."""
    text = _read(path)
    node = next(n for n in ast.parse(text).body if isinstance(n, DEFINITION) and n.name == name)
    return min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1, node.end_lineno


def _hoist(path, lines_to_move, anchor_prefix):
    """Move `lines_to_move` (a list of lines) to just above the first line that starts with anchor_prefix."""
    lines = _read(path).splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.startswith(anchor_prefix))
    lines[index:index] = lines_to_move
    _write(path, ''.join(lines))


@structural
def test_completed_binding_moved_across_its_default_initialisation_is_refused(copy_root):
    """The lane's own bindings keep their order; only the constant they capture is jumped over."""
    root = copy_root()
    facade, _ = _files(root)
    lines = _read(facade).splitlines(keepends=True)
    binding = next(i for i, line in enumerate(lines) if line.startswith('_cap_append_log = _measure_compat.bind('))
    constant = next(i for i, line in enumerate(lines) if line.startswith('_APPEND_LOG_MAX_BYTES = '))
    assert constant < binding
    assert not any(line.split(' = ')[0] in SYMBOLS for line in lines[constant + 1:binding]), 'lane order must stay intact'
    moved = lines.pop(binding)
    lines.insert(constant, moved)
    _write(facade, ''.join(lines))
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match=r'position: .*_APPEND_LOG_MAX_BYTES.*_cap_append_log'):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
def test_pending_definition_moved_across_its_default_initialisation_is_refused(copy_root):
    root = copy_root()
    _make_pending(root, ['_keepwarm_read_tail'])
    facade, _ = _files(root)
    start, end = _top_level_block(facade, '_keepwarm_read_tail')
    lines = _read(facade).splitlines(keepends=True)
    block = lines[start:end]
    del lines[start:end]
    _write(facade, ''.join(lines))
    _hoist(facade, block, '_KEEPWARM_TAIL_LINES = ')
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='position'):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
@pytest.mark.parametrize('check', [False, True])
def test_executable_statement_appended_to_the_provider_is_refused_in_both_modes(copy_root, check):
    root = copy_root()
    _, provider = _files(root)
    _write(provider, _read(provider) + '\n\nraise RuntimeError("review-only import failure")\n')
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='module-level state'):
        _run(root, check=check)
    assert [path.read_bytes() for path in _files(root)] == before


def _swap_first_two_provider_definitions(root):
    _, provider = _files(root)
    text = _read(provider)
    nodes = [n for n in ast.parse(text).body if isinstance(n, DEFINITION)][:2]
    lines = text.splitlines(keepends=True)
    first = lines[nodes[0].lineno - 1:nodes[0].end_lineno]
    second = lines[nodes[1].lineno - 1:nodes[1].end_lineno]
    between = lines[nodes[0].end_lineno:nodes[1].lineno - 1]
    lines[nodes[0].lineno - 1:nodes[1].end_lineno] = second + between + first
    _write(provider, ''.join(lines))


def _comment_in_provider(root):
    _, provider = _files(root)
    _write(provider, _read(provider) + '\n# a comment no statement check would ever see\n')


@structural
@pytest.mark.parametrize('damage, path', [(_swap_first_two_provider_definitions, 'cache_policy.py'),
                                          (_comment_in_provider, 'cache_policy.py')],
                         ids=['provider definition order', 'provider comment'])
def test_the_rebuild_catches_changes_no_finer_check_looks_for(copy_root, damage, path):
    root = copy_root()
    damage(root)
    before = [p.read_bytes() for p in _files(root)]
    with pytest.raises(_load_script().Refusal, match=f'{path}: differs from the rebuild of the frozen inputs'):
        _run(root)
    assert [p.read_bytes() for p in _files(root)] == before


@structural
def test_the_real_tree_is_the_rebuild_of_the_pinned_frozen_inputs(tmp_path):
    """In CI (shallow, no history) the pinned commit is fetched by sha; locally it is read from the object store."""
    script = _load_script()
    import io
    out = io.StringIO()
    assert script.run(REPO, check=True, out=out, scratch=tmp_path / 'u4-scratch') == 0
    assert f'byte-identical to the rebuild of the frozen inputs (pinned {script.PIN["input"]["commit"][:8]}' in out.getvalue()


@structural
def test_pin_is_consistent_with_the_manifest_and_the_tooling_it_covers():
    script = _load_script()
    pin = script.PIN
    manifest = json.loads(_read(REPO / 'scripts/measure_split_manifest.json'))
    assert pin['base'] == {'commit': manifest['base'], 'path': script.FACADE_REL, 'sha256': manifest['source_sha256']}
    for commit in (pin['base']['commit'], pin['input']['commit']):
        assert len(commit) == 40 and set(commit) <= set('0123456789abcdef')
    files = pin['input']['files']
    for relative in script.TOOL_FILES:
        assert files[relative] == hashlib.sha256((REPO / relative).read_bytes()).hexdigest(), relative
    providers = {f'{script.PROVIDER_DIR}{path.name}' for path in (SCRIPTS / '_measure').glob('*.py')}
    assert providers <= set(files)
    assert set(files) == providers | {script.FACADE_REL, script.FRAGMENT, *script.TOOL_FILES}
    assert hashlib.sha256((REPO / script.FRAGMENT).read_bytes()).hexdigest() == files[script.FRAGMENT]


def _git(*args, cwd=None):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}).stdout


@structural
def test_the_pinned_inputs_are_fetched_by_sha_into_a_checkout_that_lacks_them(tmp_path):
    """The shallow-CI path: depth-1 clone, pinned commit absent, `git fetch --depth=1 origin <sha>` supplies it."""
    script = _load_script()
    commit = script.PIN['input']['commit']
    try:
        _git('cat-file', '-e', f'{commit}^{{commit}}', cwd=REPO)
        origin = REPO.as_uri()
    except subprocess.CalledProcessError:
        origin = _git('remote', 'get-url', 'origin', cwd=REPO).strip()
    checkout = tmp_path / 'shallow-checkout'
    _git('clone', '-q', '--depth=1', '--no-checkout', origin, str(checkout))
    with pytest.raises(subprocess.CalledProcessError):
        _git('cat-file', '-e', f'{commit}^{{commit}}', cwd=checkout)
    files = script.obtain_frozen(script.PIN, checkout)
    assert set(files) == set(script.PIN['input']['files'])
    for path, digest in script.PIN['input']['files'].items():
        assert hashlib.sha256(files[path].encode('utf-8')).hexdigest() == digest


def _bad_pin(path=None, digest=None, commit=None):
    pin = json.loads(json.dumps(_load_script().PIN))
    if path is not None:
        pin['input']['files'][path] = digest
    if commit is not None:
        pin['input']['commit'] = commit
    return pin


@structural
@pytest.mark.parametrize('state', ['finished', 'pending'])
def test_unobtainable_frozen_inputs_refuse_and_write_nothing(copy_root, tmp_path, state):
    root = copy_root()
    if state == 'pending':
        _make_pending(root, list(VERBATIM))
    empty = tmp_path / 'empty-objects'
    _git('init', '-q', str(empty))
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='cannot obtain the pinned frozen inputs'):
        _load_script().run(root, objects=empty)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
def test_a_pinned_sha256_that_does_not_match_the_object_refuses_and_writes_nothing(copy_root):
    script = _load_script()
    root = copy_root()
    _make_pending(root, list(VERBATIM))
    before = [path.read_bytes() for path in _files(root)]
    wrong = hashlib.sha256(b'not measure.py').hexdigest()
    with pytest.raises(script.Refusal, match=f'{script.FACADE_REL}: sha256 .* is not the pinned {wrong[:16]}'):
        script.run(root, objects=REPO, pin=_bad_pin(script.FACADE_REL, wrong))
    with pytest.raises(script.Refusal, match='scripts/not_in_the_pinned_commit.py: not in '):
        script.run(root, objects=REPO, pin=_bad_pin('scripts/not_in_the_pinned_commit.py', wrong))
    assert [path.read_bytes() for path in _files(root)] == before


@structural
def test_no_flag_or_argument_can_bypass_the_pin_or_the_packet_hash():
    script = _load_script()
    assert not {'allow_hash_drift', 'frozen'} & set(inspect.signature(script.run).parameters)
    for flag in ('--allow-hash-drift', '--frozen=git'):
        with pytest.raises(SystemExit):
            script.main(['--check', flag])


@structural
@pytest.mark.parametrize('state, check', [('finished', False), ('finished', True), ('pending', False)])
def test_a_changed_constant_outside_the_lane_bindings_is_refused_without_any_history(copy_root, state, check):
    """The frozen inputs are pinned, not read back from the tree: a $0.10 budget turned into $999 must not be blessed."""
    root = copy_root()
    if state == 'pending':
        _make_pending(root, list(VERBATIM))
    facade, _ = _files(root)
    _replace_once(facade, '_KEEPWARM_PING_BUDGET_USD = 0.10\n', '_KEEPWARM_PING_BUDGET_USD = 999.0\n')
    assert not (root / '.git').exists()
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='measure.py: differs from the rebuild of the frozen inputs'):
        _run(root, check=check)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
def test_pruned_tree_extraction_is_identical_to_the_full_tree_call(copy_root):
    import symtable
    script = _load_script()
    names = list(VERBATIM)
    root = copy_root()
    _make_pending(root, names)
    facade, provider = _files(root)
    source = _read(facade)
    tool, packet = script.load_tool(root), script.lane_packet(root)
    tree, table = ast.parse(source), symtable.symtable(source, 'measure.py', 'exec')
    pending, _, problems, definitions = script.classify(packet, tree, ast.parse(_read(provider)), tool)
    assert not problems and sorted(pending) == sorted(names)
    provider_text, facade_text, _ = script.build(root, packet, definitions, pending, source,
                                                 _read(provider), tool)
    for name in names:
        moved, binding = tool.extract_definition(source, name, 'cache_policy', tree=tree, table=table)
        assert moved.splitlines()[0] in provider_text and moved.rstrip('\n').splitlines()[-1] in provider_text
        assert binding + '\n' in facade_text


def _make_pending(root, names):
    """Put verbatim original definitions back where the bindings are; drop the provider copies."""
    facade, provider = _files(root)
    lines = _read(facade).splitlines(keepends=True)
    for name in names:
        index = next(i for i, line in enumerate(lines) if line.startswith(f'{name} = _measure_compat.bind('))
        lines[index] = VERBATIM[name]
    _write(facade, ''.join(lines))
    text = _read(provider)
    plines = text.splitlines()
    nodes = [n for n in ast.parse(text).body if isinstance(n, DEFINITION)]
    prefix = '\n'.join(plines[:nodes[0].lineno - 1]).rstrip()
    kept = ['\n'.join(plines[n.lineno - 1:n.end_lineno]) for n in nodes if n.name not in names]
    _write(provider, prefix + ''.join('\n\n\n' + body for body in kept) + '\n')


def _segments(path):
    """Whole-line source of each definition (get_source_segment would hide a trailing comment)."""
    text = _read(path)
    lines = text.splitlines()
    return {n.name: '\n'.join(lines[min([n.lineno] + [d.lineno for d in n.decorator_list]) - 1:n.end_lineno])
            for n in ast.parse(text).body if isinstance(n, DEFINITION)}


@structural
def test_verbatim_originals_are_exactly_the_frozen_source():
    for name, text in VERBATIM.items():
        assert hashlib.sha256(text.rstrip('\n').encode()).hexdigest() == SYMBOLS[name]['sha256'], name


@structural
def test_script_rebuilds_the_committed_result_from_the_verbatim_originals(copy_root, capsys):
    names = list(VERBATIM)
    root = copy_root()
    _make_pending(root, names)
    facade, provider = _files(root)
    assert _run(root, check=True) == 1
    assert '4/181 pending, 177 done, no hash drift' in capsys.readouterr().out
    assert _run(root) == 0
    assert 'moved 4 definitions' in capsys.readouterr().out
    assert facade.read_bytes() == (SCRIPTS / 'measure.py').read_bytes()
    committed, rebuilt = _segments(SCRIPTS / '_measure/cache_policy.py'), _segments(provider)
    assert rebuilt == committed
    assert '# fail-closed: if we cannot read the verdict, refuse' in rebuilt['_keepwarm_tripwire_off']
    assert _run(root) == 0
    assert 'already applied' in capsys.readouterr().out


@structural
def test_script_matches_the_stock_mover_except_for_the_preserved_trailing_comment(copy_root):
    names = list(VERBATIM)
    mine, stock = copy_root('mine'), copy_root('stock')
    _make_pending(mine, names)
    _make_pending(stock, names)
    _run(mine)
    spec = importlib.util.spec_from_file_location('_u4_stock_mover', stock / 'scripts/measure_split.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.move(stock / 'skills/token-optimizer/scripts', names)
    assert _files(mine)[0].read_bytes() == _files(stock)[0].read_bytes()
    mine_bodies, stock_bodies = _segments(_files(mine)[1]), _segments(_files(stock)[1])
    assert {n: t for n, t in mine_bodies.items() if n != '_keepwarm_tripwire_off'} == \
           {n: t for n, t in stock_bodies.items() if n != '_keepwarm_tripwire_off'}
    assert mine_bodies['_keepwarm_tripwire_off'].replace('  # fail-closed: if we cannot read the verdict, refuse', '') \
        == stock_bodies['_keepwarm_tripwire_off']


@structural
def test_the_script_writes_lf_only_and_refuses_a_crlf_tree(copy_root):
    """The repo requires LF and the replay compares raw bytes: never the platform's text-mode newline."""
    root = copy_root()
    _make_pending(root, list(VERBATIM))
    _run(root)
    assert all(b'\r' not in path.read_bytes() for path in _files(root))
    facade, provider = _files(root)
    facade.write_bytes(facade.read_bytes().replace(b'\n', b'\r\n'))
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='CRLF'):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


@structural
def test_script_refuses_hash_drift(copy_root):
    root = copy_root()
    _make_pending(root, ['_keepwarm_sidecar_path'])
    facade, _ = _files(root)
    _write(facade, _read(facade).replace(
        'return SNAPSHOT_DIR / _KEEPWARM_SIDECAR_NAME', 'return SNAPSHOT_DIR / _KEEPWARM_SIDECAR_NAME  # drifted'))
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='drifted from the lane packet hash'):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before
