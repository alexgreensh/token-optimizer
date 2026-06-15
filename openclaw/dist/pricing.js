"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.DEFAULT_PRICING = exports.PRICING_TIER_LABELS = void 0;
exports.tierMultiplier = tierMultiplier;
exports.loadPricingTier = loadPricingTier;
exports.getPricing = getPricing;
exports.resetPricingCache = resetPricingCache;
exports.normalizeModelName = normalizeModelName;
exports.simulateModelSwitch = simulateModelSwitch;
exports.calculateCost = calculateCost;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
/** Pricing tier labels for display. */
exports.PRICING_TIER_LABELS = {
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
function tierMultiplier(tier, model) {
    if (tier !== "vertex-regional")
        return 1;
    if (model === "fable" || model === "opus" || model === "sonnet" || model === "haiku")
        return 1.1;
    return 1;
}
const HOME_DIR = process.env.HOME ?? process.env.USERPROFILE ?? "";
const TIER_CONFIG_DIR = path.join(HOME_DIR, ".openclaw", "token-optimizer");
const TIER_CONFIG_PATH = path.join(TIER_CONFIG_DIR, "config.json");
/** Load the user's selected pricing tier from config. Defaults to "anthropic". */
function loadPricingTier(openclawDir) {
    const configPath = openclawDir
        ? path.join(openclawDir, "token-optimizer", "config.json")
        : TIER_CONFIG_PATH;
    try {
        if (!fs.existsSync(configPath))
            return "anthropic";
        const data = JSON.parse(fs.readFileSync(configPath, "utf-8"));
        const tier = data?.pricingTier;
        if (tier && tier in exports.PRICING_TIER_LABELS)
            return tier;
        return "anthropic";
    }
    catch {
        return "anthropic";
    }
}
/** Default pricing (USD per token). Verified May 30, 2026. */
exports.DEFAULT_PRICING = {
    // Anthropic Claude (1M context for Fable/Opus/Sonnet as of March 13, 2026)
    // cacheWrite = 5m-TTL (1.25x input); cacheWrite1h = 1h-TTL (2x input).
    fable: { input: 10.0 / 1e6, output: 50.0 / 1e6, cacheRead: 1.0 / 1e6, cacheWrite: 12.5 / 1e6, cacheWrite1h: 20.0 / 1e6 },
    opus: { input: 5.0 / 1e6, output: 25.0 / 1e6, cacheRead: 0.5 / 1e6, cacheWrite: 6.25 / 1e6, cacheWrite1h: 10.0 / 1e6 },
    sonnet: { input: 3.0 / 1e6, output: 15.0 / 1e6, cacheRead: 0.3 / 1e6, cacheWrite: 3.75 / 1e6, cacheWrite1h: 6.0 / 1e6 },
    haiku: { input: 1.0 / 1e6, output: 5.0 / 1e6, cacheRead: 0.1 / 1e6, cacheWrite: 1.25 / 1e6, cacheWrite1h: 2.0 / 1e6 },
    // OpenAI GPT-5 family
    "gpt-5.5-pro": { input: 30.0 / 1e6, output: 180.0 / 1e6, cacheRead: 30.0 / 1e6, cacheWrite: 0 }, // cache N/A per OpenAI; billed at full input rate
    "gpt-5.5": { input: 5.0 / 1e6, output: 30.0 / 1e6, cacheRead: 0.50 / 1e6, cacheWrite: 0 },
    "gpt-5.4": { input: 2.5 / 1e6, output: 15.0 / 1e6, cacheRead: 0.25 / 1e6, cacheWrite: 0 },
    "gpt-5.4-mini": { input: 0.75 / 1e6, output: 4.5 / 1e6, cacheRead: 0.075 / 1e6, cacheWrite: 0 },
    "gpt-5.4-nano": { input: 0.20 / 1e6, output: 1.25 / 1e6, cacheRead: 0.02 / 1e6, cacheWrite: 0 },
    "gpt-5.3-codex": { input: 1.75 / 1e6, output: 14.0 / 1e6, cacheRead: 0.175 / 1e6, cacheWrite: 0 },
    "gpt-5.2-codex": { input: 1.75 / 1e6, output: 14.0 / 1e6, cacheRead: 0.175 / 1e6, cacheWrite: 0 },
    "gpt-5.2": { input: 1.75 / 1e6, output: 14.0 / 1e6, cacheRead: 0.175 / 1e6, cacheWrite: 0 },
    "gpt-5.1-codex-mini": { input: 0.25 / 1e6, output: 2.0 / 1e6, cacheRead: 0.025 / 1e6, cacheWrite: 0 },
    "gpt-5.1-codex": { input: 1.25 / 1e6, output: 10.0 / 1e6, cacheRead: 0.125 / 1e6, cacheWrite: 0 },
    "gpt-5.1": { input: 1.25 / 1e6, output: 10.0 / 1e6, cacheRead: 0.125 / 1e6, cacheWrite: 0 },
    "gpt-5-codex": { input: 1.25 / 1e6, output: 10.0 / 1e6, cacheRead: 0.125 / 1e6, cacheWrite: 0 },
    "gpt-5": { input: 1.25 / 1e6, output: 10.0 / 1e6, cacheRead: 0.125 / 1e6, cacheWrite: 0 },
    "gpt-5-mini": { input: 0.25 / 1e6, output: 2.0 / 1e6, cacheRead: 0.025 / 1e6, cacheWrite: 0 },
    "gpt-5-nano": { input: 0.05 / 1e6, output: 0.4 / 1e6, cacheRead: 0.005 / 1e6, cacheWrite: 0 },
    // OpenAI GPT-4 family
    "gpt-4.1": { input: 2.0 / 1e6, output: 8.0 / 1e6, cacheRead: 0.5 / 1e6, cacheWrite: 0 },
    "gpt-4.1-mini": { input: 0.4 / 1e6, output: 1.6 / 1e6, cacheRead: 0.1 / 1e6, cacheWrite: 0 },
    "gpt-4.1-nano": { input: 0.1 / 1e6, output: 0.4 / 1e6, cacheRead: 0.025 / 1e6, cacheWrite: 0 },
    "gpt-4o": { input: 2.5 / 1e6, output: 10.0 / 1e6, cacheRead: 1.25 / 1e6, cacheWrite: 0 },
    "gpt-4o-mini": { input: 0.15 / 1e6, output: 0.6 / 1e6, cacheRead: 0.075 / 1e6, cacheWrite: 0 },
    // OpenAI reasoning (o3 is $2/$8, NOT $0.40/$1.60 which was batch pricing)
    "o3": { input: 2.0 / 1e6, output: 8.0 / 1e6, cacheRead: 0.5 / 1e6, cacheWrite: 0 },
    "o3-pro": { input: 20.0 / 1e6, output: 80.0 / 1e6, cacheRead: 5.0 / 1e6, cacheWrite: 0 },
    "o3-mini": { input: 1.1 / 1e6, output: 4.4 / 1e6, cacheRead: 0.55 / 1e6, cacheWrite: 0 },
    "o4-mini": { input: 1.10 / 1e6, output: 4.40 / 1e6, cacheRead: 0.275 / 1e6, cacheWrite: 0 },
    // Google Gemini
    "gemini-3.5-flash": { input: 1.5 / 1e6, output: 9.0 / 1e6, cacheRead: 0.15 / 1e6, cacheWrite: 0 },
    "gemini-3.1-pro-preview": { input: 2.0 / 1e6, output: 12.0 / 1e6, cacheRead: 0.20 / 1e6, cacheWrite: 0 },
    "gemini-3.1-flash-lite": { input: 0.25 / 1e6, output: 1.5 / 1e6, cacheRead: 0.025 / 1e6, cacheWrite: 0 },
    "gemini-3-pro": { input: 2.0 / 1e6, output: 12.0 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "gemini-3-flash": { input: 0.5 / 1e6, output: 3.0 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "gemini-3.1-pro": { input: 2.0 / 1e6, output: 12.0 / 1e6, cacheRead: 0.20 / 1e6, cacheWrite: 0 },
    "gemini-2.5-pro": { input: 1.25 / 1e6, output: 10.0 / 1e6, cacheRead: 0.125 / 1e6, cacheWrite: 0 },
    "gemini-2.5-flash": { input: 0.3 / 1e6, output: 2.5 / 1e6, cacheRead: 0.03 / 1e6, cacheWrite: 0 },
    "gemini-2.5-flash-lite": { input: 0.1 / 1e6, output: 0.4 / 1e6, cacheRead: 0.01 / 1e6, cacheWrite: 0 },
    "gemini-2.0-flash": { input: 0.1 / 1e6, output: 0.4 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "gemini-2.0-flash-lite": { input: 0.075 / 1e6, output: 0.3 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "gemini-flash-lite": { input: 0.1 / 1e6, output: 0.4 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // DeepSeek
    "deepseek-v3": { input: 0.28 / 1e6, output: 0.42 / 1e6, cacheRead: 0.028 / 1e6, cacheWrite: 0 },
    "deepseek-r1": { input: 0.55 / 1e6, output: 2.19 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // Alibaba Qwen
    "qwen3": { input: 0.30 / 1e6, output: 1.20 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "qwen3-mini": { input: 0.08 / 1e6, output: 0.32 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "qwen-coder": { input: 0.15 / 1e6, output: 0.60 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // Moonshot Kimi
    "kimi-k2.5": { input: 0.50 / 1e6, output: 2.00 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // MiniMax
    "minimax-2": { input: 0.30 / 1e6, output: 1.10 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // Zhipu GLM
    "glm-4.7": { input: 0.48 / 1e6, output: 0.96 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "glm-4.7-flash": { input: 0.04 / 1e6, output: 0.04 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // Xiaomi MiMo
    "mimo-flash": { input: 0.20 / 1e6, output: 0.40 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // Mistral (Large 3 pricing, not legacy Large 2)
    "mistral-large": { input: 0.5 / 1e6, output: 1.5 / 1e6, cacheRead: 0, cacheWrite: 0 },
    "mistral-small": { input: 0.10 / 1e6, output: 0.30 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // xAI Grok
    "grok-4": { input: 3.0 / 1e6, output: 15.0 / 1e6, cacheRead: 0, cacheWrite: 0 },
    // Local models (Ollama, free but track tokens)
    "local": { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};
/**
 * Load user-configured pricing from OpenClaw's config.
 * OpenClaw stores per-model pricing at models.providers.<provider>.models[].cost
 */
function loadUserPricing(openclawDir) {
    const configPath = path.join(openclawDir, "openclaw.json");
    if (!fs.existsSync(configPath))
        return {};
    try {
        const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
        const providers = config?.models?.providers;
        if (!providers || typeof providers !== "object")
            return {};
        const userPricing = {};
        for (const [, provider] of Object.entries(providers)) {
            const p = provider;
            const models = p.models;
            if (!Array.isArray(models))
                continue;
            for (const model of models) {
                const name = model.name;
                const cost = model.cost;
                if (!name || !cost)
                    continue;
                const normalized = normalizeModelName(name);
                if (!normalized)
                    continue;
                const base = exports.DEFAULT_PRICING[normalized] ?? {
                    input: 0,
                    output: 0,
                    cacheRead: 0,
                    cacheWrite: 0,
                };
                const parseCost = (value, fallback) => {
                    if (value === undefined || value === null)
                        return fallback;
                    const numeric = typeof value === "number" ? value : Number(value);
                    return Number.isFinite(numeric) && numeric >= 0 ? numeric / 1e6 : fallback;
                };
                const input = parseCost(cost.input, base.input);
                const output = parseCost(cost.output, base.output);
                const cacheRead = parseCost(cost.cacheRead, base.cacheRead);
                const cacheWrite = parseCost(cost.cacheWrite, base.cacheWrite);
                let cacheWrite1h = base.cacheWrite1h;
                if (cost.cacheWrite1h !== undefined) {
                    cacheWrite1h = parseCost(cost.cacheWrite1h, base.cacheWrite1h ?? cacheWrite);
                }
                else if (cost.input !== undefined) {
                    cacheWrite1h = input * 2;
                }
                else if (cost.cacheWrite !== undefined) {
                    cacheWrite1h = cacheWrite * 1.6;
                }
                userPricing[normalized] = {
                    input,
                    output,
                    cacheRead,
                    cacheWrite,
                    cacheWrite1h,
                };
            }
        }
        return userPricing;
    }
    catch {
        return {};
    }
}
let _mergedPricing = null;
/**
 * Memoizes {@link normalizeModelName}. Model-id strings repeat heavily across a
 * session's records and across sessions, so caching the result of the ~60-branch
 * match below turns repeat lookups into O(1). Keyed on the raw model id; values
 * may be null, so membership is tested with Map.has (not a falsy check) to
 * distinguish a cached null from a miss. Cleared by {@link resetPricingCache}.
 */
const _normalizeCache = new Map();
/** Get pricing with user overrides merged on top of defaults. */
function getPricing(openclawDir) {
    if (_mergedPricing)
        return _mergedPricing;
    const merged = { ...exports.DEFAULT_PRICING };
    if (openclawDir) {
        const userPricing = loadUserPricing(openclawDir);
        for (const [key, rates] of Object.entries(userPricing)) {
            if (key === "__proto__" || key === "constructor" || key === "prototype")
                continue;
            merged[key] = rates;
        }
    }
    _mergedPricing = merged;
    return merged;
}
/** Reset cached pricing (for testing or config reload). */
function resetPricingCache() {
    _mergedPricing = null;
    _normalizeCache.clear();
}
/**
 * Normalize a model ID into a pricing key.
 * Handles provider prefixes (anthropic/claude-sonnet-4-6 -> sonnet)
 * and version suffixes (gpt-5.2-2026-03 -> gpt-5.2).
 */
function normalizeModelName(modelId) {
    if (_normalizeCache.has(modelId)) {
        return _normalizeCache.get(modelId);
    }
    const result = computeNormalizedModelName(modelId);
    // Bound defensively: the distinct-model-id set is small and finite in practice,
    // but cap so a pathological stream of unique ids can't grow the cache unbounded.
    if (_normalizeCache.size < 4096)
        _normalizeCache.set(modelId, result);
    return result;
}
function computeNormalizedModelName(modelId) {
    if (!modelId || modelId.startsWith("<"))
        return null;
    // Strip one or more provider prefixes:
    // openai/gpt-4o, openrouter/openai/gpt-4o, anthropic:claude-sonnet-4-6.
    const m = stripProviderPrefixes(modelId);
    // Anthropic
    if (m.includes("fable"))
        return "fable";
    if (m.includes("opus"))
        return "opus";
    if (m.includes("sonnet"))
        return "sonnet";
    if (m.includes("haiku"))
        return "haiku";
    // OpenAI GPT-5 family (most-specific first to prevent prefix shadowing)
    if (m.includes("gpt-5.5-pro"))
        return "gpt-5.5-pro";
    if (m.includes("gpt-5.5"))
        return "gpt-5.5";
    if (m.includes("gpt-5.4") && m.includes("nano"))
        return "gpt-5.4-nano";
    if (m.includes("gpt-5.4") && m.includes("mini"))
        return "gpt-5.4-mini";
    if (m.includes("gpt-5.4"))
        return "gpt-5.4";
    if (m.includes("gpt-5.3") && m.includes("codex"))
        return "gpt-5.3-codex";
    if (m.includes("gpt-5.2") && m.includes("codex"))
        return "gpt-5.2-codex";
    if (m.includes("gpt-5.2"))
        return "gpt-5.2";
    if (m.includes("gpt-5.1") && m.includes("codex") && m.includes("mini"))
        return "gpt-5.1-codex-mini";
    if (m.includes("gpt-5.1") && m.includes("codex"))
        return "gpt-5.1-codex";
    if (m.includes("gpt-5.1"))
        return "gpt-5.1";
    if (m.includes("gpt-5") && m.includes("codex"))
        return "gpt-5-codex";
    if (m.includes("gpt-5") && m.includes("nano"))
        return "gpt-5-nano";
    if (m.includes("gpt-5") && m.includes("mini"))
        return "gpt-5-mini";
    if (m.includes("gpt-5"))
        return "gpt-5";
    // OpenAI GPT-4 family
    if (m.includes("gpt-4.1") && m.includes("nano"))
        return "gpt-4.1-nano";
    if (m.includes("gpt-4.1") && m.includes("mini"))
        return "gpt-4.1-mini";
    if (m.includes("gpt-4.1"))
        return "gpt-4.1";
    if (m.includes("gpt-4o-mini"))
        return "gpt-4o-mini";
    if (m.includes("gpt-4o"))
        return "gpt-4o";
    // OpenAI reasoning
    if (m.includes("o4-mini"))
        return "o4-mini";
    if (m.includes("o3-mini"))
        return "o3-mini";
    if (m.includes("o3-pro"))
        return "o3-pro";
    if (m === "o3" || m.startsWith("o3-"))
        return "o3";
    // Google Gemini (most-specific first to prevent prefix shadowing)
    if (m.includes("gemini") && m.includes("3.5") && m.includes("flash"))
        return "gemini-3.5-flash";
    if (m.includes("gemini") && m.includes("3.1") && m.includes("pro") && m.includes("preview"))
        return "gemini-3.1-pro-preview";
    if (m.includes("gemini") && m.includes("3.1") && m.includes("flash") && m.includes("lite"))
        return "gemini-3.1-flash-lite";
    if (m.includes("gemini") && m.includes("3.1") && m.includes("pro"))
        return "gemini-3.1-pro";
    if (m.includes("gemini") && m.includes("2.5") && m.includes("flash") && m.includes("lite"))
        return "gemini-2.5-flash-lite";
    if (m.includes("gemini") && m.includes("2.5") && m.includes("flash"))
        return "gemini-2.5-flash";
    if (m.includes("gemini") && m.includes("2.5") && m.includes("pro"))
        return "gemini-2.5-pro";
    if (m.includes("2.0") && m.includes("flash") && m.includes("lite"))
        return "gemini-2.0-flash-lite";
    if (m.includes("2.0") && m.includes("flash"))
        return "gemini-2.0-flash";
    if (m.includes("gemini-3") && m.includes("flash"))
        return "gemini-3-flash";
    if (m.includes("gemini-3") && m.includes("pro"))
        return "gemini-3-pro";
    if (m.includes("flash-lite") || m.includes("flash_lite"))
        return "gemini-flash-lite";
    // DeepSeek
    if (m.includes("deepseek") && (m.includes("r1") || m.includes("reasoner")))
        return "deepseek-r1";
    if (m.includes("deepseek") && (m.includes("v3") || m.includes("chat")))
        return "deepseek-v3";
    if (m.includes("deepseek"))
        return "deepseek-v3";
    // Alibaba Qwen
    if (m.includes("qwen") && m.includes("coder"))
        return "qwen-coder";
    if (m.includes("qwen3") && m.includes("mini"))
        return "qwen3-mini";
    if (m.includes("qwen3") || m.includes("qwen-3"))
        return "qwen3";
    if (m.includes("qwen"))
        return "qwen3";
    // Moonshot Kimi
    if (m.includes("kimi") || m.includes("moonshot"))
        return "kimi-k2.5";
    // MiniMax
    if (m.includes("minimax"))
        return "minimax-2";
    // Zhipu GLM
    if (m.includes("glm") && m.includes("flash"))
        return "glm-4.7-flash";
    if (m.includes("glm"))
        return "glm-4.7";
    // Xiaomi MiMo
    if (m.includes("mimo"))
        return "mimo-flash";
    // Mistral
    if (m.includes("mistral") && (m.includes("large") || m.includes("123")))
        return "mistral-large";
    if (m.includes("mistral") && m.includes("small"))
        return "mistral-small";
    if (m.includes("mistral"))
        return "mistral-large";
    // xAI Grok
    if (m.includes("grok"))
        return "grok-4";
    // Local models (Ollama, LM Studio, etc.)
    if (m.includes("ollama") || m.includes("local") || m.includes("lmstudio"))
        return "local";
    // Unknown model, return lowercased for consistent pricing lookup
    return m;
}
const KNOWN_PROVIDER_PREFIXES = new Set([
    "anthropic", "openai", "google", "gemini", "vertex", "bedrock",
    "openrouter", "gateway", "litellm", "azure", "aws",
]);
function stripProviderPrefixes(modelId) {
    let value = modelId.trim().toLowerCase();
    while (true) {
        const slash = value.indexOf("/");
        const colon = value.indexOf(":");
        if (slash === -1 && colon === -1)
            return value;
        const useSlash = slash !== -1 && (colon === -1 || slash < colon);
        const idx = useSlash ? slash : colon;
        const delimiter = useSlash ? "/" : ":";
        const prefix = value.slice(0, idx);
        const rest = value.slice(idx + 1);
        if (!rest || !/[a-z]/.test(rest))
            return value;
        if (delimiter === "/" || KNOWN_PROVIDER_PREFIXES.has(prefix)) {
            value = rest;
            continue;
        }
        return value;
    }
}
/**
 * Estimate cost delta if a different model was used.
 * Returns savings in USD and percentage.
 */
function simulateModelSwitch(tokens, currentModel, targetModel, openclawDir) {
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
/** Calculate USD cost. Uses user config pricing if available, then defaults.
 *
 * For Claude models pass cacheWriteSplit to apply
 * the correct per-TTL-tier rate (1h = 2x input; 5m = 1.25x input). When
 * the split is unavailable, or when a remainder is unsplit, those tokens use
 * the 5m rate.
 */
function calculateCost(tokens, model, openclawDir, cacheWriteSplit) {
    const pricing = getPricing(openclawDir);
    const pricingKey = pricing[model] ? model : (normalizeModelName(model) ?? model);
    const rates = pricing[pricingKey];
    // Unknown model with no user-configured pricing: return 0 (show tokens only)
    if (!rates)
        return 0;
    const multiplier = tierMultiplier(loadPricingTier(openclawDir), pricingKey);
    let cacheWriteCost;
    let split1h = Math.max(0, cacheWriteSplit?.cacheWrite1hTokens ?? 0);
    let split5m = Math.max(0, cacheWriteSplit?.cacheWrite5mTokens ?? 0);
    if (split1h || split5m) {
        // F3: a TTL split that exceeds the total cacheWrite (inconsistent upstream
        // parse / hand-edited history) would over-price — e.g. cw1h=50k on a 30k total
        // bills 50k tokens, a 67% overcharge. Scale the buckets down proportionally so
        // split1h + split5m never exceeds tokens.cacheWrite (mirrors measure.py's
        // apportioning, which keeps the class split <= the billed total).
        const cw = Math.max(0, tokens.cacheWrite);
        const splitSum = split1h + split5m;
        if (splitSum > cw && splitSum > 0) {
            const scale = cw / splitSum;
            split1h *= scale;
            split5m *= scale;
        }
        const rate1h = rates.cacheWrite1h ?? rates.cacheWrite;
        const unsplit = Math.max(0, cw - split1h - split5m);
        cacheWriteCost =
            split1h * rate1h +
                (split5m + unsplit) * rates.cacheWrite;
    }
    else {
        cacheWriteCost = tokens.cacheWrite * rates.cacheWrite;
    }
    return (tokens.input * rates.input +
        tokens.output * rates.output +
        tokens.cacheRead * rates.cacheRead +
        cacheWriteCost) * multiplier;
}
//# sourceMappingURL=pricing.js.map