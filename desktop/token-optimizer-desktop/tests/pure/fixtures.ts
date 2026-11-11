// Shared snapshot builder for the view-model specs. Not a spec itself.
import type { Snapshot } from '../../src/contracts.ts'

/** 2026-10-03 13:06:00 in New York (17:06 UTC), a Saturday. */
export const NOW = Date.UTC(2026, 9, 3, 17, 6, 0)
export const TZ = { timeZone: 'America/New_York', locale: 'en-US' }

/** A healthy session on a 1M window: grade S, warm measured cache, low limits. */
export function snap(over: Partial<Snapshot> = {}): Snapshot {
  return {
    now: NOW,
    working: false,
    quality: {
      score: 92,
      grade: 'S',
      drag: '3 oversized tool results, about 4k tokens',
      toolCalls: 86,
      compactions: 0,
      checkpointEpoch: NOW / 1000 - 180,
      sessionStartEpoch: NOW / 1000 - 3900,
    },
    contextPercent: 34,
    contextTokens: 340_000,
    contextWindow: 1_000_000,
    fiveHour: { percentUsed: 41, resetsAt: '2026-10-03T19:20:00Z' },
    week: { percentUsed: 18, resetsAt: '2026-10-08T13:00:00Z' },
    cache: { state: 'warm', secondsLeft: 2483, lifetime: 3600, measured: true, tokensAtStake: 340_000 },
    branch: 'feat/billing-tests',
    savings: { sessionTokens: 640_000, last30Tokens: 48_000_000, daily: Array.from({ length: 30 }, (_, i) => (i + 1) * 1000) },
    savingsHeadline: null,
    savingsLoading: false,
    busy: null,
    note: null,
    handoffPending: false,
    freshArmed: false,
    ...over,
  }
}

export function withQuality(s: Snapshot, q: Partial<NonNullable<Snapshot['quality']>>): Snapshot {
  return { ...s, quality: { ...s.quality!, ...q } }
}
