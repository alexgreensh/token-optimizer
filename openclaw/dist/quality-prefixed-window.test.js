"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * Provider-prefixed Claude ids (gateways, Bedrock inference profiles like
 * "us.anthropic.claude-haiku-4-5") resolve to the same window as the bare id.
 */
const bun_test_1 = require("bun:test");
const quality_1 = require("./quality");
const cases = [
    ["anthropic/claude-haiku-4-5", 200_000],
    ["bedrock/claude-sonnet-4-5", 200_000],
    ["anthropic/claude-sonnet-4-6", 200_000],
    ["us.anthropic.claude-haiku-4-5", 200_000],
    ["eu.anthropic.claude-opus-4-7", 1_000_000],
    ["global.anthropic.claude-sonnet-5", 1_000_000],
    ["anthropic.claude-haiku-4-5", 200_000],
    ["bedrock:claude-haiku-4-5", 200_000],
    ["openrouter/anthropic/claude-sonnet-4-6[1m]", 1_000_000],
];
(0, bun_test_1.test)("provider-prefixed ids resolve like their bare form", () => {
    for (const [model, expected] of cases) {
        (0, bun_test_1.expect)((0, quality_1.contextWindowForModel)(model)).toBe(expected);
    }
});
(0, bun_test_1.test)("a family word inside an unstripped prefix is not the family", () => {
    (0, bun_test_1.expect)((0, quality_1.claudeContextWindow)("us.sonnet.claude-haiku-4-5")).toBe((0, quality_1.claudeContextWindow)("sonnet.claude-haiku-4-5"));
});
//# sourceMappingURL=quality-prefixed-window.test.js.map