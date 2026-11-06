"""Shared pieces of the lane U7 proof: paths, the frozen ownership packet, and the fixtures that both
test_measure_split_lane7.py (provider and facade behaviour) and test_measure_split_lane7_replay.py (the
mover script) use. Moved verbatim from the single test file; nothing here is new behaviour.
"""
import ast
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
PROVIDER = SCRIPTS / '_measure/daemon.py'
OWNERSHIP = json.loads((REPO / 'scripts/measure_split_ownership.json').read_text(encoding='utf-8'))
PACKET = OWNERSHIP['lanes']['7']
NAMES = [row['name'] for row in PACKET['symbols']]
FRAGMENTS = REPO / 'scripts/mutations/_measure_lane7.json'
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FACADE_REL, PROVIDER_REL = 'skills/token-optimizer/scripts/measure.py', 'skills/token-optimizer/scripts/_measure/daemon.py'
FRAGMENTS_REL = 'scripts/mutations/_measure_lane7.json'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def mover():
    return load(REPO / 'scripts/split_lanes/u7_move.py', '_u7_move_under_test')


@pytest.fixture(autouse=True)
def lf_fixtures(monkeypatch):
    """Fixture files are written with LF on every platform, as the repository requires."""
    original = Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        return original(self, data, encoding=encoding, errors=errors, newline='\n' if newline is None else newline)
    monkeypatch.setattr(Path, 'write_text', write_text)


def provider_tree():
    return ast.parse(PROVIDER.read_text(encoding='utf-8'))


def _fixture_env(home):
    """Everything a module could resolve a user directory from, kept inside this test's own temp home."""
    return {'HOME': home, 'USERPROFILE': home, 'CLAUDE_CONFIG_DIR': home / '.claude',
            'TOKEN_OPTIMIZER_SNAPSHOT_DIR': home / 'data',
            'APPDATA': home / 'AppData' / 'Roaming', 'LOCALAPPDATA': home / 'AppData' / 'Local',
            'XDG_CONFIG_HOME': home / '.config', 'XDG_DATA_HOME': home / '.local' / 'share',
            'XDG_CACHE_HOME': home / '.cache'}
