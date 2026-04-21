"use strict";
/**
 * Waste pattern detectors for OpenClaw agent sessions.
 *
 * Ported from fleet.py's detector classes. Each detector analyzes
 * AgentRun data and returns WasteFinding objects with confidence,
 * severity, monthly $ waste, and actionable fix snippets.
 *
 * Detectors implemented:
 * 1. HeartbeatModelWaste - expensive model for cron/heartbeat tasks
 * 2. HeartbeatOverFrequency - interval < 5 min across 3+ runs
 * 3. EmptyRuns - high input, near-zero output
 * 4. StaleCronConfig - dead paths in cron/hook commands
 * 5. SessionHistoryBloat - context growing without compaction
 * 6. LoopDetection - many messages with near-zero output
 * 7. AbandonedSessions - 1-2 messages then stopped
 * 8. GhostTokenQJL - QJL-inspired sketch clustering for ghost run detection
 * 9. ToolLoadingOverhead - sessions loading many tools without compact view (v2026.3.24+)
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
exports.ALL_DETECTORS = void 0;
exports.detectUnusedSkills = detectUnusedSkills;
exports.runAllDetectors = runAllDetectors;
const fs = __importStar(require("fs"));
const models_1 = require("./models");
const pricing_1 = require("./pricing");
const jl_sketcher_1 = require("./jl-sketcher");
/** Compute the span in days between first and last run. Min 1 day. */
function spanDays(runs) {
    if (runs.length < 2)
        return 1;
    const sorted = runs.map((r) => r.timestamp.getTime()).sort((a, b) => a - b);
    return Math.max(1, (sorted[sorted.length - 1] - sorted[0]) / 86_400_000);
}
// ---------------------------------------------------------------------------
// Tier 1: Config + heartbeat pattern analysis
// ---------------------------------------------------------------------------
/**
 * Detect expensive models (opus/sonnet) used for heartbeat/cron runs.
 * These should almost always be on haiku.
 */
function detectHeartbeatModelWaste(runs, _config) {
    const heartbeats = runs.filter((r) => r.runType === "heartbeat" || r.runType === "cron");
    if (heartbeats.length === 0)
        return [];
    const expensive = heartbeats.filter((r) => models_1.EXPENSIVE_MODELS.has(r.model));
    if (expensive.length === 0)
        return [];
    const totalCost = expensive.reduce((sum, r) => sum + r.costUsd, 0);
    const daysSpanned = spanDays(expensive);
    const monthlyCost = (totalCost / daysSpanned) * 30;
    // Calculate savings if switched to haiku
    let haikuCost = 0;
    for (const r of expensive) {
        haikuCost += (0, pricing_1.calculateCost)(r.tokens, "haiku");
    }
    const haikuMonthly = (haikuCost / daysSpanned) * 30;
    const savings = monthlyCost - haikuMonthly;
    if (savings < 0.1)
        return [];
    const modelsUsed = Array.from(new Set(expensive.map((r) => r.model)));
    return [
        {
            system: "openclaw",
            agentName: expensive[0].agentName,
            wasteType: "heartbeat_model_waste",
            tier: 1,
            severity: savings > 5.0 ? "high" : "medium",
            confidence: 0.9,
            description: `${expensive.length} heartbeat/cron runs using ${modelsUsed.join("/")} instead of Haiku`,
            monthlyWasteUsd: savings,
            monthlyWasteTokens: expensive.reduce((sum, r) => sum + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: `Route heartbeat/cron tasks to Haiku. Saves ~$${savings.toFixed(2)}/month.`,
            fixSnippet: '# In your agent config (config.json or cron/*.json):\n"model": "haiku"  # was: opus/sonnet',
            evidence: {
                expensiveCount: expensive.length,
                modelsUsed,
            },
        },
    ];
}
/**
 * Detect heartbeat intervals shorter than 5 minutes.
 */
function detectHeartbeatOverFrequency(runs, _config) {
    const heartbeats = runs
        .filter((r) => r.runType === "heartbeat" || r.runType === "cron")
        .sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());
    if (heartbeats.length < 3)
        return [];
    const shortIntervals = [];
    for (let i = 1; i < heartbeats.length; i++) {
        const gap = (heartbeats[i].timestamp.getTime() -
            heartbeats[i - 1].timestamp.getTime()) /
            1000;
        if (gap > 0 && gap < 300) {
            shortIntervals.push(gap);
        }
    }
    if (shortIntervals.length < 3)
        return [];
    const avgInterval = shortIntervals.reduce((a, b) => a + b, 0) / shortIntervals.length;
    const avgCostPerHb = heartbeats.reduce((sum, r) => sum + r.costUsd, 0) / heartbeats.length;
    const runsPerHourActual = 3600 / avgInterval;
    const runsPerHourOptimal = 12; // 5-min intervals
    const extraPerHour = Math.max(0, runsPerHourActual - runsPerHourOptimal);
    const monthlyExtra = extraPerHour * 16 * 30; // 16 active hours/day
    const monthlyWaste = monthlyExtra * avgCostPerHb;
    if (monthlyWaste < 0.1)
        return [];
    return [
        {
            system: "openclaw",
            agentName: heartbeats[0].agentName,
            wasteType: "heartbeat_over_frequency",
            tier: 1,
            severity: monthlyWaste < 2.0 ? "medium" : "high",
            confidence: 0.7,
            description: `Heartbeats averaging ${avgInterval.toFixed(0)}s interval (${shortIntervals.length} intervals < 5 min)`,
            monthlyWasteUsd: monthlyWaste,
            monthlyWasteTokens: 0,
            recommendation: `Increase heartbeat interval to 5+ minutes. Current average: ${avgInterval.toFixed(0)}s.`,
            fixSnippet: '# In your cron config:\n"interval": 300  # 5 minutes (was: shorter)',
            evidence: {
                avgIntervalSeconds: avgInterval,
                shortCount: shortIntervals.length,
            },
        },
    ];
}
/**
 * Detect stale cron configurations referencing dead paths.
 */
function detectStaleCronConfig(_runs, config) {
    const hooks = config.hooks;
    if (!hooks)
        return [];
    const findings = [];
    for (const [hookName, hookList] of Object.entries(hooks)) {
        if (!Array.isArray(hookList))
            continue;
        for (const hook of hookList) {
            if (typeof hook !== "object" || hook === null)
                continue;
            const cmd = hook.command;
            if (!cmd)
                continue;
            const parts = cmd.split(/\s+/);
            for (const part of parts) {
                if (part.startsWith("/") &&
                    !part.startsWith("/usr") &&
                    !part.startsWith("/bin") &&
                    !part.startsWith("$")) {
                    if (!fs.existsSync(part)) {
                        findings.push({
                            system: "openclaw",
                            agentName: "",
                            wasteType: "stale_cron",
                            tier: 1,
                            severity: "low",
                            confidence: 0.5,
                            description: `Hook '${hookName}' references non-existent path: ${part}`,
                            monthlyWasteUsd: 0,
                            monthlyWasteTokens: 0,
                            recommendation: "Remove or fix the hook referencing a dead path.",
                            fixSnippet: `# Fix or remove this hook entry:\n# ${hookName}: ${cmd}`,
                            evidence: {
                                hook: hookName,
                                command: cmd,
                                missingPath: part,
                            },
                        });
                    }
                }
            }
        }
    }
    return findings;
}
// ---------------------------------------------------------------------------
// Tier 2: Session log analysis
// ---------------------------------------------------------------------------
/**
 * Detect runs with high input but near-zero output (the #1 waste pattern).
 * Applies to ALL run types, not just heartbeat/cron.
 */
function detectEmptyRuns(runs, _config) {
    const emptyRuns = runs.filter((r) => (0, models_1.totalTokens)(r.tokens) > 5000 &&
        r.tokens.output < 100 &&
        r.messageCount <= 4);
    if (emptyRuns.length === 0)
        return [];
    // Require substantial context or explicit empty outcome to confirm
    const confirmed = emptyRuns.filter((r) => (0, models_1.totalTokens)(r.tokens) > 50_000 || r.outcome === "empty");
    if (confirmed.length < 2)
        return [];
    const totalWasteCost = confirmed.reduce((sum, r) => sum + r.costUsd, 0);
    const days = spanDays(confirmed);
    const monthlyCost = (totalWasteCost / days) * 30;
    const monthlyTokens = confirmed.reduce((sum, r) => sum + (0, models_1.totalTokens)(r.tokens), 0);
    let severity = "medium";
    if (monthlyCost > 10)
        severity = "critical";
    else if (monthlyCost > 2)
        severity = "high";
    return [
        {
            system: "openclaw",
            agentName: confirmed[0].agentName,
            wasteType: "empty_runs",
            tier: 2,
            severity,
            confidence: 0.85,
            description: `${confirmed.length} empty runs: high context load, near-zero useful output`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: monthlyTokens,
            recommendation: "Add guard conditions to skip runs when nothing to do. Route idle checks to Haiku.",
            fixSnippet: '# Add early-exit check in heartbeat script:\nif ! has_pending_work; then exit 0; fi',
            evidence: {
                emptyCount: confirmed.length,
                avgInput: Math.round(confirmed.reduce((sum, r) => sum + r.tokens.input, 0) /
                    confirmed.length),
                avgOutput: Math.round(confirmed.reduce((sum, r) => sum + r.tokens.output, 0) /
                    confirmed.length),
            },
        },
    ];
}
/**
 * Detect sessions with growing context but no compaction.
 */
function detectSessionHistoryBloat(runs, _config) {
    const longSessions = runs.filter((r) => r.messageCount > 30 && (0, models_1.totalTokens)(r.tokens) > 500_000);
    if (longSessions.length === 0)
        return [];
    const totalBloatTokens = longSessions.reduce((sum, r) => sum + r.tokens.input, 0);
    const savingsTokens = Math.round(totalBloatTokens * 0.4);
    const days = Math.max(1, new Set(longSessions.map((r) => r.timestamp.toISOString().slice(0, 10))).size);
    return [
        {
            system: "openclaw",
            agentName: "",
            wasteType: "session_history_bloat",
            tier: 2,
            severity: "medium",
            confidence: 0.6,
            description: `${longSessions.length} long sessions without apparent compaction (30+ messages, 500K+ tokens)`,
            monthlyWasteUsd: 0,
            monthlyWasteTokens: Math.round((savingsTokens / days) * 30),
            recommendation: "Use compaction at 50-70% context fill. On v2026.3.11+, context pruning is improved natively. Smart Compaction protects session state automatically.",
            fixSnippet: "# On OpenClaw v2026.3.11+, context pruning is improved.\n# Token Optimizer's Smart Compaction hooks add session state preservation.\n# Install: openclaw plugins install token-optimizer",
            evidence: {
                longSessionCount: longSessions.length,
                totalInputTokens: totalBloatTokens,
            },
        },
    ];
}
/**
 * Detect sessions with many messages but trivially small output (stuck loops).
 */
function detectLoops(runs, _config) {
    const suspects = runs.filter((r) => r.messageCount > 20 &&
        r.tokens.output < r.messageCount * 2 &&
        (0, models_1.totalTokens)(r.tokens) > 100_000 &&
        r.outcome !== "empty" &&
        r.outcome !== "abandoned" &&
        r.runType === "manual");
    if (suspects.length < 2)
        return [];
    const totalWaste = suspects.reduce((sum, r) => sum + r.costUsd, 0);
    const days = spanDays(suspects);
    const monthlyCost = (totalWaste / days) * 30;
    if (monthlyCost < 1.0)
        return [];
    return [
        {
            system: "openclaw",
            agentName: "",
            wasteType: "loop_detection",
            tier: 2,
            severity: monthlyCost < 10 ? "medium" : "high",
            confidence: 0.6,
            description: `${suspects.length} sessions with 20+ messages but near-zero output (potential stuck loops)`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: suspects.reduce((sum, r) => sum + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: "Check these sessions for retry storms or stuck tool calls. Consider timeout/loop-break logic.",
            fixSnippet: "# Add loop detection to your agent:\n# Monitor output-to-input ratio, break if < 0.01 for 5+ turns",
            evidence: {
                suspectCount: suspects.length,
                avgMessages: Math.round(suspects.reduce((sum, r) => sum + r.messageCount, 0) /
                    suspects.length),
                avgOutput: Math.round(suspects.reduce((sum, r) => sum + r.tokens.output, 0) /
                    suspects.length),
            },
        },
    ];
}
/**
 * Detect sessions with 1-2 messages then stopped (wasted startup cost).
 */
function detectAbandonedSessions(runs, _config) {
    const abandoned = runs.filter((r) => r.messageCount <= 2 &&
        (0, models_1.totalTokens)(r.tokens) > 10_000 &&
        r.runType === "manual");
    if (abandoned.length < 3)
        return [];
    const totalWaste = abandoned.reduce((sum, r) => sum + r.costUsd, 0);
    const days = spanDays(abandoned);
    const monthlyCost = (totalWaste / days) * 30;
    if (monthlyCost < 0.2)
        return [];
    return [
        {
            system: "openclaw",
            agentName: "",
            wasteType: "abandoned_sessions",
            tier: 2,
            severity: "low",
            confidence: 0.7,
            description: `${abandoned.length} abandoned sessions (1-2 messages, loaded full context then stopped)`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: abandoned.reduce((sum, r) => sum + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: "Quick checks are normal, but frequent abandons suggest startup overhead is too high.",
            fixSnippet: "# Reduce startup overhead:\n# Run /token-optimizer to identify and trim injected context",
            evidence: {
                abandonedCount: abandoned.length,
                avgInputTokens: Math.round(abandoned.reduce((sum, r) => sum + r.tokens.input, 0) /
                    abandoned.length),
            },
        },
    ];
}
// ---------------------------------------------------------------------------
// Tier 2: Ghost token detection (dual strategy: simple grouping or sketch)
// ---------------------------------------------------------------------------
/**
 * Detect "ghost" runs — sessions that load context but produce negligible output.
 *
 * Two strategies are available, controlled by config.ghostDetectorStrategy:
 *
 *   "simple" (default) — Groups runs by (agentName, model, runType) using a Map.
 *     Deterministic, O(n), easy to debug. Best when metadata fields are sufficient
 *     to identify duplicate patterns.
 *
 *   "sketch" — Uses QJL-inspired 1-bit sketch clustering (Hamming similarity).
 *     Catches fuzzy near-duplicates that differ slightly in token counts or tools.
 *     O(n²) pairwise comparison. Better if you later want to sketch actual message
 *     content for deeper similarity detection.
 *
 * Set config.ghostDetectorStrategy to toggle between them.
 */
function detectGhostTokenQJL(runs, config) {
    if (runs.length < 3)
        return [];
    const strategy = config.ghostDetectorStrategy ?? "simple";
    const clusters = strategy === "sketch"
        ? clusterRunsBySketch(runs)
        : clusterRunsByGroup(runs);
    // Within each cluster, find ghost runs (output < 100 tokens)
    let ghostRuns = [];
    for (const cluster of clusters) {
        if (cluster.length < 2)
            continue;
        const ghosts = cluster.filter((r) => r.tokens.output < 100);
        // Only flag if ghosts are a meaningful portion of the cluster
        if (ghosts.length >= 2) {
            ghostRuns.push(...ghosts);
        }
    }
    if (ghostRuns.length < 2)
        return [];
    // De-duplicate (a run could appear in multiple clusters with sketch strategy)
    const seen = new Set();
    ghostRuns = ghostRuns.filter((r) => {
        const key = `${r.sessionId}-${r.timestamp.getTime()}`;
        if (seen.has(key))
            return false;
        seen.add(key);
        return true;
    });
    if (ghostRuns.length < 2)
        return [];
    const totalWasteCost = ghostRuns.reduce((sum, r) => sum + r.costUsd, 0);
    const totalWasteTokens = ghostRuns.reduce((sum, r) => sum + r.tokens.input, 0);
    const days = spanDays(ghostRuns);
    const monthlyCost = (totalWasteCost / days) * 30;
    let severity = "medium";
    if (monthlyCost > 10)
        severity = "critical";
    else if (monthlyCost > 5)
        severity = "high";
    return [
        {
            system: "openclaw",
            agentName: ghostRuns[0].agentName,
            wasteType: "ghost_token_qjl",
            tier: 2,
            severity,
            confidence: 0.9,
            description: `${ghostRuns.length} ghost runs detected via ${strategy} strategy: near-duplicate context loaded with <100 token output`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: Math.round((totalWasteTokens / days) * 30),
            recommendation: "These runs load similar context repeatedly without producing output. " +
                "Add idempotency guards or cache results from prior identical runs.",
            fixSnippet: "# Add early-exit when context matches a recent successful run:\n" +
                "# if sketch_matches_recent_run(context): return cached_result",
            evidence: {
                strategy,
                ghostRunCount: ghostRuns.length,
                totalInputTokensWasted: totalWasteTokens,
                avgInputPerGhost: Math.round(totalWasteTokens / ghostRuns.length),
            },
        },
    ];
}
/** Simple O(n) grouping by (agentName, model, runType). */
function clusterRunsByGroup(runs) {
    const groups = new Map();
    for (const r of runs) {
        const key = `${r.agentName}|${r.model}|${r.runType}`;
        if (!groups.has(key))
            groups.set(key, []);
        groups.get(key).push(r);
    }
    return Array.from(groups.values());
}
/** Sketch-based O(n²) clustering using QJL 1-bit similarity. Falls back to simple grouping above 1000 runs. */
function clusterRunsBySketch(runs) {
    if (runs.length > 1000)
        return clusterRunsByGroup(runs);
    const items = runs.map((r, idx) => ({
        id: String(idx),
        text: [
            r.model,
            r.runType,
            r.agentName,
            `input:${Math.round(r.tokens.input / 1000)}k`,
            `msgs:${r.messageCount}`,
            ...(r.toolsUsed.length > 0 ? r.toolsUsed.slice(0, 5) : ["no-tools"]),
        ].join(" "),
    }));
    const clusters = (0, jl_sketcher_1.clusterBySketch)(items, 0.95);
    return clusters.map((ids) => ids
        .map((id) => runs[parseInt(id, 10)])
        .filter((r) => r !== undefined));
}
// ---------------------------------------------------------------------------
// Tier 3: Version-aware optimization opportunities
// ---------------------------------------------------------------------------
/**
 * Detect sessions with high tool counts that could benefit from compact tool view.
 * OpenClaw v2026.3.24 added `/tools` with compact/detailed views, reducing tool
 * loading overhead similar to Claude Code's Tool Search.
 */
function detectToolLoadingOverhead(runs, _config) {
    // Sessions with many tools loaded suggest overhead from full tool definitions
    const heavyToolSessions = runs.filter((r) => r.toolsUsed.length > 15 && (0, models_1.totalTokens)(r.tokens) > 200_000);
    if (heavyToolSessions.length < 3)
        return [];
    const avgTools = heavyToolSessions.reduce((sum, r) => sum + r.toolsUsed.length, 0) /
        heavyToolSessions.length;
    // Estimate: each full tool definition ~300-500 tokens, compact ~15 tokens
    const overheadPerSession = Math.round(avgTools * 400);
    const days = spanDays(heavyToolSessions);
    return [
        {
            system: "openclaw",
            agentName: "",
            wasteType: "tool_loading_overhead",
            tier: 3,
            severity: "medium",
            confidence: 0.5,
            description: `${heavyToolSessions.length} sessions loading ${Math.round(avgTools)} tools avg. On v2026.3.24+, use /tools compact view to reduce context overhead.`,
            monthlyWasteUsd: 0,
            monthlyWasteTokens: Math.round((overheadPerSession * heavyToolSessions.length) / days * 30),
            recommendation: "Upgrade to OpenClaw v2026.3.24+ and use `/tools` compact view. Disable unused tool providers to reduce loading overhead.",
            fixSnippet: "# List tools in compact mode (v2026.3.24+):\nopenclaw tools --compact\n\n# Disable unused providers:\nopenclaw config set providers.unused_provider.enabled false",
            evidence: {
                heavySessionCount: heavyToolSessions.length,
                avgToolsPerSession: Math.round(avgTools),
                estimatedOverheadPerSession: overheadPerSession,
            },
        },
    ];
}
// ---------------------------------------------------------------------------
// Tier 2: Behavioral waste detectors (ported from Python Sprint 2)
// ---------------------------------------------------------------------------
/** Detect sessions with many errors suggesting retry churn. */
function detectRetryChurn(runs, _config) {
    const churning = runs.filter((r) => r.outcome === "failure" && r.messageCount > 10 && (0, models_1.totalTokens)(r.tokens) > 50_000);
    if (churning.length < 2)
        return [];
    const totalWaste = churning.reduce((s, r) => s + r.costUsd, 0);
    const days = spanDays(churning);
    const monthlyCost = (totalWaste / days) * 30;
    if (monthlyCost < 0.5)
        return [];
    return [{
            system: "openclaw",
            agentName: "",
            wasteType: "retry_churn",
            tier: 2,
            severity: monthlyCost > 5 ? "high" : "medium",
            confidence: 0.7,
            description: `${churning.length} sessions with 10+ messages ending in failure (retry storms)`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: churning.reduce((s, r) => s + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: "Stop and diagnose after 2 failures instead of retrying. Add early-exit on repeated errors.",
            fixSnippet: "# Add retry limit to agent config:\n\"maxRetries\": 3",
            evidence: { churningCount: churning.length },
        }];
}
/** Detect sessions where tool errors cascade. */
function detectToolCascade(runs, _config) {
    const errorSessions = runs.filter((r) => r.outcome === "failure" && r.toolsUsed.length > 5 && (0, models_1.totalTokens)(r.tokens) > 100_000);
    if (errorSessions.length < 2)
        return [];
    const totalWaste = errorSessions.reduce((s, r) => s + r.costUsd, 0);
    const days = spanDays(errorSessions);
    const monthlyCost = (totalWaste / days) * 30;
    if (monthlyCost < 0.5)
        return [];
    return [{
            system: "openclaw",
            agentName: "",
            wasteType: "tool_cascade",
            tier: 2,
            severity: monthlyCost > 5 ? "high" : "medium",
            confidence: 0.6,
            description: `${errorSessions.length} sessions with 5+ tool calls ending in failure (error cascades)`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: errorSessions.reduce((s, r) => s + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: "Break error chains early: diagnose the root cause after 2 tool failures.",
            fixSnippet: "# Add error-break logic:\n\"breakOnConsecutiveErrors\": 3",
            evidence: { errorSessionCount: errorSessions.length },
        }];
}
/** Detect overpowered model usage: expensive model for simple tasks. */
function detectOverpoweredModel(runs, _config) {
    const simpleOnExpensive = runs.filter((r) => {
        if (!models_1.EXPENSIVE_MODELS.has(r.model))
            return false;
        if (r.tokens.output > 5000)
            return false; // not simple if lots of output
        if (r.messageCount > 10)
            return false; // not simple if long session
        if (r.toolsUsed.length > 3)
            return false; // not simple if many tools
        return true;
    });
    if (simpleOnExpensive.length < 3)
        return [];
    const totalCost = simpleOnExpensive.reduce((s, r) => s + r.costUsd, 0);
    let haikuCost = 0;
    for (const r of simpleOnExpensive) {
        haikuCost += (0, pricing_1.calculateCost)(r.tokens, "haiku");
    }
    const days = spanDays(simpleOnExpensive);
    const savings = ((totalCost - haikuCost) / days) * 30;
    if (savings < 0.5)
        return [];
    const models = Array.from(new Set(simpleOnExpensive.map((r) => r.model)));
    return [{
            system: "openclaw",
            agentName: "",
            wasteType: "overpowered_model",
            tier: 2,
            severity: savings > 10 ? "high" : "medium",
            confidence: 0.6,
            description: `${simpleOnExpensive.length} simple sessions (low output, few tools) using ${models.join("/")}`,
            monthlyWasteUsd: savings,
            monthlyWasteTokens: simpleOnExpensive.reduce((s, r) => s + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: `Route simple tasks to Haiku. Saves ~$${savings.toFixed(2)}/month.`,
            fixSnippet: `# Route simple tasks to a cheaper model:\n"model": "haiku"  # for quick checks and simple edits`,
            evidence: { simpleCount: simpleOnExpensive.length, modelsUsed: models },
        }];
}
/** Detect cheap model used for complex tasks. */
function detectWeakModel(runs, _config) {
    const CHEAP = new Set(["haiku", "gpt-5-mini", "gpt-5-nano", "gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano",
        "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-flash-lite", "deepseek-v3", "qwen3-mini", "local"]);
    const complexOnCheap = runs.filter((r) => {
        if (!CHEAP.has(r.model))
            return false;
        if ((0, models_1.totalTokens)(r.tokens) < 100_000)
            return false;
        if (r.toolsUsed.length < 10)
            return false;
        return true;
    });
    if (complexOnCheap.length < 2)
        return [];
    return [{
            system: "openclaw",
            agentName: "",
            wasteType: "weak_model",
            tier: 2,
            severity: "low",
            confidence: 0.5,
            description: `${complexOnCheap.length} complex sessions (100K+ tokens, 10+ tools) using cheap models`,
            monthlyWasteUsd: 0,
            monthlyWasteTokens: 0,
            recommendation: "Consider Sonnet for complex sessions to reduce errors and retries.",
            fixSnippet: "# Upgrade model for complex tasks:\n\"model\": \"sonnet\"  # better for multi-tool sessions",
            evidence: { complexCount: complexOnCheap.length },
        }];
}
/** Detect sessions with very high message count relative to output (bad decomposition). */
function detectBadDecomposition(runs, _config) {
    // High message count + high input + low output ratio = monolithic prompts causing confusion
    const monolithic = runs.filter((r) => {
        if (r.messageCount < 30)
            return false;
        if ((0, models_1.totalTokens)(r.tokens) < 200_000)
            return false;
        const ratio = r.tokens.output / Math.max(r.tokens.input, 1);
        return ratio < 0.02; // less than 2% output
    });
    if (monolithic.length < 2)
        return [];
    const totalWaste = monolithic.reduce((s, r) => s + r.costUsd, 0);
    const days = spanDays(monolithic);
    const monthlyCost = (totalWaste / days) * 30;
    return [{
            system: "openclaw",
            agentName: "",
            wasteType: "bad_decomposition",
            tier: 2,
            severity: "medium",
            confidence: 0.5,
            description: `${monolithic.length} sessions with 30+ messages but <2% output ratio (potential monolithic prompts)`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: monolithic.reduce((s, r) => s + (0, models_1.totalTokens)(r.tokens), 0),
            recommendation: "Break large requests into sequential steps: one task per message improves accuracy.",
            fixSnippet: "# Split monolithic prompts:\n# Instead of one long prompt, chain smaller tasks",
            evidence: { monolithicCount: monolithic.length },
        }];
}
// ---------------------------------------------------------------------------
// Tier 3: Context composition (never-used skill detection)
// ---------------------------------------------------------------------------
/**
 * Detect installed skills that have never been invoked across recorded sessions.
 *
 * Severity is ratio-based: if >80% of installed skills are unused, the finding
 * is "high". If >60% are unused, "low". Otherwise "info" (treated as "low" since
 * Severity does not include "info"; clamped to "low").
 *
 * Savings estimate: each unused active skill costs ~100 tokens per message in
 * description-loading overhead (startup cost). We report total token overhead
 * across the unused set and $0 USD (token count is the actionable signal here).
 *
 * @param installed  List of active skill names (from SkillDetail.name, non-archived)
 * @param usageMap   Map of normalized skill name -> invocation count (from getSkillUsageHistory)
 */
function detectUnusedSkills(installed, usageMap) {
    if (installed.length === 0)
        return [];
    // Normalize installed names the same way getSkillUsageHistory normalizes tool names
    const unused = installed.filter((name) => {
        const key = name.toLowerCase().trim();
        return !usageMap.has(key) || (usageMap.get(key) ?? 0) === 0;
    });
    if (unused.length === 0)
        return [];
    const unusedRatio = unused.length / installed.length;
    let severity;
    if (unusedRatio > 0.8) {
        severity = "high";
    }
    else if (unusedRatio > 0.6) {
        severity = "medium";
    }
    else {
        severity = "low"; // "info" not in Severity type; use "low" for soft signal
    }
    // ~100 tokens startup overhead per skill per message
    const TOKENS_PER_SKILL = 100;
    const monthlyWasteTokens = unused.length * TOKENS_PER_SKILL;
    return [
        {
            system: "openclaw",
            agentName: "",
            wasteType: "unused_skills",
            tier: 3,
            severity,
            confidence: 0.75,
            description: `${unused.length} of ${installed.length} installed skills have never been invoked (${Math.round(unusedRatio * 100)}% unused)`,
            monthlyWasteUsd: 0,
            monthlyWasteTokens,
            recommendation: `Archive or remove the ${unused.length} unused skill(s) to reclaim ~${monthlyWasteTokens.toLocaleString()} tokens of per-message startup overhead.`,
            fixSnippet: "# Archive unused skills to reduce context overhead:\n" +
                "# openclaw skills archive <skill-name>\n" +
                "# Or use /token-optimizer manage to toggle from the dashboard.",
            evidence: {
                unusedCount: unused.length,
                installedCount: installed.length,
                unusedRatioPct: Math.round(unusedRatio * 100),
                unusedSkills: unused,
                tokensPerSkill: TOKENS_PER_SKILL,
            },
        },
    ];
}
// ---------------------------------------------------------------------------
// Tier 2: Output token waste (v2.4.0)
// ---------------------------------------------------------------------------
/**
 * Detect sessions where output tokens are disproportionately high relative
 * to input and message count, suggesting verbose or repetitive model responses.
 */
function detectOutputWaste(runs, _config) {
    const OUTPUT_RATIO_THRESHOLD = 0.4; // output > 40% of total is suspicious
    const MIN_TOTAL_TOKENS = 50_000;
    const suspects = runs.filter((r) => {
        const total = (0, models_1.totalTokens)(r.tokens);
        if (total < MIN_TOTAL_TOKENS)
            return false;
        const outputRatio = r.tokens.output / total;
        return outputRatio > OUTPUT_RATIO_THRESHOLD && r.messageCount > 5;
    });
    if (suspects.length < 2)
        return [];
    const excessOutput = suspects.reduce((sum, r) => {
        const healthyOutput = (0, models_1.totalTokens)(r.tokens) * 0.2;
        return sum + Math.max(0, r.tokens.output - healthyOutput);
    }, 0);
    const totalWaste = suspects.reduce((sum, r) => {
        const healthyOutput = (0, models_1.totalTokens)(r.tokens) * 0.2;
        const excessRatio = Math.max(0, r.tokens.output - healthyOutput) / r.tokens.output;
        return sum + r.costUsd * excessRatio;
    }, 0);
    const days = spanDays(suspects);
    const monthlyCost = (totalWaste / days) * 30;
    if (monthlyCost < 0.5)
        return [];
    return [{
            system: "openclaw",
            agentName: "",
            wasteType: "output_waste",
            tier: 2,
            severity: monthlyCost < 5 ? "low" : monthlyCost < 20 ? "medium" : "high",
            confidence: 0.6,
            description: `${suspects.length} sessions with >40% output token ratio (${Math.round(excessOutput).toLocaleString()} excess output tokens)`,
            monthlyWasteUsd: monthlyCost,
            monthlyWasteTokens: excessOutput,
            recommendation: "Add conciseness instructions to agent prompts. Request structured output formats to reduce verbosity.",
            fixSnippet: "# Add to agent system prompt:\n# Respond concisely. No explanations unless asked.",
            evidence: {
                suspectCount: suspects.length,
                avgOutputRatio: Math.round(suspects.reduce((sum, r) => sum + r.tokens.output / (0, models_1.totalTokens)(r.tokens), 0) /
                    suspects.length * 100),
            },
        }];
}
// ---------------------------------------------------------------------------
// Registry: all detectors in execution order
// ---------------------------------------------------------------------------
exports.ALL_DETECTORS = [
    { name: "heartbeat_model_waste", tier: 1, fn: detectHeartbeatModelWaste },
    {
        name: "heartbeat_over_frequency",
        tier: 1,
        fn: detectHeartbeatOverFrequency,
    },
    { name: "stale_cron", tier: 1, fn: detectStaleCronConfig },
    { name: "empty_runs", tier: 2, fn: detectEmptyRuns },
    { name: "session_history_bloat", tier: 2, fn: detectSessionHistoryBloat },
    { name: "loop_detection", tier: 2, fn: detectLoops },
    { name: "abandoned_sessions", tier: 2, fn: detectAbandonedSessions },
    { name: "ghost_token_qjl", tier: 2, fn: detectGhostTokenQJL },
    { name: "tool_loading_overhead", tier: 3, fn: detectToolLoadingOverhead },
    { name: "retry_churn", tier: 2, fn: detectRetryChurn },
    { name: "tool_cascade", tier: 2, fn: detectToolCascade },
    { name: "overpowered_model", tier: 2, fn: detectOverpoweredModel },
    { name: "weak_model", tier: 2, fn: detectWeakModel },
    { name: "bad_decomposition", tier: 2, fn: detectBadDecomposition },
    { name: "output_waste", tier: 2, fn: detectOutputWaste },
    // unused_skills is NOT in ALL_DETECTORS because it requires an external
    // installed-skill list (not derivable from AgentRun data alone).
    // Call detectUnusedSkills() directly, passing auditContext().skills.
];
/**
 * Run all detectors against the given runs and config.
 * Returns all findings sorted by monthly waste (highest first).
 */
function runAllDetectors(runs, config = {}) {
    const findings = [];
    for (const detector of exports.ALL_DETECTORS) {
        try {
            const results = detector.fn(runs, config);
            findings.push(...results);
        }
        catch (err) {
            console.warn(`Detector '${detector.name}' failed: ${err instanceof Error ? err.message : String(err)}`);
            continue;
        }
    }
    // Sort by monthly waste, highest first
    findings.sort((a, b) => b.monthlyWasteUsd - a.monthlyWasteUsd);
    return findings;
}
//# sourceMappingURL=waste-detectors.js.map