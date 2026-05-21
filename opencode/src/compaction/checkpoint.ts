import type { SessionStore } from "../storage/session-store.js";
import type { SessionMode } from "../activity/tracker.js";
import type { TokenOptimizerConfig } from "../util/env.js";

export interface Checkpoint {
  sessionId: string;
  trigger: string;
  mode: SessionMode;
  qualityScore: number | null;
  fillPct: number | null;
  activeFiles: string[];
  decisions: string[];
  content: string;
  createdAt: number;
}

export function captureCheckpoint(
  store: SessionStore,
  sessionId: string,
  trigger: string,
  mode: SessionMode,
  qualityScore: number | null,
  fillPct: number | null,
): Checkpoint {
  const recentReads = store.getRecentReads(20);
  const recentWrites = store.getRecentWrites(20);

  const allPaths = new Set<string>();
  for (const r of recentReads) allPaths.add(r.path);
  for (const w of recentWrites) allPaths.add(w.path);
  const activeFiles = [...allPaths].slice(0, 15);

  const decisions: string[] = [];
  const cachedData = store.getQualityCache()?.data;
  if (cachedData) {
    try {
      const parsed = JSON.parse(cachedData);
      if (Array.isArray(parsed.decisions)) {
        decisions.push(...parsed.decisions.slice(0, 10));
      }
    } catch {
      // ignore
    }
  }

  const safeSessionId = sessionId.replace(/[^a-zA-Z0-9_-]/g, "_");
  const sanitizePath = (p: string) => p.replace(/[\r\n]/g, " ").slice(0, 512);

  const lines: string[] = [
    `# Checkpoint: ${trigger}`,
    `Session: ${safeSessionId}`,
    `Mode: ${mode}`,
    `Quality: ${qualityScore !== null ? Math.round(qualityScore) : "N/A"}/100`,
    `Fill: ${fillPct !== null ? Math.round(fillPct * 100) : "N/A"}%`,
    "",
    "## Active Files",
    ...activeFiles.map((f) => `- ${sanitizePath(f)}`),
  ];

  if (decisions.length > 0) {
    lines.push("", "## Decisions");
    for (const d of decisions) {
      lines.push(`- ${d.replace(/[\r\n]/g, " ").slice(0, 200)}`);
    }
  }

  const content = lines.join("\n");

  const db = store.connect();
  db.run(
    `INSERT INTO checkpoints (session_id, trigger, mode, quality_score, fill_pct, active_files, decisions, content, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    [
      sessionId,
      trigger,
      mode,
      qualityScore,
      fillPct,
      JSON.stringify(activeFiles),
      JSON.stringify(decisions),
      content,
      Date.now() / 1000,
    ],
  );

  return {
    sessionId,
    trigger,
    mode,
    qualityScore,
    fillPct,
    activeFiles,
    decisions,
    content,
    createdAt: Date.now() / 1000,
  };
}

export function pruneCheckpoints(store: SessionStore, config: TokenOptimizerConfig): number {
  if (config.checkpointRetentionDays <= 0) return 0;
  const db = store.connect();
  const cutoff = Date.now() / 1000 - config.checkpointRetentionDays * 86400;
  const result = db.run("DELETE FROM checkpoints WHERE created_at < ?", [cutoff]);
  return result.changes;
}
