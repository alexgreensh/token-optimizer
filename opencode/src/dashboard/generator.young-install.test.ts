/**
 * Young-install dashboard guard tests (OpenCode generator, v5.11.79).
 *
 * Fabricates a real trends.db in a temp dir and asserts the generated HTML
 * swaps the "/mo" run-rate hero to the measured "so far" figure when the
 * after-window is < 30 days, and restores the run-rate on a mature install.
 *
 * Run: bun test src/dashboard/generator.young-install.test.ts
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { generateDashboard } from "./generator.js";

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
  daily_usage_json TEXT,
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
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-dash-"));
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

// Recent "after" session: Sonnet-leaning, same cache-hit ratio so the routing
// lever (Opus -> Sonnet) drives a positive transformation.
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

function seed(dir: string, now: number, anchorAgeDays: number, beforeCount: number, beforeSpreadDays: number, afterCount: number, afterSpacingDays: number, perInput: number, opusShare: number) {
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

  // Install anchor fixes installTs; early window = [installTs+1d, installTs+31d).
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

  // 3 measured tool_archive events -> 3 * 4.11 = 12.33 (the "so far" / floor sum).
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

  db.run(`UPDATE session_log SET daily_usage_json = json_object(date, json_object(model, json_object(
    'input',tokens_input,'output',tokens_output,'cacheRead',tokens_cache_read,'cacheWrite',tokens_cache_write,'cost',cost_usd)))`);
  db.close();
}

test("young install: hero swaps to measured 'so far', no /mo anywhere in the savings view", () => {
  const now = Date.now();
  // Install 33d ago -> after-window = [now-2d, now] = 2 days -> trackedDays = 2.
  seed(dir, now, 33, 35, 29, 12, 0.15, 4_000_000, 0.0);
  const html = generateDashboard({ dataDir: dir });

  expect(html).toContain("Saved so far &middot;");
  expect(html).toContain("days tracked");
  expect(html).toContain(" so far");
  expect(html).toContain("$12.33");
  expect(html).toContain("capacity freed inside your limits, not cash refunded");
  // Every "/mo" emitter (hero suffix, sessions/mo, Est. $/month, per-lever /mo,
  // measured-floor /mo) is inside a guarded block. "sessions/mo" and "Est. $/month"
  // both contain the substring "/mo"; both are suppressed under the guard.
  expect(html).not.toContain("/mo");
  expect(html).not.toContain("Est. $/month");
  // A2 fix: under the guard the hero IS the floor, so the measured-floor copy must
  // NOT call it a "subset of the transformation estimate above".
  expect(html).not.toContain("subset of the transformation estimate above");
  expect(html).toContain("the same figure shown above");
});

test("mature install: run-rate /mo hero restored, 'so far' wording absent", () => {
  const now = Date.now();
  // Install 200d ago; 40 after-rows over the last 24d -> trackedDays = 30.
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  const html = generateDashboard({ dataDir: dir });

  expect(html).toContain("/mo");
  expect(html).toContain("Est. $/month");
  expect(html).toContain("Saved to date");
  expect(html).not.toContain(" so far");
  expect(html).not.toContain("days tracked");
  expect(html).toContain("capacity freed inside your limits, not cash refunded");
  // A2: run-rate keeps the original subset framing (hero IS the superset here).
  expect(html).toContain("subset of the transformation estimate above");
});

test("Tokens Saved card renders from live session_log + savings_events data", () => {
  const now = Date.now();
  // Mature install so savings.ready=true. The seed inserts 3 tool_archive
  // savings_events with tokens_saved=1000 each -> SAVED = 3000 tokens.
  // The 40 after-rows carry real token volumes -> SPENT > 0.
  // We add extra savings_events to get a meaningful percentage for assertions.
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);

  // Add 20 more tool_archive events at 500K tokens each -> +10M tokens.
  // Total SAVED = 3000 (from seed) + 10_000_000 = 10_003_000.
  const db2 = new Database(path.join(dir, "trends.db"));
  const insertEvent = db2.prepare(
    `INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, model, detail)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  );
  for (let i = 0; i < 20; i++) {
    insertEvent.run(new Date(now - i * DAY).toISOString(), "tool_archive", 500_000, 1.0, `extra-${i}`, "sonnet", "test");
  }
  db2.close();

  const html = generateDashboard({ dataDir: dir });

  // --- Exact computed values ---
  // Fresh input + output, excluding cache reads and writes.
  const SPENT = 1_954_760;
  const SAVED = 10_003_000;
  const TOTAL = SPENT + SAVED;
  const SAVED_PCT = Math.round(SAVED / TOTAL * 100);
  const SPENT_PCT = 100 - SAVED_PCT;
  function fmtNum(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
    return String(Math.round(n));
  }

  // Card header + hero
  expect(html).toContain("Tokens Saved");
  expect(html).toContain("what never had to be sent");
  expect(html).toContain("tokens saved");
  // Hero shows the EXACT SAVED figure
  expect(html).toContain(`>${fmtNum(SAVED)}</`);
  // Sub-line with the EXACT percentage
  expect(html).toContain(`≈${SAVED_PCT}%`);
  expect(html).toContain("of your total token load");
  // Spent vs Saved bar — EXACT segment strings and widths
  expect(html).toContain(`spent · ${fmtNum(SPENT)}`);
  expect(html).toContain(`saved · ${fmtNum(SAVED)}`);
  expect(html).toContain(`width:${SPENT_PCT}%`);
  expect(html).toContain(`width:${SAVED_PCT}%`);
  // Would-be total
  expect(html).toContain(`without Token Optimizer: ≈${fmtNum(TOTAL)}`);
  expect(html).toContain(`have sent <strong style="color:var(--text);font-weight:500">≈${fmtNum(TOTAL)}</strong>`);
  // Model-mix-as-usage section, relabeled with the SPENT figure
  expect(html).toContain(`Where the ${fmtNum(SPENT)} you spent went`);
  expect(html).toContain("which models do the work");
  // Footer
  expect(html).toContain("Metered floor.");
  expect(html).toContain("no per-token cost");
  // The card must not introduce "/mo" into its own section. The card now
  // renders in BOTH the Overview and Savings views; slice the FIRST card
  // (Overview) from "Tokens Saved" to "Daily Activity" (the next section
  // after the Overview card) and verify the card's own markup is clean.
  // (The savings-view hero and measured-floor card both legitimately carry
  // "/mo"; only the token-denominated card must not.)
  const cardStart = html.indexOf("Tokens Saved");
  const cardEnd = html.indexOf("Daily Activity");
  const cardHtml = html.slice(cardStart, cardEnd);
  expect(cardHtml).not.toContain("/mo");
  // The card must not emit the estimated-slice footer sentence (opencode has
  // no token-denominated estimated tier). Assert the actual omitted phrase.
  expect(cardHtml).not.toContain("A further");
  expect(cardHtml).not.toContain("not</b> included here");
});

test("Tokens Saved card is absent on a mature install with zero savings_events", () => {
  const now = Date.now();
  // Mature install: seed a full baseline (anchor + 35 before-rows + 40 after-rows)
  // but then DELETE all savings_events so SAVED = 0 while SPENT > 0.
  // This isolates the SAVED=0 fail-open path from the baseline-not-ready path.
  seed(dir, now, 200, 35, 29, 40, 0.6, 4_000_000, 0.56);
  const db2 = new Database(path.join(dir, "trends.db"));
  db2.exec("DELETE FROM savings_events");
  db2.close();

  const html = generateDashboard({ dataDir: dir });
  // Card must NOT render (fail-open: SAVED = 0 despite mature baseline).
  expect(html).not.toContain("Tokens Saved");
  expect(html).not.toContain("what never had to be sent");
  // The savings VIEW should still render (baseline is mature, just no compression events).
  // The card now lives in BOTH the Overview and Savings views, so its absence
  // confirms the SAVED=0 fail-open path.
  expect(html).toContain("id=\"view-savings\"");
});
