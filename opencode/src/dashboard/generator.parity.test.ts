/**
 * Dashboard parity tests (OpenCode generator).
 *
 * Pins the three bounded-parity requirements from the platform-parity audit:
 *   1. Persistent version labels: Core v5.13.24 + Adapter v1.1.7 in the
 *      generated HTML (header + footer), independent and both visible.
 *   2. Static regeneration instruction: the dashboard tells the user to rerun
 *      the `token_dashboard` tool. No fake Regenerate button, no HTTP server,
 *      no fetch/XHR wiring.
 *   3. Action-savings card: uses the ACTUAL selected-period savings_events
 *      window sums (measured + estimated), never a monthly run-rate or lifetime
 *      figure mislabeled as "last 30 days". Measured and estimated are separate
 *      arms. Young-install guard swaps the period label to "N days tracked".
 *
 * Run: bun test src/dashboard/generator.parity.test.ts
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { generateDashboard } from "./generator.js";
import { computeRealizedSavings } from "../savings.js";

const DAY = 86_400_000;

const TRENDS_SCHEMA = `
CREATE TABLE IF NOT EXISTS session_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL UNIQUE,
  date TEXT NOT NULL,
  project TEXT,
  model TEXT,
  tokens_input INTEGER DEFAULT 0,
  tokens_output INTEGER DEFAULT 0,
  tokens_cache_read INTEGER DEFAULT 0,
  tokens_cache_write INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  resource_health REAL,
  session_efficiency REAL,
  tool_calls INTEGER DEFAULT 0,
  compactions INTEGER DEFAULT 0,
  mode TEXT,
  duration_seconds INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
`;

const SAVINGS_EVENTS_SCHEMA = `
CREATE TABLE IF NOT EXISTS savings_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  tokens_saved INTEGER DEFAULT 0,
  cost_saved_usd REAL DEFAULT 0.0,
  session_id TEXT,
  detail TEXT,
  model TEXT
);
`;

let dir: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-parity-"));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

// Baseline (early-window) session: Opus, high cache reuse — the frozen "old way".
function beforeRow(tsMs: number, model = "opus") {
  return {
    session_id: `s-${tsMs}`,
    date: new Date(tsMs).toISOString().split("T")[0],
    project: "p",
    model,
    tokens_input: 30_000,
    tokens_output: 51_000,
    tokens_cache_read: 13_500_000,
    tokens_cache_write: 487_000,
    cost_usd: 0,
    resource_health: 80,
    session_efficiency: 80,
    tool_calls: 10,
    compactions: 0,
    mode: "agent",
    duration_seconds: 300,
    created_at: Math.floor(tsMs / 1000),
  };
}

const BASE_HIT = 13_500_000 / (13_500_000 + 30_000);

function afterRow(tsMs: number, perInput: number, model: string) {
  const cr = perInput * BASE_HIT;
  const fi = perInput - cr;
  return {
    session_id: `s-${tsMs}`,
    date: new Date(tsMs).toISOString().split("T")[0],
    project: "p",
    model,
    tokens_input: Math.round(fi),
    tokens_output: Math.round(perInput * 0.01),
    tokens_cache_read: Math.round(cr),
    tokens_cache_write: Math.round(perInput * 0.03),
    cost_usd: 0,
    resource_health: 80,
    session_efficiency: 80,
    tool_calls: 10,
    compactions: 0,
    mode: "agent",
    duration_seconds: 300,
    created_at: Math.floor(tsMs / 1000),
  };
}

function seed(
  dir: string,
  now: number,
  anchorAgeDays: number,
  beforeCount: number,
  beforeSpreadDays: number,
  afterCount: number,
  afterSpacingDays: number,
  perInput: number,
  opusShare: number,
) {
  const db = new Database(path.join(dir, "trends.db"), { create: true });
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(TRENDS_SCHEMA);
  db.exec(SAVINGS_EVENTS_SCHEMA);

  const insertSession = db.prepare(
    `INSERT INTO session_log (session_id, date, project, model, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write, cost_usd, resource_health, session_efficiency, tool_calls, compactions, mode, duration_seconds, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  const insertEvent = db.prepare(
    `INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, model, detail)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  );

  const anchorTs = now - anchorAgeDays * DAY;
  insertSession.run(...Object.values(beforeRow(anchorTs)));

  for (let i = 0; i < beforeCount; i++) {
    const ts = anchorTs + DAY + i * (beforeSpreadDays / beforeCount) * DAY;
    const model = i < Math.round(beforeCount * 0.95) ? "opus" : "sonnet";
    insertSession.run(...Object.values(beforeRow(ts, model)));
  }

  for (let i = 0; i < afterCount; i++) {
    const ts = now - (i + 1) * afterSpacingDays * DAY;
    const model = i < Math.round(afterCount * opusShare) ? "opus" : "sonnet";
    insertSession.run(...Object.values(afterRow(ts, perInput, model)));
  }

  // 3 measured tool_archive events -> 3 * 4.11 = 12.33 (window sum).
  for (let i = 0; i < 3; i++) {
    insertEvent.run(
      new Date(now - i * DAY).toISOString(),
      "tool_archive",
      1000,
      4.11,
      `evt-${i}`,
      "sonnet",
      "test",
    );
  }

  db.close();
}

function addVerbosityEvents(dir: string, now: number, count: number, costEach: number) {
  const db = new Database(path.join(dir, "trends.db"));
  const insertEvent = db.prepare(
    `INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, model, detail)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  );
  for (let i = 0; i < count; i++) {
    insertEvent.run(
      new Date(now - i * DAY).toISOString(),
      "verbosity_steer",
      500,
      costEach,
      `vs-${i}`,
      "sonnet",
      "test",
    );
  }
  db.close();
}

// ---------------------------------------------------------------------------
// 1. Version labels
// ---------------------------------------------------------------------------

test("version labels: Core v5.13.24 and Adapter v1.1.7 both present in header and footer", () => {
  const now = Date.now();
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  const html = generateDashboard({ dataDir: dir });

  // Header (persistent across all views)
  expect(html).toContain("Core v5.13.24");
  expect(html).toContain("Adapter v1.1.7");
  // Footer (second persistence point)
  const footerStart = html.indexOf("oc-footer");
  const footerHtml = html.slice(footerStart);
  expect(footerHtml).toContain("Core v5.13.24");
  expect(footerHtml).toContain("Adapter v1.1.7");
});

test("version labels are independent: adapter does not echo the core number", () => {
  const now = Date.now();
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  const html = generateDashboard({ dataDir: dir });

  // The adapter label must show 1.1.7, NOT 5.13.24 (no copy-paste of the core
  // version into the adapter slot). Target the header sub-line via its class
  // selector — the <title> tag also contains "OpenCode Dashboard" but is
  // followed by </title> immediately, so a bare /OpenCode Dashboard[^<]*/
  // regex matches the title with zero trailing chars and misses the labels.
  const headerLine = html.match(/class="sub">OpenCode Dashboard[^<]*/);
  expect(headerLine).toBeTruthy();
  expect(headerLine![0]).toContain("Adapter v1.1.7");
  expect(headerLine![0]).not.toContain("Adapter v5.13.24");
});

// ---------------------------------------------------------------------------
// 2. Regeneration instruction (static, no button, no HTTP)
// ---------------------------------------------------------------------------

test("regeneration instruction: tells user to run token_dashboard, no button or HTTP server", () => {
  const now = Date.now();
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  const html = generateDashboard({ dataDir: dir });

  // The instruction names the tool.
  expect(html).toContain("token_dashboard");
  expect(html).toContain("to refresh");

  // No fake Regenerate button (the canonical has one backed by a daemon POST;
  // OpenCode's dashboard is static, so we must NOT fake one).
  expect(html).not.toContain("Regenerate");
  expect(html).not.toContain("regen-btn");
  expect(html).not.toContain("onclick");

  // No HTTP server / fetch / XHR wiring for regeneration.
  expect(html).not.toContain("XMLHttpRequest");
  expect(html).not.toContain("__TOKEN_REGENERATE");
  // The only fetch in the page is the GitHub star count (api.github.com), which
  // is unrelated to regeneration. Confirm no regeneration-related fetch.
  const fetchMatches = html.match(/fetch\([^)]*\)/g) ?? [];
  for (const f of fetchMatches) {
    expect(f).toContain("api.github.com");
  }
});

// ---------------------------------------------------------------------------
// 3. Action-savings card: window sums, measured/estimated split, period label
// ---------------------------------------------------------------------------

test("action-savings card: uses window sums (not monthly run-rate), separates measured and estimated", () => {
  const now = Date.now();
  // Mature install: trackedDays >= 30, runRate = true.
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  // Add 2 verbosity_steer events at $2.00 each -> estimatedWindow = 4.00.
  addVerbosityEvents(dir, now, 2, 2.0);

  const html = generateDashboard({ dataDir: dir });

  // Card title with the mature period label.
  expect(html).toContain("Action savings");
  expect(html).toContain("last 30 days");

  // Measured arm: 3 tool_archive * $4.11 = $12.33 (window sum, NOT monthly-scaled).
  // The monthly-scaled figure would be $12.33 (30/30 = 1x at days=30), so we
  // also verify with a non-30 day window below to prove it's the window sum.
  expect(html).toContain("Logged actions");
  expect(html).toContain("$12.33");

  // Estimated arm: 2 verbosity * $2.00 = $4.00 (window sum).
  expect(html).toContain("Other estimates");
  expect(html).toContain("~$4.00");

  // Repeat-read coverage is unavailable (no counted_reread table on OpenCode).
  expect(html).toContain("Repeat reads avoided");
  expect(html).toContain("unavailable");

  // Total = 12.33 + 4.00 = 16.33 (window sum).
  expect(html).toContain("$16.33");

  // The card must NOT show "/mo" — it is a period total, not a run-rate.
  const cardStart = html.indexOf("Action savings");
  const cardEnd = html.indexOf("Tokens Saved", cardStart);
  const cardHtml = html.slice(cardStart, cardEnd > 0 ? cardEnd : html.length);
  expect(cardHtml).not.toContain("/mo");
});

test("action-savings card: window sum proven with non-30-day window (not monthly-scaled)", () => {
  const now = Date.now();
  // Mature install with days=15. The monthly-scaled compression would be
  // $12.33 * (30/15) = $24.66, but the action-savings card must show the
  // ACTUAL window sum $12.33, not the monthly projection.
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);

  const html = generateDashboard({ dataDir: dir, days: 15 });

  // The action-savings card shows the window sum, not the monthly-scaled figure.
  const cardStart = html.indexOf("Action savings");
  expect(cardStart).toBeGreaterThan(0);
  const cardEnd = html.indexOf("Tokens Saved", cardStart);
  const cardHtml = html.slice(cardStart, cardEnd > 0 ? cardEnd : html.length);
  // Window sum is $12.33; monthly-scaled would be $24.66.
  expect(cardHtml).toContain("$12.33");
  expect(cardHtml).not.toContain("$24.66");
  // With days=15, trackedDays=15 < 30, so the young-install guard shows the
  // honest "15 days tracked" label (NOT a run-rate "last 15 days").
  expect(cardHtml).toContain("15 days tracked");
  expect(cardHtml).not.toContain("last 15 days");
});

test("action-savings card: young-install shows 'days tracked' not 'last 30 days', no /mo", () => {
  const now = Date.now();
  // Install 33d ago -> after-window = 2 days -> trackedDays = 2, runRate = false.
  seed(dir, now, 33, 35, 29, 12, 0.15, 4_000_000, 0.0);
  const html = generateDashboard({ dataDir: dir });

  // The action-savings card renders with the young-install period label.
  expect(html).toContain("Action savings");
  expect(html).toContain("2 days tracked");
  // Slice the RENDERED card via its data-card marker, not the string
  // "Action savings" — the HTML comment documenting the card also contains
  // that phrase and "last 30 days" as a design note, confounding a
  // source-string slice.
  const cardStart = html.indexOf('data-card="action-savings"');
  expect(cardStart).toBeGreaterThan(0);
  const cardEnd = html.indexOf("Tokens Saved", cardStart);
  const cardHtml = html.slice(cardStart, cardEnd > 0 ? cardEnd : html.length);
  expect(cardHtml).not.toContain("last 30 days");
  expect(cardHtml).not.toContain("/mo");
  // Window sum is still $12.33 (the actual period total).
  expect(cardHtml).toContain("$12.33");
});

test("action-savings card: fail-open when no savings_events (mature install, zero events)", () => {
  const now = Date.now();
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  // Delete all savings_events so measured + estimated = 0.
  const db = new Database(path.join(dir, "trends.db"));
  db.exec("DELETE FROM savings_events");
  db.close();

  const html = generateDashboard({ dataDir: dir });
  // Card must NOT render (fail-open: $0 total). Assert via the unique
  // data-card marker — the HTML comment documenting the card still mentions
  // "Action savings" even when the card itself is absent.
  expect(html).not.toContain('data-card="action-savings"');
});

test("action-savings card: absent when baseline is still building (not ready)", () => {
  const now = Date.now();
  // Only 5 before-rows -> baseline not ready (< 30 min stable sessions).
  const db = new Database(path.join(dir, "trends.db"), { create: true });
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(TRENDS_SCHEMA);
  db.exec(SAVINGS_EVENTS_SCHEMA);
  const insertSession = db.prepare(
    `INSERT INTO session_log (session_id, date, project, model, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write, cost_usd, resource_health, session_efficiency, tool_calls, compactions, mode, duration_seconds, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  const anchorTs = now - 5 * DAY;
  insertSession.run(...Object.values(beforeRow(anchorTs)));
  for (let i = 0; i < 5; i++) {
    insertSession.run(...Object.values(beforeRow(anchorTs + DAY + i * DAY)));
  }
  db.close();

  const html = generateDashboard({ dataDir: dir });
  // Baseline is building -> savings.ready = false -> action-savings card absent.
  expect(html).not.toContain("Action savings");
  // The baseline-building card should be visible instead.
  expect(html).toContain("baseline is still building");
});

// ---------------------------------------------------------------------------
// 4. savings.ts: verbosityMeasuredWindowUsd field
// ---------------------------------------------------------------------------

test("computeRealizedSavings exposes verbosityMeasuredWindowUsd as the raw window sum", () => {
  const now = Date.now();
  // Mature install.
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  addVerbosityEvents(dir, now, 3, 1.5); // 3 * 1.5 = 4.5 window sum

  const savings = computeRealizedSavings(dir, 30, now);
  expect(savings.ready).toBe(true);
  // Raw window sum (before 30/days monthly scaling).
  expect(savings.verbosityMeasuredWindowUsd).toBeCloseTo(4.5, 2);
  // Monthly-scaled (30/30 = 1x at days=30, so equal here).
  expect(savings.verbosityMeasuredUsd).toBeCloseTo(4.5, 2);
});

test("computeRealizedSavings: verbosityMeasuredWindowUsd differs from monthly-scaled at non-30-day window", () => {
  const now = Date.now();
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  addVerbosityEvents(dir, now, 3, 1.5); // window sum = 4.5

  // days=15: monthlyScale = 30/15 = 2, so monthly = 4.5 * 2 = 9.0.
  const savings = computeRealizedSavings(dir, 15, now);
  expect(savings.ready).toBe(true);
  expect(savings.verbosityMeasuredWindowUsd).toBeCloseTo(4.5, 2);
  expect(savings.verbosityMeasuredUsd).toBeCloseTo(9.0, 2);
});

test("computeRealizedSavings: verbosityMeasuredWindowUsd is 0 when not ready", () => {
  const now = Date.now();
  // Only 5 before-rows -> not ready.
  const db = new Database(path.join(dir, "trends.db"), { create: true });
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(TRENDS_SCHEMA);
  db.exec(SAVINGS_EVENTS_SCHEMA);
  const insertSession = db.prepare(
    `INSERT INTO session_log (session_id, date, project, model, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write, cost_usd, resource_health, session_efficiency, tool_calls, compactions, mode, duration_seconds, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  const anchorTs = now - 5 * DAY;
  insertSession.run(...Object.values(beforeRow(anchorTs)));
  for (let i = 0; i < 5; i++) {
    insertSession.run(...Object.values(beforeRow(anchorTs + DAY + i * DAY)));
  }
  db.close();

  const savings = computeRealizedSavings(dir, 30, now);
  expect(savings.ready).toBe(false);
  expect(savings.verbosityMeasuredWindowUsd).toBe(0);
});
