/**
 * Legacy Claude-Code-style logs read by OpenClaw: harness-written user records
 * (meta, compact summaries, /compact wrappers, interrupts, bare /model) must
 * not become the session topic (#288 review finding 17).
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { parseSession, topicUserText } from "./session-parser";

let dir: string;
beforeEach(() => { dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-topic-")); });
afterEach(() => { fs.rmSync(dir, { recursive: true, force: true }); });

const assistant = {
  type: "assistant", timestamp: "2026-08-01T10:00:05.000Z",
  message: { model: "claude-opus-4-8", content: [{ type: "text", text: "ok" }],
             usage: { input_tokens: 10, output_tokens: 5 } },
};

function topicOf(lines: unknown[]): string | undefined {
  const p = path.join(dir, "s.jsonl");
  fs.writeFileSync(p, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  return parseSession(p, "agent-a", dir)?.topic;
}

test("harness records before the real prompt never name the session", () => {
  const harness = [
    { type: "user", isMeta: true, message: { content: "Caveat: local commands below" } },
    { type: "user", isCompactSummary: true, message: { content: "Summary of the earlier chat" } },
    { type: "user", message: { content: "<command-name>/compact</command-name><command-args></command-args>" } },
    { type: "user", message: { content: "<local-command-stdout>Compacted</local-command-stdout>" } },
    { type: "user", message: { content: "[Request interrupted by user]" } },
    { type: "user", message: { content: "<command-name>/model</command-name><command-args></command-args>" } },
  ];
  const real = { type: "user", message: { content: "Refactor the billing module" } };
  expect(topicOf([...harness, real, assistant])).toBe("Refactor the billing module");
});

test("real commands and prompts after a system reminder are kept", () => {
  expect(topicUserText({ message: { content:
    "<command-name>/ce:debug</command-name><command-args>fix login</command-args>" } }))
    .toBe("/ce:debug fix login");
  expect(topicUserText({ message: { content:
    "<system-reminder>be brief</system-reminder>\nfix the login bug" } }))
    .toBe("fix the login bug");
  expect(topicUserText({ message: { content: "plain prompt" } })).toBe("plain prompt");
});
