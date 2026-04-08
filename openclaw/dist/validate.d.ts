/**
 * Validate impact: compare session metrics before vs after an optimization.
 *
 * Two strategies:
 *   auto: split at the most recent session with outcome "success" after a gap
 *   halves: split session history chronologically in half
 */
import { AgentRun } from "./models";
export type Strategy = "auto" | "halves";
export type Verdict = "improved" | "regressed" | "no_change" | "insufficient_data";
export interface WindowSummary {
    count: number;
    avgTokens: number;
    avgCost: number;
    avgMessages: number;
    avgCacheHitRate: number;
}
export interface ValidateResult {
    strategy: Strategy;
    splitLabel: string;
    before: WindowSummary;
    after: WindowSummary;
    deltas: {
        tokensPct: number;
        costPct: number;
        messagesPct: number;
        cacheHitPct: number;
    };
    verdict: Verdict;
}
export declare function validateImpact(runs: AgentRun[], strategy?: Strategy): ValidateResult;
//# sourceMappingURL=validate.d.ts.map