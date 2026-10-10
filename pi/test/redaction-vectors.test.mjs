// Shared redaction vectors (tests/fixtures/redaction_vectors.json): the same table
// runs against the Python redactor and the opencode/openclaw TS copies.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { redact } from '../src/redact.ts';

const { vectors } = JSON.parse(readFileSync(new URL('../../tests/fixtures/redaction_vectors.json', import.meta.url), 'utf8'));
for (const v of vectors) {
  test(`pi redactor matches shared vector: ${v.name}`, () => {
    const out = redact(v.input.join(''));
    for (const needle of v.absent) assert.ok(!out.includes(needle), `${needle} survived: ${out}`);
    for (const needle of v.present) assert.ok(out.includes(needle), `${needle} was wrongly removed: ${out}`);
  });
}
