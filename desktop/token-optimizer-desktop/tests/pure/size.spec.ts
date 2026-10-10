import { test } from 'node:test'
import assert from 'node:assert/strict'

import { envSize, storedSize, slimSide } from '../../src/size.ts'
import { sentence } from '../../src/ladder.ts'
import { snap, withQuality, TZ } from './fixtures.ts'

const text = (s: { runs: { text: string }[] }) => s.runs.map((r) => r.text).join('')

test('envSize: only slim slims, anything else (or unset) is full', () => {
  assert.equal(envSize('slim'), 'slim')
  assert.equal(envSize(' slim '), 'slim')
  assert.equal(envSize('SLIM'), 'slim')
  assert.equal(envSize('full'), 'full')
  assert.equal(envSize('banana'), 'full')
  assert.equal(envSize(''), 'full')
  assert.equal(envSize(undefined), 'full')
})

test('storedSize: only the two real sizes count, anything else is nothing stored', () => {
  assert.equal(storedSize('slim'), 'slim')
  assert.equal(storedSize('full'), 'full')
  assert.equal(storedSize('banana'), null)
  assert.equal(storedSize(''), null)
  assert.equal(storedSize(0), null)
  assert.equal(storedSize(null), null)
  assert.equal(storedSize(undefined), null)
  assert.equal(storedSize({ size: 'slim' }), null)
})

test('a calm sentence earns nothing on the slim line', () => {
  const say = sentence(snap(), TZ)
  assert.equal(say.tone, 'good')
  assert.equal(say.action, null)
  assert.deepEqual(slimSide(say), { kind: 'none' })
})

test('a busy note is calm: nothing on the right while Clean up runs', () => {
  const say = sentence(snap({ busy: 'clean' }), TZ)
  assert.equal(text(say), 'Cleaning up.')
  assert.deepEqual(slimSide(say), { kind: 'none' })
})

test('a sentence with a button shows its icon and that button, never its text', () => {
  const say = sentence(withQuality(snap(), { score: 60 }), TZ)
  assert.equal(say.action?.id, 'clean')
  const side = slimSide(say)
  assert.equal(side.kind, 'action')
  assert.equal(side.kind === 'action' && side.icon, 'slip')
  assert.equal(side.kind === 'action' && side.action.label, 'Clean up')
})

test('the armed Start fresh keeps its confirm button on the slim line', () => {
  const say = sentence(snap({ freshArmed: true }), TZ)
  const side = slimSide(say)
  assert.equal(side.kind, 'action')
  assert.equal(side.kind === 'action' && side.action.label, 'Click again to clear')
})

test('a bad sentence with no button shows its icon and its text', () => {
  const say = sentence(snap({ fiveHour: { percentUsed: 95, resetsAt: null } }), TZ)
  assert.equal(say.tone, 'bad')
  assert.equal(say.action, null)
  const side = slimSide(say)
  assert.equal(side.kind, 'warn')
  assert.equal(side.kind === 'warn' && side.icon, 'gauge')
  assert.match(side.kind === 'warn' ? text(side) : '', /5-hour limit 95% used/)
})

test('a warning sentence with no button shows its text too', () => {
  const s = snap({ cache: { state: 'warning', secondsLeft: 120, lifetime: 3600, measured: false, tokensAtStake: null } })
  const say = sentence(s, TZ)
  assert.equal(say.tone, 'caution')
  assert.equal(say.action, null)
  const side = slimSide(say)
  assert.equal(side.kind, 'warn')
  assert.match(side.kind === 'warn' ? text(side) : '', /Cache drops in 2:00/)
})

test('a cold sentence that somehow lost its button is still not calm', () => {
  const say = { icon: 'cold' as const, tone: 'cold' as const, runs: [{ text: 'Cache is cold.' }], action: null }
  const side = slimSide(say)
  assert.equal(side.kind, 'warn')
})
