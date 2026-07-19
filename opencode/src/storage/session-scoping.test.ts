/**
 * Tests for per-project session scoping.
 *
 * The scoping argument threaded through SessionStore / restoreCheckpoint /
 * buildResumeLeanBlock is what keeps one project's checkpoints out of another's
 * context. It is an optional trailing parameter, so a refactor that drops it
 * fails OPEN (everything shares one directory again) rather than throwing.
 *
 * The load-bearing test here is the POSITIVE one: with the correct slug a real
 * checkpoint must actually be found. An isolation-only test passes even when the
 * argument is dropped entirely, because an unscoped scan of a directory holding
 * no top-level .db files also returns nothing. Verified empirically. Asserting
 * retrieval works is the only assertion that can fail when scoping breaks.
 */
import { test, expect } from "bun:test";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { mkdtempSync, mkdirSync, existsSync } from "node:fs";
import { Database } from "bun:sqlite";
import { SessionStore } from "./session-store.js";
import { buildResumeLeanBlock } from "../continuity/resume-lean.js";
import { hashProjectDir } from "../util/env.js";

const PROJECT_A = "/Users/alex/project-a";
const PROJECT_B = "/Users/alex/project-b";
const SLUG_A = hashProjectDir(PROJECT_A);
const SLUG_B = hashProjectDir(PROJECT_B);

function freshDataDir(): string {
  return mkdtempSync(join(tmpdir(), "to-scope-"));
}

/** Write a real checkpoint DB in the layout the readers expect. */
function seedCheckpoint(dataDir: string, slug: string, sessionId: string, cwd: string): void {
  const dir = join(dataDir, "token-optimizer", "sessions", slug);
  mkdirSync(dir, { recursive: true });
  const db = new Database(join(dir, `${sessionId}.db`));
  db.exec(`CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    mode TEXT,
    quality_score REAL,
    fill_pct REAL,
    active_files TEXT,
    decisions TEXT,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
  );`);
  db.run(
    `INSERT INTO checkpoints (session_id, trigger, mode, quality_score, fill_pct,
       active_files, decisions, content, created_at) VALUES (?,?,?,?,?,?,?,?,?)`,
    [
      sessionId,
      "manual",
      "lean",
      0.9,
      0.5,
      JSON.stringify([join(cwd, "src", "main.ts")]),
      JSON.stringify(["chose the scoped layout"]),
      "PRIOR SESSION CONTENT",
      Date.now() / 1000,
    ],
  );
  db.close();
}

test("SessionStore writes into the per-project subdirectory", () => {
  const dataDir = freshDataDir();
  new SessionStore(dataDir, "sess-1", SLUG_A);
  expect(existsSync(join(dataDir, "token-optimizer", "sessions", SLUG_A))).toBe(true);
});

test("two projects never share a session directory", () => {
  const dataDir = freshDataDir();
  new SessionStore(dataDir, "sess-1", SLUG_A);
  new SessionStore(dataDir, "sess-2", SLUG_B);
  const sessions = join(dataDir, "token-optimizer", "sessions");
  expect(existsSync(join(sessions, SLUG_A))).toBe(true);
  expect(existsSync(join(sessions, SLUG_B))).toBe(true);
});

test("omitting the slug keeps the legacy unscoped layout", () => {
  // Backwards compatibility: the parameter is optional by design so existing
  // two-argument callers keep working.
  const dataDir = freshDataDir();
  new SessionStore(dataDir, "sess-1");
  expect(existsSync(join(dataDir, "token-optimizer", "sessions"))).toBe(true);
});

test("LOAD-BEARING: a checkpoint in the project's scoped dir IS retrieved", () => {
  // Fails if the slug is dropped, misspelled, or the layout drifts: the reader
  // would scan the unscoped parent, find no top-level .db, and return nothing.
  const dataDir = freshDataDir();
  seedCheckpoint(dataDir, SLUG_A, "old-session", PROJECT_A);

  const [block, sid] = buildResumeLeanBlock(
    "continue the prior work",
    dataDir,
    "new-session",
    PROJECT_A,
    7,
    50,
    SLUG_A,
  );
  expect(sid).toBe("old-session");
  expect(block.length).toBeGreaterThan(0);
});

test("ISOLATION: project B cannot retrieve project A's checkpoint", () => {
  const dataDir = freshDataDir();
  seedCheckpoint(dataDir, SLUG_A, "a-session", PROJECT_A);

  const [block, sid] = buildResumeLeanBlock(
    "continue the prior work",
    dataDir,
    "new-session",
    PROJECT_B,
    7,
    50,
    SLUG_B,
  );
  expect(block).toBe("");
  expect(sid).toBe("");
});

test("ISOLATION holds even when both projects have sessions", () => {
  const dataDir = freshDataDir();
  seedCheckpoint(dataDir, SLUG_A, "a-session", PROJECT_A);
  seedCheckpoint(dataDir, SLUG_B, "b-session", PROJECT_B);

  const [, sidA] = buildResumeLeanBlock("continue the prior work", dataDir, "new", PROJECT_A, 7, 50, SLUG_A);
  const [, sidB] = buildResumeLeanBlock("continue the prior work", dataDir, "new", PROJECT_B, 7, 50, SLUG_B);
  expect(sidA).toBe("a-session");
  expect(sidB).toBe("b-session");
});
