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
} from "./session-parser";
import { runAllDetectors } from "./waste-detectors";
import { captureCheckpoint, captureCheckpointV2, restoreCheckpoint, cleanupCheckpoints } from "./smart-compact";
import { handleReadBefore, handleWriteAfter, clearCache } from "./read-cache";
import { AuditReport, AgentRun, totalTokens } from "./models";
import { buildDashboardData, writeDashboard } from "./dashboard";
import { resetPricingCache } from "./pricing";
import { auditContext } from "./context-audit";
import { scoreQuality } from "./quality";

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
  const data = buildDashboardData(runs, report, qualityReport, contextAudit);
  return writeDashboard(data);
}

// ---------------------------------------------------------------------------
// Plugin registration (called by OpenClaw plugin loader)
// ---------------------------------------------------------------------------

export default definePluginEntry({
  id: "token-optimizer",
  name: "Token Optimizer",
  description: "Token waste auditor for OpenClaw. Detects idle burns, model misrouting, and context bloat.",
  register(api: OpenClawApi) {
    api.logger.info("[token-optimizer] Plugin activated");

    // Register service so other plugins/skills can call our methods
    api.registerService("token-optimizer", {
      audit,
      scan,
      generateDashboard,
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
      const filepath = captureCheckpointV2(session) ?? captureCheckpoint(session);
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

    // Read Cache: intercept redundant reads (PreToolUse equivalent)
    api.on("agent:tool:before", (...args: unknown[]) => {
      const event = args[0] as {
        toolName?: string;
        toolInput?: Record<string, unknown>;
        agentId?: string;
        sessionId?: string;
        block?: (message: string) => void;
      } | undefined;

      if (!event?.toolName || event.toolName !== "Read") return;

      const result = handleReadBefore({
        toolName: event.toolName,
        toolInput: (event.toolInput ?? {}) as { file_path?: string; offset?: number; limit?: number },
        agentId: event.agentId ?? "unknown",
        sessionId: event.sessionId ?? "unknown",
      });

      if (result?.block && event.block) {
        event.block(result.message);
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
    });

    // Generate dashboard silently on session end
    api.on("session:end", () => {
      try {
        generateDashboard(30);
        api.logger.info("[token-optimizer] Dashboard regenerated on session end");
      } catch {
        // Silent failure, dashboard generation is non-critical
      }
    });
  },
});
