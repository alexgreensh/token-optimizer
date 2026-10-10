// A session as the band sees it, for the engine tests: every noun the mod
// calls answered from memory beneath the plugin. Not a test file itself.
import type { On } from 'claude-code'
import { mock } from 'claude-code/testing'

export const HOME = '/home/me'
export const NOW_MS = Date.parse('2026-10-03T10:00:00Z')
export const LEGACY_DIR = `${HOME}/.claude/token-optimizer`
export const TO_ROOT = '/home/me/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.26'
export const SCRIPTS = `${TO_ROOT}/skills/token-optimizer/scripts`
export const RUNNER = `${TO_ROOT}/hooks/module_runner.py`

export const BAND = {
  plugin: 'token-optimizer',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 120, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const

/**
 * The kit's settle runs what is due on the mocked clock, but work a timer starts can go on
 * through engine calls (env, fs, state) the mocked clock does not see. Settling a few rounds
 * (each a round trip to the host) lets that work land before a test asserts
 * (Clean up's compact, started on the next tick, finished after the check about 1 run in 9).
 */
function steady<C extends { settle: () => Promise<void> }>(clock: C): C {
  const settle = clock.settle.bind(clock)
  // Each round is a host round trip, which lets engine calls in flight answer.
  const steadySettle = async (): Promise<void> => {
    for (let round = 0; round < 4; round++) await settle()
  }
  // The kit's clock is frozen: copy its members onto a plain object instead of patching it.
  const out: Record<string, unknown> = {}
  const keys = new Set<string>([...Object.keys(clock), ...Object.getOwnPropertyNames(Object.getPrototypeOf(clock) ?? {})])
  for (const key of keys) {
    if (key === 'constructor') continue
    const value = (clock as Record<string, unknown>)[key]
    out[key] = typeof value === 'function' ? (value as (...a: unknown[]) => unknown).bind(clock) : value
  }
  out.settle = steadySettle
  return out as C
}

export const START = { cwd: '/work/project', surface: 'desktop', isInteractive: true } as const

export type Status = {
  savings: { session_tokens: number; total_30d_tokens: number; daily: { date: string; tokens: number }[] } | null
  savings_state: string
  savings_reason: string | null
  /** Seconds before NOW_MS of the last main-thread request; null for none. */
  requestAgoS: number | null
  cache_lifetime: '1h' | '5m' | null
}

export type World = {
  sessionId: string
  /** Environment variables beside HOME. */
  beforeContextRead?: () => Promise<void>
  rejectContextReadOnce?: boolean
  env?: Record<string, string>
  context?: { window: number; tokens?: number; percent: number }
  files: Record<string, [mtimeMs: number, contents: string]>
  status: Status
  /** How the next compact-capture / resume-lean runs answer. */
  capture: 'ok' | 'fail' | 'stub'
  lean: string
  /** How Clean up's /compact answers: a compaction, a refusal, or never. */
  compact: 'ok' | 'skip' | 'hang' | 'refused' | 'said-compacted'
  /** How `$.model.fork()` answers. */
  fork: { read: number } | 'nothing'
  theme: string
  /** Mocked-clock delay before compact-capture answers, and before a fork answers (ms). */
  captureDelayMs: number
  forkDelayMs: number
  /** Mocked-clock delay before the status command answers, with the figures as they stood when it was asked. */
  statusDelayMs: number
  /** How many of the next prompt submissions / turn completions beneath the band reject. */
  submitFails: number
  completeFails: number
  /** Mocked-clock delay before a store read answers: the band is active but has not started yet. */
  storeGetDelayMs: number
  /** Band state (`$.state`, by atom key) as a reload finds it: served while nothing has been written. */
  seed: Record<string, unknown>
  /** Every value the band wrote to each of its atoms, in order (by key). */
  written: Record<string, unknown[]>
  /** How many of the next writes of a held hand-off to `$.state` fail. */
  handoffWriteFails: number
  /** What `$.store` holds at the start. */
  store: Record<string, unknown>
  /** The live session's project. */
  cwd: string
  /** Runs inside a compaction, before it resolves (Token Optimizer's PostCompact landing mid-way). */
  duringCompact?: () => Promise<void>
  /** The engine leaves the 5-hour limit out of its usage (as it can right after a compact). */
  dropFiveHour?: boolean
  /** The 5-hour limit's percentUsed (default 40). */
  fiveHourUsed?: number
  /** When the live session began (`$.session.usage().startedAt`, mocked-clock ms); a clear sets it to its own moment; null when the engine cannot say. */
  startedAt: number | null
  /** How many of the next `$.store.get` / `$.store.delete` calls throw. */
  storeGetFails: number
  storeDeleteFails: number
  /** How many of the next `$.store.set` calls throw. */
  storeSetFails: number
  /** Mocked-clock delay before each `$.store.set` lands (ms). */
  storeSetDelayMs: number
  /** How many of the next writes of the band's UI state hang (10 minutes on the mocked clock). */
  uiWriteHangs: number
  /** What a plugin-run `/clear` does beneath the band before its call resolves (the engine ends the old session inside it). */
  clearBeneath: (() => Promise<void>) | null
  runs: { argv: string[]; stdin?: string }[]
  toasts: string[]
  compacts: number
  forks: number
  commands: string[]
  clock: ReturnType<typeof mock.clock>
}

export const quality = (score: number, compactions: number) =>
  JSON.stringify({
    resource_health: score,
    resource_health_grade: 'B',
    compactions,
    tool_calls: 12,
    last_checkpoint_epoch: NOW_MS / 1000 - 600,
    session_start_ts: NOW_MS / 1000 - 3600,
  })

const DAILY = Array.from({ length: 30 }, (_, i) => ({ date: `2026-09-${String(i + 1).padStart(2, '0')}`, tokens: (i + 1) * 1000 }))

export const SAVED: Status['savings'] = { session_tokens: 41_000, total_30d_tokens: 2_400_000, daily: DAILY }

const CHECKPOINT = '/home/me/.claude/token-optimizer/checkpoints/sess-1-start-fresh.md'

const ok = (stdout: string, exitCode = 0) => ({ value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

/** A desktop session with Token Optimizer installed, a quality cache, a warm measured cache. */
export function stub(on: On, patch: Partial<Omit<World, 'clock' | 'runs' | 'toasts' | 'compacts' | 'forks' | 'commands'>> = {}): World {
  const w: World = {
    sessionId: 'sess-1',
    files: {
      [`${HOME}/.claude/plugins/installed_plugins.json`]: [
        1,
        JSON.stringify({ version: 2, plugins: { 'token-optimizer@alexgreensh-token-optimizer': [{ scope: 'user', installPath: TO_ROOT }] } }),
      ],
      [`${SCRIPTS}/measure.py`]: [1, '#'],
      [RUNNER]: [1, '#'],
      [`${LEGACY_DIR}/quality-cache-sess-1.json`]: [1, quality(88, 0)],
    },
    status: { savings: SAVED, savings_state: 'fresh', savings_reason: null, requestAgoS: 30, cache_lifetime: '1h' },
    capture: 'ok',
    lean: 'LEAN HANDOFF TEXT',
    compact: 'ok',
    fork: { read: 600_000 },
    theme: 'light',
    captureDelayMs: 0,
    forkDelayMs: 0,
    statusDelayMs: 0,
    submitFails: 0,
    completeFails: 0,
    storeGetDelayMs: 0,
    seed: {},
    handoffWriteFails: 0,
    written: {},
    store: {},
    cwd: '/work/project',
    startedAt: NOW_MS - 3_600_000,
    uiWriteHangs: 0,
    storeGetFails: 0,
    storeDeleteFails: 0,
    storeSetFails: 0,
    storeSetDelayMs: 0,
    clearBeneath: null,
    runs: [],
    toasts: [],
    compacts: 0,
    forks: 0,
    commands: [],
    clock: steady(mock.clock(on, { now: NOW_MS })),
    ...patch,
  }
  const missing = (path: string) => new Error(`ENOENT: ${path}`)

  // `$.store` answered from `w.store`, with failures and a slow save on demand.
  on('store.get', async (_, e) => {
    if (w.storeGetDelayMs) await w.clock.sleep(w.storeGetDelayMs)
    if (w.storeGetFails > 0) {
      w.storeGetFails -= 1
      throw new Error('store read failed')
    }
    return { value: w.store[e.key] }
  })
  on('store.set', async (_, e) => {
    if (w.storeSetFails > 0) {
      w.storeSetFails -= 1
      throw new Error('store write failed')
    }
    if (w.storeSetDelayMs) await w.clock.sleep(w.storeSetDelayMs)
    w.store[e.key] = e.value
    return { value: undefined }
  })
  on('store.delete', (_, e) => {
    if (w.storeDeleteFails > 0) {
      w.storeDeleteFails -= 1
      throw new Error('store delete failed')
    }
    delete w.store[e.key]
    return { value: undefined }
  })
  mock.env(on, { HOME, ...w.env })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.SessionStart', () => ({ additionalContext: ['Recovered notes', '[Token Optimizer] Cross-session checkpoint (abcd1234): /p.md. Not your session\'s work.'] }))
  on('session.id', () => ({ value: w.sessionId }))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('session.cwd', () => ({ value: w.cwd }))
  on('session.usage', () => ({
    value: {
      ...(w.startedAt === null ? {} : { startedAt: w.startedAt }),
      context: w.context ?? { window: 1_000_000, tokens: 620_000, percent: 62 },
      rateLimits: [
        ...(w.dropFiveHour ? [] : [{ kind: 'five_hour', percentUsed: w.fiveHourUsed ?? 40, resetsAt: '2026-10-03T12:00:00Z' }]),
        { kind: 'seven_day', percentUsed: 20 },
      ],
      // An engine that reports no start time is a case the band must survive; the types now require it.
    } as never,
  }))
  on('state.get', async (_, e, next) => {
    if (e.key === 'session') {
      if (w.rejectContextReadOnce) { w.rejectContextReadOnce = false; throw new Error('state unavailable') }
      if (w.beforeContextRead) await w.beforeContextRead()
    }
    const held = await next(e)
    const seeded = e.plugin === 'token-optimizer' ? w.seed[e.key] : undefined
    return held.value?.version === 0 && held.value.value === undefined && seeded !== undefined ? { value: { value: seeded, version: 0 } } : held
  })
  on('state.set', async (_, e, next) => {
    if (e.plugin === 'token-optimizer' && e.key === 'ui' && w.uiWriteHangs > 0) {
      w.uiWriteHangs -= 1
      await w.clock.sleep(10 * 60_000)
    }
    if (e.plugin === 'token-optimizer' && e.key === 'handoff' && e.value !== null && w.handoffWriteFails > 0) {
      w.handoffWriteFails -= 1
      return { value: { isSet: false as const, version: 999 } }
    }
    const result = await next(e)
    if (e.plugin === 'token-optimizer' && result.value?.isSet) (w.written[e.key] ??= []).push(e.value)
    return result
  })
  on('config.list', async () => {
    return { value: [{ key: 'theme', label: 'Theme', kind: 'choice', value: w.theme, provider: { plugin: 'engine', tier: 'core' } }] as never }
  })
  on('fs.list', () => ({ value: [] }))
  on('fs.stat', (_, e) => {
    const file = w.files[e.path]
    if (!file) throw missing(e.path)
    return { value: { kind: 'file' as const, size: file[1].length, mtimeMs: file[0], isLink: false } }
  })
  on('fs.read', (_, e) => {
    const file = w.files[e.path]
    if (!file) throw missing(e.path)
    return { value: file[1] }
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => {
    if (w.completeFails > 0) {
      w.completeFails -= 1
      throw new Error('turn.complete failed beneath the band')
    }
    return { text: '' }
  })
  on('prompt.submit', (_, e) => {
    if (w.submitFails > 0) {
      w.submitFails -= 1
      throw new Error('prompt rejected beneath the band')
    }
    return { text: e.text, context: e.context, origin: e.origin }
  })
  on('process.run', async (_, e) => {
    const argv = [...e.argv]
    w.runs.push({ argv, stdin: e.init?.stdin })
    if (argv[0] === 'git') return ok('feat/band\n')
    if (argv.includes('status-bar')) {
      const s = w.status
      const answer = ok(
        JSON.stringify({
          schema: 1,
          savings: s.savings ? { unit: 'tokens', ...s.savings } : null,
          savings_state: s.savings_state,
          savings_reason: s.savings_reason,
          last_request_epoch: s.requestAgoS === null ? null : NOW_MS / 1000 - s.requestAgoS,
          cache_lifetime: s.cache_lifetime,
          last_checkpoint_epoch: NOW_MS / 1000 - 120,
        }),
      )
      if (w.statusDelayMs) await w.clock.sleep(w.statusDelayMs)
      return answer
    }
    if (argv.includes('compact-capture')) {
      if (w.captureDelayMs) await w.clock.sleep(w.captureDelayMs)
      if (w.capture === 'fail') return ok('', 1)
      w.files[CHECKPOINT] = [2, w.capture === 'stub' ? 'Generated: x | Note: No transcript data available\n' : '# Checkpoint\nreal work']
      return ok(`[Token Optimizer] Checkpoint saved: ${CHECKPOINT}\n`)
    }
    if (argv.includes('resume-lean')) return ok(w.lean ? `${w.lean}\n` : '', w.lean ? 0 : 1)
    return ok('', 1)
  })
  on('ui.toast', (_, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('session.compact', async () => {
    w.compacts += 1
    if (w.compact === 'hang') await w.clock.sleep(10 * 60_000)
    if (w.compact === 'skip') return { skip: 'nothing to compact' }
    if (w.duringCompact) await w.duringCompact()
    // A compaction leaves at least its summary (the shape the engine hands hooks).
    return { messages: [{ role: 'user', text: 'Summary of the conversation so far.', toolUses: [], handle: 'summary-1' }] as never }
  })
  on('model.fork', async () => {
    w.forks += 1
    if (w.forkDelayMs) await w.clock.sleep(w.forkDelayMs)
    if (w.fork === 'nothing') return { value: { isAnswered: false, reason: 'nothing-to-fork', usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } as never }
    return {
      value: {
        isAnswered: true,
        text: 'ok',
        usage: { input_tokens: 5, output_tokens: 1, cache_read_input_tokens: w.fork.read, cache_creation_input_tokens: 0 },
      },
    }
  })
  on('command.run', async (_, e) => {
    w.commands.push(e.command)
    // Clean up runs /compact as a command (a headless session refuses $.session.compact()).
    if (e.command === 'compact') {
      w.compacts += 1
      if (w.compact === 'hang') await w.clock.sleep(10 * 60_000)
      if (w.compact === 'skip') throw new Error('nothing to compact')
      // What Claude Code really does when there is too little to compact: it answers, it does not throw.
      if (w.compact === 'refused') return { text: 'Not enough messages to compact.' }
      if (w.compact === 'said-compacted') return { text: 'Compacted (ctrl+o to see full summary)' }
    }
    if (e.command === 'clear' && w.clearBeneath) await w.clearBeneath()
    return { text: '' }
  })

  return w
}
