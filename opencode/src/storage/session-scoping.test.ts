/**
 * Tests for per-project session scoping.
 *
 * The scoping argument threaded through SessionStore / restoreCheckpoint /
 * buildResumeLeanBlock is what actually keeps one project's checkpoints out of
 * another's context. It is an optional trailing parameter, so a refactor that
 * drops it fails open (everything shares one directory again) rather than
 * throwing. These tests exist so that regression is loud.
 */
import { test, expect } from "bun:test";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { mkdtempSync, existsSync } from "node:fs";
import { SessionStore } from "./session-store.js";

const PROJECT_A = "-Users-alex-project-a";
const PROJECT_B = "-Users-alex-project-b";

function freshDataDir(): string {
  return mkdtempSync(join(tmpdir(), "to-scope-"));
}

test("SessionStore writes into the per-project subdirectory", () => {
  const dataDir = freshDataDir();
  new SessionStore(dataDir, "sess-1", PROJECT_A);

  expect(existsSync(join(dataDir, "token-optimizer", "sessions", PROJECT_A))).toBe(true);
});

test("two projects never share a session directory", () => {
  const dataDir = freshDataDir();
  new SessionStore(dataDir, "sess-1", PROJECT_A);
  new SessionStore(dataDir, "sess-2", PROJECT_B);

  const sessions = join(dataDir, "token-optimizer", "sessions");
  expect(existsSync(join(sessions, PROJECT_A))).toBe(true);
  expect(existsSync(join(sessions, PROJECT_B))).toBe(true);
});

test("omitting the slug keeps the legacy unscoped layout", () => {
  // Backwards compatibility: the parameter is optional by design so existing
  // two-argument callers keep working.
  const dataDir = freshDataDir();
  new SessionStore(dataDir, "sess-1");

  expect(existsSync(join(dataDir, "token-optimizer", "sessions"))).toBe(true);
});

/*
 * DELIBERATELY NOT TESTED HERE: restoreCheckpoint / buildResumeLeanBlock
 * returning nothing for a foreign slug.
 *
 * The obvious version of that test (seed project A's directory, query with
 * project B's slug, assert empty) passes even when the slug argument is dropped
 * entirely, because an unscoped read of a directory with no valid checkpoint DBs
 * also returns empty. Verified empirically before deleting it. A test that
 * cannot fail is worse than no test: it is exactly the false confidence that let
 * the flat-migration bug through in the first place.
 *
 * Covering this properly needs a real SQLite checkpoint fixture written through
 * SessionStore. Tracked as follow-up work rather than faked here.
 */
