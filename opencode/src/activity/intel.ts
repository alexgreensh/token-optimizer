const MAX_SUMMARIES_PER_WINDOW = 3;
const COOLDOWN_WINDOW_MS = 5 * 60 * 1000;
const LARGE_OUTPUT_THRESHOLD = 8192;

let recentSummaries: number[] = [];

export function summarizeLargeOutput(output: string): void {
  if (output.length < LARGE_OUTPUT_THRESHOLD) return;

  const now = Date.now();
  recentSummaries = recentSummaries.filter((t) => now - t < COOLDOWN_WINDOW_MS);
  if (recentSummaries.length >= MAX_SUMMARIES_PER_WINDOW) return;
  recentSummaries.push(now);
}

export function resetIntelCooldown(): void {
  recentSummaries = [];
}
