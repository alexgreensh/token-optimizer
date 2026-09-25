import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { parseSession, activeBranch, rollup, IncrementalRollup } from '../src/rollup.ts';
const usage = (cost) => ({ input: 10, output: 2, cacheRead: 4, cacheWrite: 1, totalTokens: 17, ...(cost === undefined ? {} : { cost: { total: cost } }) });
const msg = (id, parentId, model, cost) => ({ id, parentId, type: 'message', message: { role: 'assistant', provider: 'anthropic', model, usage: usage(cost) } });
test('forks count only active ancestors, never sibling charges', () => {
 const rows = [msg('a', null, 'opus', .25), msg('b', 'a', 'sonnet', 0), msg('c', 'a', 'haiku', .1)];
 assert.deepEqual(rollup(activeBranch(rows, 'b')).models.map(m => m.model), ['opus', 'sonnet']);
 assert.deepEqual(rollup(activeBranch(rows, 'c')).models.map(m => m.model), ['opus', 'haiku']);
});
test('native zero-cost is not treated as missing price; no fabricated savings', () => {
 const models = rollup([msg('a', null, 'opus', 0), msg('b', 'a', 'opus')]).models;
 assert.equal(models[0].nativeCost, 0);
 assert.equal(models[0].unpricedCalls, 1);
 assert.equal(models[0].calls, 2);
 assert.equal(models[0].totalTokens, 34);
});
test('JSONL skips malformed lines and tracks Pi usage entries', () => {
 const entries = [msg('a', null, 'opus', 2), { id:'b', parentId:'a', type:'usage', provider:'anthropic', model:'opus', usage:usage(.3) }];
 const output = parseSession(entries.map(e=>JSON.stringify(e)).join('\n')+'\n{broken', 'b');
 assert.equal(output.invalidLines, 1);
 assert.equal(output.models[0].nativeCost, 2.3);
 assert.equal(output.models[0].calls, 2);
});
test('incremental snapshot does not double-count and rebuilds on fork', () => {
 const i = new IncrementalRollup(); const rows=[msg('a', null, 'opus',1), msg('b','a','sonnet',2)];
 assert.equal(i.update(activeBranch(rows,'b')).models.length, 2);
 assert.equal(i.update(activeBranch(rows,'b')).models.reduce((n,m)=>n+m.nativeCost,0),3);
 assert.equal(i.update(activeBranch([...rows,msg('c','a','haiku',4)],'c')).models.reduce((n,m)=>n+m.nativeCost,0),5);
});
