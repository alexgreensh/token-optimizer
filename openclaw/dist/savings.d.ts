export declare function savingsCategoryLabel(eventType: string): string;
export interface SavingsEventCategory {
    eventType: string;
    label: string;
    count: number;
    tokensSaved: number;
    costSavedUsd: number;
}
export interface SavingsEventsSummary {
    categories: SavingsEventCategory[];
    totalTokensSaved: number;
    totalCostSavedUsd: number;
    totalCount: number;
    unpricedTokens?: number;
    unpricedEvents?: number;
    archivePricedNetUsd?: number | null;
    /** Priced tokens/dollars that used the input rate because no billed class was
     * recorded — an honest floor, disclosed wherever these savings are shown. */
    inputRateFloorTokens?: number;
    inputRateFloorUsd?: number;
}
/**
 * Optional lookback window for savings-events reads. Mirrors Python
 * `_get_savings_summary(days=days)`, which filters `timestamp >= now - days`.
 * When omitted, ALL events are summed (the display/grouping callers want lifetime
 * totals); the compression add-back caller passes a window so a stale event is not
 * counted and then monthly-scaled.
 */
export interface SavingsEventsWindow {
    days: number;
    now?: number;
}
/**
 * Read savings-events.jsonl, group by event_type, and return per-category
 * totals + a grand total. No allowlist: every event_type in the file surfaces.
 * Returns an empty summary (not an error) when the file is missing. When `window`
 * is given, only events whose `timestamp` is within `now - days` are counted.
 */
export declare function readSavingsEventsByCategory(openclawDir?: string, window?: SavingsEventsWindow): SavingsEventsSummary;
export declare function measuredSavingsFromSummary(summary: SavingsEventsSummary): number;
export interface SavingsBreakdownItem {
    key: string;
    label: string;
    /** Exact raw lever value. */
    monthlyUsd: number;
    /** Cent-allocated display value: the displayed levers always add to the
     * displayed headline (largest-remainder cent allocation, round once). */
    monthlyUsdDisplay: number;
}
/**
 * Largest-remainder cent allocation. Rounds `total` to cents once, then
 * apportions those cents across `parts` so the returned parts always re-add to
 * the rounded total. Signed parts are handled by flooring toward -inf before
 * apportioning; `parts` need not already sum to `total` (any remainder surplus
 * lands on the first part so the sum always holds).
 */
export declare function allocateCents(total: number, parts: number[]): number[];
/** Structured baseline-building progress (mirrors measure.py `_baseline_progress`). */
export interface BaselineBuildingProgress {
    sessionsInWindow: number;
    sessionsNeeded: number;
    earlyWindowDays: number;
    daysLeft: number;
    firstDate: string;
}
export interface RealizedSavings {
    ready: boolean;
    status: string;
    /** Headline transformation = main + subagent(0) + compression add-back, monthly. */
    monthlySavingsUsd: number;
    savingsPerSession: number;
    /** "The old way" cost per session (counterfactual / current session count). */
    beforeCostPerSession: number;
    /** "Now" cost per session (actual / current session count). */
    afterCostPerSession: number;
    /** Baseline session's native cache-read fraction (0..1). Explains why identical tokens+mix
     * can price differently across the arms (the cache-hit pricing contradiction). */
    beforeCacheHit: number;
    /** Current pool cache-hit rate (0..1). */
    afterCacheHit: number;
    sessionsPerMonth: number;
    /**
     * Days of post-baseline history behind the monthly projection (floor of the
     * after-window span, clamped >= 1). Drives the dashboard's young-install
     * guard: < 30 -> show the measured cumulative ("so far") instead of the
     * "/mo" run-rate. 0 when not ready.
     */
    trackedDays: number;
    beforeMixLabel: string;
    afterMixLabel: string;
    cumulativeSavedUsd: number;
    installDate: string | null;
    breakdown: SavingsBreakdownItem[];
    /** Combined counterfactual arm = main cf + compression add-back, monthly USD. */
    counterfactualMonthlyUsd: number;
    /** Actual arm = current volume at current mix, monthly USD. */
    actualMonthlyUsd: number;
    mainTransformationUsd: number;
    /** Always 0 on OpenClaw — no Claude-style sidechains (documented gap). */
    subagentTransformationUsd: number;
    compressionTransformationUsd: number;
    /** Directly-metered compression $ before the baseline-mix reprice. */
    compressionMeasuredUsd: number;
    /** Estimated verbosity-steer $ before the baseline-output reprice. */
    verbosityMeasuredUsd: number;
    /** Repriced verbosity-steer $ (estimated output reduction at baseline mix). */
    verbosityTransformationUsd: number;
    transformationPct: number;
    beforeOpus: number;
    afterOpus: number;
    premiumTokens?: number;
    premiumCostUsd?: number;
    /** Recent tokens excluded because their model has no known price. */
    unpricedTokens?: number;
    baselineClassMixAvailable?: boolean;
    baselineTtlMixAvailable?: boolean;
    /**
     * Structured baseline-building progress. Present when `ready === false` and the
     * not-ready reason is an insufficient early-session count or the window is still
     * open. Undefined when the baseline is already frozen or when there is no history.
     */
    baselineBuilding?: BaselineBuildingProgress;
}
/**
 * Compute the realized current-volume counterfactual transformation. `now` is
 * injectable for testing. See the module header for the full methodology.
 */
export declare function computeRealizedSavings(openclawDir: string, days?: number, now?: number): RealizedSavings;
//# sourceMappingURL=savings.d.ts.map