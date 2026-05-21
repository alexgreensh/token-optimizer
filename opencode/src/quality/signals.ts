import type { SessionStore } from "../storage/session-store.js";

const STALE_READ_DISTANCE_THRESHOLD = 120;
const BLOAT_THRESHOLD_CHARS = 4000;
const CHARS_PER_TOKEN = 4;

export interface StaleReadResult {
  count: number;
  estimatedWasteTokens: number;
}

export function detectStaleReads(store: SessionStore, limit: number): StaleReadResult {
  const reads = store.getRecentReads(limit);
  const writes = store.getRecentWrites(limit * 2);

  const writesByPath = new Map<string, number[]>();
  for (const w of writes) {
    const arr = writesByPath.get(w.path) ?? [];
    arr.push(w.idx);
    writesByPath.set(w.path, arr);
  }

  const readsByPath = new Map<string, number[]>();
  for (const r of reads) {
    const arr = readsByPath.get(r.path) ?? [];
    arr.push(r.idx);
    readsByPath.set(r.path, arr);
  }

  let staleCount = 0;
  let wasteTokens = 0;
  const AVG_READ_TOKENS = 2000;

  for (const r of reads) {
    const pathWrites = writesByPath.get(r.path);
    if (!pathWrites || pathWrites.length === 0) continue;

    const priorWrites = pathWrites.filter((w) => w < r.idx);
    if (priorWrites.length > 0) {
      staleCount++;
      wasteTokens += AVG_READ_TOKENS;
      continue;
    }

    const laterWrites = pathWrites.filter((w) => w > r.idx);
    if (laterWrites.length === 0) continue;
    const firstLaterWrite = laterWrites[0];
    if (firstLaterWrite - r.idx > STALE_READ_DISTANCE_THRESHOLD) {
      const laterReads = (readsByPath.get(r.path) ?? []).filter((lr) => lr > r.idx);
      if (laterReads.length === 0) {
        staleCount++;
        wasteTokens += AVG_READ_TOKENS / 2;
      }
    }
  }

  return { count: staleCount, estimatedWasteTokens: wasteTokens };
}

export interface BloatedResultsResult {
  count: number;
  estimatedWasteTokens: number;
}

export function detectBloatedResults(store: SessionStore, limit: number): BloatedResultsResult {
  const results = store.getRecentToolResults(limit);
  const messages = store.getRecentMessages(limit);

  // Sort ascending by idx so the early-break on idx > r.idx + 10 works correctly
  // (DB returns DESC by id, but we need ASC by operation index for lookahead)
  messages.sort((a, b) => a.idx - b.idx);

  let bloatedCount = 0;
  let wasteTokens = 0;

  for (const r of results) {
    if (r.result_size < BLOAT_THRESHOLD_CHARS) continue;

    let wasReferenced = false;
    for (const m of messages) {
      if (m.idx > r.idx && m.role === "assistant" && m.is_substantive) {
        wasReferenced = true;
        break;
      }
      if (m.idx > r.idx + 10) break;
    }

    if (!wasReferenced) {
      bloatedCount++;
      wasteTokens += Math.floor(r.result_size / CHARS_PER_TOKEN);
    }
  }

  return { count: bloatedCount, estimatedWasteTokens: wasteTokens };
}

export interface DecisionDensityResult {
  substantive: number;
  total: number;
  ratio: number;
}

export function computeDecisionDensity(store: SessionStore, windowSize: number): DecisionDensityResult {
  const messages = store.getRecentMessages(windowSize);
  const substantive = messages.filter((m) => m.is_substantive).length;
  const total = messages.length;
  const ratio = total > 0 ? substantive / total : 0;
  return { substantive, total, ratio };
}

export interface AgentEfficiencyResult {
  dispatches: number;
  efficiency: number;
}

export function computeAgentEfficiency(store: SessionStore, windowSize: number): AgentEfficiencyResult {
  const dispatches = store.getRecentAgentDispatches(windowSize);
  if (dispatches.length === 0) {
    return { dispatches: 0, efficiency: 0.8 };
  }

  const totalPrompt = dispatches.reduce((s, d) => s + d.prompt_size, 0);
  const totalResult = dispatches.reduce((s, d) => s + d.result_size, 0);
  if (totalPrompt <= 0) {
    return { dispatches: dispatches.length, efficiency: 0.8 };
  }
  const total = totalPrompt + totalResult;
  const efficiency = total > 0 ? totalResult / total : 0.5;

  return { dispatches: dispatches.length, efficiency };
}
