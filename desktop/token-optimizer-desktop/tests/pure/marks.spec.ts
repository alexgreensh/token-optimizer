import { test } from 'node:test'
import assert from 'node:assert/strict'

import { marks, cards, row, savingsPill, type Card } from '../../src/marks.ts'
import { sentence } from '../../src/ladder.ts'
import { iconSvg, ICONS } from '../../src/icons.ts'
import { snap, withQuality, TZ } from './fixtures.ts'
import type { CacheView, SavingsHeadline } from '../../src/contracts.ts'

const means = (c: Card) => c.means.map((r) => r.text).join('')
const todo = (c: Card) => c.todo.map((r) => r.text).join('')
const cacheCard = (cache: CacheView) => cards(snap({ cache }), TZ).find((c) => c.id === 'cache')!
const actionIds = (c: Card) => c.actions.map((a) => a.id)

test('five marks for a healthy subscriber, each with an icon and alt text', () => {
  const m = marks(snap(), TZ)
  assert.deepEqual(m.map((x) => x.id), ['quality', 'context', 'cache', 'fiveHour', 'week'])
  for (const x of m) {
    assert.ok(x.icon in ICONS, `${x.id} icon`)
    assert.ok(x.alt.length > 0, `${x.id} alt`)
  }
  const [q, ctx, cache, five, week] = m
  assert.equal(q!.value, '92')
  assert.equal(q!.badge, 'S')
  assert.equal(q!.tone, 'good')
  assert.equal(ctx!.value, '34%')
  assert.equal(ctx!.ringPercent, 34)
  assert.equal(cache!.value, '42m') // minutes: the band redraws once a minute, never per second
  assert.equal(cache!.tone, 'good')
  assert.equal(five!.value, '41%')
  assert.equal(five!.label, '5 hours')
  assert.match(five!.alt, /renews today at 3:20 PM/)
  assert.equal(week!.label, 'week')
})

test('quality missing: the mark shows "--"', () => {
  const q = marks(snap({ quality: null }), TZ)[0]!
  assert.equal(q.value, '--')
  assert.equal(q.badge, '--')
  assert.equal(q.tone, 'none')
})

test('context missing: "--"', () => {
  const c = marks(snap({ contextPercent: null }), TZ)[1]!
  assert.equal(c.value, '--')
  assert.equal(c.ringPercent, 0)
})

test('API user with no rate limits: no limit marks', () => {
  assert.deepEqual(marks(snap({ fiveHour: null, week: null }), TZ).map((x) => x.id), ['quality', 'context', 'cache'])
})

test('cache mark per state', () => {
  const cold = marks(snap({ cache: { state: 'cold', secondsLeft: 0, lifetime: 3600, measured: true, tokensAtStake: 1 } }), TZ)[2]!
  assert.equal(cold.value, 'cold')
  assert.equal(cold.tone, 'cold')
  assert.equal(cold.icon, 'cold')
  const unknown = marks(snap({ cache: { state: 'unknown', secondsLeft: null, lifetime: 300, measured: false, tokensAtStake: null } }), TZ)[2]!
  assert.equal(unknown.value, '--')
  const warn = marks(snap({ cache: { state: 'warning', secondsLeft: 42, lifetime: 300, measured: false, tokensAtStake: 1 } }), TZ)[2]!
  assert.equal(warn.value, '1m')
  assert.equal(warn.tone, 'caution')
  assert.equal(warn.ringPercent, 14)
  assert.match(warn.alt, /estimate/)
  assert.match(warn.alt, /not measured/)
})

test('tones follow the thresholds', () => {
  const m = marks(withQuality(snap({ contextPercent: 81, fiveHour: { percentUsed: 76, resetsAt: null } }), { score: 61 }), TZ)
  assert.equal(m[0]!.tone, 'bad')
  assert.equal(m[0]!.badge, 'C')
  assert.equal(m[1]!.tone, 'bad')
  assert.equal(m[3]!.tone, 'caution')
})

test('cache card offers Keep warm only when warm or warning and measured', () => {
  const base = { secondsLeft: 900, lifetime: 3600, tokensAtStake: 720_000 }
  assert.deepEqual(actionIds(cacheCard({ ...base, state: 'warm', measured: true })), ['warm'])
  assert.deepEqual(actionIds(cacheCard({ ...base, state: 'warning', secondsLeft: 40, measured: true })), ['warm'])
  assert.deepEqual(actionIds(cacheCard({ ...base, state: 'warm', measured: false })), [])
  assert.deepEqual(actionIds(cacheCard({ ...base, state: 'warning', secondsLeft: 40, measured: false })), [])
  for (const state of ['unknown', 'refreshing', 'warming'] as const) {
    assert.deepEqual(actionIds(cacheCard({ ...base, state, measured: true })), [], state)
  }
  const cold = cacheCard({ ...base, state: 'cold', secondsLeft: 0, measured: true })
  assert.deepEqual(actionIds(cold), ['clean-first'])
  assert.deepEqual(cold.means.filter((r) => r.lose), [{ text: '720k tokens', lose: true }])
  assert.match(todo(cold), /makes later messages cheaper/)
})

test('warning cache card flags the tokens at stake', () => {
  const c = cacheCard({ state: 'warning', secondsLeft: 40, lifetime: 3600, measured: true, tokensAtStake: 720_000 })
  assert.equal(c.title, 'Drops in 1m')
  assert.deepEqual(c.means.filter((r) => r.lose), [{ text: '720k tokens', lose: true }])
})

test('every card says what it means and what to do, in plain short words', () => {
  for (const c of cards(snap(), TZ)) {
    assert.ok(means(c).length > 0, `${c.id} means`)
    assert.ok(todo(c).length > 0, `${c.id} todo`)
    for (const line of [means(c), todo(c)]) {
      assert.doesNotMatch(line, /—/, `${c.id} em-dash`)
      assert.doesNotMatch(line, /\b(compaction continuity|top_drag|tokens_at_stake)\b/, `${c.id} internal name`)
    }
  }
})

test('quality, context and limit cards', () => {
  const all = cards(snap(), TZ)
  const q = all.find((c) => c.id === 'quality')!
  assert.equal(q.title, 'Quality S 92')
  assert.equal(means(q), "How clean Claude's working context is, 0 to 100. Biggest drag: 3 oversized tool results, about 4k tokens.")
  assert.equal(todo(q), 'Nothing to do.')
  assert.deepEqual(q.actions, [{ id: 'clean', label: 'Clean up' }, { id: 'fresh', label: 'Start fresh' }])
  const ctx = all.find((c) => c.id === 'context')!
  assert.equal(ctx.title, '340k of 1M tokens')
  const five = all.find((c) => c.id === 'fiveHour')!
  assert.equal(five.title, 'Renews today at 3:20 PM')
  assert.equal(means(five), '5-hour limit 41% used; resets today at 3:20 PM, in 2h 14m.')
  assert.equal(todo(five), 'Nothing to do.')
  const week = all.find((c) => c.id === 'week')!
  assert.equal(week.title, 'Renews Thursday, Oct 8 at 9:00 AM')
  assert.deepEqual(cards(snap({ fiveHour: null, week: null }), TZ).map((c) => c.id), ['quality', 'context', 'cache'])
})

test('row: compaction count zero has no compaction fact; three says 3× compacted', () => {
  const zero = row(snap(), TZ)
  assert.equal(zero.facts.find((f) => f.icon === 'compact'), undefined)
  const three = row(withQuality(snap(), { compactions: 3 }), TZ)
  const fact = three.facts.find((f) => f.icon === 'compact')!
  assert.equal(fact.runs.map((r) => r.text).join(''), '3× compacted')
})

test('row facts: branch, session time, tool calls, checkpoint', () => {
  const r = row(snap(), TZ)
  const texts = r.facts.map((f) => [f.icon, f.runs.map((x) => x.text).join('')])
  assert.deepEqual(texts, [
    ['branch', 'feat/billing-tests'],
    ['clock', '1h 5m'],
    ['tool', '86 tools'],
    ['bookmark', 'Checkpoint 3m ago'],
  ])
  const bare = row(snap({ branch: null, quality: null }), TZ)
  assert.deepEqual(bare.facts.map((f) => f.icon), ['bookmark'])
  assert.equal(bare.facts[0]!.runs.map((x) => x.text).join(''), 'No checkpoint yet')
})

test('row savings: ready, loading with last figures, unavailable', () => {
  const ready = row(snap(), TZ).savings
  assert.equal(ready.state, 'ready')
  assert.equal(ready.sessionTokens, 640_000)
  assert.equal(ready.sessionText, '640k')
  assert.equal(ready.last30Text, '48M')
  assert.equal(ready.daily.length, 30)
  assert.equal(ready.bars.length, 30)
  assert.equal(ready.bars[29], 100)
  assert.ok(ready.bars.every((b) => b >= 8))

  const loading = row(snap({ savingsLoading: true }), TZ).savings
  assert.equal(loading.state, 'loading')
  assert.equal(loading.sessionText, '640k')

  const none = row(snap({ savings: null }), TZ).savings
  assert.equal(none.state, 'unavailable')
  assert.equal(none.sessionText, '--')
  assert.equal(none.last30Text, '--')
  assert.deepEqual(none.daily, [])
  assert.deepEqual(none.bars, [])
  assert.ok(none.reason && none.reason.length > 0)
})

test('iconSvg returns a standalone SVG with xmlns, colour and alt', () => {
  const svg = iconSvg('check', '#2f9e55', { alt: 'All clear' })
  assert.match(svg, /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg"/)
  assert.match(svg, /stroke="#2f9e55"/)
  assert.match(svg, /aria-label="All clear"/)
  assert.ok(svg.includes(ICONS.check))
  assert.ok(svg.endsWith('</svg>'))
  assert.match(iconSvg('ask', 'red" onload="x'), /stroke="red&quot; onload=&quot;x"/)
  for (const name of ['branch', 'clock', 'tool', 'compact', 'bookmark', 'cold', 'hourglass', 'gauge', 'check', 'slip', 'ask', 'saved'] as const) {
    assert.ok(ICONS[name].length > 0, name)
  }
})

test('#270: a smaller auto-compact window is described as distance to compaction, not a quality warning', () => {
  // 213k on a 1M model with autoCompactWindow 300k: the ring is 71%, quality sees 21%.
  const c = cards(snap({ contextTokens: 213_000, contextWindow: 300_000, contextPercent: 71, contextModelWindow: 1_000_000 }), TZ).find((x) => x.id === 'context')!
  assert.equal(c.title, '213k of 300k before auto-compact')
  assert.equal(means(c), '21% of the 1M model window.')
  assert.doesNotMatch(means(c), /half full/)
})

test('#270: without a smaller auto-compact window the context card is unchanged', () => {
  const c = cards(snap({ contextTokens: 213_000, contextWindow: 1_000_000, contextPercent: 21, contextModelWindow: null }), TZ).find((x) => x.id === 'context')!
  assert.equal(c.title, '213k of 1M tokens')
  assert.equal(means(c), 'Quality holds best under half full.')
})

const HEADLINE: SavingsHeadline = {
  usd30d: 237,
  usdWeek: null,
  tokens30d: 602_700_000,
  display: {
    kind: '30d',
    short: 'Saved $237/30d · 602.7M kept out',
    long: '$237 saved in 30 days · 602.7M tokens kept out of context',
    url: 'http://localhost:24842/token-optimizer#savings',
  },
}

test('savingsPill: the calm line gets the headline, long in full and short in slim', () => {
  const s = snap({ savingsHeadline: HEADLINE })
  const say = sentence(s, TZ)
  assert.equal(say.tone, 'good')
  assert.equal(say.action, null)
  assert.deepEqual(savingsPill(s, say, 'full'), { text: HEADLINE.display!.long, url: HEADLINE.display!.url })
  assert.deepEqual(savingsPill(s, say, 'slim'), { text: HEADLINE.display!.short, url: HEADLINE.display!.url })
})

test('savingsPill: a sentence plus long text that would not fit drops to short', () => {
  const s = snap({ savingsHeadline: HEADLINE, note: `Compacts:2 (observing). ${'detail '.repeat(12)}`.trim() })
  const say = sentence(s, TZ)
  const pill = savingsPill(s, say, 'full')
  assert.equal(pill?.text, HEADLINE.display!.short)
})

test('savingsPill: no headline, no display, or a sentence that asks for something means no pill', () => {
  assert.equal(savingsPill(snap(), sentence(snap(), TZ), 'full'), null) // installs that predate savings_headline
  const noDisplay = snap({ savingsHeadline: { ...HEADLINE, display: null } })
  assert.equal(savingsPill(noDisplay, sentence(noDisplay, TZ), 'full'), null)
  const urgent = snap({ savingsHeadline: HEADLINE, fiveHour: { percentUsed: 95, resetsAt: '2026-10-03T19:20:00Z' } })
  assert.equal(savingsPill(urgent, sentence(urgent, TZ), 'full'), null)
  const armed = snap({ savingsHeadline: HEADLINE, freshArmed: true })
  assert.equal(savingsPill(armed, sentence(armed, TZ), 'full'), null)
})
