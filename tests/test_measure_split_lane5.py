"""Lane U5 (audit domain): one owner per body, live facade lookups, shared state.

Behaviour tests run on the unsplit tree too (MEASURE_SPLIT_SOURCE=<BASE measure.py>);
their expected values were captured from BASE, never from this tree. Structure
tests only make sense on the split tree and are skipped for a BASE source.
"""
import ast
import builtins
import importlib.util
import inspect
import json
import os
import symtable
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
PROVIDER = SCRIPTS / '_measure/audit.py'
LANE = '5'
split_tree_only = pytest.mark.skipif(bool(os.environ.get('MEASURE_SPLIT_SOURCE')),
                                     reason='structure of the split tree; BASE has no providers')

_HOST_ENV = ('CLAUDE_CODE_DISABLE_1M_CONTEXT', 'TOKEN_OPTIMIZER_CONTEXT_SIZE', 'CODEX_MODEL', 'OPENAI_MODEL',
             'CLAUDE_MODEL', 'ANTHROPIC_MODEL', 'CLAUDE_CODE_AUTO_COMPACT_WINDOW',
             'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE', 'TOKEN_OPTIMIZER_RUNTIME')


@pytest.fixture
def m(tmp_path, monkeypatch):
    home = tmp_path / 'home é with spaces'
    (home / '.claude').mkdir(parents=True)
    for name in _HOST_ENV:
        monkeypatch.delenv(name, raising=False)
    if os.environ.get('MEASURE_SPLIT_SOURCE'):
        monkeypatch.syspath_prepend(str(SCRIPTS))
        __import__('_measure._compat')
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        yield module


def packet():
    ownership = json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))
    return ownership['lanes'][LANE]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ structure

@split_tree_only
def test_every_packet_symbol_has_one_body_in_audit_and_one_facade_binding():
    lane = packet()
    names = [row['name'] for row in lane['symbols']]
    assert names and lane['modules'] == ['audit']
    facade = ast.parse((SCRIPTS / 'measure.py').read_text(encoding='utf-8'))
    facade_bodies = {n.name for n in facade.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    bindings = {}
    for node in facade.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            bindings.setdefault(node.targets[0].id, []).append(ast.unparse(node.value.args[0]))
    provider_bodies = [n.name for n in ast.parse(PROVIDER.read_text(encoding='utf-8')).body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    other = {}
    for path in (SCRIPTS / '_measure').glob('*.py'):
        if path.stem in ('__init__', '_compat', 'audit'):
            continue
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                other.setdefault(node.name, []).append(path.stem)
    for name in names:
        assert name not in facade_bodies, f'{name}: body still in measure.py'
        assert provider_bodies.count(name) == 1, f'{name}: expected one audit.py body'
        assert bindings.get(name) == [f"_measure_providers['audit'].{name}"], f'{name}: binding {bindings.get(name)}'
        assert name not in other, f'{name}: also defined in {other[name]}'
    assert set(provider_bodies) == set(names), set(provider_bodies) ^ set(names)


@split_tree_only
def test_ownership_guard_and_budget_accept_the_mixed_tree():
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        import check_measure_split
        errors, counts = check_measure_split.check()
    finally:
        sys.path.remove(str(REPO / 'scripts'))
    # Pass 1 of the split lets domain modules exceed 1,000 lines (FILE-SIZE-POLICY.md: the plan
    # ceiling is 9,000, pass 2 splits them). U0's guard does not read .file-size-allow, so this
    # one known error is named here; any other error, or any other module, still fails.
    assert [error for error in errors if error != 'audit.py: exceeds 1000 lines'] == []
    assert counts['facade_lines'] <= counts['facade_ceiling']
    assert len(PROVIDER.read_text(encoding='utf-8').splitlines()) <= 9000


@split_tree_only
def test_audit_provider_is_definition_only_without_domain_state():
    tree = ast.parse(PROVIDER.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        assert (isinstance(node, ast.ImportFrom) and node.module == '__future__'
                or isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
                and [a.name for a in node.names] == ['_compat']), f'line {node.lineno}: module-level work'
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Global)], 'a global statement survived'
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names)]


def unqualified_globals(text):
    """Globals a provider body reads besides builtins and _compat (a facade bypass)."""
    top = symtable.symtable(text, 'audit.py', 'exec')
    allowed = set(dir(builtins)) | {'_compat'}
    found = set()

    def walk(table):
        for child in table.get_children():
            found.update(s.get_name() for s in child.get_symbols()
                         if s.is_global() and s.is_referenced() and s.get_name() not in allowed)
            walk(child)
    walk(top)
    return found


@split_tree_only
def test_no_audit_body_bypasses_the_facade_even_for_a_sibling():
    assert unqualified_globals(PROVIDER.read_text(encoding='utf-8')) == set()
    leaky = ('from . import _compat\n'
             'def sibling(): return 1\n'
             'def caller():\n    _measure_facade = _compat.facade()\n    return sibling()\n')
    assert unqualified_globals(leaky) == {'sibling'}


@split_tree_only
def test_exports_are_the_provider_functions_under_their_original_identity(m):
    for row in packet()['symbols']:
        exported = getattr(m, row['name'])
        assert exported.__module__ == m.__name__, row['name']
        assert exported.__qualname__ == row['name'], row['name']
        assert not hasattr(exported, '__wrapped__'), row['name']
        assert Path(exported.__code__.co_filename) == PROVIDER or row['name'] == '_read_codex_config', row['name']


def test_signatures_and_literal_defaults_match_the_frozen_source(m):
    for row in packet()['symbols']:
        original = ast.parse(row['signature'] + '\n    pass').body[0].args
        actual = inspect.signature(getattr(m, row['name'])).parameters
        expected = [a.arg for a in original.posonlyargs + original.args + original.kwonlyargs]
        expected += [a.arg for a in (original.vararg, original.kwarg) if a]
        assert sorted(actual) == sorted(expected), row['name']
        wanted = list(original.defaults) + [d for d in original.kw_defaults if d]
        have = list((getattr(m, row['name']).__defaults__ or ())) + list((getattr(m, row['name']).__kwdefaults__ or {}).values())
        assert len(have) == len(wanted), row['name']
        for node, value in zip(wanted, have):
            try:
                literal = ast.literal_eval(node)
            except ValueError:
                continue  # name-bearing defaults get their own snapshot test
            assert literal == value and type(literal) is type(value), row['name']


def test_name_bearing_defaults_are_definition_time_snapshots(m, monkeypatch):
    before = m._deterministic_candidates_data.__defaults__
    assert before == (30, 8.0, 60, True, None)
    monkeypatch.setattr(m, '_DETCAND_COACH_BUDGET_S', 99.0)
    monkeypatch.setattr(m, '_DETCAND_COACH_MAX_SESSIONS', 1)
    assert m._deterministic_candidates_data.__defaults__ == before


# ------------------------------------------- behaviour, values captured on BASE

def test_pure_audit_results_equal_the_unsplit_values(m):
    assert [m.score_to_grade(s) for s in (95, 85, 75, 60, 45, 10)] == ['S', 'A', 'B', 'C', 'D', 'F']
    assert [m.score_to_band(s) for s in (85, 65, 45, 5)] == ['Good', 'Fair', 'Needs Work', 'Poor']
    assert [m._degradation_band(f) for f in (0.1, 0.6, 0.75, 0.9)] == [
        ('PEAK ZONE', 'green'), ('DEGRADATION STARTING', 'yellow'), ('QUALITY DROPPING', 'orange'), ('SEVERE', 'red')]
    assert m._interpolate_curve(0.55, [(0.0, 100), (0.5, 90), (1.0, 40)]) == 85
    assert (m._is_1m_model('claude-opus-5-5[1m]'), m._is_1m_model('claude-haiku-4-5')) == (True, False)
    assert m._canonical_compact_model_id('us.anthropic.claude-opus-5-5-20261001[1m]') == 'claude-opus-5-5'
    assert m._canonical_compact_model_id('opus-5-5') == 'claude-opus-5-5'
    assert (m._host_parse_int('200,000'), m._host_parse_int('1e5'),
            m._host_parse_int('abc'), m._host_parse_int('12abc')) == (200000, 100000, None, 12)
    assert (m._parse_compact_window_value('300000'), m._parse_compact_window_value(0),
            m._parse_compact_window_value(True), m._parse_compact_window_value('auto')) == (300000, None, None, None)
    assert (m._clamp_compact_window(5), m._clamp_compact_window(10 ** 9), m._clamp_compact_window(500000)) == (
        100000, 1000000, 500000)
    assert m._model_settings_window('claude-opus-5-5[1m]', {'claude-opus-5-5': {'autoCompactWindow': 250000}}) == (
        250000, 'claude-opus-5-5')
    assert m.sanitize_label('My-Label_1') == 'My-Label_1'
    assert [m._quality_curve_for_model(x)[0] for x in ('gpt-5.5', 'gemini-3', 'claude-x')] == [
        'openai-gpt-5.5', 'google-gemini', 'anthropic-default']
    assert m._estimate_quality_with_curve(0.5, model='claude-opus-5-5') == (88, 'anthropic-default')
    assert m._estimate_messages_until_compact(200000, 10000, avg_msg_tokens=2000, model='claude-sonnet-4-5') == 95


def test_compact_window_resolution_equals_the_unsplit_values(m):
    default = {'tokens': 967000, 'source': 'default (~967K for 1M-native models)', 'user_override': False,
               'model_window': 1000000, 'default_tokens': 967000}
    assert m._resolve_compact_window('claude-opus-5-5', env={}, settings={}) == default
    assert m._resolve_compact_window('claude-opus-5-5', env={'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '300000'},
                                     settings={}) == {**default, 'tokens': 300000, 'user_override': True,
                                                      'source': 'env CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000'}
    assert m._resolve_compact_window('claude-opus-5-5', env={}, settings={'autoCompactWindow': 400000}) == {
        **default, 'tokens': 400000, 'user_override': True, 'source': 'autoCompactWindow=400000'}
    assert m.effective_compact_window('claude-opus-5-5', env={}, settings={}) == (967000, default['source'])


def test_sanitize_label_rejection_still_exits(m, capsys):
    with pytest.raises(SystemExit) as raised:
        m.sanitize_label('x y!')
    assert raised.value.code == 1
    assert 'Snapshot label must contain only letters, numbers, hyphens, underscores.' in capsys.readouterr().out


# ----------------------------------------------- live lookups, patch and undo

def test_same_domain_calls_follow_a_patched_sibling_and_revert(m, monkeypatch):
    curve = Mock(return_value=(42, 'curve-sentinel'))
    window = Mock(return_value={'tokens': 1, 'source': 'window-sentinel'})
    with monkeypatch.context() as patch:
        patch.setattr(m, '_estimate_quality_with_curve', curve)
        assert m._estimate_quality_from_fill(0.5, model='claude-opus-5-5') == 42
        assert curve.call_count == 1
        patch.setattr(m, '_resolve_compact_window', window)
        assert m.effective_compact_window('claude-opus-5-5') == (1, 'window-sentinel')
        assert window.call_count == 1
    assert m._estimate_quality_from_fill(0.5, model='claude-opus-5-5') == 88
    assert m.effective_compact_window('claude-opus-5-5', env={}, settings={})[0] == 967000


def test_codex_config_helpers_read_the_patched_reader(m, monkeypatch):
    reader = Mock(return_value={'model': ' gpt-sentinel ', 'limit': '77', 'bad': float('inf')})
    monkeypatch.setattr(m, '_read_codex_config', reader)
    assert m._codex_config_model() == 'gpt-sentinel'
    assert m._codex_config_int('limit') == 77
    assert m._codex_config_int('bad') is None
    assert reader.call_count == 3


def test_cross_domain_helpers_are_consulted_at_call_time(m, monkeypatch):
    resolver = Mock(side_effect=lambda var: {'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '123456'}.get(var))
    monkeypatch.setattr(m, '_resolve_feature_env', resolver)
    assert m._compact_window_env() == {'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '123456'}
    assert resolver.call_count == 2
    stripper = Mock(return_value='opus-sentinel-20260101')
    monkeypatch.setattr(m, '_strip_provider_prefixes', stripper)
    assert m._canonical_compact_model_id('anything') == 'claude-opus-sentinel'
    assert stripper.call_count == 1


def test_replaced_imports_and_patched_constants_reach_audit_bodies(m, monkeypatch):
    substitute = SimpleNamespace(sub=Mock(return_value='re-sentinel'))
    monkeypatch.setattr(m, 're', substitute)
    assert m._canonical_compact_model_id('anything') == 'claude-re-sentinel'
    assert substitute.sub.call_count == 1
    monkeypatch.undo()
    monkeypatch.setattr(m, '_COMPACT_WINDOW_MIN', 5)
    monkeypatch.setattr(m, '_COMPACT_WINDOW_MAX', 9)
    assert (m._clamp_compact_window(1), m._clamp_compact_window(100)) == (5, 9)


def test_a_replaced_os_module_is_the_one_audit_code_reads(m, monkeypatch):
    fake = SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if not name.startswith('__')})
    fake.environ = {'TOKEN_OPTIMIZER_CONTEXT_SIZE': '123456'}
    monkeypatch.setattr(m, 'os', fake)
    monkeypatch.setattr(m, '_context_window_cache', None)
    assert m.detect_context_window() == (123456, 'env: TOKEN_OPTIMIZER_CONTEXT_SIZE')


# ------------------------------------------------- single shared mutable state

def test_context_window_cache_is_written_and_read_on_the_facade(m, monkeypatch):
    monkeypatch.setattr(m, '_context_window_cache', None)
    first = m.detect_context_window()
    key, value = m._context_window_cache
    assert value == first and key[0] == m.detect_runtime()
    sentinel = (777, 'cache-sentinel')
    m._context_window_cache = (key, sentinel)
    assert m.detect_context_window() is sentinel
    monkeypatch.setattr(m, '_cli_context_size', 150000)
    assert m.detect_context_window() == (150000, 'cli: --context-size')
    assert m._context_window_cache[1] == (150000, 'cli: --context-size')
    if not os.environ.get('MEASURE_SPLIT_SOURCE'):
        provider = m._measure_providers.load('audit')  # deferred: loads on first lookup
        assert not [n for n in vars(provider) if 'cache' in n.lower() and not n.startswith('__')], 'the provider holds no cache'


def test_codex_config_cache_stays_on_the_facade_for_audit_readers(m, monkeypatch, tmp_path):
    (tmp_path / 'config.toml').write_text('model = "from-disk"\nlimit = 9\n', encoding='utf-8')
    monkeypatch.setattr(m, 'RUNTIME_DIR', tmp_path)
    monkeypatch.setattr(m, '_codex_config_cache', None)
    assert m._codex_config_model() == 'from-disk'
    assert m._codex_config_int('limit') == 9
    cached = m._codex_config_cache
    assert cached is not None and cached[1]['model'] == 'from-disk'
    monkeypatch.setattr(m, '_codex_config_cache', (cached[0], {'model': 'facade-sentinel'}))
    assert m._codex_config_model() == 'facade-sentinel'


def test_fixture_a_b_a_keeps_each_facades_audit_functions_and_cache(tmp_path, monkeypatch):
    for name in _HOST_ENV:
        monkeypatch.delenv(name, raising=False)

    def env(label):
        home = tmp_path / label
        (home / '.claude').mkdir(parents=True)
        return {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
                'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}
    if os.environ.get('MEASURE_SPLIT_SOURCE'):
        monkeypatch.syspath_prepend(str(SCRIPTS))
        __import__('_measure._compat')
    with isolated_measure(name='measure_u5_a', env=env('A')) as a:
        function, defaults = a.detect_context_window, a._deterministic_candidates_data.__defaults__
        a._cli_context_size = 111111
        assert a.detect_context_window() == (111111, 'cli: --context-size')
        with isolated_measure(name='measure_u5_b', env=env('B')) as b:
            assert sys.modules['measure'] is b
            assert b._context_window_cache is None
            assert b.detect_context_window is not function
            assert b.detect_context_window()[0] != 111111
            assert a._context_window_cache[1] == (111111, 'cli: --context-size')
        assert sys.modules['measure'] is a
        assert a.detect_context_window is function
        assert a._deterministic_candidates_data.__defaults__ is defaults
        assert function() == (111111, 'cli: --context-size')


# ------------------------------- local bindings are never rewritten (Astra U0 #2)

def facade_shadow_conflicts(text):
    """(function, name, line) where a locally bound name is read as a facade attribute.

    except-as targets, for/with/walrus/import/def targets and parameters bind
    locals. The mover must leave them alone; rewriting one to `_measure_facade.x`
    raises AttributeError exactly on the error path that BASE handled.
    """
    conflicts = []

    def own_nodes(function):
        stack = list(function.body) if not isinstance(function, ast.Lambda) else [function.body]
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.extend(ast.iter_child_nodes(node))

    def bound(function):
        arguments = function.args
        names = {a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs}
        names.update(a.arg for a in (arguments.vararg, arguments.kwarg) if a)
        for node in own_nodes(function):
            if isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name.split('.')[0]) for a in node.names)
        names.discard('_measure_facade')
        return names

    def visit(function, label, inherited):
        names = inherited | bound(function)
        for node in own_nodes(function):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == '_measure_facade' and node.attr in names):
                conflicts.append((label, node.attr, node.lineno))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(node, label + '.' + getattr(node, 'name', '<lambda>'), names)

    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(node, node.name, set())
    return conflicts


@split_tree_only
def test_exception_and_other_local_bindings_are_never_facade_attributes():
    assert facade_shadow_conflicts(PROVIDER.read_text(encoding='utf-8')) == []
    broken = ('from . import _compat\n'
              'def work():\n'
              '    _measure_facade = _compat.facade()\n'
              '    try:\n        raise ValueError("expected error")\n'
              '    except ValueError as exc:\n        return str(_measure_facade.exc)\n')
    assert facade_shadow_conflicts(broken) == [('work', 'exc', 7)]
    looped = ('from . import _compat\n'
              'def work():\n'
              '    _measure_facade = _compat.facade()\n'
              '    for item in (1,):\n        pass\n'
              '    return _measure_facade.item\n')
    assert facade_shadow_conflicts(looped) == [('work', 'item', 6)]
    legitimate = ('from . import _compat\n'
                  'def work(a):\n'
                  '    _measure_facade = _compat.facade()\n'
                  '    return _measure_facade.os.path.join(a)\n')
    assert facade_shadow_conflicts(legitimate) == []


@split_tree_only
def test_every_except_target_in_the_audit_provider_stays_a_local_name():
    tree = ast.parse(PROVIDER.read_text(encoding='utf-8'))
    handlers = [h for fn in tree.body if isinstance(fn, ast.FunctionDef)
                for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler) and h.name]
    assert handlers, 'the audit domain has except-as branches to protect'
    for handler in handlers:
        facade_uses = [n for stmt in handler.body for n in ast.walk(stmt) if isinstance(n, ast.Attribute)
                       and isinstance(n.value, ast.Name) and n.value.id == '_measure_facade' and n.attr == handler.name]
        assert not facade_uses, f'line {handler.lineno}: {handler.name} read from the facade'


def test_unreadable_mcp_configs_record_the_exception_class_like_base(m, tmp_path):
    malformed = tmp_path / 'malformed.json'
    malformed.write_text('{not json', encoding='utf-8')
    undecodable = tmp_path / 'undecodable.json'
    undecodable.write_bytes(b'\xff\xfe\x00')
    missing = tmp_path / 'missing.json'
    for path, reason in ((malformed, 'JSONDecodeError'), (undecodable, 'UnicodeDecodeError'),
                         (tmp_path, 'NotRegularFile')):
        skipped = []
        assert m._mcp_read_json(path, skipped) is None
        assert skipped == [{'path': str(path), 'reason': reason}]
    skipped = []
    assert m._mcp_read_json(missing, skipped) is None and skipped == []
    good = tmp_path / 'good.json'
    good.write_text('{"mcpServers": {"a": {}}}', encoding='utf-8')
    assert m._mcp_read_json(good, []) == {'mcpServers': {'a': {}}}


def test_compare_snapshots_error_branch_prints_the_caught_exception_like_base(m, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', tmp_path)
    (tmp_path / 'snapshot_before.json').write_text('not json', encoding='utf-8')
    (tmp_path / 'snapshot_after.json').write_text('{}', encoding="utf-8")
    assert m.compare_snapshots() is None
    lines = capsys.readouterr().out.splitlines()
    assert "[Error] Cannot read 'before' snapshot: Expecting value: line 1 column 1 (char 0)" in lines
    assert any(line.strip().startswith('Re-run:') and line.rstrip().endswith('snapshot before') for line in lines)
    (tmp_path / 'snapshot_before.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'snapshot_after.json').write_text('[1,', encoding='utf-8')
    assert m.compare_snapshots() is None
    assert any(line.startswith("[Error] Cannot read 'after' snapshot: Expecting value")
               or line.startswith("[Error] Cannot read 'after' snapshot: ")
               for line in capsys.readouterr().out.splitlines())


# ------------------------------------------------------- mutation fragments

def guards():
    return _load('_u5_check_regression_guards', REPO / 'scripts/check_regression_guards.py')


@split_tree_only
def test_lane5_mutation_fragments_plant_inside_the_audit_provider():
    module = guards()
    fragments = json.loads((REPO / packet()['mutation_fragment']).read_text(encoding='utf-8'))
    assert fragments and all(f.get('symbol') for f in fragments)
    provider = PROVIDER.read_text(encoding='utf-8')
    for fragment in fragments:
        changed = module.resolve_targets(REPO, module.MEASURE, [fragment])
        assert list(changed) == ['skills/token-optimizer/scripts/_measure/audit.py'], fragment['mutation']
        original, mutated = changed['skills/token-optimizer/scripts/_measure/audit.py']
        assert original == provider and mutated != original
        compile(mutated, 'audit.py', 'exec')
        assert unqualified_globals(mutated) == set(), fragment['mutation']


@split_tree_only
def test_a_stale_unqualified_fragment_is_refused_not_skipped():
    module = guards()
    stale = {'old': '    if detect_runtime() == "codex":\n        # Codex: the Claude table',
             'new': '    if False:\n        # Codex: the Claude table', 'symbol': '_resolve_compact_window'}
    with pytest.raises(module.MutationError):
        module.resolve_targets(REPO, module.MEASURE, [stale])


@split_tree_only
def test_partitioned_mutation_catalog_still_collects_with_lane5_fragments():
    module = guards()
    names = {entry[0] for entry in module.collect()}
    fragments = json.loads((REPO / packet()['mutation_fragment']).read_text(encoding='utf-8'))
    assert {f['mutation'] for f in fragments} <= names
