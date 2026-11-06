import { test } from 'node:test'
import assert from 'node:assert/strict'
import { switchedOff, timeOptions } from '../../src/switches.ts'

test('switchedOff: only the off words switch a feature off', () => {
  for (const v of ['0', 'false', 'OFF', ' no ']) assert.equal(switchedOff(v), true, v)
  for (const v of [undefined, '', '1', 'on', 'slim']) assert.equal(switchedOff(v), false, String(v))
})

test('timeOptions: 24h and 12h force a clock, anything else (or unset) follows the locale', () => {
  for (const v of ['24h', '24', ' 24H ']) assert.deepEqual(timeOptions(v), { hourCycle: 'h23' }, v)
  for (const v of ['12h', '12', '12H']) assert.deepEqual(timeOptions(v), { hourCycle: 'h12' }, v)
  for (const v of [undefined, '', 'auto', '13h']) assert.deepEqual(timeOptions(v), {}, String(v))
})
