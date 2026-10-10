/**
 * Quality Scoring for OpenClaw.
 *
 * 7-signal two-stage quality metric adapted for OpenClaw's architecture.
 * Stage 1: 5 coarse signals (context fill, session length, model routing, empty runs, outcomes)
 * Stage 2: 2 TurboQuant-inspired semantic signals (message efficiency, compression opportunity)
 * Includes distortion bounds analysis for estimated quality ceiling.
 * Score: 0-100 with color bands (Good/Fair/Needs Work/Poor).
 */

import { AgentRun, totalTokens, EXPENSIVE_MODELS } from "./models";
import { ContextAudit } from "./context-audit";
import { claudePricingKey, normalizeModelName, DEFAULT_PRICING, pricedPrefixKey } from "./pricing";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface QualitySignal {
  name: string;
  weight: number;
  score: number;
  description: string;
}

export interface QualityReport {
  score: number;
  grade: string;
  band: string;
  signals: QualitySignal[];
  recommendations: string[];
  distortionBounds?: DistortionBounds;
}

// ---------------------------------------------------------------------------
// Signal scorers (each returns 0-100)
// ---------------------------------------------------------------------------

/** Context window sizes by model family (tokens). Claude ids use claudeContextWindow() below. */
const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  // (Claude ids resolve through claudeContextWindow() below, not this table.)
  // OpenAI GPT-5 family
  "gpt-5.6": 1_050_000,
  "gpt-5.6-sol": 1_050_000,
  "gpt-5.6-terra": 1_050_000,
  "gpt-5.6-luna": 1_050_000,
  "gpt-5.4": 1_100_000,
  "gpt-5.2": 400_000,
  "gpt-5.1": 400_000,
  "gpt-5": 400_000,
  "gpt-5-mini": 400_000,
  "gpt-5-nano": 400_000,
  // OpenAI GPT-4 family
  "gpt-4.1": 1_000_000,
  "gpt-4.1-mini": 1_000_000,
  "gpt-4.1-nano": 1_000_000,
  "gpt-4o": 128_000,
  "gpt-4o-mini": 128_000,
  // OpenAI reasoning
  "o3": 200_000,
  "o3-mini": 200_000,
  "o4-mini": 200_000,
  // Google Gemini
  "gemini-3-pro": 1_000_000,
  "gemini-3-flash": 1_000_000,
  "gemini-3.1-pro": 1_000_000,
  "gemini-2.5-pro": 2_000_000,
  "gemini-2.5-flash": 1_000_000,
  "gemini-2.0-flash": 1_000_000,
  "gemini-2.0-flash-lite": 1_000_000,
  // DeepSeek
  "deepseek-v3": 128_000,
  "deepseek-r1": 128_000,
  // Qwen
  "qwen3": 128_000,
  "qwen3-mini": 128_000,
  "qwen-coder": 128_000,
  // Mistral
  "mistral-large": 262_000,
  "mistral-small": 128_000,
  // xAI
  "grok-4": 131_000,
  // Other
  "kimi-k2.5": 128_000,
  "minimax-2": 128_000,
  "glm-4.7": 128_000,
  "glm-4.7-flash": 128_000,
  "mimo-flash": 128_000,
  "o3-pro": 200_000,
  local: 128_000,
};

const DEFAULT_CONTEXT_WINDOW = 200_000;

/**
 * Context window for a Claude model id, per Claude Code's model-config docs
 * (checked 2026-10-10): Fable, Sonnet 5+, Opus 4.7+ and Haiku 5.5 are 1M with
 * no suffix; Sonnet 4.6 / Opus 4.6 are 1M only as the `[1m]` variant (200K
 * without); every older Claude is 200K. Returns null when the id carries no
 * Claude family so callers fall through to their other rules.
 */
export function claudeContextWindow(model: string): number | null {
  const raw = (model ?? "").toLowerCase().trim();
  const oneM = raw.includes("[1m]") || raw.includes("1000k");
  const id = raw
    .replace("[1m]", "")
    .replace(/^.*\//, "")
    .replace(/[-@]\d{8}$/, "");
  if (/claude[-_]?[0-3]\b/.test(id) || /claude-\d(?:[-.]\d)?-(opus|sonnet|haiku)/.test(id)) return 200_000;
  const m = /(fable|mythos|opus|sonnet|haiku)(?:[-_.](\d+))?(?:[-_.](\d{1,2}))?(?!\d)/.exec(id);
  if (!m) return null;
  const [, family, majorRaw, minorRaw] = m;
  if (family === "fable" || family === "mythos") return 1_000_000;
  const major = majorRaw === undefined ? null : parseInt(majorRaw, 10);
  const minor = minorRaw === undefined ? 0 : parseInt(minorRaw, 10);
  if (family === "haiku") {
    if (major === null) return 200_000; // bare alias: conservative
    return major > 5 || (major === 5 && minor >= 5) ? 1_000_000 : 200_000;
  }
  // opus / sonnet: a bare alias resolves to the current 5.x line (1M).
  if (major === null || major >= 5) return 1_000_000;
  if (major === 4 && family === "opus" && minor >= 7) return 1_000_000;
  if (major === 4 && minor === 6) return oneM ? 1_000_000 : 200_000;
  return 200_000;
}

/**
 * Resolve a model's context window. Tries exact match, then a Claude-family rule,
 * then substring match, so a full model id (e.g. "claude-sonnet-4-6",
 * "anthropic/claude-opus-4-8") resolves to its real window instead of silently
 * defaulting to 200K -- which would overstate fill% up to ~5x for a 1M-window
 * Claude session. Unknown models fall back to a conservative 200K ASSUMED
 * window; callers must label fill as an estimate against an assumed window.
 */
export function contextWindowForModel(model: string): number {
  if (!model) return DEFAULT_CONTEXT_WINDOW;
  const lower = model.toLowerCase();
  const direct = MODEL_CONTEXT_WINDOWS[lower];
  if (direct !== undefined) return direct;
  // Claude families: one table-driven rule (see claudeContextWindow).
  if (lower.includes("claude") || lower.includes("fable") || lower.includes("opus") || lower.includes("sonnet") || lower.includes("haiku")) {
    const claude = claudeContextWindow(lower);
    if (claude !== null) return claude;
  }
  for (const [key, value] of Object.entries(MODEL_CONTEXT_WINDOWS)) {
    if (lower.includes(key)) return value;
  }
  return DEFAULT_CONTEXT_WINDOW;
}

/**
 * Opus/Sonnet 4.6 are 1M only as the `[1m]` variant, but the runtime records
 * the plain id. Observed tokens above the 200K window prove the 1M variant, so
 * widen the window instead of reporting a fill above 100%. Any other model, or
 * tokens that fit, keep the table window.
 */
export function promoteWindowForObservedTokens(model: string, window: number, tokens: number): number {
  const lower = (model ?? "").toLowerCase();
  if (window >= 1_000_000 || lower.includes("[1m]")) return window;
  if (!/(?:^|[^a-z])(?:opus|sonnet)[-_.]4[-_.]6(?!\d)/.test(lower)) return window;
  return tokens > window ? 1_000_000 : window;
}

/**
 * Signal 1: Context fill (20%)
 * How much of each model's context window is being used.
 * Uses per-model context window sizes for accurate measurement.
 */
function scoreContextFill(
  runs: AgentRun[],
  contextAudit?: ContextAudit | null
): QualitySignal {
  let avgFill = 0;
  let dominantWindow = 200_000;

  if (runs.length > 0) {
    const fills = runs.map((r) => {
      const window = promoteWindowForObservedTokens(
        r.model, contextWindowForModel(r.model), r.tokens.input);
      return r.tokens.input / window;
    });
    avgFill = fills.reduce((a, b) => a + b, 0) / fills.length;

    // Find the most-used model's window for the overhead calculation
    const modelCounts = new Map<string, number>();
    for (const r of runs) {
      modelCounts.set(r.model, (modelCounts.get(r.model) ?? 0) + 1);
    }
    let maxCount = 0;
    for (const [model, count] of modelCounts) {
      if (count > maxCount) {
        maxCount = count;
        dominantWindow = contextWindowForModel(model);
      }
    }
  }

  // Also factor in static overhead relative to the dominant model's window
  const overhead = contextAudit ? contextAudit.totalOverhead / dominantWindow : 0;
  const effectiveFill = Math.max(avgFill, overhead);

  let score: number;
  if (effectiveFill < 0.2) score = 100;
  else if (effectiveFill < 0.4) score = 80;
  else if (effectiveFill < 0.6) score = 60;
  else if (effectiveFill < 0.8) score = 30;
  else score = 10;

  const pctStr = (effectiveFill * 100).toFixed(0);
  const windowStr = dominantWindow >= 1_000_000 ? `${dominantWindow / 1_000_000}M` : `${dominantWindow / 1_000}K`;

  return {
    name: "Context Fill",
    weight: 0.20,
    score,
    description: `Average context fill: ${pctStr}% of ${windowStr} window. ${score >= 70 ? "Healthy headroom." : "Consider compaction or trimming overhead."}`,
  };
}

/**
 * Signal 2: Session length risk (16%)
 * Longer sessions = higher risk of quality degradation.
 */
function scoreSessionLength(runs: AgentRun[]): QualitySignal {
  if (runs.length === 0) {
    return {
      name: "Session Length Risk",
      weight: 0.16,
      score: 0,
      description: "Insufficient data. Run some sessions first.",
    };
  }

  const COMPACT_THRESHOLD = 50;
  const longSessions = runs.filter((r) => r.messageCount > COMPACT_THRESHOLD);
  const longPct = (longSessions.length / runs.length) * 100;
  const avgMessages = runs.reduce((s, r) => s + r.messageCount, 0) / runs.length;

  let score: number;
  if (longPct < 5) score = 100;
  else if (longPct < 15) score = 75;
  else if (longPct < 30) score = 50;
  else score = 20;

  return {
    name: "Session Length Risk",
    weight: 0.16,
    score,
    description: `${longPct.toFixed(0)}% of sessions exceed ${COMPACT_THRESHOLD} messages (avg: ${avgMessages.toFixed(0)}). ${score >= 70 ? "Sessions are well-managed." : "Enable auto-compaction to reduce risk."}`,
  };
}

/**
 * Signal 3: Model routing efficiency (16%)
 * Are expensive models used for cheap tasks?
 */
function scoreModelRouting(runs: AgentRun[]): QualitySignal {
  if (runs.length === 0) {
    return {
      name: "Model Routing",
      weight: 0.16,
      score: 0,
      description: "Insufficient data. Run some sessions first.",
    };
  }

  const heartbeats = runs.filter(
    (r) => r.runType === "heartbeat" || r.runType === "cron"
  );

  if (heartbeats.length === 0) {
    return {
      name: "Model Routing",
      weight: 0.16,
      score: 85,
      description: "No heartbeat/cron tasks detected. Manual runs not scored for routing.",
    };
  }

  const expensiveHeartbeats = heartbeats.filter(
    (r) => EXPENSIVE_MODELS.has(r.model)
  );

  const misroutePct = (expensiveHeartbeats.length / heartbeats.length) * 100;

  let score: number;
  if (misroutePct === 0) score = 100;
  else if (misroutePct < 10) score = 80;
  else if (misroutePct < 30) score = 50;
  else score = 15;

  return {
    name: "Model Routing",
    weight: 0.16,
    score,
    description: `${misroutePct.toFixed(0)}% of heartbeats use expensive models. ${score >= 70 ? "Good routing." : "Route cron/heartbeat tasks to Haiku or equivalent."}`,
  };
}

/**
 * Signal 4: Empty run ratio (16%)
 * Runs that load context but produce nothing useful.
 */
function scoreEmptyRuns(runs: AgentRun[]): QualitySignal {
  if (runs.length === 0) {
    return {
      name: "Empty Run Ratio",
      weight: 0.16,
      score: 0,
      description: "Insufficient data. Run some sessions first.",
    };
  }

  const emptyRuns = runs.filter(
    (r) => r.outcome === "empty" ||
           (totalTokens(r.tokens) > 5000 && r.tokens.output < 100 && r.messageCount <= 4)
  );

  const emptyPct = (emptyRuns.length / runs.length) * 100;

  let score: number;
  if (emptyPct < 5) score = 100;
  else if (emptyPct < 15) score = 70;
  else if (emptyPct < 30) score = 40;
  else score = 10;

  return {
    name: "Empty Run Ratio",
    weight: 0.16,
    score,
    description: `${emptyPct.toFixed(0)}% of runs are empty (loaded context, produced nothing). ${score >= 70 ? "Efficient usage." : "Add guard conditions to skip idle runs."}`,
  };
}

/**
 * Signal 5: Outcome health (12%)
 * Ratio of success vs abandoned/empty/failure.
 */
function scoreOutcomeHealth(runs: AgentRun[]): QualitySignal {
  if (runs.length === 0) {
    return {
      name: "Outcome Health",
      weight: 0.12,
      score: 0,
      description: "Insufficient data. Run some sessions first.",
    };
  }

  const successCount = runs.filter((r) => r.outcome === "success").length;
  const successPct = (successCount / runs.length) * 100;

  let score: number;
  if (successPct > 80) score = 100;
  else if (successPct > 60) score = 70;
  else if (successPct > 40) score = 40;
  else score = 15;

  const failCount = runs.filter((r) => r.outcome === "failure").length;
  const abandonCount = runs.filter((r) => r.outcome === "abandoned").length;

  return {
    name: "Outcome Health",
    weight: 0.12,
    score,
    description: `${successPct.toFixed(0)}% success rate (${failCount} failures, ${abandonCount} abandoned). ${score >= 70 ? "Healthy outcomes." : "High failure/abandon rate, investigate root causes."}`,
  };
}

// ---------------------------------------------------------------------------
// Distortion bounds (TurboQuant-inspired estimated quality ceiling)
// ---------------------------------------------------------------------------

export interface DistortionBounds {
  /** Estimated best quality score for this configuration (heuristic upper bound). */
  theoreticalMax: number;
  /** Current achieved quality score. */
  achievedScore: number;
  /** Ratio of achieved to estimated maximum (0-1). */
  utilization: number;
  /** Actionable recommendation based on utilization. */
  recommendation: string;
}

/**
 * Compute estimated quality bounds inspired by TurboQuant distortion concepts.
 *
 * Uses a heuristic based on context window capacity to estimate an upper
 * bound on achievable quality. This is a useful approximation, not a
 * proven mathematical limit — treat it as an estimated ceiling.
 *
 * @param runs - Agent runs to analyze
 * @param modelContextWindow - Context window size in tokens for the dominant model
 * @returns Distortion bounds with estimated max, achieved score, and utilization
 */
export function computeDistortionBounds(
  runs: AgentRun[],
  modelContextWindow: number,
  precomputedSignals?: QualitySignal[]
): DistortionBounds {
  if (runs.length === 0) {
    return {
      theoreticalMax: 100,
      achievedScore: 0,
      utilization: 0,
      recommendation: "No data available. Run some sessions to establish quality bounds.",
    };
  }

  // Average message tokens across all runs
  const avgMessageTokens = runs.reduce(
    (sum, r) => sum + (r.messageCount > 0 ? totalTokens(r.tokens) / r.messageCount : 0),
    0
  ) / runs.length;

  // Effective capacity: how many "message slots" the context window can hold
  const effectiveCapacity = avgMessageTokens > 0
    ? modelContextWindow / avgMessageTokens
    : modelContextWindow / 1000; // fallback: assume 1K tokens per message

  // Distortion floor: diminishing returns curve
  const distortionFloor = 1 / Math.sqrt(Math.max(1, effectiveCapacity));

  // Theoretical maximum quality score
  const theoreticalMax = Math.round(100 * (1 - distortionFloor));

  // Use pre-computed signals when available to avoid duplicate computation
  const signals = precomputedSignals ?? [
    scoreContextFill(runs),
    scoreSessionLength(runs),
    scoreModelRouting(runs),
    scoreEmptyRuns(runs),
    scoreOutcomeHealth(runs),
    scoreMessageEfficiency(runs),
    scoreCompressionOpportunity(runs),
  ];
  const achievedScore = Math.round(
    Math.min(100, Math.max(0, signals.reduce((sum, sig) => sum + sig.score * sig.weight, 0)))
  );

  const utilization = theoreticalMax > 0 ? achievedScore / theoreticalMax : 0;

  let recommendation: string;
  if (utilization > 0.85) {
    recommendation =
      "Near optimal — structural changes needed for further gains. " +
      "Consider model upgrades or architecture redesign.";
  } else if (utilization > 0.7) {
    recommendation =
      "Good utilization. Targeted prompt optimization and caching " +
      "can close the remaining gap.";
  } else if (utilization >= 0.5) {
    recommendation =
      "Moderate room for improvement. Focus on reducing empty runs, " +
      "enabling compaction, and optimizing model routing.";
  } else {
    recommendation =
      "Significant room for improvement via signal optimization. " +
      "Start with the lowest-scoring quality signals above.";
  }

  return {
    theoreticalMax,
    achievedScore,
    utilization: Math.round(utilization * 1000) / 1000, // 3 decimal places
    recommendation,
  };
}

// ---------------------------------------------------------------------------
// Stage 2 signals (TurboQuant-inspired semantic layer)
// ---------------------------------------------------------------------------

/**
 * Signal 6: Message Efficiency (10%)
 *
 * Measures the ratio of output tokens to total tokens per session.
 * Low ratios indicate sessions that consume input without producing
 * meaningful output — analogous to low signal-to-noise ratio in
 * quantization theory.
 */
function scoreMessageEfficiency(runs: AgentRun[]): QualitySignal {
  if (runs.length === 0) {
    return {
      name: "Message Efficiency",
      weight: 0.10,
      score: 0,
      description: "Insufficient data. Run some sessions first.",
    };
  }

  const ratios = runs.map((r) => {
    const total = r.tokens.input + r.tokens.output;
    return total > 0 ? r.tokens.output / total : 0;
  });
  const avgRatio = ratios.reduce((a, b) => a + b, 0) / ratios.length;

  let score: number;
  if (avgRatio > 0.3) score = 100;
  else if (avgRatio > 0.2) score = 80;
  else if (avgRatio > 0.1) score = 50;
  else score = 20;

  return {
    name: "Message Efficiency",
    weight: 0.10,
    score,
    description: `Average output ratio: ${(avgRatio * 100).toFixed(1)}% of total tokens. ${score >= 70 ? "Good output efficiency." : "Sessions consume more input than they produce. Consider trimming system prompts."}`,
  };
}

/**
 * Signal 7: Compression Opportunity (10%)
 *
 * Estimates input redundancy by hashing the first 100 characters of each
 * run's identifying features (model + agent + tools). High redundancy
 * means many near-duplicate prompts are being sent, which represents
 * compressible waste.
 *
 * Inspired by TurboQuant's analysis of redundant attention patterns.
 */
function scoreCompressionOpportunity(runs: AgentRun[]): QualitySignal {
  if (runs.length === 0) {
    return {
      name: "Compression Opportunity",
      weight: 0.10,
      score: 0,
      description: "Insufficient data. Run some sessions first.",
    };
  }

  // Build a fingerprint for each run (first 100 chars of a canonical string)
  const hashes = new Set<string>();

  for (const r of runs) {
    const raw = [
      r.agentName,
      r.model,
      r.runType,
      [...r.toolsUsed].sort().join(","),
      `msgs:${r.messageCount}`,
    ]
      .join("|")
      .slice(0, 100);
    hashes.add(raw);
  }

  const redundancy = 1 - hashes.size / runs.length;

  let score: number;
  if (redundancy < 0.05) score = 100;
  else if (redundancy < 0.15) score = 80;
  else if (redundancy < 0.3) score = 50;
  else score = 20;

  return {
    name: "Compression Opportunity",
    weight: 0.10,
    score,
    description: `Message redundancy: ${(redundancy * 100).toFixed(0)}% of runs have near-duplicate profiles. ${score >= 70 ? "Low redundancy — inputs are diverse." : "High redundancy suggests repeated prompts. Consider caching or deduplication."}`,
  };
}

// ---------------------------------------------------------------------------
// Main scorer
// ---------------------------------------------------------------------------

function bandFromScore(score: number): string {
  if (score >= 80) return "Good";
  if (score >= 60) return "Fair";
  if (score >= 40) return "Needs Work";
  return "Poor";
}

/**
 * Convert a 0-100 quality score to a letter grade.
 * S: 90-100 | A: 80-89 | B: 70-79 | C: 55-69 | D: 40-54 | F: 0-39
 */
export function scoreToGrade(score: number): string {
  if (score >= 90) return "S";
  if (score >= 80) return "A";
  if (score >= 70) return "B";
  if (score >= 55) return "C";
  if (score >= 40) return "D";
  return "F";
}

function generateQualityRecommendations(signals: QualitySignal[]): string[] {
  const recs: string[] = [];

  const sorted = [...signals].sort((a, b) => a.score - b.score);

  for (const sig of sorted) {
    if (sig.score >= 70) continue;

    switch (sig.name) {
      case "Context Fill":
        recs.push("Trim context overhead: run `npx token-optimizer context` to identify bloated components.");
        break;
      case "Session Length Risk":
        recs.push("Enable Smart Compaction to automatically preserve session state during compaction.");
        break;
      case "Model Routing":
        recs.push("Route heartbeat/cron tasks to Haiku or a flash-tier model. Save the heavy models for manual work.");
        break;
      case "Empty Run Ratio":
        recs.push("Add early-exit conditions to heartbeat scripts: skip if no pending work.");
        break;
      case "Outcome Health":
        recs.push("Investigate failed and abandoned sessions. High abandonment may indicate excessive startup overhead.");
        break;
      case "Message Efficiency":
        recs.push("Output ratio is low — sessions consume more input than they produce. Trim system prompts and reduce context overhead.");
        break;
      case "Compression Opportunity":
        recs.push("High message redundancy detected. Cache repeated prompts or deduplicate system instructions across runs.");
        break;
    }
  }

  return recs;
}

export function scoreQuality(
  runs: AgentRun[],
  contextAudit?: ContextAudit | null
): QualityReport {
  // Stage 1: Original 5 coarse signals (weights adjusted to 80% total)
  // Stage 2: 2 new TurboQuant-inspired semantic signals (20% total)
  const signals: QualitySignal[] = [
    scoreContextFill(runs, contextAudit),
    scoreSessionLength(runs),
    scoreModelRouting(runs),
    scoreEmptyRuns(runs),
    scoreOutcomeHealth(runs),
    scoreMessageEfficiency(runs),
    scoreCompressionOpportunity(runs),
  ];

  const weightSum = signals.reduce((s, sig) => s + sig.weight, 0);
  if (Math.abs(weightSum - 1.0) > 0.001) {
    console.warn(`Quality signal weights sum to ${weightSum}, expected 1.0. Normalizing.`);
    const scale = 1.0 / weightSum;
    for (const sig of signals) sig.weight *= scale;
  }

  const weightedScore = signals.reduce(
    (sum, sig) => sum + sig.score * sig.weight,
    0
  );

  const score = Math.round(Math.min(100, Math.max(0, weightedScore)));
  const grade = scoreToGrade(score);
  const band = bandFromScore(score);
  const recommendations = generateQualityRecommendations(signals);

  // Compute distortion bounds for the dominant model
  let distortionBounds: DistortionBounds | undefined;
  if (runs.length > 0) {
    const modelCounts = new Map<string, number>();
    for (const r of runs) {
      modelCounts.set(r.model, (modelCounts.get(r.model) ?? 0) + 1);
    }
    let dominantModel = runs[0].model;
    let maxCount = 0;
    for (const [model, count] of modelCounts) {
      if (count > maxCount) {
        maxCount = count;
        dominantModel = model;
      }
    }
    const ctxWindow = contextWindowForModel(dominantModel);
    distortionBounds = computeDistortionBounds(runs, ctxWindow, signals);
  }

  return { score, grade, band, signals, recommendations, distortionBounds };
}

// ---------------------------------------------------------------------------
// Per-session quality scoring
// ---------------------------------------------------------------------------

/**
 * Score a single AgentRun's quality on a 0-100 scale.
 *
 * Signals (weights):
 *   1. Context fill (25%): input tokens / model context window
 *   2. Message count risk (25%): >50 messages = degraded
 *   3. Cache hit rate (20%): higher = better (OpenClaw: typically 0)
 *   4. Output/input ratio (15%): low ratio = wasteful
 *   5. Duration risk (15%): >60min sessions = risk
 */
export function scoreSessionQuality(run: AgentRun): { score: number; grade: string; band: string } {
  // Signal 1: Context fill (25%) - lower fill = better
  const ctxWindow = promoteWindowForObservedTokens(
    run.model, contextWindowForModel(run.model), run.tokens.input);
  const fillRatio = ctxWindow > 0 ? run.tokens.input / ctxWindow : 0;
  let fillScore: number;
  if (fillRatio < 0.2) fillScore = 100;
  else if (fillRatio < 0.4) fillScore = 80;
  else if (fillRatio < 0.6) fillScore = 60;
  else if (fillRatio < 0.8) fillScore = 30;
  else fillScore = 10;

  // Signal 2: Message count risk (25%) - >50 = degraded
  let msgScore: number;
  if (run.messageCount <= 20) msgScore = 100;
  else if (run.messageCount <= 35) msgScore = 80;
  else if (run.messageCount <= 50) msgScore = 60;
  else if (run.messageCount <= 80) msgScore = 30;
  else msgScore = 10;

  // Signal 3: Cache hit rate (20%)
  const totalTok = totalTokens(run.tokens);
  const cacheHitRate = totalTok > 0
    ? run.tokens.cacheRead / totalTok
    : 0;
  let cacheScore: number;
  if (cacheHitRate > 0.5) cacheScore = 100;
  else if (cacheHitRate > 0.3) cacheScore = 80;
  else if (cacheHitRate > 0.1) cacheScore = 60;
  else if (cacheHitRate > 0) cacheScore = 40;
  else cacheScore = 20; // No cache data (OpenClaw default)

  // Signal 4: Output/input ratio (15%) - low ratio = wasteful
  const outInRatio = run.tokens.input > 0
    ? run.tokens.output / run.tokens.input
    : 0;
  let ratioScore: number;
  if (outInRatio > 0.3) ratioScore = 100;
  else if (outInRatio > 0.15) ratioScore = 80;
  else if (outInRatio > 0.05) ratioScore = 60;
  else if (outInRatio > 0.01) ratioScore = 30;
  else ratioScore = 10;

  // Signal 5: Duration risk (15%) - >60min = risk
  const durationMin = run.durationSeconds / 60;
  let durationScore: number;
  if (durationMin <= 15) durationScore = 100;
  else if (durationMin <= 30) durationScore = 80;
  else if (durationMin <= 60) durationScore = 60;
  else if (durationMin <= 120) durationScore = 30;
  else durationScore = 10;

  const weighted =
    fillScore * 0.25 +
    msgScore * 0.25 +
    cacheScore * 0.20 +
    ratioScore * 0.15 +
    durationScore * 0.15;

  const score = Math.round(Math.min(100, Math.max(0, weighted)));
  const grade = scoreToGrade(score);
  const band = bandFromScore(score);

  return { score, grade, band };
}

// ---------------------------------------------------------------------------
// Fresh-session nudge
// ---------------------------------------------------------------------------

/** Quality threshold below which the nudge fires (mirrors Python _FRESH_NUDGE_QUALITY_THRESHOLD). */
export const FRESH_NUDGE_QUALITY_THRESHOLD = 70;

/** Minimum context fill % required for the nudge to fire (mirrors Python _FRESH_NUDGE_MIN_FILL). */
export const FRESH_NUDGE_MIN_FILL = 50;

/** Approximate tokens a fresh lean-resume block re-injects (mirrors Python _FRESH_NUDGE_LEAN_BLOCK_TOKENS). */
export const FRESH_NUDGE_LEAN_BLOCK_TOKENS = 1000;

/**
 * Estimate tokens reclaimed by starting a fresh session now.
 *
 * current_ctx = (fill_pct / 100) * context_window
 * saved       = max(0, current_ctx - FRESH_NUDGE_LEAN_BLOCK_TOKENS)
 *
 * Returns { savedTokens, contextWindow }.
 * Mirrors Python _fresh_session_savings_estimate().
 *
 * `sessionContextWindow` MUST be the same window fill_pct was measured against
 * (pass RuntimeEventContext.contextWindow or RuntimeSnapshot.contextWindow).
 * This prevents the token estimate from being inconsistent with the fill %:
 * e.g. "119K tokens = 60% of window" would be nonsense on a 1M-context session
 * if the estimate silently used a 200K fallback. Model-based re-detection is
 * kept as a fallback ONLY when the session window is genuinely unavailable.
 */
export function freshSessionSavingsEstimate(
  fillPct: number,
  model: string = "",
  sessionContextWindow?: number
): { savedTokens: number; contextWindow: number } {
  // Use the explicitly passed session window first (the authoritative value),
  // fall back to model-name detection only as a last resort.
  const contextWindow =
    (sessionContextWindow && sessionContextWindow > 0)
      ? sessionContextWindow
      : (contextWindowForModel(model) || 200_000);
  const safeFill = Math.min(100, Math.max(0, fillPct));
  const currentCtx = Math.round((safeFill / 100) * contextWindow);
  const savedTokens = Math.max(0, currentCtx - FRESH_NUDGE_LEAN_BLOCK_TOKENS);
  return { savedTokens, contextWindow };
}

/**
 * Compute the API-equivalent dollar value of the reclaimed tokens.
 *
 * Uses the model's input rate from DEFAULT_PRICING. Returns 0 on any error
 * or unknown model. Best-effort, never throws.
 * Mirrors Python _fresh_session_savings_usd().
 */
export function freshSessionSavingsUsd(savedTokens: number, model: string = ""): number {
  try {
    // Generation first (Opus 5.5 is not priced like Opus 5), then priced prefix.
    const key = claudePricingKey(model) ?? pricedPrefixKey(model) ?? normalizeModelName(model);
    const rates = key ? DEFAULT_PRICING[key] : undefined;
    const inputRatePerToken = rates?.input ?? 0;
    if (inputRatePerToken <= 0) return 0;
    return Math.max(0, savedTokens * inputRatePerToken);
  } catch {
    return 0;
  }
}

/**
 * Build the fresh-session nudge message string.
 *
 * Returns a string when ALL conditions are met:
 *   - qualityScore < FRESH_NUDGE_QUALITY_THRESHOLD (70)
 *   - fillPct >= FRESH_NUDGE_MIN_FILL (50%)
 *   - hasPriorScore = true (not a post-compaction/fresh session baseline)
 *
 * Returns null when any condition is not met. The caller is responsible for
 * once-per-session dedup (tracking whether the nudge has already fired).
 *
 * Takes precedence over the compact nudge: if this returns a message, the
 * caller must NOT also emit the /compact quality nudge.
 *
 * Mirrors Python _maybe_fresh_session_nudge() logic (minus cache read/write
 * and the _is_v5_feature_enabled guard, which the caller handles).
 *
 * `sessionContextWindow` should be RuntimeEventContext.contextWindow -- the exact
 * window used to derive fillPct. Avoids token/% inconsistency (e.g. "119K = 60%"
 * on a 1M session when a 200K fallback was used). Falls back to model detection
 * only when the session window is genuinely unavailable.
 */
export function buildFreshSessionNudgeMessage(
  qualityScore: number,
  fillPct: number,
  hasPriorScore: boolean,
  model: string = "",
  sessionContextWindow?: number
): string | null {
  if (!hasPriorScore) return null;
  if (!(qualityScore < FRESH_NUDGE_QUALITY_THRESHOLD && fillPct >= FRESH_NUDGE_MIN_FILL)) {
    return null;
  }

  const { savedTokens } = freshSessionSavingsEstimate(fillPct, model, sessionContextWindow);
  const savedStr = savedTokens >= 1000
    ? `~${Math.floor(savedTokens / 1000)}K`
    : `~${savedTokens}`;

  const usd = freshSessionSavingsUsd(savedTokens, model);
  const costStr = usd >= 0.01 ? `, about $${usd.toFixed(2)} in API-equivalent cost` : "";

  return (
    `[Token Optimizer] This session is long (${Math.round(fillPct)}% full) and context ` +
    `quality has fallen to ${qualityScore}. Starting a fresh session now would reclaim ` +
    `${savedStr} tokens (~${Math.round(fillPct)}% of your window)${costStr}. You won't lose your ` +
    `place: Token Optimizer has checkpointed your active task, key decisions, ` +
    `files, and tool results, so a new session picks up exactly where you ` +
    `stopped. Just open one and say "continue this" -- the context is rebuilt for free.`
  );
}
