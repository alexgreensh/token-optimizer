// The slim band: one line with a tiny Clawd, the + button and the five marks,
// its right end decided by the sentence; the user's size kept in $.store, with
// TOKEN_OPTIMIZER_STATUS_BAR_SIZE as the default when nothing is stored.
import { expect, test } from 'claude-code/testing'

import { BAND, START, stub, type World } from './world.ts'

type Finder = { findAll: (q: { type: string; text?: string }) => Promise<{ text: string; props: Record<string, unknown> }[]> }

/** Text elements showing exactly `text` (the kit's string query matches a substring). */
async function exactly(ui: Finder, text: string) {
  return (await ui.findAll({ type: 'Text', text })).filter(t => t.text === text)
}

async function clawd(ui: { findAll: (q: { type: string }) => Promise<{ props: Record<string, unknown> }[]> }) {
  return (await ui.findAll({ type: 'Svg' })).find(s => String(s.props.alt).startsWith('Clawd: '))
}

test('the – button slims the band to one line; + brings the full band back', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  expect((await exactly(ui, 'Token Optimizer')).length).toBeGreaterThan(0)
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('–')

  await ui.press({ key: 'size' })

  // One line: no title, no details arrow, no details row. Clawd at 24 x 19.
  expect(await ui.find({ type: 'Text', text: 'Token Optimizer' })).toBeUndefined()
  expect(await ui.find({ key: 'details' })).toBeUndefined()
  expect(await ui.find({ key: 'row' })).toBeUndefined()
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('+')
  const tiny = await clawd(ui)
  expect(tiny?.props.width).toBe(24)
  expect(tiny?.props.height).toBe(19)

  // The five marks stay, each with its word.
  for (const id of ['quality', 'context', 'cache', 'fiveHour', 'week']) {
    expect(await ui.find({ key: `mark-${id}` }), id).toBeDefined()
  }
  for (const word of ['quality', 'context', 'cache', '5 hours', 'week']) {
    expect((await exactly(ui, word)).length, word).toBeGreaterThan(0)
  }
  // Calm: nothing on the right.
  expect(await ui.find({ key: 'action' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'All clear.' })).toBeUndefined()
  expect(w.store['status-bar-size']).toBe('slim')

  await ui.press({ key: 'size' })

  expect((await exactly(ui, 'Token Optimizer')).length).toBeGreaterThan(0)
  expect(await ui.find({ key: 'details' })).toBeDefined()
  const big = await clawd(ui)
  expect(big?.props.width).toBe(72)
  expect(w.store['status-bar-size']).toBe('full')
})

test('a slim choice survives a new session; a stored one needs no press at all', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'size' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('+')

  // A new session keeps the user's size (it is theirs, not the session's).
  await $.session.start(START)
  await w.clock.settle()
  const ui2 = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui2.find({ key: 'size' }))?.props.label).toBe('+')
  expect(await ui2.find({ type: 'Text', text: 'Token Optimizer' })).toBeUndefined()
})

test('a size stored from a previous run draws slim before any press', async ($, on) => {
  const w = stub(on, { store: { 'status-bar-size': 'slim' } })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('+')
  expect(await ui.find({ type: 'Text', text: 'Token Optimizer' })).toBeUndefined()
})

test('TOKEN_OPTIMIZER_STATUS_BAR_SIZE is the default when nothing is stored', async ($, on) => {
  const w = stub(on, { env: { TOKEN_OPTIMIZER_STATUS_BAR_SIZE: 'slim' } })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('+')
})

test('an env value that is not slim draws the full band', async ($, on) => {
  const w = stub(on, { env: { TOKEN_OPTIMIZER_STATUS_BAR_SIZE: 'wide' } })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('–')
  expect((await exactly(ui, 'Token Optimizer')).length).toBeGreaterThan(0)
})

test('a stored click beats the env default', async ($, on) => {
  const w = stub(on, { env: { TOKEN_OPTIMIZER_STATUS_BAR_SIZE: 'slim' }, store: { 'status-bar-size': 'full' } })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('–')
  expect((await exactly(ui, 'Token Optimizer')).length).toBeGreaterThan(0)
})

test('a press under an env default writes the other size, which wins next run', async ($, on) => {
  const w = stub(on, { env: { TOKEN_OPTIMIZER_STATUS_BAR_SIZE: 'slim' } })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'size' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('–')
  expect(w.store['status-bar-size']).toBe('full')
})

test('a refused write still switches the band; it just does not persist', async ($, on) => {
  const w = stub(on, { storeSetFails: 1 })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'size' })
  expect((await ui.find({ key: 'size' }))?.props.label).toBe('+')
  expect(await ui.find({ type: 'Text', text: 'Token Optimizer' })).toBeUndefined()
  expect(w.store['status-bar-size']).toBeUndefined()
})

test('a details row open when slim is pressed closes, and stays closed back in full', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })
  expect(await ui.find({ key: 'row' })).toBeDefined()

  await ui.press({ key: 'size' })
  expect(await ui.find({ key: 'row' })).toBeUndefined()

  await ui.press({ key: 'size' })
  expect(await ui.find({ key: 'row' })).toBeUndefined()
  expect((await ui.find({ key: 'details' }))?.props.label).toBe('▾')
})

test('a sentence with a button shows its icon and that button on the slim line, nothing else', async ($, on) => {
  const w = stub(on)
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  w.files[key] = [1, JSON.stringify({ ...JSON.parse(w.files[key][1]), resource_health: 60 })]
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'size' })
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Clean up')
  // The sentence itself stays off the line: the button says it.
  expect(await ui.find({ type: 'Text', text: /Quality slipped/ })).toBeUndefined()
})

test('a bad sentence with no button shows its icon and text on the slim line', async ($, on) => {
  const w = stub(on, { fiveHourUsed: 95 })
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'size' })
  expect(await ui.find({ key: 'action' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /5-hour limit 95% used/ })).toBeDefined()
})

test('the slim line keeps the hover cards on its marks', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'size' })
  // The cache card's title text is drawn (hidden until hovered, as in full mode).
  expect(await ui.find({ type: 'Text', text: /^Warm for / })).toBeDefined()
})
