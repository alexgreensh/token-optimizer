import * as fs from "fs";
import * as path from "path";
import { normalizeModelName, getPricing, claudePricingKey, pricedPrefixKey, loadPricingTier, tierMultiplier } from "./pricing";

export function resolvePricedModel(model: string, openclawDir: string): string | null {
  const pricing = getPricing(openclawDir);
  const own = (key: string) => Object.prototype.hasOwnProperty.call(pricing, key);
  if (own(model)) return model;
  const id = model.toLowerCase();
  const legacy = /(?:^|[/:.])claude-(\d+)(?:[-.](\d+))?-(opus|sonnet|haiku)(?=$|[-_:])/.exec(id);
  if (legacy) {
    const [, major, minor, family] = legacy;
    const exact = `${family}-${major}${minor ? `-${minor}` : ""}`;
    if (own(exact)) return exact;
    if (family === "sonnet" && major === "3" && (!minor || minor === "5" || minor === "7") && own("sonnet-legacy")) {
      return "sonnet-legacy";
    }
    return null;
  }
  const generation = /(?:^|[^a-z])(fable|mythos|opus|sonnet|haiku)[-._]?(\d+)(?:[-._](\d{1,2})(?!\d))?/.exec(id);
  if (generation) {
    const [, family, major, minor] = generation;
    const exact = `${family}-${major}${minor ? `-${minor}` : ""}`;
    if (own(exact)) return exact;
    if (family === "mythos") {
      const alias = `fable-${major}${minor ? `-${minor}` : ""}`;
      if (own(alias)) return alias;
    }
    return null;
  }
  const key = claudePricingKey(model, pricing) ?? pricedPrefixKey(model, pricing) ?? normalizeModelName(model);
  return key && own(key) ? key : null;
}


type SessionEvidence = { sessionId?: string; model?: string; modelTokenBuckets?: Record<string, {input?: number; output?: number; cacheRead?: number; cacheWrite?: number}> };
export function loadSavingsSessions(openclawDir: string): Map<string, SessionEvidence> {
  const out = new Map<string, SessionEvidence>();
  try {
    const rows = JSON.parse(fs.readFileSync(path.join(openclawDir, "token-optimizer", "session-history.json"), "utf8"));
    if (Array.isArray(rows)) for (const row of rows) if (row && typeof row.sessionId === "string") out.set(row.sessionId, row);
  } catch { /* session history not available */ }
  return out;
}

export function priceSavingsEvent(tokens: number, eventType: string, session: SessionEvidence | undefined, openclawDir: string): {cost: number; unpricedTokens: number} {
  const amount = Number.isFinite(tokens) ? Math.max(0, tokens) : 0;
  if (!session) return {cost: 0, unpricedTokens: amount};
  const output = eventType === "verbosity_steer" || eventType === "verbosity_steer_measured";
  const buckets = session.modelTokenBuckets;
  const weights: Array<[string, number]> = [];
  if (buckets && typeof buckets === "object") {
    for (const [model, bucket] of Object.entries(buckets)) {
      if (!bucket || typeof bucket !== "object") continue;
      const numeric = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0;
      const weight = output ? numeric(bucket.output) : numeric(bucket.input) + numeric(bucket.cacheRead) + numeric(bucket.cacheWrite);
      if (weight > 0) weights.push([model, weight]);
    }
  }
  if (weights.length === 0 && session.model && !buckets) weights.push([session.model, 1]);
  const total = weights.reduce((sum, [,weight]) => sum + weight, 0);
  if (!total) return {cost: 0, unpricedTokens: amount};
  let cost = 0, unpricedTokens = 0;
  for (const [model, weight] of weights) {
    const part = amount * weight / total;
    const key = resolvePricedModel(model, openclawDir);
    if (!key) { unpricedTokens += part; continue; }
    const card = getPricing(openclawDir)[key];
    cost += part * (output ? card.output : card.input) * tierMultiplier(loadPricingTier(openclawDir), key);
  }
  return {cost, unpricedTokens};
}
