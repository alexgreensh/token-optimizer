"""The auto-refreshed price table: loader, matching, and the refresh pipeline.

Prices change constantly, so these tests never assert a live dollar figure.
They check mechanics with fixture tables: the loader applies what the file
says (and ignores a bad file), model ids resolve to the right card with no code
change, and the refresh script refuses to ship a gutted table or a price that
moved beyond 2x.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "scripts"))

import measure  # noqa: E402
import refresh_prices  # noqa: E402

TABLES = ("PRICING_TIERS", "OPENAI_MODEL_PRICING", "OPENAI_LONG_CONTEXT_PRICING",
          "GEMINI_MODEL_PRICING", "GEMINI_LONG_CONTEXT_PRICING")


@pytest.fixture()
def restore_tables():
    saved = {name: copy.deepcopy(getattr(measure, name)) for name in TABLES}
    yield
    for name, value in saved.items():
        table = getattr(measure, name)
        table.clear()
        table.update(value)
    measure._apply_bundled_prices()


def _write(tmp_path, doc):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _doc(**sections):
    base = {"schema": 1, "anthropic": {}, "openai": {}, "openai_long_context": {},
            "gemini": {}, "gemini_long_context": {}}
    base.update(sections)
    return base


def test_loader_prices_a_new_generation_without_code_changes(tmp_path, monkeypatch, restore_tables):
    monkeypatch.delenv("TOKEN_OPTIMIZER_BUNDLED_PRICES", raising=False)
    path = _write(tmp_path, _doc(
        anthropic={"opus_9": {"input": 7.0, "output": 35.0, "cache_read": 0.7}},
        openai={"gpt-9-nova": {"input": 3.0, "output": 9.0, "cache_read": 0.3}},
        gemini={"gemini-9-flash": {"input": 0.5, "output": 2.0}},
    ))
    assert measure._apply_bundled_prices(path) is True
    m = 1_000_000
    assert measure._get_model_cost("claude-opus-9-20270101", m, m, 0, 0) == pytest.approx(42.0)
    assert measure._get_model_cost("gpt-9-nova-2027-01-01", m, m, m, 0) == pytest.approx(12.3)
    assert measure._get_model_cost("gemini-9-flash", m, m, 0, 0) == pytest.approx(2.5)
    # Partner clouds: first-party rate on Vertex global / Bedrock, +10% on Vertex regional.
    assert measure.PRICING_TIERS["bedrock"]["claude_models"]["opus_9"]["input"] == pytest.approx(7.0)
    assert measure.PRICING_TIERS["vertex-regional"]["claude_models"]["opus_9"]["input"] == pytest.approx(7.7)
    # Missing cache rates are derived, never left at zero.
    card = measure.PRICING_TIERS["anthropic"]["claude_models"]["opus_9"]
    assert card["cache_write"] == pytest.approx(8.75) and card["cache_write_1h"] == pytest.approx(14.0)


@pytest.mark.parametrize("payload", [
    "{not json",
    json.dumps({"schema": 2, "anthropic": {"opus": {"input": 1, "output": 1}}}),
    json.dumps(["a", "list"]),
])
def test_loader_ignores_an_unreadable_file(tmp_path, payload, restore_tables):
    before = copy.deepcopy(measure.PRICING_TIERS)
    path = tmp_path / "prices.json"
    path.write_text(payload, encoding="utf-8")
    assert measure._apply_bundled_prices(path) is False
    assert measure.PRICING_TIERS == before
    assert measure.BUNDLED_PRICES_STATUS["error"]


def test_loader_drops_bad_cards_and_keeps_good_ones(tmp_path, restore_tables):
    path = _write(tmp_path, _doc(openai={
        "gpt-good": {"input": 1.0, "output": 2.0},
        "gpt-negative": {"input": -1.0, "output": 2.0},
        "gpt-huge": {"input": 1e9, "output": 2.0},
        "gpt-nan": {"input": float("nan"), "output": 2.0},
        "gpt-no-output": {"input": 1.0},
        "../evil": {"input": 1.0, "output": 1.0},
    }))
    assert measure._apply_bundled_prices(path) is True
    assert "gpt-good" in measure.OPENAI_MODEL_PRICING
    for bad in ("gpt-negative", "gpt-huge", "gpt-nan", "gpt-no-output", "../evil"):
        assert bad not in measure.OPENAI_MODEL_PRICING


def test_anthropic_long_context_card_loads_into_every_tier(tmp_path, restore_tables):
    """The anthropic_long_context section lands per-tier, Vertex regional +10%."""
    path = _write(tmp_path, _doc(anthropic_long_context={
        "haiku_9_9": {"input": 0.5, "output": 2.5, "cache_read": 0.05,
                      "cache_write": 0.625, "cache_write_1h": 1.0},
    }))
    assert measure._apply_bundled_prices(path) is True
    for tier in ("anthropic", "vertex-global", "bedrock"):
        card = measure.PRICING_TIERS[tier]["claude_models_lc"]["haiku_9_9"]
        assert card["input"] == pytest.approx(0.5)
    regional = measure.PRICING_TIERS["vertex-regional"]["claude_models_lc"]["haiku_9_9"]
    assert regional["input"] == pytest.approx(0.55)


def test_anthropic_long_context_surcharge_applies_over_threshold(tmp_path, restore_tables):
    """A model with an LC card pays it only when the FULL prompt (input + cache
    reads + cache writes) exceeds ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD."""
    path = _write(tmp_path, _doc(
        anthropic={"haiku_9_9": {"input": 1.0, "output": 10.0, "cache_read": 0.1,
                                 "cache_write": 1.25, "cache_write_1h": 2.0}},
        anthropic_long_context={"haiku_9_9": {"input": 5.0, "output": 50.0, "cache_read": 0.5,
                                              "cache_write": 6.25, "cache_write_1h": 10.0}},
    ))
    assert measure._apply_bundled_prices(path) is True
    thr = measure.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD
    # Under the threshold: base card.
    cost = measure._get_model_cost("claude-haiku-9-9", 50_000, 1_000, 0, 0, tier="anthropic",
                                   per_request=True)
    assert cost == pytest.approx(50_000 * 1.0 / 1e6 + 1_000 * 10.0 / 1e6)
    # Cache reads/writes count toward the prompt length too.
    cost = measure._get_model_cost("claude-haiku-9-9", 50_000, 1_000, thr, 0, tier="anthropic",
                                   per_request=True)
    assert cost == pytest.approx(50_000 * 5.0 / 1e6 + 1_000 * 50.0 / 1e6 + thr * 0.5 / 1e6)
    # A model with no LC card never surcharges.
    base = measure._get_model_cost("claude-opus-4-6", thr + 1, 1_000, 0, 0, tier="anthropic",
                                   per_request=True)
    opus = measure.PRICING_TIERS["anthropic"]["claude_models"]["opus_4_6"]
    assert base == pytest.approx((thr + 1) * opus["input"] / 1e6 + 1_000 * opus["output"] / 1e6)


def test_long_context_surcharge_never_applies_to_aggregate_calls(tmp_path, restore_tables):
    """The over-100K tier is decided per API request. A session total, a daily
    sum or a rate probe is not one request: it must be priced on the base card
    (default per_request=False), or a 600K-token session reads ~5x too high."""
    path = _write(tmp_path, _doc(
        anthropic={"haiku_9_9": {"input": 1.0, "output": 10.0, "cache_read": 0.1,
                                 "cache_write": 1.25, "cache_write_1h": 2.0}},
        anthropic_long_context={"haiku_9_9": {"input": 5.0, "output": 50.0, "cache_read": 0.5,
                                              "cache_write": 6.25, "cache_write_1h": 10.0}},
    ))
    assert measure._apply_bundled_prices(path) is True
    thr = measure.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD
    agg = measure._get_model_cost("claude-haiku-9-9", 50_000, 1_000, thr * 6, 0, tier="anthropic")
    assert agg == pytest.approx(50_000 * 1.0 / 1e6 + 1_000 * 10.0 / 1e6 + thr * 6 * 0.1 / 1e6)
    # The rate probes ask for 1M tokens of one class; that is a rate, not a request.
    assert measure._get_model_cost("claude-haiku-9-9", 1_000_000, 0, tier="anthropic") == pytest.approx(1.0)
    assert measure._get_model_cost("claude-haiku-9-9", 0, 1_000_000, tier="anthropic") == pytest.approx(10.0)


def test_shipped_haiku_5_5_rate_probes_use_the_base_card():
    """_model_rate_per_mtok routes advice: Haiku 5.5 must read $0.10, not 5x."""
    base = measure.PRICING_TIERS["anthropic"]["claude_models"]["haiku_5_5"]
    assert measure._get_model_cost("claude-haiku-5-5", 1_000_000, 0, tier="anthropic") == pytest.approx(base["input"])
    assert measure._get_model_cost("claude-haiku-5-5", 0, 1_000_000, tier="anthropic") == pytest.approx(base["output"])


def test_shipped_haiku_5_5_long_context_card_is_5x_the_base_card():
    """Haiku 5.5's over-100K card is exactly 5x its base card on every rate
    (Anthropic prices it by prompt length). Relationship, not dollars."""
    base = measure.PRICING_TIERS["anthropic"]["claude_models"].get("haiku_5_5")
    lc = measure.PRICING_TIERS["anthropic"]["claude_models_lc"].get("haiku_5_5")
    assert base is not None and lc is not None
    for field in ("input", "output", "cache_read", "cache_write", "cache_write_1h"):
        assert lc[field] == pytest.approx(base[field] * 5)


def test_loader_never_removes_a_built_in_card(tmp_path, restore_tables):
    path = _write(tmp_path, _doc(openai={"gpt-new": {"input": 1.0, "output": 2.0}}))
    measure._apply_bundled_prices(path)
    assert "gpt-4o" in measure.OPENAI_MODEL_PRICING
    assert "opus" in measure.PRICING_TIERS["anthropic"]["claude_models"]


def test_loader_can_be_disabled(tmp_path, monkeypatch, restore_tables):
    monkeypatch.setenv("TOKEN_OPTIMIZER_BUNDLED_PRICES", "0")
    path = _write(tmp_path, _doc(openai={"gpt-x": {"input": 1.0, "output": 2.0}}))
    assert measure._apply_bundled_prices(path) is False
    assert "gpt-x" not in measure.OPENAI_MODEL_PRICING


def _fresh_measure_no_bundled(monkeypatch):
    """A fresh measure.py with the bundled file disabled, so PRICING_TIERS
    shows exactly what ships in the literals."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_BUNDLED_PRICES", "0")
    spec = importlib.util.spec_from_file_location(
        "measure_no_bundled_under_test", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bundled_cards(mod, doc, section):
    """What the loader would merge for a section: cleaned cards with derived
    cache rates filled in."""
    cards = mod._clean_price_cards(doc.get(section))
    for card in cards.values():
        card.setdefault("cache_read", round(card["input"] * 0.1, 6))
        card.setdefault("cache_write", round(card["input"] * 1.25, 6))
        card.setdefault("cache_write_1h", round(card["input"] * 2, 6))
    return cards


def test_fallback_literals_equal_the_bundled_table(monkeypatch):
    """F-T2-1: when prices.json is absent/disabled, the literals alone must
    price every Claude card the bundled table carries -- first-party rates on
    anthropic / vertex-global / bedrock, +10% on vertex-regional."""
    mod = _fresh_measure_no_bundled(monkeypatch)
    doc = json.loads(
        (REPO / "skills" / "token-optimizer" / "pricing" / "prices.json").read_text(encoding="utf-8"))
    for section, table in (("anthropic", "claude_models"),
                           ("anthropic_long_context", "claude_models_lc")):
        bundled = _bundled_cards(mod, doc, section)
        assert bundled, f"{section} produced no cards"
        for tier_name, tier in mod.PRICING_TIERS.items():
            mult = 1.1 if tier_name == "vertex-regional" else 1.0
            literal = tier[table]
            assert set(literal) == set(bundled), (
                f"{tier_name}.{table}: cards {sorted(set(literal) ^ set(bundled))} "
                "differ from the bundled table")
            for key, card in bundled.items():
                want = {f: round(v * mult, 6) for f, v in card.items()}
                assert literal[key] == want, f"{tier_name}.{table}[{key}]: {literal[key]} != {want}"


def test_haiku_5_5_prices_correctly_without_the_bundled_file(monkeypatch):
    """F-T2-1 regression: the reported failure mode -- claude-haiku-5-5 was
    priced on the generic $1/$5 haiku card when prices.json did not load."""
    mod = _fresh_measure_no_bundled(monkeypatch)
    assert mod._get_model_cost("claude-haiku-5-5", 1_000_000, 0, tier="anthropic") == pytest.approx(0.1)
    assert mod._get_model_cost("claude-haiku-5-5", 0, 1_000_000, tier="anthropic") == pytest.approx(0.5)
    # Long-context card too: a >100K-prompt request pays the 5x surcharge.
    thr = mod.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD
    cost = mod._get_model_cost("claude-haiku-5-5", 1_000, 1_000, thr + 1, 0,
                               tier="anthropic", per_request=True)
    lc = mod.PRICING_TIERS["anthropic"]["claude_models_lc"]["haiku_5_5"]
    want = 1_000 * lc["input"] / 1e6 + 1_000 * lc["output"] / 1e6 + (thr + 1) * lc["cache_read"] / 1e6
    assert cost == pytest.approx(want)


def test_claude_generation_resolution():
    cards = {"opus": {}, "opus_4": {}, "opus_5_5": {}, "fable": {}, "fable_5_1": {}, "sonnet_4_6": {}}
    key = measure._claude_price_key
    assert key("claude-opus-5-5", cards) == "opus_5_5"
    assert key("claude-opus-5-5[1m]", cards) == "opus_5_5"
    assert key("claude-opus-4-20250514", cards) == "opus_4"       # dated Opus 4.0
    assert key("claude-opus-4-8", cards) == "opus_4"              # no 4.8 card: major version
    assert key("claude-mythos-5-1", cards) == "fable_5_1"         # Mythos shares Fable cards
    assert key("claude-sonnet-4-6", cards) == "sonnet_4_6"
    assert key("opus", cards) == "opus"                           # bare alias: family


def test_shipped_price_table_is_valid_and_matches_generated_ts():
    doc = json.loads(refresh_prices.PRICES_JSON.read_text(encoding="utf-8"))
    assert refresh_prices.validate(doc) == []
    rendered = refresh_prices.render_ts(doc)
    for ts in refresh_prices.TS_OUTPUTS:
        assert ts.read_text(encoding="utf-8") == rendered, f"{ts} drifted from prices.json; rerun refresh_prices.py"


FEED = {
    "claude-opus-7": {"litellm_provider": "anthropic", "mode": "chat", "input_cost_per_token": 5e-6,
                      "output_cost_per_token": 25e-6, "cache_read_input_token_cost": 5e-7,
                      "cache_creation_input_token_cost": 6.25e-6, "cache_creation_input_token_cost_above_1hr": 1e-5},
    "claude-haiku-9-9": {"litellm_provider": "anthropic", "mode": "chat",
                         "input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7,
                         "cache_read_input_token_cost": 1e-8,
                         "cache_creation_input_token_cost": 1.25e-7,
                         "cache_creation_input_token_cost_above_1hr": 2e-7,
                         "input_cost_per_token_above_100k_tokens": 5e-7,
                         "output_cost_per_token_above_100k_tokens": 2.5e-6,
                         "cache_read_input_token_cost_above_100k_tokens": 5e-8,
                         "cache_creation_input_token_cost_above_100k_tokens": 6.25e-7,
                         "cache_creation_input_token_cost_above_1hr_above_100k_tokens": 1e-6},
    "gpt-8": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 2e-6,
              "output_cost_per_token": 8e-6, "input_cost_per_token_above_272k_tokens": 4e-6,
              "output_cost_per_token_above_272k_tokens": 12e-6},
    "gpt-8-2026-01-01": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 9e-6, "output_cost_per_token": 9e-6},
    "gpt-8-audio": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 9e-6, "output_cost_per_token": 9e-6},
    "gemini/gemini-8-flash": {"litellm_provider": "gemini", "mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 4e-6},
}
OFFICIAL_MD = """
| Model | Base input | 5m writes | 1h writes | Cache hits | Output |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Claude Opus 7 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Opus 5 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |
| Claude Fable 5 | $10 / MTok | $12.50 / MTok | $20 / MTok | $1 / MTok | $50 / MTok |
| Claude Sonnet 5 | $2 / MTok<sup>3</sup> | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |
| Claude Sonnet 4.6 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |
| Claude Haiku 4.5 | $1 / MTok | $1.25 / MTok | $2 / MTok | $0.10 / MTok | $5 / MTok |
| Claude Haiku 9.9 (for prompts up to 100,000 tokens) | $0.20 / MTok | $0.25 / MTok | $0.40 / MTok | $0.02 / MTok | $1 / MTok |
| Claude Haiku 9.9 (for prompts over 100,000 tokens) | $1 / MTok | $1.25 / MTok | $2 / MTok | $0.10 / MTok | $5 / MTok |

| Claude Opus 7 | $8 / MTok | $40 / MTok |
| Claude Haiku 9.9 (for prompts over 100,000 tokens) | $0.50 / MTok | $2.50 / MTok |
"""


def test_refresh_builds_cards_and_official_page_wins():
    doc, notes = refresh_prices.build(FEED, OFFICIAL_MD)
    assert doc["anthropic"]["opus_7"]["input"] == 4.0          # official page beat LiteLLM's $5
    assert any("opus_7" in n for n in notes)
    assert doc["anthropic"]["opus"]["input"] == 5.0            # family card from its representative
    assert doc["anthropic"]["sonnet_legacy"]["input"] == 3.0
    assert doc["openai"]["gpt-8"] == {"input": 2.0, "output": 8.0, "cache_read": 2.0}
    assert doc["openai_long_context"]["gpt-8"]["input"] == 4.0
    assert "gpt-8-2026-01-01" not in doc["openai"] and "gpt-8-audio" not in doc["openai"]
    assert "gemini-8-flash" in doc["gemini"]


def test_refresh_builds_anthropic_long_context_cards():
    """The official page's "(for prompts over 100,000 tokens)" row and the
    LiteLLM *_above_100k_tokens fields both feed anthropic_long_context; the
    official page wins. The batch-pricing row (2 price cells) is ignored."""
    doc, notes = refresh_prices.build(FEED, OFFICIAL_MD)
    card = doc["anthropic_long_context"]["haiku_9_9"]
    assert card == {"input": 1.0, "output": 5.0, "cache_read": 0.10,
                    "cache_write": 1.25, "cache_write_1h": 2.0}
    assert any("long-context" in n for n in notes)
    assert doc["thresholds"]["anthropic_long_context_input"] == 100_000
    # The base card came from the "up to 100,000" row, not the LiteLLM feed.
    assert doc["anthropic"]["haiku_9_9"]["input"] == 0.20


def test_refresh_litellm_only_long_context_when_official_missing():
    doc, _ = refresh_prices.build(FEED, None)
    card = doc["anthropic_long_context"]["haiku_9_9"]
    assert card["input"] == pytest.approx(0.5)
    assert card["output"] == pytest.approx(2.5)
    assert card["cache_write_1h"] == pytest.approx(1.0)


def test_refresh_refuses_a_gutted_table():
    doc, _ = refresh_prices.build({}, None)
    assert refresh_prices.validate(doc)


def test_refresh_guard_trips_beyond_2x_or_on_removal():
    old = {"anthropic": {"opus": {"input": 5.0, "output": 25.0}},
           "openai": {"gpt-8": {"input": 2.0, "output": 8.0}}, "gemini": {}}
    fine = {"anthropic": {"opus": {"input": 4.0, "output": 20.0}},
            "openai": {"gpt-8": {"input": 2.0, "output": 8.0}, "gpt-9": {"input": 1.0, "output": 1.0}}, "gemini": {}}
    assert refresh_prices.guard(old, fine) == []
    jumped = copy.deepcopy(fine)
    jumped["anthropic"]["opus"]["output"] = 60.0
    assert refresh_prices.guard(old, jumped)
    removed = copy.deepcopy(fine)
    del removed["openai"]["gpt-8"]
    assert refresh_prices.guard(old, removed)


def test_refresh_guard_trips_on_zeroed_or_dropped_rates_and_long_context():
    old = {"anthropic": {}, "gemini": {},
           "openai": {"gpt-8": {"input": 2.0, "output": 8.0, "cache_read": 0.2}},
           "openai_long_context": {"gpt-8": {"input": 4.0, "output": 12.0}}}
    zeroed = copy.deepcopy(old)
    zeroed["openai"]["gpt-8"]["output"] = 0.0
    assert refresh_prices.guard(old, zeroed)
    dropped = copy.deepcopy(old)
    del dropped["openai"]["gpt-8"]["cache_read"]
    assert refresh_prices.guard(old, dropped)
    long_jump = copy.deepcopy(old)
    long_jump["openai_long_context"]["gpt-8"]["input"] = 40.0
    assert refresh_prices.guard(old, long_jump)
    long_gone = copy.deepcopy(old)
    long_gone["openai_long_context"] = {}
    assert refresh_prices.guard(old, long_gone)


def test_loader_leaves_the_promo_card_to_its_date_switch(tmp_path, restore_tables):
    before = dict(measure.OPENAI_MODEL_PRICING["gpt-5.6-sol"])
    path = _write(tmp_path, _doc(openai={"gpt-5.6-sol": {"input": 99.0, "output": 99.0}}))
    assert measure._apply_bundled_prices(path) is True
    assert measure.OPENAI_MODEL_PRICING["gpt-5.6-sol"] == before


def test_refresh_drops_an_absurd_card_instead_of_failing():
    feed = dict(FEED)
    feed["gpt-9-huge"] = {"litellm_provider": "openai", "mode": "chat",
                          "input_cost_per_token": 0.05, "output_cost_per_token": 1e-6}
    doc, notes = refresh_prices.build(feed, OFFICIAL_MD)
    assert "gpt-9-huge" not in doc["openai"] and "gpt-8" in doc["openai"]
    assert any("gpt-9-huge" in n for n in notes)
    assert not [e for e in refresh_prices.validate(doc) if "out of range" in e]


def test_refresh_keeps_retired_claude_cards():
    doc, _ = refresh_prices.build(FEED, OFFICIAL_MD)
    assert doc["anthropic"]["opus_3"]["input"] == 15.0
    assert doc["anthropic"]["haiku_3"]["output"] == 1.25


@pytest.mark.parametrize("model_id, key", [
    ("claude-3-5-sonnet-20241022", "sonnet_legacy"),
    ("claude-3-7-sonnet-20250219", "sonnet_legacy"),
    ("claude-3-opus-20240229", "opus_3"),
    ("claude-3-5-haiku-20241022", "haiku_3_5"),
    ("claude-3-haiku-20240307", "haiku_3"),
    ("claude-sonnet-4-20250514", "sonnet_4"),
])
def test_claude_3_era_ids_price_as_their_own_model(model_id, key):
    cards = measure.PRICING_TIERS["anthropic"]["claude_models"]
    assert measure._claude_price_key(model_id, cards) == key
    assert measure._claude_price_key(123, cards) is None


def test_fleet_prices_claude_3_era_ids_as_their_own_model():
    sys.path.insert(0, str(REPO / "skills" / "fleet-auditor" / "scripts"))
    import fleet
    assert fleet._pricing_key("claude-3-5-sonnet-20241022") == "sonnet-legacy"
    assert fleet._pricing_key("claude-3-opus-20240229") == "opus-3"
    assert fleet._pricing_key("claude-3-5-haiku-20241022") == "haiku-3-5"


def test_transcript_turns_apply_the_haiku_5_5_tier_per_request(tmp_path):
    """The one place a request's own prompt length is known: each API call in
    the transcript pays the tier for ITS prompt, not for the session total."""
    import json
    base = measure.PRICING_TIERS["anthropic"]["claude_models"]["haiku_5_5"]
    lc = measure.PRICING_TIERS["anthropic"]["claude_models_lc"]["haiku_5_5"]

    def rec(i, cache_read):
        return {"type": "assistant", "timestamp": f"2026-10-10T10:0{i}:00Z",
                "message": {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 1000, "output_tokens": 500,
                                      "cache_read_input_tokens": cache_read,
                                      "cache_creation_input_tokens": 0}}}
    p = tmp_path / "s.jsonl"
    # 3 small requests (21K prompt) and 1 big request (121K prompt).
    p.write_text("\n".join(json.dumps(rec(i, c)) for i, c in
                          enumerate([20_000, 20_000, 20_000, 120_000])), encoding="utf-8")
    turns = measure.parse_session_turns(str(p))
    costs = [t["cost_usd"] for t in turns]
    small = (1000 * base["input"] + 500 * base["output"] + 20_000 * base["cache_read"]) / 1e6
    big = (1000 * lc["input"] + 500 * lc["output"] + 120_000 * lc["cache_read"]) / 1e6
    assert costs[:3] == [pytest.approx(round(small, 6))] * 3
    assert costs[3] == pytest.approx(round(big, 6))
