import { existsSync, readdirSync, statSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import { findBestCheckpoint, type CheckpointMatch } from "./matcher.js";
import { sanitizeSessionId } from "../storage/session-store.js";
import type { TokenOptimizerConfig } from "../util/env.js";

export function restoreCheckpoint(
  dataDir: string,
  userPrompt: string,
  currentSessionId: string,
  config: TokenOptimizerConfig,
): CheckpointMatch | null {
  if (!config.features.continuity) return null;

  const sessDir = join(dataDir, "token-optimizer", "sessions");
  if (!existsSync(sessDir)) return null;

  // DB filenames are the sanitized session id, so compare like-for-like or a
  // session whose id contains special chars would fail to exclude its own file.
  const safeCurrentId = sanitizeSessionId(currentSessionId);

  const cutoff = config.checkpointRetentionDays <= 0
    ? 0
    : Date.now() / 1000 - config.checkpointRetentionDays * 86400;
  const allCheckpoints: Array<{
    session_id: string;
    content: string;
    mode: string;
    created_at: number;
  }> = [];

  try {
    const allFiles = readdirSync(sessDir);
    // Sort by mtime (newest first) BEFORE slicing. readdir order is
    // filesystem-hash order, so a bare slice(0, max) would pick arbitrary DBs
    // and could exclude the most recent sessions entirely once the count
    // exceeds checkpointRetentionMax.
    const ranked = allFiles
      .filter((f) => f.endsWith(".db"))
      .map((f) => {
        let mtimeMs = 0;
        try { mtimeMs = statSync(join(sessDir, f)).mtimeMs; } catch { /* unreadable → oldest */ }
        return { f, mtimeMs };
      })
      .sort((a, b) => b.mtimeMs - a.mtimeMs);

    // Prune session DB files whose mtime is older than the retention window so
    // the sessions/ dir does not grow without bound over months of daily use.
    // Only stale files (old mtime ⇒ no active writer) are removed; fresh files
    // remain for scanning.
    const pruneBeforeMs =
      config.checkpointRetentionDays > 0
        ? Date.now() - config.checkpointRetentionDays * 86400 * 1000
        : 0;
    const fresh: typeof ranked = [];
    for (const item of ranked) {
      const sid = item.f.replace(".db", "");
      const isStale =
        pruneBeforeMs > 0 && item.mtimeMs > 0 && item.mtimeMs < pruneBeforeMs;
      if (isStale && sid !== safeCurrentId) {
        try { rmSync(join(sessDir, item.f), { force: true }); } catch { /* best-effort */ }
        continue;
      }
      fresh.push(item);
    }

    const dbFiles = fresh.slice(0, config.checkpointRetentionMax).map((x) => x.f);

    for (const file of dbFiles) {
      const sessionId = file.replace(".db", "");
      if (sessionId === safeCurrentId) continue;

      const dbPath = join(sessDir, file);
      let db: Database | null = null;
      try {
        db = new Database(dbPath, { readonly: true });
        db.exec("PRAGMA busy_timeout=500");
        const rows = db
          .query(
            "SELECT session_id, content, mode, created_at FROM checkpoints WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
          )
          .all(cutoff, config.checkpointRetentionMax) as Array<{
          session_id: string;
          content: string;
          mode: string;
          created_at: number;
        }>;
        allCheckpoints.push(...rows);
      } catch {
        // skip corrupt/locked DBs
      } finally {
        db?.close();
      }
    }
  } catch {
    return null;
  }

  if (allCheckpoints.length === 0) return null;

  allCheckpoints.sort((a, b) => b.created_at - a.created_at);
  const candidates = allCheckpoints.slice(0, config.checkpointRetentionMax);

  return findBestCheckpoint(userPrompt, candidates, config.relevanceThreshold, config.checkpointMaxChars);
}
