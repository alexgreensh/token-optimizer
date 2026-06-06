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

function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s_.-]/g, " ")
    .split(/\s+/)
    .filter(Boolean);
}

function extractKeywords(text: string): string[] {
  return tokenize(text).filter((w) => w.length > 2 && !STOP_WORDS.has(w));
}

export function scoreRelevance(userPrompt: string, checkpointContent: string): number {
  const promptKeywords = extractKeywords(userPrompt);
  if (promptKeywords.length === 0) return 0;

  // Word-level membership, NOT substring: a keyword "test" must not match
  // "latest"/"protest", and "port" must not match "report"/"support". Substring
  // matching inflated scores and injected unrelated prior sessions.
  const contentTokens = new Set(tokenize(checkpointContent));
  let matches = 0;
  for (const kw of promptKeywords) {
    if (contentTokens.has(kw)) matches++;
  }

  return matches / promptKeywords.length;
}

export interface CheckpointMatch {
  content: string;
  score: number;
  sessionId: string;
  mode: string;
}

function safeSlice(str: string, maxChars: number): string {
  if (str.length <= maxChars) return str;
  let end = maxChars;
  const code = str.charCodeAt(end - 1);
  if (code >= 0xd800 && code <= 0xdbff) end--;
  return str.slice(0, end) + "\n[... truncated]";
}

export function findBestCheckpoint(
  userPrompt: string,
  checkpoints: Array<{ session_id: string; content: string; mode: string; created_at: number }>,
  threshold: number,
  maxChars: number = 2000,
): CheckpointMatch | null {
  let best: CheckpointMatch | null = null;
  let bestScore = 0;

  for (const cp of checkpoints) {
    const score = scoreRelevance(userPrompt, cp.content);
    if (score > bestScore && score >= threshold) {
      bestScore = score;
      best = {
        content: safeSlice(cp.content, maxChars),
        score,
        sessionId: cp.session_id,
        mode: cp.mode,
      };
    }
  }

  return best;
}
