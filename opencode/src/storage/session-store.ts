import { Database } from "bun:sqlite";
import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";

/** Reduce a session id to the charset used for its DB filename and for echoing
 * the id into checkpoint content. Single source of truth for all three callers. */
export function sanitizeSessionId(id: string): string {
  return id.replace(/[^a-zA-Z0-9_-]/g, "_");
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS activity_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool_name TEXT NOT NULL,
  tool_bucket TEXT NOT NULL,
  has_error INTEGER NOT NULL DEFAULT 0,
  result_size INTEGER DEFAULT 0,
  timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_cache (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  resource_health REAL,
  session_efficiency REAL,
  fill_pct REAL,
  compactions INTEGER DEFAULT 0,
  tool_calls INTEGER DEFAULT 0,
  last_nudge_time REAL DEFAULT 0,
  nudge_count INTEGER DEFAULT 0,
  data TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  mode TEXT,
  quality_score REAL,
  fill_pct REAL,
  active_files TEXT,
  decisions TEXT,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idx INTEGER NOT NULL,
  path TEXT NOT NULL,
  timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idx INTEGER NOT NULL,
  path TEXT NOT NULL,
  timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idx INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  result_size INTEGER NOT NULL,
  is_failure INTEGER NOT NULL DEFAULT 0,
  timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idx INTEGER NOT NULL,
  role TEXT NOT NULL,
  text_length INTEGER NOT NULL,
  is_substantive INTEGER NOT NULL DEFAULT 0,
  timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idx INTEGER NOT NULL,
  prompt_size INTEGER NOT NULL,
  result_size INTEGER NOT NULL DEFAULT 0,
  timestamp REAL NOT NULL
);
`;

export class SessionStore {
  private db: Database | null = null;
  private dbPath: string;

  /**
   * @param projectSlug Optional. When supplied, session DBs are scoped into a
   *   per-project subdirectory so a global dataDir cannot leak one project's
   *   checkpoints into another's. Appended (not inserted) so existing two-arg
   *   call sites keep working unchanged.
   */
  constructor(dataDir: string, sessionId: string, projectSlug?: string) {
    const base = join(dataDir, "token-optimizer", "sessions");
    const sessDir = projectSlug ? join(base, projectSlug) : base;
    if (!existsSync(sessDir)) {
      mkdirSync(sessDir, { recursive: true });
    }
    this.dbPath = join(sessDir, `${sanitizeSessionId(sessionId)}.db`);
  }

  connect(): Database {
    if (!this.db) {
      this.db = new Database(this.dbPath, { create: true });
      this.db.exec("PRAGMA journal_mode=WAL");
      this.db.exec("PRAGMA busy_timeout=3000");
      this.db.exec(SCHEMA);
    }
    return this.db;
  }

  close(): void {
    if (this.db) {
      this.db.close();
      this.db = null;
    }
  }

  getMeta(key: string): string | undefined {
    const db = this.connect();
    const row = db.query("SELECT value FROM session_meta WHERE key = ?").get(key) as
      | { value: string }
      | null;
    return row?.value;
  }

  setMeta(key: string, value: string): void {
    const db = this.connect();
    db.run("INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, ?)", [key, value]);
  }

  getQualityCache(): QualityCacheRow | null {
    const db = this.connect();
    const row = db.query("SELECT * FROM quality_cache WHERE id = 1").get() as QualityCacheRow | null;
    return row;
  }

  writeQualityCache(cache: Omit<QualityCacheRow, "id" | "updated_at">): void {
    const db = this.connect();
    // True upsert (ON CONFLICT DO UPDATE), not INSERT OR REPLACE: REPLACE is a
    // DELETE+INSERT that briefly drops the row and resets any column not listed.
    db.run(
      `INSERT INTO quality_cache (id, resource_health, session_efficiency, fill_pct, compactions, tool_calls, last_nudge_time, nudge_count, data, updated_at)
       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         resource_health=excluded.resource_health,
         session_efficiency=excluded.session_efficiency,
         fill_pct=excluded.fill_pct,
         compactions=excluded.compactions,
         tool_calls=excluded.tool_calls,
         last_nudge_time=excluded.last_nudge_time,
         nudge_count=excluded.nudge_count,
         data=excluded.data,
         updated_at=excluded.updated_at`,
      [
        cache.resource_health,
        cache.session_efficiency,
        cache.fill_pct,
        cache.compactions,
        cache.tool_calls,
        cache.last_nudge_time,
        cache.nudge_count,
        cache.data,
        Date.now() / 1000,
      ],
    );
  }

  recordRead(idx: number, path: string): void {
    const db = this.connect();
    db.run("INSERT INTO reads (idx, path, timestamp) VALUES (?, ?, ?)", [idx, path, Date.now() / 1000]);
  }

  recordWrite(idx: number, path: string): void {
    const db = this.connect();
    db.run("INSERT INTO writes (idx, path, timestamp) VALUES (?, ?, ?)", [idx, path, Date.now() / 1000]);
  }

  recordToolResult(idx: number, toolName: string, resultSize: number, isFailure: boolean): void {
    const db = this.connect();
    db.run(
      "INSERT INTO tool_results (idx, tool_name, result_size, is_failure, timestamp) VALUES (?, ?, ?, ?, ?)",
      [idx, toolName, resultSize, isFailure ? 1 : 0, Date.now() / 1000],
    );
  }

  recordMessage(idx: number, role: string, textLength: number, isSubstantive: boolean): void {
    const db = this.connect();
    db.run(
      "INSERT INTO messages (idx, role, text_length, is_substantive, timestamp) VALUES (?, ?, ?, ?, ?)",
      [idx, role, textLength, isSubstantive ? 1 : 0, Date.now() / 1000],
    );
  }

  recordAgentDispatch(idx: number, promptSize: number, resultSize: number): void {
    const db = this.connect();
    db.run(
      "INSERT INTO agent_dispatches (idx, prompt_size, result_size, timestamp) VALUES (?, ?, ?, ?)",
      [idx, promptSize, resultSize, Date.now() / 1000],
    );
  }

  getRecentReads(limit: number): Array<{ idx: number; path: string }> {
    const db = this.connect();
    return db
      .query("SELECT idx, path FROM reads ORDER BY id DESC LIMIT ?")
      .all(limit) as Array<{ idx: number; path: string }>;
  }

  getRecentWrites(limit: number): Array<{ idx: number; path: string }> {
    const db = this.connect();
    return db
      .query("SELECT idx, path FROM writes ORDER BY id DESC LIMIT ?")
      .all(limit) as Array<{ idx: number; path: string }>;
  }

  getRecentToolResults(limit: number): Array<{ idx: number; tool_name: string; result_size: number; is_failure: number }> {
    const db = this.connect();
    return db
      .query("SELECT idx, tool_name, result_size, is_failure FROM tool_results ORDER BY id DESC LIMIT ?")
      .all(limit) as Array<{ idx: number; tool_name: string; result_size: number; is_failure: number }>;
  }

  getRecentMessages(limit: number): Array<{ idx: number; role: string; text_length: number; is_substantive: number }> {
    const db = this.connect();
    return db
      .query("SELECT idx, role, text_length, is_substantive FROM messages ORDER BY id DESC LIMIT ?")
      .all(limit) as Array<{ idx: number; role: string; text_length: number; is_substantive: number }>;
  }

  getRecentAgentDispatches(limit: number): Array<{ idx: number; prompt_size: number; result_size: number }> {
    const db = this.connect();
    return db
      .query("SELECT idx, prompt_size, result_size FROM agent_dispatches ORDER BY id DESC LIMIT ?")
      .all(limit) as Array<{ idx: number; prompt_size: number; result_size: number }>;
  }

  private safeParseInt(value: string | undefined): number {
    const parsed = parseInt(value ?? "0", 10);
    return isNaN(parsed) ? 0 : parsed;
  }

  private atomicIncrement(key: string): number {
    const db = this.connect();
    // Single round-trip: RETURNING gives us the post-increment value without a
    // follow-up SELECT (SQLite >= 3.35, shipped in bun:sqlite).
    const row = db
      .query(
        "INSERT INTO session_meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) RETURNING CAST(value AS INTEGER) AS v",
      )
      .get(key) as { v: number } | null;
    return row?.v ?? 1;
  }

  /**
   * Cap each per-session signal table to its most recent `maxRows` rows.
   * Without this, a long session that never compacts grows these tables
   * unbounded. Table names are a fixed allowlist, never user input.
   */
  capSignalTables(maxRows: number): void {
    const db = this.connect();
    db.transaction(() => {
      for (const table of ["reads", "writes", "tool_results", "messages", "agent_dispatches"]) {
        db.run(
          `DELETE FROM ${table} WHERE id NOT IN (SELECT id FROM ${table} ORDER BY id DESC LIMIT ?)`,
          [maxRows],
        );
      }
    })();
  }

  getCompactionCount(): number {
    return this.safeParseInt(this.getMeta("compaction_count"));
  }

  incrementCompaction(): number {
    return this.atomicIncrement("compaction_count");
  }

  getToolCallCount(): number {
    return this.safeParseInt(this.getMeta("tool_call_count"));
  }

  incrementToolCallCount(): number {
    return this.atomicIncrement("tool_call_count");
  }

  getOperationIndex(): number {
    return this.safeParseInt(this.getMeta("operation_index"));
  }

  incrementOperationIndex(): number {
    return this.atomicIncrement("operation_index");
  }

  resetSignalAccumulators(): void {
    const db = this.connect();
    db.run("DELETE FROM reads");
    db.run("DELETE FROM writes");
    db.run("DELETE FROM tool_results");
    db.run("DELETE FROM messages");
    db.run("DELETE FROM agent_dispatches");
  }
}

export interface QualityCacheRow {
  id: number;
  resource_health: number | null;
  session_efficiency: number | null;
  fill_pct: number | null;
  compactions: number;
  tool_calls: number;
  last_nudge_time: number;
  nudge_count: number;
  data: string | null;
  updated_at: number;
}
