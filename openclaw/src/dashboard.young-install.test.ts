/**
 * Young-install dashboard guard tests (OpenClaw generator, v5.11.79).
 *
 * `generateDashboardHtml` is a pure string function and tolerates a minimal
 * fabricated `DashboardData`. We slice the savings view before asserting so the
 * overview stat-card and CLI summary (which emit waste-based "/mo" unconditionally)
 * do not pollute the negative assertions.
 *
 * Run: bun test src/dashboard.young-install.test.ts
 */
import { test, expect } from "bun:test";
import { generateDashboardHtml } from "./dashboard.js";
import type { DashboardData } from "./dashboard.js";
import type { RealizedSavings } from "./savings.js";

// Discriminating values chosen to be unique in the rendered output:
//   monthlySavingsUsd: 1760  -> fmtCost -> "$1.8k"
//   compressionMeasuredUsd: 12.34 -> "$12.34"
//   cumulativeSavedUsd: 999  -> "$999.00"
function savings(overrides: Partial<RealizedSavings>): RealizedSavings {
  return {
    ready: true,
    status: "ok",
    monthlySavingsUsd: 1760,
    savingsPerSession: 1.5,
    beforeCostPerSession: 5,
    afterCostPerSession: 3.5,
    beforeCacheHit: 0.9,
    afterCacheHit: 0.95,
    sessionsPerMonth: 300,
    trackedDays: 30,
    beforeMixLabel: "95% opus",
    afterMixLabel: "60% sonnet",
    cumulativeSavedUsd: 999,
    installDate: "2026-07-31",
    breakdown: [
      { key: "routing", label: "Smarter model routing (incl. cache-write)", monthlyUsd: 1760, monthlyUsdDisplay: 1760 },
    ],
    counterfactualMonthlyUsd: 2000,
    actualMonthlyUsd: 240,
    mainTransformationUsd: 1500,
    subagentTransformationUsd: 0,
    compressionTransformationUsd: 200,
    compressionMeasuredUsd: 12.34,
    verbosityMeasuredUsd: 0,
    verbosityTransformationUsd: 60,
    transformationPct: 0.88,
    beforeOpus: 0.95,
    afterOpus: 0.6,
    ...overrides,
  };
}

const baseData: DashboardData = {
  generatedAt: "2026-08-04T00:00:00.000Z",
  daysScanned: 30,
  contextWindow: 200000,
  overview: {
    totalRuns: 0,
    totalCost: 0,
    totalTokens: 0,
    totalBillableTokens: 0,
    allCostZero: true,
    monthlySavings: 0,
    wasteCount: 0,
    activeDays: 0,
    unknownModelRuns: 0,
  },
  agents: [],
  agentCosts: [],
  waste: [],
  daily: [],
  models: [],
  severityCounts: { low: 0, medium: 0, high: 0, critical: 0 },
  quality: null,
  context: null,
  sessions: [],
  pricingTier: "api",
  pricingTierLabel: "API",
  coach: null,
  savings: null,
  savingsEvents: { categories: [], totalTokensSaved: 0, totalCostSavedUsd: 0, totalCount: 0 },
  coreVersion: "5.13.10",
  adapterVersion: "2.4.21",
};

function render(overrides: Partial<RealizedSavings>): string {
  const html = generateDashboardHtml({ ...baseData, savings: savings(overrides) });
  // Slice the SAVINGS VIEW only: from `id="view-savings"` up to the next sibling
  // view (`id="view-context"`). The overview stat-card (before) and the CLI
  // summary in the aside (after) both emit waste-based "/mo" unconditionally and
  // must stay out of the negative assertions.
  const start = html.indexOf('id="view-savings"');
  const end = html.indexOf('id="view-context"');
  return html.slice(start, end === -1 ? undefined : end);
}

test("young install: hero swaps to measured 'so far', no /mo anywhere in the savings view", () => {
  const sv = render({ trackedDays: 4 });
  expect(sv).toContain("Saved so far &middot; 4 days tracked");
  expect(sv).toContain("$12.34");
  expect(sv).toContain("so far");
  expect(sv).toContain("capacity freed inside your limits, not cash refunded");
  expect(sv).not.toContain("/mo");
  expect(sv).not.toContain("/ month");
  expect(sv).not.toContain("sessions/mo");
  // Projected hero and cumulative card are suppressed under the guard.
  expect(sv).not.toContain("$1.8k");
  expect(sv).not.toContain("999");
  // A2 fix: under the guard the hero IS the floor, so the measured-floor card must
  // NOT describe the headline above as a "superset estimate" (it isn't one here).
  expect(sv).not.toContain("superset estimate");
  expect(sv).toContain("the same figure shown above");
});

test("mature install: run-rate / month hero restored, cumulative card present", () => {
  const sv = render({ trackedDays: 30 });
  expect(sv).toContain("/ month");
  expect(sv).toContain("$1.8k");
  expect(sv).toContain("/mo");
  expect(sv).toContain("$999.00");
  // The pre-existing cumulative card label has NO &middot; (distinguishes it from
  // the guarded young hero label).
  expect(sv).toContain("Saved so far");
  expect(sv).not.toContain("days tracked");
  expect(sv).toContain("capacity freed inside your limits, not cash refunded");
  // A2: run-rate keeps the original superset framing (hero IS the superset here).
  expect(sv).toContain("superset estimate");
});
