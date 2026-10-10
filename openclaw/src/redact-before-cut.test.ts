/**
 * Redact BEFORE truncating (openclaw). A secret that straddles a width cut
 * (2000 / 1500 / 3000 chars in the checkpoint writers, 200 in the telemetry
 * and savings detail) used to survive as a prefix no pattern recognises:
 * "ghp_" + 12 chars is shorter than the token shape.
 *
 * Secrets are assembled at runtime so this file contains no literal that
 * matches a real-secret shape (push protection scans test files too).
 */
import { test, expect, afterAll } from "bun:test";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { captureCheckpoint, captureCheckpointV2 } from "./smart-compact";

const j = (...parts: string[]) => parts.join("");
const TOKEN = j("ghp_", "Q7r8T9u0V1w2X3y4Z5a6B7c8D9e0F1g2H3i4");
const FRAG = "Q7r8T9u0";

const tmpRoot = mkdtempSync(join(tmpdir(), `openclaw-cut-${process.pid}-`));
const prevRoot = process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT;
process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = tmpRoot;
afterAll(() => {
  if (prevRoot === undefined) delete process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT;
  else process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = prevRoot;
  rmSync(tmpRoot, { recursive: true, force: true });
});

/** Text whose character at index `cut` falls `inside` chars into the token. */
const straddle = (cut: number, inside: number) => "a".repeat(cut - inside) + " " + TOKEN + " tail";

function persisted(file: string | null): string {
  expect(file).not.toBeNull();
  const dir = join(file!, "..");
  return readdirSync(dir).map((f) => readFileSync(join(dir, f), "utf-8")).join("\n");
}

test("v1 checkpoint body: a secret straddling the 2000-char message cut is redacted", () => {
  const file = captureCheckpoint(
    { sessionId: "cut-v1", messages: [{ role: "user", content: straddle(2000, 12) }] },
    20,
    { trigger: "stop" },
  );
  const text = persisted(file);
  expect(text).not.toContain(FRAG);
  expect(text).not.toContain("ghp_");
});

test("v2 checkpoint body: a secret straddling the 1500-char message cut is redacted", () => {
  const file = captureCheckpointV2(
    { sessionId: "cut-v2", messages: [{ role: "user", content: straddle(1500, 12) }] },
    10,
    { trigger: "stop" },
  );
  const text = persisted(file);
  expect(text).not.toContain(FRAG);
  expect(text).not.toContain("ghp_");
});

test("v2 extraction: a decision line cut at the 3000-char sample is redacted", () => {
  // The decision line is short (extraction ignores lines >= 500 chars) and sits
  // after 2970 chars of filler, so the sample cut lands 12 chars into the token.
  const content = "y".repeat(2970) + "\n" + "I decided to use " + TOKEN + " for auth";
  const file = captureCheckpointV2(
    { sessionId: "cut-v2-sample", messages: [{ role: "assistant", content }] },
    10,
    { trigger: "stop" },
  );
  const text = persisted(file);
  expect(text).not.toContain(FRAG);
});

// telemetry.ts reads HOME at import time and read-cache.ts writes under
// ~/.openclaw, so these run in a child process with a throwaway HOME.
function runInChild(script: string): string {
  const home = mkdtempSync(join(tmpdir(), `openclaw-cut-home-${process.pid}-`));
  try {
    const proc = Bun.spawnSync([process.execPath, "-e", script], {
      cwd: __dirname,
      env: { ...process.env, HOME: home, USERPROFILE: home, TOKEN_OPTIMIZER_FRAG_TOKEN: TOKEN },
    });
    expect(proc.exitCode).toBe(0);
    const dir = join(home, ".openclaw", "token-optimizer");
    return readdirSync(dir).map((f) => readFileSync(join(dir, f), "utf-8")).join("\n");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
}

test("telemetry detail: a secret straddling the 200-char cut is redacted", () => {
  const out = runInChild(`
    const { logCompressionEvent } = require("./telemetry");
    const tok = process.env.TOKEN_OPTIMIZER_FRAG_TOKEN;
    const detail = "a".repeat(200 - 12) + " " + tok;
    // the token starts at 189; the cut at 200 lands 11 chars in
    logCompressionEvent({ feature: "x", originalTokens: 10, compressedTokens: 5, detail });
  `);
  expect(out).not.toContain(FRAG.slice(0, 6));
  expect(out).not.toContain("ghp_");
});

test("savings detail: a secret straddling the 200-char cut is redacted", () => {
  const out = runInChild(`
    const { logSavingsEvent } = require("./read-cache");
    const tok = process.env.TOKEN_OPTIMIZER_FRAG_TOKEN;
    logSavingsEvent("checkpoint_restore", 10, "sess", "a".repeat(200 - 12) + " " + tok);
  `);
  expect(out).not.toContain(FRAG.slice(0, 6));
  expect(out).not.toContain("ghp_");
});
