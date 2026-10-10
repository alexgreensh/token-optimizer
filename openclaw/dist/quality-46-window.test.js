"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * Plain opus/sonnet 4.6 ids may be the 1M variant (the runtime never records
 * the `[1m]` suffix). Tokens above the 200K table window prove it.
 */
const bun_test_1 = require("bun:test");
const quality_1 = require("./quality");
(0, bun_test_1.test)("table still says plain 4.6 is 200K", () => {
    (0, bun_test_1.expect)((0, quality_1.contextWindowForModel)("claude-opus-4-6")).toBe(200_000);
    (0, bun_test_1.expect)((0, quality_1.contextWindowForModel)("claude-sonnet-4-6[1m]")).toBe(1_000_000);
});
(0, bun_test_1.test)("tokens above 200K promote a plain 4.6 id to 1M", () => {
    for (const m of ["claude-opus-4-6", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6", "claude-opus-4-6-20260301"]) {
        (0, bun_test_1.expect)((0, quality_1.promoteWindowForObservedTokens)(m, (0, quality_1.contextWindowForModel)(m), 250_000)).toBe(1_000_000);
    }
});
(0, bun_test_1.test)("tokens that fit leave the 200K window alone", () => {
    (0, bun_test_1.expect)((0, quality_1.promoteWindowForObservedTokens)("claude-opus-4-6", 200_000, 120_000)).toBe(200_000);
    (0, bun_test_1.expect)((0, quality_1.promoteWindowForObservedTokens)("claude-opus-4-6", 200_000, 200_000)).toBe(200_000);
});
(0, bun_test_1.test)("models that cannot be 1M are never promoted", () => {
    (0, bun_test_1.expect)((0, quality_1.promoteWindowForObservedTokens)("claude-opus-4-5", 200_000, 250_000)).toBe(200_000);
    (0, bun_test_1.expect)((0, quality_1.promoteWindowForObservedTokens)("claude-sonnet-4-5", 200_000, 250_000)).toBe(200_000);
    (0, bun_test_1.expect)((0, quality_1.promoteWindowForObservedTokens)("gpt-5", 200_000, 250_000)).toBe(200_000);
});
//# sourceMappingURL=quality-46-window.test.js.map