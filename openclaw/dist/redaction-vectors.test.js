"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * Shared redaction vectors (tests/fixtures/redaction_vectors.json): the same table
 * runs against the Python redactor and the other TS copies, so the four stay
 * equivalent. Fragments are joined at runtime; the fixture holds no secret shapes.
 */
const bun_test_1 = require("bun:test");
const fs_1 = require("fs");
const path_1 = require("path");
const redact_1 = require("./redact");
const { vectors } = JSON.parse((0, fs_1.readFileSync)((0, path_1.join)(__dirname, "../..", "tests", "fixtures", "redaction_vectors.json"), "utf8"));
for (const v of vectors) {
    (0, bun_test_1.test)(`openclaw redactor matches shared vector: ${v.name}`, () => {
        const out = (0, redact_1.redact)(v.input.join(""));
        for (const needle of v.absent)
            (0, bun_test_1.expect)(out.includes(needle)).toBe(false);
        for (const needle of v.present)
            (0, bun_test_1.expect)(out.includes(needle)).toBe(true);
    });
}
//# sourceMappingURL=redaction-vectors.test.js.map