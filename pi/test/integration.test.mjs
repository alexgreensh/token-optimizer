import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import extension from '../extensions/token-optimizer.ts';
import { saveSettings } from '../src/state.ts';
const root = mkdtempSync(join(tmpdir(),'pi-integ-'));
process.env.TOKEN_OPTIMIZER_PI_HOME = root;
const handlers = {}, commands = {};
extension({ on: (name, fn) => { handlers[name]=fn; }, registerCommand: (name, spec) => { commands[name]=spec; } });
const notices=[];
let branch=[];
const ctx = { ui: { notify:(msg, kind)=>notices.push({msg,kind}), confirm: async()=>true }, hasUI:false, mode:'print',
 sessionManager: { getBranch:()=>branch, getSessionId:()=> 's1', getSessionFile:()=>'/tmp/pi-session', },
 getContextUsage:()=>({percent:80}), modelRegistry:{find:()=>({cost:{input:3, output:15, cacheRead:.3, cacheWrite:3.75}})} };
const entry=(id,parentId,type,message)=>({id,parentId,type,message});
test('disabled by default, command toggles, no out-of-scope model mutations', async()=>{
 assert.equal(handlers.tool_result({ toolCallId:'a',toolName:'read',isError:false,content:[{type:'text',text:'x'.repeat(20000)}]},ctx),undefined);
 await commands['token-optimizer'].handler('enable',ctx);
 await commands['token-optimizer'].handler('archive on',ctx);
 assert.ok(notices.some(n=>n.msg.includes('enabled')));
});
test('trim on consent archives redacted recovery, does not repeat transform', async()=>{
 const event={toolCallId:'a',toolName:'read',isError:false,content:[{type:'text',text:'ghp_'+'A'.repeat(40)+'x'.repeat(20000)}]};
 const result=handlers.tool_result(event,ctx);
 assert.ok(result.content[0].text.includes('recover with /token-optimizer recover'));
 assert.ok(!result.content[0].text.includes('ghp_')); assert.ok(result.content[0].text.includes('[REDACTED]'));
 assert.equal(handlers.tool_result(event,ctx),undefined);
 const pointer=result.content[0].text.match(/recover (\d+-[0-9a-f-]+:[0-9a-f]{12})/)[1];
 await commands['token-optimizer'].handler('recover '+pointer,ctx);
 assert.ok(notices.at(-1).msg.includes('[REDACTED]'));
 assert.ok(!notices.at(-1).msg.includes('ghp_'));
});
test('read-cache recognizes unchanged file and invalidates on edit',()=>{
 const path=join(root,'sample.txt'); writeFileSync(path,'same');
 handlers.tool_result({toolCallId:'cache-a',toolName:'read',input:{path},isError:false,content:[{type:'text',text:'same'}]}, {...ctx,cwd:root});
 handlers.tool_call({toolName:'read',input:{path}}, {...ctx,cwd:root});
 assert.ok(notices.at(-1).msg.includes('repeated unchanged'));
 handlers.tool_call({toolName:'edit',input:{path}}, {...ctx,cwd:root});
 const n=notices.length;
 handlers.tool_call({toolName:'read',input:{path}}, {...ctx,cwd:root});
 assert.equal(notices.length,n);
});
test('usage branch switch excludes inactive fork and estimates only missing costs', async()=>{
 const ua={ input:10,output:2,cacheRead:0,cacheWrite:0,totalTokens:12,cost:{total:1} };
 branch=[entry('a',null,'message',{role:'assistant',provider:'anthropic',model:'opus',usage:ua}),entry('b','a','message',{role:'assistant',provider:'anthropic',model:'opus',usage:{...ua,cost:undefined}})];
 await commands['token-optimizer'].handler('usage',ctx);
 assert.match(notices.at(-1).msg,/native cost \$1.0000; estimated/);
 branch=[branch[0],branch[1],entry('c','a','message',{role:'assistant',provider:'anthropic',model:'haiku',usage:ua})];
 // Branch is supplied by Pi's manager, which returns only active ancestry.
 ctx.sessionManager.getBranch=()=>[branch[0],branch[2]];
 await commands['token-optimizer'].handler('usage',ctx);
 assert.ok(notices.at(-1).msg.includes('haiku'));
 assert.ok(!notices.at(-1).msg.includes('estimated'));
});
test('native compaction is not replaced; branch-scoped markers injected once after success',()=>{
 const event={branchEntries:[entry('u',null,'message',{role:'user',content:[{type:'text',text:'Goal: fix src/a.ts'}]})]};
 saveSettings({enabled:true,archiveToolOutput:true,continuity:true,retainDays:7},root);
 assert.equal(handlers.session_before_compact(event,ctx),undefined);
 assert.equal(handlers.session_compact({compactionEntry:{parentId:'u'}},ctx),undefined);
 const injected=handlers.before_agent_start({},ctx);
 assert.match(injected.message.content,/Goal: fix src\/a.ts/);
 assert.equal(handlers.before_agent_start({},ctx),undefined);
 assert.equal(handlers.session_compact({compactionEntry:{parentId:'other'}},ctx),undefined);
 assert.equal(handlers.before_agent_start({},ctx),undefined);
});
test('disabled continuity filters old injected markers from model context',()=>{
 saveSettings({enabled:false,archiveToolOutput:false,continuity:false,retainDays:7},root);
 const old={role:'custom',customType:'token-optimizer-continuity',content:'Goal: old',display:false};
 const normal={role:'user',content:'new'};
 assert.deepEqual(handlers.context({messages:[old,normal]},ctx).messages,[normal]);
});
test('cleanup',()=>{ rmSync(root,{recursive:true,force:true}); });
