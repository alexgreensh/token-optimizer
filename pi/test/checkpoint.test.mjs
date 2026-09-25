import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { checkpointFromBranch, writeCheckpoint, readCheckpoint } from '../src/checkpoint.ts';
test('branch checkpoints do not cross into sibling forks',()=>{const root=mkdtempSync(join(tmpdir(),'cp-'));try{const c=checkpointFromBranch('s','leaf-a',['Goal: fix src/a.ts','Next: test']);writeCheckpoint(c,root);assert.deepEqual(readCheckpoint('s','leaf-a',root),c);assert.equal(readCheckpoint('s','leaf-b',root),undefined);}finally{rmSync(root,{recursive:true,force:true});}});

test('historical goals and retractions are not revived',()=>{
 assert.deepEqual(checkpointFromBranch('s','a',['Goal: old','Cancel that goal']).goals,[]);
 assert.deepEqual(checkpointFromBranch('s','a',['Goal: old','Next: new task']).goals,['Next: new task']);
 assert.deepEqual(checkpointFromBranch('s','a',['Goal: old','normal user prompt']).goals,[]);
 assert.deepEqual(checkpointFromBranch('s','a',['Goal: CUSTOM_SECRET=supersecretkey123456789']).goals,[]);
});

test('checkpoint skips secret assignments even when caller provides them directly',()=>{
 const root=mkdtempSync(join(tmpdir(),'cp-secrets-'));try {
  for(const value of ['CUSTOM_SECRET="a very long secret value"','CUSTOM_SECRET=x','CUSTOM_SECRET="a\nvery long secret value"','{"CUSTOM_SECRET": "multi word"}','CUSTOM_SECRET: multiline\n  value','API Key: abcd123456789','<password>secret123</password>','password abc123456789','Client Secret: some spaced value','ACCESS_KEY=abcd123456789','SESSION_KEY=abcd123456789','AWS_ACCESS_KEY_ID=AKIA1234567890123456','aPi\tKeY: abcd123456789','API\u00A0Key: abcd123456789','vault.token=abcdef1234567890']) {
    const cp=checkpointFromBranch('s','leaf',['Goal: '+value]);
    assert.deepEqual(cp.goals,[],`candidate: ${value}`);
    assert.deepEqual(cp.files,[]);
    assert.equal(writeCheckpoint({...cp,goals:[value]},root),undefined,`written: ${value}`);
  }
  assert.equal(readCheckpoint('s','leaf',root),undefined);
 }finally{rmSync(root,{recursive:true,force:true});}
});
