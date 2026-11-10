from datetime import datetime, timedelta

import pytest

from test_counted_pricing_v5 import OPUS, m


def test_partial_cache_rebuild_uses_removal_position(m):
    now = datetime(2026, 11, 1)
    tier = m.PRICING_TIERS[m._load_pricing_tier()]
    turns = [
        (now + timedelta(seconds=1), OPUS, 100_000, (0, 0, 0, 100_000)),
        (now + timedelta(seconds=2), OPUS, 101_000, (30_000, 30_000, 0, 71_000)),
        (now + timedelta(seconds=3), OPUS, 99_000, (30_000, 30_000, 0, 69_000)),
        (now + timedelta(seconds=4), OPUS, 101_000, (1_000, 1_000, 0, 100_000)),
    ]
    score = m._counted_score_detail(turns, [], now, 1000, tier)
    assert score['reread_miss_turns'] == 1


def test_rebuild_boundaries():
    from _measure.counted_position import is_rebuild
    assert is_rebuild((30, 30, 0, 96), 10, 120, 100)
    assert not is_rebuild((30, 30, 0, 97), 10, 120, 100)
    assert not is_rebuild((30, 30, 0, 99), 10, 120, 100)
    assert not is_rebuild((30, 30, 0, 100), 10, 120, 100)
    assert not is_rebuild((30, 30, 0, 0), 10, 99, 100)
    assert not is_rebuild((9, 9, 0, 80), 10, 120, 100)
    assert not is_rebuild(None, 10, 120, 100)


def test_fable_52_card_and_exact_model(m):
    card = m._counted_exact_card('claude-fable-5-2', m.PRICING_TIERS['anthropic'])
    assert [card[k] for k in ('input', 'output', 'cache_read', 'cache_write', 'cache_write_1h')] == [8, 40, .2, 10, 16]


def test_unproven_resume_does_not_enter_total(m):
    result = m._action_savings_components({'resume_lean_estimated': {'cost_saved_usd': 123}})
    assert result['estimates_usd']['resume_lean_estimated'] == 0
    assert result['total_usd'] == pytest.approx(0)
