import assert from 'node:assert/strict'
import test from 'node:test'
import { decideArgs, liveMeters, parseDecision, proofOk, shouldAsk } from '../../src/autowarm.ts'
import { initialClock, view, type ClockState } from '../../src/clock.ts'

const anchor = 10_000
const now = anchor + 3_300_000
const clock: ClockState = { ...initialClock(), anchor, lifetime: '1h', contextTokens: 100_000 }
const warning = view(clock, now, 300)

test('asks once on each eligible measured warning anchor', () => {
  assert.equal(shouldAsk(clock, warning, now, null), true)
  assert.equal(shouldAsk(clock, warning, now, anchor), false)
  assert.equal(shouldAsk(clock, warning, now, anchor - 1), true)
})

test('refuses other states, a running turn, a warm-up, and the deadline margin', () => {
  assert.equal(shouldAsk(clock, { ...warning, state: 'warm' }, now, null), false)
  assert.equal(shouldAsk(clock, { ...warning, measured: false }, now, null), false)
  assert.equal(shouldAsk({ ...clock, working: true }, warning, now, null), false)
  assert.equal(shouldAsk({ ...clock, warming: true }, warning, now, null), false)
  assert.equal(shouldAsk({ ...clock, lifetime: null }, warning, now, null), false)
  assert.equal(shouldAsk({ ...clock, anchor: null }, warning, now, null), false)
  assert.equal(shouldAsk(clock, warning, anchor + 3_585_000, null), false)
})

test('only a well-formed ping decision authorizes a fork', () => {
  for (const bad of ['', 'ping', '{', '[]', 'null', '{"action":true}', '{"action":"stop"}']) {
    assert.equal(parseDecision(bad), 'stop')
  }
  assert.equal(parseDecision('{"action":"ping"}'), 'ping')
})

test('only supplied live meters appear in argv', () => {
  const meters = liveMeters({ rateLimits: [{ kind: 'five_hour', percentUsed: 50 }] })
  assert.deepEqual(decideArgs('s', 100_000, 3300, 'model', 3600, meters), [
    'keepwarm-decide', '--session', 's', '--context-tokens', '100000', '--idle-seconds', '3300',
    '--model', 'model', '--lifetime', '3600', '--json', '--five-hour-pct', '50',
  ])
})

test('proof matches the Python thresholds and fails closed on missing usage', () => {
  assert.equal(proofOk({ cache_read_input_tokens: 90_000, cache_creation_input_tokens: 5_000 }, 100_000), true)
  assert.equal(proofOk({ cache_read_input_tokens: 89_999, cache_creation_input_tokens: 5_000 }, 100_000), false)
  assert.equal(proofOk({ cache_read_input_tokens: 90_000, cache_creation_input_tokens: 5_001 }, 100_000), false)
  assert.equal(proofOk({ cache_read_input_tokens: 100_000 }, 100_000), false)
})
