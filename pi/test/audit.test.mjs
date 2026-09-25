import {test} from 'node:test';
import {strict as assert} from 'node:assert';
import {audit} from '../src/audit.ts';
import {rollup} from '../src/rollup.ts';
test('audit flags repeated tool calls without inventing savings',()=>{
 const e=[{id:'1',parentId:null,type:'message',message:{role:'assistant',content:[{type:'toolCall',name:'read',arguments:{path:'a'}},{type:'toolCall',name:'read',arguments:{path:'a'}}]}}, {id:'2',parentId:'1',type:'message',message:{role:'toolResult',content:[{type:'text',text:'x'.repeat(13000)}]}}];
 const a=audit(e,rollup(e));assert.equal(a.repeatedToolCalls,1);assert.equal(a.largeToolResults,1);assert.equal(a.savingsVerified,0);
});

test('audit reports native subtotal coverage, estimated and unpriced remainder separately',()=>{
 const summary={models:[{calls:3,nativeCost:1.2,estimatedCost:0.03,estimatedCalls:1,unpricedCalls:1}]};
 const a=audit([],summary);
 assert.equal(a.nativeCalls,1);assert.equal(a.totalModelCalls,3);
 assert.equal(a.estimatedCalls,1);assert.equal(a.unpricedCalls,1);
 assert.equal(a.modelCostNative,1.2);assert.equal(a.modelCostEstimated,0.03);
});
