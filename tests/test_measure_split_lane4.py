"""Lane U4 (cache_policy): the moved keep-warm, TTL, subagent-cache and recommendation code.

Structural tests describe the split tree and are skipped when MEASURE_SPLIT_SOURCE
points at an unsplit BASE. Everything else is behavioural and must pass on both
trees: a patched helper, a replaced imported module, a rebound path or a captured
default has to affect the moved code exactly as it affected the original.

The move script and its refusals are in test_measure_split_lane4_replay.py.
"""
import ast
import importlib.util
import inspect
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from helpers._lane4_helpers import (DEFINITION, PACKET, REPO, SCRIPTS, SYMBOLS, _files, _load_script, _read, _run,
                                    _write, copy_root, structural)
from helpers.measure_split import isolated_measure


@pytest.fixture
def m(tmp_path, monkeypatch):
    home = tmp_path / 'home é with spaces'
    home.mkdir()
    (home / '.claude').mkdir()
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        yield module


def _provider_tree():
    return ast.parse(_read(SCRIPTS / '_measure/cache_policy.py'))


def _facade_tree():
    return ast.parse(_read(SCRIPTS / 'measure.py'))


# ---------------------------------------------------------------- ownership and shape

def test_lane_packet_is_the_expected_size():
    assert PACKET['modules'] == ['cache_policy']
    assert len(SYMBOLS) == 181 == len(PACKET['symbols'])
    assert PACKET['inherited_canaries'] == []


@structural
def test_each_symbol_has_exactly_one_body_and_one_binding():
    bodies = [n.name for n in _provider_tree().body if isinstance(n, DEFINITION)]
    assert sorted(bodies) == sorted(SYMBOLS)
    facade = _facade_tree()
    assert [n.name for n in facade.body if isinstance(n, DEFINITION) and n.name in SYMBOLS] == []
    bound = {}
    for node in facade.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == '_measure_compat.bind'):
            target = node.value.args[0]
            if ast.unparse(target.value) == "_measure_providers['cache_policy']":
                bound.setdefault(node.targets[0].id, []).append(target.attr)
    assert {name: [name] for name in SYMBOLS} == bound


@structural
def test_provider_is_definition_only_and_within_budget():
    path = SCRIPTS / '_measure/cache_policy.py'
    for node in _provider_tree().body:
        if isinstance(node, DEFINITION):
            continue
        if isinstance(node, ast.ImportFrom) and (node.module == '__future__' or (
                node.level == 1 and node.module is None and [a.name for a in node.names] == ['_compat'])):
            continue
        assert isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant), f'state at line {node.lineno}'
    assert len(_read(path).splitlines()) <= 9000


@structural
def test_imported_provider_carries_no_domain_global_state(m):
    provider = sys.modules['_measure.cache_policy']
    own = {name for name, value in vars(provider).items() if not name.startswith('__')}
    assert own == set(SYMBOLS) | {'annotations', '_compat'}
    # The two explicit cache writes of this domain live on the facade only.
    for name in ('_cache_health_memo', '_SUBAGENT_CACHE_CC_VERSION'):
        assert hasattr(m, name) and not hasattr(provider, name)


@structural
def test_ownership_and_facade_budget_guard_pass_for_this_tree(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / 'scripts'))
    spec = importlib.util.spec_from_file_location('_lane4_check_measure_split', REPO / 'scripts/check_measure_split.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    errors, counts = guard.check(REPO)
    # The plan's cache_policy module is far past the 1,000-line provider limit U0-FINAL enforces. The orchestrator
    # owns that conflict (U0-FINAL-REPORT, integration item 3); tolerate exactly this one named error, nothing else.
    assert [e for e in errors if e != 'cache_policy.py: exceeds 1000 lines'] == []
    assert counts['moved'] >= 5 + len(SYMBOLS)
    assert counts['facade_lines'] <= counts['facade_ceiling']


def test_every_lane_export_stays_reachable(m):
    for name in SYMBOLS:
        assert callable(getattr(m, name)), name
    assert sys.modules['measure'] is m


@structural
def test_exports_resolve_to_the_provider_body_and_the_facade_identity(m):
    for name in SYMBOLS:
        function = getattr(m, name)
        assert function.__module__ == m.__name__, name
        assert function.__name__ == name and function.__qualname__ == name, name
        assert Path(inspect.unwrap(function).__code__.co_filename).parts[-2:] == ('_measure', 'cache_policy.py'), name


def _expected_parameters(row, namespace):
    function = ast.parse(row['signature'] + '\n    pass').body[0]
    arguments = function.args
    def value(node):
        return eval(compile(ast.Expression(node), '<default>', 'eval'), namespace)
    positional = arguments.posonlyargs + arguments.args
    defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    expected = [(a.arg, 'positional', None if d is None else value(d), d is not None) for a, d in zip(positional, defaults)]
    if arguments.vararg:
        expected.append((arguments.vararg.arg, 'vararg', None, False))
    expected += [(a.arg, 'keyword', None if d is None else value(d), d is not None)
                 for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults)]
    if arguments.kwarg:
        expected.append((arguments.kwarg.arg, 'varkw', None, False))
    return expected


def test_signatures_and_definition_time_defaults_match_the_frozen_packet(m):
    kinds = {inspect.Parameter.POSITIONAL_ONLY: 'positional', inspect.Parameter.POSITIONAL_OR_KEYWORD: 'positional',
             inspect.Parameter.VAR_POSITIONAL: 'vararg', inspect.Parameter.KEYWORD_ONLY: 'keyword',
             inspect.Parameter.VAR_KEYWORD: 'varkw'}
    namespace = dict(vars(m))
    for name, row in SYMBOLS.items():
        actual = [(p.name, kinds[p.kind], None if p.default is p.empty else p.default, p.default is not p.empty)
                  for p in inspect.signature(getattr(m, name)).parameters.values()]
        assert actual == _expected_parameters(row, namespace), name


# ---------------------------------------------------------------- captured defaults

NAME_BEARING_DEFAULTS = {
    '_keepwarm_read_tail': ('_KEEPWARM_TAIL_LINES',),
    '_cap_append_log': ('_APPEND_LOG_MAX_BYTES',),
    '_keepwarm_rotate_log': ('_KEEPWARM_SCHEDULER_LOG_MAX_BYTES',),
    '_keepwarm_build_ping_cmd': ('_KEEPWARM_PING_BUDGET_USD',),
    '_keepwarm_compact_ledger': ('_KEEPWARM_LEDGER_RETAIN_DAYS',),
}


@pytest.mark.parametrize('name', sorted(NAME_BEARING_DEFAULTS))
def test_defaults_are_the_definition_time_snapshot(m, monkeypatch, name):
    function = getattr(m, name)
    before = (function.__defaults__, function.__kwdefaults__)
    assert before[0], name
    for constant in NAME_BEARING_DEFAULTS[name]:
        assert getattr(m, constant) in before[0]
        monkeypatch.setattr(m, constant, object())
    assert (function.__defaults__, function.__kwdefaults__) == before


def test_read_tail_and_ping_command_keep_their_captured_defaults(m, monkeypatch, tmp_path):
    transcript = tmp_path / 'transcript.jsonl'
    _write(transcript, ''.join(f'{i}\n' for i in range(m._KEEPWARM_TAIL_LINES + 50)))
    budget = m._KEEPWARM_PING_BUDGET_USD
    expected_lines = m._KEEPWARM_TAIL_LINES
    monkeypatch.setattr(m, '_KEEPWARM_TAIL_LINES', 2)
    monkeypatch.setattr(m, '_KEEPWARM_PING_BUDGET_USD', 99)
    assert len(m._keepwarm_read_tail(transcript)) == expected_lines
    assert len(m._keepwarm_read_tail(transcript, 2)) == 2
    command = m._keepwarm_build_ping_cmd('session', 'model', '/bin/claude')
    assert '--fork-session' in command
    assert command[command.index('--max-budget-usd') + 1] == str(budget)


def test_append_log_cap_uses_the_captured_limit_and_the_live_os_module(m, monkeypatch, tmp_path):
    log = tmp_path / 'tick.log'
    limit = m._APPEND_LOG_MAX_BYTES
    log.write_bytes((b'x' * 99 + b'\n') * ((limit + 4096) // 100 + 1))
    size = log.stat().st_size
    monkeypatch.setattr(m, '_APPEND_LOG_MAX_BYTES', 10 ** 12)

    class RecordingOs:
        reads = 0
        @property
        def SEEK_END(self):
            type(self).reads += 1
            return os.SEEK_END

    monkeypatch.setattr(m, 'os', RecordingOs())
    m._cap_append_log(log)
    assert RecordingOs.reads == 1
    assert 0 < log.stat().st_size < size


# ---------------------------------------------------------------- patch effectiveness

def test_same_domain_helper_patch_is_effective_and_undone(m, monkeypatch, tmp_path):
    sidecar = tmp_path / 'keepwarm.jsonl'
    _write(sidecar, '{"a": 1}\nnot json\n{"b": 2}\n\n')
    original_path, original_valid = m._keepwarm_sidecar_path, m._valid_keepwarm_record
    path_fake, valid_fake = Mock(return_value=sidecar), Mock(return_value=True)
    with monkeypatch.context() as patch:
        patch.setattr(m, '_keepwarm_sidecar_path', path_fake)
        patch.setattr(m, '_valid_keepwarm_record', valid_fake)
        assert m.load_keepwarm_records() == [{'a': 1}, {'b': 2}]
        assert path_fake.call_count == 1 and valid_fake.call_count == 2
    assert m._keepwarm_sidecar_path is original_path and m._valid_keepwarm_record is original_valid
    assert m.load_keepwarm_records() == []
    assert path_fake.call_count == 1 and valid_fake.call_count == 2


def test_bypass_negative_control_fails_the_sentinel_assertion(m, monkeypatch, tmp_path):
    """A deliberately stale alias to a same-domain helper must not see the patch."""
    stale = m._valid_keepwarm_record
    fake = Mock(return_value=True)
    monkeypatch.setattr(m, '_valid_keepwarm_record', fake)
    sidecar = tmp_path / 'keepwarm.jsonl'
    _write(sidecar, '{"a": 1}\n')
    assert m.load_keepwarm_records(sidecar) == [{'a': 1}]
    assert fake.call_count == 1
    with pytest.raises(AssertionError):
        assert stale({'a': 1}) is True


def test_shared_pricing_table_and_rebound_paths_reach_moved_code(m, monkeypatch, tmp_path):
    sonnet = m.PRICING_TIERS['anthropic']['claude_models']['sonnet']
    monkeypatch.setitem(sonnet, 'input', 4_000_000)
    per_token, _, premium = m._keepwarm_derive_pricing()
    assert per_token['sonnet'] == 4.0
    assert premium['5m'] == sonnet['cache_write'] / 4_000_000
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', tmp_path / 'rebound')
    assert m._keepwarm_sidecar_path() == tmp_path / 'rebound' / m._KEEPWARM_SIDECAR_NAME
    monkeypatch.setattr(m, 'HOME', tmp_path / 'other-home')
    assert m._keepwarm_default_claude_json_path() == tmp_path / 'other-home' / '.claude.json'


def test_unmoved_callers_see_patched_moved_functions_and_the_undo(m, monkeypatch, tmp_path):
    original = m.keepwarm_billing_mode
    with monkeypatch.context() as patch:
        patch.setattr(m, 'keepwarm_billing_mode', Mock(return_value='api'))
        assert m._advice_billing_mode() == 'api'
        patch.setattr(m, 'keepwarm_billing_mode', Mock(side_effect=RuntimeError('fixture')))
        assert m._advice_billing_mode() == 'unknown'
        plist = tmp_path / 'keepwarm.plist'
        _write(plist, '')
        scheduler = Mock(return_value=plist)
        patch.setattr(m, '_normalized_platform', lambda: 'Darwin')
        patch.setattr(m, 'PLIST_PATH', tmp_path / 'missing.plist')
        patch.setattr(m, '_keepwarm_scheduler_plist_path', scheduler)
        patch.setattr(m, '_plist_job_paths', lambda path: ['fixture-path'])
        assert {Path(key): value for key, value in m._scheduler_job_paths().items()} == {plist: ['fixture-path']}
        assert scheduler.call_count == 1
    assert m.keepwarm_billing_mode is original
    assert m._advice_billing_mode() == original()


# ---------------------------------------------------------------- state stays on the facade

def test_ttl_sidecar_uses_live_clock_runtime_memo_and_facade_state(m, monkeypatch, tmp_path):
    snapshot = tmp_path / 'snap'
    snapshot.mkdir()
    monkeypatch.setattr(m, 'SNAPSHOT_DIR', snapshot)
    monkeypatch.setattr(m, 'detect_runtime', Mock(return_value='fixture-runtime'))
    # 'engine': the sidecar only serves payloads from the current rebuild engine.
    compute = Mock(return_value={'period_days': 7, 'platform': 'fixture-runtime', 'waste': 1, 'engine': 2})
    monkeypatch.setattr(m, '_get_cache_ttl_waste', compute)
    monkeypatch.setattr(m, '_cache_health_memo', None)
    clock = SimpleNamespace(time=Mock(return_value=1000.0))
    monkeypatch.setattr(m, 'time', clock)
    assert m._cache_ttl_waste_cached(days=7)['waste'] == 1
    compute.assert_called_once_with(days=7)
    assert json.loads(_read(snapshot / 'cache_health.json'))['_cached_ts'] == 1000.0
    assert m._cache_health_memo[1]['_cached_ts'] == 1000.0
    clock.time.return_value = 1001.0
    assert m._cache_ttl_waste_cached(days=7)['_cached_ts'] == 1000.0
    assert compute.call_count == 1
    clock.time.return_value = 1000.0 + m._CACHE_HEALTH_CACHE_TTL_SECONDS + 1
    m._cache_ttl_waste_cached(days=7)
    assert compute.call_count == 2
    m._cache_ttl_waste_cached(days=7, fresh=True)
    assert compute.call_count == 3
    assert sorted(path.name for path in snapshot.iterdir()) == ['cache_health.json']


def test_claude_code_version_probe_memoises_on_the_facade(m, monkeypatch):
    monkeypatch.setattr(m, '_SUBAGENT_CACHE_CC_VERSION', m._SUBAGENT_CACHE_UNKNOWN)
    run = Mock(return_value=SimpleNamespace(stdout='2.1.50 (Claude Code)\n', returncode=0))
    monkeypatch.setattr(m, 'subprocess', SimpleNamespace(run=run, SubprocessError=RuntimeError))
    monkeypatch.setattr(m, '_resolve_runtime_bin', Mock(return_value='/fixture/claude'))
    expected = m._parse_semver('2.1.50')
    assert m._subagent_cache_claude_code_version() == expected
    assert m._SUBAGENT_CACHE_CC_VERSION == expected
    assert m._subagent_cache_claude_code_version() == expected
    assert run.call_count == 1
    assert run.call_args.args[0] == ['/fixture/claude', '--version']


def test_script_path_follows_the_facade_file(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, '_stable_marketplace_script_path', lambda name: None)
    assert Path(m._keepwarm_measure_script_path()) == Path(m.__file__).resolve()
    assert Path(m._keepwarm_measure_script_path()).name == 'measure.py'
    monkeypatch.setattr(m, '__file__', str(tmp_path / 'elsewhere' / 'measure.py'))
    assert Path(m._keepwarm_measure_script_path()) == (tmp_path / 'elsewhere' / 'measure.py').resolve()
    monkeypatch.setattr(m, '_stable_marketplace_script_path', lambda name: '/stable/' + name)
    assert m._keepwarm_measure_script_path() == '/stable/measure.py'


# ---------------------------------------------------------------- exception paths (BASE == candidate)

class FixtureError(Exception):
    """Raised by fakes; its type name must reach the handler's message."""


def test_install_scheduler_error_branch_reports_the_raised_exception(m, monkeypatch):
    install = Mock(side_effect=FixtureError('boom'))
    monkeypatch.setattr(m, 'keepwarm_scheduler_install', install)
    assert m._keepwarm_enable_install_scheduler() == (
        '  Scheduler install errored (FixtureError). Run manually: measure.py keepwarm-scheduler install')
    assert install.call_count == 1 and install.call_args.kwargs == {'quiet': True}
    monkeypatch.setattr(m, 'keepwarm_scheduler_install', Mock(return_value=(True, '')))
    assert m._keepwarm_enable_install_scheduler() == '  Scheduler installed (macOS): keep-warm ticks every ~5 minutes.'
    monkeypatch.setattr(m, 'keepwarm_scheduler_install', Mock(return_value=(False, 'no launchctl\nmore')))
    assert m._keepwarm_enable_install_scheduler() == (
        '  Scheduler NOT installed: no launchctl\n'
        '  Consent is recorded; run manually: measure.py keepwarm-scheduler install')


def test_compact_item_error_branch_degrades_to_not_enough_data(m, monkeypatch):
    monkeypatch.setattr(m, '_subagent_cache_claude_only', lambda: True)
    advice = Mock(side_effect=FixtureError('boom'))
    monkeypatch.setattr(m, 'compact_advice', advice)
    base = {'id': 'compact_window', 'direction': 'smaller', 'command': '',
            'tradeoff': 'Compaction can drop early instructions.'}
    assert m._recs_compact_item() == dict(
        base, state='not_enough_data', enough_data=False,
        headline='compaction history could not be measured yet', numbers={}, partial=True,
        reason='compact_advice failed: FixtureError')
    assert advice.call_count == 1
    monkeypatch.setattr(m, '_subagent_cache_claude_only', lambda: False)
    assert m._recs_compact_item() == dict(base, state='not_applicable', enough_data=False, headline='', numbers={},
                                          reason='the compact window is a Claude Code setting')
    assert advice.call_count == 1


def test_subagent_block_error_branch_and_normal_branch(m, monkeypatch):
    monkeypatch.setattr(m, 'subagent_cache_status', Mock(side_effect=KeyError('k')))
    assert m.subagent_cache_block(days=3) == {
        'state': 'unknown', 'set_by': None, 'payoff': None, 'recommendation': None, 'hint': None,
        'reason': 'status unavailable: KeyError'}
    status = Mock(return_value={'state': 'on'})
    monkeypatch.setattr(m, 'subagent_cache_status', status)
    assert m.subagent_cache_block(days=3, now=7) == {
        'state': 'on', 'estimate': True, 'recommendation': None, 'hint': None}
    status.assert_called_once_with(days=3, now=7, use_cache=True)


@structural
def test_no_use_of_a_local_binding_was_rewritten_to_a_facade_attribute():
    script = _load_script()
    source = _read(SCRIPTS / '_measure/cache_policy.py')
    handlers = [h for n in ast.walk(ast.parse(source)) for h in ast.walk(n) if isinstance(h, ast.ExceptHandler) and h.name]
    assert handlers, 'the audit must be looking at real except-as targets'
    assert script.binding_violations(source) == []
    planted = ('from . import _compat\n'
               'def probe():\n'
               '    _measure_facade = _compat.facade()\n'
               '    try:\n'
               '        pass\n'
               '    except ValueError as exc:\n'
               '        return str(_measure_facade.exc)\n')
    assert script.binding_violations(planted) == [('probe', 'exc', 7)]
    assert script.binding_violations(planted.replace('_measure_facade.exc', 'exc')) == []


@structural
def test_retained_bootstrap_stays_in_the_facade_before_the_bridge(copy_root):
    assert '_redirect_bytecode_out_of_repo' not in SYMBOLS
    source = _read(SCRIPTS / 'measure.py')
    body = ast.parse(source).body
    definition = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == '_redirect_bytecode_out_of_repo')
    bridge = next(n for n in body if isinstance(n, ast.ImportFrom) and n.module == '_measure')
    assert definition.lineno < bridge.lineno
    root = copy_root()
    packet = root / 'scripts/measure_split_ownership.json'
    data = json.loads(_read(packet))
    data['lanes']['4']['symbols'].append(dict(data['lanes']['4']['symbols'][0], name='_redirect_bytecode_out_of_repo'))
    _write(packet, json.dumps(data))
    before = [path.read_bytes() for path in _files(root)]
    with pytest.raises(_load_script().Refusal, match='retained in measure.py'):
        _run(root)
    assert [path.read_bytes() for path in _files(root)] == before


# ---------------------------------------------------------------- context managers

@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('name, lock_name', [('_keepwarm_lock', '_KEEPWARM_LOCK_NAME'),
                                             ('_keepwarm_tripwire_lock', '_KEEPWARM_TRIPWIRE_LOCK_NAME')])
def test_lease_context_managers_keep_wrapping_and_clean_up(m, monkeypatch, name, lock_name, fail):
    events = []

    @contextmanager
    def lease(*args, **kwargs):
        events.append(('enter', args, kwargs))
        try:
            yield True
        finally:
            events.append('exit')

    monkeypatch.setattr(m, 'lease_lock', lease)
    monkeypatch.setattr(m, 'current_deadline', lambda: 'fixture-deadline')
    manager = getattr(m, name)
    assert manager.__wrapped__.__name__ == name
    try:
        with manager() as acquired:
            assert acquired is True
            if fail:
                raise ValueError('fixture')
    except ValueError:
        assert fail
    assert events[-1] == 'exit'
    assert events[0][1] == (m.SNAPSHOT_DIR / f'{getattr(m, lock_name)}.lease',)
    assert events[0][2] == {'deadline': 'fixture-deadline', 'acquire_timeout': 0.075}


@pytest.mark.parametrize('fail', [False, True])
def test_tick_singleton_excludes_a_live_contender_and_releases(m, monkeypatch, fail):
    alive = Mock(return_value=True)
    monkeypatch.setattr(m, '_pid_is_running', alive)
    lock = m.SNAPSHOT_DIR / m._KEEPWARM_TICK_LOCK_NAME
    assert m._keepwarm_tick_singleton.__wrapped__.__name__ == '_keepwarm_tick_singleton'
    try:
        with m._keepwarm_tick_singleton() as first:
            assert first is True and lock.read_text(encoding='ascii') == str(os.getpid())
            with m._keepwarm_tick_singleton() as second:
                assert second is False
            assert alive.call_count == 1 and lock.exists()
            if fail:
                raise ValueError('fixture')
    except ValueError:
        assert fail
    assert not lock.exists()
