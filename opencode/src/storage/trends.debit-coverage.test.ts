import { tmpdir } from "node:os";
import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { join } from "node:path";
import { TrendsStore } from "./trends.js";
import { generateDashboard } from "../dashboard/generator.js";

function scenario(credit: string | null, debit: string | null, debitFirst = false) {
  const dir = mkdtempSync(join(tmpdir(), "to-parity-debit-"));
  const store = new TrendsStore(dir);
  const addCredit = () => { if (credit) store.logSavingsEvent("tool_archive", 1e6, "real-credit", "credit", credit); };
  const addDebit = () => { if (debit) store.logSavingsEvent("tool_archive_reexpand", 1e6, "real-debit", "debit", debit); };
  if (debitFirst) { addDebit(); addCredit(); } else { addCredit(); addDebit(); }
  const summary = store.getCompressionSavings();
  store.close();
  return { dir, summary };
}

test("unpriced archive reversals survive netting and are disclosed", () => {
  for (const debitFirst of [false, true]) {
    const { dir, summary } = scenario("opus", "unknown-future", debitFirst);
    expect(summary.totalTokensSaved).toBe(0);
    expect(summary.totalCostSavedUsd).toBe(5);
    expect(summary.unpricedTokens).toBe(1e6);
    expect(summary.unpricedEvents).toBe(1);
    expect(summary.archivePricedNetUsd).toBe(5);
    expect(generateDashboard({ dataDir: dir })).toContain("Priced archive subtotal: $5.00");
  }
});

test("unknown credits and debits never cancel while known zero is priced", () => {
  const both = scenario("unknown-future", "unknown-future").summary;
  expect(both.unpricedTokens).toBe(2e6);
  expect(both.unpricedEvents).toBe(2);
  const inverse = scenario("unknown-future", "sonnet").summary;
  expect(inverse.archivePricedNetUsd).toBe(-2);
  expect(inverse.unpricedTokens).toBe(1e6);
  const inversePage = scenario("unknown-future", "sonnet");
  expect(generateDashboard({ dataDir: inversePage.dir })).toContain("Priced archive subtotal: -$2.00");
  expect(scenario(null, "unknown-future").summary.unpricedEvents).toBe(1);
  const free = scenario("local", "local").summary;
  expect(free.unpricedTokens).toBe(0);
  expect(free.totalCostSavedUsd).toBe(0);
  expect(scenario("opus", "sonnet").summary.totalCostSavedUsd).toBe(3);
});

test("signed reversals keep gross coverage while fixtures, media and twins stay excluded", async () => {
  const { Database } = await import("bun:sqlite");
  const dir = mkdtempSync(join(tmpdir(), "to-parity-signed-debit-"));
  const store = new TrendsStore(dir);
  store.logSavingsEvent("tool_archive", 1e6, "real", "credit", "unknown-future");
  store.logSavingsEvent("tool_archive", 1e6, "real", "credit", "unknown-future");
  const db = new Database(join(dir, "trends.db"));
  const insert = db.prepare("INSERT INTO savings_events(timestamp,event_type,tokens_saved,cost_saved_usd,session_id,detail,model) VALUES (?,?,?,?,?,?,?)");
  const stamp = new Date().toISOString();
  insert.run(stamp, "tool_archive_reexpand", -1e6, 0, "other", "debit", "unknown-future");
  insert.run(stamp, "tool_archive", 1e6, 0, "test-session-1", "fixture", "unknown-future");
  insert.run(stamp, "tool_archive", 1e6, 0, "real2", "screenshot", "unknown-future");
  const summary = store.getCompressionSavings();
  expect(summary.totalTokensSaved).toBe(0);
  expect(summary.unpricedTokens).toBe(2e6);
  expect(summary.unpricedEvents).toBe(2);
  db.close(); store.close();
});
