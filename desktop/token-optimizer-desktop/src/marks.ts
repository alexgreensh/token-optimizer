// The five marks, their hover cards and the detail row as plain
// data the drawing layer maps to elements. Pure.
import type { CacheView, Limit, Snapshot } from './contracts.ts'
import type { IconName } from './icons.ts'
import { ago, clock, duration, minutes, gradeOf, relative, renewal, tokens, type FormatOptions } from './format.ts'
import { KEEP_WARM_MARGIN_MS } from './clock.ts'
import { LIMIT_WARN, QUALITY_FLOOR, loseRun, type Action, type Run, type Sentence, type Tone } from './ladder.ts'
import type { BandSize } from './size.ts'

const KEEP_WARM_MARGIN_S = KEEP_WARM_MARGIN_MS / 1000

export type MarkId = 'quality' | 'context' | 'cache' | 'fiveHour' | 'week'
/** 'none' means no data: the drawing layer shows the mark uncoloured. */
export type MarkTone = Tone | 'none'

export type Mark = {
  id: MarkId
  icon: IconName
  /** The figure on the bar, "--" when unavailable. */
  value: string
  label: string
  tone: MarkTone
  /** 0..100 for ring marks (context, cache, limits). */
  ringPercent?: number
  /** The grade letter on the quality mark. */
  badge?: string
  /** Alt text naming the state. */
  alt: string
}

/** A hover card: title, then "What it means:" (`means`) and "What to do:" (`todo`), then action buttons. */
export type Card = { id: MarkId; title: string; means: Run[]; todo: Run[]; actions: Action[] }

export type Fact = { icon: IconName; runs: Run[] }

export type SavingsBlock = {
  state: 'ready' | 'loading' | 'unavailable'
  sessionTokens: number | null
  last30Tokens: number | null
  sessionText: string
  last30Text: string
  /** Raw daily figures, oldest first, today last; empty when unavailable. */
  daily: number[]
  /** Bar heights in percent of the tallest day, at least 8 so a quiet day still shows. */
  bars: number[]
  /** One short reason when unavailable. */
  reason: string | null
}

export type Row = { facts: Fact[]; savings: SavingsBlock }

const clamp = (p: number) => Math.max(0, Math.min(100, Math.round(p)))
const plain = (text: string): Run => ({ text })
const strong = (text: string): Run => ({ text, strong: true })

export function qualityTone(score: number): Tone {
  return score >= 80 ? 'good' : score >= QUALITY_FLOOR ? 'caution' : 'bad'
}
export function fillTone(p: number): Tone {
  return p >= 80 ? 'bad' : p >= 60 ? 'caution' : 'good'
}
export function limitTone(p: number): Tone {
  return p >= LIMIT_WARN ? 'bad' : p >= 75 ? 'caution' : 'good'
}

function estimateNote(c: CacheView): string {
  return c.measured ? 'an estimate' : 'an estimate, lifetime not measured yet'
}

function cacheMark(c: CacheView): Mark {
  const base = { id: 'cache' as const, label: 'cache' }
  const est = estimateNote(c)
  switch (c.state) {
    case 'warm':
    case 'warning': {
      const left = c.secondsLeft ?? 0
      const tone: Tone = c.state === 'warm' ? 'good' : 'caution'
      const word = c.state === 'warm' ? 'warm' : 'about to drop'
      return { ...base, icon: 'hourglass', value: minutes(left), tone, ringPercent: clamp((left / c.lifetime) * 100), alt: `Cache ${word}, ${minutes(left)} left, ${est}` }
    }
    case 'cold':
      return { ...base, icon: 'cold', value: 'cold', tone: 'cold', ringPercent: 0, alt: `Cache cold, ${est}` }
    case 'refreshing':
      return { ...base, icon: 'hourglass', value: minutes(c.lifetime), tone: 'good', ringPercent: 100, alt: 'Cache refreshing while the turn runs' }
    case 'warming':
      return {
        ...base,
        icon: 'hourglass',
        value: c.secondsLeft != null ? minutes(c.secondsLeft) : '--',
        tone: 'good',
        ringPercent: c.secondsLeft != null ? clamp((c.secondsLeft / c.lifetime) * 100) : 0,
        alt: 'Cache warm-up running',
      }
    default:
      return { ...base, icon: 'hourglass', value: '--', tone: 'none', ringPercent: 0, alt: 'Cache clock starts after the first reply' }
  }
}

function limitMark(id: 'fiveHour' | 'week', limit: Limit, now: number, opts: FormatOptions): Mark {
  const p = Math.round(limit.percentUsed)
  const name = id === 'fiveHour' ? '5-hour' : 'Weekly'
  const when = renewal(limit.resetsAt, now, opts)
  return {
    id,
    icon: 'clock',
    value: `${p}%`,
    label: id === 'fiveHour' ? '5 hours' : 'week',
    tone: limitTone(limit.percentUsed),
    ringPercent: clamp(limit.percentUsed),
    alt: `${name} limit ${p}% used` + (when ? `, renews ${when}` : ''),
  }
}

/** The marks under the sentence, in order. Limit marks are omitted when the limit is null. */
export function marks(s: Snapshot, opts: FormatOptions = {}): Mark[] {
  const q = s.quality
  const quality: Mark = q
    ? {
        id: 'quality',
        icon: q.score < QUALITY_FLOOR ? 'slip' : 'check',
        value: String(Math.round(q.score)),
        badge: gradeOf(q.score),
        label: 'quality',
        tone: qualityTone(q.score),
        alt: `Quality grade ${gradeOf(q.score)}, score ${Math.round(q.score)} of 100`,
      }
    : { id: 'quality', icon: 'check', value: '--', badge: '--', label: 'quality', tone: 'none', alt: 'Quality not measured yet' }

  const p = s.contextPercent
  const context: Mark =
    p != null
      ? { id: 'context', icon: 'gauge', value: `${Math.round(p)}%`, label: 'context', tone: fillTone(p), ringPercent: clamp(p), alt: `Context ${Math.round(p)}% full` }
      : { id: 'context', icon: 'gauge', value: '--', label: 'context', tone: 'none', ringPercent: 0, alt: 'Context fill not reported yet' }

  const out = [quality, context, cacheMark(s.cache)]
  if (s.fiveHour) out.push(limitMark('fiveHour', s.fiveHour, s.now, opts))
  if (s.week) out.push(limitMark('week', s.week, s.now, opts))
  return out
}

function cacheCard(c: CacheView): Card {
  const est = ` The clock is ${estimateNote(c)}.`
  // Offered only while a press can still run: not in the last seconds before the cache drops.
  const warm: Action[] = c.measured && (c.secondsLeft ?? 0) > KEEP_WARM_MARGIN_S ? [{ id: 'warm', label: 'Keep warm' }] : []
  const stake = c.tokensAtStake
  const left = minutes(c.secondsLeft ?? 0)
  switch (c.state) {
    case 'warm':
      return {
        id: 'cache',
        title: `Warm for ${left}`,
        means: [plain(`Messages re-read ${stake != null ? `${tokens(stake)} tokens` : 'the context'} at a tenth of the price.${est}`)],
        todo: [plain(warm.length ? `Reply within ${left} or click Keep warm before a break.` : `Reply within ${left} to keep it warm.`)],
        actions: warm,
      }
    case 'warning':
      return {
        id: 'cache',
        title: c.secondsLeft != null ? `Drops in ${left}` : 'Drops soon',
        means: [
          ...(stake != null ? [plain('Then the next message re-reads '), loseRun(stake), plain(` at full price.${est}`)] : [plain(`Then the next message re-reads the whole context at full price.${est}`)]),
        ],
        todo: [plain(warm.length ? 'Click Keep warm now.' : 'Reply now; after the drop, Clean up first makes the next read cheaper.')],
        actions: warm,
      }
    case 'cold':
      return {
        id: 'cache',
        title: 'Cold',
        means: [
          ...(stake != null ? [plain('Next message re-reads '), loseRun(stake), plain(' at full price.')] : [plain('Next message re-reads the whole context at full price.')]),
        ],
        todo: [plain('Clean up first re-reads it once and makes later messages cheaper.')],
        actions: [{ id: 'clean-first', label: 'Clean up first' }],
      }
    case 'refreshing':
      return { id: 'cache', title: 'Refreshing', means: [plain('Every request in this turn keeps the cache warm.')], todo: [plain('Nothing to do.')], actions: [] }
    case 'warming':
      return { id: 'cache', title: 'Keeping warm', means: [plain('A one-line warm-up is running.')], todo: [plain('Nothing to do.')], actions: [] }
    default:
      return { id: 'cache', title: 'Not started', means: [plain('The clock starts after the first reply.')], todo: [plain('Nothing to do.')], actions: [] }
  }
}

function limitCard(id: 'fiveHour' | 'week', limit: Limit, now: number, opts: FormatOptions): Card {
  const p = Math.round(limit.percentUsed)
  const name = id === 'fiveHour' ? '5-hour' : 'Weekly'
  const when = renewal(limit.resetsAt, now, opts)
  const at = limit.resetsAt ? Date.parse(limit.resetsAt) : NaN
  if (!when || !Number.isFinite(at)) {
    return { id, title: `${name} limit ${p}% used`, means: [plain('Renewal time not reported.')], todo: [plain('Nothing to do.')], actions: [] }
  }
  return {
    id,
    title: `Renews ${when}`,
    means: [plain(`${name} limit ${p}% used; resets ${when}, ${relative(at, now)}.`)],
    todo: [plain(p >= LIMIT_WARN ? 'Clean up frees context sooner.' : 'Nothing to do.')],
    actions: [],
  }
}

/** Hover card bodies, one per mark, same order and omissions as `marks`. */
export function cards(s: Snapshot, opts: FormatOptions = {}): Card[] {
  const q = s.quality
  const qualityActions: Action[] = [
    { id: 'clean', label: 'Clean up' },
    { id: 'fresh', label: s.freshArmed ? 'Click again to clear' : 'Start fresh' },
  ]
  const quality: Card = q
    ? {
        id: 'quality',
        title: `Quality ${gradeOf(q.score)} ${Math.round(q.score)}`,
        means: [plain(`How clean Claude's working context is, 0 to 100.${q.drag ? ` Biggest drag: ${q.drag}.` : ' Nothing is dragging it down.'}`)],
        todo: [plain(q.score < QUALITY_FLOOR ? 'Clean up compacts and keeps your direction; Start fresh keeps a checkpoint first.' : 'Nothing to do.')],
        actions: qualityActions,
      }
    : { id: 'quality', title: 'Quality --', means: [plain('The score appears after the first tool call.')], todo: [plain('Nothing to do.')], actions: qualityActions }

  // With a smaller auto-compact ceiling the ring measures distance to compaction, while
  // quality follows the model window, so the card names both and makes no quality claim.
  const model = s.contextModelWindow
  const context: Card =
    s.contextTokens != null && s.contextWindow != null
      ? model != null && model > s.contextWindow
        ? {
            id: 'context',
            title: `${tokens(s.contextTokens)} of ${tokens(s.contextWindow)} before auto-compact`,
            means: [plain(`${Math.round((s.contextTokens / model) * 100)}% of the ${tokens(model)} model window.`)],
            todo: [plain('Near full, auto-compact may lose details; Clean up compacts sooner and keeps your direction.')],
            actions: [],
          }
        : {
            id: 'context',
            title: `${tokens(s.contextTokens)} of ${tokens(s.contextWindow)} tokens`,
            means: [plain('Quality holds best under half full.')],
            todo: [plain('Past 80%, Clean up compacts and keeps your direction.')],
            actions: [],
          }
      : { id: 'context', title: 'Context --', means: [plain('The fill appears after the first reply.')], todo: [plain('Nothing to do.')], actions: [] }

  const out = [quality, context, cacheCard(s.cache)]
  if (s.fiveHour) out.push(limitCard('fiveHour', s.fiveHour, s.now, opts))
  if (s.week) out.push(limitCard('week', s.week, s.now, opts))
  return out
}

export type SavingsPill = { text: string; url: string | null }

/**
 * Sentence plus savings text that must fit beside "Token Optimizer" on the
 * title row; past this the pill drops to `display.short`.
 */
const PILL_FIT_CELLS = 100

/**
 * The green savings chip on the title row. It only joins a calm line: a good
 * tone with no action button always wins, and without `savings_headline`
 * (older installs, nothing worth a number) there is no pill at all.
 */
export function savingsPill(s: Snapshot, say: Sentence, size: BandSize): SavingsPill | null {
  if (say.tone !== 'good' || say.action !== null) return null
  const display = s.savingsHeadline?.display
  if (!display) return null
  const sentenceCells = say.runs.reduce((n, r) => n + r.text.length, 0)
  const long = display.long !== '' && sentenceCells + display.long.length <= PILL_FIT_CELLS ? display.long : ''
  const text = size === 'full' ? long || display.short || display.long : display.short || display.long
  return text === '' ? null : { text, url: display.url }
}

function savingsBlock(s: Snapshot): SavingsBlock {
  const sv = s.savings
  if (!sv) {
    return {
      state: s.savingsLoading ? 'loading' : 'unavailable',
      sessionTokens: null,
      last30Tokens: null,
      sessionText: '--',
      last30Text: '--',
      daily: [],
      bars: [],
      reason: s.savingsLoading ? null : 'Savings appear once Token Optimizer has measured some.',
    }
  }
  const top = Math.max(1, ...sv.daily)
  return {
    state: s.savingsLoading ? 'loading' : 'ready',
    sessionTokens: sv.sessionTokens,
    last30Tokens: sv.last30Tokens,
    sessionText: sv.sessionTokens != null ? tokens(sv.sessionTokens) : '--',
    last30Text: sv.last30Tokens != null ? tokens(sv.last30Tokens) : '--',
    daily: sv.daily,
    bars: sv.daily.map((v) => Math.max(8, Math.round((Math.max(0, v) / top) * 100))),
    reason: null,
  }
}

/**
 * The detail row under Clawd: only what the bar does not show.
 * `s.now` is epoch milliseconds; the quality cache's epochs are seconds.
 */
/** Longer branch names are cut so the unfolded row stays on one line. */
const BRANCH_MAX = 20

export function row(s: Snapshot, _opts: FormatOptions = {}): Row {
  const q = s.quality
  const facts: Fact[] = []
  // One line in the band: every fact in its shortest plain form.
  if (s.branch) facts.push({ icon: 'branch', runs: [strong(s.branch.length > BRANCH_MAX ? `${s.branch.slice(0, BRANCH_MAX - 1)}…` : s.branch)] })
  // The band's own sightings fill in until Token Optimizer's quality file exists (a new session).
  const startS = q?.sessionStartEpoch ?? (s.startedAtMs != null ? s.startedAtMs / 1000 : null)
  if (startS != null) facts.push({ icon: 'clock', runs: [strong(duration(s.now / 1000 - startS))] })
  const tools = Math.max(q?.toolCalls ?? 0, s.toolCallsSeen ?? 0)
  if (q?.toolCalls != null || (s.toolCallsSeen ?? 0) > 0) facts.push({ icon: 'tool', runs: [strong(String(tools)), plain(tools === 1 ? ' tool' : ' tools')] })
  const compacted = Math.max(q?.compactions ?? 0, s.compactionsSeen ?? 0)
  if (compacted > 0) facts.push({ icon: 'compact', runs: [strong(`${compacted}×`), plain(' compacted')] })
  facts.push({
    icon: 'bookmark',
    runs: (s.checkpointEpoch ?? q?.checkpointEpoch) != null
      ? [plain('Checkpoint '), strong(ago((s.checkpointEpoch ?? q!.checkpointEpoch!) * 1000, s.now))]
      : s.earlierCheckpoint
        ? [plain('Earlier checkpoint '), strong(ago(s.earlierCheckpoint.epoch * 1000, s.now))]
        : [plain('No checkpoint yet')],
  })
  return { facts, savings: savingsBlock(s) }
}
