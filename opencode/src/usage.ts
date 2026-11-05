/** Per-message usage retained by model and token class. */
export interface ModelUsage {
  model: string;
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  cost: number;
}

export type ModelTokenBuckets = Record<string, Omit<ModelUsage, "model">>;

export type DatedModelUsage = ModelUsage & { activityAt?: number };

/** Unknown timestamps stay visible as incomplete coverage, never recording time. */
export function aggregateDailyUsage(messages: Iterable<DatedModelUsage>): Record<string, ModelTokenBuckets> {
  const days: Record<string, DatedModelUsage[]> = {};
  for (const message of messages) {
    const timestamp = message.activityAt;
    const day = typeof timestamp === "number" && Number.isFinite(timestamp) && timestamp > 0 && Number.isFinite(new Date(timestamp).getTime())
      ? new Date(timestamp).toISOString().slice(0, 10) : "undated";
    (days[day] ??= []).push(message);
  }
  return Object.fromEntries(Object.entries(days).map(([day, usage]) => [day, aggregateModelUsage(usage)]));
}

export function aggregateModelUsage(messages: Iterable<ModelUsage>): ModelTokenBuckets {
  const buckets: ModelTokenBuckets = {};
  for (const message of messages) {
    const key = message.model || "unknown";
    const bucket = buckets[key] ??= { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 };
    bucket.input += message.input;
    bucket.output += message.output;
    bucket.cacheRead += message.cacheRead;
    bucket.cacheWrite += message.cacheWrite;
    bucket.cost += message.cost;
  }
  return buckets;
}
