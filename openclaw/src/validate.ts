/**
 * Validate impact: compare session metrics before vs after an optimization.
 *
 * Two strategies:
 *   auto: split at the most recent session with outcome "success" after a gap
 *   halves: split session history chronologically in half
 */

import { AgentRun, totalTokens } from "./models";
import { calculateCost } from "./pricing";

export type Strategy = "auto" | "halves";
export type Verdict = "improved" | "regressed" | "no_change" | "insufficient_data";

export interface WindowSummary {
  count: number;
  avgTokens: number;
  avgCost: number;
  avgMessages: number;
  avgCacheHitRate: number;
}

export interface ValidateResult {
  strategy: Strategy;
  splitLabel: string;
  before: WindowSummary;
  after: WindowSummary;
  deltas: { tokensPct: number; costPct: number; messagesPct: number; cacheHitPct: number };
  verdict: Verdict;
}

function summarize(runs: AgentRun[]): WindowSummary {
  const n = runs.length;
  if (n === 0) return { count: 0, avgTokens: 0, avgCost: 0, avgMessages: 0, avgCacheHitRate: 0 };

  const totalTok = runs.reduce((s, r) => s + totalTokens(r.tokens), 0);
  const totalCost = runs.reduce((s, r) => s + r.costUsd, 0);
  const totalMsgs = runs.reduce((s, r) => s + r.messageCount, 0);
  const totalCHR = runs.reduce((s, r) => {
    const full = totalTokens(r.tokens);
    return s + (full > 0 ? r.tokens.cacheRead / full : 0);
  }, 0);

  return {
    count: n,
    avgTokens: Math.round(totalTok / n),
    avgCost: Math.round((totalCost / n) * 10000) / 10000,
    avgMessages: Math.round(totalMsgs / n),
    avgCacheHitRate: Math.round((totalCHR / n) * 1000) / 1000,
  };
}

function delta(before: number, after: number, invert: boolean): number {
  if (before === 0) return 0;
  const raw = ((after - before) / Math.abs(before)) * 100;
  return Math.round((invert ? -raw : raw) * 10) / 10;
}

export function validateImpact(runs: AgentRun[], strategy: Strategy = "auto"): ValidateResult {
  if (runs.length < 4) {
    return {
      strategy,
      splitLabel: `insufficient data (${runs.length} sessions)`,
      before: summarize([]),
      after: summarize([]),
      deltas: { tokensPct: 0, costPct: 0, messagesPct: 0, cacheHitPct: 0 },
      verdict: "insufficient_data",
    };
  }

  // Sort oldest first
  const sorted = [...runs].sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());

  let before: AgentRun[];
  let after: AgentRun[];
  let splitLabel: string;

  if (strategy === "auto") {
    // Find the biggest gap between sessions as the likely optimization boundary
    let maxGap = 0;
    let splitIdx = Math.floor(sorted.length / 2);

    for (let i = 1; i < sorted.length; i++) {
      const gap = sorted[i].timestamp.getTime() - sorted[i - 1].timestamp.getTime();
      if (gap > maxGap) {
        maxGap = gap;
        splitIdx = i;
      }
    }

    // Only use gap-based split if it's meaningful (>2 sessions each side)
    if (splitIdx < 2 || sorted.length - splitIdx < 2) {
      splitIdx = Math.floor(sorted.length / 2);
      splitLabel = `chronological midpoint (auto fallback, ${splitIdx} sessions each side)`;
    } else {
      const gapHours = Math.round(maxGap / 3_600_000);
      splitLabel = `${gapHours}h gap at session ${splitIdx}`;
    }

    before = sorted.slice(0, splitIdx);
    after = sorted.slice(splitIdx);
  } else {
    const mid = Math.floor(sorted.length / 2);
    before = sorted.slice(0, mid);
    after = sorted.slice(mid);
    splitLabel = `chronological midpoint (${mid} sessions each side)`;
  }

  const beforeSummary = summarize(before);
  const afterSummary = summarize(after);

  const deltas = {
    tokensPct: delta(beforeSummary.avgTokens, afterSummary.avgTokens, true),
    costPct: delta(beforeSummary.avgCost, afterSummary.avgCost, true),
    messagesPct: delta(beforeSummary.avgMessages, afterSummary.avgMessages, true),
    cacheHitPct: delta(beforeSummary.avgCacheHitRate, afterSummary.avgCacheHitRate, false),
  };

  const improved = Object.values(deltas).filter((v) => v > 10).length;
  const regressed = Object.values(deltas).filter((v) => v < -10).length;

  let verdict: Verdict = "no_change";
  if (improved >= 2) verdict = "improved";
  else if (regressed >= 2) verdict = "regressed";

  return { strategy, splitLabel, before: beforeSummary, after: afterSummary, deltas, verdict };
}
