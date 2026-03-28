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
 */

import * as fs from "fs";
import * as path from "path";
import { AgentRun, WasteFinding, Severity, totalTokens, EXPENSIVE_MODELS } from "./models";
import { calculateCost } from "./pricing";
import { computeSketch, sketchSimilarity, clusterBySketch } from "./jl-sketcher";

type DetectorFn = (
  runs: AgentRun[],
  config: Record<string, unknown>
) => WasteFinding[];

/** Compute the span in days between first and last run. Min 1 day. */
function spanDays(runs: AgentRun[]): number {
  if (runs.length < 2) return 1;
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
function detectHeartbeatModelWaste(
  runs: AgentRun[],
  _config: Record<string, unknown>
): WasteFinding[] {
  const heartbeats = runs.filter(
    (r) => r.runType === "heartbeat" || r.runType === "cron"
  );
  if (heartbeats.length === 0) return [];

  const expensive = heartbeats.filter(
    (r) => EXPENSIVE_MODELS.has(r.model)
  );
  if (expensive.length === 0) return [];

  const totalCost = expensive.reduce((sum, r) => sum + r.costUsd, 0);
  const daysSpanned = spanDays(expensive);
  const monthlyCost = (totalCost / daysSpanned) * 30;

  // Calculate savings if switched to haiku
  let haikuCost = 0;
  for (const r of expensive) {
    haikuCost += calculateCost(r.tokens, "haiku");
  }
  const haikuMonthly = (haikuCost / daysSpanned) * 30;
  const savings = monthlyCost - haikuMonthly;

  if (savings < 0.1) return [];

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
      monthlyWasteTokens: expensive.reduce(
        (sum, r) => sum + totalTokens(r.tokens),
        0
      ),
      recommendation: `Route heartbeat/cron tasks to Haiku. Saves ~$${savings.toFixed(2)}/month.`,
      fixSnippet:
        '# In your agent config (config.json or cron/*.json):\n"model": "haiku"  # was: opus/sonnet',
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
function detectHeartbeatOverFrequency(
  runs: AgentRun[],
  _config: Record<string, unknown>
): WasteFinding[] {
  const heartbeats = runs
    .filter((r) => r.runType === "heartbeat" || r.runType === "cron")
    .sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());

  if (heartbeats.length < 3) return [];

  const shortIntervals: number[] = [];
  for (let i = 1; i < heartbeats.length; i++) {
    const gap =
      (heartbeats[i].timestamp.getTime() -
        heartbeats[i - 1].timestamp.getTime()) /
      1000;
    if (gap > 0 && gap < 300) {
      shortIntervals.push(gap);
    }
  }

  if (shortIntervals.length < 3) return [];

  const avgInterval =
    shortIntervals.reduce((a, b) => a + b, 0) / shortIntervals.length;
  const avgCostPerHb =
    heartbeats.reduce((sum, r) => sum + r.costUsd, 0) / heartbeats.length;

  const runsPerHourActual = 3600 / avgInterval;
  const runsPerHourOptimal = 12; // 5-min intervals
  const extraPerHour = Math.max(0, runsPerHourActual - runsPerHourOptimal);
  const monthlyExtra = extraPerHour * 16 * 30; // 16 active hours/day
  const monthlyWaste = monthlyExtra * avgCostPerHb;

  if (monthlyWaste < 0.1) return [];

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
      fixSnippet:
        '# In your cron config:\n"interval": 300  # 5 minutes (was: shorter)',
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
function detectStaleCronConfig(
  _runs: AgentRun[],
  config: Record<string, unknown>
): WasteFinding[] {
  const hooks = config.hooks as Record<string, unknown[]> | undefined;
  if (!hooks) return [];

  const findings: WasteFinding[] = [];

  for (const [hookName, hookList] of Object.entries(hooks)) {
    if (!Array.isArray(hookList)) continue;

    for (const hook of hookList) {
      if (typeof hook !== "object" || hook === null) continue;
      const cmd = (hook as Record<string, unknown>).command as
        | string
        | undefined;
      if (!cmd) continue;

      const parts = cmd.split(/\s+/);
      for (const part of parts) {
        if (
          part.startsWith("/") &&
          !part.startsWith("/usr") &&
          !part.startsWith("/bin") &&
          !part.startsWith("$")
        ) {
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
              recommendation:
                "Remove or fix the hook referencing a dead path.",
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
function detectEmptyRuns(
  runs: AgentRun[],
  _config: Record<string, unknown>
): WasteFinding[] {
  const emptyRuns = runs.filter(
    (r) =>
      totalTokens(r.tokens) > 5000 &&
      r.tokens.output < 100 &&
      r.messageCount <= 4
  );

  if (emptyRuns.length === 0) return [];

  // Require substantial context or explicit empty outcome to confirm
  const confirmed = emptyRuns.filter(
    (r) => totalTokens(r.tokens) > 50_000 || r.outcome === "empty"
  );

  if (confirmed.length < 2) return [];

  const totalWasteCost = confirmed.reduce((sum, r) => sum + r.costUsd, 0);
  const days = spanDays(confirmed);
  const monthlyCost = (totalWasteCost / days) * 30;
  const monthlyTokens = confirmed.reduce(
    (sum, r) => sum + totalTokens(r.tokens),
    0
  );

  let severity: Severity = "medium";
  if (monthlyCost > 10) severity = "critical";
  else if (monthlyCost > 2) severity = "high";

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
      recommendation:
        "Add guard conditions to skip runs when nothing to do. Route idle checks to Haiku.",
      fixSnippet:
        '# Add early-exit check in heartbeat script:\nif ! has_pending_work; then exit 0; fi',
      evidence: {
        emptyCount: confirmed.length,
        avgInput: Math.round(
          confirmed.reduce((sum, r) => sum + r.tokens.input, 0) /
            confirmed.length
        ),
        avgOutput: Math.round(
          confirmed.reduce((sum, r) => sum + r.tokens.output, 0) /
            confirmed.length
        ),
      },
    },
  ];
}

/**
 * Detect sessions with growing context but no compaction.
 */
function detectSessionHistoryBloat(
  runs: AgentRun[],
  _config: Record<string, unknown>
): WasteFinding[] {
  const longSessions = runs.filter(
    (r) => r.messageCount > 30 && totalTokens(r.tokens) > 500_000
  );

  if (longSessions.length === 0) return [];

  const totalBloatTokens = longSessions.reduce(
    (sum, r) => sum + r.tokens.input,
    0
  );
  const savingsTokens = Math.round(totalBloatTokens * 0.4);
  const days = Math.max(
    1,
    new Set(
      longSessions.map((r) => r.timestamp.toISOString().slice(0, 10))
    ).size
  );

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
      recommendation:
        "Use compaction at 50-70% context fill. Smart Compaction protects session state automatically.",
      fixSnippet:
        "# Token Optimizer's Smart Compaction hooks handle this automatically.\n# Install: openclaw plugins install token-optimizer-openclaw",
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
function detectLoops(
  runs: AgentRun[],
  _config: Record<string, unknown>
): WasteFinding[] {
  const suspects = runs.filter(
    (r) =>
      r.messageCount > 20 &&
      r.tokens.output < r.messageCount * 2 &&
      totalTokens(r.tokens) > 100_000 &&
      r.outcome !== "empty" &&
      r.outcome !== "abandoned" &&
      r.runType === "manual"
  );

  if (suspects.length < 2) return [];

  const totalWaste = suspects.reduce((sum, r) => sum + r.costUsd, 0);
  const days = spanDays(suspects);
  const monthlyCost = (totalWaste / days) * 30;

  if (monthlyCost < 1.0) return [];

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
      monthlyWasteTokens: suspects.reduce(
        (sum, r) => sum + totalTokens(r.tokens),
        0
      ),
      recommendation:
        "Check these sessions for retry storms or stuck tool calls. Consider timeout/loop-break logic.",
      fixSnippet:
        "# Add loop detection to your agent:\n# Monitor output-to-input ratio, break if < 0.01 for 5+ turns",
      evidence: {
        suspectCount: suspects.length,
        avgMessages: Math.round(
          suspects.reduce((sum, r) => sum + r.messageCount, 0) /
            suspects.length
        ),
        avgOutput: Math.round(
          suspects.reduce((sum, r) => sum + r.tokens.output, 0) /
            suspects.length
        ),
      },
    },
  ];
}

/**
 * Detect sessions with 1-2 messages then stopped (wasted startup cost).
 */
function detectAbandonedSessions(
  runs: AgentRun[],
  _config: Record<string, unknown>
): WasteFinding[] {
  const abandoned = runs.filter(
    (r) =>
      r.messageCount <= 2 &&
      totalTokens(r.tokens) > 10_000 &&
      r.runType === "manual"
  );

  if (abandoned.length < 3) return [];

  const totalWaste = abandoned.reduce((sum, r) => sum + r.costUsd, 0);
  const days = spanDays(abandoned);
  const monthlyCost = (totalWaste / days) * 30;

  if (monthlyCost < 0.2) return [];

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
      monthlyWasteTokens: abandoned.reduce(
        (sum, r) => sum + totalTokens(r.tokens),
        0
      ),
      recommendation:
        "Quick checks are normal, but frequent abandons suggest startup overhead is too high.",
      fixSnippet:
        "# Reduce startup overhead:\n# Run /token-optimizer to identify and trim injected context",
      evidence: {
        abandonedCount: abandoned.length,
        avgInputTokens: Math.round(
          abandoned.reduce((sum, r) => sum + r.tokens.input, 0) /
            abandoned.length
        ),
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
function detectGhostTokenQJL(
  runs: AgentRun[],
  config: Record<string, unknown>
): WasteFinding[] {
  if (runs.length < 3) return [];

  const strategy = (config.ghostDetectorStrategy as string) ?? "simple";
  const clusters: AgentRun[][] =
    strategy === "sketch"
      ? clusterRunsBySketch(runs)
      : clusterRunsByGroup(runs);

  // Within each cluster, find ghost runs (output < 100 tokens)
  let ghostRuns: AgentRun[] = [];

  for (const cluster of clusters) {
    if (cluster.length < 2) continue;

    const ghosts = cluster.filter((r) => r.tokens.output < 100);

    // Only flag if ghosts are a meaningful portion of the cluster
    if (ghosts.length >= 2) {
      ghostRuns.push(...ghosts);
    }
  }

  if (ghostRuns.length < 2) return [];

  // De-duplicate (a run could appear in multiple clusters with sketch strategy)
  const seen = new Set<string>();
  ghostRuns = ghostRuns.filter((r) => {
    const key = `${r.sessionId}-${r.timestamp.getTime()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  if (ghostRuns.length < 2) return [];

  const totalWasteCost = ghostRuns.reduce((sum, r) => sum + r.costUsd, 0);
  const totalWasteTokens = ghostRuns.reduce(
    (sum, r) => sum + r.tokens.input,
    0
  );
  const days = spanDays(ghostRuns);
  const monthlyCost = (totalWasteCost / days) * 30;

  let severity: Severity = "medium";
  if (monthlyCost > 10) severity = "critical";
  else if (monthlyCost > 5) severity = "high";

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
      recommendation:
        "These runs load similar context repeatedly without producing output. " +
        "Add idempotency guards or cache results from prior identical runs.",
      fixSnippet:
        "# Add early-exit when context matches a recent successful run:\n" +
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
function clusterRunsByGroup(runs: AgentRun[]): AgentRun[][] {
  const groups = new Map<string, AgentRun[]>();
  for (const r of runs) {
    const key = `${r.agentName}|${r.model}|${r.runType}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(r);
  }
  return Array.from(groups.values());
}

/** Sketch-based O(n²) clustering using QJL 1-bit similarity. Falls back to simple grouping above 1000 runs. */
function clusterRunsBySketch(runs: AgentRun[]): AgentRun[][] {
  if (runs.length > 1000) return clusterRunsByGroup(runs);

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

  const clusters = clusterBySketch(items, 0.95);
  return clusters.map((ids) => ids
    .map((id) => runs[parseInt(id, 10)])
    .filter((r): r is AgentRun => r !== undefined)
  );
}

// ---------------------------------------------------------------------------
// Registry: all detectors in execution order
// ---------------------------------------------------------------------------

export const ALL_DETECTORS: Array<{
  name: string;
  tier: number;
  fn: DetectorFn;
}> = [
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
];

/**
 * Run all detectors against the given runs and config.
 * Returns all findings sorted by monthly waste (highest first).
 */
export function runAllDetectors(
  runs: AgentRun[],
  config: Record<string, unknown> = {}
): WasteFinding[] {
  const findings: WasteFinding[] = [];

  for (const detector of ALL_DETECTORS) {
    try {
      const results = detector.fn(runs, config);
      findings.push(...results);
    } catch (err) {
      console.warn(`Detector '${detector.name}' failed: ${err instanceof Error ? err.message : String(err)}`);
      continue;
    }
  }

  // Sort by monthly waste, highest first
  findings.sort((a, b) => b.monthlyWasteUsd - a.monthlyWasteUsd);
  return findings;
}
