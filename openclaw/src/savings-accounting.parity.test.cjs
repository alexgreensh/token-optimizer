const { test } = require('node:test');
const assert = require('node:assert/strict');
const { countedEvents } = require('./counted-events.ts');

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
  const fs = require('node:fs'), path = require('node:path');
  const {readSavingsEventsByCategory} = require('./savings.ts');
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-parity', 'ledger-'));
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
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-parity', 'history-'));
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
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-parity', 'unknown-'));
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
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-parity', 'generation-'));
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
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-parity', 'classes-'));
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
  const dir=fs.mkdtempSync(path.join(process.cwd(),'.scratch-parity','parser-'));
  const file=path.join(dir,'s.jsonl');
  const rows=[{type:'assistant',timestamp:'2026-01-01T00:00:00Z',requestId:'a',message:{model:'claude-opus-5-5',usage:{inputTokens:100,outputTokens:20}}},{type:'assistant',timestamp:'2026-01-01T00:02:00Z',requestId:'b',message:{model:'sonnet',usage:{inputTokens:30,outputTokens:40}}}];
  fs.writeFileSync(file,rows.map(JSON.stringify).join('\n'));
  const run=parseSession(file,'agent',dir);
  assert.equal(run.modelTokenBuckets['claude-opus-5-5'].input,100);
  assert.equal(run.modelTokenBuckets.sonnet.output,40);
});
