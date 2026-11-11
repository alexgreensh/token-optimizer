import { expect } from 'claude-code/testing'
import { BAND, START, stub, test } from './world.ts'

test('a fork that was not sent cancels its reservation', async ($, on) => {
  const w = stub(on, { fork: 'nothing' })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'action' })
  await w.clock.settle()
  const actions = w.runs.filter(r => r.argv.includes('mod-action')).map(r => JSON.parse(r.stdin!).action)
  expect(actions).toEqual(['warm-prepare', 'warm-cancel'])
})

test('a confirmed manual clean sends measured compaction evidence', async ($, on) => {
  const w = stub(on, { compact: 'ok-watched' })
  const transcript = '/work/sess-1.jsonl'
  w.files[transcript] = [1, '{"synthetic":true}\n']
  w.compactBeneath = async () => {
    await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Earlier.', toolUses: [], handle: 'm-1' }] } as never)
  }
  await $.session.start(START)
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-1', transcript_path: transcript } as never)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  await w.clock.advance(5000)
  await w.clock.settle()
  const calls = w.runs.filter(r => r.argv.includes('mod-action')).map(r => JSON.parse(r.stdin!))
  expect(calls.length).toBe(2)
  expect(calls[0].action_id).toBe(calls[1].action_id)
  expect(calls[0].action).toBe('clean')
  expect(calls[0].before_tokens).toBe(620_000)
  expect(calls[0].after_tokens).toBe(40_000)
  expect(calls[0].usage.output_tokens).toBe(4)
  expect(calls[0].cost_usd).toBeGreaterThan(0)
})

test('clean up first keeps its distinct recommendation in the receipt', async ($, on) => {
  const w = stub(on, { compact: 'ok-watched' })
  w.status = { ...w.status, requestAgoS: 7200 }
  w.compactBeneath = async () => {
    await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Earlier.', toolUses: [], handle: 'm-1' }] } as never)
  }
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'action' })
  await w.clock.settle()
  await w.clock.advance(5000)
  await w.clock.settle()
  const calls = w.runs.filter(r => r.argv.includes('mod-action')).map(r => JSON.parse(r.stdin!))
  expect(calls.length).toBe(2)
  expect(calls[0].action_id).toBe(calls[1].action_id)
  expect(calls[0].action).toBe('clean-first')
})

test('successful fresh records an estimated-only receipt for the old session', async ($, on) => {
  const w = stub(on)
  w.clearBeneath = async () => {
    await $.session.end({ reason: 'clear', sessionId: 'sess-1', resume: {} } as never)
    w.sessionId = 'sess-2'
  }
  await $.session.start(START)
  await $.classic.SessionStart({ source: 'startup', session_id: 'sess-1', transcript_path: '/work/sess-1.jsonl' } as never)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'action' })
  await w.clock.settle()
  await w.clock.advance(5000)
  await w.clock.settle()
  const calls = w.runs.filter(r => r.argv.includes('mod-action')).map(r => JSON.parse(r.stdin!))
  expect(calls.length).toBe(1)
  expect(calls[0].action).toBe('fresh')
  expect(calls[0].session_id).toBe('sess-1')
  expect(calls[0].before_tokens).toBe(620_000)
  expect(calls[0].handoff_tokens_estimate).toBeGreaterThan(0)
})

test('an unconfirmed clear does not produce a fresh savings receipt', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await w.clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'action' })
  await w.clock.settle()
  await w.clock.advance(5000)
  await w.clock.settle()
  expect(w.runs.filter(r => r.argv.includes('mod-action')).length).toBe(0)
  expect(w.toasts).toContain('Start fresh could not be confirmed or recorded for accounting.')
})
