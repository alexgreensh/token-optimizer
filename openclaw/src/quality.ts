/**
 * Quality Scoring for OpenClaw.
 *
 * 7-signal two-stage quality metric adapted for OpenClaw's architecture.
 * Stage 1: 5 coarse signals (context fill, session length, model routing, empty runs, outcomes)
 * Stage 2: 2 TurboQuant-inspired semantic signals (message efficiency, compression opportunity)
 * Includes distortion bounds analysis for theoretical quality ceiling.
 * Score: 0-100 with color bands (Good/Fair/Needs Work/Poor).
 */

import { AgentRun, totalTokens } from "./models";
import { ContextAudit } from "./context-audit";

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

/** Context window sizes by model family (tokens). Verified March 17, 2026. */
const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  // Anthropic (Opus/Sonnet 1M GA since March 13, 2026)
  opus: 1_000_000,
  sonnet: 1_000_000,
  haiku: 200_000,
  // OpenAI GPT-5 family (only 5.4 has ~1.1M, rest are ~400K)
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

export function contextWindowForModel(model: string): number {
  return MODEL_CONTEXT_WINDOWS[model] ?? 200_000;
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
      const window = contextWindowForModel(r.model);
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
    (r) => r.model === "opus" || r.model === "sonnet" ||
           r.model.includes("gpt-5.4") || r.model.includes("gpt-5.2")
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
// Distortion bounds (TurboQuant-inspired theoretical quality ceiling)
// ---------------------------------------------------------------------------

export interface DistortionBounds {
  /** Best possible quality score for this configuration. */
  theoreticalMax: number;
  /** Current achieved quality score. */
  achievedScore: number;
  /** Ratio of achieved to theoretical maximum (0-1). */
  utilization: number;
  /** Actionable recommendation based on utilization. */
  recommendation: string;
}

/**
 * Compute theoretical quality bounds based on TurboQuant distortion theory.
 *
 * At any compression ratio, there is a minimum distortion (quality loss)
 * that cannot be avoided. For context windows, the effective capacity
 * determines the distortion floor: larger windows relative to message
 * size yield diminishing returns in quality improvement.
 *
 * @param runs - Agent runs to analyze
 * @param modelContextWindow - Context window size in tokens for the dominant model
 * @returns Distortion bounds with theoretical max, achieved score, and utilization
 */
export function computeDistortionBounds(
  runs: AgentRun[],
  modelContextWindow: number
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

  // Compute the achieved score using the existing scorer (without distortion context)
  const signals: QualitySignal[] = [
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
  const fingerprints: string[] = [];

  for (const r of runs) {
    const raw = [
      r.agentName,
      r.model,
      r.runType,
      r.toolsUsed.sort().join(","),
      `msgs:${r.messageCount}`,
    ]
      .join("|")
      .slice(0, 100);
    fingerprints.push(raw);
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
    distortionBounds = computeDistortionBounds(runs, ctxWindow);
    // Override achievedScore with the actual computed score
    distortionBounds.achievedScore = score;
    distortionBounds.utilization = distortionBounds.theoreticalMax > 0
      ? Math.round((score / distortionBounds.theoreticalMax) * 1000) / 1000
      : 0;
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
  const ctxWindow = contextWindowForModel(run.model);
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
