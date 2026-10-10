"""Every model in the shipped price table is billed at its OWN card, by every engine.

Pricing has regressed repeatedly the same way: a new generation (Opus 5.5,
Fable 5.1, Claude 3-era ids) falls through to its family card, at the wrong
price, while every existing test still passes. These tests are generated from
pricing/prices.json itself, so each model the daily refresh adds is checked
automatically, with no test edit.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))
sys.path.insert(0, str(REPO / "skills" / "fleet-auditor" / "scripts"))

import fleet  # noqa: E402
import measure  # noqa: E402

PRICES = json.loads((REPO / "skills" / "token-optimizer" / "pricing" / "prices.json").read_text())
GENERATION_KEY = re.compile(r"^(fable|mythos|opus|sonnet|haiku)_(\d+)(?:_(\d+))?$")


def _claude_ids():
    for key in sorted(PRICES["anthropic"]):
        m = GENERATION_KEY.match(key)
        if not m:
            continue  # family cards ("opus", "sonnet_legacy") have no model id of their own
        family, major, minor = m.groups()
        version = f"{major}-{minor}" if minor else major
        if int(major) >= 4:
            yield f"claude-{family}-{version}", key
            yield f"claude-{family}-{version}-20990101", key  # dated snapshot
        else:
            yield f"claude-{version}-{family}-20990101", key  # Claude 3-era id shape


@pytest.mark.parametrize("model_id, key", list(_claude_ids()))
def test_every_claude_card_is_reachable_by_its_model_id(model_id, key):
    cards = measure.PRICING_TIERS["anthropic"]["claude_models"]
    assert measure._claude_price_key(model_id, cards) == key
    assert fleet._pricing_key(model_id) == key.replace("_", "-")


# Deliberate aliases: OpenAI names Sol the default 5.6, so the bare id bills
# as Sol (and follows Sol's dated promo switch).
OPENAI_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


@pytest.mark.parametrize("key", sorted(PRICES["openai"]))
def test_every_openai_card_is_reachable(key):
    expected = OPENAI_ALIASES.get(key, key)
    assert measure._normalize_openai_model_name(key) == expected
    assert measure._normalize_openai_model_name(key + "-2099-01-01") == expected
    if key not in OPENAI_ALIASES:
        assert fleet._pricing_key(key) == key


@pytest.mark.parametrize("key", sorted(PRICES["gemini"]))
def test_every_gemini_card_is_reachable(key):
    assert measure._normalize_gemini_model_name(key) == key


@pytest.mark.parametrize("model_id, key", list(_claude_ids()))
def test_billed_rate_is_the_cards_own_rate(model_id, key):
    card = PRICES["anthropic"][key]
    # 50K tokens in: under the 100K long-prompt threshold, so the base card bills
    # (Haiku 5.5's over-100K card is covered in test_bundled_prices.py).
    cost = measure._get_model_cost(model_id, 50_000, 50_000, 0, 0, tier="anthropic")
    assert cost == pytest.approx((card["input"] + card["output"]) * 0.05, rel=1e-6)
