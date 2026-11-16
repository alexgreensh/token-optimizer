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


def test_pricing_snapshot_is_scoped_to_one_compute():
    class Facade:
        calls = 0
        @classmethod
        def _load_pricing_tier(cls):
            cls.calls += 1
            return 'anthropic'
    assert config_cache.current_tier() is None
    with config_cache.pricing_snapshot(Facade):
        assert config_cache.current_tier() == 'anthropic'
        assert config_cache.current_tier() == 'anthropic'
    assert Facade.calls == 1
    assert config_cache.current_tier() is None
