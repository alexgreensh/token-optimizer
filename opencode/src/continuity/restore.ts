import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import { findBestCheckpoint, type CheckpointMatch } from "./matcher.js";
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
    const dbFiles = allFiles.filter((f) => f.endsWith(".db")).slice(0, config.checkpointRetentionMax);

    for (const file of dbFiles) {
      const sessionId = file.replace(".db", "");
      if (sessionId === currentSessionId) continue;

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

  return findBestCheckpoint(userPrompt, candidates, config.relevanceThreshold);
}
