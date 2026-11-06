"""Shared constants, fixtures and helpers for the lane U4 test files.

Used by tests/test_measure_split_lane4.py (behaviour and ownership) and
tests/test_measure_split_lane4_replay.py (the move script and its refusals).
"""
import ast
import functools
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest


def _read(path):
    """Exact stored text; Windows text mode would translate newlines and hide a CRLF defect."""
    return Path(path).read_bytes().decode('utf-8')


def _write(path, text):
    """LF-exact UTF-8 bytes on every platform: the repo requires LF and the replay compares raw bytes."""
    Path(path).write_bytes(text.encode('utf-8'))


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
PACKET = json.loads(_read(REPO / 'scripts/measure_split_ownership.json'))['lanes']['4']
SYMBOLS = {row['name']: row for row in PACKET['symbols']}
BASE_RUN = bool(os.environ.get('MEASURE_SPLIT_SOURCE'))
structural = pytest.mark.skipif(BASE_RUN, reason='describes the split tree, not the unsplit BASE')
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@functools.lru_cache(maxsize=1)
def _load_script():
    spec = importlib.util.spec_from_file_location('_u4_move_under_test', REPO / 'scripts/split_lanes/u4_move.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(root, **kwargs):
    """Run the mover on a disposable root; the frozen inputs come from the pin, here via this checkout's objects."""
    return _load_script().run(root, objects=REPO, **kwargs)


@pytest.fixture
def copy_root(tmp_path):
    """A disposable repository root holding only what the mover reads."""
    def make(name='repo'):
        root = tmp_path / name
        for relative in ('scripts/measure_split.py', 'scripts/measure_split_ownership.json',
                         'scripts/measure_split_manifest.json', 'scripts/mutations/_measure_lane4.json'):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / relative, root / relative)
        target = root / 'skills/token-optimizer/scripts'
        (target / '_measure').mkdir(parents=True)
        shutil.copy2(SCRIPTS / 'measure.py', target / 'measure.py')
        for path in (SCRIPTS / '_measure').glob('*.py'):
            shutil.copy2(path, target / '_measure' / path.name)
        return root
    return make


def _files(root):
    scripts = root / 'skills/token-optimizer/scripts'
    return scripts / 'measure.py', scripts / '_measure/cache_policy.py'
