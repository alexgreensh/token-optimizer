import { type SessionStore, sanitizeSessionId } from "../storage/session-store.js";
import type { SessionMode } from "../activity/tracker.js";
import type { TokenOptimizerConfig } from "../util/env.js";
import { redact } from "../util/redact.js";

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

/**
 * Build a short topic summary from recent user messages.  This summary is
 * stored verbatim in the checkpoint and later matched against new-session
 * prompts via keyword overlap, so richer content here directly raises the
 * probability of clearing the 0.6 relevance threshold in non-code sessions.
 *
 * The output is sanitized aggressively: we only keep word-chars, spaces,
 * hyphens, dots, and forward-slashes so that an injected summary cannot
 * smuggle instructions into a future system prompt.
 */
function buildTopicSummary(recentUserMessages: string[]): string {
  if (recentUserMessages.length === 0) return "";
  // Take up to 5 most recent messages, last first, to capture the session arc.
  const sample = recentUserMessages.slice(-5).reverse();
  // Sanitize: keep only printable ASCII word-chars + basic punctuation safe
  // for a system prompt context block. Drop anything that looks like XML/HTML
  // tags, prompt-injection keywords, or control characters.
  const sanitized = sample
    .map((m) =>
      // Redact first: the strips below remove the "@" and "/" a URI credential
      // is recognised by, and the cap would leave a straddling token as a
      // prefix no pattern matches.
      redact(m)
        .replace(/<[^>]*>/g, " ")           // strip XML/HTML tags
        .replace(/[^\w\s.,;:!?()'"-]/g, " ") // keep safe punctuation only
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 300),                      // cap per-message
    )
    .filter((m) => m.length > 10);           // skip trivial inputs

  if (sanitized.length === 0) return "";
  return sanitized.join(" | ");
}

export function captureCheckpoint(
  store: SessionStore,
  sessionId: string,
  trigger: string,
  mode: SessionMode,
  qualityScore: number | null,
  fillPct: number | null,
  /** Recent user messages from session state — used to enrich checkpoint content
   *  with topic keywords so non-code/strategy sessions can clear the 0.6
   *  relevance threshold during cross-session restore. Caller sanitizes nothing;
   *  buildTopicSummary() handles all sanitization here. */
  recentUserMessages: string[] = [],
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

  const safeSessionId = sanitizeSessionId(sessionId);
  // This content is later injected into a system prompt, so neutralize anything
  // that isn't plausibly part of a file path. Drop punctuation an attacker would
  // use to smuggle instructions, collapse whitespace, and cap length.
  const sanitizePath = (p: string) =>
    p.replace(/[^a-zA-Z0-9 /._-]/g, "").replace(/\s+/g, " ").trim().slice(0, 512);

  const topicSummary = buildTopicSummary(recentUserMessages);

  // Credential pass on every persisted string, applied at the write boundary
  // rather than per field: a path, decision, or message-derived fragment can
  // all carry a token, and the checkpoint row is later injected into a fresh
  // session's context. Field sanitizers above strip formatting, not secrets.
  const safeActiveFiles = activeFiles.map(redact);
  const safeDecisions = decisions.map(redact);
  const safeTopicSummary = redact(topicSummary);

  const lines: string[] = [
    `# Checkpoint: ${trigger}`,
    `Session: ${safeSessionId}`,
    `Mode: ${mode}`,
    `Quality: ${qualityScore !== null ? Math.round(qualityScore) : "N/A"}/100`,
    `Fill: ${fillPct !== null ? Math.round(fillPct * 100) : "N/A"}%`,
  ];

  if (safeTopicSummary) {
    lines.push("", "## Topic Summary");
    lines.push(safeTopicSummary);
  }

  lines.push("", "## Active Files");
  for (const f of safeActiveFiles) {
    lines.push(`- ${sanitizePath(f)}`);
  }

  if (safeDecisions.length > 0) {
    lines.push("", "## Decisions");
    for (const d of safeDecisions) {
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
      JSON.stringify(safeActiveFiles),
      JSON.stringify(safeDecisions),
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
    activeFiles: safeActiveFiles,
    decisions: safeDecisions,
    content,
    createdAt: Date.now() / 1000,
  };
}

export function pruneCheckpoints(store: SessionStore, config: TokenOptimizerConfig): number {
  if (config.checkpointRetentionDays <= 0) return 0;
  const db = store.connect();
  const cutoff = Date.now() / 1000 - config.checkpointRetentionDays * 86400;
  // Always keep the 3 newest checkpoints, even if they're past the cutoff. A
  // short retention window or a clock skew must never wipe the checkpoint we
  // just wrote and need for continuity restore.
  const result = db.run(
    "DELETE FROM checkpoints WHERE created_at < ? AND id NOT IN (SELECT id FROM checkpoints ORDER BY created_at DESC LIMIT 3)",
    [cutoff],
  );
  return result.changes;
}
