"""Offline source rewriting has a newer floor than runtime and collection."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'scripts/measure_split.py'


def mover():
    spec = importlib.util.spec_from_file_location('floor_mover', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('version', [(3, 9, 6), (3, 9, 25), (3, 10, 0)])
@pytest.mark.parametrize('operation', ['qualifier', 'extract', 'move', 'cli'])
def test_unsupported_mover_refuses_before_parsing_or_writing(tmp_path, monkeypatch, version, operation):
    from types import SimpleNamespace
    module = mover()
    monkeypatch.setattr(module, 'sys', SimpleNamespace(version_info=version))
    marker = tmp_path / 'measure.py'
    marker.write_bytes(b'untouched invalid source')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    actions = {
        'qualifier': lambda: module.Qualifier('invalid', None, set()),
        'extract': lambda: module.extract_definition('invalid', 'work', 'runtime'),
        'move': lambda: module.move(tmp_path, ['work']),
        'cli': lambda: module.move_cli(tmp_path),
    }
    with pytest.raises(RuntimeError, match='requires Python 3.11 or newer'):
        actions[operation]()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_actual_interpreter_cli_floor_has_clear_error_and_no_writes(tmp_path):
    marker = tmp_path / 'measure.py'
    marker.write_bytes(b'untouched invalid source')
    result = subprocess.run([sys.executable, str(SCRIPT), '--scripts', str(tmp_path), '--move-cli'],
                            capture_output=True, text=True)
    if sys.version_info < (3, 11):
        assert result.returncode == 2
        assert result.stdout == ''
        assert 'requires Python 3.11 or newer' in result.stderr
        assert 'Traceback' not in result.stderr
    else:
        # It passed the version gate and reached the deliberately invalid source.
        assert result.returncode != 0
        assert 'SyntaxError' in result.stderr
        assert 'requires Python 3.11' not in result.stderr
    assert marker.read_bytes() == b'untouched invalid source'


def test_supported_parser_rewrites_repeated_fields_and_dynamic_format_spec():
    if sys.version_info < (3, 11):
        pytest.skip('offline mover requires Python 3.11+')
    from types import SimpleNamespace
    source = 'STATE="x"\nWIDTH=5\ndef work():\n    return f"""first {STATE}\nsecond {STATE!r} {STATE:>{WIDTH}}"""\n'
    module = mover()
    moved, _ = module.extract_definition(source, 'work', 'runtime')
    expected = {}
    exec(source, expected)
    values = {'_compat': SimpleNamespace(facade=lambda: SimpleNamespace(STATE='x', WIDTH=5))}
    exec(moved, values)
    assert values['work']() == expected['work']()
    assert '_measure_facade.STATE' in moved
