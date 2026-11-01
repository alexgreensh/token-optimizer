/**
 * Dashboard parity regression tests (OpenClaw adapter, v2.4.21 / Core v5.13.10).
 *
 * Covers three bounded parity gaps from the platform-parity audit:
 *   1. Persistent Core + adapter version labels in the nav (brand-meta).
 *   2. Honest static-dashboard regeneration instruction (no fake button/server).
 *   3. Windowed savings-events read so the action card shows actual Last N days
 *      measured/estimated action dollars, not a lifetime sum.
 *
 * Run: bun test src/dashboard.parity.test.ts
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { generateDashboardHtml } from "./dashboard.js";
import type { DashboardData } from "./dashboard.js";
import { readSavingsEventsByCategory } from "./savings.js";
import type { SavingsEventsWindow } from "./savings.js";

const DAY_MS = 86_400_000;

// ---------------------------------------------------------------------------
// Shared minimal DashboardData (mirrors dashboard.young-install.test.ts shape)
// ---------------------------------------------------------------------------

const baseData: DashboardData = {
  generatedAt: "2026-09-08T00:00:00.000Z",
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

// ---------------------------------------------------------------------------
// 1. Persistent version labels in the nav
// ---------------------------------------------------------------------------

test("nav brand-meta shows Core + OpenClaw adapter version labels", () => {
  const html = generateDashboardHtml(baseData);
  // The brand-meta element persists across all views (it is in the nav, not a
  // view), so it is the one version label visible on every tab.
  expect(html).toContain("brand-meta");
  expect(html).toContain("Core v5.13.10");
  expect(html).toContain("OpenClaw v2.4.21");
});

test("nav brand-meta reflects custom version values, not hardcoded", () => {
  const html = generateDashboardHtml({
    ...baseData,
    coreVersion: "9.9.99",
    adapterVersion: "3.3.33",
  });
  expect(html).toContain("Core v9.9.99");
  expect(html).toContain("OpenClaw v3.3.33");
  // The fallback defaults must NOT leak through when explicit values are given.
  expect(html).not.toContain("Core v5.13.10");
  expect(html).not.toContain("OpenClaw v2.4.21");
});

// ---------------------------------------------------------------------------
// 2. Honest static-dashboard regeneration instruction
// ---------------------------------------------------------------------------

test("sidebar shows static-dashboard regeneration instruction with CLI command", () => {
  const html = generateDashboardHtml(baseData);
  // Slice the sidebar/aside only to avoid false positives from elsewhere.
  const asideStart = html.indexOf("<aside");
  const asideEnd = html.indexOf("</aside>");
  expect(asideStart).toBeGreaterThan(-1);
  expect(asideEnd).toBeGreaterThan(asideStart);
  const aside = html.slice(asideStart, asideEnd);

  // Honest instruction: labels it static, names the CLI command.
  expect(aside).toContain("Static dashboard");
  expect(aside).toContain("static snapshot");

  // The regeneration instruction (regen-note-desc) must name the native CLI
  // command, not the published npx package (which resolves to an unrelated
  // repository). Slice the regen-note-desc to avoid false positives from the
  // Quick Commands list, which still shows the published npx invocations.
  const descStart = aside.indexOf('regen-note-desc">');
  expect(descStart).toBeGreaterThan(-1);
  const descEnd = aside.indexOf("</div>", descStart);
  expect(descEnd).toBeGreaterThan(descStart);
  const desc = aside.slice(descStart, descEnd);

  expect(desc).toContain("node dist/cli.js dashboard");
  expect(desc).toContain("openclaw source checkout");
  // No fake Regenerate button or POST endpoint.
  expect(desc).not.toContain("Regenerate");
  expect(desc.toLowerCase()).not.toContain("button");
  expect(desc).not.toContain("POST");
  expect(desc).not.toContain("/api/");
});

// ---------------------------------------------------------------------------
// 3. Windowed savings-events read (action card uses actual period, not lifetime)
// ---------------------------------------------------------------------------

let dir: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-parity-"));
  fs.mkdirSync(path.join(dir, "token-optimizer"), { recursive: true });
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function writeEvents(events: Array<Record<string, unknown>>): void {
  const lines = events.map((e) => JSON.stringify(e)).join("\n") + "\n";
  fs.writeFileSync(path.join(dir, "token-optimizer", "savings-events.jsonl"), lines);
}

test("readSavingsEventsByCategory with window excludes events older than the cutoff", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  // Recent event (5 days ago) — inside a 30-day window.
  const recent = {
    event_type: "tool_archive",
    timestamp: new Date(now - 5 * DAY_MS).toISOString(),
    tokens_saved: 1000,
    cost_saved_usd: 0.5,
  };
  // Old event (60 days ago) — outside a 30-day window.
  const old = {
    event_type: "tool_archive",
    timestamp: new Date(now - 60 * DAY_MS).toISOString(),
    tokens_saved: 5000,
    cost_saved_usd: 2.5,
  };
  writeEvents([recent, old]);

  const window: SavingsEventsWindow = { days: 30, now };
  const summary = readSavingsEventsByCategory(dir, window);

  // Only the recent event is counted.
  expect(summary.totalCount).toBe(1);
  expect(summary.totalTokensSaved).toBe(1000);
  expect(summary.totalCostSavedUsd).toBe(0.5);
});

test("readSavingsEventsByCategory without window sums all events (lifetime baseline)", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  const recent = {
    event_type: "tool_archive",
    timestamp: new Date(now - 5 * DAY_MS).toISOString(),
    tokens_saved: 1000,
    cost_saved_usd: 0.5,
  };
  const old = {
    event_type: "tool_archive",
    timestamp: new Date(now - 60 * DAY_MS).toISOString(),
    tokens_saved: 5000,
    cost_saved_usd: 2.5,
  };
  writeEvents([recent, old]);

  // No window = lifetime (the old behavior the action card used before this fix).
  const summary = readSavingsEventsByCategory(dir);

  expect(summary.totalCount).toBe(2);
  expect(summary.totalTokensSaved).toBe(6000);
  expect(summary.totalCostSavedUsd).toBe(3.0);
});

test("windowed read at the exact cutoff boundary includes the boundary event", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  // Event exactly 30 days ago — the cutoff is `now - days * DAY_MS`, and the
  // filter is `ts < cutoff` (strict), so a boundary event (ts == cutoff) is
  // INCLUDED.
  const boundary = {
    event_type: "resume_lean",
    timestamp: new Date(now - 30 * DAY_MS).toISOString(),
    tokens_saved: 200,
    cost_saved_usd: 0.1,
  };
  // Event 31 days ago — excluded.
  const justOutside = {
    event_type: "resume_lean",
    timestamp: new Date(now - 31 * DAY_MS).toISOString(),
    tokens_saved: 999,
    cost_saved_usd: 9.99,
  };
  writeEvents([boundary, justOutside]);

  const window: SavingsEventsWindow = { days: 30, now };
  const summary = readSavingsEventsByCategory(dir, window);

  expect(summary.totalCount).toBe(1);
  expect(summary.totalTokensSaved).toBe(200);
});

test("windowed read with undated events excludes them when a window is requested", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  // Missing timestamp — must be excluded under a window (cannot prove it is
  // inside the lookback), per the readSavingsEventsByCategory contract.
  const undated = {
    event_type: "hint_followed",
    tokens_saved: 500,
    cost_saved_usd: 0.25,
  };
  writeEvents([undated]);

  const window: SavingsEventsWindow = { days: 30, now };
  const summary = readSavingsEventsByCategory(dir, window);

  expect(summary.totalCount).toBe(0);
  expect(summary.totalTokensSaved).toBe(0);
});

test("windowed read with days=0 behaves as lifetime (no cutoff applied)", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  const old = {
    event_type: "checkpoint_restore",
    timestamp: new Date(now - 365 * DAY_MS).toISOString(),
    tokens_saved: 800,
    cost_saved_usd: 0.4,
  };
  writeEvents([old]);

  // days=0 disables the window (cutoff is null), so all events are summed.
  const window: SavingsEventsWindow = { days: 0, now };
  const summary = readSavingsEventsByCategory(dir, window);

  expect(summary.totalCount).toBe(1);
  expect(summary.totalTokensSaved).toBe(800);
});

test("unpriced archive actions disclose their signed priced subtotal", () => {
  const html = generateDashboardHtml({ ...baseData, savingsEvents: {
    categories: [], totalTokensSaved: 0, totalCostSavedUsd: 0, totalCount: 2,
    unpricedTokens: 1000000, unpricedEvents: 1, archivePricedNetUsd: -2,
  }});
  expect(html).toContain("action credits or reversals have no model price");
  expect(html).toContain("Priced archive subtotal: -$2.00");
});
