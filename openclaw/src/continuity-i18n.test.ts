/**
 * Session continuity blind to non-English prompts (OpenClaw).
 *
 * Mirrors the Python measure.py fix: the topic tokenizer is a two-branch class
 * (ASCII/accented-Latin run OR a whole non-ASCII run) so non-Latin scripts
 * and accented Latin stop tokenizing to nothing.
 *
 * The parity fixture (``tests/fixtures/i18n_topic_score_parity.json``) is the
 * SAME JSON asserted by the Python and OpenCode suites, so all three runtimes
 * score identical non-English inputs the same way — including the documented
 * Chinese cross-run segmentation limit.
 */
import { test, expect } from "bun:test";
import { resumeTopicScore, keywordRelevanceScore } from "./continuity.js";
import i18nFixtureJson from "../../tests/fixtures/i18n_topic_score_parity.json";

type Row = { name: string; prompt: string; checkpoint: string; expect_match: boolean; why: string };
const I18N_FIXTURE = i18nFixtureJson as Row[];

test("resumeTopicScore matches the shared i18n parity fixture", () => {
  for (const row of I18N_FIXTURE) {
    const score = resumeTopicScore(row.prompt, row.checkpoint);
    expect(score > 0).toBe(row.expect_match);
  }
});

test("keywordRelevanceScore matches the shared i18n parity fixture", () => {
  // Third changed site, live in cross-session scoring. Pass checkpoint as precomputedContent
  // so no file I/O is needed. Same rows, same verdicts as Python + resumeTopicScore.
  for (const row of I18N_FIXTURE) {
    const score = keywordRelevanceScore(row.prompt, "unused-path", row.checkpoint);
    expect(score > 0).toBe(row.expect_match);
  }
});
