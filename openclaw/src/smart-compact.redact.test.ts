/**
 * Checkpoint artifacts must not persist credentials.
 *
 * Both capture paths (v1 raw last-N dump, v2 intelligent extraction) wrote
 * message text verbatim into ~/.openclaw checkpoint files, and those files
 * are restored into a later session's context. A key mentioned in chat
 * ("does sk-... work?") landed on disk unredacted. The write boundary now
 * redacts the whole body.
 *
 * checkpoint-policy resolves the checkpoint root lazily via
 * TOKEN_OPTIMIZER_CHECKPOINT_ROOT, so the test scopes a temp root per file —
 * no HOME fakery (that would contaminate every module that bound HOME at
 * import time in this shared test process), nothing written to the real home.
 *
 * Secrets are assembled at runtime so this file contains no literal that
 * matches a real-secret shape (push protection scans test files too).
 */
import { test, expect, afterAll } from "bun:test";
import { mkdirSync, readFileSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { captureCheckpoint, captureCheckpointV2 } from "./smart-compact";

const j = (...parts: string[]) => parts.join("");
const SEC_USER = j("sk-", "A".repeat(24));
const SEC_ASSISTANT = j("ghp_", "C".repeat(36));
const SEC_DECISION = j("xoxb-", "123456789012-", "D".repeat(20));

const tmpRoot = join(
  tmpdir(),
  `openclaw-cp-test-${process.pid}-${Math.random().toString(36).slice(2, 8)}`,
);
mkdirSync(tmpRoot, { recursive: true });
const prevRoot = process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT;
process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = tmpRoot;

const MESSAGES = [
  { role: "user", content: `does ${SEC_USER} work as the api key?` },
  { role: "assistant", content: `I decided to use ${SEC_DECISION} for the deploy hook.` },
  { role: "user", content: `and the PAT is ${SEC_ASSISTANT}` },
];

afterAll(() => {
  if (prevRoot === undefined) delete process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT;
  else process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = prevRoot;
  rmSync(tmpRoot, { recursive: true, force: true });
});

test("v1 checkpoint body redacts secrets in user and assistant text", () => {
  const file = captureCheckpoint(
    { sessionId: "redact-test-v1", messages: MESSAGES },
    20,
    { trigger: "stop" },
  );
  expect(file).not.toBeNull();
  const body = readFileSync(file!, "utf-8");
  for (const secret of [SEC_USER, SEC_ASSISTANT, SEC_DECISION]) {
    expect(body).not.toContain(secret);
  }
  expect(body).toContain("[REDACTED]");
});

test("v2 checkpoint body redacts secrets in extracted sections and messages", () => {
  const file = captureCheckpointV2(
    { sessionId: "redact-test-v2", messages: MESSAGES },
    10,
    { trigger: "stop" },
  );
  expect(file).not.toBeNull();
  const body = readFileSync(file!, "utf-8");
  for (const secret of [SEC_USER, SEC_ASSISTANT, SEC_DECISION]) {
    expect(body).not.toContain(secret);
  }
  expect(body).toContain("[REDACTED]");
});
