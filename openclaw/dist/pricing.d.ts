import { TokenBreakdown } from "./models";
export type PricingTier = "anthropic" | "vertex-global" | "vertex-regional" | "bedrock";
/** Pricing tier labels for display. */
export declare const PRICING_TIER_LABELS: Record<PricingTier, string>;
/**
 * Get the cost multiplier for a pricing tier.
 * Only vertex-regional charges differently (10% surcharge on Claude models).
 * All other tiers use base Anthropic rates.
 */
export declare function tierMultiplier(tier: PricingTier, model: string): number;
/** Load the user's selected pricing tier from config. Defaults to "anthropic". */
export declare function loadPricingTier(openclawDir?: string): PricingTier;
export interface ModelPricing {
    input: number;
    output: number;
    cacheRead: number;
    /** 5-minute cache-write rate (1.25x input). Use for 5m-TTL writes or when TTL is unknown. */
    cacheWrite: number;
    /** 1-hour cache-write rate (2x input). Only set for Claude models that support the 1h tier. */
    cacheWrite1h?: number;
}
export interface CacheWriteSplit {
    cacheWrite1hTokens?: number;
    cacheWrite5mTokens?: number;
}
/** Default pricing (USD per token). Verified May 30, 2026. */
export declare const DEFAULT_PRICING: Record<string, ModelPricing>;
/** Swap the sonnet card to the introductory rate while it is in effect (idempotent). Returns
 * true when introductory pricing is active. `asOf` (epoch ms) overrides both env and clock. */
export declare function applySonnetIntroPricing(asOf?: number): boolean;
/**
 * Rate-card key for a Claude generation ("opus-5-5", then "opus-5"). Labels and
 * savings mixes stay on the family key ("opus"); only pricing needs the
 * generation, because generations are priced differently (Opus 5.5 $4/$20,
 * Opus 4.1 $15/$75, Fable 5.1 cache reads $0.25). Cards come from the
 * auto-refreshed prices.generated.ts. Mirrors measure.py `_claude_price_key`.
 */
export declare function claudePricingKey(modelId: string, table?: Record<string, ModelPricing>): string | null;
/**
 * Longest priced OpenAI/Gemini id that the model id equals or extends, so a
 * dated snapshot ("gpt-5.4-mini-2026-03-05") prices as its base model and a new
 * model is priced the day the generated table has it.
 */
export declare function pricedPrefixKey(modelId: string, table?: Record<string, ModelPricing>): string | null;
/** Get pricing with user overrides merged on top of defaults. */
export declare function getPricing(openclawDir?: string): Record<string, ModelPricing>;
/** Reset cached pricing (for testing or config reload). */
export declare function resetPricingCache(): void;
/**
 * Normalize a model ID into a pricing key.
 * Handles provider prefixes (anthropic/claude-sonnet-4-6 -> sonnet)
 * and version suffixes (gpt-5.2-2026-03 -> gpt-5.2).
 */
export declare function normalizeModelName(modelId: string): string | null;
/**
 * Rate-card key that values a model in the routing comparison. Every Claude
 * generation of a family is valued at that family's newest generation card, in
 * both the baseline and the current arm, so a vendor price change between
 * generations cannot register as a routing saving. Other models stand for
 * themselves. The exact-card cost is reported separately as the generation gap.
 */
export declare function referenceCardKey(key: string, dir?: string): string;
/**
 * Drop the routing wrapper around a model id: gateway or provider path
 * segments ("openrouter/anthropic/", "github-copilot/", "amazon-bedrock/"),
 * dotted region and provider chains ("us.anthropic.", "global.anthropic."), and
 * a colon-separated provider tag. Dots inside a version ("claude-3.5-sonnet",
 * "gpt-4.1") are never cut.
 */
export declare function stripRoutePrefixes(modelId: string): string;
/**
 * A Claude model id without the decorations a route adds around it: the
 * provider and region prefix, a bedrock "-v1:0" version tag, a "[1m]" context
 * marker, a "-thinking" mode suffix, a "-latest" alias and a vertex "@date" snapshot. What is left
 * names the generation, which must still have its own rate card.
 */
export declare function stripClaudeRouteDecorations(modelId: string): string;
/**
 * Estimate cost delta if a different model was used.
 * Returns savings in USD and percentage.
 */
export declare function simulateModelSwitch(tokens: TokenBreakdown, currentModel: string, targetModel: string, openclawDir?: string): {
    currentCost: number;
    targetCost: number;
    savingsUsd: number;
    savingsPct: number;
};
/** Calculate USD cost. Uses user config pricing if available, then defaults.
 *
 * For Claude models pass cacheWriteSplit to apply
 * the correct per-TTL-tier rate (1h = 2x input; 5m = 1.25x input). When
 * the split is unavailable, or when a remainder is unsplit, those tokens use
 * the 5m rate.
 */
export declare function calculateCost(tokens: TokenBreakdown, model: string, openclawDir?: string, cacheWriteSplit?: CacheWriteSplit): number;
//# sourceMappingURL=pricing.d.ts.map