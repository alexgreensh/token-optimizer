import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { archive, recover } from '../src/archive.ts';
import { redact } from '../src/redact.ts';
test('archive redacts secrets and recovery checks pointer',()=>{ const root=mkdtempSync(join(tmpdir(),'pi-opt-'));try { const pointer=archive('hello ghp_'+'A'.repeat(36),root); assert.equal(recover(pointer,root),'hello [REDACTED]'); assert.throws(()=>recover('../etc/passwd:0123456789ab',root)); }finally{rmSync(root,{recursive:true,force:true});} });
test('redactor catches bearer and private keys',()=>{ assert.equal(redact('Bearer '+ 'a'.repeat(32)), '[REDACTED]');assert.equal(redact('-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'),'[REDACTED]'); });

test('redaction covers URL secrets, auth flags and common provider tokens',()=>{
 const samples=['https://alice:password@example.com/a', 'postgres://alice:secret@db.example/x',
  'https://example.com/?api_key=abc123', '--password=abc123', 'PGPASSWORD=abc123',
  'npm_'+'A'.repeat(36), 'AIza'+'A'.repeat(35), 'ya29.'+'A'.repeat(25)];
 for(const sample of samples) assert.ok(!redact(sample).includes(sample), `not redacted: ${sample.slice(0,12)}`);
});
