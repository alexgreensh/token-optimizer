import {test} from 'node:test';
import {strict as assert} from 'node:assert';
import {audit} from '../src/audit.ts';
import {rollup} from '../src/rollup.ts';
test('audit flags repeated tool calls without inventing savings',()=>{
 const e=[{id:'1',parentId:null,type:'message',message:{role:'assistant',content:[{type:'toolCall',name:'read',arguments:{path:'a'}},{type:'toolCall',name:'read',arguments:{path:'a'}}]}}, {id:'2',parentId:'1',type:'message',message:{role:'toolResult',content:[{type:'text',text:'x'.repeat(13000)}]}}];
 const a=audit(e,rollup(e));assert.equal(a.repeatedToolCalls,1);assert.equal(a.largeToolResults,1);assert.equal(a.savingsVerified,0);
});
