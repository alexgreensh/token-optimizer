import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { archive, recover, purge, MAX_ARCHIVE_BYTES, MAX_RESULT_BYTES } from '../src/archive.ts';
import { redact } from '../src/redact.ts';
test('archive redacts secrets and recovery checks pointer',()=>{ const root=mkdtempSync(join(tmpdir(),'pi-opt-'));try { const pointer=archive('hello ghp_'+'A'.repeat(36),root); assert.equal(recover(pointer.pointer,root),'hello [REDACTED]'); assert.throws(()=>recover('../etc/passwd:0123456789ab',root)); }finally{rmSync(root,{recursive:true,force:true});} });
test('redactor catches bearer and private keys',()=>{ assert.equal(redact('Bearer '+ 'a'.repeat(32)), '[REDACTED]');assert.equal(redact('-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'),'[REDACTED]'); });

test('redaction covers URL secrets, auth flags and common provider tokens',()=>{
 const samples=['https://alice:password@example.com/a', 'postgres://alice:secret@db.example/x',
  'https://example.com/?api_key=abc123', '--password=abc123', 'PGPASSWORD=abc123',
  'npm_'+'A'.repeat(36), 'AIza'+'A'.repeat(35), 'ya29.'+'A'.repeat(25)];
 for(const sample of samples) assert.ok(!redact(sample).includes(sample), `not redacted: ${sample.slice(0,12)}`);
});

test('generic unknown secrets, oversized and quota-bound output are not persisted',()=>{
 const root=mkdtempSync(join(tmpdir(),'pi-secret-'));try {
  assert.equal(archive('CUSTOM_SECRET=notknownproviderlongvalue12345',root),undefined);
  assert.equal(archive('myToken: notknownproviderlongvalue12345',root),undefined);
  assert.equal(archive('JWT=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123abcd',root),undefined);
  assert.equal(archive('payload '+ 'A'.repeat(44)+'\nCUSTOM_TOKEN=boundarysplitsecret1234567890\n'+'Z'.repeat(44),root),undefined);
  assert.equal(archive('x'.repeat(MAX_RESULT_BYTES+1),root),undefined);
  assert.equal(readdirSync(root).length,0);
  const one=archive('safe text',root); assert.equal(recover(one.pointer,root),'safe text');
  assert.equal(purge('archives',root),1); assert.throws(()=>recover(one.pointer,root));
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('total archive quota fails closed without mutating original text',()=>{
 const root=mkdtempSync(join(tmpdir(),'pi-quota-'));try {
  const block=('repeated line: abcdefghijklmnopqrstuvwxyz\n').repeat(1400);
  let created=0;
  while (archive(block,root)) created++;
  assert.ok(created>100 && created<250);
  assert.ok(created*Buffer.byteLength(block)<=MAX_ARCHIVE_BYTES);
  assert.equal(archive(block,root),undefined);
  assert.equal(purge('archives',root),created);
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('secret-named keys reject quoted, short, multiline, JSON and YAML values',()=>{
 const samples=[
  'CUSTOM_SECRET="a very long secret value"',
  'CUSTOM_SECRET=x',
  'CUSTOM_SECRET="a\nvery long secret value"',
  '{"CUSTOM_SECRET": "a very long secret value"}',
  'CUSTOM_SECRET: a very long secret value',
  'api-key: short',
  'myToken: a very long secret value',
  'password =',
  'API Key: abcd123456789',
  'Access Token = x',
  'Client Secret: a very long secret',
  '<password>secret123</password>',
  'password abc123456789',
 ];
 const root=mkdtempSync(join(tmpdir(),'pi-shapes-'));try {
  for (const value of samples) assert.equal(archive(value,root),undefined,`persisted: ${value}`);
  assert.equal(readdirSync(root).length,0);
 }finally{rmSync(root,{recursive:true,force:true});}
});
