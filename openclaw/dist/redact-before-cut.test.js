"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * Redact BEFORE truncating (openclaw). A secret that straddles a width cut
 * (2000 / 1500 / 3000 chars in the checkpoint writers, 200 in the telemetry
 * and savings detail) used to survive as a prefix no pattern recognises:
 * "ghp_" + 12 chars is shorter than the token shape.
 *
 * Secrets are assembled at runtime so this file contains no literal that
 * matches a real-secret shape (push protection scans test files too).
 */
const bun_test_1 = require("bun:test");
const fs_1 = require("fs");
const child_process_1 = require("child_process");
const os_1 = require("os");
const path_1 = require("path");
const smart_compact_1 = require("./smart-compact");
const j = (...parts) => parts.join("");
const TOKEN = j("ghp_", "Q7r8T9u0V1w2X3y4Z5a6B7c8D9e0F1g2H3i4");
const FRAG = "Q7r8T9u0";
const tmpRoot = (0, fs_1.mkdtempSync)((0, path_1.join)((0, os_1.tmpdir)(), `openclaw-cut-${process.pid}-`));
const prevRoot = process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT;
process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = tmpRoot;
(0, bun_test_1.afterAll)(() => {
    if (prevRoot === undefined)
        delete process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT;
    else
        process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = prevRoot;
    (0, fs_1.rmSync)(tmpRoot, { recursive: true, force: true });
});
/** Text whose character at index `cut` falls `inside` chars into the token. */
const straddle = (cut, inside) => "a".repeat(cut - inside) + " " + TOKEN + " tail";
function persisted(file) {
    (0, bun_test_1.expect)(file).not.toBeNull();
    const dir = (0, path_1.join)(file, "..");
    return (0, fs_1.readdirSync)(dir).map((f) => (0, fs_1.readFileSync)((0, path_1.join)(dir, f), "utf-8")).join("\n");
}
(0, bun_test_1.test)("v1 checkpoint body: a secret straddling the 2000-char message cut is redacted", () => {
    const file = (0, smart_compact_1.captureCheckpoint)({ sessionId: "cut-v1", messages: [{ role: "user", content: straddle(2000, 12) }] }, 20, { trigger: "stop" });
    const text = persisted(file);
    (0, bun_test_1.expect)(text).not.toContain(FRAG);
    (0, bun_test_1.expect)(text).not.toContain("ghp_");
});
(0, bun_test_1.test)("v2 checkpoint body: a secret straddling the 1500-char message cut is redacted", () => {
    const file = (0, smart_compact_1.captureCheckpointV2)({ sessionId: "cut-v2", messages: [{ role: "user", content: straddle(1500, 12) }] }, 10, { trigger: "stop" });
    const text = persisted(file);
    (0, bun_test_1.expect)(text).not.toContain(FRAG);
    (0, bun_test_1.expect)(text).not.toContain("ghp_");
});
(0, bun_test_1.test)("v2 extraction: a decision line cut at the 3000-char sample is redacted", () => {
    // The decision line is short (extraction ignores lines >= 500 chars) and sits
    // after 2970 chars of filler, so the sample cut lands 12 chars into the token.
    const content = "y".repeat(2970) + "\n" + "I decided to use " + TOKEN + " for auth";
    const file = (0, smart_compact_1.captureCheckpointV2)({ sessionId: "cut-v2-sample", messages: [{ role: "assistant", content }] }, 10, { trigger: "stop" });
    const text = persisted(file);
    (0, bun_test_1.expect)(text).not.toContain(FRAG);
});
// telemetry.ts reads HOME at import time and read-cache.ts writes under
// ~/.openclaw, so these run in a child process with a throwaway HOME.
function runInChild(script) {
    const home = (0, fs_1.mkdtempSync)((0, path_1.join)((0, os_1.tmpdir)(), `openclaw-cut-home-${process.pid}-`));
    try {
        const proc = (0, child_process_1.spawnSync)(process.execPath, ["-e", script], {
            cwd: __dirname,
            env: { ...process.env, HOME: home, USERPROFILE: home, TOKEN_OPTIMIZER_FRAG_TOKEN: TOKEN },
            windowsHide: true,
        });
        (0, bun_test_1.expect)(proc.status).toBe(0);
        const dir = (0, path_1.join)(home, ".openclaw", "token-optimizer");
        return (0, fs_1.readdirSync)(dir).map((f) => (0, fs_1.readFileSync)((0, path_1.join)(dir, f), "utf-8")).join("\n");
    }
    finally {
        (0, fs_1.rmSync)(home, { recursive: true, force: true });
    }
}
(0, bun_test_1.test)("telemetry detail: a secret straddling the 200-char cut is redacted", () => {
    const out = runInChild(`
    const { logCompressionEvent } = require("./telemetry");
    const tok = process.env.TOKEN_OPTIMIZER_FRAG_TOKEN;
    const detail = "a".repeat(200 - 12) + " " + tok;
    // the token starts at 189; the cut at 200 lands 11 chars in
    logCompressionEvent({ feature: "x", originalTokens: 10, compressedTokens: 5, detail });
  `);
    (0, bun_test_1.expect)(out).not.toContain(FRAG.slice(0, 6));
    (0, bun_test_1.expect)(out).not.toContain("ghp_");
});
(0, bun_test_1.test)("savings detail: a secret straddling the 200-char cut is redacted", () => {
    const out = runInChild(`
    const { logSavingsEvent } = require("./read-cache");
    const tok = process.env.TOKEN_OPTIMIZER_FRAG_TOKEN;
    logSavingsEvent("checkpoint_restore", 10, "sess", "a".repeat(200 - 12) + " " + tok);
  `);
    (0, bun_test_1.expect)(out).not.toContain(FRAG.slice(0, 6));
    (0, bun_test_1.expect)(out).not.toContain("ghp_");
});
//# sourceMappingURL=redact-before-cut.test.js.map