/**
 * Estimated-tier savings parity tests (OpenClaw adapter).
 *
 * Mirrors the Python `_get_savings_summary` relocations and the OpenCode
 * trends.ts parity tests: checkpoint_restore, resume_lean and hint_followed
 * are counterfactual credits -- the trigger is observed, the magnitude is
 * inferred -- so they must NEVER appear in the measured compression total and
 * must surface only through the estimated counterfactual pool.
 *
 * Run: bun test src/savings-estimated-tier.test.ts
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  estimatedCounterfactualUsd,
  measuredSavingsFromSummary,
  readSavingsEventsByCategory,
  savingsCategoryLabel,
} from "./savings.js";
import type { SavingsEventsWindow } from "./savings.js";

const DAY_MS = 86_400_000;
const NOW = Date.parse("2026-09-08T00:00:00Z");

let dir: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-est-tier-"));
  fs.mkdirSync(path.join(dir, "token-optimizer"), { recursive: true });
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function writeEvents(events: Array<Record<string, unknown>>): void {
  const lines = events.map((e) => JSON.stringify(e)).join("\n") + "\n";
  fs.writeFileSync(path.join(dir, "token-optimizer", "savings-events.jsonl"), lines);
}

function ev(eventType: string, tokens: number, usd: number, daysAgo = 5) {
  return {
    event_type: eventType,
    timestamp: new Date(NOW - daysAgo * DAY_MS).toISOString(),
    tokens_saved: tokens,
    cost_saved_usd: usd,
  };
}

test("measuredSavingsFromSummary excludes checkpoint_restore, resume_lean and hint_followed", () => {
  writeEvents([
    ev("tool_archive", 1000, 0.5),
    ev("checkpoint_restore", 800, 0.4),
    ev("resume_lean", 600, 0.3),
    ev("hint_followed", 400, 0.2),
    ev("verbosity_steer", 300, 0.15),
    ev("setup_optimization", 200, 0.1),
    ev("mcp_cap", 100, 0.05),
    ev("fresh_session_nudge", 50, 0.02),
  ]);
  const window: SavingsEventsWindow = { days: 30, now: NOW };
  const summary = readSavingsEventsByCategory(dir, window);
  // All types still surface in the raw category listing (observed triggers).
  expect(summary.categories.length).toBe(8);
  // ...but the measured total counts only the directly-metered tool_archive.
  expect(measuredSavingsFromSummary(summary)).toBeCloseTo(0.5, 6);
});

test("estimatedCounterfactualUsd returns the three counterfactual types, grouped", () => {
  writeEvents([
    ev("tool_archive", 1000, 0.5),
    ev("checkpoint_restore", 800, 0.4),
    ev("resume_lean", 600, 0.3),
    ev("hint_followed", 400, 0.2),
    ev("checkpoint_restore", 200, 0.1, 10),
  ]);
  const window: SavingsEventsWindow = { days: 30, now: NOW };
  const est = estimatedCounterfactualUsd(dir, window);
  expect(est.total).toBeCloseTo(1.0, 6);
  expect(est.byType["checkpoint_restore"]).toBeCloseTo(0.5, 6);
  expect(est.byType["resume_lean"]).toBeCloseTo(0.3, 6);
  expect(est.byType["hint_followed"]).toBeCloseTo(0.2, 6);
  // Measured-only types never leak into the estimated pool.
  expect(est.byType["tool_archive"]).toBeUndefined();
  expect(est.byType["verbosity_steer"]).toBeUndefined();
});

test("estimated counterfactual respects the lookback window", () => {
  writeEvents([
    ev("checkpoint_restore", 800, 0.4, 5),
    ev("checkpoint_restore", 900, 0.45, 60), // outside the 30-day window
  ]);
  const window: SavingsEventsWindow = { days: 30, now: NOW };
  const est = estimatedCounterfactualUsd(dir, window);
  expect(est.total).toBeCloseTo(0.4, 6);
});

test("counterfactual category labels are marked estimated", () => {
  expect(savingsCategoryLabel("checkpoint_restore")).toContain("[est]");
  expect(savingsCategoryLabel("resume_lean")).toContain("[est]");
  expect(savingsCategoryLabel("hint_followed")).toContain("[est]");
  // Directly-metered types stay unmarked.
  expect(savingsCategoryLabel("tool_archive")).not.toContain("[est]");
});
