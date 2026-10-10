/**
 * Quality Scoring for OpenClaw.
 *
 * 7-signal two-stage quality metric adapted for OpenClaw's architecture.
 * Stage 1: 5 coarse signals (context fill, session length, model routing, empty runs, outcomes)
 * Stage 2: 2 TurboQuant-inspired semantic signals (message efficiency, compression opportunity)
 * Includes distortion bounds analysis for estimated quality ceiling.
 * Score: 0-100 with color bands (Good/Fair/Needs Work/Poor).
 */
import { AgentRun } from "./models";
import { ContextAudit } from "./context-audit";
export interface QualitySignal {
    name: string;
    weight: number;
    score: number;
    description: string;
}
export interface QualityReport {
    score: number;
    grade: string;
    band: string;
    signals: QualitySignal[];
    recommendations: string[];
    distortionBounds?: DistortionBounds;
}
export declare function claudeContextWindow(model: string): number | null;
/**
 * Resolve a model's context window. Tries exact match, then a Claude-family rule,
 * then substring match, so a full model id (e.g. "claude-sonnet-4-6",
 * "anthropic/claude-opus-4-8") resolves to its real window instead of silently
 * defaulting to 200K -- which would overstate fill% up to ~5x for a 1M-window
 * Claude session. Unknown models fall back to a conservative 200K ASSUMED
 * window; callers must label fill as an estimate against an assumed window.
 */
export declare function contextWindowForModel(model: string): number;
/**
 * Opus/Sonnet 4.6 are 1M only as the `[1m]` variant, but the runtime records
 * the plain id. Observed tokens above the 200K window prove the 1M variant, so
 * widen the window instead of reporting a fill above 100%. Any other model, or
 * tokens that fit, keep the table window.
 */
export declare function promoteWindowForObservedTokens(model: string, window: number, tokens: number): number;
export interface DistortionBounds {
    /** Estimated best quality score for this configuration (heuristic upper bound). */
    theoreticalMax: number;
    /** Current achieved quality score. */
    achievedScore: number;
    /** Ratio of achieved to estimated maximum (0-1). */
    utilization: number;
    /** Actionable recommendation based on utilization. */
    recommendation: string;
}
/**
 * Compute estimated quality bounds inspired by TurboQuant distortion concepts.
 *
 * Uses a heuristic based on context window capacity to estimate an upper
 * bound on achievable quality. This is a useful approximation, not a
 * proven mathematical limit — treat it as an estimated ceiling.
 *
 * @param runs - Agent runs to analyze
 * @param modelContextWindow - Context window size in tokens for the dominant model
 * @returns Distortion bounds with estimated max, achieved score, and utilization
 */
export declare function computeDistortionBounds(runs: AgentRun[], modelContextWindow: number, precomputedSignals?: QualitySignal[]): DistortionBounds;
/**
 * Convert a 0-100 quality score to a letter grade.
 * S: 90-100 | A: 80-89 | B: 70-79 | C: 55-69 | D: 40-54 | F: 0-39
 */
export declare function scoreToGrade(score: number): string;
export declare function scoreQuality(runs: AgentRun[], contextAudit?: ContextAudit | null): QualityReport;
/**
 * Score a single AgentRun's quality on a 0-100 scale.
 *
 * Signals (weights):
 *   1. Context fill (25%): input tokens / model context window
 *   2. Message count risk (25%): >50 messages = degraded
 *   3. Cache hit rate (20%): higher = better (OpenClaw: typically 0)
 *   4. Output/input ratio (15%): low ratio = wasteful
 *   5. Duration risk (15%): >60min sessions = risk
 */
export declare function scoreSessionQuality(run: AgentRun): {
    score: number;
    grade: string;
    band: string;
};
/** Quality threshold below which the nudge fires (mirrors Python _FRESH_NUDGE_QUALITY_THRESHOLD). */
export declare const FRESH_NUDGE_QUALITY_THRESHOLD = 70;
/** Minimum context fill % required for the nudge to fire (mirrors Python _FRESH_NUDGE_MIN_FILL). */
export declare const FRESH_NUDGE_MIN_FILL = 50;
/** Approximate tokens a fresh lean-resume block re-injects (mirrors Python _FRESH_NUDGE_LEAN_BLOCK_TOKENS). */
export declare const FRESH_NUDGE_LEAN_BLOCK_TOKENS = 1000;
/**
 * Estimate tokens reclaimed by starting a fresh session now.
 *
 * current_ctx = (fill_pct / 100) * context_window
 * saved       = max(0, current_ctx - FRESH_NUDGE_LEAN_BLOCK_TOKENS)
 *
 * Returns { savedTokens, contextWindow }.
 * Mirrors Python _fresh_session_savings_estimate().
 *
 * `sessionContextWindow` MUST be the same window fill_pct was measured against
 * (pass RuntimeEventContext.contextWindow or RuntimeSnapshot.contextWindow).
 * This prevents the token estimate from being inconsistent with the fill %:
 * e.g. "119K tokens = 60% of window" would be nonsense on a 1M-context session
 * if the estimate silently used a 200K fallback. Model-based re-detection is
 * kept as a fallback ONLY when the session window is genuinely unavailable.
 */
export declare function freshSessionSavingsEstimate(fillPct: number, model?: string, sessionContextWindow?: number): {
    savedTokens: number;
    contextWindow: number;
};
/**
 * Compute the API-equivalent dollar value of the reclaimed tokens.
 *
 * Uses the model's input rate from DEFAULT_PRICING. Returns 0 on any error
 * or unknown model. Best-effort, never throws.
 * Mirrors Python _fresh_session_savings_usd().
 */
export declare function freshSessionSavingsUsd(savedTokens: number, model?: string): number;
/**
 * Build the fresh-session nudge message string.
 *
 * Returns a string when ALL conditions are met:
 *   - qualityScore < FRESH_NUDGE_QUALITY_THRESHOLD (70)
 *   - fillPct >= FRESH_NUDGE_MIN_FILL (50%)
 *   - hasPriorScore = true (not a post-compaction/fresh session baseline)
 *
 * Returns null when any condition is not met. The caller is responsible for
 * once-per-session dedup (tracking whether the nudge has already fired).
 *
 * Takes precedence over the compact nudge: if this returns a message, the
 * caller must NOT also emit the /compact quality nudge.
 *
 * Mirrors Python _maybe_fresh_session_nudge() logic (minus cache read/write
 * and the _is_v5_feature_enabled guard, which the caller handles).
 *
 * `sessionContextWindow` should be RuntimeEventContext.contextWindow -- the exact
 * window used to derive fillPct. Avoids token/% inconsistency (e.g. "119K = 60%"
 * on a 1M session when a 200K fallback was used). Falls back to model detection
 * only when the session window is genuinely unavailable.
 */
export declare function buildFreshSessionNudgeMessage(qualityScore: number, fillPct: number, hasPriorScore: boolean, model?: string, sessionContextWindow?: number): string | null;
//# sourceMappingURL=quality.d.ts.map