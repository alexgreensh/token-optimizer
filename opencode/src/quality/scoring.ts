import type { SessionStore } from "../storage/session-store.js";
import { scoreToGrade, degradationBand } from "../util/grade.js";
import { estimateQualityFromFill } from "./curves.js";
import {
  detectStaleReads,
  detectBloatedResults,
  computeDecisionDensity,
  computeAgentEfficiency,
} from "./signals.js";
import type { TokenOptimizerConfig } from "../util/env.js";

const RESOURCE_HEALTH_WEIGHTS = {
  context_fill_degradation: 0.5,
  compaction_depth: 0.3,
  absolute_waste_tokens: 0.2,
} as const;

const SESSION_EFFICIENCY_WEIGHTS = {
  stale_reads: 0.3,
  bloated_results: 0.3,
  decision_density: 0.2,
  agent_efficiency: 0.2,
} as const;

const FILL_WARN_THRESHOLDS: Array<[number, string, string]> = [
  [0.85, "CRITICAL", "85% context fill, compact now"],
  [0.75, "WARNING", "75% context fill, consider compacting"],
];

export interface QualityResult {
  score: number;
  grade: string;
  resourceHealth: number;
  resourceHealthGrade: string;
  sessionEfficiency: number;
  sessionEfficiencyGrade: string;
  signals: Record<string, number>;
  breakdown: Record<string, SignalBreakdown>;
  fillWarning: FillWarning | null;
  toolCallWarning: ToolCallWarning | null;
  regimeChange: RegimeChange | null;
  toolCalls: number;
  fillPct: number;
}

export interface SignalBreakdown {
  score: number;
  detail: string;
  [key: string]: unknown;
}

export interface FillWarning {
  level: string;
  fillPct: number;
  message: string;
}

export interface ToolCallWarning {
  level: string;
  toolCalls: number;
  message: string;
}

export interface RegimeChange {
  fillPct: number;
  message: string;
}

function scaledToolCallThresholds(contextWindow: number, config: TokenOptimizerConfig): { warn: number; critical: number } {
  const scale = Math.max(1.0, (contextWindow / 200_000) ** 1.3);
  const warn = Math.max(1, config.toolCallWarnThreshold ?? Math.floor(25 * scale));
  const critical = Math.max(1, config.toolCallCriticalThreshold ?? Math.floor(40 * scale));
  return { warn, critical };
}

export function computeQualityScore(
  store: SessionStore,
  fillPct: number,
  model: string | undefined,
  contextWindow: number,
  config: TokenOptimizerConfig,
): QualityResult {
  const window = config.qualityWindow;

  // Signal 0: Context fill degradation (MRCR-based)
  const { quality: fillQuality, curveName } = estimateQualityFromFill(fillPct, model, contextWindow);
  const fillScore = Math.max(0, Math.min(100, ((fillQuality - 76) / (98 - 76)) * 100));

  // Signal 1: Stale reads (rolling window)
  const staleData = detectStaleReads(store, window);
  const recentReads = store.getRecentReads(window);
  let staleScore: number;
  if (recentReads.length > 0) {
    const staleRatio = Math.min(1, staleData.count / recentReads.length);
    staleScore = Math.max(0, Math.min(100, 100 - staleRatio * 100));
  } else {
    staleScore = 100;
  }

  // Signal 2: Bloated results (rolling window)
  const bloatedData = detectBloatedResults(store, window);
  const recentResults = store.getRecentToolResults(window);
  let bloatedScore: number;
  if (recentResults.length > 0) {
    const bloatedRatio = Math.min(1, bloatedData.count / recentResults.length);
    bloatedScore = Math.max(0, Math.min(100, 100 - bloatedRatio * 300));
  } else {
    bloatedScore = 100;
  }

  // Signal 3: Compaction depth
  const compactions = store.getCompactionCount();
  let compactionScore: number;
  if (compactions === 0) compactionScore = 100;
  else if (compactions === 1) compactionScore = 75;
  else if (compactions === 2) compactionScore = 45;
  else compactionScore = 20;

  // Signal 4: Decision density (rolling window)
  const densityData = computeDecisionDensity(store, window);
  const densityScore = densityData.total > 0 ? Math.min(100, densityData.ratio * 200) : 50;

  // Signal 5: Agent efficiency (rolling window)
  const agentData = computeAgentEfficiency(store, window);
  const agentScore = agentData.dispatches > 0 ? Math.min(100, agentData.efficiency * 150) : 80;

  // Signal 6: Absolute waste tokens
  const totalWaste = staleData.estimatedWasteTokens + bloatedData.estimatedWasteTokens;
  const wasteFraction = contextWindow > 0 ? totalWaste / contextWindow : 0;
  const wasteScore = Math.max(0, Math.min(100, 100 - wasteFraction * 1000));

  const signals: Record<string, number> = {
    context_fill_degradation: round1(fillScore),
    stale_reads: round1(staleScore),
    bloated_results: round1(bloatedScore),
    compaction_depth: round1(compactionScore),
    decision_density: round1(densityScore),
    agent_efficiency: round1(agentScore),
    absolute_waste_tokens: round1(wasteScore),
  };

  // Dual composite
  const resourceHealth =
    signals.context_fill_degradation * RESOURCE_HEALTH_WEIGHTS.context_fill_degradation +
    signals.compaction_depth * RESOURCE_HEALTH_WEIGHTS.compaction_depth +
    signals.absolute_waste_tokens * RESOURCE_HEALTH_WEIGHTS.absolute_waste_tokens;

  const sessionEfficiency =
    signals.stale_reads * SESSION_EFFICIENCY_WEIGHTS.stale_reads +
    signals.bloated_results * SESSION_EFFICIENCY_WEIGHTS.bloated_results +
    signals.decision_density * SESSION_EFFICIENCY_WEIGHTS.decision_density +
    signals.agent_efficiency * SESSION_EFFICIENCY_WEIGHTS.agent_efficiency;

  // Compaction loss estimate
  let compactionLossPct = 0;
  if (compactions === 1) compactionLossPct = 65;
  else if (compactions === 2) compactionLossPct = 88;
  else if (compactions >= 3) compactionLossPct = 95;

  const bandName = degradationBand(fillPct);

  const breakdown: Record<string, SignalBreakdown> = {
    context_fill_degradation: {
      score: signals.context_fill_degradation,
      fillPct: round1(fillPct * 100),
      qualityEstimate: fillQuality,
      qualityCurve: curveName,
      model: model ?? "unknown",
      modelContextWindow: contextWindow,
      band: bandName,
      detail: `${Math.round(fillPct * 100)}% fill, ${bandName.toLowerCase()} (${curveName})`,
    },
    stale_reads: {
      score: signals.stale_reads,
      count: staleData.count,
      windowReads: recentReads.length,
      estimatedWasteTokens: staleData.estimatedWasteTokens,
      detail: staleData.count > 0
        ? `${staleData.count} stale file reads (${recentReads.length} in window)`
        : "No stale reads",
    },
    bloated_results: {
      score: signals.bloated_results,
      count: bloatedData.count,
      windowResults: recentResults.length,
      estimatedWasteTokens: bloatedData.estimatedWasteTokens,
      detail: bloatedData.count > 0
        ? `${bloatedData.count} bloated results (${recentResults.length} in window)`
        : "No bloated results",
    },
    compaction_depth: {
      score: signals.compaction_depth,
      compactions,
      cumulativeLossPct: compactionLossPct,
      detail: compactions > 0
        ? `${compactions} compaction(s) (~${compactionLossPct}% cumulative context loss)`
        : "No compactions",
    },
    decision_density: {
      score: signals.decision_density,
      substantiveMessages: densityData.substantive,
      windowMessages: densityData.total,
      ratio: round2(densityData.ratio),
      detail: densityData.total > 0
        ? `${Math.round(densityData.ratio * 100)}% substantive (${densityData.total} in window)`
        : "No messages",
    },
    agent_efficiency: {
      score: signals.agent_efficiency,
      dispatchCount: agentData.dispatches,
      detail: agentData.dispatches > 0
        ? `${agentData.dispatches} agent dispatches`
        : "No agents used",
    },
    absolute_waste_tokens: {
      score: signals.absolute_waste_tokens,
      totalWasteTokens: totalWaste,
      wasteFraction: round4(wasteFraction),
      detail: totalWaste > 0
        ? `${totalWaste} waste tokens (${round1(wasteFraction * 100)}% of window)`
        : "No measurable waste",
    },
  };

  // Fill warnings (independent of composite)
  let fillWarning: FillWarning | null = null;
  for (const [threshold, level, message] of FILL_WARN_THRESHOLDS) {
    if (fillPct >= threshold) {
      fillWarning = { level, fillPct: round1(fillPct * 100), message };
      break;
    }
  }

  // Tool call fatigue (gated on fill >= 50%)
  const toolCalls = store.getToolCallCount();
  let toolCallWarning: ToolCallWarning | null = null;
  if (fillPct >= 0.5) {
    const { warn, critical } = scaledToolCallThresholds(contextWindow, config);
    if (toolCalls >= critical) {
      toolCallWarning = {
        level: "CRITICAL",
        toolCalls,
        message: `${critical}+ tool calls, instruction adherence severely degraded`,
      };
    } else if (toolCalls >= warn) {
      toolCallWarning = {
        level: "WARNING",
        toolCalls,
        message: `${warn}+ tool calls, consider a fresh session`,
      };
    }
  }

  // 50% fill regime change
  let regimeChange: RegimeChange | null = null;
  if (fillPct > 0.5) {
    regimeChange = {
      fillPct: round1(fillPct * 100),
      message: "System prompt erosion accelerating, middle content at highest risk",
    };
  }

  const rhRounded = round1(resourceHealth);
  const seRounded = round1(sessionEfficiency);

  return {
    score: rhRounded,
    grade: scoreToGrade(Math.round(resourceHealth)),
    resourceHealth: rhRounded,
    resourceHealthGrade: scoreToGrade(Math.round(resourceHealth)),
    sessionEfficiency: seRounded,
    sessionEfficiencyGrade: scoreToGrade(Math.round(sessionEfficiency)),
    signals,
    breakdown,
    fillWarning,
    toolCallWarning,
    regimeChange,
    toolCalls,
    fillPct,
  };
}

export function enforceMonotonicity(
  newResult: QualityResult,
  cachedResourceHealth: number | null,
  cachedCompactions: number,
  currentCompactions: number,
): QualityResult {
  if (cachedResourceHealth === null) return newResult;
  if (currentCompactions > cachedCompactions) return newResult;

  if (newResult.resourceHealth > cachedResourceHealth) {
    const clamped = { ...newResult };
    clamped.resourceHealth = cachedResourceHealth;
    clamped.score = cachedResourceHealth;
    clamped.grade = scoreToGrade(Math.round(cachedResourceHealth));
    clamped.resourceHealthGrade = clamped.grade;
    return clamped;
  }

  return newResult;
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function round4(n: number): number {
  return Math.round(n * 10000) / 10000;
}
