/**
 * Pre-existing continuity bugs surfaced by the #127 torture-room gauntlet (OpenCode).
 * Not caused by the i18n change; fixed alongside it at Alex's request.
 */
import { test, expect } from "bun:test";
import { resumeIntent } from "./resume-lean.js";

test("'continue from ...' is recognized as resume intent (was dropped from the TS port)", () => {
  // Python _RESUME_INTENT_RE had the `from` branch; both TS ports had lost it, so
  // "continue from where we left off" silently did not trigger resume.
  expect(resumeIntent("continue from where we left off")).toBe(true);
  expect(resumeIntent("continue from the checkpoint")).toBe(true);
  // Guard: a bare "from" is still not a cue (avoids "import data from checkpoint file").
  expect(resumeIntent("import data from checkpoint file")).toBe(false);
});

// The \z topic-summary bug is internal (buildLeanResumeContext); assert the regex directly
// since JS has no \z and the old literal-"z" truncated summaries at their first "z".
test("topic-summary terminator captures past a 'z' and to end-of-input (\\z fix)", () => {
  const re = /^## Topic Summary\s*\n([\s\S]*?)(?:^##|$(?![\s\S]))/m;
  expect("## Topic Summary\namazing refactor\n## Next".match(re)?.[1]?.trim()).toBe("amazing refactor");
  expect("## Topic Summary\nno trailing heading here".match(re)?.[1]?.trim()).toBe("no trailing heading here");
});
