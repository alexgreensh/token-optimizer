"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * Regression test for the OpenClaw nested-role session format.
 *
 * OpenClaw writes conversation lines as { type: "message", message: { role } }
 * with the role NESTED, plus type:"session"/"compaction"/"reset" markers
 * (verified against openclaw/skills/session-logs/SKILL.md, whose own jq recipe
 * filters on `.message.role`). The parser previously read a top-level
 * type:"user"/"assistant" (the Claude-Code shape), so every OpenClaw line
 * missed the gate, messageCount stayed 0, parseSession returned null, and the
 * audit reported 0 sessions across an entire OpenClaw install.
 *
 * These tests are load-bearing: before normalizedType(), every "parses the
 * OpenClaw format" assertion below returned null / [] / 0. They also pin the
 * legacy Claude-Code top-level-type path so the fix stays backward compatible.
 */
const bun_test_1 = require("bun:test");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const os = __importStar(require("os"));
const session_parser_1 = require("./session-parser");
const prices_generated_1 = require("./prices.generated");
let dir;
(0, bun_test_1.beforeEach)(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-parser-"));
});
(0, bun_test_1.afterEach)(() => {
    fs.rmSync(dir, { recursive: true, force: true });
});
function writeSession(name, lines) {
    const p = path.join(dir, name);
    fs.writeFileSync(p, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
    return p;
}
// Real OpenClaw transcript: a session header, a nested-role user turn, and a
// nested-role assistant turn carrying usage under message.usage.
const OPENCLAW_LINES = [
    { type: "session", id: "sess-1", cwd: "/tmp/proj", timestamp: "2026-08-01T10:00:00.000Z", version: "2026.6.6" },
    {
        type: "message",
        timestamp: "2026-08-01T10:00:01.000Z",
        id: "m1",
        parentId: "sess-1",
        message: { role: "user", content: [{ type: "text", text: "Refactor the auth module for me please" }] },
    },
    {
        type: "message",
        timestamp: "2026-08-01T10:00:05.000Z",
        id: "m2",
        parentId: "m1",
        message: {
            role: "assistant",
            model: "claude-opus-4-8",
            content: [{ type: "text", text: "Done. Here is the refactor with tests and a summary of the changes." }],
            usage: { inputTokens: 4200, outputTokens: 900, cacheReadInputTokens: 1500 },
        },
    },
];
(0, bun_test_1.test)("parseSession reads the OpenClaw nested-role format (regression)", () => {
    const p = writeSession("sess-1.jsonl", OPENCLAW_LINES);
    const run = (0, session_parser_1.parseSession)(p, "agent-a", dir);
    // Pre-fix this was null (messageCount stayed 0 -> early return).
    (0, bun_test_1.expect)(run).not.toBeNull();
    (0, bun_test_1.expect)(run.system).toBe("openclaw");
    (0, bun_test_1.expect)(run.messageCount).toBe(2); // the session header must NOT count
    (0, bun_test_1.expect)(run.tokens.input).toBe(4200);
    (0, bun_test_1.expect)(run.tokens.output).toBe(900);
    (0, bun_test_1.expect)(run.tokens.cacheRead).toBe(1500);
});
(0, bun_test_1.test)("non-message marker lines (session/compaction/reset) are not counted as turns", () => {
    const p = writeSession("sess-markers.jsonl", [
        { type: "session", id: "s", timestamp: "2026-08-01T10:00:00.000Z" },
        { type: "compaction", firstKeptEntryId: "m1", timestamp: "2026-08-01T10:00:02.000Z" },
        { type: "reset", firstKeptEntryId: "m1", timestamp: "2026-08-01T10:00:03.000Z" },
        ...OPENCLAW_LINES.slice(1), // the two real messages
    ]);
    const run = (0, session_parser_1.parseSession)(p, "agent-a", dir);
    (0, bun_test_1.expect)(run).not.toBeNull();
    (0, bun_test_1.expect)(run.messageCount).toBe(2);
});
(0, bun_test_1.test)("parseSessionTurns pairs a user->assistant turn from the nested-role format", () => {
    const p = writeSession("sess-1.jsonl", OPENCLAW_LINES);
    const turns = (0, session_parser_1.parseSessionTurns)(p, dir);
    // Pre-fix this was [] (no record matched the user/assistant gate).
    (0, bun_test_1.expect)(turns.length).toBeGreaterThanOrEqual(1);
    const withOutput = turns.find((t) => t.outputTokens > 0);
    (0, bun_test_1.expect)(withOutput).toBeDefined();
    (0, bun_test_1.expect)(withOutput.outputTokens).toBe(900);
});
(0, bun_test_1.test)("extractCostlyPrompts surfaces the nested-role user prompt text", () => {
    const p = writeSession("sess-1.jsonl", OPENCLAW_LINES);
    const prompts = (0, session_parser_1.extractCostlyPrompts)(p, 5, dir);
    // Pre-fix this was [] (the user gate never matched).
    (0, bun_test_1.expect)(prompts.length).toBeGreaterThanOrEqual(1);
    (0, bun_test_1.expect)(prompts[0].text).toContain("Refactor the auth module");
});
(0, bun_test_1.test)("legacy Claude-Code top-level-type sessions still parse (backward compat)", () => {
    const legacy = [
        {
            type: "user",
            timestamp: "2026-08-01T10:00:01.000Z",
            message: { role: "user", content: [{ type: "text", text: "hello there" }] },
        },
        {
            type: "assistant",
            timestamp: "2026-08-01T10:00:05.000Z",
            message: {
                role: "assistant",
                model: "claude-opus-4-8",
                content: [{ type: "text", text: "Hi! How can I help with your project today?" }],
                usage: { inputTokens: 300, outputTokens: 120 },
            },
        },
    ];
    const p = writeSession("legacy.jsonl", legacy);
    const run = (0, session_parser_1.parseSession)(p, "agent-a", dir);
    (0, bun_test_1.expect)(run).not.toBeNull();
    (0, bun_test_1.expect)(run.messageCount).toBe(2);
    (0, bun_test_1.expect)(run.tokens.output).toBe(120);
});
(0, bun_test_1.test)("a generation priced unlike its family (Opus 5.5) is billed at its own card", () => {
    // Pre-fix the parser priced every turn by family name ("opus"), so the
    // generation card never applied and Opus 5.5 was billed as Opus 5.
    const lines = OPENCLAW_LINES.map((l) => JSON.parse(JSON.stringify(l)));
    lines[2].message.model = "claude-opus-5-5";
    const p = writeSession("sess-opus55.jsonl", lines);
    const run = (0, session_parser_1.parseSession)(p, "agent-a", dir);
    const card = prices_generated_1.GENERATED_PRICING["opus-5-5"];
    const expected = 4200 * card.input + 900 * card.output + 1500 * card.cacheRead;
    (0, bun_test_1.expect)(card.input).not.toBe(prices_generated_1.GENERATED_PRICING["opus"].input);
    (0, bun_test_1.expect)(run.costUsd).toBeCloseTo(expected, 9);
});
(0, bun_test_1.test)("streamed chunks that carry only a message id count as one request", () => {
    const chunk = (outputTokens) => ({
        type: "assistant",
        timestamp: "2026-08-01T10:00:05.000Z",
        message: {
            id: "msg_abc",
            role: "assistant",
            model: "claude-opus-4-8",
            content: [{ type: "text", text: "Working on it now." }],
            usage: { input_tokens: 1000, output_tokens: outputTokens },
        },
    });
    const p = writeSession("sdk.jsonl", [
        {
            type: "user",
            timestamp: "2026-08-01T10:00:01.000Z",
            message: { role: "user", content: "Refactor the auth module for me please" },
        },
        chunk(100),
        chunk(250),
        chunk(400),
    ]);
    const run = (0, session_parser_1.parseSession)(p, "agent-a", dir);
    (0, bun_test_1.expect)(run).not.toBeNull();
    (0, bun_test_1.expect)(run.tokens.input).toBe(1000);
    (0, bun_test_1.expect)(run.tokens.output).toBe(400);
});
//# sourceMappingURL=session-parser.test.js.map