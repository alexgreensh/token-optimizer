const { test } = require('node:test');
const os = require('node:os');
const assert = require('node:assert/strict');
const { countedEvents } = require('./counted-events.ts');

test('calibrated verbosity is withheld until an un-nudged control exists', () => {
  const fs = require('node:fs'), path = require('node:path');
  const {readSavingsEventsByCategory, measuredSavingsFromSummary} = require('./test-source-loader.cjs').loadSource('savings.ts');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'to-verbosity-control-'));
  fs.mkdirSync(path.join(dir, 'token-optimizer'));
  const rows = [
    {timestamp:'2026-10-01T00:00:00Z', session_id:'a', event_type:'tool_archive', tokens_saved:10, cost_saved_usd:1},
    {timestamp:'2026-10-01T00:00:10Z', session_id:'a', event_type:'verbosity_steer_measured', tokens_saved:100, cost_saved_usd:5},
  ];
  fs.writeFileSync(path.join(dir, 'token-optimizer', 'savings-events.jsonl'), rows.map(JSON.stringify).join('\n'));
  const summary = readSavingsEventsByCategory(dir);
  assert.equal(summary.totalCostSavedUsd, 1);
  assert.equal(summary.totalTokensSaved, 10);
  assert.equal(measuredSavingsFromSummary(summary), 1);
  assert.equal(summary.verbosityOutputEstimated.basis, 'not_measured');
  assert.equal(summary.verbosityOutputEstimated.events, 1);
});

test('filters fixtures, screenshots and twins without losing real sessions', () => {
  const row = (session, seconds, extra = {}) => ({timestamp: `2026-10-01T00:00:${String(seconds).padStart(2,'0')}Z`, session_id: session, event_type: 'tool_archive', tokens_saved: 100, cost_saved_usd: 1, detail: 'read file', ...extra});
  const rows = [row('test-session-1',0), row('codex-sess-123',0), row('codex-sess-123x',0), row('a12ef',0), row('a12ef',2), row('a12ef',10), row('',0), row('',1), row('real',0,{detail:'mcp__claude-in-chrome__computer'}), row('real',0,{detail:'mcp__computer_use__get_app_state'}), row('real',0,{detail:'',tool_name:'mcp__claude-in-chrome__computer'})];
  assert.deepEqual(countedEvents(rows, 'savings').map(x => x.session_id), ['codex-sess-123x','a12ef','a12ef','','']);
});
test('compression twins require a session and matching sizes and pattern', () => {
  const row = (time, extra = {}) => ({timestamp:`2026-10-01T00:00:${String(time).padStart(2,'0')}Z`,session_id:'real', feature:'build_output_compress',command_pattern:'make',original_tokens:100,compressed_tokens:10,...extra});
  const rows = [row(0), row(2), row(2,{command_pattern:'cargo'}), row(2,{session_id:null}), row(3,{session_id:null}), row(8,{session_id:'f3bench-sess123'})];
  assert.equal(countedEvents(rows,'compression').length,4);
});
test('savings ledger applies exclusions before reporting categories', () => {
  const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
  const {readSavingsEventsByCategory} = require('./savings.ts');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-ledger-'));
  fs.mkdirSync(path.join(dir, 'token-optimizer'));
  const base = {timestamp:'2026-10-01T00:00:00Z',session_id:'real',event_type:'resume_lean',tokens_saved:100,cost_saved_usd:1,detail:'x'};
  const rows = [base,{...base,timestamp:'2026-10-01T00:00:02Z'},{...base,session_id:'test-session-1'},{...base,session_id:'real2',tier:'opportunity'},{...base,session_id:'a123',tokens_saved:50}];
  fs.writeFileSync(path.join(dir,'token-optimizer','savings-events.jsonl'),rows.map(JSON.stringify).join('\n'));
  const result = readSavingsEventsByCategory(dir);
  assert.equal(result.totalTokensSaved,150);
  assert.equal(result.totalCostSavedUsd,2);
});
test('cache-hit changes alone are not credited as savings', () => {
  const fs = require('node:fs'), path = require('node:path');
  const {computeRealizedSavings} = require('./savings.ts');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-history-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  const day=86400000, t=Date.parse('2026-01-01T00:00:00Z'), now=t+60*day;
  const row=(id,ts,read) => ({sessionId:id,ts,model:'opus',input:100000,output:10000,cacheRead:read,cacheWrite:0,cacheWrite1h:0,cacheWrite5m:0,costUsd:1,durationSec:300});
  const history=[row('install',t,0)];
  for(let i=0;i<35;i++) history.push(row('before'+i,t+(2+i/2)*day,0));
  for(let i=0;i<12;i++) history.push(row('after'+i,now-(i+1)*day,900000));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(history));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  assert.equal(result.monthlySavingsUsd,0);
  assert.equal(result.breakdown.some(x => x.key==='context_rereads'),false);
});
test('unpriced sessions do not produce proxy-priced savings', () => {
  const fs = require('node:fs'), path = require('node:path');
  const {computeRealizedSavings} = require('./savings.ts');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-unknown-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000, t=Date.parse('2026-01-01T00:00:00Z'), now=t+60*day;
  const row=(i,ts) => ({sessionId:String(i),ts,model:'unpriced-model-xyz',input:100000,output:1,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const rows=[row('install',t)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  assert.equal(computeRealizedSavings(dir,30,now).ready,false);
});
test('baseline prices known generation at its own card', () => {
  const fs = require('node:fs'), path = require('node:path');
  const {computeRealizedSavings} = require('./savings.ts');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-generation-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts) => ({sessionId:id,ts,model:'claude-opus-5-5',input:100000,output:10000,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const rows=[row('install',t)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  assert.equal(result.beforeCostPerSession,0.6);
});
test('mixed-model sessions price each token class at its actual generation', () => {
  const fs = require('node:fs'), path = require('node:path');
  const {computeRealizedSavings} = require('./savings.ts');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-classes-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const zero={cacheRead:0,cacheWrite:0,cacheWrite1h:0,cacheWrite5m:0};
  const row=(id,ts) => ({sessionId:id,ts,model:'opus',input:100000,output:10000,...zero,costUsd:0,durationSec:300,modelTokenBuckets:{'claude-opus-5-5':{input:100000,output:0,...zero},sonnet:{input:0,output:10000,...zero}}});
  const rows=[row('install',t)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  assert.equal(result.beforeCostPerSession,0.5);
  assert.equal(result.monthlySavingsUsd,0);
});
test('parser retains per-model token classes in a mixed session', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {parseSession}=require('./session-parser.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-parser-'));
  const file=path.join(dir,'s.jsonl');
  const rows=[{type:'assistant',timestamp:'2026-01-01T00:00:00Z',requestId:'a',message:{model:'claude-opus-5-5',usage:{inputTokens:100,outputTokens:20}}},{type:'assistant',timestamp:'2026-01-01T00:02:00Z',requestId:'b',message:{model:'sonnet',usage:{inputTokens:30,outputTokens:40}}}];
  fs.writeFileSync(file,rows.map(JSON.stringify).join('\n'));
  const run=parseSession(file,'agent',dir);
  assert.equal(run.modelTokenBuckets['claude-opus-5-5'].input,100);
  assert.equal(run.modelTokenBuckets.sonnet.output,40);
});
test('premium usage is disclosed without masking cheaper-model routing', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-premium-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model) => ({sessionId:id,ts,model,input:100000,output:10000,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const rows=[row('install',t,'opus')];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day,'opus'));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day,i<6?'fable':'sonnet'));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  assert.ok(result.monthlySavingsUsd>0);
  assert.equal(result.premiumTokens,660000);
  assert.ok(result.premiumCostUsd>0);
});
test('mixed-model cache writes use each model and TTL rate', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-ttl-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts) => ({sessionId:id,ts,model:'opus',input:100000,output:0,cacheRead:0,cacheWrite:100000,cacheWrite1h:50000,cacheWrite5m:50000,costUsd:0,durationSec:300,modelTokenBuckets:{opus:{input:100000,output:0,cacheRead:0,cacheWrite:50000,cacheWrite1h:50000,cacheWrite5m:0},sonnet:{input:0,output:0,cacheRead:0,cacheWrite:50000,cacheWrite1h:0,cacheWrite5m:50000}}});
  const rows=[row('install',t)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  // A bare family id is valued at the family's newest generation card (Opus 5.5 / Sonnet 5.5).
  assert.equal(result.beforeCostPerSession,0.925);
  assert.equal(result.monthlySavingsUsd,0);
});
test('separate cache-write tokens are not capped at fresh input', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-writes-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts) => ({sessionId:id,ts,model:'opus',input:100000,output:0,cacheRead:0,cacheWrite:200000,cacheWrite1h:200000,cacheWrite5m:0,costUsd:0,durationSec:300});
  const rows=[row('install',t)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  assert.equal(computeRealizedSavings(dir,30,now).beforeCostPerSession,2);
});
test('eligible model shares are normalized exactly once for input and 1h writes', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-normalize-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model) => ({sessionId:id,ts,model,input:1000000,output:0,cacheRead:0,cacheWrite:1000000,cacheWrite1h:1000000,cacheWrite5m:0,costUsd:0,durationSec:300});
  const rows=[row('install',t,'opus-5-5')];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day,'opus-5-5'));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day,i<6?'fable':'sonnet'));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  assert.equal(result.beforeCostPerSession,12);
  assert.equal(result.afterCostPerSession,6);
  assert.equal(result.savingsPerSession,6);
});
test('unknown Claude generation is not priced as its family or major generation', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-future-model-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts) => ({sessionId:id,ts,model:'claude-opus-5-99',input:100000,output:10000,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const rows=[row('install',t)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  assert.equal(computeRealizedSavings(dir,30,now).ready,false);
});
test('screenshot tools and differently priced duplicate events follow core identity', () => {
  const row={timestamp:'2026-10-01T00:00:00Z',session_id:'real',event_type:'tool_archive',tokens_saved:100,detail:'replaced mcp__playwright__browser_take_screenshot',cost_saved_usd:1};
  assert.equal(countedEvents([row],'savings').length,0);
  const text={...row,detail:'ordinary output'};
  assert.equal(countedEvents([text,{...text,timestamp:'2026-10-01T00:00:01Z',cost_saved_usd:2}],'savings').length,1);
  assert.equal(countedEvents([{...text,session_id:''},{...text,session_id:'',timestamp:'2026-10-01T00:00:01Z'}],'savings').length,2);
});
test('frozen before cost per session is unchanged by current session volume', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-frozen-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model,n=1)=>({sessionId:id,ts,model,input:100000*n,output:10000*n,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const early=[row('install',t,'opus')];
  for(let i=0;i<35;i++) early.push(row('before'+i,t+(2+i/2)*day,'opus'));
  const history=n=>[...early,...Array.from({length:12},(_,i)=>row('after'+i,now-(i+1)*day,'sonnet',n))];
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(history(1)));
  const first=computeRealizedSavings(dir,30,now);
  const baseline=fs.readFileSync(path.join(state,'baseline-state.json'),'utf8');
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(history(5)));
  const second=computeRealizedSavings(dir,30,now);
  assert.equal(first.beforeCostPerSession,second.beforeCostPerSession);
  assert.equal(fs.readFileSync(path.join(state,'baseline-state.json'),'utf8'),baseline);
});
test('native writer and historical rows use their session pricing and report missing coverage', () => {
  const fs=require('node:fs'),path=require('node:path');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-events-'));
  const {logSavingsEvent}=require('./read-cache.ts');
  const {readSavingsEventsByCategory,measuredSavingsFromSummary}=require('./savings.ts');
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify([{sessionId:'known',model:'opus-5-5',input:100000,output:10,cacheRead:0,cacheWrite:0},{sessionId:'mixed',model:'opus-5-5',modelTokenBuckets:{'opus-5-5':{input:100},'unpriced-model':{input:100}}}]));
  logSavingsEvent('delta_read',1000000,'known','new',{openclawDir:dir});
  fs.appendFileSync(path.join(state,'savings-events.jsonl'),JSON.stringify({timestamp:'2026-10-01T00:00:00Z',session_id:'known',event_type:'delta_read',tokens_saved:1000000,detail:'old'})+'\n');
  fs.appendFileSync(path.join(state,'savings-events.jsonl'),JSON.stringify({timestamp:'2026-10-01T00:00:00Z',session_id:'mixed',event_type:'delta_read',tokens_saved:1000000,detail:'mixed'})+'\n');
  fs.appendFileSync(path.join(state,'savings-events.jsonl'),JSON.stringify({timestamp:'2026-10-01T00:00:00Z',session_id:'missing',event_type:'delta_read',tokens_saved:1000000,detail:'unknown'})+'\n');
  const result=readSavingsEventsByCategory(dir);
  assert.equal(result.totalTokensSaved,4000000);
  assert.equal(result.totalCostSavedUsd,10);
  assert.equal(measuredSavingsFromSummary(result),10);
  assert.equal(result.unpricedTokens,1500000);
  assert.equal(result.unpricedEvents,2);
  const first=JSON.parse(fs.readFileSync(path.join(state,'savings-events.jsonl'),'utf8').split('\n')[0]);
  assert.equal(first.model,'opus-5-5');
  assert.equal(first.cost_saved_usd,4);
});
test('premium additions do not dilute eligible routing or change its frozen footprint', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-premium-equal-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model)=>({sessionId:id,ts,model,input:100000,output:10000,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const early=[row('install',t,'opus')];
  for(let i=0;i<35;i++) early.push(row('before'+i,t+(2+i/2)*day,'opus'));
  fs.writeFileSync(path.join(state,'savings-events.jsonl'), JSON.stringify({timestamp:new Date(now).toISOString(),event_type:'checkpoint_restore',session_id:'after0',tokens_saved:100000,cost_saved_usd:1,detail:'fixed'})+'\n');
  const history=(extra)=>[...early,...Array.from({length:12},(_,i)=>{
    const r=row('after'+i,now-(i+1)*day,'sonnet');
    if(extra) {r.input*=2;r.output*=2;r.modelTokenBuckets={sonnet:{input:100000,output:10000,cacheRead:0,cacheWrite:0,cacheWrite1h:0,cacheWrite5m:0},fable:{input:100000,output:10000,cacheRead:0,cacheWrite:0,cacheWrite1h:0,cacheWrite5m:0}};}
    return r;
  })];
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(history(false)));
  const base=computeRealizedSavings(dir,30,now);
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(history(true)));
  const premium=computeRealizedSavings(dir,30,now);
  assert.equal(premium.beforeCostPerSession,base.beforeCostPerSession);
  assert.equal(premium.afterCostPerSession,base.afterCostPerSession);
  assert.equal(premium.monthlySavingsUsd,base.monthlySavingsUsd);
  assert.equal(premium.sessionsPerMonth,base.sessionsPerMonth);
  assert.ok(premium.premiumTokens>0);
});
test('premium-only sessions do not increase eligible routing session count', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-premium-only-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model)=>({sessionId:id,ts,model,input:100000,output:10000,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const early=[row('install',t,'opus')];
  for(let i=0;i<35;i++) early.push(row('before'+i,t+(2+i/2)*day,'opus'));
  const after=Array.from({length:12},(_,i)=>row('after'+i,now-(i+1)*day,'sonnet'));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify([...early,...after]));
  const base=computeRealizedSavings(dir,30,now);
  const premium=Array.from({length:6},(_,i)=>row('premium'+i,now-(i+1)*day,'fable'));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify([...early,...after,...premium]));
  const changed=computeRealizedSavings(dir,30,now);
  assert.equal(changed.sessionsPerMonth,base.sessionsPerMonth);
  assert.equal(changed.monthlySavingsUsd,base.monthlySavingsUsd);
});
test('an unobserved current class is omitted from both comparison arms', () => {
  const fs=require('node:fs'),path=require('node:path');
  const {computeRealizedSavings}=require('./savings.ts');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-missing-class-'));
  const state=path.join(dir,'token-optimizer');fs.mkdirSync(state);
  const day=86400000,t=Date.parse('2026-01-01T00:00:00Z'),now=t+60*day;
  const row=(id,ts,model,output)=>({sessionId:id,ts,model,input:100000,output,cacheRead:0,cacheWrite:0,costUsd:0,durationSec:300});
  const rows=[row('install',t,'opus',10000)];
  for(let i=0;i<35;i++) rows.push(row('before'+i,t+(2+i/2)*day,'opus',10000));
  for(let i=0;i<12;i++) rows.push(row('after'+i,now-(i+1)*day,'sonnet',0));
  fs.writeFileSync(path.join(state,'session-history.json'),JSON.stringify(rows));
  const result=computeRealizedSavings(dir,30,now);
  assert.equal(result.ready,true);
  assert.equal(result.beforeCostPerSession,0.4);
  assert.equal(result.afterCostPerSession,0.2);
});
test('legacy Claude model pricing accepts only supported generation cards and aliases', () => {
  const path = require('node:path');
  const {resolvePricedModel,priceSavingsEvent} = require('./event-pricing.ts');
  const dir = path.join(os.tmpdir(), 'to-parity-legacy-pricing');
  const known = [
    ['claude-3-sonnet-20240229', 'sonnet-legacy', 3],
    ['anthropic/claude-3-5-sonnet-20241022', 'sonnet-legacy', 3],
    ['anthropic/claude-3-7-sonnet-20250219', 'sonnet-legacy', 3],
    ['anthropic.claude-3-5-sonnet-20241022-v2:0', 'sonnet-legacy', 3],
    ['claude-3-opus-20240229', 'opus-3', 15],
    ['anthropic/claude-3-5-haiku-20241022', 'haiku-3-5', 0.8],
    ['anthropic/claude-opus-5-5-20261001', 'opus-5-5', 4],
  ];
  for (const [model, key, cost] of known) {
    assert.equal(resolvePricedModel(model, dir), key);
    const result = priceSavingsEvent(1000000, 'checkpoint_restore', {model}, dir);
    assert.ok(Math.abs(result.cost - cost) < 1e-10, model);
    assert.equal(result.unpricedTokens, 0);
  }
  for (const model of ['claude-9-opus', 'anthropic/claude-3-9-sonnet-20241022', 'claude-opus-99', 'claude-opus-5-99']) {
    assert.equal(resolvePricedModel(model, dir), null);
    assert.deepEqual(priceSavingsEvent(1000000, 'checkpoint_restore', {model}, dir), {cost: 0, unpricedTokens: 1000000, floorTokens: 0});
  }
});

test('archive coverage retains unknown credits and signed reversals', () => {
  const fs=require('node:fs'), path=require('node:path');
  const {logSavingsEvent}=require('./read-cache.ts');
  const {readSavingsEventsByCategory,measuredSavingsFromSummary}=require('./savings.ts');
  function scenario(credit,debit,amount=1e6,debitFirst=false) {
    const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-debit-'));
    const a=()=>credit && logSavingsEvent('tool_archive',1e6,'credit','credit',{openclawDir:dir,model:credit});
    const b=()=>debit && logSavingsEvent('tool_archive_reexpand',amount,'debit','debit',{openclawDir:dir,model:debit});
    if(debitFirst){b();a();}else{a();b();}
    return readSavingsEventsByCategory(dir);
  }
  for(const order of [false,true]) {
    const x=scenario('opus','unknown-future',1e6,order);
    assert.equal(x.unpricedTokens,1e6);assert.equal(x.unpricedEvents,1);
    assert.equal(x.archivePricedNetUsd,5);assert.equal(x.totalCostSavedUsd,5);assert.equal(x.totalTokensSaved,0);assert.equal(measuredSavingsFromSummary(x),5);
  }
  const both=scenario('unknown-future','unknown-future');
  assert.equal(both.unpricedTokens,2e6);assert.equal(both.unpricedEvents,2);
  const inverse=scenario('unknown-future','sonnet',-1e6);
  assert.equal(inverse.unpricedTokens,1e6);assert.equal(inverse.archivePricedNetUsd,-2);
  assert.equal(measuredSavingsFromSummary(inverse),0);
  assert.equal(scenario(null,'unknown-future').unpricedTokens,1e6);
  assert.equal(scenario('local','local').unpricedEvents,0);
  assert.equal(measuredSavingsFromSummary(scenario('opus','sonnet')),3);
  const dir=fs.mkdtempSync(path.join(os.tmpdir(), 'to-parity-mixed-debit-'));
  logSavingsEvent('tool_archive',1e6,'credit','credit',{openclawDir:dir,model:'opus'});
  logSavingsEvent('tool_archive_reexpand',1e6,'debit1','debit',{openclawDir:dir,model:'sonnet'});
  logSavingsEvent('tool_archive_reexpand',-1e6,'debit2','debit',{openclawDir:dir,model:'sonnet'});
  assert.equal(readSavingsEventsByCategory(dir).archivePricedNetUsd,1);
});
