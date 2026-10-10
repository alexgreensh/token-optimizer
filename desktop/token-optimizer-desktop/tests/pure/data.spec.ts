// Data gathering and re-keying over a fake engine port: the scenarios
// under Node, so CI covers them. tests/engine/data.test.ts runs the same
// gatherer through register.tsx inside the engine.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  NOT_FOUND,
  findTokenOptimizerRoot,
  STATUS_TIMEOUT_MS,
  gather,
  mergeStored,
  readQuality,
  resetLauncher,
  shouldReset,
  type DataIo,
} from '../../hooks/data.ts'

const HOME = '/home/me'
const NOW_MS = Date.parse('2026-10-03T10:00:00Z')
const LEGACY_DIR = `${HOME}/.claude/token-optimizer`
const DATA_DIR = `${HOME}/.claude/plugins/data/token-optimizer-alexgreensh-token-optimizer/token-optimizer`
const TO_ROOT = `${HOME}/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.26`
const SCRIPTS = `${TO_ROOT}/skills/token-optimizer/scripts`
const RUNNER = `${TO_ROOT}/hooks/module_runner.py`

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
  files: Record<string, [mtimeMs: number, contents: string]>
  dataDirs: string[]
  git: 'branch' | 'no-repo' | 'missing'
  status: 'ok' | 'timeout' | 'loading' | 'noisy' | 'removed'
  runs: { argv: string[]; timeoutMs: number }[]
}

function world(patch: Partial<World> = {}): World {
  return { sessionId: 'sess-1', files: {}, dataDirs: [], git: 'branch', status: 'ok', runs: [], ...patch }
}

function withTokenOptimizer(w: World): World {
  w.files[`${HOME}/.claude/plugins/installed_plugins.json`] = [
    1,
    JSON.stringify({ version: 2, plugins: { 'token-optimizer@alexgreensh-token-optimizer': [{ scope: 'user', installPath: TO_ROOT }] } }),
  ]
  w.files[`${SCRIPTS}/measure.py`] = [1, '#']
  w.files[RUNNER] = [1, '#']

  return w
}

function fakeIo(w: World): DataIo {
  const missing = (path: string) => Promise.reject(new Error(`ENOENT: ${path}`))

  return {
    now: async () => NOW_MS,
    sessionId: async () => w.sessionId,
    cwd: async () => '/work/project',
    envHome: async () => HOME,
    envUserProfile: async () => undefined,
    usage: async () => ({
      startedAt: 0,
      context: { window: 1_000_000, tokens: 620_000, percent: 62 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 80, resetsAt: '2026-10-03T12:00:00Z' },
        { kind: 'seven_day', percentUsed: 20 },
      ],
    }),
    list: async path => {
      if (path !== `${HOME}/.claude/plugins/data`) {
        return missing(path)
      }

      return w.dataDirs.map(name => ({ name }))
    },
    stat: async path => {
      const file = w.files[path]

      return file ? { kind: 'file', mtimeMs: file[0] } : missing(path)
    },
    read: async path => {
      const file = w.files[path]

      return file ? file[1] : missing(path)
    },
    run: async (argv, init) => {
      w.runs.push({ argv, timeoutMs: init.timeoutMs })

      if (argv[0] === 'git') {
        if (w.git === 'missing') {
          throw new Error('spawn git ENOENT')
        }

        return w.git === 'branch' ? { exitCode: 0, stdout: 'feat/band\n' } : { exitCode: 128, stdout: '' }
      }

      if (w.status === 'timeout') {
        throw new Error('process.run: timed out')
      }

      if (w.status === 'noisy') {
        // A stray line printed before the JSON (an interpreter warning, say).
        return { exitCode: 0, stdout: `warning: something chatty\n${STATUS}` }
      }

      if (w.status === 'loading') {
        return { exitCode: 0, stdout: JSON.stringify({ savings: null, savings_state: 'loading', savings_reason: 'computing savings' }) }
      }

      if (w.status === 'removed') {
        // The status command checked the files: retention removed this session's checkpoint.
        return { exitCode: 0, stdout: JSON.stringify({ ...JSON.parse(STATUS), last_checkpoint_epoch: null }) }
      }

      return { exitCode: 0, stdout: STATUS }
    },
  }
}

test('the newer quality cache wins across the two storage directories', async () => {
  const w = world({ dataDirs: ['token-optimizer-alexgreensh-token-optimizer', 'unrelated-plugin'] })
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [2000, quality(40, 0)]
  w.files[`${DATA_DIR}/quality-cache-sess-1.json`] = [5000, quality(81, 3)]
  const io = fakeIo(w)

  const q = await readQuality(io, HOME, 'sess-1')
  assert.equal(q?.score, 81)
  assert.equal(q?.compactions, 3)

  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [9000, quality(40, 0)]
  assert.equal((await readQuality(io, HOME, 'sess-1'))?.score, 40)
})

test('a clear re-keys: the new session starts with nothing of the old one', async () => {
  const w = withTokenOptimizer(world())
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [1, quality(77, 2)]
  const io = fakeIo(w)

  const first = { ...(await gather(io, null, { savings: true })), sheetOpen: true }
  assert.equal(first.sessionId, 'sess-1')
  assert.equal(first.quality?.compactions, 2)
  assert.equal(first.cacheLifetime, '1h')

  // The classic SessionStart (source clear) hands the hook the new id; the
  // status command is slow this time, so nothing new replaces the old facts.
  w.status = 'timeout'
  const fresh = await gather(io, first, { sessionId: 'sess-2', reset: true, savings: true })
  assert.equal(fresh.sessionId, 'sess-2')
  assert.equal(fresh.quality, null)
  assert.equal(fresh.savings, null)
  assert.equal(fresh.checkpointEpoch, null)
  assert.equal(fresh.lastRequestEpoch, null)
  assert.equal(fresh.cacheLifetime, null)
  assert.equal(fresh.sheetOpen, false)
  assert.equal(mergeStored(first, fresh, true).sheetOpen, false)
})

test('a typed clear is caught by the id alone: a stored atom of another session is ignored', async () => {
  const w = withTokenOptimizer(world())
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [1, quality(77, 2)]
  const first = await gather(fakeIo(w), null, { savings: true })

  w.sessionId = 'sess-2'
  w.status = 'timeout'
  const next = await gather(fakeIo(w), { ...first, sheetOpen: true }, { savings: true })
  assert.equal(next.sessionId, 'sess-2')
  assert.equal(next.quality, null)
  assert.equal(next.savings, null)
  assert.equal(next.sheetOpen, false)

  assert.equal(shouldReset(next, 'sess-2'), false)
  assert.equal(shouldReset(next, 'sess-3'), true)
  assert.equal(shouldReset(null, 'sess-2'), true)
})

test('a checkpoint the status command no longer finds is not shown from the quality cache', async () => {
  const w = withTokenOptimizer(world({ status: 'removed' }))
  w.files[`${LEGACY_DIR}/quality-cache-sess-1.json`] = [1, quality(77, 2)]

  const s = await gather(fakeIo(w), null, { savings: true })
  assert.equal(s.checkpointEpoch, null)
  assert.equal(s.quality?.checkpointEpoch, null)

  // Without a status answer the quality cache still fills in.
  w.status = 'timeout'
  const offline = await gather(fakeIo(w), null, { savings: true })
  assert.equal(offline.checkpointEpoch, NOW_MS / 1000 - 600)
})

test('a status command that times out keeps the last savings and does not throw', async () => {
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)

  const first = await gather(io, null, { savings: true })
  assert.equal(first.savings?.sessionTokens, 41_000)
  assert.equal(first.savings?.daily.length, 30)
  assert.equal(first.checkpointEpoch, NOW_MS / 1000 - 120)
  const launched = w.runs.find(run => run.argv.includes('status-bar'))
  assert.deepEqual(launched, {
    argv: ['python3', RUNNER, SCRIPTS, 'measure', 'status-bar', '--session', 'sess-1', '--json'],
    timeoutMs: STATUS_TIMEOUT_MS,
  })

  w.status = 'timeout'
  const second = await gather(io, { ...first, sheetOpen: true }, { savings: true })
  assert.deepEqual(second.savings, first.savings)
  assert.equal(second.savingsState, 'fresh')
  assert.equal(second.cacheLifetime, '1h')
  assert.equal(second.lastRequestEpoch, first.lastRequestEpoch)
  assert.equal(second.contextPercent, 62)
  assert.equal(second.sheetOpen, true)
})

test('while savings load the last known figures stay, marked loading', async () => {
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)
  const first = await gather(io, null, { savings: true })

  w.status = 'loading'
  const next = await gather(io, first, { savings: true })
  assert.deepEqual(next.savings, first.savings)
  assert.equal(next.savingsState, 'loading')
})

test('a gather without the status command keeps savings and spawns nothing but git', async () => {
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)
  const first = await gather(io, null, { savings: true })
  w.runs = []

  const next = await gather(io, first)
  assert.deepEqual(next.savings, first.savings)
  assert.ok(w.runs.every(run => run.argv[0] === 'git'))
})

test('outside a git repository the branch is empty', async () => {
  const w = world({ git: 'no-repo' })
  assert.equal((await gather(fakeIo(w), null)).branch, null)

  w.git = 'missing'
  assert.equal((await gather(fakeIo(w), null)).branch, null)

  w.git = 'branch'
  assert.equal((await gather(fakeIo(w), null)).branch, 'feat/band')
})

test('without Token Optimizer savings and checkpoint are null and everything else fills', async () => {
  const w = world()
  const s = await gather(fakeIo(w), null, { savings: true })

  assert.equal(s.savings, null)
  assert.equal(s.savingsState, 'unavailable')
  assert.equal(s.savingsReason, NOT_FOUND)
  assert.equal(s.checkpointEpoch, null)
  assert.equal(s.contextPercent, 62)
  assert.equal(s.contextTokens, 620_000)
  assert.equal(s.contextWindow, 1_000_000)
  assert.deepEqual(s.fiveHour, { percentUsed: 80, resetsAt: '2026-10-03T12:00:00Z' })
  assert.deepEqual(s.week, { percentUsed: 20, resetsAt: null })
  assert.equal(s.branch, 'feat/band')
  assert.equal(s.gatheredAt, NOW_MS)
  assert.ok(w.runs.every(run => run.argv[0] === 'git'))
})

test('the skill install runs measure.py directly', async () => {
  const w = world()
  w.files[`${HOME}/.claude/skills/token-optimizer/scripts/measure.py`] = [1, '#']
  await gather(fakeIo(w), null, { savings: true, transcript: '/t/s.jsonl' })

  const launched = w.runs.find(run => run.argv.includes('status-bar'))
  assert.deepEqual(launched?.argv, [
    'python3',
    `${HOME}/.claude/skills/token-optimizer/scripts/measure.py`,
    'status-bar',
    '--session',
    'sess-1',
    '--json',
    '--transcript',
    '/t/s.jsonl',
  ])
})

test('without python3 the launcher falls back to python, then py -3, and remembers what worked', async () => {
  resetLauncher()
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)
  const missing = new Set(['python3', 'python'])
  const run = io.run
  io.run = async (argv, init) => {
    if (missing.has(argv[0] ?? '')) {
      w.runs.push({ argv, timeoutMs: init.timeoutMs })
      throw new Error(`spawn ${argv[0]} ENOENT`)
    }
    return run(argv, init)
  }

  const s = await gather(io, null, { savings: true })
  assert.equal(s.savings?.sessionTokens, 41_000)
  const tried = w.runs.filter(r => r.argv.includes('status-bar')).map(r => r.argv.slice(0, 2))
  assert.deepEqual(tried, [['python3', RUNNER], ['python', RUNNER], ['py', '-3']])

  w.runs = []
  await gather(io, s, { savings: true })
  assert.deepEqual(w.runs.filter(r => r.argv.includes('status-bar')).map(r => r.argv[0]), ['py'])
  resetLauncher()
})

test('a timed-out status command is not retried with another launcher', async () => {
  resetLauncher()
  const w = withTokenOptimizer(world({ status: 'timeout' }))
  await gather(fakeIo(w), null, { savings: true })
  assert.equal(w.runs.filter(r => r.argv.includes('status-bar')).length, 1)
})

test('a registry entry outside the Claude folder is never run', async () => {
  const w = world()
  w.files[`${HOME}/.claude/plugins/installed_plugins.json`] = [
    1,
    JSON.stringify({ version: 2, plugins: { 'token-optimizer@x': [{ scope: 'user', installPath: '/tmp/elsewhere/5.13.29' }] } }),
  ]
  w.files['/tmp/elsewhere/5.13.29/skills/token-optimizer/scripts/measure.py'] = [1, '#']
  const s = await gather(fakeIo(w), null, { savings: true })
  assert.equal(w.runs.some(r => r.argv.some(a => a.startsWith('/tmp/elsewhere'))), false)
  assert.equal(s.savingsReason, NOT_FOUND)
})

test('a stray line before the JSON still reads the figures, not "update Token Optimizer"', async () => {
  const w = withTokenOptimizer(world({ status: 'noisy' }))
  const s = await gather(fakeIo(w), null, { savings: true })
  assert.notEqual(s.savings, null)
  assert.equal(s.savingsReason, null)
})

test('a kept limit is dropped once its own renewal has passed, never pinned', async () => {
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)
  const first = await gather(io, null, {})
  // The last known 5-hour figure renewed an hour before now, and usage stops reporting it.
  const stale = { ...first, fiveHour: { percentUsed: 80, resetsAt: new Date(NOW_MS - 3_600_000).toISOString() } }
  const next = await gather({ ...io, usage: async () => ({ rateLimits: [] }) }, stale, {})
  assert.equal(next.fiveHour, null)
  const garbled = { ...first, fiveHour: { percentUsed: 80, resetsAt: 'not a time' } }
  assert.equal((await gather({ ...io, usage: async () => ({ rateLimits: [] }) }, garbled, {})).fiveHour, null)
})

test('on Windows a registry install under the Claude folder is found, whatever the separators', async () => {
  const win = 'C:\\Users\\me'
  const root = `${win}\\.claude\\plugins\\cache\\alexgreensh-token-optimizer\\token-optimizer\\5.13.29`
  const w = world()
  w.files[`${win}/.claude/plugins/installed_plugins.json`] = [1, JSON.stringify({ version: 2, plugins: { 'token-optimizer@x': [{ scope: 'user', installPath: root }] } })]
  w.files[`${root}/skills/token-optimizer/scripts/measure.py`] = [1, '#']
  const io = { ...fakeIo(w), envHome: async () => undefined, envUserProfile: async () => win }
  const found = await findTokenOptimizerRoot(io, win)
  assert.equal(found?.scriptsDir, `${root}/skills/token-optimizer/scripts`)
})

test('shipped inside Token Optimizer, its own scripts are used before any registry entry', async () => {
  const w = world()
  const other = `${HOME}/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.1`
  w.files[`${HOME}/.claude/plugins/installed_plugins.json`] = [1, JSON.stringify({ version: 2, plugins: { 'token-optimizer@x': [{ scope: 'user', installPath: other }] } })]
  w.files[`${other}/skills/token-optimizer/scripts/measure.py`] = [1, '#']
  w.files[`${SCRIPTS}/measure.py`] = [1, '#']
  w.files[RUNNER] = [1, '#']
  const found = await findTokenOptimizerRoot({ ...fakeIo(w), pluginRoot: () => TO_ROOT }, HOME)
  assert.deepEqual(found, { scriptsDir: SCRIPTS, runner: RUNNER })
})

test('loaded on its own from a checkout, the scripts two folders up are used', async () => {
  const w = world()
  const repo = '/work/token-optimizer'
  w.files[`${repo}/skills/token-optimizer/scripts/measure.py`] = [1, '#']
  const found = await findTokenOptimizerRoot({ ...fakeIo(w), pluginRoot: () => `${repo}/desktop/token-optimizer-desktop` }, HOME)
  assert.equal(found?.scriptsDir, `${repo}/skills/token-optimizer/scripts`)
})

test('gather reads the auto-compaction ceiling and re-reads changes each refresh', async () => {
  const io = fakeIo(world())
  io.usage = async () => ({ context: { tokens: 191_100, window: 1_000_000, percent: 19.11 } })
  let ceiling: string | undefined = '480000'
  let reads = 0
  io.envAutoCompactWindow = async () => { reads++; return ceiling }
  const first = await gather(io, null)
  assert.equal(first.contextWindow, 480_000)
  assert.equal(first.contextPercent, 39.8125)
  ceiling = '240000'
  const changed = await gather(io, first)
  assert.equal(changed.contextWindow, 240_000)
  assert.equal(changed.contextPercent, 79.625)
  ceiling = undefined
  const removed = await gather(io, changed)
  assert.equal(removed.contextWindow, 1_000_000)
  assert.equal(removed.contextPercent, 19.11)
  assert.equal(reads, 3)
})

test('gather survives a missing or rejected auto-compaction environment read', async () => {
  const io = fakeIo(world())
  const baseline = await gather(io, null)
  io.envAutoCompactWindow = async () => { throw new Error('environment unavailable') }
  const result = await gather(io, null)
  assert.equal(result.contextWindow, baseline.contextWindow)
  assert.equal(result.contextPercent, baseline.contextPercent)
  assert.deepEqual(result.fiveHour, baseline.fiveHour)
  assert.equal(result.sessionId, baseline.sessionId)
})
