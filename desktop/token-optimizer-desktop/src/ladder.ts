// The sentence ladder: the single most urgent thing to say, and
// at most one button. Pure.
import type { Limit, Mood, Snapshot } from './contracts.ts'
import type { IconName } from './icons.ts'
import { DASHBOARD_OPENING } from './actions.ts'
import { clock, gradeOf, renewalPhrase, tokens, type FormatOptions } from './format.ts'

export type Tone = 'good' | 'caution' | 'bad' | 'cold'

/** A run of text. Text is full ink; only `lose` (bold red) carries colour. `strong` is bold ink. */
export type Run = { text: string; lose?: true; strong?: true }

export type ActionId = 'clean' | 'clean-first' | 'fresh' | 'warm'
export type Action = { id: ActionId; label: string }

export type Sentence = { icon: IconName; tone: Tone; runs: Run[]; action: Action | null }

/** Quality below grade B (score under 70) is worth a sentence. */
export const QUALITY_FLOOR = 70
export const LIMIT_WARN = 90
export const COMPACT_HEAVY = 3

const say = (icon: IconName, tone: Tone, runs: Run[], action: Action | null = null): Sentence => ({ icon, tone, runs, action })
const plain = (text: string): Run => ({ text })

/** The tokens the next message would re-read, as the one bold red run. */
export function loseRun(n: number): Run {
  return { text: `${tokens(n)} tokens`, lose: true }
}

function renews(limit: Limit, now: number, opts: FormatOptions): string {
  const when = renewalPhrase(limit.resetsAt, now, opts)
  return when ? ` Renews ${when}.` : ''
}

function limitSentence(name: string, limit: Limit, now: number, opts: FormatOptions): Sentence {
  const head = limit.percentUsed >= 100 ? `${name} limit reached.` : `${name} limit ${Math.round(limit.percentUsed)}% used.`
  return say('gauge', 'bad', [plain(head + renews(limit, now, opts))])
}

export function sentence(s: Snapshot, opts: FormatOptions = {}): Sentence {
  // What the band is doing right now leads, with no button.
  if (s.busy === 'clean') return say('compact', 'good', [plain('Cleaning up.')])
  if (s.busy === 'fresh-capture') return say('bookmark', 'good', [plain('Saving checkpoint.')])
  if (s.busy === 'fresh-clear') return say('bookmark', 'good', [plain('Clearing.')])
  if (s.busy === 'dashboard') return say('gauge', 'good', [plain(DASHBOARD_OPENING)])
  if (s.busy === 'warming') return say('hourglass', 'good', [plain('Keeping the cache warm.')])
  if (s.freshArmed) {
    return say('bookmark', 'caution', [plain('Start fresh clears this conversation after saving a checkpoint.')], {
      id: 'fresh',
      label: 'Click again to clear',
    })
  }
  if (s.handoffPending) return say('bookmark', 'good', [plain('Checkpoint ready, it joins your first message.')])

  // The ladder, first match wins. Null limits (API users) skip their rules.
  const five = s.fiveHour
  const week = s.week
  if (five && five.percentUsed >= 100) return limitSentence('5-hour', five, s.now, opts)
  if (week && week.percentUsed >= 100) return limitSentence('Weekly', week, s.now, opts)
  if (five && five.percentUsed >= LIMIT_WARN) return limitSentence('5-hour', five, s.now, opts)
  if (week && week.percentUsed >= LIMIT_WARN) return limitSentence('Weekly', week, s.now, opts)

  const q = s.quality
  if (q && q.compactions >= COMPACT_HEAVY) {
    return say('compact', 'bad', [plain(`Compacted ${q.compactions} times. Early detail is mostly gone.`)], {
      id: 'fresh',
      label: 'Start fresh',
    })
  }
  if (q && q.score < QUALITY_FLOOR) {
    return say('slip', 'bad', [plain(`Quality slipped to ${gradeOf(q.score)}.`)], { id: 'clean', label: 'Clean up' })
  }

  // Cache rules hold still while a turn runs: every request refreshes the cache.
  if (!s.working) {
    const c = s.cache
    if (c.state === 'cold') {
      const runs: Run[] =
        c.tokensAtStake != null
          ? [plain('Cache is cold. Next message re-reads '), loseRun(c.tokensAtStake), plain(' at full price.')]
          : [plain('Cache is cold. Next message re-reads the whole context at full price.')]
      runs.push(plain(' Cleaning up first makes later messages cheaper.'))
      return say('cold', 'cold', runs, { id: 'clean-first', label: 'Clean up first' })
    }
    if (c.state === 'warning') {
      const head = c.secondsLeft != null ? `Cache drops in ${clock(c.secondsLeft)}.` : 'Cache drops soon.'
      const runs: Run[] =
        c.tokensAtStake != null
          ? [plain(`${head} Then the next message re-reads `), loseRun(c.tokensAtStake), plain('.')]
          : [plain(`${head} Then the next message re-reads the whole context.`)]
      // Keep warm only on a measured lifetime.
      return say('hourglass', 'caution', runs, c.measured ? { id: 'warm', label: 'Keep warm' } : null)
    }
  }

  return say('check', 'good', [plain(s.note ?? 'All clear.')])
}

/** How healthy the session looks, for Clawd's face. */
export function moodOf(s: Snapshot): Mood {
  const limits = [s.fiveHour, s.week].filter((l): l is Limit => l !== null)
  const q = s.quality
  if (limits.some((l) => l.percentUsed >= LIMIT_WARN) || (q && q.compactions >= COMPACT_HEAVY)) return 'panic'
  if (q && q.score < QUALITY_FLOOR) return 'worried'
  if (!s.working && (s.cache.state === 'cold' || s.cache.state === 'warning')) return 'worried'
  return 'calm'
}
