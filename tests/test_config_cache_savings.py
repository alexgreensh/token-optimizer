import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import config_cache


def test_config_cache_tracks_edits_and_explicit_invalidation(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"tier":"one"}')
    assert config_cache.read(path, json)['tier'] == 'one'
    before = path.stat()
    path.write_text('{"tier":"two"}')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert config_cache.read(path, json)['tier'] == 'two'
    before = path.stat()
    path.write_text('{"tier":"six"}')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    config_cache.clear()
    assert config_cache.read(path, json)['tier'] == 'six'
    path.unlink()
    assert config_cache.read(path, json) == {}
