/**
 * Pre-existing continuity bugs surfaced by the #127 torture-room gauntlet (OpenCode).
 * Not caused by the i18n change; fixed alongside it at Alex's request.
 */
import { test, expect } from "bun:test";
import { resumeIntent, resumeTopicScore, buildLeanResumeContext, type CheckpointRow } from "./resume-lean.js";

test("resumeTopicScore strips ALL intent phrases, not just the first (parity with Python re.sub)", () => {
  // "resume the conversation" is a SECOND intent phrase; a non-global .replace stripped only
  // the first ("continue on"), leaking "conversation" into the topic set (score 1.0 -> 0.5)
  // and diverging from Python's re.sub. Must be 1.0 (only "parser" is a real topic token).
  expect(resumeTopicScore("continue on the parser, resume the conversation", "parser module notes")).toBe(1.0);
});

test("'continue from ...' is recognized as resume intent (was dropped from the TS port)", () => {
  // Python _RESUME_INTENT_RE had the `from` branch; both TS ports had lost it, so
  // "continue from where we left off" silently did not trigger resume.
  expect(resumeIntent("continue from where we left off")).toBe(true);
  expect(resumeIntent("continue from the checkpoint")).toBe(true);
  // Guard: a bare "from" is still not a cue (avoids "import data from checkpoint file").
  expect(resumeIntent("import data from checkpoint file")).toBe(false);
});

function cpWith(content: string): CheckpointRow {
  return {
    session_id: "testsess1",
    trigger: "manual",
    mode: "lean",
    quality_score: null,
    fill_pct: null,
    active_files: JSON.stringify([]),
    decisions: JSON.stringify([]),
    content,
    created_at: Math.floor(Date.now() / 1000),
    dbPath: "/tmp/test-resume-lean.db",
  };
}

test("topic summary is captured past a 'z' and to end-of-input (\\z fix, via production)", () => {
  // Exercises the PRODUCTION extractor, not a re-declared regex: the old JS-invalid `\z`
  // matched a literal "z", truncating "amazing" -> "ama". Reverting the fix reddens this.
  const block = buildLeanResumeContext(cpWith("## Topic Summary\namazing refactor\n## Next"), "testsess1");
  expect(block).toContain("amazing refactor");
  // ...and captures to end-of-input when there is no trailing "## " heading.
  const block2 = buildLeanResumeContext(cpWith("## Topic Summary\nno trailing heading here"), "testsess1");
  expect(block2).toContain("no trailing heading here");
});
