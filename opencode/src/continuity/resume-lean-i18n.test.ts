/**
 * Session continuity blind to non-English prompts (OpenCode).
 *
 * Mirrors the Python measure.py fix: the topic tokenizer is a two-branch class
 * (ASCII/accented-Latin run OR a whole non-ASCII run) so non-Latin scripts
 * and accented Latin stop tokenizing to nothing.
 *
 * The parity fixture (``tests/fixtures/i18n_topic_score_parity.json``) is the
 * SAME JSON asserted by the Python and OpenClaw suites, so all three runtimes
 * score identical non-English inputs the same way — including the documented
 * Chinese cross-run segmentation limit.
 */
import { test, expect } from "bun:test";
import { resumeTopicScore } from "./resume-lean.js";
import i18nFixtureJson from "../../../tests/fixtures/i18n_topic_score_parity.json";

type Row = { name: string; prompt: string; checkpoint: string; expect_match: boolean; why: string };
const I18N_FIXTURE = i18nFixtureJson as Row[];

test("resumeTopicScore matches the shared i18n parity fixture", () => {
  for (const row of I18N_FIXTURE) {
    const score = resumeTopicScore(row.prompt, row.checkpoint);
    expect(score > 0).toBe(row.expect_match);
  }
});
