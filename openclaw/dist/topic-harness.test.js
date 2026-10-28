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
 * Legacy Claude-Code-style logs read by OpenClaw: harness-written user records
 * (meta, compact summaries, /compact wrappers, interrupts, bare /model) must
 * not become the session topic (#288 review finding 17).
 */
const bun_test_1 = require("bun:test");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const os = __importStar(require("os"));
const session_parser_1 = require("./session-parser");
let dir;
(0, bun_test_1.beforeEach)(() => { dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-topic-")); });
(0, bun_test_1.afterEach)(() => { fs.rmSync(dir, { recursive: true, force: true }); });
const assistant = {
    type: "assistant", timestamp: "2026-08-01T10:00:05.000Z",
    message: { model: "claude-opus-4-8", content: [{ type: "text", text: "ok" }],
        usage: { input_tokens: 10, output_tokens: 5 } },
};
function topicOf(lines) {
    const p = path.join(dir, "s.jsonl");
    fs.writeFileSync(p, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
    return (0, session_parser_1.parseSession)(p, "agent-a", dir)?.topic;
}
(0, bun_test_1.test)("harness records before the real prompt never name the session", () => {
    const harness = [
        { type: "user", isMeta: true, message: { content: "Caveat: local commands below" } },
        { type: "user", isCompactSummary: true, message: { content: "Summary of the earlier chat" } },
        { type: "user", message: { content: "<command-name>/compact</command-name><command-args></command-args>" } },
        { type: "user", message: { content: "<local-command-stdout>Compacted</local-command-stdout>" } },
        { type: "user", message: { content: "[Request interrupted by user]" } },
        { type: "user", message: { content: "<command-name>/model</command-name><command-args></command-args>" } },
    ];
    const real = { type: "user", message: { content: "Refactor the billing module" } };
    (0, bun_test_1.expect)(topicOf([...harness, real, assistant])).toBe("Refactor the billing module");
});
(0, bun_test_1.test)("real commands and prompts after a system reminder are kept", () => {
    (0, bun_test_1.expect)((0, session_parser_1.topicUserText)({ message: { content: "<command-name>/ce:debug</command-name><command-args>fix login</command-args>" } }))
        .toBe("/ce:debug fix login");
    (0, bun_test_1.expect)((0, session_parser_1.topicUserText)({ message: { content: "<system-reminder>be brief</system-reminder>\nfix the login bug" } }))
        .toBe("fix the login bug");
    (0, bun_test_1.expect)((0, session_parser_1.topicUserText)({ message: { content: "plain prompt" } })).toBe("plain prompt");
});
//# sourceMappingURL=topic-harness.test.js.map