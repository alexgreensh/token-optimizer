/**
 * Fresh-session nudge: fires once per session when context is BOTH long
 * (fill >= FRESH_NUDGE_MIN_FILL_PCT) AND degraded (quality < FRESH_NUDGE_QUALITY_THRESHOLD).
 *
 * Confidently reassures the user that Token Optimizer has checkpointed their
 * active task so a fresh session resumes exactly where they stopped, and shows
 * the concrete tokens they would reclaim by starting fresh now.
 *
 * Takes PRECEDENCE over the ordinary quality/compact nudge (the caller skips
 * that when this fires — both messages would be noise).
 *
 * Ported from Python _maybe_fresh_session_nudge / _fresh_session_savings_estimate
 * in skills/token-optimizer/scripts/measure.py.
 */

import { contextWindowForModel } from "../util/context-window.js";
import { claudePricingKey, DEFAULT_PRICING, isUnpricedClaudeVersion, normalizeModelName, pricedPrefixKey } from "../pricing.js";

// ---------------------------------------------------------------------------
// Per-model input rates ($/M tokens). Mirrors Python PRICING_TIERS["anthropic"]
// and the non-Claude model table. Sonnet is resolved from the DATE-GATED pricing
// table instead (see modelInputRatePer1M), so it is intentionally omitted here.
// The sonnet-based fallback for unknown models is likewise the gated rate.
// ---------------------------------------------------------------------------
const MODEL_INPUT_RATES: Record<string, number> = {
  // Anthropic Claude (sonnet gated separately)
  fable: 10.0,
  opus: 5.0,
  haiku: 1.0,
  // GPT-5 family
  "gpt-5.6-sol": 5.0,
  "gpt-5.6-terra": 2.0,
  "gpt-5.6-luna": 0.20,
  "gpt-5.5-pro": 30.0,
  "gpt-5.5": 5.0,
  "gpt-5.4": 2.5,
  "gpt-5.4-mini": 0.75,
  "gpt-5.4-nano": 0.2,
  "gpt-5.3-codex": 1.75,
  "gpt-5.2-codex": 1.75,
  "gpt-5.2": 1.75,
  "gpt-5.1-codex-mini": 0.25,
  "gpt-5.1-codex": 1.25,
  "gpt-5.1": 1.25,
  "gpt-5-codex": 1.25,
  "gpt-5": 1.25,
  "gpt-5-mini": 0.25,
  "gpt-5-nano": 0.05,
  // GPT-4 family
  "gpt-4.1": 2.0,
  "gpt-4.1-mini": 0.4,
  "gpt-4.1-nano": 0.1,
  "gpt-4o": 2.5,
  "gpt-4o-mini": 0.15,
  // OpenAI reasoning
  "o3-pro": 20.0,
  o3: 2.0,
  "o3-mini": 1.1,
  "o4-mini": 1.1,
  // Google Gemini
  "gemini-2.5-pro": 1.25,
  "gemini-2.5-flash": 0.3,
  "gemini-2.5-flash-lite": 0.1,
  "gemini-2.0-flash": 0.1,
  "gemini-2.0-flash-lite": 0.075,
};

/**
 * Look up the API input rate ($/M tokens) for the given model.
 * Substring-matches the lowercase model id against the table — same
 * strategy as contextWindowForModel. Falls back to the (date-gated) Sonnet rate.
 */
export function modelInputRatePer1M(model?: string): number {
  // Sonnet (and the sonnet-based fallback) derive from the DATE-GATED pricing table so the
  // nudge's dollar estimate matches the savings dashboard during Sonnet's introductory window
  // (parity with OpenClaw's freshSessionTurnEconomics + Python's _fresh_session_turn_economics, which
  // both read the gated table). The simple per-model map still serves every other model.
  // Sonnet is omitted from MODEL_INPUT_RATES, so any sonnet id (and every unknown model) falls
  // through to this date-gated rate — keeping the nudge in step with the savings dashboard.
  const sonnetRate = DEFAULT_PRICING.sonnet.input * 1e6;
  if (!model) return sonnetRate;
  if (isUnpricedClaudeVersion(model)) return 0;
  const lower = model.toLowerCase();
  const canonical = normalizeModelName(model);

  // The shared, auto-refreshed rate table wins, so the nudge's dollars match the
  // dashboard as prices change. The local map below only covers models that
  // table does not price.
  const shared = DEFAULT_PRICING[claudePricingKey(model) ?? pricedPrefixKey(model) ?? canonical];
  if (shared) return shared.input * 1e6;

  const direct = MODEL_INPUT_RATES[canonical] ?? MODEL_INPUT_RATES[lower];
  if (direct !== undefined) return direct;

  // Substring scan (longest key wins via insertion order — most specific first
  // in the table above). Mirrors Python's model-tier resolution.
  for (const [key, rate] of Object.entries(MODEL_INPUT_RATES)) {
    if (lower.includes(key)) return rate;
  }

  return sonnetRate;
}

/**
 * Per-token rate ($/token) for a token class on the given model.
 * Shared pricing table first (exact class rates); models only covered by
 * MODEL_INPUT_RATES have no cache-class card, so cache classes bill at the
 * input rate -- same fallback as Python's `rates.get("cache_read") or in_rate`.
 */
function modelClassRatePerToken(model: string | undefined, klass: "input" | "cacheRead" | "cacheWrite"): number {
  if (model && !isUnpricedClaudeVersion(model)) {
    const key = claudePricingKey(model) ?? pricedPrefixKey(model) ?? normalizeModelName(model);
    const rate = DEFAULT_PRICING[key]?.[klass];
    if (typeof rate === "number" && rate > 0) return rate;
  }
  return modelInputRatePer1M(model) / 1_000_000;
}

export interface FreshSessionTurnEconomics {
  /** Recurring per-turn saving: warm context re-sent at the cache-read rate,
   * minus the working set the fresh session carries anyway. */
  perTurnUsd: number;
  /** One-time cost: lean block cache-write handover + rebuild re-reads at input. */
  oneTimeUsd: number;
  /** ceil(oneTime / perTurn); null when perTurn <= 0 (no fabricated N). */
  breakevenTurns: number | null;
}

/**
 * Per-turn saving and break-even turns for starting a fresh session now.
 * Mirrors Python _fresh_session_turn_economics. savedTokens is the warm
 * context a continued session keeps re-sending; rebuildTokens is the working
 * set the fresh session must carry. OpenCode has no active-read store to
 * measure it, so the default lean-block floor is a lower bound on rebuild
 * size: per-turn savings are an upper bound and payback is a lower bound.
 * Counterfactual ESTIMATE, never a meter; no horizon is assumed.
 */
export function freshSessionTurnEconomics(
  savedTokens: number,
  model?: string,
  rebuildTokens: number = FRESH_NUDGE_LEAN_BLOCK_TOKENS,
): FreshSessionTurnEconomics {
  const zero: FreshSessionTurnEconomics = { perTurnUsd: 0, oneTimeUsd: 0, breakevenTurns: null };
  try {
    const saved = Math.max(0, Math.floor(savedTokens || 0));
    const rebuild = Math.max(0, Math.floor(rebuildTokens || 0));
    const cr = modelClassRatePerToken(model, "cacheRead");
    const cw = modelClassRatePerToken(model, "cacheWrite");
    const inp = modelClassRatePerToken(model, "input");
    const perTurnUsd = Math.max(0, (saved - rebuild) * cr);
    const oneTimeUsd = FRESH_NUDGE_LEAN_BLOCK_TOKENS * cw + rebuild * inp;
    const breakevenTurns = perTurnUsd > 0 ? Math.max(1, Math.ceil(oneTimeUsd / perTurnUsd)) : null;
    return { perTurnUsd, oneTimeUsd, breakevenTurns };
  } catch {
    return zero;
  }
}

function intEnv(key: string, fallback: number): number {
  const raw = process.env[key]?.trim();
  if (!raw) return fallback;
  const parsed = parseInt(raw, 10);
  return isNaN(parsed) ? fallback : parsed;
}

// Env-tunable thresholds, matching Python constants.
export const FRESH_NUDGE_QUALITY_THRESHOLD = intEnv("TOKEN_OPTIMIZER_FRESH_NUDGE_QUALITY", 70);
export const FRESH_NUDGE_MIN_FILL_PCT = intEnv("TOKEN_OPTIMIZER_FRESH_NUDGE_MIN_FILL", 50);

/** Tokens re-injected by a fresh lean-resume (the small overhead the new session pays). */
const FRESH_NUDGE_LEAN_BLOCK_TOKENS = 1000;

export interface FreshNudgeResult {
  shouldNudge: boolean;
  message: string | null;
}

/**
 * Estimate tokens reclaimed by starting a fresh session now.
 * current context size = (fillPct / 100) * contextWindow
 * savings = current context - lean block re-injection overhead
 *
 * @param fillPct        0-100 (percentage, not fraction)
 * @param model          optional model id — used only as a last-resort fallback
 *                       when sessionWindow is unavailable
 * @param sessionWindow  the EXACT context-window value the fill% was measured
 *                       against (pass the same value used in computeQualityScore).
 *                       When provided this takes priority over re-deriving from the
 *                       model, which guarantees token count == fill% of that window
 *                       (e.g. 54% of 1_000_000 ≈ 540K, never ~107K on a 200K fallback).
 * @returns [savedTokens, contextWindow]
 */
export function freshSessionSavingsEstimate(fillPct: number, model?: string, sessionWindow?: number): [number, number] {
  // Priority: explicit session window > model-based lookup > default fallback.
  // Re-deriving from the model is a last resort: it can silently disagree with
  // the window the fill % was actually computed against (e.g. 200K fallback on a
  // 1M session makes the token estimate 5x too low).
  const contextWindow = (sessionWindow && sessionWindow > 0)
    ? sessionWindow
    : contextWindowForModel(model ?? "");
  const clampedFill = Math.max(0, Math.min(100, fillPct));
  const currentCtx = Math.round((clampedFill / 100) * contextWindow);
  const saved = Math.max(0, currentCtx - FRESH_NUDGE_LEAN_BLOCK_TOKENS);
  return [saved, contextWindow];
}

/**
 * Check whether the fresh-session nudge should fire for this turn.
 *
 * @param currentScore        current quality/resource-health score (0-100)
 * @param fillPct             current context fill as 0-100 (percentage, not fraction)
 * @param previousScore       score from the previous turn (null = no prior score yet)
 * @param freshNudgeFired     whether the nudge already fired this session
 * @param nudgesEnabled       whether quality nudges are enabled in config
 * @param continuityEnabled   whether checkpoint continuity is enabled. The nudge's
 *                            whole pitch ("start fresh, your place is saved") only
 *                            holds when continuity actually restores the checkpoint
 *                            in the new session. With continuity off, suppress the
 *                            nudge so the ordinary quality nudge (/compact) takes
 *                            over instead of promising a restore that never happens.
 * @param model               optional model id — fallback for context-window lookup
 * @param sessionWindow       the EXACT context-window value the fill% was measured
 *                            against; threads through to freshSessionSavingsEstimate
 *                            so the token count is consistent with the fill% display
 * @param qualityThreshold    score below which (with fill) the nudge may fire; defaults
 *                            to the env-tunable module constant, overridable via config
 * @param minFillPct          fill% at/above which the nudge may fire; same default rule
 */
export function checkFreshSessionNudge(
  currentScore: number,
  fillPct: number,
  previousScore: number | null,
  freshNudgeFired: boolean,
  nudgesEnabled: boolean,
  continuityEnabled: boolean,
  model?: string,
  sessionWindow?: number,
  qualityThreshold: number = FRESH_NUDGE_QUALITY_THRESHOLD,
  minFillPct: number = FRESH_NUDGE_MIN_FILL_PCT,
): FreshNudgeResult {
  if (!nudgesEnabled) return { shouldNudge: false, message: null };

  // The nudge promises "Token Optimizer has checkpointed your task, so a new
  // session picks up where you stopped." That is only true when continuity is on.
  // If the user disabled it, do not inject that promise into the system prompt --
  // bail so the ordinary quality nudge handles the long+degraded session instead.
  if (!continuityEnabled) return { shouldNudge: false, message: null };

  // Post-compaction suppression: no prior score means this is a fresh/just-compacted
  // session. Let the ordinary nudge seed the baseline first.
  if (previousScore === null) return { shouldNudge: false, message: null };

  // Once per session.
  if (freshNudgeFired) return { shouldNudge: false, message: null };

  // Both conditions must hold: long session AND degraded quality.
  if (!(currentScore < qualityThreshold && fillPct >= minFillPct)) {
    return { shouldNudge: false, message: null };
  }

  const [saved] = freshSessionSavingsEstimate(fillPct, model, sessionWindow);
  const savedStr = saved >= 1000 ? `~${Math.floor(saved / 1000)}K` : `~${saved}`;
  const fillRounded = Math.round(fillPct);
  const scoreRounded = Math.round(currentScore);

  const econ = freshSessionTurnEconomics(saved, model);
  // Per-turn saving + break-even is a counterfactual estimate; no horizon is
  // assumed, so the message never claims a session-total. When the per-turn
  // saving is negligible the dollar clause is omitted rather than inventing
  // a payback period.
  let costStr = "";
  if (econ.perTurnUsd >= 0.001) {
    const perStr = econ.perTurnUsd >= 0.01 ? `$${econ.perTurnUsd.toFixed(2)}` : "<$0.01";
    const be = econ.breakevenTurns;
    const beStr = be ? `, pays back after ~${be} turn${be === 1 ? "" : "s"}` : "";
    costStr = ` (saves ~${perStr} per turn${beStr}, est.)`;
  }

  const message =
    `[Token Optimizer] This session is long (${fillRounded}% full) and context quality has fallen to ${scoreRounded}. ` +
    `Starting a fresh session now would reclaim ${savedStr} tokens (~${fillRounded}% of your window)${costStr}. ` +
    `You won't lose your place: Token Optimizer has checkpointed your active task, key decisions, files, and tool results, ` +
    `so a new session picks up exactly where you stopped. Just open one and say "continue this".`;

  return { shouldNudge: true, message };
}
