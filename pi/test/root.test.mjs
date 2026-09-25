import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
test('git package exposes only Pi extension, not Claude Code skills',()=>{
 const pkg=JSON.parse(readFileSync(new URL('../../package.json',import.meta.url),'utf8'));
 assert.deepEqual(pkg.pi.extensions,['./pi/extensions/token-optimizer.ts']);
 assert.deepEqual(pkg.pi.skills,[]);
});
test('root package does not change CommonJS execution of existing statusline',()=>{
 const pkg=JSON.parse(readFileSync(new URL('../../package.json',import.meta.url),'utf8'));
 assert.notEqual(pkg.type,'module');
});
