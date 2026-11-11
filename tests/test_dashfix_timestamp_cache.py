import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import timestamp_cache


def test_timestamp_cache_preserves_utc_and_invalid_input():
    timestamp_cache.parse.cache_clear()
    args = (datetime, timezone.utc, None, ('UTC', 'UTC'))
    expected = datetime(2026, 1, 1, 10, 0)
    assert timestamp_cache.parse('2026-01-01T12:00:00+02:00', *args) == expected
    assert timestamp_cache.parse('2026-01-01T12:00:00+02:00', *args) == expected
    assert timestamp_cache.parse('bad', *args) is None
    assert timestamp_cache.parse.cache_info().hits == 1
