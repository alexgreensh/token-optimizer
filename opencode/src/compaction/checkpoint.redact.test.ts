/**
 * Checkpoint rows must not persist credentials.
 *
 * captureCheckpoint stores user-message-derived text (topic summary),
 * file paths, and quality-cache decisions into the checkpoints table, and
 * the row's content is later injected into a fresh session's context.
 * Before the fix none of those strings passed a credential filter, so a
 * token in a user message ("use sk-... for the request") or a decision
 * quoting a PAT landed verbatim in the DB. The write boundary now redacts
 * every persisted string.
 *
 * Secrets are assembled at runtime so this file contains no literal that
 * matches a real-secret shape (push protection scans test files too).
 */
import { test, expect, afterEach } from "bun:test";
import { mkdirSync, rmSync } from "fs";
import { join } from "path";
import { SessionStore } from "../storage/session-store.js";
import { captureCheckpoint } from "./checkpoint.js";

const j = (...parts: string[]) => parts.join("");
const SEC_TOPIC = j("sk-", "A".repeat(24));
const SEC_DECISION = j("ghp_", "C".repeat(36));
const SEC_PATH = j("xoxb-", "123456789012-", "D".repeat(20));

const dirs: string[] = [];
function tmpDir(): string {
  const d = join(
    import.meta.dir,
    `.test-tmp-${process.pid}-${dirs.length}-${Math.random().toString(36).slice(2, 8)}`,
  );
  mkdirSync(d, { recursive: true });
  dirs.push(d);
  return d;
}
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

test("checkpoint row redacts secrets in topic, decisions, and file paths", () => {
  const store = new SessionStore(tmpDir(), "sess-redact-test");
  try {
    store.recordWrite(1, `/work/${SEC_PATH}/app.ts`);
    store.writeQualityCache({
      resource_health: 80,
      session_efficiency: 80,
      fill_pct: 0.5,
      compactions: 0,
      tool_calls: 1,
      last_nudge_time: 0,
      nudge_count: 0,
      data: JSON.stringify({ decisions: [`decided to use ${SEC_DECISION} for auth`] }),
    });

    const cp = captureCheckpoint(
      store,
      "sess-redact-test",
      "stop",
      "general",
      80,
      0.5,
      [`please route this through ${SEC_TOPIC} and tell me if it works`],
    );

    const row = store
      .connect()
      .query("SELECT active_files, decisions, content FROM checkpoints ORDER BY id DESC LIMIT 1")
      .get() as { active_files: string; decisions: string; content: string };

    for (const secret of [SEC_TOPIC, SEC_DECISION, SEC_PATH]) {
      expect(row.content).not.toContain(secret);
      expect(row.decisions).not.toContain(secret);
      expect(row.active_files).not.toContain(secret);
      // The returned Checkpoint object is injected into context too.
      expect(cp.content).not.toContain(secret);
    }
    expect(row.content).toContain("[REDACTED]");
  } finally {
    store.close();
  }
});
