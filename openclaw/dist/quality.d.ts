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
export declare function contextWindowForModel(model: string): number;
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
//# sourceMappingURL=quality.d.ts.map