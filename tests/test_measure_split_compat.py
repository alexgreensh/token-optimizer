"""Characterization of the facade contract, on unsplit and canary trees."""
import inspect
import json
import runpy
import importlib.util
import sys
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from helpers.measure_split import isolated_measure, definition_nodes

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def m(tmp_path, monkeypatch):
    home = tmp_path / 'home é with spaces'
    home.mkdir()
    (home / '.claude').mkdir()
    if os.environ.get('MEASURE_SPLIT_SOURCE'):
        # BASE has no provider dependency. These are the guard's infrastructure.
        monkeypatch.syspath_prepend(str(REPO / 'skills/token-optimizer/scripts'))
        __import__('_measure._compat')
    with isolated_measure(env={'HOME': home, 'USERPROFILE': home,
                               'CLAUDE_CONFIG_DIR': home / '.claude',
                               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}) as module:
        if os.environ.get('MEASURE_SPLIT_SOURCE'):
            assert Path(module._get_model_cost.__code__.co_filename) == Path(os.environ['MEASURE_SPLIT_SOURCE'])
        yield module


def test_same_domain_and_cross_domain_price_patch_and_undo(m, monkeypatch, tmp_path):
    fake = Mock(return_value=17.25)
    original = m._get_model_cost
    with monkeypatch.context() as patch:
        patch.setattr(m, '_get_model_cost', fake)
        assert m._cost_from_model_breakdown({'sonnet': {'fresh_input': 2}}) == 17.25
        assert fake.call_count == 1
        fake.reset_mock()
        result = m._simulate_model_switch({'model': 'opus', 'total_input_tokens': 12,
                                           'total_output_tokens': 3})
        assert result['current_cost'] == 17.25
        assert fake.call_count == 2
        fake.reset_mock()
        savings_consumer = _moved_consumer('_cost_per_session', 'savings', tmp_path)
        assert savings_consumer(1, 0, 0, 0, {}, 'anthropic') == 17.25
        assert fake.call_count == 1
    assert m._get_model_cost is original
    assert m._cost_from_model_breakdown({'sonnet': {'fresh_input': 2}}) != 17.25
    assert savings_consumer(1, 0, 0, 0, {}, 'anthropic') != 17.25


def test_moved_price_lookup_uses_live_helper_and_shared_dictionary(m, monkeypatch):
    fake = Mock(return_value='fixture-model')
    monkeypatch.setattr(m, '_normalize_openai_model_name', fake)
    monkeypatch.setitem(m.OPENAI_MODEL_PRICING, 'fixture-model', {'input': 7, 'output': 11, 'cache_read': 2})
    assert m._get_model_cost('anything', 1_000_000, 1_000_000, tier='anthropic') == 18
    assert fake.call_count == 1
    m.OPENAI_MODEL_PRICING['fixture-model']['input'] = 9
    assert m._get_model_cost('anything', 1_000_000, 0, tier='anthropic') == 9


def test_cache_writes_stay_on_facade(m, tmp_path, monkeypatch):
    config = tmp_path / 'config.toml'
    config.write_text('model = "test-model"\n', encoding='utf-8')
    monkeypatch.setattr(m, 'RUNTIME_DIR', tmp_path)
    monkeypatch.setattr(m, '_codex_config_cache', None)
    data = m._read_codex_config()
    assert data == {'model': 'test-model'}
    assert m._codex_config_cache[1] is data
    assert m._codex_config_model() == 'test-model'


def test_facade_file_and_imported_os_are_live(m, tmp_path, monkeypatch):
    fake = SimpleNamespace(environ={})
    monkeypatch.setattr(m, 'os', fake)
    monkeypatch.setattr(m, '__file__', str(tmp_path / 'scripts/measure.py'))
    assert m._bundled_prices_path() == tmp_path / 'pricing/prices.json'
    fake.environ['TOKEN_OPTIMIZER_PRICES_FILE'] = str(tmp_path / 'override.json')
    assert m._bundled_prices_path() == tmp_path / 'override.json'


@pytest.mark.parametrize('fail', [False, True])
def test_contextmanager_cleanup_and_wrapped_identity(m, monkeypatch, fail):
    events = []
    @contextmanager
    def lease(*args, **kwargs):
        events.append(('enter', args, kwargs))
        try:
            yield True
        finally:
            events.append('exit')
    monkeypatch.setattr(m, 'lease_lock', lease)
    monkeypatch.setattr(m, 'current_deadline', lambda: None)
    assert m._settings_lock.__wrapped__.__name__ == '_settings_lock'
    try:
        with m._settings_lock() as acquired:
            assert acquired
            if fail:
                raise ValueError('fixture')
    except ValueError:
        assert fail
    assert events[-1] == 'exit'
    assert events[0][2]['acquire_timeout'] == 0.075


def test_a_b_a_restore_preserves_functions_defaults_and_paths(tmp_path):
    def env(name):
        home = tmp_path / name
        home.mkdir()
        return {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
                'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data'}
    with isolated_measure(name='measure_fixture_a', env=env('A')) as a:
        function = a._get_model_cost
        defaults = function.__defaults__
        a._codex_config_cache = ('A', {'model': 'A'})
        with isolated_measure(name='measure_fixture_b', env=env('B')) as b:
            assert sys.modules['measure'] is b
            assert a.RUNTIME_DIR != b.RUNTIME_DIR
            assert b._codex_config_cache is None
            assert b._get_model_cost is not function
        assert sys.modules['measure'] is a
        assert a._get_model_cost is function
        assert function.__defaults__ is defaults
        assert a._codex_config_cache[0] == 'A'
        assert a._bundled_prices_path().parent.name == 'pricing'


def test_source_extracted_body_uses_supplied_namespace(tmp_path):
    namespace = {'Path': Path, 'os': SimpleNamespace(environ={}), '__file__': str(tmp_path / 'scripts/measure.py')}
    nodes = definition_nodes(['_bundled_prices_path'], namespace)
    exec(compile(nodes, '<fixture-source>', 'exec'), namespace)
    assert namespace['_bundled_prices_path']() == tmp_path / 'pricing/prices.json'


def test_all_frozen_exports_remain_reachable(m):
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text())
    for row in manifest['symbols']:
        assert hasattr(m, row['name']), row['name']
    assert sys.modules['measure'] is m


def test_bypass_negative_control_breaks_live_lookup(m, monkeypatch):
    """A deliberately stale same-domain alias must fail the sentinel assertion."""
    captured = m._normalize_openai_model_name
    fake = Mock(return_value='fixture-model')
    monkeypatch.setattr(m, '_normalize_openai_model_name', fake)
    monkeypatch.setitem(m.OPENAI_MODEL_PRICING, 'fixture-model', {'input': 7, 'output': 0, 'cache_read': 0})
    assert m._get_model_cost('anything', 1_000_000, 0, tier='anthropic') == 7
    with pytest.raises(AssertionError):
        assert captured('anything') == 'fixture-model'


def _moved_consumer(name, domain, tmp_path):
    """Exercise future lane consumers through ordinary provider loading."""
    if sys.version_info < (3, 11):
        pytest.skip('offline mover requires Python 3.11+')
    from helpers.measure_split import measure_source
    sys.path.insert(0, str(REPO / 'scripts'))
    import measure_split
    body, _ = measure_split.extract_definition(measure_source(Path(sys.modules['measure'].__file__)), name, domain)
    path = tmp_path / (name + '_provider.py')
    path.write_text('from _measure import _compat\n' + body, encoding='utf-8')
    spec = importlib.util.spec_from_file_location(name + '_provider', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from _measure import _compat
    original = getattr(sys.modules['measure'], name)
    return _compat.bind(getattr(module, name), original.__module__,
                        defaults=original.__defaults__, kwdefaults=original.__kwdefaults__)


def test_moved_savings_consumer_uses_patched_connection(m, tmp_path, monkeypatch):
    consumer = _moved_consumer('_get_savings_summary', 'savings', tmp_path)
    connection = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(fetchall=lambda: [('fixture', 1, 123, 4.5)])), close=Mock())
    factory = Mock(return_value=connection)
    monkeypatch.setattr(m, '_init_trends_db', factory)
    def table(conn, name):
        assert conn is connection
        return name
    monkeypatch.setattr(m, '_counted_table', table)
    monkeypatch.setattr(m, '_session_model_reprice', lambda *a, **kw: ({}, 0, 0))
    result = consumer()
    assert result['total_tokens'] == 123
    assert factory.call_count == 1 and connection.execute.call_count == 1
    connection.close.assert_called_once()


def test_rebinding_database_redirects_the_next_connection(m, tmp_path, monkeypatch):
    target = tmp_path / 'rebound.db'
    monkeypatch.setattr(m, 'TRENDS_DB', target)
    conn = m._init_trends_db()
    try:
        assert Path(conn.execute('PRAGMA database_list').fetchone()[2]) == target
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='session_log'").fetchone() == ('session_log',)
    finally:
        conn.close()


def test_transformed_consumers_use_replaced_subprocess_and_datetime(m, tmp_path, monkeypatch):
    child = _moved_consumer('_run_script_child', 'runtime', tmp_path)
    runner = Mock(return_value='child-sentinel')
    monkeypatch.setattr(m, 'subprocess', SimpleNamespace(run=runner))
    monkeypatch.setattr(m, '_windows_stdio_kwargs', lambda: {})
    assert child('fixture.py', ['value']) == 'child-sentinel'
    assert runner.call_count == 1
    parse = _moved_consumer('_parse_iso_process_datetime', 'daemon', tmp_path)
    from datetime import datetime
    clock = SimpleNamespace(fromisoformat=Mock(return_value=datetime(2026, 1, 1)),
                            now=Mock(return_value=datetime(2026, 1, 1, 0, 1)))
    monkeypatch.setattr(m, 'datetime', clock)
    monkeypatch.setattr(m, '_normalize_ps_iso', lambda value: value)
    monkeypatch.setattr(m, '_local_start_label', lambda value: 'fixture-start')
    assert parse('fixture-date') == {'started': 'fixture-start', 'elapsed_seconds': 60}
    clock.fromisoformat.assert_called_once_with('fixture-date')


def test_fresh_provider_defaults_survive_environment_a_b_a(tmp_path, monkeypatch):
    import _measure
    from _measure import _compat
    from types import ModuleType
    package = tmp_path / 'providers'
    package.mkdir()
    (package / 'fixture_defaults.py').write_text(
        'from . import _compat\n'
        'def probe(value=None):\n'
        '    facade = _compat.facade()\n'
        '    return value, facade.VALUE, facade.__file__\n')
    monkeypatch.setattr(_measure, 'DOMAIN_MODULES', ('fixture_defaults',))
    monkeypatch.setattr(_measure, '__path__', [str(package), *_measure.__path__])
    previous = sys.modules.get('measure')
    try:
        def initialize(label, default):
            monkeypatch.setenv('SPLIT_FIXTURE_DEFAULT', str(default))
            module = ModuleType('measure')
            module.__file__ = str(tmp_path / label / 'measure.py')
            module.VALUE = label
            provider = _compat.prepare(module)['fixture_defaults']
            module.probe = _compat.bind(provider.probe, 'measure',
                                        defaults=(int(__import__('os').environ['SPLIT_FIXTURE_DEFAULT']),))
            return module
        a = initialize('A', 11)
        function, defaults = a.probe, a.probe.__defaults__
        b = initialize('B', 22)
        assert b.probe() == (22, 'B', b.__file__)
        assert b.probe is not function
        sys.modules['measure'] = a
        monkeypatch.setenv('SPLIT_FIXTURE_DEFAULT', '99')
        assert function.__defaults__ is defaults
        assert function() == (11, 'A', a.__file__)
    finally:
        sys.modules.pop('_measure.fixture_defaults', None)
        if previous is None:
            sys.modules.pop('measure', None)
        else:
            sys.modules['measure'] = previous
