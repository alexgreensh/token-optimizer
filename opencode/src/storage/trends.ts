import { Database } from "bun:sqlite";
import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";

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

// Sonnet input rate ($/M tokens) used as fallback when the active model is
// unknown at savings-log time (e.g. checkpoint inject fires before the first
// assistant message arrives). Matches Python's _log_savings_event fallback.
const SONNET_INPUT_RATE_PER_MTOK = 3.0;

export interface SessionTrendData {
  sessionId: string;
  project: string | null;
  model: string | null;
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
    }
    return this.db;
  }

  /**
   * Log a realized-savings event to the savings_events table.
   *
   * This is the TypeScript equivalent of Python's `_log_savings_event`.
   * Model is not known at checkpoint-inject time, so cost_saved_usd is
   * priced at the Sonnet input fallback rate — same behaviour as Python's
   * resolver when the session model cannot be determined.
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
  ): void {
    if (tokensSaved <= 0) return;
    try {
      const db = this.connect();
      const costSavedUsd = (tokensSaved * SONNET_INPUT_RATE_PER_MTOK) / 1_000_000;
      db.run(
        `INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
        [
          new Date().toISOString(),
          eventType,
          tokensSaved,
          costSavedUsd,
          sessionId ?? null,
          detail ?? null,
          model ?? null,
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
   * OVERCOUNT VERIFICATION (Fix 7): tokens_cache_write is populated from
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
   * (setup_optimization, mcp_cap, hint_followed) and tool_archive re-expansions
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
  } {
    try {
      const db = this.connect();
      const cutoff = new Date(now - days * 86_400_000).toISOString();
      const rows = db
        .query(
          `SELECT event_type, COUNT(*) as cnt,
                  SUM(tokens_saved) as tok, SUM(cost_saved_usd) as cost
           FROM savings_events WHERE timestamp >= ? GROUP BY event_type`,
        )
        .all(cutoff) as Array<{ event_type: string; cnt: number; tok: number | null; cost: number | null }>;

      const byCategory = new Map<string, { events: number; tokens: number; cost: number }>();
      for (const r of rows) {
        byCategory.set(r.event_type, {
          events: r.cnt,
          tokens: r.tok ?? 0,
          cost: r.cost ?? 0,
        });
      }

      // Relocate estimated-tier categories OUT of the measured total (A1/A3/U-G
      // in measure.py: setup_optimization is a one-time trim double-counted by
      // structural_savings; mcp_cap and hint_followed are observed-trigger but
      // estimated-magnitude; verbosity_steer output reduction is estimated not
      // metered). They never belong in the measured realized total.
      for (const k of ["setup_optimization", "mcp_cap", "hint_followed", "verbosity_steer"]) {
        byCategory.delete(k);
      }

      // B6: net tool_archive re-expansions against tool_archive (re-popped
      // results didn't stay collapsed), floored at 0. The debit is not its own line.
      const reexpand = byCategory.get("tool_archive_reexpand");
      if (reexpand) {
        byCategory.delete("tool_archive_reexpand");
        const ta = byCategory.get("tool_archive");
        if (ta) {
          ta.tokens = Math.max(0, ta.tokens - reexpand.tokens);
          ta.cost = Math.max(0, ta.cost - reexpand.cost);
        }
      }

      let totalTokensSaved = 0;
      let totalCostSavedUsd = 0;
      let totalEvents = 0;
      for (const v of byCategory.values()) {
        totalTokensSaved += v.tokens;
        totalCostSavedUsd += v.cost;
        totalEvents += v.events;
      }
      return { totalTokensSaved, totalCostSavedUsd, totalEvents };
    } catch {
      return { totalTokensSaved: 0, totalCostSavedUsd: 0, totalEvents: 0 };
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
    try {
      const db = this.connect();
      const cutoff = new Date(now - days * 86_400_000).toISOString();
      const row = db
        .query(
          `SELECT SUM(cost_saved_usd) as cost
           FROM savings_events WHERE timestamp >= ? AND event_type = 'verbosity_steer'`,
        )
        .get(cutoff) as { cost: number | null } | null;
      return Math.max(0, row?.cost ?? 0);
    } catch {
      return 0;
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
    const date = new Date().toISOString().split("T")[0];
    // Upsert that PRESERVES the original date + created_at. INSERT OR REPLACE is a
    // DELETE+INSERT, so a re-recorded session (double session.deleted) would jump
    // to today's date bucket and lose its original timestamp.
    db.run(
      `INSERT INTO session_log
       (session_id, date, project, model, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write,
        cost_usd, resource_health, session_efficiency, tool_calls, compactions, mode, duration_seconds, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(session_id) DO UPDATE SET
         project=excluded.project, model=excluded.model,
         tokens_input=excluded.tokens_input, tokens_output=excluded.tokens_output,
         tokens_cache_read=excluded.tokens_cache_read, tokens_cache_write=excluded.tokens_cache_write,
         cost_usd=excluded.cost_usd, resource_health=excluded.resource_health,
         session_efficiency=excluded.session_efficiency, tool_calls=excluded.tool_calls,
         compactions=excluded.compactions, mode=excluded.mode,
         duration_seconds=excluded.duration_seconds`,
      [
        data.sessionId,
        date,
        data.project,
        data.model,
        data.tokensInput,
        data.tokensOutput,
        data.tokensCacheRead,
        data.tokensCacheWrite,
        data.costUsd,
        data.resourceHealth,
        data.sessionEfficiency,
        data.toolCalls,
        data.compactions,
        data.mode,
        data.durationSeconds,
        Date.now() / 1000,
      ],
    );
  }

  getRecentSessions(days: number = 30): Array<Record<string, unknown>> {
    const db = this.connect();
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    const cutoffStr = cutoff.toISOString().split("T")[0];

    return db
      .query("SELECT * FROM session_log WHERE date >= ? ORDER BY created_at DESC")
      .all(cutoffStr) as Array<Record<string, unknown>>;
  }

  /** All sessions ever recorded, oldest-first. Used to establish the realized-
   *  savings baseline (the earliest stable usage window). */
  getAllSessions(): Array<Record<string, unknown>> {
    const db = this.connect();
    return db
      .query("SELECT * FROM session_log ORDER BY created_at ASC")
      .all() as Array<Record<string, unknown>>;
  }

  getDailyStats(days: number = 30): Array<Record<string, unknown>> {
    const db = this.connect();
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    const cutoffStr = cutoff.toISOString().split("T")[0];

    return db
      .query(
        `SELECT date,
                COUNT(*) as sessions,
                SUM(tokens_input) as total_input,
                SUM(tokens_output) as total_output,
                AVG(COALESCE(resource_health, 0)) as avg_resource_health,
                AVG(COALESCE(session_efficiency, 0)) as avg_session_efficiency
         FROM session_log
         WHERE date >= ?
         GROUP BY date
         ORDER BY date DESC`,
      )
      .all(cutoffStr) as Array<Record<string, unknown>>;
  }
}
