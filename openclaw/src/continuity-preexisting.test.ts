/**
 * Pre-existing continuity bug surfaced by the #127 torture-room gauntlet (OpenClaw).
 * Not caused by the i18n change; fixed alongside it at Alex's request.
 */
import { test, expect } from "bun:test";
import { isResumeIntent, resumeTopicScore } from "./continuity.js";

test("resumeTopicScore strips ALL intent phrases, not just the first (parity with Python re.sub)", () => {
  // Non-global .replace stripped only the first phrase, leaking a later phrase's content word
  // into the topic set (1.0 -> 0.5), diverging from Python. Must be 1.0.
  expect(resumeTopicScore("continue on the parser, resume the conversation", "parser module notes")).toBe(1.0);
});

test("'continue from ...' is recognized as resume intent (was dropped from the TS port)", () => {
  expect(isResumeIntent("continue from where we left off")).toBe(true);
  expect(isResumeIntent("continue from the checkpoint")).toBe(true);
  expect(isResumeIntent("import data from checkpoint file")).toBe(false);
});
