/**
 * Token Optimizer for OpenClaw - Plugin Entry Point
 *
 * Uses definePluginEntry() to register with the OpenClaw plugin system:
 * - api.registerService() for the token-optimizer service
 * - api.on() for lifecycle events
 * - api.logger for structured logging
 */

import * as fs from "fs";
import * as path from "path";
import {
  findOpenClawDir,
  scanAllSessions,
  classifyCronRuns,
  parseSession,
  parseSessionTurns,
  extractCostlyPrompts,
} from "./session-parser";
import {
  captureCheckpoint,
  captureCheckpointV2,
  restoreCheckpoint,
  cleanupCheckpoints,
  loadMessagesFromSessionFile,
} from "./smart-compact";
import { handleReadBefore, handleWriteAfter, clearCache } from "./read-cache";
import { AuditReport, AgentRun, totalTokens, CostlyPrompt } from "./models";
import { buildDashboardData, writeDashboard, buildAgentCostBreakdown } from "./dashboard";
export type { AgentCostBreakdown, DashboardData } from "./dashboard";
import { generateCoachData } from "./coach";
export { generateCoachData } from "./coach";
export type { CoachData, CoachPattern } from "./coach";
import { resetPricingCache } from "./pricing";
import { auditContext, getSkillUsageHistory } from "./context-audit";
import { scoreQuality } from "./quality";
import {
  buildRuntimeSnapshot,
  clearCheckpointState,
  getCheckpointHealth,
  type CheckpointTelemetrySummary,
  getCheckpointTelemetrySummary,
  markEvaluated,
  maybeDecideEditBatchCheckpoint,
  maybeDecidePreFanoutCheckpoint,
  maybeDecideSnapshotCheckpoint,
  registerWriteEvent,
  shouldEvaluateRuntimeState,
} from "./checkpoint-policy";
import { runAllDetectors, detectUnusedSkills } from "./waste-detectors";

// ---------------------------------------------------------------------------
// OpenClaw Plugin API types (minimal, avoids external dependency)
// ---------------------------------------------------------------------------

interface OpenClawApi {
  registerService(name: string, service: Record<string, unknown>): void;
  on(event: string, handler: (...args: unknown[]) => void): void;
  logger: {
    info(msg: string, ...args: unknown[]): void;
    warn(msg: string, ...args: unknown[]): void;
    error(msg: string, ...args: unknown[]): void;
  };
}

interface PluginEntryOptions {
  id: string;
  name: string;
  description: string;
  register: (api: OpenClawApi) => void;
}

function definePluginEntry(options: PluginEntryOptions): PluginEntryOptions {
  return options;
}

// ---------------------------------------------------------------------------
// Core audit logic (used by both plugin and CLI)
// ---------------------------------------------------------------------------

/**
 * Run a full audit: scan sessions, classify cron runs, detect waste.
 */
export function audit(days: number = 30): AuditReport | null {
  resetPricingCache();
  const openclawDir = findOpenClawDir();
  if (!openclawDir) {
    return null;
  }

  const runs = scanAllSessions(openclawDir, days);
  classifyCronRuns(openclawDir, runs);

  // Load config for Tier 1 detectors
  const config = loadConfig(openclawDir);

  const findings = runAllDetectors(runs, config);

  const totalCost = runs.reduce((sum, r) => sum + r.costUsd, 0);
  const totalTok = runs.reduce((sum, r) => sum + totalTokens(r.tokens), 0);
  const monthlySavings = findings.reduce(
    (sum, f) => sum + f.monthlyWasteUsd,
    0
  );
  const agents = Array.from(new Set(runs.map((r) => r.agentName)));

  return {
    scannedAt: new Date(),
    daysScanned: days,
    agentsFound: agents,
    totalSessions: runs.length,
    totalCostUsd: totalCost,
    totalTokens: totalTok,
    findings,
    monthlySavingsUsd: monthlySavings,
  };
}

/**
 * Scan sessions only (no waste detection). Returns raw AgentRun data.
 */
export function scan(days: number = 30): AgentRun[] | null {
  const openclawDir = findOpenClawDir();
  if (!openclawDir) return null;

  const runs = scanAllSessions(openclawDir, days);
  classifyCronRuns(openclawDir, runs);
  return runs;
}

/**
 * Parse a session JSONL file into per-turn token/cost data.
 *
 * Each entry in the returned array represents one user→assistant exchange,
 * with token counts, tools called, model used, and cost for that turn.
 * Returns an empty array if the file cannot be read or has no valid turns.
 */
export { parseSessionTurns };
export { extractTopic } from "./session-parser";

/**
 * Extract the top N costliest user prompts from a session JSONL file.
 *
 * Pairs each user message text with the token/cost data from the subsequent
 * assistant turn. Sidechain messages and tool-result-only turns are skipped.
 * Text is truncated to 120 characters.
 *
 * Returns CostlyPrompt[] sorted by costUsd descending, length <= topN (default 5).
 */
export { extractCostlyPrompts };
export type { CostlyPrompt };

/**
 * Load OpenClaw config for Tier 1 analysis.
 */
function loadConfig(openclawDir: string): Record<string, unknown> {
  const configPath = path.join(openclawDir, "config.json");

  if (!fs.existsSync(configPath)) return {};

  try {
    return JSON.parse(fs.readFileSync(configPath, "utf-8"));
  } catch {
    return {};
  }
}

/**
 * Generate the HTML dashboard, write to disk, return the file path.
 */
export function generateDashboard(days: number = 30): string | null {
  resetPricingCache();
  const openclawDir = findOpenClawDir();
  if (!openclawDir) return null;

  const runs = scanAllSessions(openclawDir, days);
  classifyCronRuns(openclawDir, runs);
  const config = loadConfig(openclawDir);
  const findings = runAllDetectors(runs, config);

  const totalCost = runs.reduce((sum, r) => sum + r.costUsd, 0);
  const totalTok = runs.reduce((sum, r) => sum + totalTokens(r.tokens), 0);
  const monthlySavings = findings.reduce((sum, f) => sum + f.monthlyWasteUsd, 0);
  const agents = Array.from(new Set(runs.map((r) => r.agentName)));

  const report: AuditReport = {
    scannedAt: new Date(),
    daysScanned: days,
    agentsFound: agents,
    totalSessions: runs.length,
    totalCostUsd: totalCost,
    totalTokens: totalTok,
    findings,
    monthlySavingsUsd: monthlySavings,
  };

  const contextAudit = auditContext(openclawDir);
  const qualityReport = scoreQuality(runs, contextAudit);

  // Build coach data
  const activeSkillNames = contextAudit.skills
    .filter((s) => !s.isArchived)
    .map((s) => s.name);
  const skillUsage = getSkillUsageHistory(runs);
  const unusedSkillFindings = detectUnusedSkills(activeSkillNames, skillUsage);
  const agentCosts = buildAgentCostBreakdown(runs);

  // Collect costly prompts from the 10 most recent sessions
  const recentSessions = [...runs]
    .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime())
    .slice(0, 10);
  const allCostlyPrompts: import("./models").CostlyPrompt[] = [];
  for (const session of recentSessions) {
    const prompts = extractCostlyPrompts(session.sourcePath, 3, openclawDir);
    allCostlyPrompts.push(...prompts);
  }
  allCostlyPrompts.sort((a, b) => b.costUsd - a.costUsd);
  const topCostlyPrompts = allCostlyPrompts.slice(0, 5);

  const coachData = generateCoachData(
    contextAudit,
    runs,
    topCostlyPrompts,
    agentCosts,
    unusedSkillFindings
  );

  const data = buildDashboardData(runs, report, qualityReport, contextAudit, coachData);
  return writeDashboard(data);
}

export function doctor(): Record<string, unknown> {
  const health = getCheckpointHealth();
  return {
    ok: health.issues.length === 0,
    checkpointRoot: health.checkpointRoot,
    sessionCount: health.sessionCount,
    checkpointCount: health.checkpointCount,
    policyCount: health.policyCount,
    pendingCount: health.pendingCount,
    checkpointBytes: health.checkpointBytes,
    recentCheckpointEvents: health.recentEventCount,
    lastCheckpointTrigger: health.lastTrigger,
    issues: health.issues,
  };
}

export function checkpointTelemetry(days: number = 7): CheckpointTelemetrySummary {
  return getCheckpointTelemetrySummary(days);
}

// ---------------------------------------------------------------------------
// Never-used skill detection (public API)
// ---------------------------------------------------------------------------

/**
 * Returns a skill-name -> invocation-count map built from tool call history
 * across all provided sessions. Use alongside auditContext().skills to feed
 * detectUnusedSkills().
 */
export { getSkillUsageHistory } from "./context-audit";

/**
 * Returns WasteFinding objects for installed skills that have zero invocations.
 * Pass auditContext().skills.active.map(s => s.name) as `installed`, and
 * getSkillUsageHistory(sessions) as `usageMap`.
 */
export { detectUnusedSkills } from "./waste-detectors";

// ---------------------------------------------------------------------------
// Plugin registration (called by OpenClaw plugin loader)
// ---------------------------------------------------------------------------

export default definePluginEntry({
  id: "token-optimizer",
  name: "Token Optimizer",
  description: "Token waste auditor for OpenClaw. Detects idle burns, model misrouting, and context bloat.",
  register(api: OpenClawApi) {
    api.logger.info("[token-optimizer] Plugin activated");
    const openclawDir = findOpenClawDir();
    const contextAudit = openclawDir ? auditContext(openclawDir) : null;

    // Register service so other plugins/skills can call our methods
    api.registerService("token-optimizer", {
      audit,
      scan,
      generateDashboard,
      doctor,
    });

    // Log on gateway startup
    api.on("gateway:startup", () => {
      api.logger.info("[token-optimizer] Gateway started, ready to audit");

      // Clean up old checkpoints on startup
      const cleaned = cleanupCheckpoints(7);
      if (cleaned > 0) {
        api.logger.info(
          `[token-optimizer] Cleaned ${cleaned} old checkpoint(s)`
        );
      }
    });

    // Log on agent bootstrap
    api.on("agent:bootstrap", (...args: unknown[]) => {
      const agentId =
        typeof args[0] === "object" && args[0] !== null
          ? (args[0] as Record<string, unknown>).agentId
          : undefined;
      api.logger.info(
        `[token-optimizer] Agent bootstrapped: ${agentId ?? "unknown"}`
      );
    });

    // Smart Compaction v2: capture before compaction (intelligent extraction)
    api.on("session:compact:before", (...args: unknown[]) => {
      const session = args[0] as {
        sessionId: string;
        messages?: Array<{ role: string; content: string; timestamp?: string }>;
      } | undefined;

      if (!session?.sessionId) {
        api.logger.warn(
          "[token-optimizer] compact:before fired without session data"
        );
        return;
      }

      // Try v2 (intelligent extraction), fall back to v1
      const filepath = captureCheckpointV2(session, 10, {
        trigger: "compact",
        eventKind: "compact-before",
      }) ?? captureCheckpoint(session, 20, {
        trigger: "compact",
        eventKind: "compact-before",
      });
      if (filepath) {
        api.logger.info(
          `[token-optimizer] Checkpoint saved: ${filepath}`
        );
      }

      // Clear read-cache on compaction (context is reset, cache entries are stale)
      clearCache("default", session.sessionId);
    });

    // Smart Compaction: restore after compaction
    api.on("session:compact:after", (...args: unknown[]) => {
      const session = args[0] as {
        sessionId: string;
        inject?: (content: string) => void;
      } | undefined;

      if (!session?.sessionId) return;

      const checkpoint = restoreCheckpoint(session.sessionId);
      if (checkpoint && session.inject) {
        session.inject(checkpoint);
        api.logger.info(
          `[token-optimizer] Checkpoint restored for session ${session.sessionId}`
        );
      }
    });

    api.on("session:patch", (...args: unknown[]) => {
      const event = args[0] as {
        sessionId?: string;
        agentId?: string;
      } | undefined;
      if (!event?.sessionId || !openclawDir) return;
      maybeCheckpointFromRuntimeSnapshot(openclawDir, contextAudit, event.agentId, event.sessionId, api, "session-patch");
    });

    // Read Cache: intercept redundant reads (PreToolUse equivalent)
    api.on("agent:tool:before", (...args: unknown[]) => {
      const event = args[0] as {
        toolName?: string;
        toolInput?: Record<string, unknown>;
        agentId?: string;
        sessionId?: string;
        block?: (message: string) => void;
      } | undefined;

      if (!event?.toolName) return;

      if (event.toolName === "Read") {
        const result = handleReadBefore({
          toolName: event.toolName,
          toolInput: (event.toolInput ?? {}) as { file_path?: string; offset?: number; limit?: number },
          agentId: event.agentId ?? "unknown",
          sessionId: event.sessionId ?? "unknown",
        });

        if (result?.block && event.block) {
          event.block(result.message);
        }
      }

      if (
        openclawDir &&
        event.sessionId &&
        (event.toolName === "Agent" || event.toolName === "Task")
      ) {
        const decision = maybeDecidePreFanoutCheckpoint(event.sessionId);
        const snapshot = decision
          ? buildRuntimeEventContext(
              openclawDir,
              contextAudit,
              event.agentId,
              event.sessionId,
              "tool-before",
              event.toolName
            )
          : null;
        captureDecisionCheckpoint(decision, snapshot, api);
      }
    });

    // Read Cache: invalidate on file writes (PostToolUse equivalent)
    api.on("agent:tool:after", (...args: unknown[]) => {
      const event = args[0] as {
        toolName?: string;
        toolInput?: Record<string, unknown>;
        agentId?: string;
        sessionId?: string;
      } | undefined;

      if (!event?.toolName) return;

      handleWriteAfter({
        toolName: event.toolName,
        toolInput: (event.toolInput ?? {}) as { file_path?: string; offset?: number; limit?: number },
        agentId: event.agentId ?? "unknown",
        sessionId: event.sessionId ?? "unknown",
      });

      if (!openclawDir || !event.sessionId) return;

      const filePath =
        typeof event.toolInput?.file_path === "string"
          ? event.toolInput.file_path
          : undefined;
      let milestoneSnapshot: RuntimeEventContext | null = null;
      if (isWriteTool(event.toolName)) {
        registerWriteEvent(event.sessionId, filePath);
        const decision = maybeDecideEditBatchCheckpoint(event.sessionId);
        if (decision) {
          milestoneSnapshot = buildRuntimeEventContext(
            openclawDir,
            contextAudit,
            event.agentId,
            event.sessionId,
            "tool-after",
            event.toolName
          );
        }
        captureDecisionCheckpoint(decision, milestoneSnapshot, api);
      }

      maybeCheckpointFromRuntimeSnapshot(
        openclawDir,
        contextAudit,
        event.agentId,
        event.sessionId,
        api,
        "tool-after",
        milestoneSnapshot
      );
    });

    // Generate dashboard silently on session end
    api.on("session:end", (...args: unknown[]) => {
      try {
        const event = args[0] as {
          sessionId?: string;
          agentId?: string;
        } | undefined;
        if (openclawDir && event?.sessionId) {
          maybeCheckpointFromRuntimeSnapshot(openclawDir, contextAudit, event.agentId, event.sessionId, api, "session-end");
          clearCheckpointState(event.sessionId);
        }
        generateDashboard(30);
        api.logger.info("[token-optimizer] Dashboard regenerated on session end");
      } catch {
        // Silent failure, dashboard generation is non-critical
      }
    });
  },
});

type RuntimeEventKind = "session-patch" | "tool-before" | "tool-after" | "session-end";

interface RuntimeEventContext {
  sessionId: string;
  sessionFile: string;
  fillPct: number;
  qualityScore: number;
  toolName?: string;
  eventKind: RuntimeEventKind;
  model: string;
}

function resolveSessionFile(openclawDir: string, agentId: string | undefined, sessionId: string): string | null {
  if (agentId) {
    const direct = path.join(openclawDir, "agents", agentId, "sessions", `${sessionId}.jsonl`);
    if (fs.existsSync(direct)) return direct;
  }

  const agentsDir = path.join(openclawDir, "agents");
  if (!fs.existsSync(agentsDir)) return null;
  for (const entry of fs.readdirSync(agentsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const candidate = path.join(agentsDir, entry.name, "sessions", `${sessionId}.jsonl`);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function isWriteTool(toolName: string): boolean {
  return toolName === "Edit" || toolName === "Write" || toolName === "MultiEdit" || toolName === "NotebookEdit";
}

function buildRuntimeEventContext(
  openclawDir: string,
  contextAudit: ReturnType<typeof auditContext> | null,
  agentId: string | undefined,
  sessionId: string,
  eventKind: RuntimeEventKind,
  toolName?: string
): RuntimeEventContext | null {
  const sessionFile = resolveSessionFile(openclawDir, agentId, sessionId);
  if (!sessionFile) return null;

  const agentName = agentId ?? path.basename(path.dirname(path.dirname(sessionFile)));
  const run = parseSession(sessionFile, agentName, openclawDir);
  if (!run) return null;

  const snapshot = buildRuntimeSnapshot(run, contextAudit);
  return {
    sessionId,
    sessionFile,
    fillPct: snapshot.fillPct,
    qualityScore: snapshot.qualityScore,
    toolName,
    eventKind,
    model: run.model,
  };
}

function maybeCheckpointFromRuntimeSnapshot(
  openclawDir: string,
  contextAudit: ReturnType<typeof auditContext> | null,
  agentId: string | undefined,
  sessionId: string,
  api: OpenClawApi,
  eventKind: RuntimeEventKind,
  precomputedSnapshot: RuntimeEventContext | null = null
): void {
  if (!shouldEvaluateRuntimeState(sessionId)) return;
  const snapshot =
    precomputedSnapshot ??
    buildRuntimeEventContext(openclawDir, contextAudit, agentId, sessionId, eventKind);
  if (!snapshot) return;
  markEvaluated(sessionId);
  const decision = maybeDecideSnapshotCheckpoint(sessionId, {
    fillPct: snapshot.fillPct,
    qualityScore: snapshot.qualityScore,
  });
  captureDecisionCheckpoint(decision, snapshot, api);
}

function captureDecisionCheckpoint(
  decision: { trigger: string; fillPct?: number; qualityScore?: number } | null,
  snapshot: RuntimeEventContext | null,
  api: OpenClawApi
): void {
  if (!decision || !snapshot) return;
  const enrichedDecision = {
    ...decision,
    fillPct: decision.fillPct ?? snapshot.fillPct,
    qualityScore: decision.qualityScore ?? snapshot.qualityScore,
  };

  const session = {
    sessionId: snapshot.sessionId,
    messages: loadMessagesFromSessionFile(snapshot.sessionFile),
  };

  const filepath =
    captureCheckpointV2(session, 10, {
      trigger: enrichedDecision.trigger,
      fillPct: enrichedDecision.fillPct,
      qualityScore: enrichedDecision.qualityScore,
      toolName: snapshot.toolName,
      eventKind: snapshot.eventKind,
      model: snapshot.model,
    }) ??
    captureCheckpoint(session, 20, {
      trigger: enrichedDecision.trigger,
      fillPct: enrichedDecision.fillPct,
      qualityScore: enrichedDecision.qualityScore,
      toolName: snapshot.toolName,
      eventKind: snapshot.eventKind,
      model: snapshot.model,
    });

  if (filepath) {
    api.logger.info(`[token-optimizer] Checkpoint saved (${enrichedDecision.trigger}): ${filepath}`);
  }
}
