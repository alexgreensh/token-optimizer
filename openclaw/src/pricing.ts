import { TokenBreakdown } from "./models";

export interface ModelPricing {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
}

/** USD per token, indexed by normalized model name */
export const DEFAULT_PRICING: Record<string, ModelPricing> = {
  opus: {
    input: 15.0 / 1e6,
    output: 75.0 / 1e6,
    cacheRead: 1.5 / 1e6,
    cacheWrite: 18.75 / 1e6,
  },
  sonnet: {
    input: 3.0 / 1e6,
    output: 15.0 / 1e6,
    cacheRead: 0.3 / 1e6,
    cacheWrite: 3.75 / 1e6,
  },
  haiku: {
    input: 0.25 / 1e6,
    output: 1.25 / 1e6,
    cacheRead: 0.025 / 1e6,
    cacheWrite: 0.3125 / 1e6,
  },
  "gpt-4o": {
    input: 2.5 / 1e6,
    output: 10.0 / 1e6,
    cacheRead: 1.25 / 1e6,
    cacheWrite: 0,
  },
  "gpt-4o-mini": {
    input: 0.15 / 1e6,
    output: 0.6 / 1e6,
    cacheRead: 0,
    cacheWrite: 0,
  },
  "gemini-2.5-pro": {
    input: 1.25 / 1e6,
    output: 10.0 / 1e6,
    cacheRead: 0.315 / 1e6,
    cacheWrite: 0,
  },
};

/**
 * Normalize a model ID string into a pricing key.
 *
 * "claude-sonnet-4-6" -> "sonnet"
 * "gpt-4o-2024-08-06" -> "gpt-4o"
 * Returns the raw ID if no known pattern matches.
 */
export function normalizeModelName(modelId: string): string | null {
  if (!modelId || modelId.startsWith("<")) return null;

  const m = modelId.toLowerCase();
  if (m.includes("opus")) return "opus";
  if (m.includes("sonnet")) return "sonnet";
  if (m.includes("haiku")) return "haiku";
  if (m.includes("gpt-4o-mini")) return "gpt-4o-mini";
  if (m.includes("gpt-4o")) return "gpt-4o";
  if (m.includes("gemini") && m.includes("pro")) return "gemini-2.5-pro";

  return modelId;
}

/** Calculate USD cost for a token breakdown. Falls back to sonnet rates. */
export function calculateCost(tokens: TokenBreakdown, model: string): number {
  const rates = DEFAULT_PRICING[model] ?? DEFAULT_PRICING["sonnet"];
  return (
    tokens.input * rates.input +
    tokens.output * rates.output +
    tokens.cacheRead * rates.cacheRead +
    tokens.cacheWrite * rates.cacheWrite
  );
}
