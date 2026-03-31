"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.EXPENSIVE_MODELS = void 0;
exports.totalTokens = totalTokens;
function totalTokens(t) {
    return t.input + t.output + t.cacheRead + t.cacheWrite;
}
/** Models considered expensive (should not be used for heartbeat/cron tasks). */
exports.EXPENSIVE_MODELS = new Set([
    "opus", "sonnet", "gpt-5.4", "gpt-5.2", "gpt-5", "gpt-4.1",
    "gpt-4o", "o3", "o3-pro", "gemini-3-pro", "gemini-2.5-pro", "grok-4",
]);
//# sourceMappingURL=models.js.map