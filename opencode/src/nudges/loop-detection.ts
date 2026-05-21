const SIMILARITY_THRESHOLD = 0.6;
const MIN_REPEATS = 3;
const LOOKBACK = 10;

export interface LoopWarning {
  detected: boolean;
  message: string | null;
}

function simpleFingerprint(text: string): string {
  return text
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200);
}

function jaccardSimilarity(a: string, b: string): number {
  const wordsA = new Set(a.split(/\s+/));
  const wordsB = new Set(b.split(/\s+/));
  if (wordsA.size === 0 && wordsB.size === 0) return 1;

  let intersection = 0;
  for (const w of wordsA) {
    if (wordsB.has(w)) intersection++;
  }
  const union = wordsA.size + wordsB.size - intersection;
  return union > 0 ? intersection / union : 0;
}

export function detectLoop(recentTexts: string[]): LoopWarning {
  if (recentTexts.length < MIN_REPEATS) {
    return { detected: false, message: null };
  }

  const window = recentTexts.slice(-LOOKBACK);
  const fingerprints = window.map(simpleFingerprint);

  const groups = new Map<number, number>();
  for (let i = 0; i < fingerprints.length; i++) {
    let foundGroup = -1;
    for (const [groupIdx, _count] of groups) {
      if (jaccardSimilarity(fingerprints[groupIdx], fingerprints[i]) >= SIMILARITY_THRESHOLD) {
        foundGroup = groupIdx;
        break;
      }
    }

    if (foundGroup >= 0) {
      groups.set(foundGroup, (groups.get(foundGroup) ?? 0) + 1);
    } else {
      groups.set(i, 1);
    }
  }

  for (const [_idx, count] of groups) {
    if (count >= MIN_REPEATS) {
      return {
        detected: true,
        message: `[Token Optimizer] Detected ${count} similar messages in the last ${window.length} turns. You may be in a retry loop. Consider a different approach or compacting context.`,
      };
    }
  }

  return { detected: false, message: null };
}
