/**
 * Shared redaction vectors (tests/fixtures/redaction_vectors.json): the same table
 * runs against the Python redactor and the other TS copies, so the four stay
 * equivalent. Fragments are joined at runtime; the fixture holds no secret shapes.
 */
import { test, expect } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { redact } from "./util/redact";

type Vector = { name: string; input: string[]; absent: string[]; present: string[] };
const { vectors } = JSON.parse(
  readFileSync(join(import.meta.dir, "../..", "tests", "fixtures", "redaction_vectors.json"), "utf8"),
) as { vectors: Vector[] };

for (const v of vectors) {
  test(`opencode redactor matches shared vector: ${v.name}`, () => {
    const out = redact(v.input.join(""));
    for (const needle of v.absent) expect(out.includes(needle)).toBe(false);
    for (const needle of v.present) expect(out.includes(needle)).toBe(true);
  });
}
