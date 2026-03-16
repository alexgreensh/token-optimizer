/**
 * Smart Compaction v1: capture last N messages before compaction, restore after.
 */

import * as fs from "fs";
import * as path from "path";

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const CHECKPOINT_DIR = path.join(HOME, ".openclaw", "token-optimizer", "checkpoints");

/** Strip path traversal characters from session IDs */
function sanitizeSessionId(id: string): string {
  const clean = id.replace(/[^a-zA-Z0-9_-]/g, "_");
  if (!clean || clean === "." || clean === "..") return "invalid-session";
  return clean;
}

/** Verify resolved path stays within checkpoint directory */
function safeCheckpointPath(sessionId: string): string {
  const safe = sanitizeSessionId(sessionId);
  const filepath = path.join(CHECKPOINT_DIR, `${safe}.md`);
  const resolved = path.resolve(filepath);
  if (!resolved.startsWith(path.resolve(CHECKPOINT_DIR) + path.sep)) {
    throw new Error("Path traversal detected");
  }
  return filepath;
}

export function captureCheckpoint(
  session: {
    sessionId: string;
    messages?: Array<{ role: string; content: string; timestamp?: string }>;
  },
  maxMessages: number = 20
): string | null {
  const messages = session.messages;
  if (!messages || messages.length === 0) return null;

  fs.mkdirSync(CHECKPOINT_DIR, { recursive: true, mode: 0o700 });

  const recent = messages.slice(-maxMessages);

  const lines: string[] = [
    "# Session Checkpoint",
    `> Captured at ${new Date().toISOString()} before compaction`,
    `> Session: ${sanitizeSessionId(session.sessionId)}`,
    `> Messages preserved: ${recent.length} of ${messages.length}`,
    "",
  ];

  for (const msg of recent) {
    const role = msg.role === "user" ? "User" : "Assistant";
    const ts = msg.timestamp ? ` (${msg.timestamp})` : "";
    lines.push(`## ${role}${ts}`);
    lines.push("");
    const content =
      msg.content.length > 2000
        ? msg.content.slice(0, 2000) + "\n\n[...truncated]"
        : msg.content;
    lines.push(content);
    lines.push("");
  }

  const filepath = safeCheckpointPath(session.sessionId);
  fs.writeFileSync(filepath, lines.join("\n"), { encoding: "utf-8", mode: 0o600 });
  return filepath;
}

export function restoreCheckpoint(sessionId: string): string | null {
  try {
    const filepath = safeCheckpointPath(sessionId);
    return fs.readFileSync(filepath, "utf-8");
  } catch {
    return null;
  }
}

export function cleanupCheckpoints(maxAgeDays: number = 7): number {
  if (!fs.existsSync(CHECKPOINT_DIR)) return 0;

  const cutoff = Date.now() - maxAgeDays * 86400 * 1000;
  let cleaned = 0;

  try {
    for (const file of fs.readdirSync(CHECKPOINT_DIR)) {
      if (!file.endsWith(".md")) continue;
      const filepath = path.join(CHECKPOINT_DIR, file);
      try {
        if (fs.statSync(filepath).mtimeMs < cutoff) {
          fs.unlinkSync(filepath);
          cleaned++;
        }
      } catch {
        continue;
      }
    }
  } catch {
    // skip
  }

  return cleaned;
}
