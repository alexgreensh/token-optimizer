// The band as drawn: Clawd, the sentence, five marks, hover cards and
// the unfolding row on desktop; nothing on the terminal or when switched off.
import { expect, test } from 'claude-code/testing'

import { BAND, START, quality, stub, type World } from './world.ts'

/** Sags the session's quality below the floor, so the row's clean-up buttons are called for. */
function sag(w: World): void {
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  w.files[key] = [1, quality(60, 0)]
}

const RED = '#d6453d' // clawd.ts LIGHT.bad

type Finder = { findAll: (q: { type: string; text?: string }) => Promise<{ text: string; props: Record<string, unknown> }[]> }

/** Text elements showing exactly `text` (the kit's string query matches a substring). */
async function exactly(ui: Finder, text: string) {
  return (await ui.findAll({ type: 'Text', text })).filter(t => t.text === text)
}

async function passesOn(mounting: Promise<unknown>): Promise<boolean> {
  try {
    await mounting
    return false
  } catch {
    // Nothing beneath the plugin draws in a test, so a pass-on rejects.
    return true
  }
}

test('desktop draws Clawd, "Token Optimizer", the sentence and five marks', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  const svgs = await ui.findAll({ type: 'Svg' })
  expect(svgs.some(s => String(s.props.alt).startsWith('Clawd: ') && s.props.isInteractive !== true)).toBe(true)
  expect((await exactly(ui, 'Token Optimizer')).length).toBeGreaterThan(0)
  expect((await exactly(ui, 'All clear.')).length).toBeGreaterThan(0)

  for (const id of ['quality', 'context', 'cache', 'fiveHour', 'week']) {
    expect(await ui.find({ key: `mark-${id}` }), id).toBeDefined()
  }
  expect((await exactly(ui, '62%')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '88')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '60m')).length).toBeGreaterThan(0)

  // Every Svg names its state.
  for (const svg of svgs) {
    expect(typeof svg.props.alt === 'string' && (svg.props.alt as string).length > 0).toBe(true)
  }
  // Text is ink: nothing grey, nothing italic.
  for (const t of await ui.findAll({ type: 'Text' })) {
    expect(t.props.dimColor).toBeUndefined()
    expect(t.props.italic).toBeUndefined()
  }
})

test('the terminal draws nothing: its own status line is there', async ($, on) => {
  const w = stub(on)
  await $.session.start({ ...START, surface: 'terminal' })
  expect(await passesOn($.ui.mount({ ...BAND, surface: 'terminal' }))).toBe(true)
})

test('a survey holds the band', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  expect(await passesOn($.ui.mount({ ...BAND, props: { ...BAND.props, hasSurvey: true }, surface: 'desktop' }))).toBe(true)
})

test('TOKEN_OPTIMIZER_STATUS_BAR=0 hides the band on desktop', async ($, on) => {
  const w = stub(on, { env: { TOKEN_OPTIMIZER_STATUS_BAR: '0' } })
  await $.session.start(START)
  await w.clock.settle()
  expect(await passesOn($.ui.mount({ ...BAND, surface: 'desktop' }))).toBe(true)
})

test("the arrow under Clawd unfolds the row, points up while open, and folds it again", async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const arrow = async () => (await ui.find({ key: 'details' }))?.props

  expect((await arrow())?.label).toBe('▾')
  expect(await ui.find({ key: 'row' })).toBeUndefined()

  await ui.press({ key: 'details' })
  expect((await arrow())?.label).toBe('▴')
  expect(await ui.find({ key: 'row' })).toBeDefined()
  expect((await exactly(ui, 'feat/band')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '41k')).length).toBeGreaterThan(0)
  expect(await ui.find({ type: 'Text', text: /^Saved / })).toBeDefined()

  await ui.press({ key: 'details' })
  expect(await ui.find({ key: 'row' })).toBeUndefined()
  expect((await arrow())?.label).toBe('▾')
})

test('a cold cache draws the token count bold red, and Keep warm nowhere', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  const lose = (await exactly(ui, '620k tokens'))[0]
  expect(lose?.props.bold).toBe(true)
  expect(lose?.props.color).toBe(RED)
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Clean up first')
  const labels = (await ui.findAll({ type: 'Button' })).map(b => b.props.label)
  expect(labels.includes('Keep warm')).toBe(false)
  expect((await exactly(ui, 'cold')).length).toBeGreaterThan(0)
})

test('without savings the row shows "--" totals and the reason, and every other fact', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, savings: null, savings_state: 'unavailable', savings_reason: 'Savings database not found.' }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })

  // Only the 30-day total: "saved this session" stays off below 1K.
  // No figures to state: the reason stands alone, never a "Saved -- tokens" line.
  expect(await ui.find({ type: 'Text', text: /^Saved / })).toBeUndefined()
  expect((await exactly(ui, 'Savings database not found.')).length).toBeGreaterThan(0)
  expect((await exactly(ui, 'feat/band')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '1h 0m')).length).toBeGreaterThan(0)
  expect(await ui.find({ type: 'Text', text: /12 tools/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Checkpoint / })).toBeDefined()
})

test("the terminal's dark theme does not darken the desktop: pictures draw light and carry their own dark-mode rule", async ($, on) => {
  const w = stub(on, { theme: 'dark-daltonized' })
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const svgs = await ui.findAll({ type: 'Svg' })
  const clawd = svgs.find(s => String(s.props.alt).startsWith('Clawd: '))
  expect(String(clawd?.props.source)).toContain('#d97757') // LIGHT.skin
  const quality = svgs.find(s => String(s.props.alt).startsWith('Quality'))
  expect(String(quality?.props.source)).toContain('prefers-color-scheme: dark')
})

test('a healthy session offers no row buttons: nothing is called for', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })
  for (const id of ['clean', 'fresh', 'warm']) expect(await ui.find({ key: `row-${id}` }), id).toBeUndefined()
})

test('the unfolded row carries every card action that can run now, with no pointer needed', async ($, on) => {
  const w = stub(on)
  sag(w)
  w.status = { ...w.status, requestAgoS: 3600 - 120 } // two minutes of cache left
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await ui.find({ key: 'row-fresh' })).toBeUndefined()
  await ui.press({ key: 'details' })

  // Never twice: what the sentence's button offers stays off the row.
  const offered = (await ui.find({ key: 'action' }))?.props.label
  if (offered !== 'Clean up' && offered !== 'Clean up first') expect((await ui.find({ key: 'row-clean' }))?.props.label).toBe('Clean up')
  else expect(await ui.find({ key: 'row-clean' })).toBeUndefined()
  expect((await ui.find({ key: 'row-fresh' }))?.props.label).toBe('Start fresh')
  if (offered !== 'Keep warm') expect((await ui.find({ key: 'row-warm' }))?.props.label).toBe('Keep warm')

  await ui.press({ key: 'row-fresh' })
  // Armed, the confirm sits beside the sentence that says what it does (and leaves the row).
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
  expect(await ui.find({ key: 'row-fresh' })).toBeUndefined()
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(true)
})

test('the row offers Keep warm only while it can run: never on a cold cache', async ($, on) => {
  const w = stub(on)
  sag(w)
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })
  expect(await ui.find({ key: 'row-fresh' })).toBeDefined()
  expect(await ui.find({ key: 'row-warm' })).toBeUndefined()
})

test('a new pose fades in over the old one, which stays beneath until the fade is done', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const clawds = async () => (await ui.findAll({ type: 'Svg' })).filter(s => String(s.props.alt).startsWith('Clawd: '))
  expect((await clawds()).length).toBe(1)

  await $.turn.start({ text: 'go', turnId: 't1' })
  const both = await clawds()
  expect(both.length).toBe(2)
  expect(String(both[1]!.props.source)).toContain('attributeName="opacity" from="0" to="1"')

  await w.clock.advance(1000)
  await w.clock.settle()
  expect((await clawds()).length).toBe(1)
})

test('watching, Clawd has a hidden look toward the band, shown while the pointer is anywhere on it', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await w.clock.advance(5000) // past the wake-up
  await w.clock.settle()
  const looks = (await ui.findAll({ type: 'Svg' })).filter(s => s.props.alt === 'Clawd: watching your pointer')
  // One look: the desktop reveals within the band's keyed Box, not across separate hover groups.
  expect(looks.length).toBe(1)
  expect(looks.every(s => !String(s.props.source).includes('attributeName="opacity"'))).toBe(true)
})

test('a compaction the band watched counts at once, before the transcript or Token Optimizer catch up', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await w.clock.settle()
  await $.session.compact({
    trigger: 'manual',
    messages: [{ role: 'user', text: 'Earlier work.', toolUses: [], handle: 'm-1' }],
  } as never)
  await ui.press({ key: 'details' })
  expect(await ui.find({ type: 'Text', text: /1×/ })).toBeDefined()
})

test('a refresh that comes back without the 5-hour limit keeps the mark', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await w.clock.settle()
  expect((await exactly(ui, '5 hours')).length).toBeGreaterThan(0)
  w.dropFiveHour = true
  await w.clock.advance(61_000) // the minute's quality refresh
  await w.clock.settle()
  expect((await exactly(ui, '5 hours')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '40%')).length).toBeGreaterThan(0)
})

test('a new session with no Token Optimizer quality file yet still shows its time and tool calls', async ($, on) => {
  const w = stub(on)
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  delete w.files[key]
  on('tool.call', () => ({ result: { content: 'ok' } }) as never)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await w.clock.settle()
  for (let i = 0; i < 3; i++) {
    try {
      await $.tool.call({ tool: 'Read', input: { file_path: '/work/project/a.ts' } } as never)
    } catch {
      // The count is what is under test, not the tool.
    }
  }
  await ui.press({ key: 'details' })
  await w.clock.settle()
  expect((await exactly(ui, '1h 0m')).length).toBeGreaterThan(0)
  expect(await ui.find({ type: 'Text', text: /^3 tools$|3 tools/ })).toBeDefined()
  // Every mark keeps its word, at any width.
  for (const word of ['quality', 'context', 'cache', '5 hours', 'week']) expect((await exactly(ui, word)).length, word).toBeGreaterThan(0)
})

const turnEnd = { turnId: 't1', answer: '', durationMs: 1000, isAborted: false, reason: 'answer' } as never

test('tool calls are counted without a redraw per call; the count lands when the turn ends', async ($, on) => {
  const w = stub(on)
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  delete w.files[key]
  on('tool.call', () => ({ result: { content: 'ok' } }) as never)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })
  await w.clock.settle()
  await $.turn.start({ text: 'go', turnId: 't1' })
  for (let i = 0; i < 4; i++) {
    try {
      await $.tool.call({ tool: 'Read', input: { file_path: '/work/project/a.ts' } } as never)
    } catch {
      // The count is under test, not the tool.
    }
  }
  // Mid-turn: nothing written yet (a write per call would restart Clawd each time).
  expect(await ui.find({ type: 'Text', text: /4 tools/ })).toBeUndefined()
  await $.turn.complete(turnEnd)
  await w.clock.settle()
  expect(await ui.find({ type: 'Text', text: /4 tools/ })).toBeDefined()
})

test("a compaction Token Optimizer has already counted is not counted again by the band", async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await w.clock.settle()
  // Token Optimizer's PostCompact refresh records it, and the band re-reads it, while the compaction is still settling.
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  w.duringCompact = async () => {
    w.files[key] = [2, quality(88, 1)]
    await w.clock.advance(61_000)
    await w.clock.settle()
  }
  await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Earlier work.', toolUses: [], handle: 'm-1' }] } as never)
  await w.clock.advance(6000)
  await w.clock.settle()
  await ui.press({ key: 'details' })
  await w.clock.settle()
  expect(await ui.find({ type: 'Text', text: /1×/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /2×/ })).toBeUndefined()
})

test('while savings are first measured the row says so, never "Saved -- tokens"', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, savings: null, savings_state: 'loading', savings_reason: null }
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })
  await w.clock.settle()
  expect(await ui.find({ type: 'Text', text: /^Saved / })).toBeUndefined()
  expect((await exactly(ui, 'Measuring savings…')).length).toBeGreaterThan(0)
})

test('a reduced window with unknown tokens never shows the old quality-cache fill', async ($, on) => {
  const w = stub(on, {
    env: { CLAUDE_CODE_AUTO_COMPACT_WINDOW: '480000' },
    context: { window: 1_000_000, percent: 18 },
  })
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  const q = JSON.parse(w.files[key][1])
  w.files[key] = [1, JSON.stringify({ ...q, fill_pct: 18 })]
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await exactly(ui, '18%')).length).toBe(0)
  expect((await ui.findAll({ type: 'Svg' })).some(s => s.props.alt === 'Context fill not reported yet')).toBe(true)
})

test('a rejected session read still permits publication', async ($, on) => {
  const w = stub(on, { env: { CLAUDE_CODE_AUTO_COMPACT_WINDOW: '480000' }, rejectContextReadOnce: true,
    context: { window: 1_000_000, tokens: 191_100, percent: 18 } })
  await $.session.start(START)
  await w.clock.settle()
  const last = w.written.session!.at(-1) as { contextWindow: number; contextPercent: number }
  expect(last.contextWindow).toBe(480_000)
  expect(last.contextPercent).toBe(39.8125)
})

test('a delayed initial session read does not publish after another session starts', async ($, on) => {
  const w = stub(on)
  let release!: () => void
  let entered!: () => void
  const ready = new Promise<void>(resolve => { entered = resolve })
  const gate = new Promise<void>(resolve => { release = resolve })
  let first = true
  w.beforeContextRead = async () => {
    if (first) { first = false; entered(); await gate }
  }
  const oldStart = $.session.start(START)
  await ready
  w.sessionId = 'sess-2'
  const newStart = $.session.start(START)
  for (let i = 0; i < 8; i++) await w.clock.settle()
  release()
  await Promise.all([oldStart, newStart])
  await w.clock.settle()
  expect(w.written.session!.length).toBeGreaterThan(0)
  expect((w.written.session!.at(-1) as { sessionId: string }).sessionId).toBe('sess-2')
  expect(w.written.session!.every(s => (s as { sessionId: string }).sessionId === 'sess-2')).toBe(true)
})
