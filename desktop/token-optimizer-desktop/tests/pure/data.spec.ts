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
  readStatusBar,
  PYTHON_PROBE,
  PYTHON_PROBE_TIMEOUT_MS,
  PYTHON_PROOF,
  PythonNotFoundError,
  resetLauncher,
  runMeasure,
  shouldReset,
  type DataIo,
} from '../../hooks/data.ts'
import { NO_PYTHON_REASON, prepareHandoff } from '../../src/actions.ts'

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
  savings_headline: {
    usd_30d: 12.3,
    usd_week: null,
    tokens_30d: 2_400_000,
    display: { kind: 'tokens', short: '2.4M tokens kept out', long: '2.4M tokens kept out of context', url: 'http://localhost:24842/token-optimizer#savings' },
  },
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

test('the savings headline lands with the status answer and follows savings while loading', async () => {
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)
  const first = await gather(io, null, { savings: true })
  assert.equal(first.savingsHeadline?.usd30d, 12.3)
  assert.equal(first.savingsHeadline?.display?.kind, 'tokens')

  w.status = 'loading'
  const loading = await gather(io, first, { savings: true })
  assert.deepEqual(loading.savingsHeadline, first.savingsHeadline)

  // An answer without the field (an older Token Optimizer) clears it, like savings.
  const bare = { ...JSON.parse(STATUS) }
  delete bare.savings_headline
  const older = await gather(
    { ...io, run: async (argv, init) => (argv.includes('status-bar') ? { exitCode: 0, stdout: JSON.stringify(bare) } : io.run(argv, init)) },
    first,
    { savings: true },
  )
  assert.equal(older.savingsHeadline, null)
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

test('a compact window from settings or /autocompact, resolved by the status command, sets the context mark', async () => {
  const w = withTokenOptimizer(world())
  const io = fakeIo(w)
  io.usage = async () => ({ context: { tokens: 200_000, window: 1_000_000, percent: 20 } })
  const run = io.run
  let window: number | null = 400_000
  io.run = async (argv, init) => {
    const out = await run(argv, init)
    return argv.includes('status-bar') ? { ...out, stdout: JSON.stringify({ ...JSON.parse(out.stdout), compactWindow: { tokens: window, source: 'setting: autoCompactWindow' } }) } : out
  }

  const first = await gather(io, null, { savings: true })
  assert.equal(first.compactWindow, 400_000)
  assert.equal(first.contextWindow, 400_000)
  assert.equal(first.contextPercent, 50)
  assert.equal(first.contextWindowReduced, true)
  assert.equal(first.contextModelWindow, 1_000_000)

  // A refresh that does not run the status command keeps the resolved window.
  const kept = await gather(io, first)
  assert.equal(kept.contextWindow, 400_000)
  assert.equal(kept.contextPercent, 50)

  // The env override and the setting both apply: the smaller one wins.
  io.envAutoCompactWindow = async () => '250000'
  const both = await gather(io, kept, { savings: true })
  assert.equal(both.contextWindow, 250_000)
  assert.equal(both.contextPercent, 80)

  // The user removes both: the host's own figures come back, and the reduced flag clears.
  io.envAutoCompactWindow = async () => undefined
  window = null
  const cleared = await gather(io, both, { savings: true })
  assert.equal(cleared.compactWindow, null)
  assert.equal(cleared.contextWindow, 1_000_000)
  assert.equal(cleared.contextPercent, 20)
  assert.equal(cleared.contextWindowReduced, false)
  assert.equal(cleared.contextModelWindow, null)
})

test('an older Token Optimizer that reports no compact window changes nothing', async () => {
  const io = fakeIo(withTokenOptimizer(world()))
  io.usage = async () => ({ context: { tokens: 200_000, window: 1_000_000, percent: 20 } })
  const got = await gather(io, null, { savings: true })
  assert.equal(got.compactWindow, null)
  assert.equal(got.contextWindow, 1_000_000)
  assert.equal(got.contextPercent, 20)
})

const WINDOWS_SCRIPTS = 'C:\\Users\\me\\.claude\\skills\\token-optimizer\\scripts'
const STORE_MISSING = 'Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings > Apps > Advanced app settings > App execution aliases.'
// The same notice on a German Windows (#247): the alias speaks the user's language.
const STORE_MISSING_DE = 'Python wurde nicht gefunden; ohne Argumente ausführen, um aus dem Microsoft Store zu installieren, oder deaktivieren Sie diese Verknüpfung unter "Einstellungen > Apps > Erweiterte App-Einstellungen > App-Ausführungsaliase".'

// On a Windows install (#261) every launcher must prove it is a live Python 3
// before it runs measure.py: `py -3` first, then `python`, then `python3`. The
// proof is the probe's printed answer, never an exit code or a message, so a
// WindowsApps Store alias stub is skipped in any language and with any exit.
type Answer = { exitCode: number; stdout: string; stderr?: string } | 'enoent' | 'timeout'
const PROVEN: Answer = { exitCode: 0, stdout: `${PYTHON_PROOF}\r\n` }
const ALIAS_9009: Answer = { exitCode: 9009, stdout: '', stderr: STORE_MISSING }

const isProbe = (argv: readonly string[]) => argv.includes(PYTHON_PROBE)

/** A Windows host: each launcher answers the probe, then measure.py. */
function windowsIo(w: World, launchers: Record<string, { probe: Answer; measure?: Answer }>): DataIo {
  const io = fakeIo(w)
  io.run = async (argv, init) => {
    w.runs.push({ argv, timeoutMs: init.timeoutMs })
    const host = launchers[argv[0] ?? '']
    const answer = host === undefined ? 'enoent' : isProbe(argv) ? host.probe : (host.measure ?? { exitCode: 0, stdout: STATUS })
    if (answer === 'enoent') throw new Error(`spawn ${argv[0]} ENOENT`)
    if (answer === 'timeout') throw new Error('process.run: timed out')
    return answer
  }
  return io
}

const tried = (w: World) => w.runs.map(r => `${r.argv[0]}${isProbe(r.argv) ? ' probe' : ''}`)
const WINDOWS_ROOT = { scriptsDir: WINDOWS_SCRIPTS, runner: null }

test('on Windows py -3 goes first, is proven once, then runs measure.py without another probe', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { py: { probe: PROVEN }, python: { probe: ALIAS_9009 }, python3: { probe: ALIAS_9009 } })
  assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 0)
  assert.deepEqual(tried(w), ['py probe', 'py'])
  assert.deepEqual(w.runs[0]?.argv, ['py', '-3', '-I', '-c', PYTHON_PROBE])
  assert.equal(w.runs[0]?.timeoutMs, PYTHON_PROBE_TIMEOUT_MS)
  assert.deepEqual(w.runs[1]?.argv.slice(0, 3), ['py', '-3', `${WINDOWS_SCRIPTS}/measure.py`])
  w.runs = []
  await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })
  assert.deepEqual(tried(w), ['py'])
  resetLauncher()
})

// The Store alias stub as the reporter met it: python and python3 resolve to
// WindowsApps, whatever exit code arrives and wherever its notice goes.
for (const [name, stub] of [
  ['exit 9009, English notice on stderr', ALIAS_9009],
  ['exit 49 (low byte), German notice', { exitCode: 49, stdout: '', stderr: STORE_MISSING_DE }],
  ['exit 49, notice to the console only', { exitCode: 49, stdout: '' }],
  ['notice printed to stdout', { exitCode: 9009, stdout: STORE_MISSING }],
  ['exit 0 with the notice on stdout', { exitCode: 0, stdout: STORE_MISSING }],
  ['an unknown exit code with nothing printed', { exitCode: 1, stdout: '' }],
] as const) {
  test(`a Store alias stub (${name}) is skipped and the real interpreter is remembered`, async () => {
    resetLauncher()
    const w = world()
    const io = windowsIo(w, { python: { probe: stub }, python3: { probe: PROVEN } })
    assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 0)
    assert.deepEqual(tried(w), ['py probe', 'python probe', 'python3 probe', 'python3'])
    w.runs = []
    await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })
    assert.deepEqual(tried(w), ['python3'])
    resetLauncher()
  })
}

test('a real Store-installed Python (it passes the probe) is used like any other', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { python: { probe: PROVEN } })
  assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 0)
  assert.deepEqual(tried(w), ['py probe', 'python probe', 'python'])
  resetLauncher()
})

test('py -3 with no Python 3 behind it moves on instead of failing Start fresh', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { py: { probe: { exitCode: 103, stdout: '', stderr: 'No suitable Python runtime found' } }, python: { probe: PROVEN } })
  assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 0)
  assert.deepEqual(tried(w), ['py probe', 'python probe', 'python'])
  resetLauncher()
})

test('only Store alias stubs and no py launcher rejects as PythonNotFoundError, and the band says Python is needed', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { python: { probe: ALIAS_9009 }, python3: { probe: { exitCode: 49, stdout: '', stderr: STORE_MISSING_DE } } })
  await assert.rejects(runMeasure(io, WINDOWS_ROOT, ['compact-capture'], { timeoutMs: 1000 }), (error: unknown) => error instanceof PythonNotFoundError && error.name === 'PythonNotFoundError')
  assert.deepEqual(tried(w), ['py probe', 'python probe', 'python3 probe'])
  assert.equal(w.runs.some(r => r.argv.includes('compact-capture')), false)
  w.runs = []
  assert.equal(await readStatusBar(io, WINDOWS_ROOT, 'sess-1'), 'nopython')
  resetLauncher()
})

test('Start fresh on a Windows box with only Store alias stubs says Python 3 is missing', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { python: { probe: ALIAS_9009 }, python3: { probe: ALIAS_9009 } })
  const r = await prepareHandoff(
    { run: (args, stdin) => runMeasure(io, WINDOWS_ROOT, args, { timeoutMs: 1000, ...(stdin === undefined ? {} : { stdin }) }), read: io.read },
    { sessionId: 'sess-1', transcriptPath: null, cwd: '/work/project', now: NOW_MS },
  )
  assert.deepEqual(r, { ok: false, reason: NO_PYTHON_REASON })
  resetLauncher()
})

test('the status bar reads through a localized Store alias', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { python: { probe: { exitCode: 49, stdout: '', stderr: STORE_MISSING_DE } }, python3: { probe: PROVEN } })
  const bar = await readStatusBar(io, WINDOWS_ROOT, 'sess-1')
  assert.ok(bar !== null && bar !== 'outdated' && bar !== 'nopython', `status bar read: ${String(bar)}`)
  assert.equal(bar.savings?.sessionTokens, 41_000)
  resetLauncher()
})

test('a timed-out probe is not retried with another launcher', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { py: { probe: 'timeout' }, python: { probe: PROVEN } })
  await assert.rejects(runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 }), /timed out/)
  assert.deepEqual(tried(w), ['py probe'])
  resetLauncher()
})

test('macOS and Linux keep python3 first and are never probed', async () => {
  resetLauncher()
  const w = withTokenOptimizer(world())
  await gather(fakeIo(w), null, { savings: true })
  assert.equal(w.runs.some(r => isProbe(r.argv)), false)
  assert.equal(w.runs.find(r => r.argv.includes('status-bar'))?.argv[0], 'python3')
  resetLauncher()
})

// Second line of defence: a proven launcher whose measure.py run still comes
// back as the alias (exit 9009, or 49 with nothing printed) moves on.
for (const exitCode of [9009, 49]) {
  test(`after a proof, a measure.py run that exits ${exitCode} with nothing printed still falls through`, async () => {
    resetLauncher()
    const w = world()
    const io = windowsIo(w, {
      python: { probe: PROVEN, measure: { exitCode, stdout: '', stderr: STORE_MISSING } },
      python3: { probe: PROVEN },
    })
    assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 0)
    assert.deepEqual(tried(w), ['py probe', 'python probe', 'python', 'python3 probe', 'python3'])
    resetLauncher()
  })
}

for (const scenario of [
  { name: 'POSIX root', scriptsDir: SCRIPTS, stdout: '', stderr: STORE_MISSING, runs: 1 },
  { name: 'command already produced output', scriptsDir: WINDOWS_SCRIPTS, stdout: STATUS, stderr: STORE_MISSING, runs: 2 },
]) {
  test(`exit 49 is not retried for ${scenario.name}`, async () => {
    resetLauncher()
    const w = world()
    const io = fakeIo(w)
    io.run = async (argv, init) => {
      w.runs.push({ argv, timeoutMs: init.timeoutMs })
      return isProbe(argv) ? PROVEN as { exitCode: number; stdout: string } : { exitCode: 49, stdout: scenario.stdout, stderr: scenario.stderr }
    }
    assert.equal((await runMeasure(io, { scriptsDir: scenario.scriptsDir, runner: null }, ['status-bar'], { timeoutMs: 1000 })).exitCode, 49)
    assert.equal(w.runs.length, scenario.runs)
    resetLauncher()
  })
}

test('a real script exit 305 is not treated as a low-byte missing-Python result', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { py: { probe: PROVEN, measure: { exitCode: 305, stdout: '', stderr: STORE_MISSING } } })
  assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 305)
  assert.deepEqual(tried(w), ['py probe', 'py'])
  resetLauncher()
})

test('py is never treated as an alias: its exit 49 is a real answer', async () => {
  resetLauncher()
  const w = world()
  const io = windowsIo(w, { py: { probe: PROVEN, measure: { exitCode: 49, stdout: '' } }, python: { probe: PROVEN } })
  assert.equal((await runMeasure(io, WINDOWS_ROOT, ['status-bar'], { timeoutMs: 1000 })).exitCode, 49)
  assert.deepEqual(tried(w), ['py probe', 'py'])
  resetLauncher()
})
