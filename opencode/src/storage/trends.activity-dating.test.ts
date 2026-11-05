import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { TrendsStore, type SessionTrendData } from "./trends.js";
import * as usage from "../usage.js";

const day = (ago: number) => new Date(Date.now() - ago * 86400000).toISOString().slice(0, 10);
const bucket = (input: number) => ({ input, output: input / 10, cacheRead: 0, cacheWrite: 0, cost: input / 1e6 });
const session = (extra: Partial<SessionTrendData> = {}): SessionTrendData => ({
  sessionId: "sample", project: null, model: "sonnet", tokensInput: 11000,
  tokensOutput: 1100, tokensCacheRead: 0, tokensCacheWrite: 0, costUsd: 0.011,
  resourceHealth: 80, sessionEfficiency: 70, toolCalls: 5, compactions: 0,
  mode: null, durationSeconds: 60, ...extra,
});
const store = () => new TrendsStore(mkdtempSync(join(tmpdir(), "to-activity-")));

test("recording old activity today never moves it into the window", () => {
  const db = store();
  db.recordSession(session({ dailyUsage: { [day(70)]: { sonnet: bucket(10000) } } }));
  expect(db.getRecentSessions(30)).toHaveLength(0);
  db.recordSession(session({ dailyUsage: { [day(70)]: { sonnet: bucket(10000) } } }));
  expect(db.getDailyStats(30)).toHaveLength(0);
  db.close();
});

test("a multi-day session contributes only its in-window model buckets", () => {
  const db = store();
  db.recordSession(session({ dailyUsage: {
    [day(70)]: { sonnet: bucket(10000) }, [day(1)]: { opus: bucket(1000) },
  } }));
  expect(db.getRecentSessions(30)[0]?.tokens_input).toBe(1000);
  expect(db.getRecentSessions(30)[0]?.model).toBe("opus");
  expect(db.getDailyStats(30)[0]?.total_input).toBe(1000);
  expect(db.getAllSessions()).toHaveLength(2);
  db.close();
});

test("undated usage is disclosed and never dated at recording time", () => {
  const db = store();
  db.recordSession(session());
  expect(db.getRecentSessions()).toHaveLength(0);
  expect(db.getActivityCoverage().complete).toBe(false);
  db.close();
});

test("streamed observations retain their request activity day", () => {
  const messages = new Map();
  const timestamp = Date.now() - 86400000;
  for (let i = 0; i < 3; i++) messages.set("one-request", { model: "sonnet", ...bucket(1000), activityAt: timestamp });
  expect(usage.aggregateDailyUsage(messages.values())).toEqual({ [day(1)]: { sonnet: bucket(1000) } });
});
