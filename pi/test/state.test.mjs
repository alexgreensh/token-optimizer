import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
// TypeScript 5.9 can transpile this source on Node 22 via strip-types? Tests import compiled after tsc in later rounds.
test('Pi package manifest declares one extension and no runtime dependency on host', () => {
 const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
 assert.deepEqual(pkg.pi.extensions, ['./extensions/token-optimizer.ts']);
 assert.equal(pkg.dependencies?.['@earendil-works/pi-coding-agent'], undefined);
 assert.equal(pkg.peerDependencies['@earendil-works/pi-coding-agent'], '*');
});
