import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { checkpointFromBranch, writeCheckpoint, readCheckpoint } from '../src/checkpoint.ts';
test('branch checkpoints do not cross into sibling forks',()=>{const root=mkdtempSync(join(tmpdir(),'cp-'));try{const c=checkpointFromBranch('s','leaf-a',['Goal: fix src/a.ts','Next: test']);writeCheckpoint(c,root);assert.deepEqual(readCheckpoint('s','leaf-a',root),c);assert.equal(readCheckpoint('s','leaf-b',root),undefined);}finally{rmSync(root,{recursive:true,force:true});}});
