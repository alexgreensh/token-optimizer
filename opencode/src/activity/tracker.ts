import type { SessionStore } from "../storage/session-store.js";

const WINDOW_SIZE = 10;
const PRUNE_THRESHOLD = 30;
const PRUNE_KEEP = 20;

const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "file_write", "file_edit"]);
const READ_TOOLS = new Set(["Read", "Glob", "Grep", "file_read", "find"]);
const AGENT_TOOLS = new Set(["Agent", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"]);

const INFRA_BASH_RE =
  /\b(?:systemctl|nginx|docker|kubectl|service|daemon|launchctl|brew|apt|apt-get|yum|dnf|pacman)\b/;
const GIT_WRITE_RE = /\bgit\s+(?:push|pull|merge|rebase|cherry-pick|tag)\b/;
const INSTALL_RE = /\b(?:pip|npm|pnpm|yarn|bun|cargo|go)\s+(?:install|add|update|upgrade)\b/;

export type ToolBucket =
  | "edit"
  | "read"
  | "agent"
  | "mcp"
  | "bash_infra"
  | "bash_git"
  | "bash_install"
  | "bash_other"
  | "web"
  | "other";

export type SessionMode = "code" | "debug" | "review" | "infra" | "general";

export function classifyTool(toolName: string, command: string = ""): ToolBucket {
  if (EDIT_TOOLS.has(toolName)) return "edit";
  if (READ_TOOLS.has(toolName)) return "read";
  if (AGENT_TOOLS.has(toolName)) return "agent";
  if (toolName.startsWith("mcp__") || toolName.startsWith("mcp_")) return "mcp";
  if (toolName === "Bash" || toolName === "shell" || toolName === "bash") {
    if (INFRA_BASH_RE.test(command)) return "bash_infra";
    if (GIT_WRITE_RE.test(command)) return "bash_git";
    if (INSTALL_RE.test(command)) return "bash_install";
    return "bash_other";
  }
  if (toolName === "WebSearch" || toolName === "WebFetch") return "web";
  return "other";
}

export function detectMode(recentBuckets: ToolBucket[], hasRecentErrors: boolean = false): SessionMode {
  if (recentBuckets.length < 3) return "general";

  const editCount = recentBuckets.filter((b) => b === "edit").length;
  const readCount = recentBuckets.filter((b) => b === "read").length;
  const infraCount = recentBuckets.filter((b) =>
    b === "bash_infra" || b === "bash_git" || b === "bash_install",
  ).length;
  const webCount = recentBuckets.filter((b) => b === "web").length;
  const bashOther = recentBuckets.filter((b) => b === "bash_other").length;

  if (infraCount >= 3) return "infra";
  if (hasRecentErrors && readCount >= 3 && editCount <= 1) return "debug";
  if (editCount >= 4) return "code";
  if (readCount >= 4 && editCount === 0) return "review";
  if (webCount >= 3) return "review";
  if (editCount >= 2 && (bashOther >= 2 || readCount >= 2)) return "code";

  return "general";
}

export function logToolUse(
  store: SessionStore,
  toolName: string,
  command: string = "",
  hasError: boolean = false,
  resultSize: number = 0,
): SessionMode | null {
  try {
    const bucket = classifyTool(toolName, command);
    const db = store.connect();

    db.run(
      "INSERT INTO activity_log (tool_name, tool_bucket, has_error, result_size, timestamp) VALUES (?, ?, ?, ?, ?)",
      [toolName.slice(0, 64), bucket, hasError ? 1 : 0, resultSize, Date.now() / 1000],
    );

    const rows = db
      .query("SELECT tool_bucket, has_error FROM activity_log ORDER BY id DESC LIMIT ?")
      .all(WINDOW_SIZE) as Array<{ tool_bucket: string; has_error: number }>;

    const rowCount = (
      db.query("SELECT COUNT(*) as cnt FROM activity_log").get() as { cnt: number }
    ).cnt;
    if (rowCount > PRUNE_THRESHOLD) {
      db.run(
        "DELETE FROM activity_log WHERE id NOT IN (SELECT id FROM activity_log ORDER BY id DESC LIMIT ?)",
        [PRUNE_KEEP],
      );
    }

    const recentBuckets = rows.map((r) => r.tool_bucket as ToolBucket);
    const hasRecentErrors = rows.some((r) => r.has_error === 1);
    const mode = detectMode(recentBuckets, hasRecentErrors);

    store.setMeta("current_mode", mode);
    return mode;
  } catch {
    return null;
  }
}
