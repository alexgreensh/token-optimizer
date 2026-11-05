/**
 * Young-install savings guard tests (OpenCode engine, v5.11.79).
 *
 * Pins the `trackedDays` signal (floor of the after-window span) that drives the
 * dashboard's young-install guard: with < 30 days of post-baseline history the
 * "/mo" run-rate annualizes a tiny sample, so the dashboard swaps the hero to the
 * measured cumulative ("so far"). Also pins the raw-vs-scaled compression fields.
 *
 * Run: bun test src/savings.young-install.test.ts
 */
import { test, expect } from "bun:test";
import { computeRealizedSavings } from "./savings.js";

const DAY = 86_400_000;
const T0 = Date.parse("2026-01-01T00:00:00Z"); // install day

function row(tsMs: number, model: string, fi: number, cr: number, cw: number, out: number) {
  return {
    created_at: Math.floor(tsMs / 1000),
    date: new Date(tsMs).toISOString(),
    model,
    tokens_input: fi,
    tokens_cache_read: cr,
    tokens_cache_write: cw,
    tokens_output: out,
    cost_usd: 0,
    duration_seconds: 300, // 5 min — above the 60s quality gate
  };
}

// Fixed "before" (early-window) sessions: ~95% Opus, high cache reuse. An anchor
// row at T0 fixes installTs (early window = [T0+1d, T0+31d)); the block sits densely
// inside it. Identical in every scenario (frozen baseline).
function beforeRows() {
  const rows = [row(T0, "opus", 30_000, 13_500_000, 487_000, 51_000)]; // install anchor
  for (let i = 0; i < 35; i++) {
    const ts = T0 + 2 * DAY + i * 0.5 * DAY; // T0+2d .. ~T0+19.5d, all inside the window
    const model = i < 33 ? "opus" : "sonnet"; // ~95% Opus by session
    rows.push(row(ts, model, 30_000, 13_500_000, 487_000, 51_000));
  }
  return rows;
}

// `n` recent "after" sessions with a chosen per-session size, opus share, cache hit,
// and spacing. Sonnet-leaning (low opusShare) vs the 95%-Opus baseline guarantees a
// positive routing transformation (monthlySavingsUsd > 0).
const BASE_HIT = 13_500_000 / (13_500_000 + 30_000);

function afterRows(
  now: number,
  n: number,
  perInput: number,
  opusShare: number,
  spacingDays = 0.2,
  hit = BASE_HIT,
) {
  const rows = [];
  const cr = perInput * hit;
  const fi = perInput - cr;
  const cw = perInput * 0.03;
  const out = perInput * 0.01;
  for (let i = 0; i < n; i++) {
    const model = i < Math.round(n * opusShare) ? "opus" : "sonnet";
    rows.push(row(now - (i + 1) * DAY * spacingDays, model, fi, cr, cw, out));
  }
  return rows;
}

function run(
  now: number,
  days: number,
  after: ReturnType<typeof afterRows>,
  compressionOverride?: number,
) {
  return computeRealizedSavings(
    "/tmp/oc-young-install-test",
    days,
    now,
    [...beforeRows(), ...after],
    compressionOverride,
  );
}

test("young after-window: trackedDays is the after-window span, not install age", () => {
  // Day 33 post-install: after-window = [T0+31d, T0+33d] = 2 days.
  const NOW = T0 + 33 * DAY;
  const after = afterRows(NOW, 12, 4_000_000, 0.5, 0.15); // 12 rows over the last ~2 days
  const r = run(NOW, 30, after, 12.34);
  expect(r.ready).toBe(true);
  expect(r.trackedDays).toBeGreaterThanOrEqual(1);
  expect(r.trackedDays).toBeLessThanOrEqual(3);
  // Raw window sum (pre-monthly-scale); at days=30 the scaled field is identical.
  expect(r.compressionMeasuredWindowUsd).toBe(12.34);
  expect(r.compressionMeasuredUsd).toBe(12.34);
});

test("exactly 30 days of after-window keeps the run-rate (boundary)", () => {
  // now - 30d == windowEnd == T0+31d exactly.
  const NOW = T0 + 61 * DAY;
  const after = afterRows(NOW, 40, 4_000_000, 0.56, 0.5); // 40 rows over the last 20 days
  const r = run(NOW, 30, after);
  expect(r.ready).toBe(true);
  expect(r.trackedDays).toBe(30);
});

test("mature install: trackedDays == 30 at the default 30-day lookback", () => {
  const NOW = T0 + 200 * DAY;
  const after = afterRows(NOW, 40, 4_000_000, 0.56);
  const r = run(NOW, 30, after);
  expect(r.ready).toBe(true);
  expect(r.trackedDays).toBe(30);
});

test("small lookback on a mature install: trackedDays == days (parity with Python min(days, si.days))", () => {
  const NOW = T0 + 200 * DAY;
  const after = afterRows(NOW, 40, 4_000_000, 0.56);
  const r = run(NOW, 7, after);
  expect(r.ready).toBe(true);
  expect(r.trackedDays).toBe(7);
});

test("raw-vs-scaled pin: compressionMeasuredWindowUsd is the unscaled window sum", () => {
  // days=60 -> monthly scale = 30/60 = 0.5; the scaled field halves the raw sum.
  const NOW = T0 + 200 * DAY;
  const after = afterRows(NOW, 40, 4_000_000, 0.56);
  const r = run(NOW, 60, after, 60);
  expect(r.compressionMeasuredWindowUsd).toBe(60);
  expect(r.compressionMeasuredUsd).toBe(30);
});
