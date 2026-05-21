const STOP_WORDS = new Set([
  "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
  "have", "has", "had", "do", "does", "did", "will", "would", "shall",
  "should", "may", "might", "must", "can", "could", "to", "of", "in",
  "for", "on", "with", "at", "by", "from", "as", "into", "about",
  "like", "through", "after", "over", "between", "out", "up", "down",
  "that", "this", "it", "its", "my", "your", "his", "her", "we", "they",
  "them", "what", "which", "who", "when", "where", "how", "not", "no",
  "but", "or", "and", "if", "then", "so", "than", "too", "very", "just",
  "i", "me", "let", "us",
]);

function extractKeywords(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s_.-]/g, " ")
    .split(/\s+/)
    .filter((w) => w.length > 2 && !STOP_WORDS.has(w));
}

export function scoreRelevance(userPrompt: string, checkpointContent: string): number {
  const promptKeywords = extractKeywords(userPrompt);
  if (promptKeywords.length === 0) return 0;

  const contentLower = checkpointContent.toLowerCase();
  let matches = 0;

  for (const kw of promptKeywords) {
    if (contentLower.includes(kw)) matches++;
  }

  return matches / promptKeywords.length;
}

export interface CheckpointMatch {
  content: string;
  score: number;
  sessionId: string;
  mode: string;
}

export function findBestCheckpoint(
  userPrompt: string,
  checkpoints: Array<{ session_id: string; content: string; mode: string; created_at: number }>,
  threshold: number,
): CheckpointMatch | null {
  let best: CheckpointMatch | null = null;
  let bestScore = 0;

  for (const cp of checkpoints) {
    const score = scoreRelevance(userPrompt, cp.content);
    if (score > bestScore && score >= threshold) {
      bestScore = score;
      best = {
        content: cp.content,
        score,
        sessionId: cp.session_id,
        mode: cp.mode,
      };
    }
  }

  return best;
}
