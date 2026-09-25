/** Pi's append-only JSONL is a tree. Only count entries on the active leaf's ancestry. */
export type Usage = {
  input: number; output: number; cacheRead: number; cacheWrite: number;
  totalTokens: number; cost?: { total?: number };
};
export type Entry = {
  id: string; parentId: string | null; type: string;
  message?: { role?: string; provider?: string; model?: string; usage?: Usage };
  kind?: string; provider?: string; model?: string; usage?: Usage;
};
export type ModelTotals = {
  provider: string; model: string; input: number; output: number;
  cacheRead: number; cacheWrite: number; totalTokens: number;
  nativeCost: number; unpricedCalls: number; calls: number;
};
export type Rollup = { models: ModelTotals[]; entries: number; invalidLines: number };
const finite = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0;
export function activeBranch(entries: Entry[], leafId?: string | null): Entry[] {
  const byId = new Map(entries.map(e => [e.id, e]));
  const branch: Entry[] = [];
  const seen = new Set<string>();
  let id = leafId === undefined ? entries.at(-1)?.id : leafId ?? undefined;
  while (id && byId.has(id) && !seen.has(id)) {
    seen.add(id);
    const entry = byId.get(id)!;
    branch.push(entry);
    id = entry.parentId ?? undefined;
  }
  return branch.reverse();
}
export function rollup(entries: Entry[]): Rollup {
  const models = new Map<string, ModelTotals>();
  for (const entry of entries) {
    const isAssistant = entry.type === "message" && entry.message?.role === "assistant";
    const isUsage = entry.type === "usage";
    if (!isAssistant && !isUsage) continue;
    const m = entry.message;
    const usage = isAssistant ? m?.usage : entry.usage;
    const model = isAssistant ? m?.model : entry.model;
    const provider = isAssistant ? m?.provider : entry.provider;
    if (!usage || !model || !provider) continue;
    const key = `${provider}\0${model}`;
    let result = models.get(key);
    if (!result) {
      result = { provider, model, input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, nativeCost: 0, unpricedCalls: 0, calls: 0 };
      models.set(key, result);
    }
    result.input += finite(usage.input);
    result.output += finite(usage.output);
    result.cacheRead += finite(usage.cacheRead);
    result.cacheWrite += finite(usage.cacheWrite);
    result.totalTokens += finite(usage.totalTokens);
    if (typeof usage.cost?.total === "number" && Number.isFinite(usage.cost.total) && usage.cost.total >= 0) result.nativeCost += usage.cost.total;
    else result.unpricedCalls++;
    result.calls++;
  }
  return { models: [...models.values()], entries: entries.length, invalidLines: 0 };
}
/** Parse an immutable snapshot; malformed trailing lines are ignored, not charged. */
export function parseSession(text: string, leafId?: string | null): Rollup {
  let invalidLines = 0;
  const entries: Entry[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const item: unknown = JSON.parse(line);
      if (!item || typeof item !== "object") { invalidLines++; continue; }
      const e = item as Partial<Entry>;
      if (typeof e.id === "string" && typeof e.type === "string" && (e.parentId === null || typeof e.parentId === "string")) entries.push(e as Entry);
    } catch { invalidLines++; }
  }
  return { ...rollup(activeBranch(entries, leafId)), invalidLines };
}
/** Track entry IDs in memory, reset on fork/switch; rollup remains branch-aware. */
export class IncrementalRollup {
  private signature = "";
  private value: Rollup = { models: [], entries: 0, invalidLines: 0 };
  update(branch: Entry[]): Rollup {
    const signature = branch.map(e => e.id).join("\0");
    if (signature !== this.signature) {
      this.signature = signature;
      this.value = rollup(branch);
    }
    return this.value;
  }
}
