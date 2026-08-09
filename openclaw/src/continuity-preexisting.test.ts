/**
 * Pre-existing continuity bug surfaced by the #127 torture-room gauntlet (OpenClaw).
 * Not caused by the i18n change; fixed alongside it at Alex's request.
 */
import { test, expect } from "bun:test";
import { isResumeIntent } from "./continuity.js";

test("'continue from ...' is recognized as resume intent (was dropped from the TS port)", () => {
  expect(isResumeIntent("continue from where we left off")).toBe(true);
  expect(isResumeIntent("continue from the checkpoint")).toBe(true);
  expect(isResumeIntent("import data from checkpoint file")).toBe(false);
});
