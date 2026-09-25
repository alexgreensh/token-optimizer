import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { repeatedCall, qualityScore, ReadCache } from '../src/signals.ts';
test('signal needs three consecutive calls',()=>{ assert.equal(repeatedCall([], 'x'),false); assert.equal(repeatedCall(['x'],'x'),false); assert.equal(repeatedCall(['x','x'],'x'),true); assert.equal(repeatedCall(['x','y'],'x'),false); });
test('read-cache invalidates on fingerprint changes',()=>{ const c=new ReadCache();c.set('a','1','content');assert.equal(c.get('a','1'),'content');assert.equal(c.get('a','2'),undefined);assert.equal(c.get('a','1'),undefined); });
test('quality score bounded',()=>{ assert.equal(qualityScore(''),100); assert.ok(qualityScore('sorry, sorry, sorry')<100); });
