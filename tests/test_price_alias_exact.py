"""Foreign (OpenAI / Gemini) model ids map to a rate card only when they equal a
card id or are a numeric/date snapshot, a provider-routed form or a preview build of
one. A different variant (-lite, -mini, -image, -codex on a base that has none, a
later generation) is unpriced and never borrows a shorter card."""
import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture(scope="module")
def m():
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    yield mod
    sys.modules.pop("measure", None)


def _card(mod, model):
    return mod._normalize_openai_model_name(model) or mod._normalize_gemini_model_name(model)


# Every id priced before exact matching, and the card it priced at. All of these
# are the same model as their card and must keep the same price.
STAYS_PRICED = [
    ('GPT-5.4', 'gpt-5.4'),
    ('azure/gpt-5', 'gpt-5'),
    ('developers.openai.com/api/docs/models/gpt-6-astra', 'gpt-6-astra'),
    ('gemini-2.5-flash', 'gemini-2.5-flash'),
    ('gemini-2.5-flash-001', 'gemini-2.5-flash'),
    ('gemini-2.5-flash-20260101', 'gemini-2.5-flash'),
    ('gemini-2.5-flash-lite', 'gemini-2.5-flash-lite'),
    ('gemini-2.5-flash-lite-001', 'gemini-2.5-flash-lite'),
    ('gemini-2.5-flash-preview-05-20', 'gemini-2.5-flash'),
    ('gemini-2.5-flash-preview-09-2025', 'gemini-2.5-flash'),
    ('gemini-2.5-flash@20260101', 'gemini-2.5-flash'),
    ('gemini-2.5-pro', 'gemini-2.5-pro'),
    ('gemini-3-flash', 'gemini-3-flash'),
    ('gemini-3-flash-preview', 'gemini-3-flash-preview'),
    ('gemini-3-pro', 'gemini-3-pro'),
    ('gemini-3-pro-preview', 'gemini-3-pro'),
    ('gemini-3.1-flash-lite', 'gemini-3.1-flash-lite'),
    ('gemini-3.1-flash-lite-preview', 'gemini-3.1-flash-lite-preview'),
    ('gemini-3.1-pro', 'gemini-3.1-pro'),
    ('gemini-3.1-pro-preview', 'gemini-3.1-pro-preview'),
    ('gemini-3.1-pro-preview-customtools', 'gemini-3.1-pro-preview-customtools'),
    ('gemini-3.5-flash', 'gemini-3.5-flash'),
    ('gemini-3.5-flash-lite', 'gemini-3.5-flash-lite'),
    ('gemini-3.5-flash-lite-preview', 'gemini-3.5-flash-lite'),
    ('gemini-3.6-flash', 'gemini-3.6-flash'),
    ('gemini-3.7-flash', 'gemini-3.7-flash'),
    ('gemini-3.8-flash', 'gemini-3.8-flash'),
    ('gemini-4-argon', 'gemini-4-argon'),
    ('gemini-flash-latest', 'gemini-flash-latest'),
    ('gemini-flash-lite-latest', 'gemini-flash-lite-latest'),
    ('gemini-gemma-2-27b-it', 'gemini-gemma-2-27b-it'),
    ('gemini-gemma-2-9b-it', 'gemini-gemma-2-9b-it'),
    ('gemini-omni-1.1-flash', 'gemini-omni-1.1-flash'),
    ('gemini-omni-flash-preview', 'gemini-omni-flash-preview'),
    ('gemini-pro-latest', 'gemini-pro-latest'),
    ('gemini-robotics-er-2-preview', 'gemini-robotics-er-2-preview'),
    ('gemini-robotics-er-2-streaming-preview', 'gemini-robotics-er-2-streaming-preview'),
    ('gemini/gemini-2.5-flash', 'gemini-2.5-flash'),
    ('google/gemini-2.5-pro', 'gemini-2.5-pro'),
    ('gpt 5.4', 'gpt-5.4'),
    ('gpt-3.5-turbo', 'gpt-3.5-turbo'),
    ('gpt-3.5-turbo-16k', 'gpt-3.5-turbo-16k'),
    ('gpt-4', 'gpt-4'),
    ('gpt-4-0613', 'gpt-4'),
    ('gpt-4-1106-preview', 'gpt-4-1106-preview'),
    ('gpt-4-turbo', 'gpt-4-turbo'),
    ('gpt-4.1', 'gpt-4.1'),
    ('gpt-4.1-mini', 'gpt-4.1-mini'),
    ('gpt-4.1-nano', 'gpt-4.1-nano'),
    ('gpt-4o', 'gpt-4o'),
    ('gpt-4o-2024-08-06', 'gpt-4o'),
    ('gpt-4o-mini', 'gpt-4o-mini'),
    ('gpt-5', 'gpt-5'),
    ('gpt-5-chat', 'gpt-5-chat'),
    ('gpt-5-chat-latest', 'gpt-5-chat-latest'),
    ('gpt-5-codex', 'gpt-5-codex'),
    ('gpt-5-codex-2025-09-15', 'gpt-5-codex'),
    ('gpt-5-mini', 'gpt-5-mini'),
    ('gpt-5-nano', 'gpt-5-nano'),
    ('gpt-5-pro', 'gpt-5-pro'),
    ('gpt-5.1', 'gpt-5.1'),
    ('gpt-5.1-chat-latest', 'gpt-5.1-chat-latest'),
    ('gpt-5.1-codex', 'gpt-5.1-codex'),
    ('gpt-5.1-codex-max', 'gpt-5.1-codex-max'),
    ('gpt-5.1-codex-max-2026-01-01', 'gpt-5.1-codex-max'),
    ('gpt-5.1-codex-mini', 'gpt-5.1-codex-mini'),
    ('gpt-5.2', 'gpt-5.2'),
    ('gpt-5.2-chat-latest', 'gpt-5.2-chat-latest'),
    ('gpt-5.2-codex', 'gpt-5.2-codex'),
    ('gpt-5.2-pro', 'gpt-5.2-pro'),
    ('gpt-5.3-chat-latest', 'gpt-5.3-chat-latest'),
    ('gpt-5.3-codex', 'gpt-5.3-codex'),
    ('gpt-5.4', 'gpt-5.4'),
    ('gpt-5.4-2026-03-05', 'gpt-5.4'),
    ('gpt-5.4-mini', 'gpt-5.4-mini'),
    ('gpt-5.4-mini-2026-03-05', 'gpt-5.4-mini'),
    ('gpt-5.4-nano', 'gpt-5.4-nano'),
    ('gpt-5.4-pro', 'gpt-5.4-pro'),
    ('gpt-5.5', 'gpt-5.5'),
    ('gpt-5.5-cyber', 'gpt-5.5-cyber'),
    ('gpt-5.5-cyber-preview', 'gpt-5.5-cyber-preview'),
    ('gpt-5.5-pro', 'gpt-5.5-pro'),
    ('gpt-5.6', 'gpt-5.6-sol'),
    ('gpt-5.6-2026-07-09', 'gpt-5.6-sol'),
    ('gpt-5.6-cyber', 'gpt-5.6-cyber'),
    ('gpt-5.6-luna', 'gpt-5.6-luna'),
    ('gpt-5.6-sol', 'gpt-5.6-sol'),
    ('gpt-5.6-terra', 'gpt-5.6-terra'),
    ('gpt-5.6-terra-2026-07-09', 'gpt-5.6-terra'),
    ('gpt-5.6_luna', 'gpt-5.6-luna'),
    ('gpt-6-astra', 'gpt-6-astra'),
    ('gpt-6-astra-2026-09-01', 'gpt-6-astra'),
    ('gpt-6-luna', 'gpt-6-luna'),
    ('gpt-6-sol', 'gpt-6-sol'),
    ('gpt-6.1-sol', 'gpt-6.1-sol'),
    ('gpt-6.1-terra', 'gpt-6.1-terra'),
    ('models/gemini-2.5-pro', 'gemini-2.5-pro'),
    ('o1', 'o1'),
    ('o1-pro', 'o1-pro'),
    ('o3', 'o3'),
    ('o3-2025-04-16', 'o3'),
    ('o3-mini', 'o3-mini'),
    ('o3-pro', 'o3-pro'),
    ('o4-mini', 'o4-mini'),
    ('o4-mini-2025-04-16', 'o4-mini'),
    ('openai/gpt-5', 'gpt-5'),
    ('openai/gpt-5.4-2026-03-05', 'gpt-5.4'),
    ('openai/gpt-6-astra-2026-09-01', 'gpt-6-astra'),
    ('openrouter/openai/gpt-5.6-sol-2026-07-09', 'gpt-5.6-sol'),
    ('vertex_ai/gemini-2.5-pro', 'gemini-2.5-pro'),
]

# Priced before exact matching by borrowing a shorter card; a different product
# or an unknown variant, so now unpriced.
NOW_UNPRICED = [
    "gemini-2.5-flash-future", "gemini-2.5-pro-unlisted-variant", "gpt-5.4-future",
    "gpt-5.4-unlisted-variant", "gpt-5.5-codex", "gpt-5.6-", "gpt-5.6-unlisted-variant",
    # Never a card; the old prefix match could have borrowed one.
    "gemini-2.5-flash-image", "gemini-3.5-flash-live", "gemini-2.5-pro-exp-03-25",
    "gpt-5.3-codex-spark", "gpt-5.4-codex", "gpt-5.2-codex-latest", "o3-deep-research",
    "gpt-5.6-sol-high", "gpt-5-4", "gpt-oss-120b", "codex", "unknown", "",
]


@pytest.mark.parametrize("model,card", STAYS_PRICED)
def test_same_model_ids_keep_their_card(m, model, card):
    assert _card(m, model) == card


@pytest.mark.parametrize("model", NOW_UNPRICED)
def test_different_variants_do_not_borrow_a_card(m, model):
    assert _card(m, model) is None


def test_every_table_key_prices_at_itself(m):
    for table, fn in ((m.OPENAI_MODEL_PRICING, m._normalize_openai_model_name),
                      (m.GEMINI_MODEL_PRICING, m._normalize_gemini_model_name)):
        for key in table:
            expected = "gpt-5.6-sol" if key == "gpt-5.6" else key
            assert fn(key) == expected, key


def test_spend_cost_of_an_unlisted_variant_is_zero_not_a_borrowed_card(m):
    assert m._get_model_cost("gpt-5.4-future", 1_000_000, 0) == 0
    assert m._get_model_cost("gpt-5.4", 1_000_000, 0) > 0


@pytest.mark.parametrize("model,card", [
    ("gpt-5.4-2026-03-05", "gpt-5.4"), ("openai/gpt-5.6-sol-2026-07-09", "gpt-5.6-sol"),
    ("GPT-5.6 Sol Pro", "gpt-5.6-sol"), ("gemini-2.5-flash-lite-001", "gemini-2.5-flash-lite"),
    ("gemini-3-pro-preview", "gemini-3-pro"), ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
])
def test_fleet_resolver_keeps_same_model_ids(model, card):
    sys.path.insert(0, str(SCRIPTS.parent.parent / "fleet-auditor" / "scripts"))
    import shared
    assert shared.normalize_model_name(model) == card


@pytest.mark.parametrize("model", ["gpt-5.4-future", "gpt-5.6-unlisted-variant", "gemini-2.5-flash-image"])
def test_fleet_resolver_does_not_borrow_a_card_for_other_variants(model):
    sys.path.insert(0, str(SCRIPTS.parent.parent / "fleet-auditor" / "scripts"))
    import shared
    assert shared.normalize_model_name(model) == model
