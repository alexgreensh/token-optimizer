import { homedir, platform } from "node:os";
import { join } from "node:path";
import type { PluginOptions } from "@opencode-ai/plugin";

export interface TokenOptimizerConfig {
  /**
   * Override the data directory. Defaults to the platform-appropriate global
   * location (XDG_DATA_HOME on Linux, ~/Library/Application Support on macOS,
   * %LOCALAPPDATA% on Windows). When unset, the resolved value is cached.
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

export function resolveDataDir(config: TokenOptimizerConfig): string {
  const from = config.dataDir ?? process.env.TOKEN_OPTIMIZER_DATA_DIR;
  if (from) {
    // Strip trailing "token-optimizer" so storage code can safely
    // join(dataDir, "token-optimizer", ...) without double nesting.
    // ponytail: guard, remove when storage paths are refactored to take base dir once
    if (from.endsWith("token-optimizer") || from.endsWith("token-optimizer/"))
      return from.replace(/\/?token-optimizer\/?$/, "");
    return from;
  }
  const home = homedir();
  switch (platform()) {
    case "darwin":
      return join(home, "Library", "Application Support");
    case "win32":
      return join(
        process.env.LOCALAPPDATA ?? join(home, "AppData", "Local"),
      );
    default:
      return join(
        process.env.XDG_DATA_HOME ?? join(home, ".local", "share"),
      );
  }
}

/**
 * Encode a project path the same way Claude Code does: replace every
 * non-alphanumeric character with `-`. Matching this convention means the
 * token-optimizer session directory structure follows the same project
 * identity scheme as Claude Code's own `~/.claude/projects/` directories,
 * making cross-tool project identification consistent.
 *
 * Examples:
 *   /Users/alex/my project  →  -Users-alex-my-project
 *   D:\\Code\\my app        →  D--Code-my-app
 */
export function hashProjectDir(worktree: string): string {
  return worktree.replace(/[^A-Za-z0-9]/g, "-");
}

export function resolveConfig(options?: PluginOptions): TokenOptimizerConfig {
  const opts = (options ?? {}) as Record<string, unknown>;
  const features = (opts.features ?? {}) as Record<string, unknown>;

  return {
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
