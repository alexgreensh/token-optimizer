import { existsSync, readdirSync, statSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import { findBestCheckpoint, type CheckpointMatch } from "./matcher.js";
import { sanitizeSessionId } from "../storage/session-store.js";
import type { TokenOptimizerConfig } from "../util/env.js";
import {
  resumeIntent,
  buildResumeLeanBlock,
  logResumeLeanSavings,
} from "./resume-lean.js";
import type { TrendsStore } from "../storage/trends.js";

export function restoreCheckpoint(
  dataDir: string,
  userPrompt: string,
  currentSessionId: string,
  config: TokenOptimizerConfig,
  /** Project slug for scoping session scans under sessions/{slug}/. Derived
   *  from ctx.project.worktree by the caller (hashProjectDir). */
  projectSlug?: string,
  /** Optional: when supplied and the prompt shows resume-intent, the lean
   *  reconstruction path fires and savings are credited to trends.db. */
  trendsStore?: TrendsStore,
  /** Working directory for same-project scoping. Comes from ctx.project.path
   *  or process.cwd() at call time (the caller decides). */
  cwd?: string,
): CheckpointMatch | null {
  if (!config.features.continuity) return null;

  const sessDir = projectSlug
    ? join(dataDir, "token-optimizer", "sessions", projectSlug)
    : join(dataDir, "token-optimizer", "sessions");
  if (!existsSync(sessDir)) return null;

  // DB filenames are the sanitized session id, so compare like-for-like or a
  // session whose id contains special chars would fail to exclude its own file.
  const safeCurrentId = sanitizeSessionId(currentSessionId);

  // ---------------------------------------------------------------------------
  // Resume-lean path: natural-language "continue prior work" → full lean block.
  // Fires BEFORE the lightweight keyword-hint path so a deliberate "continue
  // the token-optimizer work" request returns a full reconstruction, not a hint.
  // Falls through to the lightweight hint when no same-project match is found.
  // ---------------------------------------------------------------------------
  if (config.features.continuity && cwd && resumeIntent(userPrompt)) {
    try {
      const [leanBlock, targetSid] = buildResumeLeanBlock(
        userPrompt,
        dataDir,
        safeCurrentId,
        projectSlug,
        cwd,
        config.checkpointRetentionDays,
        config.checkpointRetentionMax,
      );
      if (leanBlock && targetSid) {
        // Credit the avoided cold-resume cost. Best-effort: never breaks injection.
        if (trendsStore) {
          const leanRawBytes = Buffer.byteLength(leanBlock, "utf8");
          logResumeLeanSavings(trendsStore, targetSid, leanBlock, leanRawBytes);
        }
        // Wrap as a CheckpointMatch so the caller can inject it uniformly.
        return {
          content: leanBlock,
          score: 1.0,
          sessionId: targetSid,
          mode: "resume_lean",
          rawBytes: Buffer.byteLength(leanBlock, "utf8"),
        };
      }
      // No same-project match. When cwd is known, do NOT fall through to the
      // (un-gated) lightweight hint — that could surface a DIFFERENT project's
      // checkpoint for an explicit "continue our work" request. Return null
      // instead. Only fall through when cwd is absent (best-effort mode).
      if (cwd) return null;
    } catch {
      // Best-effort: never break normal continuity over the lean path.
    }
  }

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
