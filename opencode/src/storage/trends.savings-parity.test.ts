import { expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { TrendsStore } from "./trends.js";
import { knownModelRate } from "../pricing.js";

test("counted savings excludes fixtures, media, and twins while pricing unknowns from their own session", () => {
  const dir = join(process.cwd(), "..", ".scratch-parity", `ledger-${Date.now()}-${Math.random().toString(36).slice(2)}`);
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
  insert.run(at(-5000), "checkpoint_restore", 1000000, 3, "real", "same", "unknown");
  insert.run(at(-4000), "checkpoint_restore", 1000000, 3, "real", "same", "unknown");
  insert.run(at(0), "checkpoint_restore", 1000000, 3, "real", "same", "unknown");
  insert.run(at(0), "tool_archive", 9999999, 30, "real", "replaced mcp__claude-in-chrome__computer", "sonnet");
  insert.run(at(0), "checkpoint_restore", 9999999, 30, "test-session-42", "fixture", "sonnet");
  insert.run(at(0), "checkpoint_restore", 100, 3, "a123abc", "real subagent", "sonnet");
  insert.run(at(0), "verbosity_steer", 1000000, 3, "real", "estimated", "unknown");
  expect(store.getCompressionSavings(30, now).totalTokensSaved).toBe(2000100);
  expect(store.getCompressionSavings(30, now).totalCostSavedUsd).toBeCloseTo(11, 6);
  expect(store.getVerbositySavings(30, now)).toBeCloseTo(20, 6);
  expect(knownModelRate("opus-5-5", "input")).toBeCloseTo(4 / 1e6, 10);
  expect(knownModelRate("unknown", "input")).toBeNull();
  db.close();
  store.close();
});

test("fixture matching preserves real near matches and empty-session events", () => {
  const dir = join(process.cwd(), "..", ".scratch-parity", `fixtures-${Date.now()}-${Math.random().toString(36).slice(2)}`);
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
    insert.run(stamp, "checkpoint_restore", 100, 1, id, "same", "sonnet");
  }
  for (const id of ["test-project", "s", "codex-sess-live", "codex-sess-1-live", "a123abc", "", ""]) {
    insert.run(stamp, "checkpoint_restore", 100, 1, id, "same", "sonnet");
  }
  expect(store.getCompressionSavings(30, now).totalTokensSaved).toBe(700);
  db.close();
  store.close();
});

test("unknown events use their session's per-class model mix", () => {
  const dir = join(process.cwd(), "..", ".scratch-parity", `mix-${Date.now()}-${Math.random().toString(36).slice(2)}`);
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
  insert.run(stamp, "checkpoint_restore", 1_000_000, 1, "mixed", "x", "unknown");
  insert.run(stamp, "verbosity_steer", 1_000_000, 1, "mixed", "x", "unknown");
  const input = 1_000_000 * (knownModelRate("sonnet", "input")! / 4 + knownModelRate("opus-5-5", "input")! * 3 / 4);
  const output = 1_000_000 * (knownModelRate("sonnet", "output")! * 3 / 4 + knownModelRate("opus-5-5", "output")! / 4);
  expect(store.getCompressionSavings(30, now).totalCostSavedUsd).toBeCloseTo(input, 6);
  expect(store.getVerbositySavings(30, now)).toBeCloseTo(output, 6);
  expect(JSON.parse(String(store.getAllSessions()[0].model_token_buckets))).toEqual(buckets);
  db.close(); store.close();
});

test("partial and missing session model prices expose unpriced coverage", () => {
  const dir = join(process.cwd(), "..", ".scratch-parity", `partial-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  const store = new TrendsStore(dir);
  store.recordSession({ sessionId: "partial", project: null, model: "opus-5-5", modelTokenBuckets: {
    "opus-5-5": { input: 100, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 },
    "unpriced-model": { input: 900, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 },
  }, tokensInput: 1000, tokensOutput: 0, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0,
  resourceHealth: null, sessionEfficiency: null, toolCalls: 0, compactions: 0, mode: null, durationSeconds: 1 });
  store.logSavingsEvent("checkpoint_restore", 1_000_000, "partial", "partial");
  let result = store.getCompressionSavings();
  expect(result.totalCostSavedUsd).toBeCloseTo(0.4, 8);
  expect(result.unpricedTokens).toBe(900_000);
  expect(result.unpricedEvents).toBe(1);
  store.logSavingsEvent("checkpoint_restore", 1_000_000, "missing", "missing");
  result = store.getCompressionSavings();
  expect(result.totalTokensSaved).toBe(2_000_000);
  expect(result.unpricedTokens).toBe(1_900_000);
  expect(result.unpricedEvents).toBe(2);
  store.close();
});
