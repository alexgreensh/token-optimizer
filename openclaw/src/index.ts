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
  storePendingContinuityHint,
  consumePendingContinuityHint,
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
// Bounded per-session tracking
// ---------------------------------------------------------------------------

/**
 * Upper bound on the number of sessions held in the in-memory continuity and
 * fresh-nudge collections. These are normally pruned on session:end, but a
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
 * _freshNudgePriorScores is re-set on every session:patch, so refreshing keeps
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

function safeOn(api: OpenClawApi, event: string, handler: (...args: unknown[]) => void): void {
  api.on(event, (...args: unknown[]) => {
    try {
      handler(...args);
    } catch (err) {
      api.logger.error(`[token-optimizer] ${event} handler error: ${err}`);
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
    // a topic-matched injection so we fire at most once per new session.
    // NOTE: session:start is "future/planned" in the OpenClaw plugin spec
    // (openclaw/docs/openclaw-plugin-spec.md line 242).  Until it lands we
    // trigger off the FIRST session:patch event that arrives with an
    // inject callback, guarded by this Set.  When session:start is added,
    // replace the session:patch guard with a session:start handler here.
    const _continuityInjectedSessions = new Set<string>();

    // Fresh-session nudge: per-session state.
    // _freshNudgeFiredSessions: once-per-session dedup; cleared on session:end.
    // _freshNudgePriorScores: last known quality score for each session; used to
    //   detect the "prior score established" condition that mirrors Python's
    //   _nudge_previous_score check (suppresses firing on the very first scored
    //   turn, i.e. right after a compaction/fresh session when there is no baseline).
    const _freshNudgeFiredSessions = new Set<string>();
    const _freshNudgePriorScores = new Map<string, number>();

    // Register service so other plugins/skills can call our methods
    api.registerService("token-optimizer", {
      audit,
      scan,
      generateDashboard,
      doctor,
      // v5 Active Compression surface
      listV5Features,
      isV5Enabled,
      setV5,
      getCompressionSummary,
    });

    // Log on gateway startup
    safeOn(api,"gateway:startup", () => {
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
    safeOn(api,"agent:bootstrap", (...args: unknown[]) => {
      const agentId =
        typeof args[0] === "object" && args[0] !== null
          ? (args[0] as Record<string, unknown>).agentId
          : undefined;
      api.logger.info(
        `[token-optimizer] Agent bootstrapped: ${agentId ?? "unknown"}`
      );
    });

    // Smart Compaction v2: capture before compaction (intelligent extraction)
    safeOn(api,"session:compact:before", (...args: unknown[]) => {
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
      clearCache("default", session.sessionId);
    });

    // Smart Compaction: restore after compaction
    safeOn(api,"session:compact:after", (...args: unknown[]) => {
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

        // T4 (U-B): credit the avoided reconstruction.
        // Floor = checkpoint content byte size / 3.3 (calibrated estimator).
        // Active = sum of tokensEst for files read >=2x this session (working set).
        // credited = min(200 000, max(floor, active)).
        try {
          const floorTokens = estimateTokensFromBytes(Buffer.byteLength(checkpoint, "utf-8"));
          const activeTokens = getActiveReadTokens("default", session.sessionId);
          const credited = Math.min(200_000, Math.max(floorTokens, activeTokens));
          if (credited > 0) {
            logSavingsEvent("checkpoint_restore", credited, session.sessionId, "restored from compact");
          }
        } catch { /* best-effort: never break inject */ }
      }

      // Fallback continuity injection: if a cross-session hint was matched on
      // first session:patch but couldn't be injected then (no inject callback),
      // consume and inject it now.  This fires at most once per session because
      // consumePendingContinuityHint() deletes the sidecar after reading.
      // TODO(continuity): remove this fallback once session:start exposes inject.
      if (session.inject) {
        const pendingHint = consumePendingContinuityHint(session.sessionId);
        if (pendingHint) {
          session.inject(pendingHint);
          api.logger.info(
            `[token-optimizer] Cross-session continuity hint injected via compact:after fallback ` +
            `for session ${session.sessionId}`
          );
        }
      }
    });

    safeOn(api,"session:patch", (...args: unknown[]) => {
      const event = args[0] as {
        sessionId?: string;
        agentId?: string;
        // inject() is not documented for session:patch in the spec today but
        // some gateway versions forward it.  We accept it opportunistically.
        inject?: (content: string) => void;
        // firstMessage carries the user's first prompt when the patch is the
        // "session initialized" patch (gateway-version-dependent).
        firstMessage?: string;
      } | undefined;
      if (!event?.sessionId || !openclawDir) return;

      // ── Cross-session continuity injection (new-session only) ──────────────
      // Trigger: first session:patch for this sessionId.
      // We chose session:patch because session:start is "future/planned" per
      // openclaw-plugin-spec.md line 242.  session:patch is the earliest
      // session-scoped event available.  The _continuityInjectedSessions guard
      // ensures we attempt injection at most once per session.
      if (!_continuityInjectedSessions.has(event.sessionId)) {
        try {
          // Resolve the first user prompt. Prefer the gateway-forwarded
          // firstMessage, but fall back to reading the session transcript on
          // disk so injection does NOT depend on an undocumented, gateway-
          // version-specific field. Without this, sessions whose first
          // session:patch carries no firstMessage never matched at all.
          let promptText = (event.firstMessage ?? "").trim();
          if (!promptText) {
            promptText = firstUserPromptFromSession(openclawDir, event.agentId, event.sessionId);
          }

          if (promptText) {
            // We finally have a prompt to score against — consume the one-shot
            // guard ONLY now. A bare init session:patch (no prompt yet) leaves
            // the guard unset so a later patch with the real prompt still runs.
            boundedSetAdd(_continuityInjectedSessions, event.sessionId);

            // ── Cold-resume-lean path (upgrade from lightweight hint) ────────
            // When the user signals resume intent ("continue our work / last
            // session"), inject a FULL lean reconstruction of the right same-
            // project prior checkpoint. Falls through to the lightweight hint
            // when intent not detected or no same-project match found.
            const resumeLeanBlock = tryBuildResumeLeanHint(
              promptText,
              event.sessionId,
              process.cwd(),
              logSavingsEvent
            );
            if (resumeLeanBlock) {
              // Cold-resume-lean matched: inject the full reconstruction block
              // and skip the lightweight hint (lean block is the full replacement).
              if (typeof event.inject === "function") {
                event.inject(resumeLeanBlock);
                api.logger.info(
                  `[token-optimizer] Cold-resume-lean injected for session ${event.sessionId}`
                );
              } else {
                storePendingContinuityHint(event.sessionId, resumeLeanBlock);
                api.logger.info(
                  `[token-optimizer] Cold-resume-lean matched for session ${event.sessionId}; queued for next inject point.`
                );
              }
            } else {
              // ── End cold-resume-lean path — fall through to lightweight hint ─
              // CHANGE 2 parity: when resume intent fired AND cwd is known but no
              // same-project match was found, do NOT fall through to the lightweight
              // cross-session hint (it is un-gated and could surface a DIFFERENT
              // project's checkpoint). Mirrors Python _continuity_prompt_hint:
              //   if cwd: return ""  # no cross-project fallthrough on explicit resume
              const _cwd = process.cwd();
              if (isResumeIntent(promptText) && _cwd) {
                api.logger.info(
                  `[token-optimizer] Resume intent detected but no same-project checkpoint for session ${event.sessionId}; suppressing cross-project fallthrough.`
                );
                // Skip the lightweight hint entirely — do NOT surface another project's context.
              } else {
                const candidate = findBestContinuityCheckpoint(promptText, event.sessionId, _cwd);
                if (candidate) {
                  const hint = buildContinuityHint(candidate);
                  // T5 (U-G) serve side: record which files this hint surfaced so a
                  // later Read of one can claim the avoided-search credit. Best-effort:
                  // never let this bookkeeping break the hint itself.
                  try {
                    const hintedPaths = extractHintedPaths(candidate.content);
                    if (hintedPaths.length > 0) {
                      recordHintServe(event.sessionId, hintedPaths);
                    }
                  } catch { /* best-effort */ }
                  if (typeof event.inject === "function") {
                    // Best case: gateway forwards an inject callback on session:patch.
                    event.inject(hint);
                    api.logger.info(
                      `[token-optimizer] Cross-session continuity injected for session ${event.sessionId} ` +
                      `(score=${candidate.score.toFixed(2)}, source=${candidate.entry.sessionDirName})`
                    );
                  } else {
                    // No inject path here. Persist the hint so the next
                    // session:compact:after — the spec-documented inject point —
                    // delivers it. Always store on a match so it lands at the
                    // earliest opportunity.
                    storePendingContinuityHint(event.sessionId, hint);
                    api.logger.info(
                      `[token-optimizer] Cross-session match for session ${event.sessionId} ` +
                      `(score=${candidate.score.toFixed(2)}, source=${candidate.entry.sessionDirName}); ` +
                      `queued for the next inject point.`
                    );
                  }
                } else {
                  api.logger.info(
                    `[token-optimizer] No matching prior checkpoint for session ${event.sessionId} ` +
                    `(threshold=${_CONTINUITY_THRESHOLD})`
                  );
                }
              }
            }
          } else {
            // Bare init patch before the first user message. Do NOT consume the
            // guard — a later session:patch (or the on-disk transcript) will
            // carry the prompt and we retry then.
            api.logger.info(
              `[token-optimizer] session:patch for new session ${event.sessionId}: ` +
              `no user prompt yet; will retry continuity on the next patch.`
            );
          }
        } catch (err) {
          api.logger.warn(`[token-optimizer] Cross-session continuity error: ${err}`);
        }
      }
      // ── End continuity injection ──────────────────────────────────────────

      // ── Fresh-session nudge ───────────────────────────────────────────────
      // Fires once per session when quality score < 70 AND context fill >= 50%.
      // Suppressed until a prior score is established (mirrors Python's
      // _nudge_previous_score guard against firing right after a compaction).
      // Delivery: inject() when available (session:patch), otherwise stored as
      // a pending hint for the next session:compact:after inject point.
      // Takes precedence over the /compact quality nudge (no double message).
      if (event.sessionId && !_freshNudgeFiredSessions.has(event.sessionId)) {
        try {
          const nudgeSnapshot = buildRuntimeEventContext(
            openclawDir, freshContextAudit(), event.agentId, event.sessionId, "session-patch"
          );
          if (nudgeSnapshot) {
            const hasPriorScore = _freshNudgePriorScores.has(event.sessionId);
            const nudgeMsg = buildFreshSessionNudgeMessage(
              nudgeSnapshot.qualityScore,
              nudgeSnapshot.fillPct,
              hasPriorScore,
              nudgeSnapshot.model,
              nudgeSnapshot.contextWindow  // thread the exact window fillPct was measured against
            );
            // Always update the prior-score baseline (whether or not the nudge fired).
            boundedMapSet(_freshNudgePriorScores, event.sessionId, nudgeSnapshot.qualityScore);

            if (nudgeMsg) {
              boundedSetAdd(_freshNudgeFiredSessions, event.sessionId);
              const { savedTokens } = freshSessionSavingsEstimate(nudgeSnapshot.fillPct, nudgeSnapshot.model, nudgeSnapshot.contextWindow);
              logSavingsEvent("fresh_session_nudge", savedTokens, event.sessionId,
                `score=${nudgeSnapshot.qualityScore} fill_pct=${nudgeSnapshot.fillPct.toFixed(1)}`);
              logCompressionEvent({
                feature: "fresh_session_nudge",
                sessionId: event.sessionId,
                detail: `score=${nudgeSnapshot.qualityScore} fill_pct=${nudgeSnapshot.fillPct.toFixed(1)} est_saved=${savedTokens}`,
                verified: false,
              });
              if (typeof event.inject === "function") {
                event.inject(nudgeMsg);
                api.logger.info(
                  `[token-optimizer] Fresh-session nudge injected for session ${event.sessionId} ` +
                  `(score=${nudgeSnapshot.qualityScore}, fill=${nudgeSnapshot.fillPct.toFixed(1)}%)`
                );
              } else {
                storePendingContinuityHint(event.sessionId, nudgeMsg);
                api.logger.info(
                  `[token-optimizer] Fresh-session nudge queued for session ${event.sessionId} ` +
                  `(score=${nudgeSnapshot.qualityScore}, fill=${nudgeSnapshot.fillPct.toFixed(1)}%); ` +
                  `will deliver at next inject point.`
                );
              }
            }
          }
        } catch (err) {
          api.logger.warn(`[token-optimizer] Fresh-session nudge error: ${err}`);
        }
      }
      // ── End fresh-session nudge ───────────────────────────────────────────

      maybeCheckpointFromRuntimeSnapshot(openclawDir, freshContextAudit(), event.agentId, event.sessionId, api, "session-patch");
    });

    // Read Cache: intercept redundant reads (PreToolUse equivalent)
    safeOn(api,"agent:tool:before", (...args: unknown[]) => {
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
              freshContextAudit(),
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
    safeOn(api,"agent:tool:after", (...args: unknown[]) => {
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
            freshContextAudit(),
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
        freshContextAudit(),
        event.agentId,
        event.sessionId,
        api,
        "tool-after",
        milestoneSnapshot
      );
    });

    // Generate dashboard silently on session end
    safeOn(api, "session:end", (...args: unknown[]) => {
      const event = args[0] as {
        sessionId?: string;
        agentId?: string;
      } | undefined;

      try {
        if (openclawDir && event?.sessionId) {
          maybeCheckpointFromRuntimeSnapshot(openclawDir, freshContextAudit(), event.agentId, event.sessionId, api, "session-end");
        }
      } catch { /* checkpoint failure should not block dashboard */ }
      try {
        generateDashboard(30);
        api.logger.info("[token-optimizer] Dashboard regenerated on session end");
      } finally {
        // Always clean up session state, even if checkpoint or dashboard fails
        if (event?.sessionId) {
          clearCheckpointState(event.sessionId);
          // Release the per-session continuity one-shot guard so the Set does
          // not grow unbounded over a long-running gateway.
          _continuityInjectedSessions.delete(event.sessionId);
          // Release fresh-session nudge state for this session.
          _freshNudgeFiredSessions.delete(event.sessionId);
          _freshNudgePriorScores.delete(event.sessionId);
        }
        // Prune checkpoints older than the retention window here too: an
        // always-on gateway may never restart, so the gateway:startup cleanup
        // alone would let old checkpoints accumulate indefinitely.
        try { cleanupCheckpoints(7); } catch { /* best-effort */ }
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
  /** The exact context-window (tokens) used to derive fillPct. Threaded into
   *  freshSessionSavingsEstimate so the token count is always consistent with %. */
  contextWindow: number;
  toolName?: string;
  eventKind: RuntimeEventKind;
  model: string;
}

/**
 * Read the first non-empty USER message from a session's on-disk transcript.
 * Used by continuity injection so the match no longer depends on the gateway
 * forwarding an (undocumented) firstMessage field on session:patch. Returns ""
 * when the transcript is missing or has no user text yet. Never throws.
 */
function firstUserPromptFromSession(
  openclawDir: string,
  agentId: string | undefined,
  sessionId: string
): string {
  try {
    const sessionFile = resolveSessionFile(openclawDir, agentId, sessionId);
    if (!sessionFile) return "";
    const messages = loadMessagesFromSessionFile(sessionFile);
    if (!messages) return "";
    for (const m of messages) {
      if (m.role === "user" && typeof m.content === "string" && m.content.trim()) {
        return m.content.trim().slice(0, 2000);
      }
    }
    return "";
  } catch {
    return "";
  }
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
