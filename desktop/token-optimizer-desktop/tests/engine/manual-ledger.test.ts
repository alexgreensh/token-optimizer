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
