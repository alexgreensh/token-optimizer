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
import { computeRealizedSavings } from "./savings";
import {
  captureCheckpoint,
  captureCheckpointV2,
  restoreCheckpoint,
  cleanupCheckpoints,
  loadMessagesFromSessionFile,
} from "./smart-compact";
import {
  handleReadBefore,
  handleWriteAfter,
  clearCache,
  getActiveReadTokens,
  logSavingsEvent,
  recordHintServe,
} from "./read-cache";
import { estimateTokensFromBytes } from "./token-estimate";
import { AuditReport, AgentRun, totalTokens, CostlyPrompt } from "./models";
import { buildDashboardData, writeDashboard, buildAgentCostBreakdown } from "./dashboard";
export type { AgentCostBreakdown, DashboardData } from "./dashboard";
import { generateCoachData } from "./coach";
export { generateCoachData } from "./coach";
export type { CoachData, CoachPattern } from "./coach";
import { resetPricingCache } from "./pricing";
import { auditContext, getSkillUsageHistory } from "./context-audit";
import {
  scoreQuality,
  buildFreshSessionNudgeMessage,
  freshSessionSavingsEstimate,
  FRESH_NUDGE_QUALITY_THRESHOLD,
  FRESH_NUDGE_MIN_FILL,
} from "./quality";
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
import {
  findBestContinuityCheckpoint,
  buildContinuityHint,
  extractHintedPaths,
  RELEVANCE_THRESHOLD as _CONTINUITY_THRESHOLD,
  tryBuildResumeLeanHint,
  isResumeIntent,
} from "./continuity";
import {
  V5_FEATURES,
  isV5Enabled,
  setV5,
  listV5Features,
  type V5FeatureId,
} from "./v5-features";
import {
  logCompressionEvent,
  getCompressionSummary,
  pruneOldEvents,
} from "./telemetry";
export {
  V5_FEATURES,
  isV5Enabled,
  setV5,
  listV5Features,
  type V5FeatureId,
} from "./v5-features";
export {
  logCompressionEvent,
  getCompressionSummary,
  pruneOldEvents,
  type CompressionSummary,
  type CompressionEvent,
} from "./telemetry";

// ---------------------------------------------------------------------------
// OpenClaw Plugin API types (minimal, avoids external dependency)
// ---------------------------------------------------------------------------

interface OpenClawApi {
  registerService(service: {
    id: string;
    start: (context: OpenClawServiceContext) => void | Promise<void>;
    stop?: (context: OpenClawServiceContext) => void | Promise<void>;
  }): void;
  on(event: string, handler: (...args: unknown[]) => unknown | Promise<unknown>): void;
  session: {
    workflow: {
      enqueueNextTurnInjection(input: {
        sessionKey: string;
        text: string;
        idempotencyKey?: string;
        placement?: "prepend_context" | "append_context";
      }): Promise<{ enqueued: boolean }>;
    };
  };
  logger: {
    info(msg: string, ...args: unknown[]): void;
    warn(msg: string, ...args: unknown[]): void;
    error(msg: string, ...args: unknown[]): void;
    debug?(msg: string, ...args: unknown[]): void;
  };
}

interface OpenClawServiceContext {
  logger: OpenClawApi["logger"];
}

interface OpenClawHookContext {
  agentId?: string;
  sessionId?: string;
  sessionKey?: string;
  trigger?: string;
  jobId?: string;
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
// Bounded per-session tracking
// ---------------------------------------------------------------------------

/**
 * Upper bound on the number of sessions held in the in-memory continuity and
 * fresh-nudge collections. These are normally pruned on session_end, but a
 * session that ends uncleanly (process killed, network disconnect) never fires
 * that event, so without a cap the collections would grow for the gateway's
 * entire lifetime. The bound evicts the oldest tracked session once exceeded.
 */
const MAX_TRACKED_SESSIONS = 2000;

/**
 * add() to a Set with oldest-first eviction past MAX_TRACKED_SESSIONS.
 *
 * No recency refresh on a repeat add: the Set guards here
 * (_continuityInjectedSessions, _freshNudgeFiredSessions) are add-once
 * fire-guards, so a key is never re-added and a refresh would be dead code. A
 * guard could therefore age out while its session is still live, but only once
 * the cap (a leaked-session backstop) is exceeded -- the cost is at most one
 * duplicate injection/nudge for that session, which is acceptable.
 */
function boundedSetAdd(set: Set<string>, key: string): void {
  set.add(key);
  while (set.size > MAX_TRACKED_SESSIONS) {
    const oldest = set.keys().next().value;
    if (oldest === undefined || oldest === key) break;
    set.delete(oldest);
  }
}

/**
 * set() on a Map with oldest-first eviction past MAX_TRACKED_SESSIONS. Unlike
 * the Set helper, this refreshes recency on update (delete + re-set) because
 * _freshNudgePriorScores is re-set on every agent turn, so refreshing keeps
 * an actively-scored session from being evicted while still in use.
 */
function boundedMapSet<V>(map: Map<string, V>, key: string, value: V): void {
  if (map.has(key)) map.delete(key);
  map.set(key, value);
  while (map.size > MAX_TRACKED_SESSIONS) {
    const oldest = map.keys().next().value;
    if (oldest === undefined || oldest === key) break;
    map.delete(oldest);
  }
}

/**
 * Per-session monotonic compaction counter. Used as a fallback discriminator
 * in the after_compaction idempotencyKey when the host omits messageCount
 * (and compactedCount). Without this, every compaction on a session where
 * sessionKey === sessionId and both host fields are absent produces the
 * identical key `checkpoint-restore:SID:SID:0`, so the 2nd+ compaction gets
 * enqueued:false and its checkpoint restore is silently dropped.
 *
 * The counter is incremented once per after_compaction event for a given
 * sessionKey, so retries of the SAME compaction (same event, same messageCount)
 * produce the same key, but successive compactions produce distinct keys.
 */
const _compactionCounter = new Map<string, number>();

function nextCompactionSeq(sessionKey: string): number {
  const seq = (_compactionCounter.get(sessionKey) ?? 0) + 1;
  boundedMapSet(_compactionCounter, sessionKey, seq);
  return seq;
}

/**
 * Module-level logger reference, set during register(). Lets standalone
 * helper functions (buildRuntimeEventContext) emit debug logs without
 * threading api through every call.
 */
let _logger: OpenClawApi["logger"] | null = null;

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

  const savings = computeRealizedSavings(openclawDir, days);

  const data = buildDashboardData(runs, report, qualityReport, contextAudit, coachData, savings);
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
// Safe event handler wrapper (prevents unhandled throws from crashing gateway)
// ---------------------------------------------------------------------------

function safeOn(
  api: OpenClawApi,
  event: string,
  handler: (...args: unknown[]) => unknown | Promise<unknown>
): void {
  api.on(event, async (...args: unknown[]) => {
    try {
      return await handler(...args);
    } catch (err) {
      api.logger.error(`[token-optimizer] ${event} handler error: ${err}`);
      return undefined;
    }
  });
}

// ---------------------------------------------------------------------------
// Plugin registration (called by OpenClaw plugin loader)
// ---------------------------------------------------------------------------

export default definePluginEntry({
  id: "token-optimizer-openclaw",
  name: "Token Optimizer",
  description: "Token waste auditor for OpenClaw. Detects idle burns, model misrouting, and context bloat.",
  register(api: OpenClawApi) {
    api.logger.info("[token-optimizer] Plugin activated");
    _logger = api.logger;
    const openclawDir = findOpenClawDir();

    // Lazy-refresh context audit (TTL-based) so long-running gateways stay current
    let _ctxCache: ReturnType<typeof auditContext> | null = null;
    let _ctxTs = 0;
    const CTX_TTL = 5 * 60 * 1000; // 5 minutes
    function freshContextAudit(): ReturnType<typeof auditContext> | null {
      if (!openclawDir) return null;
      const now = Date.now();
      if (_ctxCache && (now - _ctxTs) < CTX_TTL) return _ctxCache;
      try {
        _ctxCache = auditContext(openclawDir);
      } catch {
        _ctxCache = null;
      }
      _ctxTs = now;
      return _ctxCache;
    }

    // Cross-session continuity: tracks sessions that have already received
    // a topic-matched prompt contribution so we fire at most once per session.
    const _continuityInjectedSessions = new Set<string>();

    // Fresh-session nudge: per-session state.
    // _freshNudgeFiredSessions: once-per-session dedup; cleared on session_end.
    // _freshNudgePriorScores: last known quality score for each session; used to
    //   detect the "prior score established" condition that mirrors Python's
    //   _nudge_previous_score check (suppresses firing on the very first scored
    //   turn, i.e. right after a compaction/fresh session when there is no baseline).
    const _freshNudgeFiredSessions = new Set<string>();
    const _freshNudgePriorScores = new Map<string, number>();

    api.registerService({
      id: "token-optimizer",
      start: (context) => {
        context.logger.info("[token-optimizer] Service started");
      },
    });

    // Log on gateway startup
    safeOn(api, "gateway_start", () => {
      api.logger.info("[token-optimizer] Gateway started, ready to audit");

      // Clean up old checkpoints on startup
      const cleaned = cleanupCheckpoints(7);
      if (cleaned > 0) {
        api.logger.info(
          `[token-optimizer] Cleaned ${cleaned} old checkpoint(s)`
        );
      }

      // v5 telemetry hygiene: drop events older than 90 days so the JSONL
      // file does not grow unbounded across long-running gateways.
      try {
        const dropped = pruneOldEvents(90);
        if (dropped > 0) {
          api.logger.info(
            `[token-optimizer] Pruned ${dropped} v5 telemetry event(s) older than 90d`
          );
        }
      } catch {
        // Never crash the gateway over telemetry cleanup.
      }
    });

    // Log on agent bootstrap
    safeOn(api, "before_agent_start", (...args: unknown[]) => {
      const context = args[1] as OpenClawHookContext | undefined;
      api.logger.info(
        `[token-optimizer] Agent bootstrapped: ${context?.agentId ?? "unknown"}`
      );
    });

    // Smart Compaction v2: capture before compaction (intelligent extraction)
    safeOn(api, "before_compaction", (...args: unknown[]) => {
      const event = args[0] as {
        messages?: Array<{ role: string; content: string; timestamp?: string }>;
      } | undefined;
      const context = args[1] as OpenClawHookContext | undefined;
      const sessionId = context?.sessionId ?? context?.sessionKey;

      if (!sessionId) {
        api.logger.warn(
          "[token-optimizer] before_compaction fired without a session identity"
        );
        return;
      }
      const session = { sessionId, messages: event?.messages };

      // Try v2 (intelligent extraction), fall back to v1
      let filepath: string | null = null;
      try {
        filepath = captureCheckpointV2(session, 10, {
          trigger: "compact",
          eventKind: "compact-before",
        });
      } catch { /* v2 threw, try v1 */ }
      if (!filepath) {
        try {
          filepath = captureCheckpoint(session, 20, {
            trigger: "compact",
            eventKind: "compact-before",
          });
        } catch { /* v1 also failed */ }
      }
      if (filepath) {
        api.logger.info(
          `[token-optimizer] Checkpoint saved: ${filepath}`
        );
      }

      // Clear read-cache on compaction (context is reset, cache entries are stale)
      clearCache(context?.agentId ?? "default", session.sessionId);
    });

    // Smart Compaction: restore after compaction
    safeOn(api, "after_compaction", async (...args: unknown[]) => {
      const event = args[0] as {
        compactedCount?: number;
        previousSessionId?: string;
        messageCount?: number;
        tokenCount?: number;
      } | undefined;
      const context = args[1] as OpenClawHookContext | undefined;
      const sessionId = context?.sessionId ?? context?.sessionKey;

      if (!sessionId || !context?.sessionKey) {
        api.logger.warn(
          "[token-optimizer] after_compaction fired without sessionId/sessionKey; skipping restore"
        );
        return;
      }

      const checkpoint = restoreCheckpoint(sessionId);
      if (checkpoint) {
        // Build a unique-per-compaction idempotency key that is still stable
        // for retries of the SAME compaction.
        //
        // Primary discriminator: messageCount (changes per compaction, stable
        // within one). When the host omits messageCount, fall back to a
        // per-session monotonic compaction counter so successive compactions
        // still get distinct keys. Without this, when sessionKey === sessionId
        // and both previousSessionId and compactedCount are absent, every
        // compaction yields the identical key
        // `checkpoint-restore:SID:SID:0`, so the 2nd+ compaction gets
        // enqueued:false and its checkpoint restore is silently dropped.
        const prevId = event?.previousSessionId ?? sessionId;
        const compactedCount = event?.compactedCount ?? 0;
        const messageCount = event?.messageCount;
        const discriminator =
          typeof messageCount === "number"
            ? `msg:${messageCount}`
            : `seq:${nextCompactionSeq(context.sessionKey)}`;
        const idempotencyKey =
          `checkpoint-restore:${context.sessionKey}:` +
          `${prevId}:${compactedCount}:${discriminator}`;

        const result = await api.session.workflow.enqueueNextTurnInjection({
          sessionKey: context.sessionKey,
          text: checkpoint,
          placement: "prepend_context",
          idempotencyKey,
        });
        if (!result.enqueued) {
          api.logger.info(
            `[token-optimizer] Checkpoint restore already queued for session ${sessionId}`
          );
          return;
        }
        api.logger.info(
          `[token-optimizer] Checkpoint restore queued for session ${sessionId}`
        );

        // Credit the avoided reconstruction.
        // Floor = checkpoint content byte size / 3.3 (calibrated estimator).
        // Active = sum of tokensEst for files read >=2x this session (working set).
        // credited = min(200 000, max(floor, active)).
        try {
          const floorTokens = estimateTokensFromBytes(Buffer.byteLength(checkpoint, "utf-8"));
          const activeTokens = getActiveReadTokens(context.agentId ?? "default", sessionId);
          const credited = Math.min(200_000, Math.max(floorTokens, activeTokens));
          if (credited > 0) {
            logSavingsEvent("checkpoint_restore", credited, sessionId, "queued after compact");
          }
        } catch { /* best-effort: never break restore */ }
      }
    });

    safeOn(api, "agent_turn_prepare", (...args: unknown[]) => {
      const event = args[0] as { prompt?: string } | undefined;
      const context = args[1] as OpenClawHookContext | undefined;
      const sessionId = context?.sessionId ?? context?.sessionKey;
      const promptText = event?.prompt?.trim();
      const isUserTurn = context?.trigger === "user" && !context.jobId;
      const contributions: string[] = [];

      if (!sessionId || !promptText || !openclawDir) {
        if (!sessionId) {
          api.logger.warn("[token-optimizer] agent_turn_prepare without sessionId/sessionKey; skipping prompt contributions");
        }
        return;
      }
      if (!isUserTurn) {
        // This fires on every internal (non-user) turn. Lowered from info to
        // debug so it does not spam INFO on gateways with many subagent turns.
        api.logger.debug?.(`[token-optimizer] Skipping non-user agent turn for session ${sessionId}`);
        return;
      }

      // Continuity is first-user-turn-only. Its guard is independent from the
      // nudge guard, which must be evaluated on later turns as fill increases.
      if (!_continuityInjectedSessions.has(sessionId)) {
        try {
          boundedSetAdd(_continuityInjectedSessions, sessionId);
          const cwd = process.cwd();
          const resumeLeanBlock = tryBuildResumeLeanHint(promptText, sessionId, cwd, logSavingsEvent);
          if (resumeLeanBlock) {
            contributions.push(resumeLeanBlock);
            api.logger.info(`[token-optimizer] Cold-resume-lean added for session ${sessionId}`);
          } else if (isResumeIntent(promptText) && cwd) {
            api.logger.info(`[token-optimizer] Resume intent without same-project checkpoint for session ${sessionId}; suppressing cross-project fallthrough.`);
          } else {
            const candidate = findBestContinuityCheckpoint(promptText, sessionId, cwd);
            if (candidate) {
              contributions.push(buildContinuityHint(candidate, promptText, cwd));
              try {
                const hintedPaths = extractHintedPaths(candidate.content);
                if (hintedPaths.length > 0) recordHintServe(sessionId, hintedPaths);
              } catch { /* best-effort */ }
              api.logger.info(`[token-optimizer] Cross-session continuity added for session ${sessionId} (score=${candidate.score.toFixed(2)}, source=${candidate.entry.sessionDirName})`);
            } else {
              api.logger.info(`[token-optimizer] No matching prior checkpoint for session ${sessionId} (threshold=${_CONTINUITY_THRESHOLD})`);
            }
          }
        } catch (err) {
          api.logger.warn(`[token-optimizer] Cross-session continuity error: ${err}`);
        }
      }

      // Fresh-session nudge is evaluated on every eligible turn, independently
      // of continuity, so its prior-score baseline survives early low-fill turns.
      if (!_freshNudgeFiredSessions.has(sessionId)) {
        try {
          const nudgeSnapshot = buildRuntimeEventContext(
            openclawDir, freshContextAudit(), context?.agentId, sessionId, "agent-turn-prepare"
          );
          if (nudgeSnapshot) {
            const hasPriorScore = _freshNudgePriorScores.has(sessionId);
            const nudgeMsg = buildFreshSessionNudgeMessage(
              nudgeSnapshot.qualityScore, nudgeSnapshot.fillPct, hasPriorScore,
              nudgeSnapshot.model, nudgeSnapshot.contextWindow
            );
            boundedMapSet(_freshNudgePriorScores, sessionId, nudgeSnapshot.qualityScore);
            if (nudgeMsg) {
              boundedSetAdd(_freshNudgeFiredSessions, sessionId);
              const { savedTokens } = freshSessionSavingsEstimate(nudgeSnapshot.fillPct, nudgeSnapshot.model, nudgeSnapshot.contextWindow);
              logSavingsEvent("fresh_session_nudge", savedTokens, sessionId,
                `score=${nudgeSnapshot.qualityScore} fill_pct=${nudgeSnapshot.fillPct.toFixed(1)}`,
                {openclawDir, model: nudgeSnapshot.model});
              logCompressionEvent({
                feature: "fresh_session_nudge", sessionId,
                detail: `score=${nudgeSnapshot.qualityScore} fill_pct=${nudgeSnapshot.fillPct.toFixed(1)} est_saved=${savedTokens}`,
                verified: false,
              });
              contributions.push(nudgeMsg);
              api.logger.info(`[token-optimizer] Fresh-session nudge added for session ${sessionId} (score=${nudgeSnapshot.qualityScore}, fill=${nudgeSnapshot.fillPct.toFixed(1)}%)`);
            }
          }
        } catch (err) {
          api.logger.warn(`[token-optimizer] Fresh-session nudge error: ${err}`);
        }
      }

      maybeCheckpointFromRuntimeSnapshot(
        openclawDir, freshContextAudit(), context?.agentId, sessionId, api, "agent-turn-prepare"
      );
      return contributions.length > 0
        ? { prependContext: contributions.join("\n\n") }
        : undefined;
    });

    // Read Cache: intercept redundant reads (PreToolUse equivalent)
    safeOn(api, "before_tool_call", (...args: unknown[]) => {
      const event = args[0] as {
        toolName?: string;
        params?: Record<string, unknown>;
      } | undefined;
      const context = args[1] as OpenClawHookContext | undefined;
      const sessionId = context?.sessionId ?? context?.sessionKey;

      if (!event?.toolName) return;

      if (event.toolName === "Read") {
        const result = handleReadBefore({
          toolName: event.toolName,
          toolInput: (event.params ?? {}) as { file_path?: string; offset?: number; limit?: number },
          agentId: context?.agentId ?? "unknown",
          sessionId: sessionId ?? "unknown",
        });

        if (result?.block) {
          return { block: true, blockReason: result.message };
        }
      }

      if (
        openclawDir &&
        sessionId &&
        (event.toolName === "Agent" || event.toolName === "Task")
      ) {
        const decision = maybeDecidePreFanoutCheckpoint(sessionId);
        const snapshot = decision
          ? buildRuntimeEventContext(
              openclawDir,
              freshContextAudit(),
              context?.agentId,
              sessionId,
              "tool-before",
              event.toolName
            )
          : null;
        captureDecisionCheckpoint(decision, snapshot, api);
      }
    });

    // Read Cache: invalidate on file writes (PostToolUse equivalent)
    safeOn(api, "after_tool_call", (...args: unknown[]) => {
      const event = args[0] as {
        toolName?: string;
        params?: Record<string, unknown>;
      } | undefined;
      const context = args[1] as OpenClawHookContext | undefined;
      const sessionId = context?.sessionId ?? context?.sessionKey;

      if (!event?.toolName) return;

      handleWriteAfter({
        toolName: event.toolName,
        toolInput: (event.params ?? {}) as { file_path?: string; offset?: number; limit?: number },
        agentId: context?.agentId ?? "unknown",
        sessionId: sessionId ?? "unknown",
      });

      if (!openclawDir || !sessionId) return;

      const filePath =
        typeof event.params?.file_path === "string"
          ? event.params.file_path
          : undefined;
      let milestoneSnapshot: RuntimeEventContext | null = null;
      if (isWriteTool(event.toolName)) {
        registerWriteEvent(sessionId, filePath);
        const decision = maybeDecideEditBatchCheckpoint(sessionId);
        if (decision) {
          milestoneSnapshot = buildRuntimeEventContext(
            openclawDir,
            freshContextAudit(),
            context?.agentId,
            sessionId,
            "tool-after",
            event.toolName
          );
        }
        captureDecisionCheckpoint(decision, milestoneSnapshot, api);
      }

      maybeCheckpointFromRuntimeSnapshot(
        openclawDir,
        freshContextAudit(),
        context?.agentId,
        sessionId,
        api,
        "tool-after",
        milestoneSnapshot
      );
    });

    // Generate dashboard silently on session end
    safeOn(api, "session_end", (...args: unknown[]) => {
      const context = args[1] as OpenClawHookContext | undefined;
      const sessionId = context?.sessionId ?? context?.sessionKey;

      try {
        if (openclawDir && sessionId) {
          maybeCheckpointFromRuntimeSnapshot(openclawDir, freshContextAudit(), context?.agentId, sessionId, api, "session-end");
        }
      } catch { /* checkpoint failure should not block dashboard */ }
      try {
        generateDashboard(30);
        api.logger.info("[token-optimizer] Dashboard regenerated on session end");
      } finally {
        // Always clean up session state, even if checkpoint or dashboard fails
        if (sessionId) {
          clearCheckpointState(sessionId);
          // Release the per-session continuity one-shot guard so the Set does
          // not grow unbounded over a long-running gateway.
          _continuityInjectedSessions.delete(sessionId);
          // Release fresh-session nudge state for this session.
          _freshNudgeFiredSessions.delete(sessionId);
          _freshNudgePriorScores.delete(sessionId);
          // Release the per-session compaction counter.
          const sessionKey = context?.sessionKey ?? sessionId;
          _compactionCounter.delete(sessionKey);
        }
        // Prune checkpoints older than the retention window here too: an
        // always-on gateway may never restart, so the gateway:startup cleanup
        // alone would let old checkpoints accumulate indefinitely.
        try { cleanupCheckpoints(7); } catch { /* best-effort */ }
      }
    });
  },
});

type RuntimeEventKind = "agent-turn-prepare" | "tool-before" | "tool-after" | "session-end";

interface RuntimeEventContext {
  sessionId: string;
  sessionFile: string;
  fillPct: number;
  qualityScore: number;
  /** The exact context-window (tokens) used to derive fillPct. Threaded into
   *  freshSessionSavingsEstimate so the token count is always consistent with %. */
  contextWindow: number;
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
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(agentsDir, { withFileTypes: true });
  } catch {
    return null;
  }
  for (const entry of entries) {
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

  // Cache-drift visibility: when the session has no input tokens (missing or
  // stale session data), scoreSessionQuality defaults fillScore to 100, which
  // masks real degradation. Emit a one-line debug log so the fallback is
  // visible without spamming INFO.
  if (run.tokens.input === 0) {
    _logger?.debug?.(
      `[token-optimizer] session ${sessionId} has 0 input tokens; quality score defaulting to 100-fill fallback (cache drift?)`
    );
  }

  const snapshot = buildRuntimeSnapshot(run, contextAudit);
  return {
    sessionId,
    sessionFile,
    fillPct: snapshot.fillPct,
    qualityScore: snapshot.qualityScore,
    contextWindow: snapshot.contextWindow,
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
    contextWindow: snapshot.contextWindow,
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

  let filepath: string | null = null;
  try {
    filepath = captureCheckpointV2(session, 10, {
      trigger: enrichedDecision.trigger,
      fillPct: enrichedDecision.fillPct,
      qualityScore: enrichedDecision.qualityScore,
      toolName: snapshot.toolName,
      eventKind: snapshot.eventKind,
      model: snapshot.model,
    });
  } catch { /* v2 threw, try v1 */ }
  if (!filepath) {
    try {
      filepath = captureCheckpoint(session, 20, {
        trigger: enrichedDecision.trigger,
        fillPct: enrichedDecision.fillPct,
        qualityScore: enrichedDecision.qualityScore,
        toolName: snapshot.toolName,
        eventKind: snapshot.eventKind,
        model: snapshot.model,
      });
    } catch { /* v1 also failed */ }
  }

  if (filepath) {
    api.logger.info(`[token-optimizer] Checkpoint saved (${enrichedDecision.trigger}): ${filepath}`);
  }
}
