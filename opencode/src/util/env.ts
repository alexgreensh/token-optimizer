import type { PluginOptions } from "@opencode-ai/plugin";

export interface TokenOptimizerConfig {
  qualityWindow: number;
  toolCallWarnThreshold: number | null;
  toolCallCriticalThreshold: number | null;
  checkpointMaxFiles: number;
  checkpointTtlSeconds: number;
  checkpointRetentionDays: number;
  checkpointRetentionMax: number;
  relevanceThreshold: number;
  checkpointCooldownSeconds: number;
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
    relevanceThreshold: floatEnv("TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD", 0.3),
    checkpointCooldownSeconds: intEnv("TOKEN_OPTIMIZER_CHECKPOINT_COOLDOWN_SECONDS", 90),
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
