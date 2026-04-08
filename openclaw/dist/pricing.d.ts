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
    cacheWrite: number;
}
/** Default pricing (USD per token). Verified March 17, 2026. */
export declare const DEFAULT_PRICING: Record<string, ModelPricing>;
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
 * Estimate cost delta if a different model was used.
 * Returns savings in USD and percentage.
 */
export declare function simulateModelSwitch(tokens: TokenBreakdown, currentModel: string, targetModel: string, openclawDir?: string): {
    currentCost: number;
    targetCost: number;
    savingsUsd: number;
    savingsPct: number;
};
/** Calculate USD cost. Uses user config pricing if available, then defaults. */
export declare function calculateCost(tokens: TokenBreakdown, model: string, openclawDir?: string): number;
//# sourceMappingURL=pricing.d.ts.map