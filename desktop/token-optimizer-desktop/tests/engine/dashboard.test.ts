// The "Full dashboard" link in the unfolded band, both sizes: where it sits, what its click
// runs, the busy guard and the failure note, through register.tsx inside the engine.
import { expect, test } from 'claude-code/testing'

import { BUSY_TIMEOUT_MS, DASHBOARD_TIMEOUT_MS, NOTE_MS } from '../../src/actions.ts'
import { BAND, START, quality, stub, SCRIPTS, type World } from './world.ts'

const OPENING = 'Opening the dashboard.'
const FAILED = 'Could not open the dashboard.'

type Finder = { findAll: (q: { type: string; text?: string }) => Promise<{ text: string; props: Record<string, unknown> }[]> }
const texts = async (ui: Finder) => (await ui.findAll({ type: 'Text' })).map(t => t.text)
const has = async (ui: Finder, text: string) => (await texts(ui)).some(t => t.includes(text))
const dashboardRuns = (w: World) => w.runs.filter(r => r.argv.includes('dashboard'))

/** Sags the session's quality below the floor, so the row offers Clean up. */
function sag(w: World): void {
  const key = Object.keys(w.files).find(k => k.includes('quality-cache-sess-1'))!
  w.files[key] = [1, quality(60, 0)]
}

async function mounted($: Parameters<Parameters<typeof test>[1]>[0], on: Parameters<Parameters<typeof test>[1]>[1], patch: Partial<World> = {}, size: 'full' | 'slim' = 'full') {
  const w = stub(on, patch)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  if (size === 'slim') await ui.press({ key: 'size' })
  return { w, ui }
}

for (const size of ['full', 'slim'] as const) {
  test(`${size}: the folded band has no dashboard link, the unfolded one has a plain "Full dashboard" link`, async ($, on) => {
    const { ui } = await mounted($, on, {}, size)
    expect(await ui.find({ key: 'dashboard' })).toBeUndefined()
    expect(await has(ui, 'Full dashboard')).toBe(false)

    await ui.press({ key: 'details' })
    const link = await ui.find({ key: 'dashboard' })
    expect(link).toBeDefined()
    expect(link?.props.label).toBe('Full dashboard')
    // A link, not a fourth button: drawn without chrome, no variant, no dimmed or new colour.
    expect(link?.props.plain).toBe(true)
    expect(link?.props.variant).toBeUndefined()
    expect(link?.props.dimColor).toBeUndefined()
    expect(link?.props.role).toBeUndefined()

    // Folding it again takes the link away.
    await ui.press({ key: 'details' })
    expect(await ui.find({ key: 'dashboard' })).toBeUndefined()
  })

  test(`${size}: the link ends the unfolded area, after the row's own buttons`, async ($, on) => {
    const w = stub(on)
    sag(w)
    await $.session.start(START)
    await w.clock.settle()
    const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
    if (size === 'slim') await ui.press({ key: 'size' })
    await ui.press({ key: 'details' })
    const keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))
    expect(keys.some(k => k.startsWith('row-'))).toBe(true)
    expect(keys[keys.length - 1]).toBe('dashboard')
  })

  test(`${size}: a click runs measure.py dashboard through the runner with its own bounded timeout, argv only`, async ($, on) => {
    const { w, ui } = await mounted($, on, {}, size)
    await ui.press({ key: 'details' })
    await ui.press({ key: 'dashboard' })
    await w.clock.settle()

    const runs = dashboardRuns(w)
    expect(runs.length).toBe(1)
    const argv = runs[0]!.argv
    // The same launcher the other measure.py calls use: python, the module runner, the scripts folder, then the command.
    expect(argv.slice(-1)).toEqual(['dashboard'])
    expect(argv.some(a => a.endsWith('module_runner.py'))).toBe(true)
    expect(argv).toContain(SCRIPTS)
    expect(argv).not.toContain('--quiet')
    // No shell string: every argument is its own element and none is a command line.
    expect(argv.some(a => /^(sh|bash|cmd|cmd\.exe|powershell)(\.exe)?$/i.test(a) || a === '-c' || a === '/c')).toBe(false)
    expect(argv.filter(a => /\s/.test(a))).toEqual([])
    expect(runs[0]!.timeoutMs).toBe(DASHBOARD_TIMEOUT_MS)
    expect(DASHBOARD_TIMEOUT_MS).toBeLessThan(BUSY_TIMEOUT_MS)

    // Done: nothing left on the note line, no toast, back to the calm sentence.
    expect(w.toasts).toEqual([])
    expect(await has(ui, OPENING)).toBe(false)
    expect(await has(ui, FAILED)).toBe(false)
  })

  test(`${size}: while it runs the note says "Opening the dashboard." and a second click is ignored`, async ($, on) => {
    const { w, ui } = await mounted($, on, { dashboardDelayMs: 5_000 }, size)
    await ui.press({ key: 'details' })
    await ui.press({ key: 'dashboard' })
    await w.clock.advance(1_000)
    expect(await has(ui, OPENING)).toBe(true)

    await ui.press({ key: 'dashboard' })
    await ui.press({ key: 'dashboard' })
    await w.clock.settle()
    expect(dashboardRuns(w).length).toBe(1)

    await w.clock.advance(10_000)
    await w.clock.settle()
    expect(dashboardRuns(w).length).toBe(1)
    expect(await has(ui, OPENING)).toBe(false)
    expect(await has(ui, FAILED)).toBe(false)
    // And it can be opened again afterwards.
    await ui.press({ key: 'dashboard' })
    await w.clock.advance(10_000)
    await w.clock.settle()
    expect(dashboardRuns(w).length).toBe(2)
  })

  for (const how of ['fail', 'browser-fail', 'throws'] as const) {
    test(`${size}: a dashboard that ${how === 'fail' ? 'exits non-zero' : how === 'throws' ? 'cannot run' : 'cannot open the browser'} says "Could not open the dashboard." for NOTE_MS and nothing else`, async ($, on) => {
      const { w, ui } = await mounted($, on, { dashboard: how }, size)
      await ui.press({ key: 'details' })
      await ui.press({ key: 'dashboard' })
      await w.clock.settle()

      expect(await has(ui, FAILED)).toBe(true)
      expect(await has(ui, OPENING)).toBe(false)
      expect(w.toasts).toEqual([])
      expect(w.commands).toEqual([])
      expect(w.compacts).toBe(0)

      await w.clock.advance(NOTE_MS - 1_000)
      expect(await has(ui, FAILED)).toBe(true)
      await w.clock.advance(2_000)
      expect(await has(ui, FAILED)).toBe(false)
      // Free again: a click runs it once more (a runner that cannot start tries each Python launcher, hence "more than before").
      const before = dashboardRuns(w).length
      await ui.press({ key: 'dashboard' })
      await w.clock.settle()
      expect(dashboardRuns(w).length).toBeGreaterThan(before)
    })
  }

  test(`${size}: a dashboard that never answers ends at the busy timeout with the failure note`, async ($, on) => {
    const { w, ui } = await mounted($, on, { dashboard: 'hang' }, size)
    await ui.press({ key: 'details' })
    await ui.press({ key: 'dashboard' })
    await w.clock.advance(1_000)
    expect(await has(ui, OPENING)).toBe(true)
    await w.clock.advance(BUSY_TIMEOUT_MS)
    await w.clock.settle()
    expect(await has(ui, OPENING)).toBe(false)
    expect(await has(ui, FAILED)).toBe(true)
    expect(w.toasts).toEqual([])
    expect(dashboardRuns(w).length).toBe(1)
  })

  test(`${size}: without Token Optimizer's scripts the link fails the same way and runs nothing`, async ($, on) => {
    const w = stub(on)
    delete w.files[`${SCRIPTS}/measure.py`]
    await $.session.start(START)
    await w.clock.settle()
    const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
    if (size === 'slim') await ui.press({ key: 'size' })
    await ui.press({ key: 'details' })
    await ui.press({ key: 'dashboard' })
    await w.clock.settle()
    expect(dashboardRuns(w).length).toBe(0)
    expect(await has(ui, FAILED)).toBe(true)
  })

  test(`${size}: another button's work blocks the link, and the dashboard's blocks theirs`, async ($, on) => {
    const w = stub(on, { dashboardDelayMs: 5_000 })
    sag(w)
    await $.session.start(START)
    await w.clock.settle()
    const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
    if (size === 'slim') await ui.press({ key: 'size' })
    await ui.press({ key: 'details' })

    await ui.press({ key: 'dashboard' })
    await w.clock.advance(1_000)
    // Clean up is ignored while the dashboard opens.
    const clean = (await ui.findAll({ type: 'Button' })).find(b => String(b.props.label) === 'Clean up')
    if (clean) await ui.press({ key: String(clean.props.key) })
    await w.clock.advance(10_000)
    await w.clock.settle()
    expect(w.compacts).toBe(0)
    expect(dashboardRuns(w).length).toBe(1)
  })
}

test('full: the sentence line carries the status; slim has no sentence line, so the link shows it beside itself', async ($, on) => {
  const { w, ui } = await mounted($, on, { dashboardDelayMs: 5_000 }, 'slim')
  await ui.press({ key: 'details' })
  expect(await ui.find({ key: 'dashboard-status' })).toBeUndefined()
  await ui.press({ key: 'dashboard' })
  await w.clock.advance(1_000)
  expect((await ui.find({ key: 'dashboard-status' }))?.props).toBeDefined()
  expect(await has(ui, OPENING)).toBe(true)
  // Only once: the full band draws it on its sentence line, not twice.
  await ui.press({ key: 'size' })
  expect((await texts(ui)).filter(t => t.includes(OPENING)).length).toBe(1)
})

test('the link is text like the rest of the row: nothing grey, nothing italic, no icon of its own', async ($, on) => {
  const { ui } = await mounted($, on)
  await ui.press({ key: 'details' })
  for (const t of await ui.findAll({ type: 'Text' })) {
    expect(t.props.dimColor).toBeUndefined()
    expect(t.props.italic).toBeUndefined()
  }
  const svgs = (await ui.findAll({ type: 'Svg' })).map(s => String(s.props.alt))
  expect(svgs.some(a => /dashboard/i.test(a))).toBe(false)
})
