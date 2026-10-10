// Shapes shared by the pure modules. Nothing here imports from 'claude-code':
// register.tsx maps the engine's events and values onto these.

/** Clawd's 14 moments, one pose each. */
export type Pose =
  | 'wake'
  | 'idle'
  | 'think'
  | 'read'
  | 'type'
  | 'lift'
  | 'ask'
  | 'write'
  | 'compact'
  | 'done'
  | 'stop'
  | 'error'
  | 'cold'
  | 'sleep'

/** How healthy the session looks; drives Clawd's eyes and sweat drop. */
export type Mood = 'calm' | 'worried' | 'panic'

/** Where the cache clock stands. */
export type CacheState = 'unknown' | 'refreshing' | 'warm' | 'warning' | 'cold' | 'warming'

/** The cache clock as the view model reads it. */
export type CacheView = {
  state: CacheState
  /** Whole seconds until the cache lapses; null when unknown. */
  secondsLeft: number | null
  /** Lifetime in seconds the clock is counting against (3600 or 300). */
  lifetime: number
  /** True when the lifetime came from a measurement, not the plan default. */
  measured: boolean
  /** Tokens the next message would re-read at full price if the cache lapsed. */
  tokensAtStake: number | null
}

/** A usage limit as `$.session.usage()` reports it. */
export type Limit = { percentUsed: number; resetsAt: string | null }

/** Token Optimizer's quality cache fields the band reads. */
export type Quality = {
  score: number
  grade: string
  drag: string | null
  toolCalls: number | null
  compactions: number
  checkpointEpoch: number | null
  sessionStartEpoch: number | null
  /** Token Optimizer's own context fill, %; known right after a compact, before the next reply. */
  fillPct?: number | null
}

/** What `measure.py status-bar` returns, reduced to what the band shows. */
export type Savings = {
  sessionTokens: number | null
  last30Tokens: number | null
  /** 30 entries, oldest first; today last. */
  daily: number[]
}

/** Everything the view model needs for one frame. */
export type Snapshot = {
  now: number
  working: boolean
  quality: Quality | null
  contextPercent: number | null
  contextTokens: number | null
  contextWindow: number | null
  fiveHour: Limit | null
  week: Limit | null
  cache: CacheView
  branch: string | null
  savings: Savings | null
  savingsLoading: boolean
  busy: 'clean' | 'fresh-capture' | 'fresh-clear' | 'dashboard' | 'warming' | null
  /** One-line outcome shown in place of "All clear." for a few seconds. */
  note: string | null
  /** A Start fresh hand-off waits for the first prompt. */
  handoffPending: boolean
  /** Start fresh is armed and waiting for its second click. */
  freshArmed: boolean
  /** When the session began (ms), from the engine: the row's session time before Token Optimizer reports one. */
  startedAtMs?: number | null
  /** Tool calls and compactions the band watched itself. */
  toolCallsSeen?: number
  compactionsSeen?: number
  /** When this session last saved a checkpoint, any trigger (epoch seconds). */
  checkpointEpoch?: number | null
  /** An earlier session's checkpoint on this work, when this session has none of its own (epoch seconds). */
  earlierCheckpoint?: { epoch: number; about: string | null } | null
}
