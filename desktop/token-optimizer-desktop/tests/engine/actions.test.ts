// The three buttons: Clean up, Start fresh and Keep warm through
// register.tsx inside the engine, every guard on the mocked clock.
import { expect, test, type Engine } from 'claude-code/testing'

import { BAND, NOW_MS, START, stub } from './world.ts'

const sentenceOf = async (ui: { findAll: (q: { type: string }) => Promise<{ text: string }[]> }) =>
  (await ui.findAll({ type: 'Text' })).map(t => t.text)

const has = async (ui: Parameters<typeof sentenceOf>[0], text: string) => (await sentenceOf(ui)).some(t => t.includes(text))

const COMPOSER = { kind: 'composer' } as const

type Live = { sessionId: string; startedAt: number | null; clearBeneath: (() => Promise<void>) | null; clock: { now: () => number; sleep: (ms: number) => Promise<void> } }

let lastStart: unknown
/** A clear lands: the old session ends with reason clear, then the classic SessionStart carries the new id. */
const clearLands = async ($: Engine, w: Live, from: string, to: string) => {
  await $.session.end({ reason: 'clear', sessionId: from, resume: {} } as never)
  w.sessionId = to
  w.startedAt = w.clock.now()
  lastStart = await $.classic.SessionStart({ source: 'clear', session_id: to } as never)
}
/**
 * Start fresh's own clear as the engine runs it: the old session ends inside
 * the band's `command.run` call, the call resolves, then the classic
 * SessionStart arrives on its own (as observed on Claude Code).
 */
const ourClearEnds = ($: Engine, w: Live, from: string, to: string, afterMs = 0) => {
  w.clearBeneath = async () => {
    if (afterMs) await w.clock.sleep(afterMs)
    await $.session.end({ reason: 'clear', sessionId: from, resume: {} } as never)
    w.sessionId = to
    w.startedAt = w.clock.now()
  }
}
const ourClearStarts = async ($: Engine, to: string) => {
  lastStart = await $.classic.SessionStart({ source: 'clear', session_id: to } as never)
}

test('Clean up while a turn runs: no compaction and a one-line note', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  await $.turn.start({ text: 'go', turnId: 't1' })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(0)
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]?.includes('\n')).toBe(false)
  expect(await has(ui, 'Cleaning up.')).toBe(false)
})

test('a skipped compaction returns the sentence to its rule and names the skip', async ($, on) => {
  const w = stub(on, { compact: 'skip' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  expect(await has(ui, 'Cleaning up.')).toBe(false)
  // The test engine names a mock's rejection by itself; the band passes on whatever reason the engine gives.
  const said = w.toasts.find(t => t.startsWith('Clean up skipped: '))
  expect(said).toBeDefined()
  expect(await has(ui, said!)).toBe(true)
})

test('a /compact Claude Code refuses says why instead of "Cleaned up."', async ($, on) => {
  const w = stub(on, { compact: 'refused' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  expect(w.toasts).toEqual(['Clean up skipped: Not enough messages to compact.'])
  expect(await has(ui, 'Clean up skipped: Not enough messages to compact.')).toBe(true)
  expect(await has(ui, 'Cleaned up.')).toBe(false)
})

test('a /compact that prints "Compacted" counts as cleaned up', async ($, on) => {
  const w = stub(on, { compact: 'said-compacted' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.toasts).toEqual([])
  expect(await has(ui, 'Cleaned up.')).toBe(true)
})

test('a compaction that hangs past the timeout no longer says "Cleaning up."', async ($, on) => {
  const w = stub(on, { compact: 'hang' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(await has(ui, 'Cleaning up.')).toBe(true)
  // A second press while busy does not start another compaction.
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)

  await w.clock.advance(120_000)
  expect(await has(ui, 'Cleaning up.')).toBe(false)
})

test('Keep warm 10 s before the deadline is refused', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, requestAgoS: 3590 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Keep warm')
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(0)
  expect(w.toasts.length).toBe(1)
})

test('Keep warm 200 s before the deadline forks once, however often it is pressed while warming', async ($, on) => {
  const w = stub(on, { forkDelayMs: 2000 })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(true)
  await ui.press({ key: 'card-cache-warm' }).catch(() => undefined)
  await w.clock.settle()
  expect(w.forks).toBe(1)

  await w.clock.advance(2000)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(false)
  // The receipt: what was re-read, and the clock re-anchored to the warm-up (an hour from now).
  const receipt = w.toasts.at(-1)!
  expect(receipt).toMatch(/^Cache kept warm: re-read \S+ tokens from the cache at a tenth of the price\. The clock is back to 60m\.$/)
  expect(await has(ui, receipt)).toBe(true)
})

test('a warm-up that reads almost nothing finds the cache lapsed: cold, and the toast states the cost', async ($, on) => {
  const w = stub(on, { fork: { read: 10 } })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
  expect(w.toasts.at(-1)).toMatch(/620k tokens at full price/)
  expect(await has(ui, 'Cache is cold.')).toBe(true)
})

test('Start fresh arms on the first press; after 5 s the next press arms again without acting', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-fresh' })
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
  await w.clock.advance(5001)
  expect(await ui.find({ key: 'action' })).toBeUndefined()

  await ui.press({ key: 'card-quality-fresh' })
  await w.clock.settle()
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
  expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(false)
  expect(w.commands).toEqual([])
})

test('Start fresh confirmed: capture, clear, then the held text joins the first prompt once, with no cross-session pointer', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  await $.classic.UserPromptSubmit({ prompt: 'hi', transcript_path: '/t/sess-1.jsonl', session_id: 'sess-1' } as never).catch(() => undefined)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  ourClearEnds($, w, 'sess-1', 'sess-2')

  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(await has(ui, 'Saving checkpoint.')).toBe(true)
  // Presses while it runs are ignored.
  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'card-quality-fresh' })
  await w.clock.settle()
  expect(w.runs.filter(r => r.argv.includes('compact-capture')).length).toBe(1)

  await w.clock.advance(1000)
  await w.clock.settle()
  const capture = w.runs.find(r => r.argv.includes('compact-capture'))
  expect(capture?.argv.slice(-5)).toEqual(['compact-capture', '--trigger', 'start-fresh', '--budget-seconds', '25'])
  expect(JSON.parse(capture?.stdin ?? '{}').session_id).toBe('sess-1')
  expect(w.runs.find(r => r.argv.includes('resume-lean'))?.argv.slice(-3)).toEqual(['resume-lean', 'sess-1', '--print'])
  expect(w.commands).toEqual(['clear'])

  await ourClearStarts($, 'sess-2')
  const started = lastStart
  const context = (started as { additionalContext?: string[] }).additionalContext ?? []
  expect(context.join('\n').includes('Cross-session checkpoint')).toBe(false)
  expect(context.includes('Recovered notes')).toBe(true)
  expect(await has(ui, 'Checkpoint ready, it joins your first message.')).toBe(true)

  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
  const second = await $.prompt.submit({ text: 'again', wait: false, origin: COMPOSER })
  expect(second.context ?? []).toEqual([])
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
})

test('a failed capture, a stub checkpoint, or an empty resume queues no clear', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  for (const patch of [{ capture: 'fail' as const }, { capture: 'stub' as const }, { capture: 'ok' as const, lean: '' }]) {
    Object.assign(w, patch)
    w.toasts = []
    await ui.press({ key: 'card-quality-fresh' })
    await ui.press({ key: 'action' })
    await w.clock.settle()
    expect(w.commands).toEqual([])
    expect(w.toasts.length).toBe(1)
    expect(await has(ui, 'Saving checkpoint.')).toBe(false)
  }
})

test('a pending hand-off on disk survives a restart and joins the first prompt of a session started since the save', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'HELD FROM BEFORE', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff }, startedAt: NOW_MS - 500 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await has(ui, 'Checkpoint ready, it joins your first message.')).toBe(true)

  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['HELD FROM BEFORE'])
  expect(w.toasts.length).toBe(0)
  const again = await $.prompt.submit({ text: 'more', wait: false, origin: COMPOSER })
  expect(again.context ?? []).toEqual([])
})

type Mount = Awaited<ReturnType<typeof mountBand>>
const mountBand = ($: Engine) => $.ui.mount({ ...BAND, surface: 'desktop' })
const cacheAlt = async (ui: Mount) => (await ui.findAll({ type: 'Svg' })).map(s => String(s.props.alt)).find(alt => alt.startsWith('Cache'))
const turnEnd = (reason: 'answer' | 'error') => ({ turnId: 't1', answer: '', durationMs: 1000, isAborted: false, reason }) as never
const confirmFresh = async (ui: Mount) => {
  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'action' })
}

test('a /clear typed while Start fresh saves its checkpoint queues no second clear', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(await has(ui, 'Saving checkpoint.')).toBe(true)

  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(1000)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  const first = await $.prompt.submit({ text: 'new work', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a turn started while Start fresh saves its checkpoint stops it before the clear', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  await $.turn.start({ text: 'go', turnId: 't1' })
  await w.clock.advance(1000)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  expect(w.toasts).toEqual(['Start fresh waits until the turn finishes.'])
})

test('two presses of Clean up at once start one compaction', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await Promise.all([ui.press({ key: 'card-quality-clean' }), ui.press({ key: 'card-quality-clean' })])
  await w.clock.settle()
  expect(w.compacts).toBe(1)
})

test('a /clear disarms Start fresh: one press in the new session arms again, never clears', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-fresh' })
  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await ui.press({ key: 'card-quality-fresh' })
  await w.clock.settle()
  expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(false)
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
})

for (const [why, held] of [
  ['saved more than 10 minutes ago', { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'TOO OLD', createdAt: NOW_MS - 10 * 60_000 - 1 }],
] as const) {
  test(`a hand-off ${why} is discarded with a one-line note, never joined`, async ($, on) => {
    // A session that started since the save: only the age rules it out.
    const w = stub(on, { store: { handoff: held }, startedAt: NOW_MS - 1 })
    await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
    const ui = await mountBand($)
    expect(await has(ui, 'Checkpoint ready')).toBe(false)
    const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
    expect(first.context ?? []).toEqual([])
    expect(w.toasts.length).toBe(1)
    expect(w.toasts[0]).toBe("Start fresh's saved hand-off was discarded: it is more than 10 minutes old.")
    // Deleted: the next start finds nothing to drop.
    w.sessionId = 'sess-2'
    await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
    expect(w.toasts.length).toBe(1)
  })
}

test('a clear still queued after 120 s keeps the hand-off and says when it will clear', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2', 125_000)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  expect(await has(ui, 'Clearing.')).toBe(true)

  await w.clock.advance(120_000)
  expect(w.toasts.at(-1)).toBe('Start fresh clears when the current turn ends.')
  expect(await has(ui, 'Clearing.')).toBe(false)

  await w.clock.advance(5_000)
  await w.clock.settle()
  await ourClearStarts($, 'sess-2')
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a prompt rejected beneath the band keeps the hand-off for the next one', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'LEAN HANDOFF TEXT', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff }, submitFails: 1, startedAt: NOW_MS - 500 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  await expect($.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })).rejects.toBeDefined()
  const retry = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(retry.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a Keep warm that lands after a clear leaves the new session\'s clock alone', async ($, on) => {
  const w = stub(on, { forkDelayMs: 2000 })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)

  w.sessionId = 'sess-2'
  w.status = { ...w.status, requestAgoS: null, cache_lifetime: null }
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(2000)
  await w.clock.settle()
  expect(await cacheAlt(ui)).toBe('Cache clock starts after the first reply')
  expect(w.toasts).toEqual([])
})

test('a Keep warm fork that never answers gives up after 120 s and can be pressed again', async ($, on) => {
  const w = stub(on, { forkDelayMs: 10 * 60_000 })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(await has(ui, 'Keeping the cache warm.')).toBe(true)

  await w.clock.advance(120_000)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(false)
  expect(w.toasts).toEqual(['Keep warm did not run: the request failed.'])
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Keep warm')
})

test('a hand-off the band cannot hold stops Start fresh before the clear', async ($, on) => {
  const w = stub(on, { handoffWriteFails: 100 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]?.startsWith('Start fresh stopped:')).toBe(true)
  // Nothing was left on disk for a later session to pick up.
  w.sessionId = 'sess-3'
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const first = await $.prompt.submit({ text: 'go', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a turn that fails beneath the band still ends the turn on the band', async ($, on) => {
  const w = stub(on, { completeFails: 1 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  await $.turn.start({ text: 'go', turnId: 't1' })
  await expect($.turn.complete(turnEnd('error'))).rejects.toBeDefined()
  await w.clock.settle()
  // The turn is over: Clean up runs instead of waiting for it.
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.toasts.includes('Clean up waits until the turn finishes.')).toBe(false)
  expect(w.compacts).toBe(1)
})

test('an open question from before a reload closes at the end of the turn', async ($, on) => {
  // A reload finds Clawd asking; the band is live but has not run a pose event yet (a store read is slow).
  const asking = { pose: 'ask', since: NOW_MS, now: NOW_MS, working: true, rawSub: null, rawSince: NOW_MS, sub: null, agents: {}, permissions: 1, questions: 0, compacting: false, cold: false, lastActivity: NOW_MS, until: { wake: 0, done: 0, stop: 0, error: 0 } }
  const w = stub(on, { storeGetDelayMs: 1000, seed: { pose: asking } })
  const starting = $.session.start(START)
  await $.turn.complete(turnEnd('answer'))
  const ui = await mountBand($)
  const clawd = (await ui.findAll({ type: 'Svg' })).map(s => String(s.props.alt)).find(alt => alt.startsWith('Clawd: '))
  expect(clawd).toBeDefined()
  expect(clawd).not.toBe('Clawd: needs you')
  await w.clock.advance(1000)
  await starting
})

test('Keep warm with no recorded context size names the session\'s own size when the cache lapsed', async ($, on) => {
  const w = stub(on, { fork: { read: 0 }, seed: { clock: { anchor: NOW_MS - 3400_000, lifetime: '1h', contextTokens: 0, working: false, warming: false, lapsed: false } } })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
  expect(w.toasts.at(-1)).toMatch(/620k tokens at full price/)
})

test('a new session starts with its own clock, not the last one\'s', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  w.sessionId = 'sess-9'
  w.status = { ...w.status, requestAgoS: null, cache_lifetime: null }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  expect(await cacheAlt(ui)).toBe('Cache clock starts after the first reply')
  const labels = (await ui.findAll({ type: 'Button' })).map(b => b.props.label)
  expect(labels.includes('Keep warm')).toBe(false)
})

test('resuming an older session shows that session\'s own cache clock', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  w.sessionId = 'sess-old'
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  expect((await cacheAlt(ui))?.startsWith('Cache cold')).toBe(true)
})

test('a refresh that started before a clear never brings the old clock into the new session', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  // The savings refresh a few seconds after a turn is still waiting on the status command when /clear lands.
  w.statusDelayMs = 5000
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete(turnEnd('answer'))
  await w.clock.advance(5000)
  w.statusDelayMs = 0
  w.sessionId = 'sess-2'
  w.status = { ...w.status, requestAgoS: null, cache_lifetime: null }
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(5000)
  await w.clock.settle()
  const ui = await mountBand($)
  expect(await cacheAlt(ui)).toBe('Cache clock starts after the first reply')
})

test('a hand-off from another project is left for its owner: not joined, not deleted, no note', async ($, on) => {
  const handoff = { fromSessionId: 'sess-a1', cwd: '/work/other', checkpointPath: '/cp.md', text: 'PROJECT A WORK', createdAt: NOW_MS - 1000 }
  // Project B's session started after the save: only the project rules it out.
  const w = stub(on, { store: { handoff }, startedAt: NOW_MS - 500 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
  const first = await $.prompt.submit({ text: 'project b', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  expect(w.toasts).toEqual([])
  // Project A's session still finds it.
  w.sessionId = 'sess-a2'
  w.cwd = '/work/other'
  await $.session.start({ ...START, cwd: '/work/other' })
  const own = await $.prompt.submit({ text: 'project a', wait: false, origin: COMPOSER })
  expect(own.context).toEqual(['PROJECT A WORK'])
})

test('an expired hand-off from another project is still left for its owner, who drops it with the note', async ($, on) => {
  const handoff = { fromSessionId: 'sess-a1', cwd: '/work/other', checkpointPath: '/cp.md', text: 'PROJECT A WORK', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff }, startedAt: NOW_MS - 500 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  await w.clock.advance(10 * 60_000)
  const later = await $.prompt.submit({ text: 'project b later', wait: false, origin: COMPOSER })
  expect(later.context ?? []).toEqual([])
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  expect(w.toasts).toEqual([])
  w.sessionId = 'sess-a2'
  w.cwd = '/work/other'
  await $.session.start({ ...START, cwd: '/work/other' })
  expect(w.toasts).toEqual(["Start fresh's saved hand-off was discarded: it is more than 10 minutes old."])
})

test('a session that began before the hand-off was saved never takes it, and says nothing', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'NOT FOR AN OLDER SESSION', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff } })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  expect(w.toasts).toEqual([])
  expect(w.store.handoff).toEqual(handoff)
})

test('a /clear typed right after Start fresh: the fresh conversation still gets the hand-off, once', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2')
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  await ourClearStarts($, 'sess-2')
  // The person types /clear before sending anything.
  await w.clock.advance(1000)
  await clearLands($, w, 'sess-2', 'sess-3')
  const context = (lastStart as { additionalContext?: string[] }).additionalContext ?? []
  expect(context.join('\n').includes('Cross-session checkpoint')).toBe(false)
  expect(await has(ui, 'Checkpoint ready, it joins your first message.')).toBe(true)
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
  const second = await $.prompt.submit({ text: 'again', wait: false, origin: COMPOSER })
  expect(second.context ?? []).toEqual([])
  // Another clear after it was taken finds nothing.
  await clearLands($, w, 'sess-3', 'sess-4')
  const third = await $.prompt.submit({ text: 'later', wait: false, origin: COMPOSER })
  expect(third.context ?? []).toEqual([])
})

test('a reload between Start fresh\'s clear and the first prompt still delivers the hand-off', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2')
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  // The mod reloads before the new conversation announces itself: the band starts over.
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  await ourClearStarts($, 'sess-2')
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a lost classic SessionStart still delivers the hand-off on the first prompt', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2')
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  // No classic SessionStart ever arrives for sess-2.
  expect(await has(ui, 'Checkpoint ready, it joins your first message.')).toBe(true)
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
  const second = await $.prompt.submit({ text: 'again', wait: false, origin: COMPOSER })
  expect(second.context ?? []).toEqual([])
})

test('the session that pressed Start fresh never takes its own hand-off while the clear waits', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2', 60_000)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  const typed = await $.prompt.submit({ text: 'one more thing', wait: false, origin: COMPOSER })
  expect(typed.context ?? []).toEqual([])
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
  await w.clock.advance(60_000)
  await ourClearStarts($, 'sess-2')
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a Start fresh that stands down because the session changed says so', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(1000)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  expect(w.toasts).toEqual(['Start fresh stopped: the session changed.'])
})

test('a press that hangs frees the buttons once the busy timeout passes', async ($, on) => {
  const w = stub(on, { uiWriteHangs: 1 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(0)
  await w.clock.advance(120_001)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
})

test('a resume with no session.start shows no old clock and offers no Keep warm, then reads its own', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Keep warm')
  // The person resumes an older session: the id changes, no session.start fires.
  w.sessionId = 'sess-old'
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  const resumed = await mountBand($)
  expect(await cacheAlt(resumed)).toBe('Cache clock starts after the first reply')
  expect((await resumed.findAll({ type: 'Button' })).some(b => b.props.label === 'Keep warm')).toBe(false)
  await w.clock.settle()
  expect((await cacheAlt(resumed))?.startsWith('Cache cold')).toBe(true)
})

test('a warm-up left marked running by a reload is cleared, and Keep warm can run', async ($, on) => {
  const w = stub(on, { seed: { clock: { anchor: NOW_MS - 3400_000, lifetime: '1h', contextTokens: 620_000, working: false, warming: true, lapsed: false } } })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(false)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
})

test('a /clear typed while Start fresh\'s clear waits: the hand-off joins the conversation its own clear creates', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  // The person's /clear runs first; ours then clears the conversation it made.
  w.clearBeneath = async () => {
    await clearLands($, w, 'sess-1', 'sess-u')
    await $.session.end({ reason: 'clear', sessionId: 'sess-u', resume: {} } as never)
    w.sessionId = 'sess-2'
  }
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  await ourClearStarts($, 'sess-2')
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('Clean up while the last compaction still runs past the busy timeout starts no second one', async ($, on) => {
  const w = stub(on, { compact: 'hang' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  await w.clock.advance(120_001)
  w.toasts = []
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  expect(w.toasts).toEqual(['Still finishing the last clean-up.'])
})

test('Clean up still expires at 121 s: "Clean up timed out." and the button is free again', async ($, on) => {
  const w = stub(on, { compact: 'hang' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  w.toasts = []
  await w.clock.advance(121_000)
  await w.clock.settle()
  expect(w.toasts).toEqual(['Clean up timed out.'])
})

test('Start fresh while its last clear is still queued past the busy timeout queues no second clear', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2', 10 * 60_000)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  await w.clock.advance(120_001)
  w.toasts = []
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  expect(w.toasts).toEqual(['Still clearing.'])
})

test('a compaction timed out but still running refuses Start fresh: no capture, no clear (one engine call)', async ($, on) => {
  const w = stub(on, { compact: 'hang' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  await w.clock.advance(120_001)
  w.toasts = []
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands.filter(c => c !== "compact")).toEqual([]) // the hung clean-up is itself a command
  expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(false)
  expect(w.toasts).toEqual(['Still finishing the last clean-up.'])
})

test('Start fresh\'s clear timed out but still running refuses Clean up: no compaction (one engine call)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  ourClearEnds($, w, 'sess-1', 'sess-2', 10 * 60_000)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  await w.clock.advance(120_001)
  w.toasts = []
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(0)
  expect(w.toasts).toEqual(['Still clearing.'])
})

test('two quick presses start one engine call', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-fresh' })
  await Promise.all([ui.press({ key: 'action' }), ui.press({ key: 'card-quality-clean' })])
  await w.clock.settle()
  expect(w.compacts + w.commands.length).toBe(1)
  // Two Clean up presses at once while nothing else runs: still one.
  const before = w.compacts
  await w.clock.advance(120_001)
  await Promise.all([ui.press({ key: 'card-quality-clean' }), ui.press({ key: 'card-quality-clean' })])
  await w.clock.settle()
  expect(w.compacts - before).toBe(1)
})

const POINTER = 'Cross-session checkpoint'
const startContext = () => ((lastStart as { additionalContext?: string[] }).additionalContext ?? []).join('\n')

for (const kind of ['compact', 'clear'] as const) {
  const refusal = kind === 'compact' ? 'Still finishing the last clean-up.' : 'Still clearing.'
  test(`a reload while our ${kind} still runs keeps refusing a second compaction or clear for 10 minutes`, async ($, on) => {
    // The band reloaded while its engine call was in flight: only the persisted record is left.
    const w = stub(on, { seed: { engineCall: { kind, startedAt: NOW_MS - 60_000 } } })
    await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
    const ui = await mountBand($)
    await ui.press({ key: 'card-quality-clean' })
    await w.clock.settle()
    expect(w.compacts).toBe(0)
    expect(w.toasts).toEqual([refusal])
    w.toasts = []
    await confirmFresh(ui)
    await w.clock.settle()
    expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(false)
    expect(w.commands).toEqual([])
    expect(w.toasts).toEqual([refusal])
  })
}

test('a persisted engine call older than 10 minutes is cleared and blocks nothing', async ($, on) => {
  const w = stub(on, { seed: { engineCall: { kind: 'compact', startedAt: NOW_MS - 10 * 60_000 - 1 } } })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  expect(w.toasts.some(t => t.startsWith('Still'))).toBe(false)
})

test('our engine call is recorded before it runs and cleared when it settles', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  expect(w.written.engineCall).toEqual([{ kind: 'compact', startedAt: NOW_MS }, null])
})

test('a store that cannot be read attaches nothing, even with a hand-off mirrored for drawing', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'MIRROR ONLY', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff }, seed: { handoff }, startedAt: NOW_MS - 500, storeGetFails: 1 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  w.storeGetFails = 1
  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a hand-off whose delete fails is not attached and stays held', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'HELD', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff }, startedAt: NOW_MS - 500 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  w.storeDeleteFails = 1
  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  expect(w.store.handoff).toEqual(handoff)
  // Once it can be deleted, it joins the next prompt, once.
  const second = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(second.context).toEqual(['HELD'])
})

test('the hand-off is stamped when its save lands: a session started during the save never takes it', async ($, on) => {
  const w = stub(on, { storeSetDelayMs: 2000 })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  w.clearBeneath = async () => {
    await $.session.end({ reason: 'clear', sessionId: 'sess-1', resume: {} } as never)
    w.sessionId = 'sess-2'
    // This conversation began while the hand-off was still being saved.
    w.startedAt = NOW_MS + 1000
  }
  await confirmFresh(ui)
  await w.clock.settle()
  await w.clock.advance(10_000)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  expect((w.store.handoff as { createdAt: number }).createdAt).toBe(NOW_MS + 2000)
  await ourClearStarts($, 'sess-2')
  expect(startContext().includes(POINTER)).toBe(true)
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  // A conversation started after the save takes it.
  await clearLands($, w, 'sess-2', 'sess-3')
  const next = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(next.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a clear whose new session cannot say when it started shows no "Checkpoint ready", keeps the pointer, attaches nothing', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  w.clearBeneath = async () => {
    await $.session.end({ reason: 'clear', sessionId: 'sess-1', resume: {} } as never)
    w.sessionId = 'sess-2'
    w.startedAt = null
  }
  await confirmFresh(ui)
  await w.clock.settle()
  await w.clock.advance(1000)
  await ourClearStarts($, 'sess-2')
  expect(startContext().includes(POINTER)).toBe(true)
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  // Once the engine can say, the first prompt after takes it.
  w.startedAt = w.clock.now()
  const next = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(next.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a clear uses the new session\'s own start time, not when the event arrived', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await mountBand($)
  w.clearBeneath = async () => {
    await $.session.end({ reason: 'clear', sessionId: 'sess-1', resume: {} } as never)
    w.sessionId = 'sess-2'
    // The engine reports a start before the hand-off was saved.
    w.startedAt = NOW_MS - 1
  }
  await confirmFresh(ui)
  await w.clock.settle()
  await w.clock.advance(1000)
  await ourClearStarts($, 'sess-2')
  expect(startContext().includes(POINTER)).toBe(true)
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
})
