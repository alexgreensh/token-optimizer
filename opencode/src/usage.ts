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
