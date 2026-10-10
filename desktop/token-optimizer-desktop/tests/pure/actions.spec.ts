// The three buttons' pure parts: arming, busy timeouts, notes, the
// Start fresh capture and hand-off, the cross-session pointer strip, and the
// per-request usage reads. tests/engine/actions.test.ts drives the same
// handlers through register.tsx inside the engine.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  BUSY_TIMEOUT_MS,
  busyTimeoutMs,
  CAPTURE_TIMEOUT_MS,
  DASHBOARD_ARGS,
  DASHBOARD_FAILED,
  DASHBOARD_OPENING,
  DASHBOARD_TIMEOUT_MS,
  FRESH_ARM_MS,
  HANDOFF_TTL_MS,
  NOTE_MS,
  canOpenDashboard,
  dashboardFailed,
  dashboardStatus,
  withBusy,
  attachesHandoff,
  handoffFate,
  isStubCheckpoint,
  busyNow,
  checkpointPathFrom,
  initialUi,
  isArmed,
  lifetimeFromUsage,
  noteNow,
  prepareHandoff,
  requestContextTokens,
  stripCrossSessionPointer,
  warmToast,
  withNote,
  type HandoffPort,
} from '../../src/actions.ts'

const T = 1_000_000

test('Start fresh stays armed for 5 seconds and no longer', () => {
  const ui = { ...initialUi(), freshArmedAt: T }
  assert.equal(isArmed(ui, T + FRESH_ARM_MS - 1), true)
  assert.equal(isArmed(ui, T + FRESH_ARM_MS), false)
  assert.equal(isArmed(initialUi(), T), false)
})

test('a busy state lapses after 120 seconds so "Cleaning up." never sticks', () => {
  const ui = { ...initialUi(), busy: 'clean' as const, busySince: T }
  assert.equal(busyNow(ui, T + BUSY_TIMEOUT_MS - 1), 'clean')
  assert.equal(busyNow(ui, T + BUSY_TIMEOUT_MS), null)
  assert.equal(busyNow(initialUi(), T), null)
})

test('only the dashboard\'s busy state outlives 120 seconds; every other button keeps exactly 120', () => {
  assert.equal(BUSY_TIMEOUT_MS, 120_000)
  for (const busy of ['clean', 'fresh-capture', 'fresh-clear'] as const) {
    assert.equal(busyTimeoutMs(busy), BUSY_TIMEOUT_MS)
    const ui = { ...initialUi(), busy, busySince: T }
    assert.equal(busyNow(ui, T + 121_000), null)
  }
  assert.equal(busyTimeoutMs(null), BUSY_TIMEOUT_MS)
  assert.equal(busyTimeoutMs('dashboard'), DASHBOARD_TIMEOUT_MS + 30_000)
  const opening = { ...initialUi(), busy: 'dashboard' as const, busySince: T }
  assert.equal(busyNow(opening, T + 121_000), 'dashboard')
  assert.equal(busyNow(opening, T + DASHBOARD_TIMEOUT_MS + 30_000 - 1), 'dashboard')
  assert.equal(busyNow(opening, T + DASHBOARD_TIMEOUT_MS + 30_000), null)
})

test('a note shows for a few seconds, then the sentence returns to its rule', () => {
  const ui = withNote(initialUi(), 'Clean up skipped: nothing to compact.', T)
  assert.equal(noteNow(ui, T + 1000), 'Clean up skipped: nothing to compact.')
  assert.equal(noteNow(ui, T + 60_000), null)
})

test('the checkpoint path is read from compact-capture output', () => {
  assert.equal(checkpointPathFrom('[Token Optimizer] Checkpoint saved: /h/.claude/token-optimizer/checkpoints/a.md\n'), '/h/.claude/token-optimizer/checkpoints/a.md')
  assert.equal(checkpointPathFrom('noise\n[Token Optimizer] Checkpoint saved: C:\\Users\\me\\cp.md\r\n'), 'C:\\Users\\me\\cp.md')
  assert.equal(checkpointPathFrom(''), null)
  assert.equal(checkpointPathFrom('[Token Optimizer] something else'), null)
})

test('only Token Optimizer cross-session pointer lines are removed from the start context', () => {
  const entries = [
    'Recovered context A',
    '[Token Optimizer] Cross-session checkpoint (abcd1234) about x: /p.md. Not your session\'s work, load only if resuming that session.',
    'line one\n[Token Optimizer] Cross-session checkpoint: /q.md. Not yours.\nline three',
  ]
  assert.deepEqual(stripCrossSessionPointer(entries), ['Recovered context A', 'line one\nline three'])
  assert.equal(stripCrossSessionPointer(undefined), undefined)
})

test('lifetime comes from the per-request cache_creation split when the usage carries it', () => {
  assert.equal(lifetimeFromUsage({ cache_creation: { ephemeral_1h_input_tokens: 500, ephemeral_5m_input_tokens: 0 } }), '1h')
  assert.equal(lifetimeFromUsage({ cache_creation: { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 40 } }), '5m')
  assert.equal(lifetimeFromUsage({ cache_creation: { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 0 } }), null)
  assert.equal(lifetimeFromUsage({ input_tokens: 1 }), null)
  assert.equal(lifetimeFromUsage(null), null)
})

test('context tokens of one request are input plus cache read plus cache write', () => {
  assert.equal(requestContextTokens({ input_tokens: 10, cache_read_input_tokens: 42_000, cache_creation_input_tokens: 600, output_tokens: 99 }), 42_610)
})

test('the warm-up toast says what it did, and what it cost when the cache had lapsed', () => {
  assert.match(warmToast({ ok: true, lapsed: false, contextTokens: 340_000 }), /warm/i)
  assert.equal(
    warmToast({ ok: true, lapsed: false, contextTokens: 340_000, cacheRead: 29_328, lifetimeS: 3600 }),
    'Cache kept warm: re-read 29k tokens from the cache at a tenth of the price. The clock is back to 60m.',
  )
  const lapsed = warmToast({ ok: true, lapsed: true, contextTokens: 340_000 })
  assert.match(lapsed, /340k tokens/)
  assert.match(lapsed, /full price/)
  assert.match(warmToast({ ok: false, reason: 'api-error' }), /api-error/)
})

test('the hand-off joins prompts the person typed, not notifications or peers', () => {
  for (const kind of ['composer', 'sdk', 'bridge', 'unclassified']) assert.equal(attachesHandoff(kind), true, kind)
  for (const kind of ['task-notification', 'peer', 'auto-continuation', 'scheduled-trigger']) assert.equal(attachesHandoff(kind), false, kind)
})

type Calls = { args: string[]; stdin?: string }[]

function port(over: Partial<{ capture: string; file: string | null; lean: string; captureFails: boolean; captureExit: number; leanExit: number }> = {}): { port: HandoffPort; calls: Calls } {
  const o = { capture: '[Token Optimizer] Checkpoint saved: /cp/a.md\n', file: '# Checkpoint\nreal content', lean: 'LEAN BLOCK', captureFails: false, captureExit: 0, leanExit: 0, ...over }
  const calls: Calls = []
  return {
    calls,
    port: {
      run: async (args, stdin) => {
        calls.push({ args, stdin })
        if (args[0] === 'compact-capture') {
          if (o.captureFails) throw new Error('spawn failed')
          return { exitCode: o.captureExit, stdout: o.capture }
        }
        return { exitCode: o.leanExit, stdout: o.lean }
      },
      read: async path => {
        if (o.file === null || path !== '/cp/a.md') throw new Error('ENOENT')
        return o.file
      },
    },
  }
}

test('Start fresh captures with the session on stdin, checks the file, then builds the lean text', async () => {
  const { port: p, calls } = port()
  const r = await prepareHandoff(p, { sessionId: 'old-1', transcriptPath: '/t/old-1.jsonl', cwd: '/work/a', now: T })
  assert.deepEqual(r, { ok: true, handoff: { fromSessionId: 'old-1', cwd: '/work/a', checkpointPath: '/cp/a.md', text: 'LEAN BLOCK', createdAt: T } })
  assert.deepEqual(calls[0], { args: ['compact-capture', '--trigger', 'start-fresh', '--budget-seconds', '25'], stdin: JSON.stringify({ session_id: 'old-1', transcript_path: '/t/old-1.jsonl' }) })
  assert.deepEqual(calls[1]?.args, ['resume-lean', 'old-1', '--print'])
})

test('Start fresh aborts on a failed capture, a missing or stub checkpoint, or an empty resume', async () => {
  const cases = [
    port({ captureFails: true }),
    port({ capture: 'nothing printed' }),
    port({ file: null }),
    port({ file: 'Generated: x | Trigger: start-fresh | Note: No transcript data available\n' }),
    port({ lean: '   \n' }),
    port({ leanExit: 1 }),
  ]
  for (const { port: p } of cases) {
    const r = await prepareHandoff(p, { sessionId: 'old-1', transcriptPath: null, cwd: '/work/a', now: T })
    assert.equal(r.ok, false)
    if (!r.ok) assert.ok(r.reason.length > 0 && !r.reason.includes('\n'))
  }
})

test('a capture that exits non-zero aborts before resume-lean, even with a path printed', async () => {
  const { port: p, calls } = port({ captureExit: 1 })
  const r = await prepareHandoff(p, { sessionId: 'old-1', transcriptPath: null, cwd: '/work/a', now: T })
  assert.deepEqual(r, { ok: false, reason: 'the checkpoint could not be saved' })
  assert.equal(calls.some(c => c.args[0] === 'resume-lean'), false)
})

test('the capture budget it asks for stays under the time Start fresh waits for it (O1)', async () => {
  const { port: p, calls } = port()
  await prepareHandoff(p, { sessionId: 'old-1', transcriptPath: null, cwd: '/work/a', now: T })
  const args = calls[0]?.args ?? []
  const budget = Number(args[args.indexOf('--budget-seconds') + 1])
  assert.equal(budget, 25)
  assert.ok(budget * 1000 < CAPTURE_TIMEOUT_MS)
})

test('a hand-off joins another session in its project that started since the save, within 10 minutes', () => {
  const h = { fromSessionId: 'old-1', cwd: '/work/a', checkpointPath: '/cp/a.md', text: 'LEAN', createdAt: T }
  const at = { sessionId: 'new-1', cwd: '/work/a', startedAt: T + 500, now: T + 1000 }
  assert.equal(handoffFate(h, at), 'attach')
  // Any fresh conversation qualifies: Start fresh's own clear, a typed /clear after it, a new window.
  assert.equal(handoffFate(h, { ...at, sessionId: 'typed-clear-2', startedAt: T + 900 }), 'attach')
  assert.equal(handoffFate(h, { ...at, startedAt: T }), 'attach')
  assert.equal(handoffFate(h, { ...at, now: T + HANDOFF_TTL_MS }), 'attach')
  // Never the session that saved it, one that began before the save, or one whose start is unknown.
  assert.equal(handoffFate(h, { ...at, sessionId: 'old-1' }), 'skip')
  assert.equal(handoffFate(h, { ...at, startedAt: T - 1 }), 'skip')
  assert.equal(handoffFate(h, { ...at, startedAt: null }), 'skip')
  assert.equal(handoffFate(h, { ...at, sessionId: '' }), 'skip')
  // Another project's is skipped, never dropped, even when old.
  assert.equal(handoffFate(h, { ...at, cwd: '/work/b' }), 'skip')
  assert.equal(handoffFate(h, { ...at, cwd: '/work/b', now: T + HANDOFF_TTL_MS + 1 }), 'skip')
  // An expired one in this project is dropped, whoever looks.
  assert.deepEqual(handoffFate(h, { ...at, now: T + HANDOFF_TTL_MS + 1 }), { drop: 'it is more than 10 minutes old' })
  assert.deepEqual(handoffFate(h, { ...at, sessionId: 'old-1', now: T + HANDOFF_TTL_MS + 1 }), { drop: 'it is more than 10 minutes old' })
  assert.equal(HANDOFF_TTL_MS, 10 * 60_000)
})


test('a checkpoint is empty only when it says so and holds almost nothing else', () => {
  assert.equal(isStubCheckpoint('# Checkpoint\n\nNo transcript data available\n'), true)
  const real = '# Checkpoint\n\n' + 'We discussed why "No transcript data available" shows up. '.repeat(1) + 'Edited src/a.ts, src/b.ts; tests pass; next: wire the export path and review the cache logic in detail.'.repeat(3)
  assert.equal(isStubCheckpoint(real), false)
})

test('a hand-off still being stamped is held; one a crash left half-saved is dropped after a minute', () => {
  const h = { fromSessionId: 'old-session-1', cwd: '/w', checkpointPath: '/c.md', text: 't', createdAt: Number.MAX_SAFE_INTEGER, pendingSince: 1_000_000 }
  const at = { sessionId: 'new-session-2', cwd: '/w', startedAt: 1_000_500 }
  assert.equal(handoffFate(h, { ...at, now: 1_000_600 }), 'skip')
  const late = handoffFate(h, { ...at, now: 1_000_000 + 61_000 })
  assert.equal(typeof late === 'object' && 'drop' in late, true)
})

// ---- Full dashboard link ----

test('the dashboard link runs the dashboard command and nothing else, with its own bounded timeout', () => {
  // No --quiet: quiet regenerates without opening the browser.
  // --user: a person clicked, so measure.py must not apply the 20 s hook budget (the runner's stdin is
  // not a tty and it cannot pass env, so without the flag a heavy rebuild is cut off and nothing opens).
  assert.deepEqual([...DASHBOARD_ARGS], ['dashboard', '--user'])
  assert.ok(DASHBOARD_TIMEOUT_MS > 0)
  // Under the dashboard's own busy timeout, so the runner answers before the busy state gives up on its own.
  assert.ok(DASHBOARD_TIMEOUT_MS < busyTimeoutMs('dashboard'))
  assert.equal(DASHBOARD_TIMEOUT_MS, 300_000)
})

test('the dashboard timeout covers a heavy rebuild with margin', () => {
  // Measured: `measure.py dashboard --user` on a real, heavy history, cold (nothing cached): 80 s.
  // (A synthetic 800-session / 1.1 GB history took 38 s, which undersized the first limit.)
  // The timeout keeps at least 3x the real one for a slower disk.
  const MEASURED_COLD_HEAVY_MS = 80_000
  assert.ok(DASHBOARD_TIMEOUT_MS >= 3 * MEASURED_COLD_HEAVY_MS)
})

test('the dashboard link says what it is doing in the band\'s own note words', () => {
  assert.equal(DASHBOARD_OPENING, 'Opening the dashboard.')
  assert.equal(DASHBOARD_FAILED, 'Could not open the dashboard.')
})

test('a second click on the dashboard link is ignored while anything is busy', () => {
  assert.equal(canOpenDashboard(initialUi(), T), true)
  const opening = withBusy(initialUi(), 'dashboard', T)
  assert.equal(canOpenDashboard(opening, T + 1), false)
  assert.equal(canOpenDashboard(withBusy(initialUi(), 'clean', T), T + 1), false)
  // A stale busy state has timed out and no longer blocks: the dashboard's own at its longer limit,
  // any other button's at 120 s.
  assert.equal(canOpenDashboard(opening, T + BUSY_TIMEOUT_MS), false)
  assert.equal(canOpenDashboard(opening, T + busyTimeoutMs('dashboard')), true)
  assert.equal(canOpenDashboard(withBusy(initialUi(), 'clean', T), T + BUSY_TIMEOUT_MS), true)
})

test('the dashboard run failed when it threw, timed out, exited non-zero, or the opener could not open the browser', () => {
  assert.equal(dashboardFailed({ exitCode: 0, stdout: '  Opened: http://localhost:24842/token-optimizer\n' }), false)
  assert.equal(dashboardFailed({ exitCode: 0, stdout: '' }), false)
  assert.equal(dashboardFailed(null), true)
  // A runner answer with no stdout must not throw: the band would sit on "Opening" with no note.
  assert.equal(dashboardFailed({ exitCode: 0 } as unknown as { exitCode: number; stdout: string }), false)
  assert.equal(dashboardFailed({ exitCode: 1, stdout: '' }), true)
  assert.equal(dashboardFailed({ exitCode: 2, stdout: 'usage: measure.py' }), true)
  // measure.py prints this and still exits 0 when xdg-open / open / startfile fails.
  assert.equal(dashboardFailed({ exitCode: 0, stdout: '\n  Could not auto-open browser. Open manually:\n  file:///x/dashboard.html\n' }), true)
})

test('the dashboard status shows while it opens and for the note\'s lifetime after it fails, never otherwise', () => {
  assert.equal(dashboardStatus('dashboard', null), DASHBOARD_OPENING)
  assert.equal(dashboardStatus(null, DASHBOARD_FAILED), DASHBOARD_FAILED)
  assert.equal(dashboardStatus(null, null), null)
  assert.equal(dashboardStatus('clean', null), null)
  assert.equal(dashboardStatus(null, 'Cleaned up.'), null)
  assert.equal(noteNow(withNote(initialUi(), DASHBOARD_FAILED, T), T + NOTE_MS - 1), DASHBOARD_FAILED)
  assert.equal(noteNow(withNote(initialUi(), DASHBOARD_FAILED, T), T + NOTE_MS), null)
})
