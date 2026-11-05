import { Database } from "bun:sqlite";
import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { knownModelRate } from "../pricing.js";
import { aggregateModelUsage, type ModelTokenBuckets } from "../usage.js";

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

// Keep the ledger intact; all savings readers use the same filtered view.
const COUNTED_EVENTS_VIEW = `
CREATE TEMP VIEW IF NOT EXISTS counted_savings_events AS
SELECT e.* FROM main.savings_events e
WHERE NOT (e.session_id IS NOT NULL AND (
  e.session_id GLOB 'test-session-*' OR e.session_id GLOB 'failwire-*' OR
  e.session_id GLOB 'f3bench-sess*' OR e.session_id GLOB 'unit-c-never*' OR
  e.session_id GLOB 'torture-*' OR e.session_id GLOB 'sess_test*' OR
  (e.session_id GLOB 'codex-sess-[0-9]*' AND substr(e.session_id, 12) NOT GLOB '*[^0-9]*')
))
AND NOT (e.event_type IN ('tool_archive', 'tool_archive_refetch_block') AND e.detail IS NOT NULL AND (
  e.detail GLOB '*claude-in-chrome__computer*' OR e.detail GLOB '*computer_use__*' OR
  e.detail GLOB '*[Ss]creenshot*'
))
AND NOT (e.tokens_saved > 0 AND e.session_id IS NOT NULL AND e.session_id != '' AND EXISTS (
  SELECT 1 FROM main.savings_events p WHERE p.session_id = e.session_id
  AND p.event_type = e.event_type AND p.tokens_saved = e.tokens_saved
  AND COALESCE(p.detail, '') = COALESCE(e.detail, '')
  AND (p.timestamp < e.timestamp OR (p.timestamp = e.timestamp AND p.id < e.id))
  AND (julianday(e.timestamp) - julianday(p.timestamp)) * 86400.0 BETWEEN 0 AND 3
));`;

/** Billing classes a savings event's avoided tokens can be priced at. */
export type SavingsEventClass = "input" | "output" | "cacheRead" | "cacheWrite";
const SAVINGS_EVENT_CLASSES = new Set<string>(["input", "output", "cacheRead", "cacheWrite"]);
function savingsEventClass(value: unknown): SavingsEventClass | undefined {
  return typeof value === "string" && SAVINGS_EVENT_CLASSES.has(value) ? value as SavingsEventClass : undefined;
}

function sessionRate(model: string | null, encoded: string | null, klass: SavingsEventClass): { rate: number; coverage: number } {
  try {
    const buckets = JSON.parse(encoded ?? "null") as ModelTokenBuckets | null;
    let weighted = 0, total = 0;
    if (buckets && typeof buckets === "object") {
      for (const [name, bucket] of Object.entries(buckets)) {
        const rate = knownModelRate(name, klass);
        const tokens = Math.max(0, Number(bucket?.[klass]) || 0);
        if (Number.isFinite(tokens)) { weighted += tokens * (rate ?? 0); total += tokens; }
      }
      if (total > 0) {
        const known = Object.entries(buckets).reduce((sum, [name, bucket]) => {
          const tokens = Math.max(0, Number(bucket?.[klass]) || 0);
          return sum + (Number.isFinite(tokens) && knownModelRate(name, klass) !== null ? tokens : 0);
        }, 0);
        return { rate: weighted / total, coverage: known / total };
      }
      if (Object.keys(buckets).length > 0) return { rate: 0, coverage: 0 };
    }
  } catch { /* old or invalid bucket data uses the recorded session model */ }
  const rate = knownModelRate(model ?? "", klass);
  return { rate: rate ?? 0, coverage: rate === null ? 0 : 1 };
}

export interface SessionTrendData {
  sessionId: string;
  project: string | null;
  model: string | null;
  modelTokenBuckets?: ModelTokenBuckets;
  dailyUsage?: Record<string, ModelTokenBuckets>;
  tokensInput: number;
  tokensOutput: number;
  tokensCacheRead: number;
  tokensCacheWrite: number;
  costUsd: number;
  resourceHealth: number | null;
  sessionEfficiency: number | null;
  toolCalls: number;
  compactions: number;
  mode: string | null;
  durationSeconds: number;
}

export class TrendsStore {
  private db: Database | null = null;
  private dbPath: string;

  constructor(dataDir: string) {
    const trendsDir = dataDir;
    if (!existsSync(trendsDir)) {
      mkdirSync(trendsDir, { recursive: true });
    }
    this.dbPath = join(trendsDir, "trends.db");
  }

  private connect(): Database {
    if (!this.db) {
      this.db = new Database(this.dbPath, { create: true });
      this.db.exec("PRAGMA journal_mode=WAL");
      this.db.exec("PRAGMA busy_timeout=3000");
      this.db.exec(TRENDS_SCHEMA);
      this.db.exec(SAVINGS_EVENTS_SCHEMA);
      const cols = this.db.query("PRAGMA table_info(session_log)").all() as Array<{ name: string }>;
      if (!cols.some((col) => col.name === "model_token_buckets")) {
        this.db.exec("ALTER TABLE session_log ADD COLUMN model_token_buckets TEXT");
      }
      for (const name of ["daily_usage_json", "first_activity_day", "last_activity_day"]) {
        if (!cols.some((col) => col.name === name)) this.db.exec(`ALTER TABLE session_log ADD COLUMN ${name} TEXT`);
      }
      const eventCols = this.db.query("PRAGMA table_info(savings_events)").all() as Array<{ name: string }>;
      if (!eventCols.some((col) => col.name === "token_class")) {
        this.db.exec("ALTER TABLE savings_events ADD COLUMN token_class TEXT");
      }
      this.db.exec(COUNTED_EVENTS_VIEW);
    }
    return this.db;
  }

  /**
   * Log a realized-savings event to the savings_events table.
   *
   * This is the TypeScript equivalent of Python's `_log_savings_event`.
   * If the model is not yet known, leave the amount unpriced until the session
   * has a model. The reader resolves it from that session without changing history.
   *
   * Guards:
   *   - tokensSaved <= 0  → no-op (never credit zero or negative)
   *   - Any exception     → silently swallowed (must never break the caller)
   */
  logSavingsEvent(
    eventType: string,
    tokensSaved: number,
    sessionId: string | null,
    detail: string | null,
    model: string | null = null,
    tokenClass?: string,
  ): void {
    if (tokensSaved <= 0) return;
    try {
      const db = this.connect();
      const klass = savingsEventClass(tokenClass) ?? (eventType === "verbosity_steer" ? "output" : "input");
      // A class the provider does not bill (zero rate) reverts to the input
      // rate: on such a card new context is charged as fresh input.
      let rate = knownModelRate(model ?? "", klass);
      if (rate === 0 && klass !== "input") rate = knownModelRate(model ?? "", "input");
      const costSavedUsd = tokensSaved * (rate ?? 0);
      db.run(
        `INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model, token_class)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          new Date().toISOString(),
          eventType,
          tokensSaved,
          costSavedUsd,
          sessionId ?? null,
          detail ?? null,
          model ?? null,
          savingsEventClass(tokenClass) ?? null,
        ],
      );
    } catch {
      // Best-effort: never crash the caller over savings tracking
    }
  }

  /**
   * True if a savings event of the given type for the given target session was
   * already credited within the specified window.
   *
   * Prevents double-counting when a user opens the same cold session from two
   * different fresh sessions (cross-session dedup). Mirrors Python's
   * `_resume_lean_already_credited` which dedups on the TARGET session_uuid.
   *
   * Best-effort: returns false on any error so we never block savings accounting.
   */
  hasRecentSavingsEvent(
    eventType: string,
    sessionId: string,
    withinMs: number,
  ): boolean {
    if (!sessionId || withinMs <= 0) return false;
    try {
      const db = this.connect();
      const cutoff = new Date(Date.now() - withinMs).toISOString();
      const row = db.query(
        `SELECT 1 FROM savings_events
         WHERE event_type = ?
           AND session_id = ?
           AND timestamp >= ?
         LIMIT 1`,
      ).get(eventType, sessionId, cutoff);
      return row !== null;
    } catch {
      return false;
    }
  }

  /**
   * Returns the tokens_cache_write value from session_log for the given session,
   * or 0 if not found / unavailable.
   *
   * This is the closest opencode equivalent to Python's
   * `cache_create_1h_tokens + cache_create_5m_tokens` — the real cold-rewrite
   * cost that a lean resume avoids. Used by logResumeLeanSavings for the
   * primary avoided-cost estimate.
   *
   * OVERCOUNT VERIFICATION: tokens_cache_write is populated from
   * `t?.cache?.write` in the message.updated handler, which maps directly to
   * OpenCode SDK's `Message.tokens.cache.write` field. Per the SDK type definition
   * (types.gen.d.ts L120), `cache.write` is the cache CREATION count only —
   * distinct from `cache.read` (cheap hits). A `--resume` cold rewrite ONLY incurs
   * write (creation) cost; read hits on an established cache do NOT re-pay write cost.
   * Therefore tokens_cache_write is write-only and does NOT conflate cache-read tokens.
   * No discount needed: this column is the correct, non-overcounting avoided-cost metric.
   *
   * Best-effort: returns 0 on any error.
   */
  getSessionCacheWrite(sessionId: string): number {
    if (!sessionId) return 0;
    try {
      const db = this.connect();
      const row = db.query(
        `SELECT tokens_cache_write FROM session_log
         WHERE session_id = ?
         ORDER BY created_at DESC
         LIMIT 1`,
      ).get(sessionId) as { tokens_cache_write: number } | null;
      return (row?.tokens_cache_write ?? 0) > 0 ? row!.tokens_cache_write : 0;
    } catch {
      return 0;
    }
  }

  /**
   * Summarize compression/volume-reduction savings_events over the window for
   * the realized-savings compression add-back (pool #3).
   *
   * Mirrors Python's `_get_savings_summary`: the returned `totalCostSavedUsd` is
   * the MEASURED compression floor with estimated-tier categories relocated OUT
   * (setup_optimization, mcp_cap, hint_followed, verbosity_steer,
   * checkpoint_restore, resume_lean) and tool_archive re-expansions
   * NETTED against tool_archive (a re-popped result didn't stay collapsed, so its
   * eager credit is reversed, floored at 0). What remains is the directly-metered
   * removed-token dollars the old way would have re-read.
   *
   * Best-effort: returns zeros on any error (never throws). `now` is injectable
   * for testing.
   */
  getCompressionSavings(days: number = 30, now: number = Date.now()): {
    totalTokensSaved: number;
    totalCostSavedUsd: number;
    totalEvents: number;
    archivePricedNetUsd: number | null;
    unpricedTokens: number;
    unpricedEvents: number;
    inputRateFloorTokens: number;
    inputRateFloorUsd: number;
  } {
    try {
      const db = this.connect();
      const cutoff = new Date(now - days * 86_400_000).toISOString();
      const rows = db
        .query(
          `SELECT e.event_type, e.tokens_saved, e.cost_saved_usd, e.model, e.token_class, s.model AS session_model, s.model_token_buckets
           FROM counted_savings_events e LEFT JOIN session_log s ON s.session_id = e.session_id
           WHERE e.timestamp >= ?`,
        )
        .all(cutoff) as Array<{ event_type: string; tokens_saved: number; cost_saved_usd: number; model: string | null; token_class: string | null; session_model: string | null; model_token_buckets: string | null }>;

      const byCategory = new Map<string, { events: number; tokens: number; cost: number; unpricedTokens: number; unpricedEvents: number; floorTokens: number; floorUsd: number }>();
      for (const r of rows) {
        const item = byCategory.get(r.event_type) ?? { events: 0, tokens: 0, cost: 0, unpricedTokens: 0, unpricedEvents: 0, floorTokens: 0, floorUsd: 0 };
        // A stored billed class prices the event at that class; without one the
        // input rate is an honest floor (disclosed via the floor totals).
        const klass = savingsEventClass(r.token_class) ?? "input";
        const unknown = !r.model || knownModelRate(r.model, klass) === null;
        const rate = unknown ? sessionRate(r.session_model, r.model_token_buckets, klass) : { rate: 0, coverage: 1 };
        item.events++;
        const tokens = r.tokens_saved ?? 0;
        const cost = unknown ? tokens * rate.rate : (r.cost_saved_usd ?? 0);
        const absCost = r.event_type === "tool_archive_reexpand" ? Math.abs(cost) : cost;
        item.tokens += r.event_type === "tool_archive_reexpand" ? Math.abs(tokens) : tokens;
        item.cost += absCost;
        item.unpricedTokens += Math.abs(tokens) * (1 - rate.coverage);
        if (rate.coverage < 1 && tokens !== 0) item.unpricedEvents++;
        if (r.token_class === null || r.token_class === undefined) {
          item.floorUsd += absCost;
          item.floorTokens += Math.abs(tokens) * (unknown ? rate.coverage : 1);
        }
        byCategory.set(r.event_type, item);
      }

      // Relocate estimated-tier categories OUT of the measured total (A1/A3/U-G
      // in measure.py: setup_optimization is a one-time trim double-counted by
      // structural_savings; mcp_cap and hint_followed are observed-trigger but
      // estimated-magnitude; verbosity_steer output reduction is estimated not
      // metered; checkpoint_restore credits avoided working-set re-reads that
      // never happened, and resume_lean credits an avoided cold reload -- both
      // inferred magnitudes, never observed removals). They never belong in
      // the measured realized total; the counterfactual three are surfaced via
      // getEstimatedCounterfactualSavings instead.
      for (const k of ["setup_optimization", "mcp_cap", "hint_followed", "verbosity_steer", "checkpoint_restore", "resume_lean"]) {
        byCategory.delete(k);
      }

      // B6: net tool_archive re-expansions against tool_archive (re-popped
      // results didn't stay collapsed), floored at 0. The debit is not its own line.
      const reexpand = byCategory.get("tool_archive_reexpand");
      const archive = byCategory.get("tool_archive");
      const archivePricedNetUsd = archive || reexpand ? (archive?.cost ?? 0) - (reexpand?.cost ?? 0) : null;
      if (reexpand) {
        byCategory.delete("tool_archive_reexpand");
        const ta = byCategory.get("tool_archive");
        if (ta) {
          ta.tokens = Math.max(0, ta.tokens - reexpand.tokens);
          ta.cost = Math.max(0, ta.cost - reexpand.cost);
          // The input-rate floor is the part of the NET counted amount that has no
          // class evidence: archive floor less re-expansion floor, never above it.
          ta.floorUsd = Math.min(ta.cost, Math.max(0, ta.floorUsd - reexpand.floorUsd));
          ta.floorTokens = Math.min(ta.tokens, Math.max(0, ta.floorTokens - reexpand.floorTokens));
        }
      }

      let totalTokensSaved = 0;
      let totalCostSavedUsd = 0;
      let totalEvents = 0;
      // Coverage is gross: uncertain credits and reversals cannot cancel.
      let unpricedTokens = reexpand?.unpricedTokens ?? 0, unpricedEvents = reexpand?.unpricedEvents ?? 0;
      let floorTokens = 0, floorUsd = 0;
      for (const v of byCategory.values()) {
        totalTokensSaved += v.tokens;
        totalCostSavedUsd += v.cost;
        totalEvents += v.events;
        unpricedTokens += v.unpricedTokens;
        unpricedEvents += v.unpricedEvents;
        floorTokens += v.floorTokens;
        floorUsd += v.floorUsd;
      }
      return { totalTokensSaved, totalCostSavedUsd, totalEvents, archivePricedNetUsd, unpricedTokens, unpricedEvents, inputRateFloorTokens: floorTokens, inputRateFloorUsd: floorUsd };
    } catch {
      return { totalTokensSaved: 0, totalCostSavedUsd: 0, totalEvents: 0, archivePricedNetUsd: null, unpricedTokens: 0, unpricedEvents: 0, inputRateFloorTokens: 0, inputRateFloorUsd: 0 };
    }
  }

  /**
   * Sum the ESTIMATED verbosity_steer savings (cost_saved_usd) over the window.
   * These are estimated output-token reductions from lean-output conciseness nudges —
   * the trigger is observed but the magnitude is not metered. Mirrors measure.py
   * `_get_savings_summary` which relocates verbosity_steer to the estimated tier.
   * The caller reprices to the baseline OUTPUT rate and adds as a separate pool.
   *
   * Best-effort: returns 0 on any error (never throws). `now` is injectable.
   */
  getVerbositySavings(days: number = 30, now: number = Date.now()): number {
    return this.getVerbositySavingsDetail(days, now).costUsd;
  }

  getVerbositySavingsDetail(days: number = 30, now: number = Date.now()): { costUsd: number; unpricedTokens: number; unpricedEvents: number } {
    try {
      const db = this.connect();
      const cutoff = new Date(now - days * 86_400_000).toISOString();
      const rows = db
        .query(
          `SELECT e.tokens_saved, e.cost_saved_usd, e.model, e.token_class, s.model AS session_model, s.model_token_buckets
           FROM counted_savings_events e LEFT JOIN session_log s ON s.session_id = e.session_id
           WHERE e.timestamp >= ? AND e.event_type = 'verbosity_steer'`,
        )
        .all(cutoff) as Array<{ tokens_saved: number; cost_saved_usd: number; model: string | null; token_class: string | null; session_model: string | null; model_token_buckets: string | null }>;
      let costUsd = 0, unpricedTokens = 0, unpricedEvents = 0;
      for (const r of rows) {
        const klass = savingsEventClass(r.token_class) ?? "output";
        const unknown = !r.model || knownModelRate(r.model, klass) === null;
        const rate = unknown ? sessionRate(r.session_model, r.model_token_buckets, klass) : { rate: 0, coverage: 1 };
        costUsd += unknown ? (r.tokens_saved ?? 0) * rate.rate : (r.cost_saved_usd ?? 0);
        unpricedTokens += Math.abs(r.tokens_saved ?? 0) * (1 - rate.coverage);
        if (rate.coverage < 1 && r.tokens_saved !== 0) unpricedEvents++;
      }
      return { costUsd: Math.max(0, costUsd), unpricedTokens, unpricedEvents };
    } catch {
      return { costUsd: 0, unpricedTokens: 0, unpricedEvents: 0 };
    }
  }

  /**
   * Sum the ESTIMATED counterfactual event credits over the window, grouped by
   * event type: checkpoint_restore (working-set re-reads a restore skipped),
   * resume_lean (cold-resume rewrite avoided) and hint_followed (searches a
   * followed hint replaced). Triggers are observed; magnitudes are inferred --
   * mirrors measure.py `_get_savings_summary` relocations that feed
   * `_estimate_before_after_savings`' estimated_volume_pools. The caller
   * reprices to the baseline INPUT rate and adds them as a separate estimated
   * pool, never inside the measured total.
   *
   * Best-effort: returns zeros on any error (never throws). `now` injectable.
   */
  getEstimatedCounterfactualSavings(days: number = 30, now: number = Date.now()): {
    totalTokensSaved: number;
    totalCostSavedUsd: number;
    totalEvents: number;
    byType: Record<string, { tokens: number; cost: number; events: number }>;
    unpricedTokens: number;
    unpricedEvents: number;
  } {
    const empty = { totalTokensSaved: 0, totalCostSavedUsd: 0, totalEvents: 0, byType: {} as Record<string, { tokens: number; cost: number; events: number }>, unpricedTokens: 0, unpricedEvents: 0 };
    try {
      const db = this.connect();
      const cutoff = new Date(now - days * 86_400_000).toISOString();
      const rows = db
        .query(
          `SELECT e.event_type, e.tokens_saved, e.cost_saved_usd, e.model, e.token_class, s.model AS session_model, s.model_token_buckets
           FROM counted_savings_events e LEFT JOIN session_log s ON s.session_id = e.session_id
           WHERE e.timestamp >= ? AND e.event_type IN ('checkpoint_restore', 'resume_lean', 'hint_followed')`,
        )
        .all(cutoff) as Array<{ event_type: string; tokens_saved: number; cost_saved_usd: number; model: string | null; token_class: string | null; session_model: string | null; model_token_buckets: string | null }>;
      const byType: Record<string, { tokens: number; cost: number; events: number }> = {};
      let unpricedTokens = 0, unpricedEvents = 0;
      for (const r of rows) {
        const item = byType[r.event_type] ?? { tokens: 0, cost: 0, events: 0 };
        const klass = savingsEventClass(r.token_class) ?? "input";
        const unknown = !r.model || knownModelRate(r.model, klass) === null;
        const rate = unknown ? sessionRate(r.session_model, r.model_token_buckets, klass) : { rate: 0, coverage: 1 };
        item.events++;
        item.tokens += r.tokens_saved ?? 0;
        item.cost += unknown ? (r.tokens_saved ?? 0) * rate.rate : (r.cost_saved_usd ?? 0);
        unpricedTokens += Math.abs(r.tokens_saved ?? 0) * (1 - rate.coverage);
        if (rate.coverage < 1 && r.tokens_saved !== 0) unpricedEvents++;
        byType[r.event_type] = item;
      }
      let totalTokensSaved = 0, totalCostSavedUsd = 0, totalEvents = 0;
      for (const v of Object.values(byType)) {
        totalTokensSaved += v.tokens;
        totalCostSavedUsd += v.cost;
        totalEvents += v.events;
      }
      return { totalTokensSaved, totalCostSavedUsd, totalEvents, byType, unpricedTokens, unpricedEvents };
    } catch {
      return empty;
    }
  }

  close(): void {
    if (this.db) {
      this.db.close();
      this.db = null;
    }
  }

  recordSession(data: SessionTrendData): void {
    const db = this.connect();
    const days = Object.keys(data.dailyUsage ?? {}).filter(activityDay).sort();
    const first = days[0] ?? null, last = days.at(-1) ?? null;
    db.run(
      `INSERT INTO session_log
       (session_id, date, project, model, model_token_buckets, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write,
        cost_usd, resource_health, session_efficiency, tool_calls, compactions, mode, duration_seconds, created_at,
        daily_usage_json, first_activity_day, last_activity_day)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(session_id) DO UPDATE SET
         date=excluded.date, first_activity_day=excluded.first_activity_day, last_activity_day=excluded.last_activity_day,
         daily_usage_json=excluded.daily_usage_json,
         project=excluded.project, model=excluded.model,
         model_token_buckets=COALESCE(excluded.model_token_buckets, session_log.model_token_buckets),
         tokens_input=excluded.tokens_input, tokens_output=excluded.tokens_output,
         tokens_cache_read=excluded.tokens_cache_read, tokens_cache_write=excluded.tokens_cache_write,
         cost_usd=excluded.cost_usd, resource_health=excluded.resource_health,
         session_efficiency=excluded.session_efficiency, tool_calls=excluded.tool_calls,
         compactions=excluded.compactions, mode=excluded.mode,
         duration_seconds=excluded.duration_seconds`,
      [data.sessionId, last ?? "", data.project, data.model,
       data.modelTokenBuckets ? JSON.stringify(data.modelTokenBuckets) : null,
       data.tokensInput, data.tokensOutput, data.tokensCacheRead, data.tokensCacheWrite,
       data.costUsd, data.resourceHealth, data.sessionEfficiency, data.toolCalls,
       data.compactions, data.mode, data.durationSeconds, Date.now() / 1000,
       data.dailyUsage ? JSON.stringify(data.dailyUsage) : null, first, last],
    );
  }

  /** One usage row per session and activity day. Legacy undated rows are omitted. */
  private activityRows(days?: number): Array<Record<string, unknown>> {
    const cutoff = days === undefined ? "" : new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    const result: Array<Record<string, unknown>> = [];
    for (const row of this.connect().query("SELECT * FROM session_log").all() as Array<Record<string, unknown>>) {
      const usage = dailyUsage(row.daily_usage_json);
      const dated = Object.keys(usage).filter(activityDay).sort();
      const last = dated.at(-1);
      for (const day of dated) {
        if (day < cutoff) continue;
        const buckets = usage[day]!;
        const totals = Object.values(buckets).reduce((acc, bucket) => ({
          input: acc.input + bucket.input, output: acc.output + bucket.output,
          cacheRead: acc.cacheRead + bucket.cacheRead, cacheWrite: acc.cacheWrite + bucket.cacheWrite,
          cost: acc.cost + bucket.cost,
        }), { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 });
        const model = Object.entries(buckets).sort((a, b) =>
          (b[1].input + b[1].output + b[1].cacheWrite) - (a[1].input + a[1].output + a[1].cacheWrite))[0]?.[0] ?? "unknown";
        result.push({ ...row, date: day, model, model_token_buckets: JSON.stringify(buckets),
          tokens_input: totals.input, tokens_output: totals.output, tokens_cache_read: totals.cacheRead,
          tokens_cache_write: totals.cacheWrite, cost_usd: totals.cost,
          tool_calls: day === last ? row.tool_calls : 0,
          compactions: day === last ? row.compactions : 0,
          duration_seconds: day === last ? row.duration_seconds : 0,
          cohort_duration_seconds: row.duration_seconds,
          metadata_basis: "lifetime session cohort on last activity day" });
      }
    }
    return result.sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }

  getActivityCoverage(): { complete: boolean; undatedSessions: number } {
    const rows = this.connect().query("SELECT daily_usage_json FROM session_log").all() as Array<{ daily_usage_json: string | null }>;
    const undatedSessions = rows.filter((r) => {
      const usage = dailyUsage(r.daily_usage_json);
      return Object.keys(usage).length === 0 || Object.keys(usage).some((day) => !activityDay(day));
    }).length;
    return { complete: undatedSessions === 0, undatedSessions };
  }

  /** One session, with volumes summed only over its in-window activity days. */
  getRecentSessions(days: number = 30): Array<Record<string, unknown>> {
    const sessions = new Map<string, Record<string, unknown>>();
    for (const row of this.activityRows(days)) {
      const key = String(row.session_id), previous = sessions.get(key);
      if (!previous) { sessions.set(key, row); continue; }
      const summed = { ...row };
      for (const field of ["tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write", "cost_usd", "tool_calls", "compactions", "duration_seconds"]) {
        summed[field] = Number(previous[field] ?? 0) + Number(row[field] ?? 0);
      }
      const models = aggregateModelUsage([previous, row].flatMap((r) =>
        Object.entries(JSON.parse(String(r.model_token_buckets)) as ModelTokenBuckets).map(([model, bucket]) => ({ model, ...bucket }))));
      summed.model_token_buckets = JSON.stringify(models);
      sessions.set(key, summed);
    }
    return [...sessions.values()].sort((a, b) => String(b.date).localeCompare(String(a.date)));
  }

  /** Dated slices for savings windows, oldest first. Recording time is metadata. */
  getAllSessions(): Array<Record<string, unknown>> {
    return this.activityRows();
  }

  getDailyStats(days: number = 30): Array<Record<string, unknown>> {
    const daily = new Map<string, Record<string, number | string>>();
    for (const row of this.activityRows(days)) {
      const day = String(row.date);
      const stats = daily.get(day) ?? { date: day, sessions: 0, total_input: 0, total_output: 0,
        avg_resource_health: 0, avg_session_efficiency: 0 };
      for (const [field, source] of [["total_input", "tokens_input"], ["total_output", "tokens_output"],
                                    ["avg_resource_health", "resource_health"], ["avg_session_efficiency", "session_efficiency"]]) {
        stats[field!] = Number(stats[field!] ?? 0) + Number(row[source!] ?? 0);
      }
      stats.sessions = Number(stats.sessions) + 1;
      daily.set(day, stats);
    }
    const result: Array<Record<string, unknown>> = [...daily.values()].map((r) => ({ ...r,
      avg_resource_health: Number(r.avg_resource_health) / Number(r.sessions),
      avg_session_efficiency: Number(r.avg_session_efficiency) / Number(r.sessions),
    }));
    return result.sort((a, b) => String(b.date).localeCompare(String(a.date)));
  }
}

function activityDay(day: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(Date.parse(day))
    && new Date(day).toISOString().slice(0, 10) === day;
}

function dailyUsage(raw: unknown): Record<string, ModelTokenBuckets> {
  try {
    const value = JSON.parse(String(raw ?? "null"));
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const result: Record<string, ModelTokenBuckets> = {};
    for (const [day, models] of Object.entries(value)) {
      if (!models || typeof models !== "object" || Array.isArray(models)) continue;
      const buckets: ModelTokenBuckets = {};
      for (const [model, bucket] of Object.entries(models)) {
        if (!bucket || typeof bucket !== "object") continue;
        const b = bucket as Record<string, unknown>;
        buckets[model] = { input: Math.max(0, Number(b.input) || 0), output: Math.max(0, Number(b.output) || 0),
          cacheRead: Math.max(0, Number(b.cacheRead) || 0), cacheWrite: Math.max(0, Number(b.cacheWrite) || 0), cost: Math.max(0, Number(b.cost) || 0) };
      }
      result[day] = buckets;
    }
    return result;
  } catch { return {}; }
}
