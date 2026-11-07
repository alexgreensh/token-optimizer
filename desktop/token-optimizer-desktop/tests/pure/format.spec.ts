import { test } from 'node:test'
import assert from 'node:assert/strict'

import { clock, tokens, relative, ago, duration, renewal, renewalPhrase, gradeOf } from '../../src/format.ts'
import { NOW, TZ } from './fixtures.ts'

test('clock renders m:ss, clamps below zero and caps at 60:00', () => {
  assert.equal(clock(0), '0:00')
  assert.equal(clock(42), '0:42')
  assert.equal(clock(2483), '41:23')
  assert.equal(clock(3600), '60:00')
  assert.equal(clock(7200), '60:00')
  assert.equal(clock(-5), '0:00')
  assert.equal(clock(41.7), '0:41')
})

test('tokens are short and round', () => {
  assert.equal(tokens(940), '940')
  assert.equal(tokens(4_200), '4k')
  assert.equal(tokens(340_000), '340k')
  assert.equal(tokens(999_700), '1M')
  assert.equal(tokens(1_200_000), '1.2M')
  assert.equal(tokens(1_000_000), '1M')
  assert.equal(tokens(50_000_000), '50M')
  assert.equal(tokens(48_400_000), '48M')
})

test('relative time reads like speech', () => {
  const m = 60_000
  assert.equal(relative(NOW + (2 * 60 + 14) * m, NOW), 'in 2h 14m')
  assert.equal(relative(NOW + (60 + 2) * m, NOW), 'in 1h 02m')
  assert.equal(relative(NOW + 48 * m, NOW), 'in 48 minutes')
  assert.equal(relative(NOW + 1 * m, NOW), 'in 1 minute')
  assert.equal(relative(NOW + 20_000, NOW), 'in under a minute')
  assert.equal(relative(NOW + 6 * 24 * 60 * m, NOW), 'in 6 days')
  assert.equal(relative(NOW + 30 * 60 * m, NOW), 'in 1 day 6h')
  assert.equal(ago(NOW - 3 * m, NOW), '3m ago')
  assert.equal(ago(NOW - 10_000, NOW), 'just now')
  assert.equal(ago(NOW - 125 * m, NOW), '2h 5m ago')
  assert.equal(ago(NOW - 3 * 24 * 60 * m, NOW), '3 days ago')
})

test('duration is compact', () => {
  assert.equal(duration(3900), '1h 5m')
  assert.equal(duration(48 * 60), '48m')
  assert.equal(duration(20), 'under 1m')
})

test('renewal: an ISO reset renders as local time', () => {
  // NOW is 1:06 PM in New York on Saturday, Oct 3; the reset is 3:20 PM.
  assert.equal(renewal('2026-10-03T19:20:00Z', NOW, TZ), 'today at 3:20 PM')
  assert.equal(relative(Date.parse('2026-10-03T19:20:00Z'), NOW), 'in 2h 14m')
  assert.equal(renewal('2026-10-04T13:00:00Z', NOW, TZ), 'tomorrow at 9:00 AM')
  assert.equal(renewal('2026-10-08T13:00:00Z', NOW, TZ), 'Thursday, Oct 8 at 9:00 AM')
  // In Tokyo it is already Sunday 2:06 AM, so New York's "tomorrow" is Tokyo's today.
  assert.equal(renewal('2026-10-04T13:00:00Z', NOW, { timeZone: 'Asia/Tokyo', locale: 'en-US' }), 'today at 10:00 PM')
  assert.equal(renewal('not a date', NOW, TZ), null)
})

test('renewal: hourCycle forces a 12- or 24-hour clock whatever the locale', () => {
  const h23 = { ...TZ, hourCycle: 'h23' } as const
  assert.equal(renewal('2026-10-03T19:20:00Z', NOW, h23), 'today at 15:20')
  assert.equal(renewal('2026-10-08T13:00:00Z', NOW, h23), 'Thursday, Oct 8 at 09:00')
  // Midnight is 00:xx, not 24:xx.
  assert.equal(renewal('2026-10-04T04:05:00Z', NOW, h23), 'tomorrow at 00:05')
  assert.equal(renewalPhrase('2026-10-03T19:20:00Z', NOW, h23), 'at 15:20')
  // A locale that defaults to 24 hours can still ask for 12.
  const de = { timeZone: 'America/New_York', locale: 'de-DE' }
  assert.equal(renewal('2026-10-03T19:20:00Z', NOW, de), 'today at 15:20')
  assert.equal(renewal('2026-10-03T19:20:00Z', NOW, { ...de, hourCycle: 'h12' }), 'today at 3:20 PM')
})

test('renewalPhrase drops "today" for the sentence', () => {
  assert.equal(renewalPhrase('2026-10-03T19:20:00Z', NOW, TZ), 'at 3:20 PM')
  assert.equal(renewalPhrase('2026-10-08T13:00:00Z', NOW, TZ), 'Thursday, Oct 8 at 9:00 AM')
})

test('grades use the score_to_grade boundaries in measure.py', () => {
  const cases: Array<[number, string]> = [
    [100, 'S'], [90, 'S'], [89.9, 'A'], [80, 'A'], [79, 'B'], [70, 'B'],
    [69, 'C'], [55, 'C'], [54, 'D'], [40, 'D'], [39, 'F'], [0, 'F'],
  ]
  for (const [score, grade] of cases) assert.equal(gradeOf(score), grade, `score ${score}`)
})
