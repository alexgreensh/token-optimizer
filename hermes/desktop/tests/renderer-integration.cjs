const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const assert = require('node:assert/strict')
;(async () => {
  const code = fs.readFileSync(path.join(__dirname, '../desktop-plugins/token-optimizer-bar/plugin.js'), 'utf8')
  const state = []; let cursor = 0; let root; const calls = []
  const atom = initial => { let value = initial; return { get: () => value, set: next => { value = next } } }
  const session = atom('test-session'), profile = atom('default'), busy = atom({})
  const host = { state: { focusedUsage: atom(null), focusedSessionId: session, focusedStoredSessionId: atom('test-session'), focusedSessionProfile: profile, busyBySession: busy, model: atom('model'), gateway: atom('open') }, request: async (...args) => { calls.push(args); return { status: 'compressed' } }, notify: () => {}, notifyError: () => {} }
  const sandbox = vm.createContext({ console, Number, Date, Math, Boolean, window: { matchMedia: () => ({ matches: true }), setInterval: () => 1, clearInterval: () => {} } })
  const module = new vm.SourceTextModule(code, { context: sandbox })
  const mocks = {
    '@hermes/plugin-sdk': { COMPOSER_AREAS: { top: 'top' }, host, useValue: atom => atom.get(), useQuery: () => ({ data: {} }), gatewayActivationEpoch: () => 1, Popover: 'Popover', PopoverTrigger: 'PopoverTrigger', PopoverContent: 'PopoverContent' },
    react: { useState: initial => { const i = cursor++; if (!(i in state)) state[i] = typeof initial === 'function' ? initial() : initial; return [state[i], v => { state[i] = typeof v === 'function' ? v(state[i]) : v }] }, useEffect: () => {} },
    'react/jsx-runtime': { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }
  }
  await module.link(name => new vm.SyntheticModule(Object.keys(mocks[name]), function () { for (const [k,v] of Object.entries(mocks[name])) this.setExport(k,v) }, { context: sandbox }))
  await module.evaluate()
  module.namespace.default.register({ register: entry => { root = entry.render }, os: {} })
  const render = () => { cursor = 0; const element = root(); return element.type(element.props) }
  const buttons = node => !node || typeof node !== 'object' ? [] : [ ...(node.type === 'button' ? [node] : []), ...[].concat(node.props?.children || []).flatMap(buttons) ]
  const first = buttons(render()).find(x => x.props.children === 'Clean up')
  assert.ok(first, 'Clean up is present')
  await first.props.onClick()
  assert.equal(calls.length, 0, 'first click only requests confirmation')
  const confirm = buttons(render()).find(x => x.props.children === 'Conferma compattazione')
  assert.ok(confirm, 'confirmation is shown')
  await confirm.props.onClick()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], 'session.compress')
  assert.equal(calls[0][1].session_id, 'test-session')
  assert.equal(calls[0][1].profile, 'default')
  await buttons(render()).find(x => x.props.children === 'Clean up').props.onClick()
  const changed = buttons(render()).find(x => x.props.children === 'Conferma compattazione')
  session.set('another-session')
  await changed.props.onClick()
  assert.equal(calls.length, 1, 'session change must prevent compaction')
  session.set('test-session')
  await buttons(render()).find(x => x.props.children === 'Clean up').props.onClick()
  const pending = buttons(render()).find(x => x.props.children === 'Conferma compattazione')
  busy.set({ 'test-session': true })
  await pending.props.onClick()
  assert.equal(calls.length, 1, 'busy session must prevent compaction')
  console.log('integration: explicit double-click and target passed')
})().catch(e => { console.error(e); process.exitCode = 1 })
