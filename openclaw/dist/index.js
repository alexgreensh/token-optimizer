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
exports.audit = audit;
exports.scan = scan;
exports.generateDashboard = generateDashboard;
exports.doctor = doctor;
exports.checkpointTelemetry = checkpointTelemetry;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const session_parser_1 = require("./session-parser");
const smart_compact_1 = require("./smart-compact");
const read_cache_1 = require("./read-cache");
const models_1 = require("./models");
const dashboard_1 = require("./dashboard");
const pricing_1 = require("./pricing");
const context_audit_1 = require("./context-audit");
const quality_1 = require("./quality");
const checkpoint_policy_1 = require("./checkpoint-policy");
const waste_detectors_1 = require("./waste-detectors");
function definePluginEntry(options) {
    return options;
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
    const data = (0, dashboard_1.buildDashboardData)(runs, report, qualityReport, contextAudit);
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
// Plugin registration (called by OpenClaw plugin loader)
// ---------------------------------------------------------------------------
exports.default = definePluginEntry({
    id: "token-optimizer",
    name: "Token Optimizer",
    description: "Token waste auditor for OpenClaw. Detects idle burns, model misrouting, and context bloat.",
    register(api) {
        api.logger.info("[token-optimizer] Plugin activated");
        const openclawDir = (0, session_parser_1.findOpenClawDir)();
        const contextAudit = openclawDir ? (0, context_audit_1.auditContext)(openclawDir) : null;
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
            const cleaned = (0, smart_compact_1.cleanupCheckpoints)(7);
            if (cleaned > 0) {
                api.logger.info(`[token-optimizer] Cleaned ${cleaned} old checkpoint(s)`);
            }
        });
        // Log on agent bootstrap
        api.on("agent:bootstrap", (...args) => {
            const agentId = typeof args[0] === "object" && args[0] !== null
                ? args[0].agentId
                : undefined;
            api.logger.info(`[token-optimizer] Agent bootstrapped: ${agentId ?? "unknown"}`);
        });
        // Smart Compaction v2: capture before compaction (intelligent extraction)
        api.on("session:compact:before", (...args) => {
            const session = args[0];
            if (!session?.sessionId) {
                api.logger.warn("[token-optimizer] compact:before fired without session data");
                return;
            }
            // Try v2 (intelligent extraction), fall back to v1
            const filepath = (0, smart_compact_1.captureCheckpointV2)(session, 10, {
                trigger: "compact",
                eventKind: "compact-before",
            }) ?? (0, smart_compact_1.captureCheckpoint)(session, 20, {
                trigger: "compact",
                eventKind: "compact-before",
            });
            if (filepath) {
                api.logger.info(`[token-optimizer] Checkpoint saved: ${filepath}`);
            }
            // Clear read-cache on compaction (context is reset, cache entries are stale)
            (0, read_cache_1.clearCache)("default", session.sessionId);
        });
        // Smart Compaction: restore after compaction
        api.on("session:compact:after", (...args) => {
            const session = args[0];
            if (!session?.sessionId)
                return;
            const checkpoint = (0, smart_compact_1.restoreCheckpoint)(session.sessionId);
            if (checkpoint && session.inject) {
                session.inject(checkpoint);
                api.logger.info(`[token-optimizer] Checkpoint restored for session ${session.sessionId}`);
            }
        });
        api.on("session:patch", (...args) => {
            const event = args[0];
            if (!event?.sessionId || !openclawDir)
                return;
            maybeCheckpointFromRuntimeSnapshot(openclawDir, contextAudit, event.agentId, event.sessionId, api, "session-patch");
        });
        // Read Cache: intercept redundant reads (PreToolUse equivalent)
        api.on("agent:tool:before", (...args) => {
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
                    ? buildRuntimeEventContext(openclawDir, contextAudit, event.agentId, event.sessionId, "tool-before", event.toolName)
                    : null;
                captureDecisionCheckpoint(decision, snapshot, api);
            }
        });
        // Read Cache: invalidate on file writes (PostToolUse equivalent)
        api.on("agent:tool:after", (...args) => {
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
                    milestoneSnapshot = buildRuntimeEventContext(openclawDir, contextAudit, event.agentId, event.sessionId, "tool-after", event.toolName);
                }
                captureDecisionCheckpoint(decision, milestoneSnapshot, api);
            }
            maybeCheckpointFromRuntimeSnapshot(openclawDir, contextAudit, event.agentId, event.sessionId, api, "tool-after", milestoneSnapshot);
        });
        // Generate dashboard silently on session end
        api.on("session:end", (...args) => {
            try {
                const event = args[0];
                if (openclawDir && event?.sessionId) {
                    maybeCheckpointFromRuntimeSnapshot(openclawDir, contextAudit, event.agentId, event.sessionId, api, "session-end");
                    (0, checkpoint_policy_1.clearCheckpointState)(event.sessionId);
                }
                generateDashboard(30);
                api.logger.info("[token-optimizer] Dashboard regenerated on session end");
            }
            catch {
                // Silent failure, dashboard generation is non-critical
            }
        });
    },
});
function resolveSessionFile(openclawDir, agentId, sessionId) {
    if (agentId) {
        const direct = path.join(openclawDir, "agents", agentId, "sessions", `${sessionId}.jsonl`);
        if (fs.existsSync(direct))
            return direct;
    }
    const agentsDir = path.join(openclawDir, "agents");
    if (!fs.existsSync(agentsDir))
        return null;
    for (const entry of fs.readdirSync(agentsDir, { withFileTypes: true })) {
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
    const filepath = (0, smart_compact_1.captureCheckpointV2)(session, 10, {
        trigger: enrichedDecision.trigger,
        fillPct: enrichedDecision.fillPct,
        qualityScore: enrichedDecision.qualityScore,
        toolName: snapshot.toolName,
        eventKind: snapshot.eventKind,
        model: snapshot.model,
    }) ??
        (0, smart_compact_1.captureCheckpoint)(session, 20, {
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
//# sourceMappingURL=index.js.map