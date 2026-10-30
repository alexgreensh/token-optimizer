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
