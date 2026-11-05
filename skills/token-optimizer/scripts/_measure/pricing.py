"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _get_model_cost(model, input_tokens, output_tokens, cache_read=None, cache_create=None, tier=None,
                    cache_create_1h=None, cache_create_5m=None, per_request=None):
    """Calculate USD cost for a given model and token counts using the active pricing tier.

    Returns cost in USD. OpenAI/Codex and Gemini models use provider-specific
    rate cards; Claude models use the selected Claude provider tier.

    For Claude models, cache_create tokens are priced by TTL tier when the split is known:
      - cache_create_1h: 1-hour TTL writes (2x input rate, e.g. $10/MTok for Opus)
      - cache_create_5m: 5-minute TTL writes (1.25x input rate, e.g. $6.25/MTok for Opus)
    When the split is unavailable (both None), the total cache_create uses the 5m rate
    (conservative; 5m is the more common tier for most Claude Code workloads).

    per_request=True says the token counts are ONE API request. Only then does a
    model with a prompt-length card (Haiku 5.5 over 100K) pay it: the tier is
    decided per request, so a session total, a per-day sum or a 1M-token rate
    probe must be priced on the base card or it reads ~5x too high.
    """
    _measure_facade = _compat.facade()
    if tier is None:
        tier = _measure_facade._load_pricing_tier()
    tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])

    full_input = int(input_tokens or 0) + int(cache_read or 0) + int(cache_create or 0)

    for norm_fn, pricing, lc_pricing, lc_threshold in (
        (_measure_facade._normalize_openai_model_name, _measure_facade.OPENAI_MODEL_PRICING, _measure_facade.OPENAI_LONG_CONTEXT_PRICING, _measure_facade.OPENAI_LONG_CONTEXT_INPUT_THRESHOLD),
        (_measure_facade._normalize_gemini_model_name, _measure_facade.GEMINI_MODEL_PRICING, _measure_facade.GEMINI_LONG_CONTEXT_PRICING, _measure_facade.GEMINI_LONG_CONTEXT_INPUT_THRESHOLD),
    ):
        key = norm_fn(model)
        if key:
            rates = pricing[key]
            if full_input > lc_threshold and key in lc_pricing:
                rates = lc_pricing[key]
            return (
                input_tokens * rates["input"] / 1e6
                + output_tokens * rates["output"] / 1e6
                + cache_read * rates["cache_read"] / 1e6
                + cache_create * rates.get("cache_write", 0) / 1e6
            )

    normalized = _measure_facade._claude_price_key(model, tier_data["claude_models"]) if model else None
    if normalized and normalized in tier_data["claude_models"]:
        rates = tier_data["claude_models"][normalized]
        lc_models = tier_data.get("claude_models_lc") or {}
    else:
        # Non-Claude model: use Anthropic tier rates for Claude, skip for others
        rates = _measure_facade.PRICING_TIERS["anthropic"]["claude_models"].get(normalized or "", None)
        lc_models = _measure_facade.PRICING_TIERS["anthropic"].get("claude_models_lc") or {}
        if rates is None:
            return 0.0

    # Prompt-length surcharge: Anthropic counts ALL of a request's input
    # (input + cache reads + cache writes = full_input) against the
    # long-context threshold. Haiku 5.5 >100K pays 5x on every rate. Applies
    # to a single request only (per_request); aggregates use the base card.
    if (per_request and normalized in lc_models
            and full_input > _measure_facade.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD):
        rates = lc_models[normalized]

    # Price cache-write tokens by TTL tier when the split is available.
    # 1h tier = 2x input (cache_write_1h); 5m tier = 1.25x input (cache_write).
    # Fall back to 5m rate for the full total when no split is stored.
    if cache_create_1h is not None and cache_create_5m is not None:
        split_1h = int(cache_create_1h or 0)
        split_5m = int(cache_create_5m or 0)
        unsplit = max(0, int(cache_create or 0) - split_1h - split_5m)
        cc_write_cost = (
            split_1h * rates.get("cache_write_1h", rates["cache_write"]) / 1e6
            + (split_5m + unsplit) * rates["cache_write"] / 1e6
        )
    else:
        cc_write_cost = int(cache_create or 0) * rates["cache_write"] / 1e6

    cost = (
        input_tokens * rates["input"] / 1e6
        + output_tokens * rates["output"] / 1e6
        + cache_read * rates["cache_read"] / 1e6
        + cc_write_cost
    )
    return cost


def _bundled_prices_path():
    _measure_facade = _compat.facade()
    override = _measure_facade.os.environ.get("TOKEN_OPTIMIZER_PRICES_FILE", "").strip()
    if override:
        return _measure_facade.Path(override).expanduser()
    return _measure_facade.Path(_measure_facade.__file__).resolve().parent.parent / "pricing" / "prices.json"
