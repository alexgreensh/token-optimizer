import { homedir, platform } from "node:os";
import { join, sep } from "node:path";
import { createHash } from "node:crypto";
import type { PluginOptions } from "@opencode-ai/plugin";

export interface TokenOptimizerConfig {
  /**
   * Base directory under which the `token-optimizer/` data folder is created.
   * Unset means "resolve the platform-global location" (see resolveDataDir).
   * Set it via PluginOptions.dataDir or TOKEN_OPTIMIZER_DATA_DIR.
   */
  dataDir?: string;
  qualityWindow: number;
  toolCallWarnThreshold: number | null;
  toolCallCriticalThreshold: number | null;
  checkpointMaxFiles: number;
  checkpointTtlSeconds: number;
  checkpointRetentionDays: number;
  checkpointRetentionMax: number;
  relevanceThreshold: number;
  checkpointCooldownSeconds: number;
  checkpointMaxChars: number;
  freshNudgeQualityThreshold: number;
  freshNudgeMinFillPct: number;
  features: {
    qualityNudges: boolean;
    loopDetection: boolean;
    smartCompaction: boolean;
    continuity: boolean;
    activityTracking: boolean;
    trends: boolean;
  };
}

function intEnv(key: string, fallback: number): number {
  const raw = process.env[key]?.trim();
  if (!raw) return fallback;
  const parsed = parseInt(raw, 10);
  if (isNaN(parsed)) {
    console.warn(`[Token Optimizer] Invalid ${key}=${raw}, using default ${fallback}`);
    return fallback;
  }
  return parsed;
}

function floatEnv(key: string, fallback: number): number {
  const raw = process.env[key]?.trim();
  if (!raw) return fallback;
  const parsed = parseFloat(raw);
  if (isNaN(parsed)) {
    console.warn(`[Token Optimizer] Invalid ${key}=${raw}, using default ${fallback}`);
    return fallback;
  }
  return parsed;
}

function boolEnv(key: string, fallback: boolean): boolean {
  const raw = process.env[key]?.trim()?.toLowerCase();
  if (!raw) return fallback;
  if (["0", "false", "no", "off"].includes(raw)) return false;
  if (["1", "true", "yes", "on"].includes(raw)) return true;
  return fallback;
}

const DATA_FOLDER = "token-optimizer";

/**
 * Drop a trailing `token-optimizer` path segment if the caller already included
 * one, so storage code can always `join(base, DATA_FOLDER, ...)` without ending
 * up with `.../token-optimizer/token-optimizer/`.
 *
 * Segment-aware on purpose. A regex like /\/?token-optimizer\/?$/ also matches
 * the tail of `my-token-optimizer`, silently truncating a legitimate directory
 * name to `my-`. Splitting on separators can only ever match a whole segment.
 */
/**
 * The host facts this module branches on.
 *
 * Injected rather than read inline so every platform branch is reachable from a
 * test on any one machine. Reading `platform()` directly inside the resolver
 * meant the Windows and Linux paths shipped without ever executing.
 */
export interface HostContext {
  platform: string;
  home: string;
  env: Record<string, string | undefined>;
  /** Separator used when rejoining a split path. */
  sep: string;
}

function hostContext(): HostContext {
  return { platform: platform(), home: homedir(), env: process.env, sep };
}

export function stripDataFolderSuffix(dir: string, host: HostContext = hostContext()): string {
  // Split on what is actually a separator for THIS platform. Treating "\" as a
  // separator on POSIX would split the single legal segment `weird\name` in two
  // and rejoin it with "/", silently rewriting an explicitly configured dataDir.
  const parts = dir.split(host.platform === "win32" ? /[\\/]/ : /\//);
  while (parts.length > 1 && parts[parts.length - 1] === "") parts.pop();
  if (parts.length > 1 && parts[parts.length - 1] === DATA_FOLDER) {
    parts.pop();
    return parts.join(host.sep) || host.sep;
  }
  return dir;
}

/**
 * The platform-global base directory, with no config or env override applied.
 * Split out from resolveDataDir purely so each branch is directly testable.
 */
export function platformDataDir(host: HostContext = hostContext()): string {
  switch (host.platform) {
    case "darwin":
      return join(host.home, "Library", "Application Support");
    case "win32":
      return host.env.LOCALAPPDATA?.trim() || join(host.home, "AppData", "Local");
    default:
      return host.env.XDG_DATA_HOME?.trim() || join(host.home, ".local", "share");
  }
}

/**
 * Resolve the base directory that holds the `token-optimizer/` data folder.
 *
 * Precedence: explicit config.dataDir -> TOKEN_OPTIMIZER_DATA_DIR -> platform
 * global location. Returns the BASE; callers append DATA_FOLDER themselves.
 *
 * Previously this was the project directory, which meant session DBs and
 * trends.db landed inside whatever repo you happened to be working in.
 */
export function resolveDataDir(
  config?: Pick<TokenOptimizerConfig, "dataDir">,
  host: HostContext = hostContext(),
): string {
  const explicit = config?.dataDir ?? host.env.TOKEN_OPTIMIZER_DATA_DIR;
  if (explicit && explicit.trim()) return stripDataFolderSuffix(explicit.trim(), host);
  return platformDataDir(host);
}

/**
 * Encode a project path into a single filesystem-safe directory name, matching
 * Claude Code's `~/.claude/projects/` convention so the same project resolves to
 * the same slug across both tools.
 *
 *   /Users/alex/my project  ->  -Users-alex-my-project
 *   D:\Code\my app          ->  D--Code-my-app
 *
 * Returns "unknown-project" for empty input rather than "", which would collapse
 * the scoped path back onto the unscoped parent directory.
 */
export function hashProjectDir(worktree: string): string {
  const raw = worktree ?? "";
  const slug = raw.replace(/[^A-Za-z0-9]/g, "-");
  const readable = slug.replace(/^-+$/, "") ? slug : "unknown-project";

  // The readable slug alone is NOT a safe isolation key. Collapsing every
  // non-alphanumeric to "-" maps `client-a`, `client.a` and `client_a` onto one
  // identical slug, and since this is the only boundary between projects in a
  // shared global data dir, colliding projects would read each other's
  // checkpoints. The digest restores uniqueness; the slug stays for debuggability.
  //
  // Note this intentionally diverges from Claude Code's plain encoding, which
  // tolerates collisions because it never uses them as a security boundary.
  const digest = createHash("sha256").update(raw).digest("hex").slice(0, 10);
  return `${readable}-${digest}`;
}

export function resolveConfig(options?: PluginOptions): TokenOptimizerConfig {
  const opts = (options ?? {}) as Record<string, unknown>;
  const features = (opts.features ?? {}) as Record<string, unknown>;

  return {
    // Explicit opts win; env var is picked up later by resolveDataDir so that
    // TOKEN_OPTIMIZER_DATA_DIR still works when no options are passed at all.
    dataDir: typeof opts.dataDir === "string" && opts.dataDir.trim() ? opts.dataDir.trim() : undefined,
    qualityWindow: intEnv(
      "TOKEN_OPTIMIZER_QUALITY_WINDOW",
      typeof opts.qualityWindow === "number" ? opts.qualityWindow : 20,
    ),
    toolCallWarnThreshold:
      opts.toolCallWarnThreshold === null
        ? null
        : intEnv("TOKEN_OPTIMIZER_TOOL_CALL_WARN", typeof opts.toolCallWarnThreshold === "number" ? opts.toolCallWarnThreshold : 25),
    toolCallCriticalThreshold:
      opts.toolCallCriticalThreshold === null
        ? null
        : intEnv("TOKEN_OPTIMIZER_TOOL_CALL_CRITICAL", typeof opts.toolCallCriticalThreshold === "number" ? opts.toolCallCriticalThreshold : 40),
    checkpointMaxFiles: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_FILES", 10),
    checkpointTtlSeconds: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_TTL", 300),
    checkpointRetentionDays: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS", 7),
    checkpointRetentionMax: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX", 50),
    // Restored prior-session content is injected into the system prompt, so the
    // match must be genuinely relevant before it earns that trust (0.6, not 0.3).
    relevanceThreshold: floatEnv("TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD", 0.6),
    checkpointCooldownSeconds: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_COOLDOWN_SECONDS", 90),
    checkpointMaxChars: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_MAX_CHARS", 2000),
    // Fresh-session nudge firing thresholds. Overridable via PluginOptions like
    // every other tunable (previously env-only, inconsistent with the rest).
    freshNudgeQualityThreshold: intEnv(
      "TOKEN_OPTIMIZER_FRESH_NUDGE_QUALITY",
      typeof opts.freshNudgeQualityThreshold === "number" ? opts.freshNudgeQualityThreshold : 70,
    ),
    freshNudgeMinFillPct: intEnv(
      "TOKEN_OPTIMIZER_FRESH_NUDGE_MIN_FILL",
      typeof opts.freshNudgeMinFillPct === "number" ? opts.freshNudgeMinFillPct : 50,
    ),
    features: {
      qualityNudges: features.qualityNudges !== false && boolEnv("TOKEN_OPTIMIZER_NUDGES", true),
      loopDetection: features.loopDetection !== false && boolEnv("TOKEN_OPTIMIZER_LOOP_DETECTION", true),
      smartCompaction: features.smartCompaction !== false && boolEnv("TOKEN_OPTIMIZER_SMART_COMPACTION", true),
      continuity: features.continuity !== false && boolEnv("TOKEN_OPTIMIZER_CONTINUITY", true),
      activityTracking: features.activityTracking !== false && boolEnv("TOKEN_OPTIMIZER_ACTIVITY", true),
      trends: features.trends !== false && boolEnv("TOKEN_OPTIMIZER_TRENDS", true),
    },
  };
}
