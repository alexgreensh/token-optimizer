import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { repeatedCall, safeTrim, qualityScore, ReadCache } from '../src/signals.ts';
test('signal needs three consecutive calls',()=>{ assert.equal(repeatedCall([], 'x'),false); assert.equal(repeatedCall(['x'],'x'),false); assert.equal(repeatedCall(['x','x'],'x'),true); assert.equal(repeatedCall(['x','y'],'x'),false); });
test('safe trim is bounded and keeps ends',()=>{ const v='start'+'a'.repeat(20000)+'end'; const out=safeTrim(v); assert.ok(out.startsWith('start')); assert.ok(out.endsWith('end')); assert.ok(out.length<13000); assert.equal(safeTrim('small'),null); });
test('read-cache invalidates on fingerprint changes',()=>{ const c=new ReadCache();c.set('a','1','content');assert.equal(c.get('a','1'),'content');assert.equal(c.get('a','2'),undefined);assert.equal(c.get('a','1'),undefined); });
test('quality score bounded',()=>{ assert.equal(qualityScore(''),100); assert.ok(qualityScore('sorry, sorry, sorry')<100); });
