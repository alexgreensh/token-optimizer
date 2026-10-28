// Claude Code's built-in policy guard skips every classic.* event for a
// person's plugin and tells the user once per skipped event. Every classic
// handler we register is therefore one more notice on a managed machine, so
// the set is pinned: add one only for behavior that has no other event.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('only the classic events with no other source are registered', () => {
  const source = readFileSync(new URL('../../hooks/register.tsx', import.meta.url), 'utf8')
  const found = [...source.matchAll(/\bon\(\s*'(classic\.[A-Za-z*]+)'/g)].map(m => m[1]).sort()
  assert.deepEqual(found, ['classic.PermissionRequest', 'classic.SessionStart'])
})
