/** Fast, local heuristics. Signals only; never hide errors or mutate user files. */
export function estimateTokens(text: string): number { return Math.ceil(text.length / 4); }
export function qualityScore(text: string): number {
  if (!text) return 100;
  const normalized = text.toLowerCase().replace(/\s+/g, " ");
  const repetitive = (normalized.match(/(?:^|\s)(?:sorry|apologies|let me|i will)(?:\s|$)/g) ?? []).length;
  return Math.max(0, 100 - Math.min(40, repetitive * 8));
}
export function repeatedCall(history: string[], signature: string, threshold = 3): boolean {
  return history.slice(-threshold + 1).every(s => s === signature) && history.length >= threshold - 1;
}
export function safeTrim(text: string, maxChars = 12000): string | null {
  if (text.length <= maxChars) return null;
  if (maxChars < 512) throw new RangeError("maxChars must be at least 512");
  const head = Math.floor(maxChars * .65), tail = maxChars - head;
  return `${text.slice(0, head)}\n\n[... middle omitted by Token Optimizer ...]\n\n${text.slice(-tail)}`;
}
export class ReadCache {
  private values = new Map<string, { fingerprint: string; text: string }>();
  get(path: string, fingerprint: string): string | undefined {
    const item = this.values.get(path);
    if (item && item.fingerprint !== fingerprint) { this.values.delete(path); return; }
    return item?.text;
  }
  set(path: string, fingerprint: string, text: string): void { this.values.set(path, { fingerprint, text }); }
  invalidate(path: string): void { this.values.delete(path); }
  clear(): void { this.values.clear(); }
}
