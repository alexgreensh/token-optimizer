import { tmpdir } from "node:os";
import { expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { TrendsStore } from "./trends.js";
import { knownModelRate } from "../pricing.js";

test("counted savings excludes fixtures, media, and twins while pricing unknowns from their own session", () => {
  const dir = join(tmpdir(), `to-parity-ledger-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  store.recordSession({ sessionId: "real", project: null, model: "opus-5-5", tokensInput: 1,
    tokensOutput: 0, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0,
    resourceHealth: null, sessionEfficiency: null, toolCalls: 0, compactions: 0,
    mode: null, durationSeconds: 1 });
  const db = new Database(join(dir, "trends.db"));
  const insert = db.prepare(`INSERT INTO savings_events
    (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model) VALUES (?, ?, ?, ?, ?, ?, ?)`);
  const now = Date.now();
  const at = (ms: number) => new Date(now + ms).toISOString();
  // tool_archive is the measured vehicle; checkpoint_restore verifies the
  // estimated-tier relocation (Python parity: _get_savings_summary pops it).
  insert.run(at(-5000), "tool_archive", 1000000, 3, "real", "replaced bash (40k chars)", "unknown");
  insert.run(at(-4000), "tool_archive", 1000000, 3, "real", "replaced bash (40k chars)", "unknown");
  insert.run(at(0), "tool_archive", 1000000, 3, "real", "replaced bash (40k chars)", "unknown");
  insert.run(at(0), "tool_archive", 9999999, 30, "real", "replaced mcp__claude-in-chrome__computer", "sonnet");
  insert.run(at(0), "tool_archive", 100, 3, "a123abc", "replaced bash", "sonnet");
  insert.run(at(0), "verbosity_steer", 1000000, 3, "real", "estimated", "unknown");
  insert.run(at(0), "checkpoint_restore", 777777, 2.5, "real", "restored from auto", "unknown");
  insert.run(at(0), "resume_lean", 50000, 0.15, "real", "lean resume", "sonnet");
  insert.run(at(0), "hint_followed", 8000, 0.02, "real", "hint->read: auth.py", "sonnet");
  expect(store.getCompressionSavings(30, now).totalTokensSaved).toBe(2000100);
  expect(store.getCompressionSavings(30, now).totalCostSavedUsd).toBeCloseTo(11, 6);
  expect(store.getVerbositySavings(30, now)).toBeCloseTo(20, 6);
  // The counterfactual credits are NOT in the measured total; they surface in
  // the estimated pool instead (same session-mix pricing as the old path).
  const estCf = store.getEstimatedCounterfactualSavings(30, now);
  expect(estCf.byType.checkpoint_restore?.tokens).toBe(777777);
  expect(estCf.byType.checkpoint_restore?.events).toBe(1);
  expect(estCf.byType.resume_lean?.tokens).toBe(50000);
  expect(estCf.byType.hint_followed?.tokens).toBe(8000);
  expect(estCf.totalTokensSaved).toBe(777777 + 50000 + 8000);
  expect(knownModelRate("opus-5-5", "input")).toBeCloseTo(4 / 1e6, 10);
  expect(knownModelRate("unknown", "input")).toBeNull();
  db.close();
  store.close();
});

test("fixture matching preserves real near matches and empty-session events", () => {
  const dir = join(tmpdir(), `to-parity-fixtures-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  store.recordSession({ sessionId: "real", project: null, model: "sonnet", tokensInput: 1,
    tokensOutput: 0, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0,
    resourceHealth: null, sessionEfficiency: null, toolCalls: 0, compactions: 0,
    mode: null, durationSeconds: 1 });
  const db = new Database(join(dir, "trends.db"));
  const insert = db.prepare(`INSERT INTO savings_events
    (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model) VALUES (?, ?, ?, ?, ?, ?, ?)`);
  const now = Date.now();
  const stamp = new Date(now).toISOString();
  for (const id of ["test-session-1", "failwire-1", "f3bench-sess1", "unit-c-never1", "torture-1", "sess_test1", "codex-sess-123"]) {
    insert.run(stamp, "tool_archive", 100, 1, id, "replaced bash", "sonnet");
  }
  for (const id of ["test-project", "s", "codex-sess-live", "codex-sess-1-live", "a123abc", "", ""]) {
    insert.run(stamp, "tool_archive", 100, 1, id, "replaced bash", "sonnet");
  }
  expect(store.getCompressionSavings(30, now).totalTokensSaved).toBe(700);
  db.close();
  store.close();
});

test("unknown events use their session's per-class model mix", () => {
  const dir = join(tmpdir(), `to-parity-mix-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  const buckets = { sonnet: { input: 100, output: 300, cacheRead: 0, cacheWrite: 0, cost: 0 },
    "opus-5-5": { input: 300, output: 100, cacheRead: 0, cacheWrite: 0, cost: 0 } };
  store.recordSession({ sessionId: "mixed", project: null, model: "sonnet", modelTokenBuckets: buckets,
    tokensInput: 400, tokensOutput: 400, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0,
    resourceHealth: null, sessionEfficiency: null, toolCalls: 0, compactions: 0, mode: null, durationSeconds: 1 });
  const db = new Database(join(dir, "trends.db"));
  const insert = db.prepare(`INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model) VALUES (?, ?, ?, ?, ?, ?, ?)`);
  const now = Date.now(), stamp = new Date(now).toISOString();
  insert.run(stamp, "tool_archive", 1_000_000, 1, "mixed", "replaced bash", "unknown");
  insert.run(stamp, "verbosity_steer", 1_000_000, 1, "mixed", "estimated", "unknown");
  const input = 1_000_000 * (knownModelRate("sonnet", "input")! / 4 + knownModelRate("opus-5-5", "input")! * 3 / 4);
  const output = 1_000_000 * (knownModelRate("sonnet", "output")! * 3 / 4 + knownModelRate("opus-5-5", "output")! / 4);
  expect(store.getCompressionSavings(30, now).totalCostSavedUsd).toBeCloseTo(input, 6);
  expect(store.getVerbositySavings(30, now)).toBeCloseTo(output, 6);
  expect(JSON.parse(String(store.getAllSessions()[0].model_token_buckets))).toEqual(buckets);
  db.close(); store.close();
});

test("partial and missing session model prices expose unpriced coverage", () => {
  const dir = join(tmpdir(), `to-parity-partial-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  store.recordSession({ sessionId: "partial", project: null, model: "opus-5-5", modelTokenBuckets: {
    "opus-5-5": { input: 100, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 },
    "unpriced-model": { input: 900, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 },
  }, tokensInput: 1000, tokensOutput: 0, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0,
  resourceHealth: null, sessionEfficiency: null, toolCalls: 0, compactions: 0, mode: null, durationSeconds: 1 });
  store.logSavingsEvent("tool_archive", 1_000_000, "partial", "replaced bash", "partial");
  let result = store.getCompressionSavings();
  expect(result.totalCostSavedUsd).toBeCloseTo(0.4, 8);
  expect(result.unpricedTokens).toBe(900_000);
  expect(result.unpricedEvents).toBe(1);
  store.logSavingsEvent("tool_archive", 1_000_000, "missing", "replaced bash", "missing");
  result = store.getCompressionSavings();
  expect(result.totalTokensSaved).toBe(2_000_000);
  expect(result.unpricedTokens).toBe(1_900_000);
  expect(result.unpricedEvents).toBe(2);
  store.close();
});

test("platform exclusion vectors preserve unattributed actions", () => {
  const dir = join(tmpdir(), `to-parity-vector-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  store.recordSession({ sessionId: "real", project: null, model: "sonnet", tokensInput: 1, tokensOutput: 0,
    tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0, resourceHealth: null, sessionEfficiency: null,
    toolCalls: 0, compactions: 0, mode: null, durationSeconds: 1 });
  const db = new Database(join(dir, "trends.db"));
  const insert = db.prepare(`INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model) VALUES (?, ?, ?, ?, ?, ?, ?)`);
  const now = Date.now(), a = new Date(now - 1000).toISOString(), b = new Date(now).toISOString();
  insert.run(a, "tool_archive", 100, 1, "real", "replaced bash", "sonnet");
  insert.run(b, "tool_archive", 100, 2, "real", "replaced bash", "sonnet");
  insert.run(b, "tool_archive", 100, 1, "real", "replaced mcp__playwright__browser_take_screenshot", "sonnet");
  insert.run(b, "tool_archive", 100, 1, "", "replaced bash", "sonnet");
  insert.run(b, "tool_archive", 100, 2, "", "replaced bash", "sonnet");
  const result = store.getCompressionSavings(30, now);
  expect(result.totalEvents).toBe(3);
  expect(result.totalTokensSaved).toBe(300);
  expect((db.query("SELECT COUNT(*) AS n FROM savings_events").get() as { n: number }).n).toBe(5);
  db.close(); store.close();
});

test("an unknown legacy model remains unpriced in the event reader", () => {
  const dir = join(tmpdir(), `to-parity-legacy-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  store.recordSession({ sessionId: "legacy", project: null, model: "claude-3-9-sonnet", tokensInput: 100,
    tokensOutput: 0, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0, resourceHealth: null,
    sessionEfficiency: null, toolCalls: 0, compactions: 0, mode: null, durationSeconds: 1 });
  store.logSavingsEvent("tool_archive", 1_000_000, "legacy", "legacy generation", "claude-3-9-sonnet");
  const result = store.getCompressionSavings();
  expect(result.totalTokensSaved).toBe(1_000_000);
  expect(result.totalCostSavedUsd).toBe(0);
  expect(result.unpricedTokens).toBe(1_000_000);
  expect(result.unpricedEvents).toBe(1);
  store.close();
});
