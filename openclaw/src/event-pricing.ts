import * as fs from "fs";
import * as path from "path";
import { getPricing, loadPricingTier, tierMultiplier } from "./pricing";

export function resolvePricedModel(model: string, openclawDir: string): string | null {
  const pricing = getPricing(openclawDir);
  const own = (key: string) => Object.prototype.hasOwnProperty.call(pricing, key);
  if (own(model)) return model;
  const id = model.toLowerCase();
  const bare = id.replace(/^(?:anthropic|openai|google|deepseek|vertex)\//, "");
  const dated = bare.replace(/-(?:\d{8}|\d{4}-\d{2}-\d{2})$/, "");
  if (own(dated)) return dated;
  if (!/(?:^|[/:.])claude-/.test(id) && !/^(?:fable|mythos|opus|sonnet|haiku)[-._]?\d/.test(id)) return null;
  if (!/^(?:(?:anthropic\/|anthropic\.)?claude-)?(?:\d+(?:[-.]\d+)?-(?:opus|sonnet|haiku)|(?:fable|mythos|opus|sonnet|haiku)[-._]?\d+(?:[-._]\d+)?)(?:-\d{8}(?:-v\d+:\d+)?)?$/.test(id)) return null;
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
  return null;
}


/** Billing class a savings event's avoided tokens would have been charged at. */
export type SavingsEventClass = "input" | "output" | "cacheRead" | "cacheWrite" | "cacheWrite1h";
const SAVINGS_EVENT_CLASSES = new Set<string>(["input", "output", "cacheRead", "cacheWrite", "cacheWrite1h"]);
export function savingsEventClass(value: unknown): SavingsEventClass | undefined {
  return typeof value === "string" && SAVINGS_EVENT_CLASSES.has(value) ? value as SavingsEventClass : undefined;
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

export function priceSavingsEvent(tokens: number, eventType: string, session: SessionEvidence | undefined, openclawDir: string, tokenClass?: SavingsEventClass): {cost: number; unpricedTokens: number; floorTokens: number} {
  const amount = Number.isFinite(tokens) ? Math.abs(tokens) : 0;
  const sign = tokens < 0 ? -1 : 1;
  if (!session) return {cost: 0, unpricedTokens: amount, floorTokens: 0};
  const output = tokenClass === undefined ? (eventType === "verbosity_steer" || eventType === "verbosity_steer_measured") : false;
  // With no stored billed class, a non-output event is priced at the input
  // rate: an honest floor, disclosed to the caller via floorTokens.
  const klass: SavingsEventClass = tokenClass ?? (output ? "output" : "input");
  const floor = tokenClass === undefined && !output;
  const buckets = session.modelTokenBuckets;
  const weights: Array<[string, number]> = [];
  if (buckets && typeof buckets === "object") {
    for (const [model, bucket] of Object.entries(buckets)) {
      if (!bucket || typeof bucket !== "object") continue;
      const numeric = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0;
      const weight = klass === "output" ? numeric(bucket.output)
        : klass === "cacheRead" ? numeric(bucket.cacheRead)
        : klass === "cacheWrite" || klass === "cacheWrite1h" ? numeric(bucket.cacheWrite)
        : numeric(bucket.input) + numeric(bucket.cacheRead) + numeric(bucket.cacheWrite);
      if (weight > 0) weights.push([model, weight]);
    }
  }
  if (weights.length === 0 && session.model && !buckets) weights.push([session.model, 1]);
  const total = weights.reduce((sum, [,weight]) => sum + weight, 0);
  if (!total) return {cost: 0, unpricedTokens: amount, floorTokens: 0};
  let cost = 0, unpricedTokens = 0, floorTokens = 0;
  for (const [model, weight] of weights) {
    const part = amount * weight / total;
    const key = resolvePricedModel(model, openclawDir);
    if (!key) { unpricedTokens += part; continue; }
    const card = getPricing(openclawDir)[key];
    // A class the provider does not bill (missing or zero rate) reverts to the
    // fresh-input rate: on such a card new context is charged as input.
    const classRate = klass === "input" ? card.input
      : klass === "cacheWrite1h" ? (card.cacheWrite1h ?? card.cacheWrite)
      : card[klass];
    const rate = classRate || card.input;
    cost += part * rate * tierMultiplier(loadPricingTier(openclawDir), key);
    if (floor) floorTokens += part;
  }
  return {cost: cost * sign, unpricedTokens, floorTokens};
}
