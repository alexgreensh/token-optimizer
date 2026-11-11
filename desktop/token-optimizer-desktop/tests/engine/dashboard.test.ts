// The "Dashboard ↗" link capping the gauges row in both sizes, folded or not: what its
// click runs, the busy guard and the failure note, through register.tsx inside the engine.
import type { On } from 'claude-code'
import { expect, type Engine } from 'claude-code/testing'

import { BUSY_TIMEOUT_MS, busyTimeoutMs, DASHBOARD_TIMEOUT_MS, NOTE_MS } from '../../src/actions.ts'
import { BAND, HEADLINE, SAVED, START, quality, stub, SCRIPTS, type Status, type World, test } from './world.ts'

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

/** The calm status answer, with `savings_headline` added when a test wants the pill. */
const calmStatus = (savings_headline?: unknown): Status => ({
  savings: SAVED,
  savings_headline,
  savings_state: 'fresh',
  savings_reason: null,
  requestAgoS: 30,
  cache_lifetime: '1h',
})

async function mounted($: Engine, on: On, patch: Partial<World> = {}, size: 'full' | 'slim' = 'full') {
  const w = stub(on, patch)
  await $.session.start(START)
  await w.clock.settle() // the status read runs just after the start
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  if (size === 'slim') await ui.press({ key: 'size' })
  return { w, ui }
}

for (const size of ['full', 'slim'] as const) {
  test(`${size}: the folded band already ends its gauges with a plain "Dashboard ↗" link`, async ($, on) => {
    const { ui } = await mounted($, on, {}, size)
    const link = await ui.find({ key: 'dashboard' })
    expect(link).toBeDefined()
    expect(link?.props.label).toBe('Dashboard ↗')
    // A link, not a fourth button: drawn without chrome, no variant, no dimmed or new colour.
    expect(link?.props.plain).toBe(true)
    expect(link?.props.variant).toBeUndefined()
    expect(link?.props.dimColor).toBeUndefined()
    expect(link?.props.role).toBeUndefined()

    // Unfolding keeps the same single link: it lives on the gauges row, not in the row.
    await ui.press({ key: 'details' })
    expect((await ui.findAll({ type: 'Button' })).filter(b => b.props.key === 'dashboard').length).toBe(1)
  })

  test(`${size}: the link is the last button on the gauges row while folded`, async ($, on) => {
    const { ui } = await mounted($, on, {}, size)
    const keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))
    expect(keys[keys.length - 1]).toBe('dashboard')
  })

  test(`${size}: a click runs measure.py dashboard through the runner with its own bounded timeout, argv only`, async ($, on) => {
    const { w, ui } = await mounted($, on, {}, size)
    await ui.press({ key: 'dashboard' })
    await w.clock.settle()

    const runs = dashboardRuns(w)
    expect(runs.length).toBe(1)
    const argv = runs[0]!.argv
    // The same launcher the other measure.py calls use: python, the module runner, the scripts folder, then the command.
    expect(argv.slice(-2)).toEqual(['dashboard', '--user'])
    expect(argv.some(a => a.endsWith('module_runner.py'))).toBe(true)
    expect(argv).toContain(SCRIPTS)
    expect(argv).not.toContain('--quiet')
    // No shell string: every argument is its own element and none is a command line.
    expect(argv.some(a => /^(sh|bash|cmd|cmd\.exe|powershell)(\.exe)?$/i.test(a) || a === '-c' || a === '/c')).toBe(false)
    expect(argv.filter(a => /\s/.test(a))).toEqual([])
    expect(runs[0]!.timeoutMs).toBe(DASHBOARD_TIMEOUT_MS)
    expect(DASHBOARD_TIMEOUT_MS).toBeLessThan(busyTimeoutMs('dashboard'))

    // Done: nothing left on the note line, no toast, back to the calm sentence.
    expect(w.toasts).toEqual([])
    expect(await has(ui, OPENING)).toBe(false)
    expect(await has(ui, FAILED)).toBe(false)
  })

  test(`${size}: while it runs the note says "Opening the dashboard." and a second click is ignored`, async ($, on) => {
    const { w, ui } = await mounted($, on, { dashboardDelayMs: 5_000 }, size)
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
    await ui.press({ key: 'dashboard' })
    await w.clock.advance(1_000)
    expect(await has(ui, OPENING)).toBe(true)
    await w.clock.advance(busyTimeoutMs('dashboard'))
    await w.clock.settle()
    expect(await has(ui, OPENING)).toBe(false)
    expect(await has(ui, FAILED)).toBe(true)
    expect(w.toasts).toEqual([])
    expect(dashboardRuns(w).length).toBe(1)
  })

  test(`${size}: a heavy rebuild is still "Opening the dashboard." past 120 s, then opens`, async ($, on) => {
    // 150 s: past the old 120 s busy limit, inside the dashboard's own.
    const { w, ui } = await mounted($, on, { dashboardDelayMs: 150_000 }, size)
    await ui.press({ key: 'dashboard' })
    await w.clock.advance(BUSY_TIMEOUT_MS + 1_000)
    expect(await has(ui, OPENING)).toBe(true)
    expect(await has(ui, FAILED)).toBe(false)
    // Still busy: a second click is ignored.
    await ui.press({ key: 'dashboard' })
    expect(dashboardRuns(w).length).toBe(1)

    await w.clock.advance(40_000)
    await w.clock.settle()
    expect(await has(ui, OPENING)).toBe(false)
    expect(await has(ui, FAILED)).toBe(false)
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

test('slim: the dashboard progress stands beside the link on the one line; full carries it on the sentence', async ($, on) => {
  const { w, ui } = await mounted($, on, { dashboardDelayMs: 5_000 }, 'slim')
  await ui.press({ key: 'dashboard' })
  await w.clock.advance(1_000)
  expect((await ui.find({ key: 'dashboard-status' }))?.props).toBeDefined()
  expect(await has(ui, OPENING)).toBe(true)
  // Only once: the full band draws it on its sentence line, not beside the link too.
  await ui.press({ key: 'size' })
  expect(await ui.find({ key: 'dashboard-status' })).toBeUndefined()
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

test('the savings pill shows the headline on a calm line, long in full and short in slim, and opens the dashboard', async ($, on) => {
  const { w, ui } = await mounted($, on, { status: calmStatus(HEADLINE) })
  const pill = await ui.find({ key: 'savings-pill' })
  expect(pill?.props.label).toBe(`${HEADLINE.display.long} ↗`)
  // A link like Dashboard ↗: plain, no chrome of its own.
  expect(pill?.props.plain).toBe(true)

  await ui.press({ key: 'size' })
  expect((await ui.find({ key: 'savings-pill' }))?.props.label).toBe(`${HEADLINE.display.short} ↗`)

  await ui.press({ key: 'savings-pill' })
  await w.clock.settle()
  expect(dashboardRuns(w).length).toBe(1)
})

test('no pill when the status answer has no savings_headline (an older install)', async ($, on) => {
  const { ui } = await mounted($, on)
  expect(await ui.find({ key: 'savings-pill' })).toBeUndefined()
})

test('no pill when the headline has no display text', async ($, on) => {
  const { ui } = await mounted($, on, { status: calmStatus({ ...HEADLINE, display: null }) })
  expect(await ui.find({ key: 'savings-pill' })).toBeUndefined()
})

test('no pill while the sentence asks for something: the sentence and its button win', async ($, on) => {
  const w = stub(on, { status: calmStatus(HEADLINE) })
  sag(w)
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await ui.find({ key: 'savings-pill' })).toBeUndefined()
})
