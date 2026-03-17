/**
 * Smart Compaction v2: intelligent extraction + last N messages fallback.
 *
 * v1: capture last N messages as markdown.
 * v2: extract decisions, errors, file modifications, and user instructions
 *     to preserve the most relevant context in fewer tokens.
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

// ---------------------------------------------------------------------------
// v2: Intelligent extraction
// ---------------------------------------------------------------------------

interface ExtractedContext {
  decisions: string[];
  errors: string[];
  fileChanges: string[];
  userInstructions: string[];
}

const DECISION_PATTERNS = [
  /\bI'll\b/i, /\bLet's\b/i, /\bdecided\b/i, /\bchoosing\b/i,
  /\bgoing with\b/i, /\busing\b/i, /\bswitching to\b/i,
];

const ERROR_PATTERNS = [
  /\bError[:!]/i, /\bfailed\b/i, /\bexception\b/i, /\bstack trace\b/i,
  /\btraceback\b/i, /\bTypeError\b/, /\bSyntaxError\b/, /\bReferenceError\b/,
  /\bENOENT\b/, /\bEACCES\b/, /\bconnection refused\b/i,
];

const FILE_CHANGE_PATTERNS = [
  /\bwrit(?:e|ing|ten)\b/i, /\bedit(?:ed|ing)?\b/i, /\bcreated?\b/i,
  /\bmodif(?:y|ied|ying)\b/i, /\btool_use\b/,
];

const INSTRUCTION_PATTERNS = [
  /\balways\b/i, /\bnever\b/i, /\bmake sure\b/i, /\bdon't\b/i,
  /\bdo not\b/i, /\bmust\b/i, /\bshould\b/i, /\bprefer\b/i,
];

function matchesAny(text: string, patterns: RegExp[]): boolean {
  return patterns.some((p) => p.test(text));
}

function extractIntelligent(
  messages: Array<{ role: string; content: string; timestamp?: string }>
): ExtractedContext {
  const ctx: ExtractedContext = {
    decisions: [],
    errors: [],
    fileChanges: [],
    userInstructions: [],
  };

  for (const msg of messages) {
    const content = msg.content;
    if (!content) continue;

    // Truncate very long messages for pattern matching
    const sample = content.slice(0, 3000);

    if (msg.role === "assistant") {
      if (matchesAny(sample, DECISION_PATTERNS)) {
        // Extract the decision sentence (first matching line)
        const lines = sample.split("\n");
        for (const line of lines) {
          if (matchesAny(line, DECISION_PATTERNS) && line.length > 10 && line.length < 500) {
            ctx.decisions.push(line.trim());
            break;
          }
        }
      }

      if (matchesAny(sample, ERROR_PATTERNS)) {
        const lines = sample.split("\n");
        const errorLines: string[] = [];
        for (const line of lines) {
          if (matchesAny(line, ERROR_PATTERNS) && line.length < 300) {
            errorLines.push(line.trim());
            if (errorLines.length >= 3) break;
          }
        }
        if (errorLines.length > 0) {
          ctx.errors.push(errorLines.join("\n"));
        }
      }

      if (matchesAny(sample, FILE_CHANGE_PATTERNS)) {
        const lines = sample.split("\n");
        for (const line of lines) {
          if (matchesAny(line, FILE_CHANGE_PATTERNS) && line.length > 10 && line.length < 300) {
            ctx.fileChanges.push(line.trim());
            break;
          }
        }
      }
    }

    if (msg.role === "user" && matchesAny(sample, INSTRUCTION_PATTERNS)) {
      const lines = sample.split("\n");
      for (const line of lines) {
        if (matchesAny(line, INSTRUCTION_PATTERNS) && line.length > 10 && line.length < 500) {
          ctx.userInstructions.push(line.trim());
          break;
        }
      }
    }
  }

  // Deduplicate
  ctx.decisions = [...new Set(ctx.decisions)].slice(0, 10);
  ctx.errors = [...new Set(ctx.errors)].slice(0, 5);
  ctx.fileChanges = [...new Set(ctx.fileChanges)].slice(0, 10);
  ctx.userInstructions = [...new Set(ctx.userInstructions)].slice(0, 10);

  return ctx;
}

/**
 * v2 checkpoint: intelligent extraction + recent messages fallback.
 * Produces a more focused checkpoint than v1's raw last-N dump.
 */
export function captureCheckpointV2(
  session: {
    sessionId: string;
    messages?: Array<{ role: string; content: string; timestamp?: string }>;
  },
  maxRecentMessages: number = 10
): string | null {
  const messages = session.messages;
  if (!messages || messages.length === 0) return null;

  try {
    fs.mkdirSync(CHECKPOINT_DIR, { recursive: true, mode: 0o700 });
  } catch {
    return null;
  }

  const extracted = extractIntelligent(messages);
  const recent = messages.slice(-maxRecentMessages);

  const lines: string[] = [
    "# Session Checkpoint (v2)",
    `> Captured at ${new Date().toISOString()} before compaction`,
    `> Session: ${sanitizeSessionId(session.sessionId)}`,
    `> Total messages: ${messages.length}`,
    "",
  ];

  if (extracted.userInstructions.length > 0) {
    lines.push("## User Instructions");
    lines.push("");
    for (const inst of extracted.userInstructions) {
      lines.push(`- ${inst}`);
    }
    lines.push("");
  }

  if (extracted.decisions.length > 0) {
    lines.push("## Key Decisions");
    lines.push("");
    for (const dec of extracted.decisions) {
      lines.push(`- ${dec}`);
    }
    lines.push("");
  }

  if (extracted.errors.length > 0) {
    lines.push("## Errors Encountered");
    lines.push("");
    for (const err of extracted.errors) {
      lines.push("```");
      lines.push(err);
      lines.push("```");
      lines.push("");
    }
  }

  if (extracted.fileChanges.length > 0) {
    lines.push("## File Changes");
    lines.push("");
    for (const fc of extracted.fileChanges) {
      lines.push(`- ${fc}`);
    }
    lines.push("");
  }

  lines.push("## Recent Messages");
  lines.push("");
  for (const msg of recent) {
    const role = msg.role === "user" ? "User" : "Assistant";
    const ts = msg.timestamp ? ` (${msg.timestamp})` : "";
    lines.push(`### ${role}${ts}`);
    lines.push("");
    const content =
      msg.content.length > 1500
        ? msg.content.slice(0, 1500) + "\n\n[...truncated]"
        : msg.content;
    lines.push(content);
    lines.push("");
  }

  const filepath = safeCheckpointPath(session.sessionId);
  try {
    fs.writeFileSync(filepath, lines.join("\n"), { encoding: "utf-8", mode: 0o600 });
  } catch {
    return null;
  }
  return filepath;
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
