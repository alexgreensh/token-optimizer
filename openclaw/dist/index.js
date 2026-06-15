"use strict";
/**
 * Token Optimizer for OpenClaw - Plugin Entry Point
 *
 * Uses definePluginEntry() to register with the OpenClaw plugin system:
 * - api.registerService() for the token-optimizer service
 * - api.on() for lifecycle events
 * - api.logger for structured logging
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.detectUnusedSkills = exports.getSkillUsageHistory = exports.extractCostlyPrompts = exports.extractTopic = exports.parseSessionTurns = exports.pruneOldEvents = exports.getCompressionSummary = exports.logCompressionEvent = exports.listV5Features = exports.setV5 = exports.isV5Enabled = exports.V5_FEATURES = exports.generateCoachData = void 0;
exports.audit = audit;
exports.scan = scan;
exports.generateDashboard = generateDashboard;
exports.doctor = doctor;
exports.checkpointTelemetry = checkpointTelemetry;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const session_parser_1 = require("./session-parser");
Object.defineProperty(exports, "parseSessionTurns", { enumerable: true, get: function () { return session_parser_1.parseSessionTurns; } });
Object.defineProperty(exports, "extractCostlyPrompts", { enumerable: true, get: function () { return session_parser_1.extractCostlyPrompts; } });
const savings_1 = require("./savings");
const smart_compact_1 = require("./smart-compact");
const read_cache_1 = require("./read-cache");
const token_estimate_1 = require("./token-estimate");
const models_1 = require("./models");
const dashboard_1 = require("./dashboard");
const coach_1 = require("./coach");
var coach_2 = require("./coach");
Object.defineProperty(exports, "generateCoachData", { enumerable: true, get: function () { return coach_2.generateCoachData; } });
const pricing_1 = require("./pricing");
const context_audit_1 = require("./context-audit");
const quality_1 = require("./quality");
const checkpoint_policy_1 = require("./checkpoint-policy");
const waste_detectors_1 = require("./waste-detectors");
const continuity_1 = require("./continuity");
const v5_features_1 = require("./v5-features");
const telemetry_1 = require("./telemetry");
var v5_features_2 = require("./v5-features");
Object.defineProperty(exports, "V5_FEATURES", { enumerable: true, get: function () { return v5_features_2.V5_FEATURES; } });
Object.defineProperty(exports, "isV5Enabled", { enumerable: true, get: function () { return v5_features_2.isV5Enabled; } });
Object.defineProperty(exports, "setV5", { enumerable: true, get: function () { return v5_features_2.setV5; } });
Object.defineProperty(exports, "listV5Features", { enumerable: true, get: function () { return v5_features_2.listV5Features; } });
var telemetry_2 = require("./telemetry");
Object.defineProperty(exports, "logCompressionEvent", { enumerable: true, get: function () { return telemetry_2.logCompressionEvent; } });
Object.defineProperty(exports, "getCompressionSummary", { enumerable: true, get: function () { return telemetry_2.getCompressionSummary; } });
Object.defineProperty(exports, "pruneOldEvents", { enumerable: true, get: function () { return telemetry_2.pruneOldEvents; } });
function definePluginEntry(options) {
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
function boundedSetAdd(set, key) {
    set.add(key);
    while (set.size > MAX_TRACKED_SESSIONS) {
        const oldest = set.keys().next().value;
        if (oldest === undefined || oldest === key)
            break;
        set.delete(oldest);
    }
}
/**
 * set() on a Map with oldest-first eviction past MAX_TRACKED_SESSIONS. Unlike
 * the Set helper, this refreshes recency on update (delete + re-set) because
 * _freshNudgePriorScores is re-set on every session:patch, so refreshing keeps
 * an actively-scored session from being evicted while still in use.
 */
function boundedMapSet(map, key, value) {
    if (map.has(key))
        map.delete(key);
    map.set(key, value);
    while (map.size > MAX_TRACKED_SESSIONS) {
        const oldest = map.keys().next().value;
        if (oldest === undefined || oldest === key)
            break;
        map.delete(oldest);
    }
}
// ---------------------------------------------------------------------------
// Core audit logic (used by both plugin and CLI)
// ---------------------------------------------------------------------------
/**
 * Run a full audit: scan sessions, classify cron runs, detect waste.
 */
function audit(days = 30) {
    (0, pricing_1.resetPricingCache)();
    const openclawDir = (0, session_parser_1.findOpenClawDir)();
    if (!openclawDir) {
        return null;
    }
    const runs = (0, session_parser_1.scanAllSessions)(openclawDir, days);
    (0, session_parser_1.classifyCronRuns)(openclawDir, runs);
    // Load config for Tier 1 detectors
    const config = loadConfig(openclawDir);
    const findings = (0, waste_detectors_1.runAllDetectors)(runs, config);
    const totalCost = runs.reduce((sum, r) => sum + r.costUsd, 0);
    const totalTok = runs.reduce((sum, r) => sum + (0, models_1.totalTokens)(r.tokens), 0);
    const monthlySavings = findings.reduce((sum, f) => sum + f.monthlyWasteUsd, 0);
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
function scan(days = 30) {
    const openclawDir = (0, session_parser_1.findOpenClawDir)();
    if (!openclawDir)
        return null;
    const runs = (0, session_parser_1.scanAllSessions)(openclawDir, days);
    (0, session_parser_1.classifyCronRuns)(openclawDir, runs);
    return runs;
}
var session_parser_2 = require("./session-parser");
Object.defineProperty(exports, "extractTopic", { enumerable: true, get: function () { return session_parser_2.extractTopic; } });
/**
 * Load OpenClaw config for Tier 1 analysis.
 */
function loadConfig(openclawDir) {
    const configPath = path.join(openclawDir, "config.json");
    if (!fs.existsSync(configPath))
        return {};
    try {
        return JSON.parse(fs.readFileSync(configPath, "utf-8"));
    }
    catch {
        return {};
    }
}
/**
 * Generate the HTML dashboard, write to disk, return the file path.
 */
function generateDashboard(days = 30) {
    (0, pricing_1.resetPricingCache)();
    const openclawDir = (0, session_parser_1.findOpenClawDir)();
    if (!openclawDir)
        return null;
    const runs = (0, session_parser_1.scanAllSessions)(openclawDir, days);
    (0, session_parser_1.classifyCronRuns)(openclawDir, runs);
    const config = loadConfig(openclawDir);
    const findings = (0, waste_detectors_1.runAllDetectors)(runs, config);
    const totalCost = runs.reduce((sum, r) => sum + r.costUsd, 0);
    const totalTok = runs.reduce((sum, r) => sum + (0, models_1.totalTokens)(r.tokens), 0);
    const monthlySavings = findings.reduce((sum, f) => sum + f.monthlyWasteUsd, 0);
    const agents = Array.from(new Set(runs.map((r) => r.agentName)));
    const report = {
        scannedAt: new Date(),
        daysScanned: days,
        agentsFound: agents,
        totalSessions: runs.length,
        totalCostUsd: totalCost,
        totalTokens: totalTok,
        findings,
        monthlySavingsUsd: monthlySavings,
    };
    const contextAudit = (0, context_audit_1.auditContext)(openclawDir);
    const qualityReport = (0, quality_1.scoreQuality)(runs, contextAudit);
    // Build coach data
    const activeSkillNames = contextAudit.skills
        .filter((s) => !s.isArchived)
        .map((s) => s.name);
    const skillUsage = (0, context_audit_1.getSkillUsageHistory)(runs);
    const unusedSkillFindings = (0, waste_detectors_1.detectUnusedSkills)(activeSkillNames, skillUsage);
    const agentCosts = (0, dashboard_1.buildAgentCostBreakdown)(runs);
    // Collect costly prompts from the 10 most recent sessions
    const recentSessions = [...runs]
        .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime())
        .slice(0, 10);
    const allCostlyPrompts = [];
    for (const session of recentSessions) {
        const prompts = (0, session_parser_1.extractCostlyPrompts)(session.sourcePath, 3, openclawDir);
        allCostlyPrompts.push(...prompts);
    }
    allCostlyPrompts.sort((a, b) => b.costUsd - a.costUsd);
    const topCostlyPrompts = allCostlyPrompts.slice(0, 5);
    const coachData = (0, coach_1.generateCoachData)(contextAudit, runs, topCostlyPrompts, agentCosts, unusedSkillFindings);
    const savings = (0, savings_1.computeRealizedSavings)(openclawDir, days);
    const data = (0, dashboard_1.buildDashboardData)(runs, report, qualityReport, contextAudit, coachData, savings);
    return (0, dashboard_1.writeDashboard)(data);
}
function doctor() {
    const health = (0, checkpoint_policy_1.getCheckpointHealth)();
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
function checkpointTelemetry(days = 7) {
    return (0, checkpoint_policy_1.getCheckpointTelemetrySummary)(days);
}
// ---------------------------------------------------------------------------
// Never-used skill detection (public API)
// ---------------------------------------------------------------------------
/**
 * Returns a skill-name -> invocation-count map built from tool call history
 * across all provided sessions. Use alongside auditContext().skills to feed
 * detectUnusedSkills().
 */
var context_audit_2 = require("./context-audit");
Object.defineProperty(exports, "getSkillUsageHistory", { enumerable: true, get: function () { return context_audit_2.getSkillUsageHistory; } });
/**
 * Returns WasteFinding objects for installed skills that have zero invocations.
 * Pass auditContext().skills.active.map(s => s.name) as `installed`, and
 * getSkillUsageHistory(sessions) as `usageMap`.
 */
var waste_detectors_2 = require("./waste-detectors");
Object.defineProperty(exports, "detectUnusedSkills", { enumerable: true, get: function () { return waste_detectors_2.detectUnusedSkills; } });
// ---------------------------------------------------------------------------
// Safe event handler wrapper (prevents unhandled throws from crashing gateway)
// ---------------------------------------------------------------------------
function safeOn(api, event, handler) {
    api.on(event, (...args) => {
        try {
            handler(...args);
        }
        catch (err) {
            api.logger.error(`[token-optimizer] ${event} handler error: ${err}`);
        }
    });
}
// ---------------------------------------------------------------------------
// Plugin registration (called by OpenClaw plugin loader)
// ---------------------------------------------------------------------------
exports.default = definePluginEntry({
    id: "token-optimizer-openclaw",
    name: "Token Optimizer",
    description: "Token waste auditor for OpenClaw. Detects idle burns, model misrouting, and context bloat.",
    register(api) {
        api.logger.info("[token-optimizer] Plugin activated");
        const openclawDir = (0, session_parser_1.findOpenClawDir)();
        // Lazy-refresh context audit (TTL-based) so long-running gateways stay current
        let _ctxCache = null;
        let _ctxTs = 0;
        const CTX_TTL = 5 * 60 * 1000; // 5 minutes
        function freshContextAudit() {
            if (!openclawDir)
                return null;
            const now = Date.now();
            if (_ctxCache && (now - _ctxTs) < CTX_TTL)
                return _ctxCache;
            try {
                _ctxCache = (0, context_audit_1.auditContext)(openclawDir);
            }
            catch {
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
        const _continuityInjectedSessions = new Set();
        // Fresh-session nudge: per-session state.
        // _freshNudgeFiredSessions: once-per-session dedup; cleared on session:end.
        // _freshNudgePriorScores: last known quality score for each session; used to
        //   detect the "prior score established" condition that mirrors Python's
        //   _nudge_previous_score check (suppresses firing on the very first scored
        //   turn, i.e. right after a compaction/fresh session when there is no baseline).
        const _freshNudgeFiredSessions = new Set();
        const _freshNudgePriorScores = new Map();
        // Register service so other plugins/skills can call our methods
        api.registerService("token-optimizer", {
            audit,
            scan,
            generateDashboard,
            doctor,
            // v5 Active Compression surface
            listV5Features: v5_features_1.listV5Features,
            isV5Enabled: v5_features_1.isV5Enabled,
            setV5: v5_features_1.setV5,
            getCompressionSummary: telemetry_1.getCompressionSummary,
        });
        // Log on gateway startup
        safeOn(api, "gateway:startup", () => {
            api.logger.info("[token-optimizer] Gateway started, ready to audit");
            // Clean up old checkpoints on startup
            const cleaned = (0, smart_compact_1.cleanupCheckpoints)(7);
            if (cleaned > 0) {
                api.logger.info(`[token-optimizer] Cleaned ${cleaned} old checkpoint(s)`);
            }
            // v5 telemetry hygiene: drop events older than 90 days so the JSONL
            // file does not grow unbounded across long-running gateways.
            try {
                const dropped = (0, telemetry_1.pruneOldEvents)(90);
                if (dropped > 0) {
                    api.logger.info(`[token-optimizer] Pruned ${dropped} v5 telemetry event(s) older than 90d`);
                }
            }
            catch {
                // Never crash the gateway over telemetry cleanup.
            }
        });
        // Log on agent bootstrap
        safeOn(api, "agent:bootstrap", (...args) => {
            const agentId = typeof args[0] === "object" && args[0] !== null
                ? args[0].agentId
                : undefined;
            api.logger.info(`[token-optimizer] Agent bootstrapped: ${agentId ?? "unknown"}`);
        });
        // Smart Compaction v2: capture before compaction (intelligent extraction)
        safeOn(api, "session:compact:before", (...args) => {
            const session = args[0];
            if (!session?.sessionId) {
                api.logger.warn("[token-optimizer] compact:before fired without session data");
                return;
            }
            // Try v2 (intelligent extraction), fall back to v1
            let filepath = null;
            try {
                filepath = (0, smart_compact_1.captureCheckpointV2)(session, 10, {
                    trigger: "compact",
                    eventKind: "compact-before",
                });
            }
            catch { /* v2 threw, try v1 */ }
            if (!filepath) {
                try {
                    filepath = (0, smart_compact_1.captureCheckpoint)(session, 20, {
                        trigger: "compact",
                        eventKind: "compact-before",
                    });
                }
                catch { /* v1 also failed */ }
            }
            if (filepath) {
                api.logger.info(`[token-optimizer] Checkpoint saved: ${filepath}`);
            }
            // Clear read-cache on compaction (context is reset, cache entries are stale)
            (0, read_cache_1.clearCache)("default", session.sessionId);
        });
        // Smart Compaction: restore after compaction
        safeOn(api, "session:compact:after", (...args) => {
            const session = args[0];
            if (!session?.sessionId)
                return;
            const checkpoint = (0, smart_compact_1.restoreCheckpoint)(session.sessionId);
            if (checkpoint && session.inject) {
                session.inject(checkpoint);
                api.logger.info(`[token-optimizer] Checkpoint restored for session ${session.sessionId}`);
                // T4 (U-B): credit the avoided reconstruction.
                // Floor = checkpoint content byte size / 3.3 (calibrated estimator).
                // Active = sum of tokensEst for files read >=2x this session (working set).
                // credited = min(200 000, max(floor, active)).
                try {
                    const floorTokens = (0, token_estimate_1.estimateTokensFromBytes)(Buffer.byteLength(checkpoint, "utf-8"));
                    const activeTokens = (0, read_cache_1.getActiveReadTokens)("default", session.sessionId);
                    const credited = Math.min(200_000, Math.max(floorTokens, activeTokens));
                    if (credited > 0) {
                        (0, read_cache_1.logSavingsEvent)("checkpoint_restore", credited, session.sessionId, "restored from compact");
                    }
                }
                catch { /* best-effort: never break inject */ }
            }
            // Fallback continuity injection: if a cross-session hint was matched on
            // first session:patch but couldn't be injected then (no inject callback),
            // consume and inject it now.  This fires at most once per session because
            // consumePendingContinuityHint() deletes the sidecar after reading.
            // TODO(continuity): remove this fallback once session:start exposes inject.
            if (session.inject) {
                const pendingHint = (0, continuity_1.consumePendingContinuityHint)(session.sessionId);
                if (pendingHint) {
                    session.inject(pendingHint);
                    api.logger.info(`[token-optimizer] Cross-session continuity hint injected via compact:after fallback ` +
                        `for session ${session.sessionId}`);
                }
            }
        });
        safeOn(api, "session:patch", (...args) => {
            const event = args[0];
            if (!event?.sessionId || !openclawDir)
                return;
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
                        const resumeLeanBlock = (0, continuity_1.tryBuildResumeLeanHint)(promptText, event.sessionId, process.cwd(), read_cache_1.logSavingsEvent);
                        if (resumeLeanBlock) {
                            // Cold-resume-lean matched: inject the full reconstruction block
                            // and skip the lightweight hint (lean block is the full replacement).
                            if (typeof event.inject === "function") {
                                event.inject(resumeLeanBlock);
                                api.logger.info(`[token-optimizer] Cold-resume-lean injected for session ${event.sessionId}`);
                            }
                            else {
                                (0, continuity_1.storePendingContinuityHint)(event.sessionId, resumeLeanBlock);
                                api.logger.info(`[token-optimizer] Cold-resume-lean matched for session ${event.sessionId}; queued for next inject point.`);
                            }
                        }
                        else {
                            // ── End cold-resume-lean path — fall through to lightweight hint ─
                            // CHANGE 2 parity: when resume intent fired AND cwd is known but no
                            // same-project match was found, do NOT fall through to the lightweight
                            // cross-session hint (it is un-gated and could surface a DIFFERENT
                            // project's checkpoint). Mirrors Python _continuity_prompt_hint:
                            //   if cwd: return ""  # no cross-project fallthrough on explicit resume
                            const _cwd = process.cwd();
                            if ((0, continuity_1.isResumeIntent)(promptText) && _cwd) {
                                api.logger.info(`[token-optimizer] Resume intent detected but no same-project checkpoint for session ${event.sessionId}; suppressing cross-project fallthrough.`);
                                // Skip the lightweight hint entirely — do NOT surface another project's context.
                            }
                            else {
                                const candidate = (0, continuity_1.findBestContinuityCheckpoint)(promptText, event.sessionId, _cwd);
                                if (candidate) {
                                    const hint = (0, continuity_1.buildContinuityHint)(candidate);
                                    // T5 (U-G) serve side: record which files this hint surfaced so a
                                    // later Read of one can claim the avoided-search credit. Best-effort:
                                    // never let this bookkeeping break the hint itself.
                                    try {
                                        const hintedPaths = (0, continuity_1.extractHintedPaths)(candidate.content);
                                        if (hintedPaths.length > 0) {
                                            (0, read_cache_1.recordHintServe)(event.sessionId, hintedPaths);
                                        }
                                    }
                                    catch { /* best-effort */ }
                                    if (typeof event.inject === "function") {
                                        // Best case: gateway forwards an inject callback on session:patch.
                                        event.inject(hint);
                                        api.logger.info(`[token-optimizer] Cross-session continuity injected for session ${event.sessionId} ` +
                                            `(score=${candidate.score.toFixed(2)}, source=${candidate.entry.sessionDirName})`);
                                    }
                                    else {
                                        // No inject path here. Persist the hint so the next
                                        // session:compact:after — the spec-documented inject point —
                                        // delivers it. Always store on a match so it lands at the
                                        // earliest opportunity.
                                        (0, continuity_1.storePendingContinuityHint)(event.sessionId, hint);
                                        api.logger.info(`[token-optimizer] Cross-session match for session ${event.sessionId} ` +
                                            `(score=${candidate.score.toFixed(2)}, source=${candidate.entry.sessionDirName}); ` +
                                            `queued for the next inject point.`);
                                    }
                                }
                                else {
                                    api.logger.info(`[token-optimizer] No matching prior checkpoint for session ${event.sessionId} ` +
                                        `(threshold=${continuity_1.RELEVANCE_THRESHOLD})`);
                                }
                            }
                        }
                    }
                    else {
                        // Bare init patch before the first user message. Do NOT consume the
                        // guard — a later session:patch (or the on-disk transcript) will
                        // carry the prompt and we retry then.
                        api.logger.info(`[token-optimizer] session:patch for new session ${event.sessionId}: ` +
                            `no user prompt yet; will retry continuity on the next patch.`);
                    }
                }
                catch (err) {
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
                    const nudgeSnapshot = buildRuntimeEventContext(openclawDir, freshContextAudit(), event.agentId, event.sessionId, "session-patch");
                    if (nudgeSnapshot) {
                        const hasPriorScore = _freshNudgePriorScores.has(event.sessionId);
                        const nudgeMsg = (0, quality_1.buildFreshSessionNudgeMessage)(nudgeSnapshot.qualityScore, nudgeSnapshot.fillPct, hasPriorScore, nudgeSnapshot.model, nudgeSnapshot.contextWindow // thread the exact window fillPct was measured against
                        );
                        // Always update the prior-score baseline (whether or not the nudge fired).
                        boundedMapSet(_freshNudgePriorScores, event.sessionId, nudgeSnapshot.qualityScore);
                        if (nudgeMsg) {
                            boundedSetAdd(_freshNudgeFiredSessions, event.sessionId);
                            const { savedTokens } = (0, quality_1.freshSessionSavingsEstimate)(nudgeSnapshot.fillPct, nudgeSnapshot.model, nudgeSnapshot.contextWindow);
                            (0, read_cache_1.logSavingsEvent)("fresh_session_nudge", savedTokens, event.sessionId, `score=${nudgeSnapshot.qualityScore} fill_pct=${nudgeSnapshot.fillPct.toFixed(1)}`);
                            (0, telemetry_1.logCompressionEvent)({
                                feature: "fresh_session_nudge",
                                sessionId: event.sessionId,
                                detail: `score=${nudgeSnapshot.qualityScore} fill_pct=${nudgeSnapshot.fillPct.toFixed(1)} est_saved=${savedTokens}`,
                                verified: false,
                            });
                            if (typeof event.inject === "function") {
                                event.inject(nudgeMsg);
                                api.logger.info(`[token-optimizer] Fresh-session nudge injected for session ${event.sessionId} ` +
                                    `(score=${nudgeSnapshot.qualityScore}, fill=${nudgeSnapshot.fillPct.toFixed(1)}%)`);
                            }
                            else {
                                (0, continuity_1.storePendingContinuityHint)(event.sessionId, nudgeMsg);
                                api.logger.info(`[token-optimizer] Fresh-session nudge queued for session ${event.sessionId} ` +
                                    `(score=${nudgeSnapshot.qualityScore}, fill=${nudgeSnapshot.fillPct.toFixed(1)}%); ` +
                                    `will deliver at next inject point.`);
                            }
                        }
                    }
                }
                catch (err) {
                    api.logger.warn(`[token-optimizer] Fresh-session nudge error: ${err}`);
                }
            }
            // ── End fresh-session nudge ───────────────────────────────────────────
            maybeCheckpointFromRuntimeSnapshot(openclawDir, freshContextAudit(), event.agentId, event.sessionId, api, "session-patch");
        });
        // Read Cache: intercept redundant reads (PreToolUse equivalent)
        safeOn(api, "agent:tool:before", (...args) => {
            const event = args[0];
            if (!event?.toolName)
                return;
            if (event.toolName === "Read") {
                const result = (0, read_cache_1.handleReadBefore)({
                    toolName: event.toolName,
                    toolInput: (event.toolInput ?? {}),
                    agentId: event.agentId ?? "unknown",
                    sessionId: event.sessionId ?? "unknown",
                });
                if (result?.block && event.block) {
                    event.block(result.message);
                }
            }
            if (openclawDir &&
                event.sessionId &&
                (event.toolName === "Agent" || event.toolName === "Task")) {
                const decision = (0, checkpoint_policy_1.maybeDecidePreFanoutCheckpoint)(event.sessionId);
                const snapshot = decision
                    ? buildRuntimeEventContext(openclawDir, freshContextAudit(), event.agentId, event.sessionId, "tool-before", event.toolName)
                    : null;
                captureDecisionCheckpoint(decision, snapshot, api);
            }
        });
        // Read Cache: invalidate on file writes (PostToolUse equivalent)
        safeOn(api, "agent:tool:after", (...args) => {
            const event = args[0];
            if (!event?.toolName)
                return;
            (0, read_cache_1.handleWriteAfter)({
                toolName: event.toolName,
                toolInput: (event.toolInput ?? {}),
                agentId: event.agentId ?? "unknown",
                sessionId: event.sessionId ?? "unknown",
            });
            if (!openclawDir || !event.sessionId)
                return;
            const filePath = typeof event.toolInput?.file_path === "string"
                ? event.toolInput.file_path
                : undefined;
            let milestoneSnapshot = null;
            if (isWriteTool(event.toolName)) {
                (0, checkpoint_policy_1.registerWriteEvent)(event.sessionId, filePath);
                const decision = (0, checkpoint_policy_1.maybeDecideEditBatchCheckpoint)(event.sessionId);
                if (decision) {
                    milestoneSnapshot = buildRuntimeEventContext(openclawDir, freshContextAudit(), event.agentId, event.sessionId, "tool-after", event.toolName);
                }
                captureDecisionCheckpoint(decision, milestoneSnapshot, api);
            }
            maybeCheckpointFromRuntimeSnapshot(openclawDir, freshContextAudit(), event.agentId, event.sessionId, api, "tool-after", milestoneSnapshot);
        });
        // Generate dashboard silently on session end
        safeOn(api, "session:end", (...args) => {
            const event = args[0];
            try {
                if (openclawDir && event?.sessionId) {
                    maybeCheckpointFromRuntimeSnapshot(openclawDir, freshContextAudit(), event.agentId, event.sessionId, api, "session-end");
                }
            }
            catch { /* checkpoint failure should not block dashboard */ }
            try {
                generateDashboard(30);
                api.logger.info("[token-optimizer] Dashboard regenerated on session end");
            }
            finally {
                // Always clean up session state, even if checkpoint or dashboard fails
                if (event?.sessionId) {
                    (0, checkpoint_policy_1.clearCheckpointState)(event.sessionId);
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
                try {
                    (0, smart_compact_1.cleanupCheckpoints)(7);
                }
                catch { /* best-effort */ }
            }
        });
    },
});
/**
 * Read the first non-empty USER message from a session's on-disk transcript.
 * Used by continuity injection so the match no longer depends on the gateway
 * forwarding an (undocumented) firstMessage field on session:patch. Returns ""
 * when the transcript is missing or has no user text yet. Never throws.
 */
function firstUserPromptFromSession(openclawDir, agentId, sessionId) {
    try {
        const sessionFile = resolveSessionFile(openclawDir, agentId, sessionId);
        if (!sessionFile)
            return "";
        const messages = (0, smart_compact_1.loadMessagesFromSessionFile)(sessionFile);
        if (!messages)
            return "";
        for (const m of messages) {
            if (m.role === "user" && typeof m.content === "string" && m.content.trim()) {
                return m.content.trim().slice(0, 2000);
            }
        }
        return "";
    }
    catch {
        return "";
    }
}
function resolveSessionFile(openclawDir, agentId, sessionId) {
    if (agentId) {
        const direct = path.join(openclawDir, "agents", agentId, "sessions", `${sessionId}.jsonl`);
        if (fs.existsSync(direct))
            return direct;
    }
    const agentsDir = path.join(openclawDir, "agents");
    let entries;
    try {
        entries = fs.readdirSync(agentsDir, { withFileTypes: true });
    }
    catch {
        return null;
    }
    for (const entry of entries) {
        if (!entry.isDirectory())
            continue;
        const candidate = path.join(agentsDir, entry.name, "sessions", `${sessionId}.jsonl`);
        if (fs.existsSync(candidate))
            return candidate;
    }
    return null;
}
function isWriteTool(toolName) {
    return toolName === "Edit" || toolName === "Write" || toolName === "MultiEdit" || toolName === "NotebookEdit";
}
function buildRuntimeEventContext(openclawDir, contextAudit, agentId, sessionId, eventKind, toolName) {
    const sessionFile = resolveSessionFile(openclawDir, agentId, sessionId);
    if (!sessionFile)
        return null;
    const agentName = agentId ?? path.basename(path.dirname(path.dirname(sessionFile)));
    const run = (0, session_parser_1.parseSession)(sessionFile, agentName, openclawDir);
    if (!run)
        return null;
    const snapshot = (0, checkpoint_policy_1.buildRuntimeSnapshot)(run, contextAudit);
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
function maybeCheckpointFromRuntimeSnapshot(openclawDir, contextAudit, agentId, sessionId, api, eventKind, precomputedSnapshot = null) {
    if (!(0, checkpoint_policy_1.shouldEvaluateRuntimeState)(sessionId))
        return;
    const snapshot = precomputedSnapshot ??
        buildRuntimeEventContext(openclawDir, contextAudit, agentId, sessionId, eventKind);
    if (!snapshot)
        return;
    (0, checkpoint_policy_1.markEvaluated)(sessionId);
    const decision = (0, checkpoint_policy_1.maybeDecideSnapshotCheckpoint)(sessionId, {
        fillPct: snapshot.fillPct,
        qualityScore: snapshot.qualityScore,
        contextWindow: snapshot.contextWindow,
    });
    captureDecisionCheckpoint(decision, snapshot, api);
}
function captureDecisionCheckpoint(decision, snapshot, api) {
    if (!decision || !snapshot)
        return;
    const enrichedDecision = {
        ...decision,
        fillPct: decision.fillPct ?? snapshot.fillPct,
        qualityScore: decision.qualityScore ?? snapshot.qualityScore,
    };
    const session = {
        sessionId: snapshot.sessionId,
        messages: (0, smart_compact_1.loadMessagesFromSessionFile)(snapshot.sessionFile),
    };
    let filepath = null;
    try {
        filepath = (0, smart_compact_1.captureCheckpointV2)(session, 10, {
            trigger: enrichedDecision.trigger,
            fillPct: enrichedDecision.fillPct,
            qualityScore: enrichedDecision.qualityScore,
            toolName: snapshot.toolName,
            eventKind: snapshot.eventKind,
            model: snapshot.model,
        });
    }
    catch { /* v2 threw, try v1 */ }
    if (!filepath) {
        try {
            filepath = (0, smart_compact_1.captureCheckpoint)(session, 20, {
                trigger: enrichedDecision.trigger,
                fillPct: enrichedDecision.fillPct,
                qualityScore: enrichedDecision.qualityScore,
                toolName: snapshot.toolName,
                eventKind: snapshot.eventKind,
                model: snapshot.model,
            });
        }
        catch { /* v1 also failed */ }
    }
    if (filepath) {
        api.logger.info(`[token-optimizer] Checkpoint saved (${enrichedDecision.trigger}): ${filepath}`);
    }
}
//# sourceMappingURL=index.js.map