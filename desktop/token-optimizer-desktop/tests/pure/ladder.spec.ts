import { test } from 'node:test'
import assert from 'node:assert/strict'

import { sentence, moodOf, type Sentence } from '../../src/ladder.ts'
import { snap, withQuality, TZ } from './fixtures.ts'

const text = (s: Sentence) => s.runs.map((r) => r.text).join('')
const coldCache = { state: 'cold' as const, secondsLeft: 0, lifetime: 3600, measured: true, tokensAtStake: 720_000 }
const warnCache = { state: 'warning' as const, secondsLeft: 42, lifetime: 3600, measured: true, tokensAtStake: 720_000 }

test('healthy session: All clear with no button', () => {
  const s = sentence(snap(), TZ)
  assert.equal(text(s), 'All clear.')
  assert.equal(s.tone, 'good')
  assert.equal(s.icon, 'check')
  assert.equal(s.action, null)
})

test('5-hour at 93% and compacted 3 times: the 5-hour sentence with no button', () => {
  const s = sentence(withQuality(snap({ fiveHour: { percentUsed: 93, resetsAt: '2026-10-03T19:20:00Z' } }), { compactions: 3 }), TZ)
  assert.equal(text(s), '5-hour limit 93% used. Renews at 3:20 PM.')
  assert.equal(s.tone, 'bad')
  assert.equal(s.action, null)
})

test('weekly at 95% and everything else healthy: the weekly sentence', () => {
  const s = sentence(snap({ week: { percentUsed: 95, resetsAt: '2026-10-08T13:00:00Z' } }), TZ)
  assert.equal(text(s), 'Weekly limit 95% used. Renews Thursday, Oct 8 at 9:00 AM.')
  assert.equal(s.action, null)
})

test('5-hour beats weekly when both are at 90% or more', () => {
  const s = sentence(snap({ fiveHour: { percentUsed: 90, resetsAt: null }, week: { percentUsed: 99, resetsAt: null } }), TZ)
  assert.equal(text(s), '5-hour limit 90% used.')
})

test('a limit at 100%: limit reached with its renewal, ahead of a 5-hour at 95%', () => {
  const s = sentence(snap({
    fiveHour: { percentUsed: 95, resetsAt: '2026-10-03T19:20:00Z' },
    week: { percentUsed: 100, resetsAt: '2026-10-08T13:00:00Z' },
  }), TZ)
  assert.equal(text(s), 'Weekly limit reached. Renews Thursday, Oct 8 at 9:00 AM.')
  assert.equal(s.tone, 'bad')
  const five = sentence(snap({ fiveHour: { percentUsed: 104, resetsAt: '2026-10-03T19:20:00Z' } }), TZ)
  assert.equal(text(five), '5-hour limit reached. Renews at 3:20 PM.')
})

test('compacted 3 times: Start fresh', () => {
  const s = sentence(withQuality(snap(), { compactions: 3 }), TZ)
  assert.equal(text(s), 'Compacted 3 times. Early detail is mostly gone.')
  assert.deepEqual(s.action, { id: 'fresh', label: 'Start fresh' })
})

test('score 72 (grade B): no quality sentence; score 61: Quality slipped to C with Clean up', () => {
  assert.equal(text(sentence(withQuality(snap(), { score: 72, grade: 'B' }), TZ)), 'All clear.')
  const s = sentence(withQuality(snap(), { score: 61, grade: 'C' }), TZ)
  assert.equal(text(s), 'Quality slipped to C.')
  assert.equal(s.icon, 'slip')
  assert.deepEqual(s.action, { id: 'clean', label: 'Clean up' })
})

test('cache cold while healthy: the token count is flagged and Clean up first is offered', () => {
  const s = sentence(snap({ cache: coldCache }), TZ)
  assert.equal(s.tone, 'cold')
  assert.equal(s.icon, 'cold')
  const lose = s.runs.filter((r) => r.lose)
  assert.deepEqual(lose, [{ text: '720k tokens', lose: true }])
  assert.match(text(s), /^Cache is cold\. Next message re-reads 720k tokens at full price\./)
  assert.match(text(s), /makes later messages cheaper/)
  assert.deepEqual(s.action, { id: 'clean-first', label: 'Clean up first' })
})

test('cache in warning: countdown, tokens flagged, Keep warm when measured', () => {
  const s = sentence(snap({ cache: warnCache }), TZ)
  assert.equal(text(s), 'Cache drops in 0:42. Then the next message re-reads 720k tokens.')
  assert.equal(s.tone, 'caution')
  assert.deepEqual(s.runs.filter((r) => r.lose), [{ text: '720k tokens', lose: true }])
  assert.deepEqual(s.action, { id: 'warm', label: 'Keep warm' })
})

test('cache in warning with an unmeasured lifetime: no Keep warm', () => {
  const s = sentence(snap({ cache: { ...warnCache, measured: false } }), TZ)
  assert.match(text(s), /^Cache drops in 0:42/)
  assert.equal(s.action, null)
})

test('only the token run ever carries lose', () => {
  for (const cache of [coldCache, warnCache]) {
    const runs = sentence(snap({ cache }), TZ).runs
    assert.equal(runs.filter((r) => r.lose).length, 1)
  }
  assert.ok(sentence(snap(), TZ).runs.every((r) => !r.lose))
})

test('cache in warning or cold while a turn runs: no cache sentence', () => {
  assert.equal(text(sentence(snap({ working: true, cache: warnCache }), TZ)), 'All clear.')
  assert.equal(text(sentence(snap({ working: true, cache: coldCache }), TZ)), 'All clear.')
})

test('quality missing: the quality rule is skipped', () => {
  assert.equal(text(sentence(snap({ quality: null }), TZ)), 'All clear.')
  const cold = sentence(snap({ quality: null, cache: coldCache }), TZ)
  assert.equal(cold.icon, 'cold')
})

test('API user with no rate limits: no limit rules, the next rule fires', () => {
  const s = sentence(withQuality(snap({ fiveHour: null, week: null }), { compactions: 4 }), TZ)
  assert.equal(text(s), 'Compacted 4 times. Early detail is mostly gone.')
})

test('quality beats a cold cache; compaction beats quality', () => {
  assert.equal(sentence(withQuality(snap({ cache: coldCache }), { score: 50 }), TZ).icon, 'slip')
  assert.equal(sentence(withQuality(snap(), { score: 50, compactions: 3 }), TZ).icon, 'compact')
})

test('busy states lead, with no button', () => {
  const clean = sentence(snap({ busy: 'clean', fiveHour: { percentUsed: 95, resetsAt: null } }), TZ)
  assert.equal(text(clean), 'Cleaning up.')
  assert.equal(clean.action, null)
  assert.equal(text(sentence(snap({ busy: 'fresh-capture' }), TZ)), 'Saving checkpoint.')
  assert.equal(text(sentence(snap({ busy: 'fresh-clear' }), TZ)), 'Clearing.')
  assert.equal(text(sentence(snap({ busy: 'warming' }), TZ)), 'Keeping the cache warm.')
  const dashboard = sentence(snap({ busy: 'dashboard' }), TZ)
  assert.equal(text(dashboard), 'Opening the dashboard.')
  assert.equal(dashboard.action, null)
})

test('Start fresh armed asks for the second click', () => {
  const s = sentence(withQuality(snap({ freshArmed: true }), { compactions: 3 }), TZ)
  assert.match(text(s), /Start fresh clears this conversation/)
  assert.deepEqual(s.action, { id: 'fresh', label: 'Click again to clear' })
})

test('pending hand-off says the checkpoint joins the first message', () => {
  const s = sentence(snap({ handoffPending: true, quality: null }), TZ)
  assert.equal(text(s), 'Checkpoint ready, it joins your first message.')
  assert.equal(s.icon, 'bookmark')
  assert.equal(s.action, null)
})

test('a note replaces All clear but not an urgent sentence', () => {
  assert.equal(text(sentence(snap({ note: 'Cleaned up. 120k tokens freed.' }), TZ)), 'Cleaned up. 120k tokens freed.')
  assert.equal(text(sentence(withQuality(snap({ note: 'x' }), { score: 50 }), TZ)), 'Quality slipped to D.')
})

test('mood', () => {
  assert.equal(moodOf(snap()), 'calm')
  assert.equal(moodOf(snap({ fiveHour: { percentUsed: 92, resetsAt: null } })), 'panic')
  assert.equal(moodOf(snap({ week: { percentUsed: 100, resetsAt: null } })), 'panic')
  assert.equal(moodOf(withQuality(snap(), { compactions: 3 })), 'panic')
  assert.equal(moodOf(withQuality(snap(), { score: 61 })), 'worried')
  assert.equal(moodOf(snap({ cache: warnCache })), 'worried')
  assert.equal(moodOf(snap({ cache: coldCache, working: true })), 'calm')
  assert.equal(moodOf(snap({ quality: null, fiveHour: null, week: null })), 'calm')
})
