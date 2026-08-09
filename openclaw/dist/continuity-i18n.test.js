"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * GitHub #127 — session continuity blind to non-English prompts (OpenClaw).
 *
 * Mirrors the Python measure.py fix: the topic tokenizer is a two-branch class
 * (ASCII/accented-Latin run OR a whole non-ASCII run) so Korean/Chinese/Japanese
 * and accented Latin stop tokenizing to nothing.
 *
 * The parity fixture (``tests/fixtures/i18n_topic_score_parity.json``) is the
 * SAME JSON asserted by the Python and OpenCode suites, so all three runtimes
 * score identical non-English inputs the same way — including the two documented
 * known limits (Chinese cross-run matching, short-Korean-noun len>3 floor).
 */
const bun_test_1 = require("bun:test");
const continuity_js_1 = require("./continuity.js");
const i18n_topic_score_parity_json_1 = __importDefault(require("../../tests/fixtures/i18n_topic_score_parity.json"));
const I18N_FIXTURE = i18n_topic_score_parity_json_1.default;
(0, bun_test_1.test)("resumeTopicScore matches the shared i18n parity fixture (#127)", () => {
    for (const row of I18N_FIXTURE) {
        const score = (0, continuity_js_1.resumeTopicScore)(row.prompt, row.checkpoint);
        (0, bun_test_1.expect)(score > 0).toBe(row.expect_match);
    }
});
(0, bun_test_1.test)("keywordRelevanceScore matches the shared i18n parity fixture (#127)", () => {
    // Third changed site, live in cross-session scoring. Pass checkpoint as precomputedContent
    // so no file I/O is needed. Same rows, same verdicts as Python + resumeTopicScore.
    for (const row of I18N_FIXTURE) {
        const score = (0, continuity_js_1.keywordRelevanceScore)(row.prompt, "unused-path", row.checkpoint);
        (0, bun_test_1.expect)(score > 0).toBe(row.expect_match);
    }
});
//# sourceMappingURL=continuity-i18n.test.js.map