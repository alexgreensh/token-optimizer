import * as fs from "fs";
import * as path from "path";
import { TokenBreakdown } from "./models";

// ---------------------------------------------------------------------------
// Pricing tier types and constants
// ---------------------------------------------------------------------------

export type PricingTier = "anthropic" | "vertex-global" | "vertex-regional" | "bedrock";

/** Pricing tier labels for display. */
export const PRICING_TIER_LABELS: Record<PricingTier, string> = {
  anthropic: "Anthropic API (direct)",
  "vertex-global": "Google Vertex AI (global)",
  "vertex-regional": "Google Vertex AI (regional, +10%)",
  bedrock: "AWS Bedrock",
};

/**
 * Get the cost multiplier for a pricing tier.
 * Only vertex-regional charges differently (10% surcharge on Claude models).
 * All other tiers use base Anthropic rates.
 */
export function tierMultiplier(tier: PricingTier, model: string): number {
  if (tier !== "vertex-regional") return 1;
  if (model === "opus" || model === "sonnet" || model === "haiku") return 1.1;
  return 1;
}

const HOME_DIR = process.env.HOME ?? process.env.USERPROFILE ?? "";
const TIER_CONFIG_DIR = path.join(HOME_DIR, ".openclaw", "token-optimizer");
const TIER_CONFIG_PATH = path.join(TIER_CONFIG_DIR, "config.json");

/** Load the user's selected pricing tier from config. Defaults to "anthropic". */
export function loadPricingTier(openclawDir?: string): PricingTier {
  const configPath = openclawDir
    ? path.join(openclawDir, "token-optimizer", "config.json")
    : TIER_CONFIG_PATH;

  try {
    if (!fs.existsSync(configPath)) return "anthropic";
    const data = JSON.parse(fs.readFileSync(configPath, "utf-8"));
    const tier = data?.pricingTier as string | undefined;
    if (tier && tier in PRICING_TIER_LABELS) return tier as PricingTier;
    return "anthropic";
  } catch {
    return "anthropic";
  }
}

// ---------------------------------------------------------------------------
// Model pricing
// ---------------------------------------------------------------------------

export interface ModelPricing {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
}

/** Default pricing (USD per token). Verified March 17, 2026. */
export const DEFAULT_PRICING: Record<string, ModelPricing> = {
  // Anthropic Claude (1M context for Opus/Sonnet as of March 13, 2026)
  opus:            { input: 5.0 / 1e6,   output: 25.0 / 1e6,  cacheRead: 0.5 / 1e6,   cacheWrite: 6.25 / 1e6 },
  sonnet:          { input: 3.0 / 1e6,   output: 15.0 / 1e6,  cacheRead: 0.3 / 1e6,   cacheWrite: 3.75 / 1e6 },
  haiku:           { input: 1.0 / 1e6,   output: 5.0 / 1e6,   cacheRead: 0.1 / 1e6,   cacheWrite: 1.25 / 1e6 },
  // OpenAI GPT-5 family
  "gpt-5.4":       { input: 2.5 / 1e6,   output: 15.0 / 1e6,  cacheRead: 0.25 / 1e6,  cacheWrite: 0 },
  "gpt-5.2":       { input: 1.75 / 1e6,  output: 14.0 / 1e6,  cacheRead: 0.175 / 1e6, cacheWrite: 0 },
  "gpt-5.1":       { input: 1.25 / 1e6,  output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gpt-5":         { input: 1.25 / 1e6,  output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gpt-5-mini":    { input: 0.25 / 1e6,  output: 2.0 / 1e6,   cacheRead: 0.025 / 1e6, cacheWrite: 0 },
  "gpt-5-nano":    { input: 0.05 / 1e6,  output: 0.4 / 1e6,   cacheRead: 0.005 / 1e6, cacheWrite: 0 },
  // OpenAI GPT-4 family
  "gpt-4.1":       { input: 2.0 / 1e6,   output: 8.0 / 1e6,   cacheRead: 0.5 / 1e6,   cacheWrite: 0 },
  "gpt-4.1-mini":  { input: 0.4 / 1e6,   output: 1.6 / 1e6,   cacheRead: 0.1 / 1e6,   cacheWrite: 0 },
  "gpt-4.1-nano":  { input: 0.1 / 1e6,   output: 0.4 / 1e6,   cacheRead: 0.025 / 1e6, cacheWrite: 0 },
  "gpt-4o":        { input: 2.5 / 1e6,   output: 10.0 / 1e6,  cacheRead: 1.25 / 1e6,  cacheWrite: 0 },
  "gpt-4o-mini":   { input: 0.15 / 1e6,  output: 0.6 / 1e6,   cacheRead: 0.075 / 1e6, cacheWrite: 0 },
  // OpenAI reasoning (o3 is $2/$8, NOT $0.40/$1.60 which was batch pricing)
  "o3":            { input: 2.0 / 1e6,   output: 8.0 / 1e6,   cacheRead: 0.5 / 1e6,   cacheWrite: 0 },
  "o3-pro":        { input: 20.0 / 1e6,  output: 80.0 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "o3-mini":       { input: 1.1 / 1e6,   output: 4.4 / 1e6,   cacheRead: 0,           cacheWrite: 0 },
  "o4-mini":       { input: 1.1 / 1e6,   output: 4.4 / 1e6,   cacheRead: 0,           cacheWrite: 0 },
  // Google Gemini
  "gemini-3-pro":  { input: 2.0 / 1e6,   output: 12.0 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "gemini-3-flash": { input: 0.5 / 1e6,  output: 3.0 / 1e6,   cacheRead: 0,           cacheWrite: 0 },
  "gemini-3.1-pro": { input: 2.0 / 1e6,  output: 12.0 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "gemini-2.5-pro": { input: 1.25 / 1e6, output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gemini-2.5-flash": { input: 0.3 / 1e6, output: 2.5 / 1e6,  cacheRead: 0.03 / 1e6,  cacheWrite: 0 },
  "gemini-2.0-flash": { input: 0.1 / 1e6, output: 0.4 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "gemini-2.0-flash-lite": { input: 0.075 / 1e6, output: 0.3 / 1e6, cacheRead: 0,     cacheWrite: 0 },
  "gemini-flash-lite": { input: 0.1 / 1e6, output: 0.4 / 1e6, cacheRead: 0,           cacheWrite: 0 },
  // DeepSeek
  "deepseek-v3":   { input: 0.28 / 1e6,  output: 0.42 / 1e6,  cacheRead: 0.028 / 1e6, cacheWrite: 0 },
  "deepseek-r1":   { input: 0.55 / 1e6,  output: 2.19 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Alibaba Qwen
  "qwen3":         { input: 0.30 / 1e6,  output: 1.20 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "qwen3-mini":    { input: 0.08 / 1e6,  output: 0.32 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "qwen-coder":    { input: 0.15 / 1e6,  output: 0.60 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Moonshot Kimi
  "kimi-k2.5":     { input: 0.50 / 1e6,  output: 2.00 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // MiniMax
  "minimax-2":     { input: 0.30 / 1e6,  output: 1.10 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Zhipu GLM
  "glm-4.7":       { input: 0.48 / 1e6,  output: 0.96 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  "glm-4.7-flash": { input: 0.04 / 1e6,  output: 0.04 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Xiaomi MiMo
  "mimo-flash":    { input: 0.20 / 1e6,  output: 0.40 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Mistral (Large 3 pricing, not legacy Large 2)
  "mistral-large":  { input: 0.5 / 1e6,  output: 1.5 / 1e6,   cacheRead: 0,           cacheWrite: 0 },
  "mistral-small":  { input: 0.10 / 1e6, output: 0.30 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // xAI Grok
  "grok-4":        { input: 3.0 / 1e6,   output: 15.0 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Local models (Ollama, free but track tokens)
  "local":         { input: 0,           output: 0,            cacheRead: 0,           cacheWrite: 0 },
};

/**
 * Load user-configured pricing from OpenClaw's config.
 * OpenClaw stores per-model pricing at models.providers.<provider>.models[].cost
 */
function loadUserPricing(openclawDir: string): Record<string, ModelPricing> {
  const configPath = path.join(openclawDir, "openclaw.json");
  if (!fs.existsSync(configPath)) return {};

  try {
    const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
    const providers = config?.models?.providers;
    if (!providers || typeof providers !== "object") return {};

    const userPricing: Record<string, ModelPricing> = {};

    for (const [, provider] of Object.entries(providers)) {
      const p = provider as Record<string, unknown>;
      const models = p.models as Array<Record<string, unknown>> | undefined;
      if (!Array.isArray(models)) continue;

      for (const model of models) {
        const name = model.name as string | undefined;
        const cost = model.cost as Record<string, number> | undefined;
        if (!name || !cost) continue;

        const normalized = normalizeModelName(name);
        if (!normalized) continue;

        userPricing[normalized] = {
          input: (cost.input ?? 0) / 1e6,
          output: (cost.output ?? 0) / 1e6,
          cacheRead: (cost.cacheRead ?? 0) / 1e6,
          cacheWrite: (cost.cacheWrite ?? 0) / 1e6,
        };
      }
    }

    return userPricing;
  } catch {
    return {};
  }
}

let _mergedPricing: Record<string, ModelPricing> | null = null;

/** Get pricing with user overrides merged on top of defaults. */
export function getPricing(openclawDir?: string): Record<string, ModelPricing> {
  if (_mergedPricing) return _mergedPricing;

  const merged = { ...DEFAULT_PRICING };

  if (openclawDir) {
    const userPricing = loadUserPricing(openclawDir);
    for (const [key, rates] of Object.entries(userPricing)) {
      if (key === "__proto__" || key === "constructor" || key === "prototype") continue;
      merged[key] = rates;
    }
  }

  _mergedPricing = merged;
  return merged;
}

/** Reset cached pricing (for testing or config reload). */
export function resetPricingCache(): void {
  _mergedPricing = null;
}

/**
 * Normalize a model ID into a pricing key.
 * Handles provider prefixes (anthropic/claude-sonnet-4-6 -> sonnet)
 * and version suffixes (gpt-5.2-2026-03 -> gpt-5.2).
 */
export function normalizeModelName(modelId: string): string | null {
  if (!modelId || modelId.startsWith("<")) return null;

  // Strip provider prefix (anthropic/, openai/, google/, deepseek/)
  const m = modelId.toLowerCase().replace(/^[a-z-]+\//, "");

  // Anthropic
  if (m.includes("opus")) return "opus";
  if (m.includes("sonnet")) return "sonnet";
  if (m.includes("haiku")) return "haiku";

  // OpenAI GPT-5 family (specific before general, order matters)
  if (m.includes("gpt-5") && m.includes("nano")) return "gpt-5-nano";
  if (m.includes("gpt-5") && m.includes("mini")) return "gpt-5-mini";
  if (m.includes("gpt-5.4")) return "gpt-5.4";
  if (m.includes("gpt-5.2")) return "gpt-5.2";
  if (m.includes("gpt-5.1")) return "gpt-5.1";
  if (m.includes("gpt-5")) return "gpt-5";

  // OpenAI GPT-4 family
  if (m.includes("gpt-4.1") && m.includes("nano")) return "gpt-4.1-nano";
  if (m.includes("gpt-4.1") && m.includes("mini")) return "gpt-4.1-mini";
  if (m.includes("gpt-4.1")) return "gpt-4.1";
  if (m.includes("gpt-4o-mini")) return "gpt-4o-mini";
  if (m.includes("gpt-4o")) return "gpt-4o";

  // OpenAI reasoning
  if (m.includes("o4-mini")) return "o4-mini";
  if (m.includes("o3-mini")) return "o3-mini";
  if (m.includes("o3-pro")) return "o3-pro";
  if (m === "o3" || m.startsWith("o3-")) return "o3";

  // Google Gemini (specific before general)
  if (m.includes("2.0") && m.includes("flash") && m.includes("lite")) return "gemini-2.0-flash-lite";
  if (m.includes("2.0") && m.includes("flash")) return "gemini-2.0-flash";
  if (m.includes("flash-lite") || m.includes("flash_lite")) return "gemini-flash-lite";
  if (m.includes("gemini") && m.includes("3.1") && m.includes("pro")) return "gemini-3.1-pro";
  if (m.includes("gemini") && m.includes("2.5") && m.includes("flash")) return "gemini-2.5-flash";
  if (m.includes("gemini") && m.includes("2.5") && m.includes("pro")) return "gemini-2.5-pro";
  if (m.includes("gemini-3") && m.includes("flash")) return "gemini-3-flash";
  if (m.includes("gemini-3") && m.includes("pro")) return "gemini-3-pro";

  // DeepSeek
  if (m.includes("deepseek") && (m.includes("r1") || m.includes("reasoner"))) return "deepseek-r1";
  if (m.includes("deepseek") && (m.includes("v3") || m.includes("chat"))) return "deepseek-v3";
  if (m.includes("deepseek")) return "deepseek-v3";

  // Alibaba Qwen
  if (m.includes("qwen") && m.includes("coder")) return "qwen-coder";
  if (m.includes("qwen3") && m.includes("mini")) return "qwen3-mini";
  if (m.includes("qwen3") || m.includes("qwen-3")) return "qwen3";
  if (m.includes("qwen")) return "qwen3";

  // Moonshot Kimi
  if (m.includes("kimi") || m.includes("moonshot")) return "kimi-k2.5";

  // MiniMax
  if (m.includes("minimax")) return "minimax-2";

  // Zhipu GLM
  if (m.includes("glm") && m.includes("flash")) return "glm-4.7-flash";
  if (m.includes("glm")) return "glm-4.7";

  // Xiaomi MiMo
  if (m.includes("mimo")) return "mimo-flash";

  // Mistral
  if (m.includes("mistral") && (m.includes("large") || m.includes("123"))) return "mistral-large";
  if (m.includes("mistral") && m.includes("small")) return "mistral-small";
  if (m.includes("mistral")) return "mistral-large";

  // xAI Grok
  if (m.includes("grok")) return "grok-4";

  // Local models (Ollama, LM Studio, etc.)
  if (m.includes("ollama") || m.includes("local") || m.includes("lmstudio")) return "local";

  // Unknown model, return lowercased for consistent pricing lookup
  return m;
}

/**
 * Estimate cost delta if a different model was used.
 * Returns savings in USD and percentage.
 */
export function simulateModelSwitch(
  tokens: TokenBreakdown,
  currentModel: string,
  targetModel: string,
  openclawDir?: string
): { currentCost: number; targetCost: number; savingsUsd: number; savingsPct: number } {
  const currentCost = calculateCost(tokens, currentModel, openclawDir);
  const targetCost = calculateCost(tokens, targetModel, openclawDir);
  const savingsUsd = Math.max(0, currentCost - targetCost);
  const savingsPct = currentCost > 0 ? (savingsUsd / currentCost) * 100 : 0;
  return {
    currentCost: Math.round(currentCost * 10000) / 10000,
    targetCost: Math.round(targetCost * 10000) / 10000,
    savingsUsd: Math.round(savingsUsd * 10000) / 10000,
    savingsPct: Math.round(savingsPct * 10) / 10,
  };
}

/** Calculate USD cost. Uses user config pricing if available, then defaults. */
export function calculateCost(
  tokens: TokenBreakdown,
  model: string,
  openclawDir?: string
): number {
  const pricing = getPricing(openclawDir);
  const rates = pricing[model];

  // Unknown model with no user-configured pricing: return 0 (show tokens only)
  if (!rates) return 0;

  return (
    tokens.input * rates.input +
    tokens.output * rates.output +
    tokens.cacheRead * rates.cacheRead +
    tokens.cacheWrite * rates.cacheWrite
  );
}
