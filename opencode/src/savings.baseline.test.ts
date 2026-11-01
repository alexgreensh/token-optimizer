/**
 * Baseline-savings stability tests (frozen-baseline port, v5.11.18).
 *
 * Proves the headline fix on the OpenCode engine: the pre-TO baseline
 * ("old way / session") is a FROZEN, factual anchor that does NOT move when the
 * current period's workload volume changes; "now / session" moves only with
 * efficiency (model mix + cache reuse). Mirrors the Python suite.
 *
 * Run: bun test src/savings.baseline.test.ts
 */
import { test, expect } from "bun:test";
import { computeRealizedSavings } from "./savings.js";

const DAY = 86_400_000;
const T0 = Date.parse("2026-01-01T00:00:00Z"); // install day
const NOW = T0 + 200 * DAY; // well past the 31-day early window

function row(tsMs: number, model: string, fi: number, cr: number, cw: number, out: number) {
  return {
    created_at: Math.floor(tsMs / 1000),
    model,
    tokens_input: fi,
    tokens_cache_read: cr,
    tokens_cache_write: cw,
    tokens_output: out,
    cost_usd: 0,
    duration_seconds: 300, // 5 min — above the 60s quality gate
  };
}

// Fixed "before" (early-window) sessions: ~95% Opus, high cache reuse. This is the
// frozen baseline — identical in every scenario below. An anchor row at T0 fixes
// installTs (early window = [T0+1d, T0+31d)); the block sits densely inside it.
function beforeRows() {
  const rows = [row(T0, "opus", 30_000, 13_500_000, 487_000, 51_000)]; // install anchor
  for (let i = 0; i < 35; i++) {
    const ts = T0 + 2 * DAY + i * 0.5 * DAY; // T0+2d .. ~T0+19.5d, all inside the window
    const model = i < 33 ? "opus" : "sonnet"; // ~95% Opus by session
    rows.push(row(ts, model, 30_000, 13_500_000, 487_000, 51_000));
  }
  return rows;
}

// `n` recent "after" sessions with a chosen per-session size, opus share, and cache hit.
// hit is set by the fi:cr split; size scales both while holding the ratio (so curHit is
// constant across sizes — isolating volume from efficiency).
// Default current cache-hit = the baseline cohort's native hit (13.5M/(13.5M+30k)),
// so the caching lever is ~0 and routing (Opus-share) is isolated. Drop it below this
// and a caching regression can legitimately cancel the routing win (tested separately).
const BASE_HIT = 13_500_000 / (13_500_000 + 30_000);

function afterRows(n: number, perInput: number, opusShare: number, hit = BASE_HIT) {
  const rows = [];
  const cr = perInput * hit;
  const fi = perInput - cr;
  const cw = perInput * 0.03;
  const out = perInput * 0.01;
  // Spread across the last ~24 days (inside the [NOW-30d, NOW] current window) so every
  // after row counts regardless of n (n up to 120 at 0.2d spacing = 24d).
  for (let i = 0; i < n; i++) {
    const model = i < Math.round(n * opusShare) ? "opus" : "sonnet";
    rows.push(row(NOW - (i + 1) * DAY * 0.2, model, fi, cr, cw, out));
  }
  return rows;
}

function run(after: ReturnType<typeof afterRows>) {
  return computeRealizedSavings("/tmp/oc-baseline-test", 30, NOW, [...beforeRows(), ...after]);
}

test("baseline is frozen across different current volumes", () => {
  const light = run(afterRows(40, 2_000_000, 0.56));
  const heavy = run(afterRows(40, 10_000_000, 0.56));
  expect(light.ready).toBe(true);
  expect(heavy.ready).toBe(true);
  // The old-way per-session baseline must not move when current volume 5x's.
  expect(heavy.beforeCostPerSession).toBeCloseTo(light.beforeCostPerSession, 8);
  // "now" depends on mix + cache-hit (held constant here), not volume -> also stable.
  expect(heavy.afterCostPerSession).toBeCloseTo(light.afterCostPerSession, 8);
  expect(heavy.savingsPerSession).toBeCloseTo(light.savingsPerSession, 8);
});

test("baseline frozen across session count; monthly scales with count", () => {
  // opusShare 0.5 is exact at both counts (6/12 == 60/120), so the realized mix — and
  // thus afterCps — is identical; only the count differs. (0.56 would round differently.)
  const few = run(afterRows(12, 4_000_000, 0.5));
  const many = run(afterRows(120, 4_000_000, 0.5));
  expect(few.beforeCostPerSession).toBeCloseTo(many.beforeCostPerSession, 8);
  expect(few.savingsPerSession).toBeCloseTo(many.savingsPerSession, 8);
  // 10x the sessions -> ~10x the monthly transformation (per-session anchor is fixed).
  const ratio = many.monthlySavingsUsd / Math.max(1e-9, few.monthlySavingsUsd);
  expect(ratio).toBeGreaterThan(9);
  expect(ratio).toBeLessThan(11);
});

test("now reacts to model mix; old way does not", () => {
  const lean = run(afterRows(40, 4_000_000, 0.2));
  const heavyOpus = run(afterRows(40, 4_000_000, 0.9));
  expect(lean.beforeCostPerSession).toBeCloseTo(heavyOpus.beforeCostPerSession, 8);
  // More Opus now -> costs more now -> less saved per session.
  expect(heavyOpus.afterCostPerSession).toBeGreaterThan(lean.afterCostPerSession);
  expect(heavyOpus.savingsPerSession).toBeLessThan(lean.savingsPerSession);
});

test("savings positive for a 95%-Opus baseline vs a 56%-Opus now", () => {
  const r = run(afterRows(40, 4_000_000, 0.56));
  expect(r.beforeCostPerSession).toBeGreaterThan(r.afterCostPerSession);
  expect(r.afterCostPerSession).toBeGreaterThan(0);
  expect(r.savingsPerSession).toBeGreaterThan(0);
});

test("provider cache reuse changes do not become optimizer savings", () => {
  const cold = run(afterRows(40, 2_000_000, 0.56, 0.3));
  const warm = run(afterRows(40, 2_000_000, 0.56, 0.8));
  expect(cold.ready).toBe(true);
  expect(warm.ready).toBe(true);
  expect(cold.monthlySavingsUsd).toBeCloseTo(warm.monthlySavingsUsd, 8);
  expect(warm.breakdown.some((item) => item.key === "context_rereads")).toBe(false);
});

test("model distributions are weighted separately for each token class", () => {
  const recent = (flipped: boolean) => Array.from({ length: 20 }, (_, i) => {
    const opus = i % 2 === 0;
    const inputHigh = opus !== flipped;
    return row(NOW - (i + 1) * DAY, opus ? "opus-5-5" : "sonnet",
      inputHigh ? 190_000 : 10_000, 0, 0, inputHigh ? 10_000 : 190_000);
  });
  const a = run(recent(false));
  const b = run(recent(true));
  expect(a.ready).toBe(true);
  expect(b.ready).toBe(true);
  expect(b.afterCostPerSession).toBeGreaterThan(a.afterCostPerSession);
});

test("premium usage is disclosed without masking the eligible model mix", () => {
  const baseline = Array.from({ length: 36 }, (_, i) =>
    row(T0 + (i === 0 ? 0 : 2 + (i - 1) / 2) * DAY, "opus-5-5", 100_000, 0, 0, 100_000));
  const recent = Array.from({ length: 20 }, (_, i) => ({
    ...row(NOW - (i + 1) * DAY, "sonnet", 200_000, 0, 0, 200_000),
    model_token_buckets: JSON.stringify({
      sonnet: { input: 100_000, output: 100_000, cacheRead: 0, cacheWrite: 0 },
      fable: { input: 100_000, output: 100_000, cacheRead: 0, cacheWrite: 0 },
    }),
  }));
  const s = computeRealizedSavings("", 30, NOW, [...baseline, ...recent]);
  expect(s.ready).toBe(true);
  expect(s.monthlySavingsUsd).toBeGreaterThan(0);
  expect(s.premiumTokens).toBeGreaterThan(0);
  expect(s.premiumCostUsd).toBeGreaterThan(0);
});

test("unknown baseline tokens do not inflate priced savings", () => {
  const original = beforeRows();
  const mixed = original.map((r) => ({ ...r, tokens_input: r.tokens_input * 10, tokens_output: r.tokens_output * 10,
    model_token_buckets: JSON.stringify({
      [r.model]: { input: r.tokens_input, output: r.tokens_output, cacheRead: r.tokens_cache_read, cacheWrite: r.tokens_cache_write },
      "unpriced-model": { input: r.tokens_input * 9, output: r.tokens_output * 9, cacheRead: 0, cacheWrite: 0 },
    }) }));
  const recent = afterRows(40, 2_000_000, 0.5);
  const a = computeRealizedSavings("", 30, NOW, [...original, ...recent]);
  const b = computeRealizedSavings("", 30, NOW, [...mixed, ...recent]);
  expect(b.beforeCostPerSession).toBeCloseTo(a.beforeCostPerSession, 8);
  expect(b.monthlySavingsUsd).toBeCloseTo(a.monthlySavingsUsd, 8);
  expect(b.unpricedBaselineTokens).toBeGreaterThan(0);
});

test("premium usage added to identical eligible work does not reduce savings", () => {
  const baseline = Array.from({ length: 36 }, (_, i) =>
    row(T0 + (i === 0 ? 0 : 2 + (i - 1) / 2) * DAY, "opus-5-5", 100_000, 0, 0, 100_000));
  const recent = (premium: boolean) => Array.from({ length: 20 }, (_, i) => ({
    ...row(NOW - (i + 1) * DAY, "sonnet", premium ? 200_000 : 100_000, 0, 0, premium ? 200_000 : 100_000),
    model_token_buckets: JSON.stringify({ sonnet: { input: 100_000, output: 100_000, cacheRead: 0, cacheWrite: 0 },
      ...(premium ? { fable: { input: 100_000, output: 100_000, cacheRead: 0, cacheWrite: 0 } } : {}) }),
  }));
  const a = computeRealizedSavings("", 30, NOW, [...baseline, ...recent(false)]);
  const b = computeRealizedSavings("", 30, NOW, [...baseline, ...recent(true)]);
  expect(a.monthlySavingsUsd).toBeGreaterThan(0);
  expect(b.monthlySavingsUsd).toBeCloseTo(a.monthlySavingsUsd, 8);
  expect(b.sessionsPerMonth).toBe(a.sessionsPerMonth);
});

test("premium-only sessions do not dilute cumulative savings", () => {
  const baseline = Array.from({ length: 36 }, (_, i) =>
    row(T0 + (i === 0 ? 0 : 2 + (i - 1) / 2) * DAY, "opus-5-5", 100_000, 0, 0, 100_000));
  const oldEligible = Array.from({ length: 20 }, (_, i) => row(NOW - (60 + i) * DAY, "sonnet-5-5", 100_000, 0, 0, 100_000));
  const currentEligible = Array.from({ length: 20 }, (_, i) => row(NOW - (i + 1) * DAY, "sonnet-5-5", 100_000, 0, 0, 100_000));
  const currentPremium = Array.from({ length: 10 }, (_, i) => row(NOW - (i + 1) * DAY, "fable-5-1", 100_000, 0, 0, 100_000));
  const oldPremium = Array.from({ length: 10 }, (_, i) => row(NOW - (90 + i) * DAY, "fable-5-1", 100_000, 0, 0, 100_000));
  const base = computeRealizedSavings("", 30, NOW, [...baseline, ...oldEligible, ...currentEligible]);
  const current = computeRealizedSavings("", 30, NOW, [...baseline, ...oldEligible, ...currentEligible, ...currentPremium]);
  const historical = computeRealizedSavings("", 30, NOW, [...baseline, ...oldEligible, ...currentEligible, ...oldPremium]);
  expect(base.monthlySavingsUsd).toBeCloseTo(24, 8);
  expect(base.cumulativeSavedUsd).toBeCloseTo(48, 8);
  expect(current.cumulativeSavedUsd).toBeCloseTo(base.cumulativeSavedUsd, 8);
  expect(historical.cumulativeSavedUsd).toBeCloseTo(base.cumulativeSavedUsd, 8);
});
