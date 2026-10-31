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
 * Dashboard parity regression tests (OpenClaw adapter, v2.4.21 / Core v5.13.10).
 *
 * Covers three bounded parity gaps from the platform-parity audit:
 *   1. Persistent Core + adapter version labels in the nav (brand-meta).
 *   2. Honest static-dashboard regeneration instruction (no fake button/server).
 *   3. Windowed savings-events read so the action card shows actual Last N days
 *      measured/estimated action dollars, not a lifetime sum.
 *
 * Run: bun test src/dashboard.parity.test.ts
 */
const bun_test_1 = require("bun:test");
const fs = __importStar(require("node:fs"));
const os = __importStar(require("node:os"));
const path = __importStar(require("node:path"));
const dashboard_js_1 = require("./dashboard.js");
const savings_js_1 = require("./savings.js");
const DAY_MS = 86_400_000;
// ---------------------------------------------------------------------------
// Shared minimal DashboardData (mirrors dashboard.young-install.test.ts shape)
// ---------------------------------------------------------------------------
const baseData = {
    generatedAt: "2026-09-08T00:00:00.000Z",
    daysScanned: 30,
    contextWindow: 200000,
    overview: {
        totalRuns: 0,
        totalCost: 0,
        totalTokens: 0,
        totalBillableTokens: 0,
        allCostZero: true,
        monthlySavings: 0,
        wasteCount: 0,
        activeDays: 0,
        unknownModelRuns: 0,
    },
    agents: [],
    agentCosts: [],
    waste: [],
    daily: [],
    models: [],
    severityCounts: { low: 0, medium: 0, high: 0, critical: 0 },
    quality: null,
    context: null,
    sessions: [],
    pricingTier: "api",
    pricingTierLabel: "API",
    coach: null,
    savings: null,
    savingsEvents: { categories: [], totalTokensSaved: 0, totalCostSavedUsd: 0, totalCount: 0 },
    coreVersion: "5.13.10",
    adapterVersion: "2.4.21",
};
// ---------------------------------------------------------------------------
// 1. Persistent version labels in the nav
// ---------------------------------------------------------------------------
(0, bun_test_1.test)("nav brand-meta shows Core + OpenClaw adapter version labels", () => {
    const html = (0, dashboard_js_1.generateDashboardHtml)(baseData);
    // The brand-meta element persists across all views (it is in the nav, not a
    // view), so it is the one version label visible on every tab.
    (0, bun_test_1.expect)(html).toContain("brand-meta");
    (0, bun_test_1.expect)(html).toContain("Core v5.13.10");
    (0, bun_test_1.expect)(html).toContain("OpenClaw v2.4.21");
});
(0, bun_test_1.test)("nav brand-meta reflects custom version values, not hardcoded", () => {
    const html = (0, dashboard_js_1.generateDashboardHtml)({
        ...baseData,
        coreVersion: "9.9.99",
        adapterVersion: "3.3.33",
    });
    (0, bun_test_1.expect)(html).toContain("Core v9.9.99");
    (0, bun_test_1.expect)(html).toContain("OpenClaw v3.3.33");
    // The fallback defaults must NOT leak through when explicit values are given.
    (0, bun_test_1.expect)(html).not.toContain("Core v5.13.10");
    (0, bun_test_1.expect)(html).not.toContain("OpenClaw v2.4.21");
});
// ---------------------------------------------------------------------------
// 2. Honest static-dashboard regeneration instruction
// ---------------------------------------------------------------------------
(0, bun_test_1.test)("sidebar shows static-dashboard regeneration instruction with CLI command", () => {
    const html = (0, dashboard_js_1.generateDashboardHtml)(baseData);
    // Slice the sidebar/aside only to avoid false positives from elsewhere.
    const asideStart = html.indexOf("<aside");
    const asideEnd = html.indexOf("</aside>");
    (0, bun_test_1.expect)(asideStart).toBeGreaterThan(-1);
    (0, bun_test_1.expect)(asideEnd).toBeGreaterThan(asideStart);
    const aside = html.slice(asideStart, asideEnd);
    // Honest instruction: labels it static, names the CLI command.
    (0, bun_test_1.expect)(aside).toContain("Static dashboard");
    (0, bun_test_1.expect)(aside).toContain("static snapshot");
    // The regeneration instruction (regen-note-desc) must name the native CLI
    // command, not the published npx package (which resolves to an unrelated
    // repository). Slice the regen-note-desc to avoid false positives from the
    // Quick Commands list, which still shows the published npx invocations.
    const descStart = aside.indexOf('regen-note-desc">');
    (0, bun_test_1.expect)(descStart).toBeGreaterThan(-1);
    const descEnd = aside.indexOf("</div>", descStart);
    (0, bun_test_1.expect)(descEnd).toBeGreaterThan(descStart);
    const desc = aside.slice(descStart, descEnd);
    (0, bun_test_1.expect)(desc).toContain("node dist/cli.js dashboard");
    (0, bun_test_1.expect)(desc).toContain("openclaw source checkout");
    // No fake Regenerate button or POST endpoint.
    (0, bun_test_1.expect)(desc).not.toContain("Regenerate");
    (0, bun_test_1.expect)(desc.toLowerCase()).not.toContain("button");
    (0, bun_test_1.expect)(desc).not.toContain("POST");
    (0, bun_test_1.expect)(desc).not.toContain("/api/");
});
// ---------------------------------------------------------------------------
// 3. Windowed savings-events read (action card uses actual period, not lifetime)
// ---------------------------------------------------------------------------
let dir;
(0, bun_test_1.beforeEach)(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-parity-"));
    fs.mkdirSync(path.join(dir, "token-optimizer"), { recursive: true });
});
(0, bun_test_1.afterEach)(() => {
    fs.rmSync(dir, { recursive: true, force: true });
});
function writeEvents(events) {
    const lines = events.map((e) => JSON.stringify(e)).join("\n") + "\n";
    fs.writeFileSync(path.join(dir, "token-optimizer", "savings-events.jsonl"), lines);
}
(0, bun_test_1.test)("readSavingsEventsByCategory with window excludes events older than the cutoff", () => {
    const now = Date.parse("2026-09-08T00:00:00Z");
    // Recent event (5 days ago) — inside a 30-day window.
    const recent = {
        event_type: "tool_archive",
        timestamp: new Date(now - 5 * DAY_MS).toISOString(),
        tokens_saved: 1000,
        cost_saved_usd: 0.5,
    };
    // Old event (60 days ago) — outside a 30-day window.
    const old = {
        event_type: "tool_archive",
        timestamp: new Date(now - 60 * DAY_MS).toISOString(),
        tokens_saved: 5000,
        cost_saved_usd: 2.5,
    };
    writeEvents([recent, old]);
    const window = { days: 30, now };
    const summary = (0, savings_js_1.readSavingsEventsByCategory)(dir, window);
    // Only the recent event is counted.
    (0, bun_test_1.expect)(summary.totalCount).toBe(1);
    (0, bun_test_1.expect)(summary.totalTokensSaved).toBe(1000);
    (0, bun_test_1.expect)(summary.totalCostSavedUsd).toBe(0.5);
});
(0, bun_test_1.test)("readSavingsEventsByCategory without window sums all events (lifetime baseline)", () => {
    const now = Date.parse("2026-09-08T00:00:00Z");
    const recent = {
        event_type: "tool_archive",
        timestamp: new Date(now - 5 * DAY_MS).toISOString(),
        tokens_saved: 1000,
        cost_saved_usd: 0.5,
    };
    const old = {
        event_type: "tool_archive",
        timestamp: new Date(now - 60 * DAY_MS).toISOString(),
        tokens_saved: 5000,
        cost_saved_usd: 2.5,
    };
    writeEvents([recent, old]);
    // No window = lifetime (the old behavior the action card used before this fix).
    const summary = (0, savings_js_1.readSavingsEventsByCategory)(dir);
    (0, bun_test_1.expect)(summary.totalCount).toBe(2);
    (0, bun_test_1.expect)(summary.totalTokensSaved).toBe(6000);
    (0, bun_test_1.expect)(summary.totalCostSavedUsd).toBe(3.0);
});
(0, bun_test_1.test)("windowed read at the exact cutoff boundary includes the boundary event", () => {
    const now = Date.parse("2026-09-08T00:00:00Z");
    // Event exactly 30 days ago — the cutoff is `now - days * DAY_MS`, and the
    // filter is `ts < cutoff` (strict), so a boundary event (ts == cutoff) is
    // INCLUDED.
    const boundary = {
        event_type: "resume_lean",
        timestamp: new Date(now - 30 * DAY_MS).toISOString(),
        tokens_saved: 200,
        cost_saved_usd: 0.1,
    };
    // Event 31 days ago — excluded.
    const justOutside = {
        event_type: "resume_lean",
        timestamp: new Date(now - 31 * DAY_MS).toISOString(),
        tokens_saved: 999,
        cost_saved_usd: 9.99,
    };
    writeEvents([boundary, justOutside]);
    const window = { days: 30, now };
    const summary = (0, savings_js_1.readSavingsEventsByCategory)(dir, window);
    (0, bun_test_1.expect)(summary.totalCount).toBe(1);
    (0, bun_test_1.expect)(summary.totalTokensSaved).toBe(200);
});
(0, bun_test_1.test)("windowed read with undated events excludes them when a window is requested", () => {
    const now = Date.parse("2026-09-08T00:00:00Z");
    // Missing timestamp — must be excluded under a window (cannot prove it is
    // inside the lookback), per the readSavingsEventsByCategory contract.
    const undated = {
        event_type: "hint_followed",
        tokens_saved: 500,
        cost_saved_usd: 0.25,
    };
    writeEvents([undated]);
    const window = { days: 30, now };
    const summary = (0, savings_js_1.readSavingsEventsByCategory)(dir, window);
    (0, bun_test_1.expect)(summary.totalCount).toBe(0);
    (0, bun_test_1.expect)(summary.totalTokensSaved).toBe(0);
});
(0, bun_test_1.test)("windowed read with days=0 behaves as lifetime (no cutoff applied)", () => {
    const now = Date.parse("2026-09-08T00:00:00Z");
    const old = {
        event_type: "checkpoint_restore",
        timestamp: new Date(now - 365 * DAY_MS).toISOString(),
        tokens_saved: 800,
        cost_saved_usd: 0.4,
    };
    writeEvents([old]);
    // days=0 disables the window (cutoff is null), so all events are summed.
    const window = { days: 0, now };
    const summary = (0, savings_js_1.readSavingsEventsByCategory)(dir, window);
    (0, bun_test_1.expect)(summary.totalCount).toBe(1);
    (0, bun_test_1.expect)(summary.totalTokensSaved).toBe(800);
});
(0, bun_test_1.test)("unpriced archive actions disclose their signed priced subtotal", () => {
    const html = (0, dashboard_js_1.generateDashboardHtml)({ ...baseData, savingsEvents: {
            categories: [], totalTokensSaved: 0, totalCostSavedUsd: 0, totalCount: 2,
            unpricedTokens: 1000000, unpricedEvents: 1, archivePricedNetUsd: -2,
        } });
    (0, bun_test_1.expect)(html).toContain("action credits or reversals have no model price");
    (0, bun_test_1.expect)(html).toContain("Priced archive subtotal: -$2.00");
});
//# sourceMappingURL=dashboard.parity.test.js.map