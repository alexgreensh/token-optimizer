/**
 * Minimal per-MTok rate card for OpenCode's realized-savings counterfactual.
 *
 * OpenCode persists a pre-computed `cost_usd` per session (priced at its own
 * model when recorded), so the ACTUAL arm needs no rate card. The
 * COUNTERFACTUAL arm, however, reprices the same token VOLUME at a DIFFERENT
 * (baseline) model mix, which requires a per-class rate card the stored cost
 * cannot supply. This module is that card.
 *
 * Rates mirror openclaw/src/pricing.ts exactly (USD per token; verified
 * May 30, 2026). cacheWrite = 5-minute-TTL rate (1.25x input); cacheWrite1h =
 * 1-hour-TTL rate (2x input), only set for Claude models that support it.
 *
 * A "mix" is a {modelKey -> share} map (shares sum to ~1). price() blends the
 * per-token cost across the mix; unpriced models fall back to a proxy rate.
 */

import { GENERATED_PRICING } from "./prices.generated.js";

export interface ModelPricing {
  input: number;
  output: number;
  cacheRead: number;
  /** 5-minute cache-write rate (1.25x input). Used when TTL is unknown. */
  cacheWrite: number;
  /** 1-hour cache-write rate (2x input). Only set for Claude models. */
  cacheWrite1h?: number;
}

/** Default pricing (USD per token). Mirrors openclaw/src/pricing.ts. */
export const DEFAULT_PRICING: Record<string, ModelPricing> = {
  // Anthropic Claude (context windows: see claudeContextWindow() in util/context-window.ts)
  fable:           { input: 10.0 / 1e6,  output: 50.0 / 1e6,  cacheRead: 1.0 / 1e6,   cacheWrite: 12.5 / 1e6, cacheWrite1h: 20.0 / 1e6 },
  opus:            { input: 5.0 / 1e6,   output: 25.0 / 1e6,  cacheRead: 0.5 / 1e6,   cacheWrite: 6.25 / 1e6, cacheWrite1h: 10.0 / 1e6 },
  // Generation cards priced off-family (platform.claude.com pricing, verified 2026-09-24):
  // Fable/Mythos 5.1 cache reads are 0.025x input; Opus 5.5 is $4/$20 with 0.05x reads.
  "fable-5-1":     { input: 10.0 / 1e6,  output: 50.0 / 1e6,  cacheRead: 0.25 / 1e6,  cacheWrite: 12.5 / 1e6, cacheWrite1h: 20.0 / 1e6 },
  "opus-5-5":      { input: 4.0 / 1e6,   output: 20.0 / 1e6,  cacheRead: 0.2 / 1e6,   cacheWrite: 5.0 / 1e6,  cacheWrite1h: 8.0 / 1e6 },
  sonnet:          { input: 3.0 / 1e6,   output: 15.0 / 1e6,  cacheRead: 0.3 / 1e6,   cacheWrite: 3.75 / 1e6, cacheWrite1h: 6.0 / 1e6 },
  haiku:           { input: 1.0 / 1e6,   output: 5.0 / 1e6,   cacheRead: 0.1 / 1e6,   cacheWrite: 1.25 / 1e6, cacheWrite1h: 2.0 / 1e6 },
  // OpenAI GPT-5 family
  "gpt-5.6-sol":   { input: 5.0 / 1e6,   output: 30.0 / 1e6,  cacheRead: 0.50 / 1e6,  cacheWrite: 6.25 / 1e6 },
  "gpt-5.6-terra": { input: 2.0 / 1e6,   output: 12.0 / 1e6,  cacheRead: 0.20 / 1e6,  cacheWrite: 2.50 / 1e6 },
  "gpt-5.6-luna":  { input: 0.20 / 1e6,  output: 1.20 / 1e6,  cacheRead: 0.02 / 1e6,  cacheWrite: 0.25 / 1e6 },
  "gpt-5.5-pro":   { input: 30.0 / 1e6,  output: 180.0 / 1e6, cacheRead: 30.0 / 1e6,  cacheWrite: 0 },
  "gpt-5.5":       { input: 5.0 / 1e6,   output: 30.0 / 1e6,  cacheRead: 0.50 / 1e6,  cacheWrite: 0 },
  "gpt-5.4":       { input: 2.5 / 1e6,   output: 15.0 / 1e6,  cacheRead: 0.25 / 1e6,  cacheWrite: 0 },
  "gpt-5.4-mini":  { input: 0.75 / 1e6,  output: 4.5 / 1e6,   cacheRead: 0.075 / 1e6, cacheWrite: 0 },
  "gpt-5.4-nano":  { input: 0.20 / 1e6,  output: 1.25 / 1e6,  cacheRead: 0.02 / 1e6,  cacheWrite: 0 },
  "gpt-5.3-codex": { input: 1.75 / 1e6,  output: 14.0 / 1e6,  cacheRead: 0.175 / 1e6, cacheWrite: 0 },
  "gpt-5.2-codex": { input: 1.75 / 1e6,  output: 14.0 / 1e6,  cacheRead: 0.175 / 1e6, cacheWrite: 0 },
  "gpt-5.2":       { input: 1.75 / 1e6,  output: 14.0 / 1e6,  cacheRead: 0.175 / 1e6, cacheWrite: 0 },
  "gpt-5.1-codex-mini": { input: 0.25 / 1e6, output: 2.0 / 1e6, cacheRead: 0.025 / 1e6, cacheWrite: 0 },
  "gpt-5.1-codex": { input: 1.25 / 1e6,  output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gpt-5.1":       { input: 1.25 / 1e6,  output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gpt-5-codex":   { input: 1.25 / 1e6,  output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gpt-5":         { input: 1.25 / 1e6,  output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gpt-5-mini":    { input: 0.25 / 1e6,  output: 2.0 / 1e6,   cacheRead: 0.025 / 1e6, cacheWrite: 0 },
  "gpt-5-nano":    { input: 0.05 / 1e6,  output: 0.4 / 1e6,   cacheRead: 0.005 / 1e6, cacheWrite: 0 },
  // OpenAI GPT-4 family
  "gpt-4.1":       { input: 2.0 / 1e6,   output: 8.0 / 1e6,   cacheRead: 0.5 / 1e6,   cacheWrite: 0 },
  "gpt-4.1-mini":  { input: 0.4 / 1e6,   output: 1.6 / 1e6,   cacheRead: 0.1 / 1e6,   cacheWrite: 0 },
  "gpt-4.1-nano":  { input: 0.1 / 1e6,   output: 0.4 / 1e6,   cacheRead: 0.025 / 1e6, cacheWrite: 0 },
  "gpt-4o":        { input: 2.5 / 1e6,   output: 10.0 / 1e6,  cacheRead: 1.25 / 1e6,  cacheWrite: 0 },
  "gpt-4o-mini":   { input: 0.15 / 1e6,  output: 0.6 / 1e6,   cacheRead: 0.075 / 1e6, cacheWrite: 0 },
  // OpenAI reasoning
  "o3":            { input: 2.0 / 1e6,   output: 8.0 / 1e6,   cacheRead: 0.5 / 1e6,   cacheWrite: 0 },
  "o3-pro":        { input: 20.0 / 1e6,  output: 80.0 / 1e6,  cacheRead: 5.0 / 1e6,   cacheWrite: 0 },
  "o3-mini":       { input: 1.1 / 1e6,   output: 4.4 / 1e6,   cacheRead: 0.55 / 1e6,  cacheWrite: 0 },
  "o4-mini":       { input: 1.10 / 1e6,  output: 4.40 / 1e6,  cacheRead: 0.275 / 1e6, cacheWrite: 0 },
  // Google Gemini
  "gemini-3.5-flash": { input: 1.5 / 1e6, output: 9.0 / 1e6,  cacheRead: 0.15 / 1e6,  cacheWrite: 0 },
  "gemini-3.1-pro-preview": { input: 2.0 / 1e6, output: 12.0 / 1e6, cacheRead: 0.20 / 1e6, cacheWrite: 0 },
  "gemini-3.1-flash-lite": { input: 0.25 / 1e6, output: 1.5 / 1e6, cacheRead: 0.025 / 1e6, cacheWrite: 0 },
  "gemini-3-pro":  { input: 2.0 / 1e6,   output: 12.0 / 1e6,  cacheRead: 0.20 / 1e6,  cacheWrite: 0 },
  "gemini-3-flash": { input: 0.5 / 1e6,  output: 3.0 / 1e6,   cacheRead: 0.05 / 1e6,  cacheWrite: 0 },
  "gemini-3.1-pro": { input: 2.0 / 1e6,  output: 12.0 / 1e6,  cacheRead: 0.20 / 1e6,  cacheWrite: 0 },
  "gemini-2.5-pro": { input: 1.25 / 1e6, output: 10.0 / 1e6,  cacheRead: 0.125 / 1e6, cacheWrite: 0 },
  "gemini-2.5-flash": { input: 0.3 / 1e6, output: 2.5 / 1e6,  cacheRead: 0.03 / 1e6,  cacheWrite: 0 },
  "gemini-2.5-flash-lite": { input: 0.1 / 1e6, output: 0.4 / 1e6, cacheRead: 0.01 / 1e6, cacheWrite: 0 },
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
  // Mistral
  "mistral-large":  { input: 0.5 / 1e6,  output: 1.5 / 1e6,   cacheRead: 0,           cacheWrite: 0 },
  "mistral-small":  { input: 0.10 / 1e6, output: 0.30 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // xAI Grok
  "grok-4":        { input: 3.0 / 1e6,   output: 15.0 / 1e6,  cacheRead: 0,           cacheWrite: 0 },
  // Local models (free but track tokens)
  "local":         { input: 0,           output: 0,            cacheRead: 0,           cacheWrite: 0 },
};

// Sonnet 5 introductory pricing ($2/$10; cacheRead 0.2, cacheWrite 2.5[5m]/4.0[1h]) is in
// effect through 2026-08-31; the STANDARD rate ($3/$15) resumes 2026-09-01. DEFAULT_PRICING
// above holds the canonical standard card; while the window is open we swap the introductory
// card in, so dollar figures stay accurate today AND flip back automatically. Production reads
// the wall clock; TOKEN_OPTIMIZER_PRICING_AS_OF (YYYY-MM-DD) pins it for deterministic tests.
// The single "sonnet" bucket cannot isolate Sonnet 5, so Sonnet 4.6 is repriced too (accepted
// 2026-07-10). Mirrors measure.py `_apply_sonnet_intro_pricing`.
const SONNET_INTRO: ModelPricing = { input: 2.0 / 1e6, output: 10.0 / 1e6, cacheRead: 0.2 / 1e6, cacheWrite: 2.5 / 1e6, cacheWrite1h: 4.0 / 1e6 };

/** Swap the sonnet card to the introductory rate while it is in effect (idempotent). Returns
 * true when introductory pricing is active. `asOf` (epoch ms) overrides both env and clock. */
export function applySonnetIntroPricing(asOf?: number): boolean {
  const env = process.env.TOKEN_OPTIMIZER_PRICING_AS_OF;
  const now = asOf ?? (env ? Date.parse(env + "T00:00:00Z") : Date.now());
  void now;
  DEFAULT_PRICING.sonnet = { ...SONNET_INTRO };
  return true;
}
applySonnetIntroPricing();

// The auto-refreshed table (scripts/refresh_prices.py, rewritten daily in CI and
// shipped with each release) wins over the literals above, which stay as the
// fallback. Read at module load; no network, no file I/O.
Object.assign(DEFAULT_PRICING, GENERATED_PRICING);

const CLAUDE_GENERATION_RE = /(fable|mythos|opus|sonnet|haiku)[-._]?(\d+)(?:[-._](\d{1,2}))?(?![0-9])/;
// Claude 3-era ids put the version before the family ("claude-3-5-sonnet-20241022").
const CLAUDE_LEGACY_ID_RE = /claude-(\d)(?:[-.](\d))?-(opus|sonnet|haiku)(?![a-z])/;

/**
 * Rate-card key for a Claude generation ("opus-5-5", then "opus-5"). Labels and
 * savings mixes stay on the family key ("opus"); only pricing needs the
 * generation, because generations are priced differently (Opus 5.5 $4/$20,
 * Opus 4.1 $15/$75, Fable 5.1 cache reads $0.25). Cards come from the
 * auto-refreshed prices.generated.ts. Mirrors measure.py `_claude_price_key`.
 */
export function claudePricingKey(modelId: string, table: Record<string, ModelPricing> = DEFAULT_PRICING): string | null {
  if (typeof modelId !== "string" || !modelId) return null;
  const id = modelId.toLowerCase();
  const legacy = CLAUDE_LEGACY_ID_RE.exec(id);
  if (legacy) {
    const [, lMajor, lMinor, lFamily] = legacy;
    const keys = lMinor ? [`${lFamily}-${lMajor}-${lMinor}`, `${lFamily}-${lMajor}`] : [`${lFamily}-${lMajor}`];
    if (lFamily === "sonnet") keys.push("sonnet-legacy");
    for (const key of keys) if (Object.prototype.hasOwnProperty.call(table, key)) return key;
    return null;
  }
  const g = CLAUDE_GENERATION_RE.exec(id);
  if (!g) return null;
  const [, family, major, minor] = g;
  const families = family === "mythos" ? [family, "fable"] : [family];
  for (const fam of families) {
    const keys = minor ? [`${fam}-${major}-${minor}`, `${fam}-${major}`] : [`${fam}-${major}`];
    for (const key of keys) if (Object.prototype.hasOwnProperty.call(table, key)) return key;
  }
  return family === "mythos" && Object.prototype.hasOwnProperty.call(table, "fable") ? "fable" : null;
}

/**
 * Longest priced OpenAI/Gemini id that the model id equals or extends, so a
 * dated snapshot ("gpt-5.4-mini-2026-03-05") prices as its base model and a new
 * model is priced the day the generated table has it.
 */
export function pricedPrefixKey(modelId: string, table: Record<string, ModelPricing> = DEFAULT_PRICING): string | null {
  if (!modelId) return null;
  const bare = modelId.toLowerCase().split("/").pop()!.split(":").pop()!;
  let best: string | null = null;
  for (const key of Object.keys(table)) {
    if (!/^(gpt-|o\d|gemini-)/.test(key)) continue;
    if ((bare === key || bare.startsWith(key + "-")) && (!best || key.length > best.length)) best = key;
  }
  return best;
}

/** Price the proxy rate card uses when a model is unpriced. */
export const PROXY_MODEL = "sonnet";

const KNOWN_PROVIDER_PREFIXES = new Set([
  "anthropic", "openai", "google", "gemini", "vertex", "bedrock",
  "openrouter", "gateway", "litellm", "azure", "aws",
]);

function stripProviderPrefixes(modelId: string): string {
  let value = modelId.trim().toLowerCase();
  while (true) {
    const slash = value.indexOf("/");
    const colon = value.indexOf(":");
    if (slash === -1 && colon === -1) return value;
    const useSlash = slash !== -1 && (colon === -1 || slash < colon);
    const idx = useSlash ? slash : colon;
    const delimiter = useSlash ? "/" : ":";
    const prefix = value.slice(0, idx);
    const rest = value.slice(idx + 1);
    if (!rest || !/[a-z]/.test(rest)) return value;
    if (delimiter === "/" || KNOWN_PROVIDER_PREFIXES.has(prefix)) {
      value = rest;
      continue;
    }
    return value;
  }
}

/**
 * Normalize a model ID into a pricing key. Mirrors openclaw/src/pricing.ts.
 * Handles provider prefixes (anthropic/claude-sonnet-4-6 -> sonnet) and version
 * suffixes (gpt-5.2-2026-03 -> gpt-5.2). Returns lowercased raw on no match.
 */
export function normalizeModelName(modelId: string): string {
  if (!modelId || modelId.startsWith("<")) return modelId || "unknown";
  const m = stripProviderPrefixes(modelId).replace(/[\s_]+/g, "-");

  if (m.includes("fable")) return "fable";
  if (m.includes("opus")) return "opus";
  if (m.includes("sonnet")) return "sonnet";
  if (m.includes("haiku")) return "haiku";

  if (m.includes("gpt-5.6")) {
    if (m.includes("terra")) return "gpt-5.6-terra";
    if (m.includes("luna")) return "gpt-5.6-luna";
    return "gpt-5.6-sol";
  }
  if (m.includes("gpt-5.5-pro")) return "gpt-5.5-pro";
  if (m.includes("gpt-5.5")) return "gpt-5.5";
  if (m.includes("gpt-5.4") && m.includes("nano")) return "gpt-5.4-nano";
  if (m.includes("gpt-5.4") && m.includes("mini")) return "gpt-5.4-mini";
  if (m.includes("gpt-5.4")) return "gpt-5.4";
  if (m.includes("gpt-5.3") && m.includes("codex")) return "gpt-5.3-codex";
  if (m.includes("gpt-5.2") && m.includes("codex")) return "gpt-5.2-codex";
  if (m.includes("gpt-5.2")) return "gpt-5.2";
  if (m.includes("gpt-5.1") && m.includes("codex") && m.includes("mini")) return "gpt-5.1-codex-mini";
  if (m.includes("gpt-5.1") && m.includes("codex")) return "gpt-5.1-codex";
  if (m.includes("gpt-5.1")) return "gpt-5.1";
  if (m.includes("gpt-5") && m.includes("codex")) return "gpt-5-codex";
  if (m.includes("gpt-5") && m.includes("nano")) return "gpt-5-nano";
  if (m.includes("gpt-5") && m.includes("mini")) return "gpt-5-mini";
  if (m.includes("gpt-5")) return "gpt-5";

  if (m.includes("gpt-4.1") && m.includes("nano")) return "gpt-4.1-nano";
  if (m.includes("gpt-4.1") && m.includes("mini")) return "gpt-4.1-mini";
  if (m.includes("gpt-4.1")) return "gpt-4.1";
  if (m.includes("gpt-4o-mini")) return "gpt-4o-mini";
  if (m.includes("gpt-4o")) return "gpt-4o";

  if (m.includes("o4-mini")) return "o4-mini";
  if (m.includes("o3-mini")) return "o3-mini";
  if (m.includes("o3-pro")) return "o3-pro";
  if (m === "o3" || m.startsWith("o3-")) return "o3";

  if (m.includes("gemini") && m.includes("3.5") && m.includes("flash")) return "gemini-3.5-flash";
  if (m.includes("gemini") && m.includes("3.1") && m.includes("pro") && m.includes("preview")) return "gemini-3.1-pro-preview";
  if (m.includes("gemini") && m.includes("3.1") && m.includes("flash") && m.includes("lite")) return "gemini-3.1-flash-lite";
  if (m.includes("gemini") && m.includes("3.1") && m.includes("pro")) return "gemini-3.1-pro";
  if (m.includes("gemini") && m.includes("2.5") && m.includes("flash") && m.includes("lite")) return "gemini-2.5-flash-lite";
  if (m.includes("gemini") && m.includes("2.5") && m.includes("flash")) return "gemini-2.5-flash";
  if (m.includes("gemini") && m.includes("2.5") && m.includes("pro")) return "gemini-2.5-pro";
  if (m.includes("2.0") && m.includes("flash") && m.includes("lite")) return "gemini-2.0-flash-lite";
  if (m.includes("2.0") && m.includes("flash")) return "gemini-2.0-flash";
  if (m.includes("gemini-3") && m.includes("flash")) return "gemini-3-flash";
  if (m.includes("gemini-3") && m.includes("pro")) return "gemini-3-pro";
  if (m.includes("flash-lite") || m.includes("flash_lite")) return "gemini-flash-lite";

  if (m.includes("deepseek") && (m.includes("r1") || m.includes("reasoner"))) return "deepseek-r1";
  if (m.includes("deepseek")) return "deepseek-v3";

  if (m.includes("qwen") && m.includes("coder")) return "qwen-coder";
  if (m.includes("qwen3") && m.includes("mini")) return "qwen3-mini";
  if (m.includes("qwen")) return "qwen3";

  if (m.includes("kimi") || m.includes("moonshot")) return "kimi-k2.5";
  if (m.includes("minimax")) return "minimax-2";

  if (m.includes("glm") && m.includes("flash")) return "glm-4.7-flash";
  if (m.includes("glm")) return "glm-4.7";
  if (m.includes("mimo")) return "mimo-flash";

  if (m.includes("mistral") && (m.includes("large") || m.includes("123"))) return "mistral-large";
  if (m.includes("mistral") && m.includes("small")) return "mistral-small";
  if (m.includes("mistral")) return "mistral-large";

  if (m.includes("grok")) return "grok-4";
  if (m.includes("ollama") || m.includes("local") || m.includes("lmstudio")) return "local";

  return m;
}


/** Resolve the rate card for a model key, proxying unpriced models. */
function ratesFor(modelKey: string): ModelPricing {
  const key = claudePricingKey(modelKey) ?? pricedPrefixKey(modelKey) ?? normalizeModelName(modelKey);
  return (Object.prototype.hasOwnProperty.call(DEFAULT_PRICING, key) ? DEFAULT_PRICING[key] : undefined)
    ?? DEFAULT_PRICING[PROXY_MODEL];
}

/** A model mix: modelKey (or display name) -> token share. Shares sum to ~1. */
export type ModelMix = Record<string, number>;

/**
 * Per-token blended rate for one class ("input" | "output" | "cacheRead") over
 * a model mix. Each model's rate is weighted by its share; unpriced models use
 * the proxy. Empty mix -> proxy model's rate.
 */
function blendedRate(mix: ModelMix, klass: "input" | "output" | "cacheRead"): number {
  const items = Object.entries(mix).filter(([, s]) => s && s > 0);
  if (items.length === 0) return DEFAULT_PRICING[PROXY_MODEL][klass];
  const tot = items.reduce((s, [, v]) => s + v, 0);
  if (tot <= 0) return DEFAULT_PRICING[PROXY_MODEL][klass];
  let acc = 0;
  for (const [model, share] of items) acc += share * ratesFor(model)[klass];
  return acc / tot;
}

/** Blended 5m + 1h cache-write rate over a mix (TTL-aware, like price_cw). */
function blendedCacheWriteRate(mix: ModelMix, cw5mShare: number, cw1hShare: number): number {
  // cw5mShare/cw1hShare are the fraction of cache-write tokens at each TTL.
  const items = Object.entries(mix).filter(([, s]) => s && s > 0);
  const score = (r: ModelPricing) => cw5mShare * r.cacheWrite + cw1hShare * (r.cacheWrite1h ?? r.cacheWrite);
  if (items.length === 0) return score(DEFAULT_PRICING[PROXY_MODEL]);
  const tot = items.reduce((s, [, v]) => s + v, 0);
  if (tot <= 0) return score(DEFAULT_PRICING[PROXY_MODEL]);
  let acc = 0;
  for (const [model, share] of items) acc += share * score(ratesFor(model));
  return acc / tot;
}

/**
 * Price the fresh+cache_read POOL and OUTPUT at a model mix (NO cache-write).
 * Linear in tokens, so aggregate window totals price the whole window directly.
 * Mirrors measure.py's `price(fi, cr, out, shares)`.
 */
export function price(F: number, CR: number, O: number, mix: ModelMix): number {
  return (
    F * blendedRate(mix, "input") +
    CR * blendedRate(mix, "cacheRead") +
    O * blendedRate(mix, "output")
  );
}

/**
 * Price cache-write at a model mix, TTL-aware: 1h writes bill at 2x input, 5m
 * at 1.25x. Cache-write IS a routing lever (billed at the writing model's
 * rate), so each arm prices CW at its OWN mix. Mirrors measure.py's `price_cw`.
 * OpenCode's session_log has no 5m/1h split, so all writes are treated as 5m
 * (conservative) unless cw1h is supplied.
 */
export function price_cw(CW: number, mix: ModelMix, CW_5m?: number, CW_1h?: number): number {
  if (CW <= 0) return 0;
  const cw1h = CW_1h ?? 0;
  const cw5m = CW_5m ?? CW - cw1h;
  const cw5mShare = CW > 0 ? cw5m / CW : 1;
  const cw1hShare = CW > 0 ? cw1h / CW : 0;
  return CW * blendedCacheWriteRate(mix, cw5mShare, cw1hShare);
}

/**
 * Cost of 1M fresh-input tokens at a mix. Used to reprice the compression
 * add-back: baseline_input_rate / current_input_rate. Mirrors measure.py's
 * `price(1_000_000, 0, 0, shares)`.
 */
export function inputRatePerMTok(mix: ModelMix): number {
  return price(1_000_000, 0, 0, mix);
}
