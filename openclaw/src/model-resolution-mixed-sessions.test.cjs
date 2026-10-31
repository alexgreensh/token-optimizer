const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {resolvePricedModel} = require('./event-pricing.ts');
const {computeRealizedSavings} = require('./savings.ts');
test('savings prices only evidenced foreign cards and dated aliases', () => {
  for (const model of ['gpt-5.4-unlisted-variant','gpt-5.6-unlisted-variant','gemini-2.5-pro-unlisted-variant','gpt-5.99','deepseek-v99']) assert.equal(resolvePricedModel(model,''),null,model);
  for (const model of ['openai/gpt-5.4-2026-03-05','google/gemini-2.5-pro','deepseek-v3','local']) assert.notEqual(resolvePricedModel(model,''),null,model);
});
test('mixed unknown tokens preserve known routing and frozen baseline', () => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'to-native-mixed-'));
  const state=path.join(dir,'token-optimizer'); fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model)=>({sessionId:id,ts,model,input:100000,output:10000,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const early=[row('install',t,'opus')]; for(let i=0;i<35;i++) early.push(row('before'+i,t+(2+i/2)*day,'opus'));
  const after=Array.from({length:12},(_,i)=>row('after'+i,now-(i+1)*day,'sonnet'));
  const file=path.join(state,'session-history.json'); fs.writeFileSync(file,JSON.stringify([...early,...after]));
  const before=computeRealizedSavings(dir,30,now);
  const baseline=fs.readFileSync(path.join(state,'baseline-state.json'),'utf8');
  for(const r of after){r.input++;r.modelTokenBuckets={sonnet:{input:100000,output:10000,cacheRead:0,cacheWrite:0},'unpriced-model-xyz':{input:1,output:0,cacheRead:0,cacheWrite:0}};}
  fs.writeFileSync(file,JSON.stringify([...early,...after]));
  const mixed=computeRealizedSavings(dir,30,now);
  assert.equal(mixed.ready,true); assert.equal(mixed.monthlySavingsUsd,before.monthlySavingsUsd); assert.equal(mixed.sessionsPerMonth,before.sessionsPerMonth);
  assert.equal(mixed.unpricedTokens,12); assert.equal(fs.readFileSync(path.join(state,'baseline-state.json'),'utf8'),baseline);
});
test('aggregate sessions use base cards without treating the total as one long request', () => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'to-native-tier-'));
  const state=path.join(dir,'token-optimizer'); fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts)=>({sessionId:id,ts,model:'gpt-5.4',input:1000000,output:0,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const rows=[row('install',t)]; for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  assert.equal(computeRealizedSavings(dir,30,now).beforeCostPerSession,2.5);
  const {calculateCost}=require('./pricing.ts');
  assert.equal(calculateCost({input:1000000,output:0,cacheRead:0,cacheWrite:0},'gpt-5.4',dir),5);
});
