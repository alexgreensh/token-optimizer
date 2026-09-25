import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { archive, recover } from '../src/archive.ts';
import { redact } from '../src/redact.ts';
test('archive redacts secrets and recovery checks pointer',()=>{ const root=mkdtempSync(join(tmpdir(),'pi-opt-'));try { const pointer=archive('hello ghp_'+'A'.repeat(36),root); assert.equal(recover(pointer,root),'hello [REDACTED]'); assert.throws(()=>recover('../etc/passwd:0123456789ab',root)); }finally{rmSync(root,{recursive:true,force:true});} });
test('redactor catches bearer and private keys',()=>{ assert.equal(redact('Bearer '+ 'a'.repeat(32)), '[REDACTED]');assert.equal(redact('-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'),'[REDACTED]'); });
