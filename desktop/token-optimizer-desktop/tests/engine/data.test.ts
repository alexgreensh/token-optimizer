// Data gathering and re-keying inside the engine, through the plugin's
// own hooks module: the engine lets only that module call `$`, so these tests
// raise the events register.tsx wires (session.start, and the classic
// SessionStart with source clear) and read what it stored by stubbing the
// `$.state` noun beneath it. Every other noun the gatherer calls is stubbed
// as a session would answer. tests/pure/data.spec.ts covers the same
// gatherer under Node.
import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const HOME = '/home/me'
const NOW_MS = Date.parse('2026-10-03T10:00:00Z')
const LEGACY_DIR = `${HOME}/.claude/token-optimizer`
const DATA_DIR = `${HOME}/.claude/plugins/data/token-optimizer-alexgreensh-token-optimizer/token-optimizer`
const TO_ROOT = '/home/me/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.26'
const SCRIPTS = `${TO_ROOT}/skills/token-optimizer/scripts`
const RUNNER = `${TO_ROOT}/hooks/module_runner.py`

type Stored = {
  sessionId: string
  quality: { score: number; compactions: number } | null
  savings: { sessionTokens: number | null; daily: number[] } | null
  savingsState: string
  savingsReason: string | null
  checkpointEpoch: number | null
  cacheLifetime: string | null
  lastRequestEpoch: number | null
  contextPercent: number | null
  contextWindow: number | null
  contextTokens: number | null
  fiveHour: unknown
  week: unknown
  branch: string | null
  sheetOpen: boolean
}

const quality = (score: number, compactions: number) =>
  JSON.stringify({ resource_health: score, resource_health_grade: 'B', compactions, tool_calls: 12, last_checkpoint_epoch: NOW_MS / 1000 - 600 })

const STATUS = JSON.stringify({
  schema: 1,
  savings: { unit: 'tokens', session_tokens: 41_000, total_30d_tokens: 2_400_000, daily: [{ date: '2026-10-03', tokens: 900, usd: 0.01 }] },
  savings_state: 'fresh',
  savings_reason: null,
  last_request_epoch: NOW_MS / 1000 - 30,
  cache_lifetime: '1h',
  last_checkpoint_epoch: NOW_MS / 1000 - 120,
})

type World = {
  sessionId: string
  autoCompactWindow?: string
  context?: { window: number; tokens: number; percent: number }
  files: Record<string, [mtimeMs: number, contents: string]>
  dataDirs: string[]
  git: 'branch' | 'no-repo'
  status: 'ok' | 'timeout'
  runs: string[][]
  state: Map<string, { value: unknown; version: number }>
}

const world = (patch: Partial<World> = {}): World => ({
  sessionId: 'sess-1',
  files: {},
  dataDirs: [],
  git: 'branch',
  status: 'ok',
  runs: [],
  state: new Map(),
  ...patch,
})

const withTokenOptimizer = (w: World): World => {
  w.files[`${HOME}/.claude/plugins/installed_plugins.json`] = [
    1,
    JSON.stringify({ version: 2, plugins: { 'token-optimizer@alexgreensh-token-optimizer': [{ scope: 'user', installPath: TO_ROOT }] } }),
  ]
  w.files[`${SCRIPTS}/measure.py`] = [1, '#']
  w.files[RUNNER] = [1, '#']

  return w
}

const stored = (w: World) => (w.state.get('token-optimizer/session')?.value ?? null) as Stored | null

const ok = (stdout: string, exitCode = 0) => ({ value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

function stub(on: On, w: World) {
  const missing = (path: string) => new Error(`ENOENT: ${path}`)

  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.SessionStart', () => ({}))
  // A mocked clock: the status read runs just after a start, on the clock.
  const clock = mock.clock(on, { now: NOW_MS })
  on('session.id', () => ({ value: w.sessionId }))
  on('session.cwd', () => ({ value: '/work/project' }))
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? HOME : e.name === 'CLAUDE_CODE_AUTO_COMPACT_WINDOW' ? w.autoCompactWindow : undefined }))
  on('session.usage', () => ({
    value: {
      startedAt: 0,
      context: w.context ?? { window: 1_000_000, tokens: 620_000, percent: 62 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 80, resetsAt: '2026-10-03T12:00:00Z' },
        { kind: 'seven_day', percentUsed: 20 },
      ],
    },
  }))
  on('fs.list', (_, e) => {
    if (e.path !== `${HOME}/.claude/plugins/data`) {
      throw missing(e.path ?? '')
    }

    return { value: w.dataDirs.map(name => ({ name, kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false })) }
  })
  on('fs.stat', (_, e) => {
    const file = w.files[e.path]

    if (!file) {
      throw missing(e.path)
    }

    return { value: { kind: 'file' as const, size: file[1].length, mtimeMs: file[0], isLink: false } }
  })
  on('fs.read', (_, e) => {
    const file = w.files[e.path]

    if (!file) {
      throw missing(e.path)
    }

    return { value: file[1] }
  })
  on('process.run', (_, e) => {
    w.runs.push([...e.argv])

    if (e.argv[0] === 'git') {
      return w.git === 'branch' ? ok('feat/band\n') : ok('', 128)
    }

    if (w.status === 'timeout') {
      throw new Error('process.run: timed out')
    }

    return ok(STATUS)
  })
  // The session's named values, held in memory as the host would.
  on('state.get', (_, e) => {
    const held = w.state.get(`${e.plugin}/${e.key}`)

    return { value: held ?? { value: undefined, version: 0 } }
  })
  on('state.set', (_, e) => {
    const name = `${e.plugin}/${e.key}`
    const version = (w.state.get(name)?.version ?? 0) + 1
    w.state.set(name, { value: e.value, version })

    return { value: { isSet: true as const, version } }
  })
  return clock
}

const START = { cwd: '/work/project', surface: 'desktop', isInteractive: true } as const

test('the newer quality cache wins across the two storage directories', async ($, on) => {
  const w = world({ dataDirs: ['token-optimizer-alexgreensh-token-optimizer', 'unrelated-plugin'] })
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [2000, quality(40, 0)]
  w.files[`${DATA_DIR}/quality-cache-sess-1.json`] = [5000, quality(81, 3)]
  const clock = stub(on, w)

  await $.session.start(START)
  await clock.settle()
  expect(stored(w)?.quality?.score).toBe(81)
  expect(stored(w)?.quality?.compactions).toBe(3)
})

test('a clear re-keys the atom: the new session starts with nothing of the old one', async ($, on) => {
  const w = withTokenOptimizer(world())
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [1, quality(77, 2)]
  const clock = stub(on, w)

  await $.session.start(START)
  await clock.settle()
  expect(stored(w)?.sessionId).toBe('sess-1')
  expect(stored(w)?.quality?.compactions).toBe(2)
  expect(stored(w)?.cacheLifetime).toBe('1h')

  // No session.start follows a clear; the classic SessionStart carries the new id.
  w.sessionId = 'sess-2'
  w.status = 'timeout'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await clock.settle()

  const fresh = stored(w)
  expect(fresh?.sessionId).toBe('sess-2')
  expect(fresh?.quality).toBeNull()
  expect(fresh?.savings).toBeNull()
  expect(fresh?.checkpointEpoch).toBeNull()
  expect(fresh?.cacheLifetime).toBeNull()
  expect(fresh?.lastRequestEpoch).toBeNull()
  expect(fresh?.sheetOpen).toBe(false)
})

test('a status command that times out keeps the last savings and does not throw', async ($, on) => {
  const w = withTokenOptimizer(world())
  const clock = stub(on, w)

  await $.session.start(START)
  await clock.settle()
  const first = stored(w)
  expect(first?.savings?.sessionTokens).toBe(41_000)
  expect(first?.savings?.daily.length).toBe(30)
  expect(w.runs.find(argv => argv.includes('status-bar'))).toEqual([
    'python3', RUNNER, SCRIPTS, 'measure', 'status-bar', '--session', 'sess-1', '--json',
  ])

  w.status = 'timeout'
  await $.session.start(START)
  await clock.settle()
  expect(stored(w)?.savings).toEqual(first?.savings)
  expect(stored(w)?.cacheLifetime).toBe('1h')
  expect(stored(w)?.contextPercent).toBe(62)
})

test('outside a git repository the branch is empty', async ($, on) => {
  const w = world({ git: 'no-repo' })
  const clock = stub(on, w)

  await $.session.start(START)
  await clock.settle()
  expect(stored(w)?.branch).toBeNull()
})

test('without Token Optimizer savings and checkpoint are null and everything else fills', async ($, on) => {
  const w = world()
  const clock = stub(on, w)

  await $.session.start(START)
  await clock.settle()
  const s = stored(w)
  expect(s?.savings).toBeNull()
  expect(s?.savingsState).toBe('unavailable')
  expect(s?.savingsReason).toBe('Token Optimizer not found')
  expect(s?.checkpointEpoch).toBeNull()
  expect(s?.contextPercent).toBe(62)
  expect(s?.fiveHour).toEqual({ percentUsed: 80, resetsAt: '2026-10-03T12:00:00Z' })
  expect(s?.week).toEqual({ percentUsed: 20, resetsAt: null })
  expect(s?.branch).toBe('feat/band')
  expect(w.runs.every(argv => argv[0] === 'git')).toBe(true)
})

test('two clears in a row: the first clear\'s late read never writes over the second session', async ($, on) => {
  const w = withTokenOptimizer(world())
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [1, quality(77, 2)]
  const clock = stub(on, w)
  await $.session.start(START)
  await clock.settle()
  // Both clears land before either one's deferred status read runs.
  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  w.sessionId = 'sess-3'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-3' } as never)
  await clock.settle()
  expect(stored(w)?.sessionId).toBe('sess-3')
})

test('the engine environment ceiling reaches the stored context window and percent', async ($, on) => {
  const w = world({ autoCompactWindow: '480000', context: { window: 1_000_000, tokens: 191_100, percent: 18 } })
  const clock = stub(on, w)
  await $.session.start({ cwd: '/work/project', surface: 'desktop', isInteractive: true })
  await clock.settle()
  expect(stored(w)?.contextWindow).toBe(480_000)
  expect(stored(w)?.contextPercent).toBe(39.8125)
  expect(stored(w)?.contextTokens).toBe(191_100)
})
