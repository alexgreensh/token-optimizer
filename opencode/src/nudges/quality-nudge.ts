import type { SessionStore } from "../storage/session-store.js";
import { scoreToGrade } from "../util/grade.js";

const SCORE_DROP_THRESHOLD = 15;
const CRITICAL_THRESHOLD = 60;
const COOLDOWN_MS = 5 * 60 * 1000;
const SESSION_CAP = 3;

export interface NudgeResult {
  shouldNudge: boolean;
  message: string | null;
}

export function checkQualityNudge(
  store: SessionStore,
  currentScore: number,
  previousScore: number | null,
): NudgeResult {
  if (previousScore === null) return { shouldNudge: false, message: null };

  const cache = store.getQualityCache();
  const nudgeCount = cache?.nudge_count ?? 0;
  const lastNudgeTime = cache?.last_nudge_time ?? 0;
  const now = Date.now() / 1000;

  if (nudgeCount >= SESSION_CAP) return { shouldNudge: false, message: null };
  if (now - lastNudgeTime < COOLDOWN_MS / 1000) return { shouldNudge: false, message: null };

  const drop = previousScore - currentScore;
  const crossedCritical = previousScore >= CRITICAL_THRESHOLD && currentScore < CRITICAL_THRESHOLD;

  if (drop > SCORE_DROP_THRESHOLD || crossedCritical) {
    const grade = scoreToGrade(Math.round(currentScore));
    const message = crossedCritical
      ? `[Token Optimizer] Context health dropped below critical threshold: ${Math.round(currentScore)}/100 (${grade}). Consider compacting or starting a fresh session.`
      : `[Token Optimizer] Context health dropped ${Math.round(drop)} points to ${Math.round(currentScore)}/100 (${grade}). Quality is degrading.`;

    return { shouldNudge: true, message };
  }

  return { shouldNudge: false, message: null };
}
