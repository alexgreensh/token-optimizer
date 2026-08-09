"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
/**
 * Pre-existing continuity bug surfaced by the #127 torture-room gauntlet (OpenClaw).
 * Not caused by the i18n change; fixed alongside it at Alex's request.
 */
const bun_test_1 = require("bun:test");
const continuity_js_1 = require("./continuity.js");
(0, bun_test_1.test)("'continue from ...' is recognized as resume intent (was dropped from the TS port)", () => {
    (0, bun_test_1.expect)((0, continuity_js_1.isResumeIntent)("continue from where we left off")).toBe(true);
    (0, bun_test_1.expect)((0, continuity_js_1.isResumeIntent)("continue from the checkpoint")).toBe(true);
    (0, bun_test_1.expect)((0, continuity_js_1.isResumeIntent)("import data from checkpoint file")).toBe(false);
});
//# sourceMappingURL=continuity-preexisting.test.js.map