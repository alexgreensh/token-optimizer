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
exports.savingsCategoryLabel = savingsCategoryLabel;
exports.readSavingsEventsByCategory = readSavingsEventsByCategory;
exports.measuredSavingsFromSummary = measuredSavingsFromSummary;
exports.allocateCents = allocateCents;
exports.computeRealizedSavings = computeRealizedSavings;
/**
 * OpenClaw savings counterfactual and read-time event aggregation.
 * Frozen baseline token classes are compared with the current eligible model mix.
 * Premium usage above the baseline rate ceiling is disclosed separately.
 * Provider cache-hit changes are not attributed to Token Optimizer.
 */
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const counted_events_1 = require("./counted-events");
const session_parser_1 = require("./session-parser");
const pricing_1 = require("./pricing");
const event_pricing_1 = require("./event-pricing");
// --- Constants (mirror measure.py baseline tunables) ------------------------
const BASELINE_ONBOARDING_DAYS = 1; // skip day 1 (learning-curve sessions)
const BASELINE_EARLY_WINDOW_DAYS = 30; // the "before" window after onboarding
const BASELINE_MIN_STABLE_SESSIONS = 30; // need this many before freezing
const AFTER_MIN_SESSIONS = 10; // need this many recent sessions to compare
// Quality gates: exclude empty/transient sessions that inflate count and skew
// per-session costs (mirrors measure.py _MIN_INPUT_TOKENS / _MIN_DURATION_MINUTES).
const MIN_INPUT_TOKENS = 1000;
const MIN_DURATION_SECONDS = 60;
// Winsorization is used ONLY when freezing the baseline's efficiency anchors
// (mix shares + pool cache-hit pattern); it never touches the headline VOLUME,
// which aggregates the current window with NO outlier drop (see methodology #1).
const WINSOR_PCT = 0.99; // cap the top 1% of sessions by cost (baseline anchors only)
const WINSOR_MIN_SAMPLE = 10; // below this, plain mean (cap is meaningless)
const BASELINE_VERSION = 2; // per-session pricing baseline (anchors: mix + cache-hit)
const DAY_MS = 86_400_000;
const PROXY_MODEL = "sonnet"; // price unpriced/unknown models at this rate card
// Estimated-tier savings categories: their magnitude is not directly metered, so
// they are EXCLUDED from the measured compression add-back (mirrors measure.py
// `_get_savings_summary` relocations of setup_optimization / mcp_cap / hint_followed).
const ESTIMATED_TIER_CATEGORIES = new Set([
    "setup_optimization",
    "mcp_cap",
    "hint_followed",
    "verbosity_steer",
    "fresh_session_nudge",
]);
const CLASSES = ["fi", "cr", "cw", "out"];
// --- Storage ----------------------------------------------------------------
function storageDir(openclawDir) {
    const dir = path.join(openclawDir, "token-optimizer");
    try {
        fs.mkdirSync(dir, { recursive: true });
    }
    catch {
        /* best effort */
    }
    return dir;
}
/**
 * Strict numeric coercion at the data boundary (F1/F2). Token fields arrive from
 * scanned AgentRuns OR persisted JSON, where a string ("999999999"), NaN, or a
 * negative is possible (hand-edited history, corrupt upstream parse). Without this,
 * `+=` concatenates a string into a giant numeric string that detonates the headline
 * (~1e40), and a negative silently zeroes/poisons the math. We parse, drop non-finite
 * to 0, and clamp to >= 0 so no string/NaN/negative ever reaches arithmetic.
 */
function tCacheHit(fresh, cached) {
    return fresh + cached > 0 ? cached / (fresh + cached) : 0;
}
function numClamp(value) {
    const n = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(n) || n < 0)
        return 0;
    return n;
}
/**
 * Coerce + clamp a raw record's token fields into a clean SessionRecord. Mirrors
 * non-negative token fields. OpenClaw reports fresh input and cache writes
 * separately. TTL buckets are constrained to the cache-write total.
 */
function sanitizeRecord(raw) {
    const input = numClamp(raw.input);
    const output = numClamp(raw.output);
    const cacheRead = numClamp(raw.cacheRead);
    const cacheWrite = numClamp(raw.cacheWrite);
    let cw1h = numClamp(raw.cacheWrite1h);
    let cw5m = numClamp(raw.cacheWrite5m);
    // Keep the TTL split inside the clamped cacheWrite total (proportional scale-down).
    const splitSum = cw1h + cw5m;
    if (splitSum > cacheWrite && splitSum > 0) {
        const scale = cacheWrite / splitSum;
        cw1h *= scale;
        cw5m *= scale;
    }
    return {
        sessionId: String(raw.sessionId),
        ts: raw.ts,
        model: raw.model,
        input,
        output,
        cacheRead,
        cacheWrite,
        cacheWrite1h: cw1h,
        cacheWrite5m: cw5m,
        costUsd: numClamp(raw.costUsd),
        durationSec: numClamp(raw.durationSec),
        modelTokenBuckets: raw.modelTokenBuckets,
    };
}
function toRecord(r) {
    return sanitizeRecord({
        sessionId: r.sessionId,
        ts: r.timestamp.getTime(),
        model: r.rawModel ?? r.model,
        modelTokenBuckets: r.modelTokenBuckets,
        input: r.tokens.input,
        output: r.tokens.output,
        cacheRead: r.tokens.cacheRead,
        cacheWrite: r.tokens.cacheWrite,
        cacheWrite1h: r.cacheWrite1hTokens ?? 0,
        cacheWrite5m: r.cacheWrite5mTokens ?? 0,
        costUsd: r.costUsd,
        durationSec: r.durationSeconds ?? 0,
    });
}
/**
 * Merge freshly-scanned sessions into persisted history (dedup by sessionId),
 * write back, return the union. Persistence keeps the baseline alive even after
 * OpenClaw prunes old JSONL session files.
 */
function mergeAndPersistHistory(openclawDir, fresh) {
    const file = path.join(storageDir(openclawDir), "session-history.json");
    const byId = new Map();
    try {
        const stored = JSON.parse(fs.readFileSync(file, "utf-8"));
        for (const r of stored) {
            if (!r || !r.sessionId)
                continue;
            // F1/F2: persisted JSON can carry string/NaN/negative token fields (hand-edited
            // or corrupt history). Re-sanitize at this boundary so no bad value ever reaches
            // the headline `+=` aggregation.
            const ts = typeof r.ts === "number" && Number.isFinite(r.ts) ? r.ts : 0;
            const rec = sanitizeRecord({
                sessionId: r.sessionId,
                ts,
                model: typeof r.model === "string" ? r.model : String(r.model),
                input: r.input,
                output: r.output,
                cacheRead: r.cacheRead,
                cacheWrite: r.cacheWrite,
                cacheWrite1h: r.cacheWrite1h,
                cacheWrite5m: r.cacheWrite5m,
                costUsd: r.costUsd,
                durationSec: r.durationSec,
                modelTokenBuckets: r.modelTokenBuckets,
            });
            byId.set(rec.sessionId, rec);
        }
    }
    catch {
        /* no history yet */
    }
    for (const r of fresh)
        if (r.sessionId)
            byId.set(r.sessionId, r); // fresh wins
    const merged = Array.from(byId.values()).sort((a, b) => a.ts - b.ts);
    try {
        const tmp = file + ".tmp";
        fs.writeFileSync(tmp, JSON.stringify(merged), { mode: 0o600 });
        fs.renameSync(tmp, file);
    }
    catch {
        /* best effort */
    }
    return merged;
}
// --- Per-session pricing (multi-model correct) ------------------------------
function classTokens(r) {
    // OpenClaw token usage is already decomposed (input is fresh; cacheRead and
    // cacheWrite are separate), so no cache_hit_rate derivation is needed.
    return { fi: r.input, cr: r.cacheRead, cw: r.cacheWrite, out: r.output };
}
/** Resolve the rate card without substituting another model. */
function priceKey(model, proxy, openclawDir) {
    const key = (0, event_pricing_1.resolvePricedModel)(model, openclawDir);
    return key ?? model;
}
// Aggregate volume does not establish request length, so use base rates.
function aggregateCost(tokens, model, dir, split) {
    const scale = Math.max(1, tokens.input + tokens.cacheRead + tokens.cacheWrite, tokens.output);
    return scale * (0, pricing_1.calculateCost)({ input: tokens.input / scale, output: tokens.output / scale, cacheRead: tokens.cacheRead / scale, cacheWrite: tokens.cacheWrite / scale }, model, dir, split ? { cacheWrite1hTokens: (split.cacheWrite1hTokens ?? 0) / scale, cacheWrite5mTokens: (split.cacheWrite5mTokens ?? 0) / scale } : undefined);
}
/**
 * Per-class cost of ONE session, priced at its own model.
 * Returned per class so the waterfall can use real per-class effective rates.
 */
function sessionClassCost(r, proxy, openclawDir) {
    if (r.modelTokenBuckets && Object.keys(r.modelTokenBuckets).length > 0) {
        const total = { fi: 0, cr: 0, cw: 0, out: 0 };
        for (const [model, b] of Object.entries(r.modelTokenBuckets)) {
            if (!b || !(0, event_pricing_1.resolvePricedModel)(model, openclawDir))
                continue;
            const cost = sessionClassCost({ ...r, model, input: numClamp(b.input), output: numClamp(b.output), cacheRead: numClamp(b.cacheRead), cacheWrite: numClamp(b.cacheWrite), cacheWrite1h: numClamp(b.cacheWrite1h), cacheWrite5m: numClamp(b.cacheWrite5m), modelTokenBuckets: undefined }, proxy, openclawDir);
            for (const c of CLASSES)
                total[c] += cost[c];
        }
        return total;
    }
    const model = priceKey(r.model, proxy, openclawDir);
    const t = classTokens(r);
    // Isolate each class by zeroing the others (calculateCost is linear in tokens,
    // so per-class costs sum to the full session cost).
    const split = { cacheWrite1hTokens: r.cacheWrite1h, cacheWrite5m: r.cacheWrite5m };
    return {
        fi: aggregateCost({ input: t.fi, output: 0, cacheRead: 0, cacheWrite: 0 }, model, openclawDir),
        cr: aggregateCost({ input: 0, output: 0, cacheRead: t.cr, cacheWrite: 0 }, model, openclawDir),
        cw: aggregateCost({ input: 0, output: 0, cacheRead: 0, cacheWrite: t.cw }, model, openclawDir, { cacheWrite1hTokens: r.cacheWrite1h, cacheWrite5mTokens: r.cacheWrite5m }),
        out: aggregateCost({ input: 0, output: t.out, cacheRead: 0, cacheWrite: 0 }, model, openclawDir),
    };
}
function modelShares(records) {
    const byModel = {};
    let total = 0;
    for (const r of records) {
        const t = r.input + r.output + r.cacheRead + r.cacheWrite;
        byModel[r.model] = (byModel[r.model] ?? 0) + t;
        total += t;
    }
    if (total <= 0)
        return {};
    const shares = {};
    for (const [m, t] of Object.entries(byModel))
        shares[m] = t / total;
    return shares;
}
function dominantPricedModel(records, openclawDir) {
    const pricing = (0, pricing_1.getPricing)(openclawDir);
    const byModel = {};
    for (const r of records) {
        const key = pricing[r.model] ? r.model : (0, pricing_1.normalizeModelName)(r.model) ?? r.model;
        if (pricing[key])
            byModel[key] = (byModel[key] ?? 0) + (r.input + r.output + r.cacheRead + r.cacheWrite);
    }
    let best = PROXY_MODEL;
    let bestTok = -1;
    for (const [m, t] of Object.entries(byModel))
        if (t > bestTok) {
            best = m;
            bestTok = t;
        }
    return best;
}
function modelClassShares(records) {
    const totals = { fi: {}, cr: {}, cw: {}, out: {} };
    for (const r of records) {
        const buckets = r.modelTokenBuckets && Object.keys(r.modelTokenBuckets).length ? r.modelTokenBuckets : { [r.model]: { input: r.input, output: r.output, cacheRead: r.cacheRead, cacheWrite: r.cacheWrite, cacheWrite1h: r.cacheWrite1h, cacheWrite5m: r.cacheWrite5m } };
        for (const [model, b] of Object.entries(buckets)) {
            if (!b)
                continue;
            const values = { fi: numClamp(b.input), cr: numClamp(b.cacheRead), cw: numClamp(b.cacheWrite), out: numClamp(b.output) };
            for (const c of CLASSES)
                totals[c][model] = (totals[c][model] ?? 0) + values[c];
        }
    }
    for (const c of CLASSES) {
        const sum = Object.values(totals[c]).reduce((a, b) => a + b, 0);
        if (sum > 0)
            for (const model of Object.keys(totals[c]))
                totals[c][model] /= sum;
    }
    return totals;
}
function modelTtlShares(records) {
    const shares = { oneHour: {}, fiveMinute: {} };
    for (const r of records) {
        const buckets = r.modelTokenBuckets && Object.keys(r.modelTokenBuckets).length ? r.modelTokenBuckets : { [r.model]: { input: r.input, output: r.output, cacheRead: r.cacheRead, cacheWrite: r.cacheWrite, cacheWrite1h: r.cacheWrite1h, cacheWrite5m: r.cacheWrite5m } };
        for (const [model, b] of Object.entries(buckets)) {
            if (!b)
                continue;
            const total = numClamp(b.cacheWrite), one = Math.min(total, numClamp(b.cacheWrite1h));
            shares.oneHour[model] = (shares.oneHour[model] ?? 0) + one;
            shares.fiveMinute[model] = (shares.fiveMinute[model] ?? 0) + total - one;
        }
    }
    for (const mix of [shares.oneHour, shares.fiveMinute]) {
        const total = Object.values(mix).reduce((a, b) => a + b, 0);
        if (total > 0)
            for (const model of Object.keys(mix))
                mix[model] /= total;
    }
    return shares;
}
// --- Mix-weighted pricing (counterfactual primitives) -----------------------
// Price token classes at a MODEL MIX (share-weighted blend across the priced
// models in an era). calculateCost is per-token linear, so we blend the per-MTok
// rates by share and apply once. Unpriced models have no rate card.
/** Resolve a share map into priced [model, share] pairs. */
function pricedShares(shares, proxy, openclawDir) {
    const blended = {};
    for (const [m, s] of Object.entries(shares)) {
        if (!s || s <= 0)
            continue;
        const model = (0, event_pricing_1.resolvePricedModel)(m, openclawDir);
        if (model)
            blended[model] = (blended[model] ?? 0) + s;
    }
    const items = Object.entries(blended).filter(([, s]) => s > 0);
    if (items.length === 0)
        return [];
    return items;
}
/**
 * Price the fresh + cache_read pool and output at a model mix (NO cache-write).
 * Share-weighted blend over the era's priced models.
 */
function pricePool(fi, cr, out, shares, proxy, openclawDir) {
    const items = pricedShares(shares, proxy, openclawDir);
    const tot = items.reduce((a, [, s]) => a + s, 0);
    let cost = 0;
    for (const [model, s] of items) {
        cost +=
            (s / tot) *
                aggregateCost({ input: fi, output: out, cacheRead: cr, cacheWrite: 0 }, model, openclawDir);
    }
    return cost;
}
/**
 * Price cache-write at a model mix, TTL-aware (1h writes bill at 2x input, 5m at
 * 1.25x). Cache-write IS a routing lever (#1): it is billed at the WRITING model's
 * rate, so it is priced at each arm's OWN mix (baseline mix in the counterfactual,
 * actual mix in actual) — exactly like fresh/cache_read/output.
 */
function priceCacheWrite(cw, cw1h, cw5m, shares, proxy, openclawDir) {
    const items = pricedShares(shares, proxy, openclawDir);
    const tot = items.reduce((a, [, s]) => a + s, 0);
    let cost = 0;
    for (const [model, s] of items) {
        cost +=
            (s / tot) *
                aggregateCost({ input: 0, output: 0, cacheRead: 0, cacheWrite: cw }, model, openclawDir, { cacheWrite1hTokens: cw1h, cacheWrite5mTokens: cw5m });
    }
    return cost;
}
function priceClasses(fi, cr, cw, out, shares, fallback, proxy, dir, write1hFraction = 0) {
    const mix = (c) => shares && Object.keys(shares[c] ?? {}).length ? shares[c] : fallback;
    return pricePool(fi, 0, 0, mix("fi"), proxy, dir) + pricePool(0, cr, 0, mix("cr"), proxy, dir) + pricePool(0, 0, out, mix("out"), proxy, dir) + priceCacheWrite(cw, cw * write1hFraction, cw * (1 - write1hFraction), mix("cw"), proxy, dir);
}
function classRate(model, c, dir) {
    return (0, pricing_1.calculateCost)({ input: c === "fi" ? 1 : 0, cacheRead: c === "cr" ? 1 : 0, cacheWrite: c === "cw" ? 1 : 0, output: c === "out" ? 1 : 0 }, model, dir);
}
/** Exclude current models above the baseline price ceiling for each token class. */
function comparableMix(before, after, beforeAll, afterAll, dir) {
    const b = { fi: {}, cr: {}, cw: {}, out: {} };
    const a = { fi: {}, cr: {}, cw: {}, out: {} };
    const eligible = { fi: 0, cr: 0, cw: 0, out: 0 };
    const ceiling = { fi: 0, cr: 0, cw: 0, out: 0 };
    for (const c of CLASSES) {
        const bm = before ? (before[c] ?? {}) : beforeAll;
        const am = after ? (after[c] ?? {}) : afterAll;
        ceiling[c] = Math.max(0, ...Object.entries(bm).filter(([, v]) => v > 0).map(([m]) => classRate(m, c, dir)));
        b[c] = bm;
        for (const [model, share] of Object.entries(am)) {
            if (share > 0 && classRate(model, c, dir) <= ceiling[c] + 1e-15) {
                a[c][model] = share;
                eligible[c] += share;
            }
        }
    }
    return { before: b, after: a, eligible, ceiling };
}
/** Cost of 1M fresh-input tokens at a model mix (the "input rate" for repricing). */
function inputRate(shares, proxy, openclawDir) {
    return pricePool(1_000_000, 0, 0, shares, proxy, openclawDir);
}
/**
 * Aggregate an era. Each session is priced at its own model; then the top 1% of
 * sessions BY COST are winsorized (scaled down) so one runaway session can't
 * dominate. Effective per-class rates are derived from the winsorized totals so
 * the waterfall telescopes exactly to costPerSession.
 */
function computeEraStats(records, openclawDir, proxyOverride) {
    const zero = { fi: 0, cr: 0, cw: 0, out: 0 };
    if (records.length === 0) {
        return { n: 0, costPerSession: 0, meanTokens: { ...zero }, effRate: { ...zero }, shares: {} };
    }
    // A SINGLE proxy (from full history) must price unpriced models in BOTH eras,
    // otherwise the dominant priced model can differ between before/after and
    // create a spurious cost delta for unpriced sessions.
    const proxy = proxyOverride ?? dominantPricedModel(records, openclawDir);
    // Per-session: class tokens, class costs, total cost.
    const rows = records.map((r) => {
        const tok = classTokens(r);
        const cost = sessionClassCost(r, proxy, openclawDir);
        const totalCost = cost.fi + cost.cr + cost.cw + cost.out;
        return { tok, cost, totalCost };
    });
    // Winsorize the top 1% of sessions by total cost.
    const n = rows.length;
    let cap = Infinity;
    if (n >= WINSOR_MIN_SAMPLE) {
        const costs = rows.map((r) => r.totalCost).sort((a, b) => a - b);
        // floor (not round): round((n-1)*0.99) == n-1 for n<=51, which makes the cap
        // the max element and winsorization a no-op for every minimum-sample window.
        // floor leaves the top ~1% genuinely cappable. Clamp to <= n-2 so at least
        // the single largest session is always above the cap.
        cap = costs[Math.min(n - 2, Math.floor((n - 1) * WINSOR_PCT))];
    }
    const tokSum = { ...zero };
    const costSum = { ...zero };
    for (const row of rows) {
        const scale = row.totalCost > cap && row.totalCost > 0 ? cap / row.totalCost : 1;
        for (const c of CLASSES) {
            tokSum[c] += row.tok[c] * scale;
            costSum[c] += row.cost[c] * scale;
        }
    }
    const meanTokens = { ...zero };
    const effRate = { ...zero };
    let costPerSession = 0;
    for (const c of CLASSES) {
        meanTokens[c] = tokSum[c] / n;
        effRate[c] = tokSum[c] > 0 ? costSum[c] / tokSum[c] : 0;
        costPerSession += costSum[c] / n; // == Σ effRate[c]*meanTokens[c]
    }
    return { n, costPerSession, meanTokens, effRate, shares: modelShares(records), classShares: modelClassShares(records), ttlShares: modelTtlShares(records) };
}
function baselinePath(openclawDir) {
    return path.join(storageDir(openclawDir), "baseline-state.json");
}
function loadFrozenBaseline(openclawDir) {
    try {
        const b = JSON.parse(fs.readFileSync(baselinePath(openclawDir), "utf-8"));
        if (b && b.version === BASELINE_VERSION)
            return b;
    }
    catch {
        /* none */
    }
    return null;
}
function getOrComputeBaseline(openclawDir, history, now, proxy) {
    const frozen = loadFrozenBaseline(openclawDir);
    if (frozen)
        return { baseline: frozen, reason: "frozen" };
    if (history.length === 0)
        return { baseline: null, reason: "no history" };
    const installTs = history[0].ts;
    const firstDate = new Date(installTs).toISOString().slice(0, 10);
    const windowStart = installTs + BASELINE_ONBOARDING_DAYS * DAY_MS;
    const windowEnd = windowStart + BASELINE_EARLY_WINDOW_DAYS * DAY_MS;
    const before = history.filter((r) => r.ts >= windowStart && r.ts < windowEnd);
    if (before.length < BASELINE_MIN_STABLE_SESSIONS) {
        const daysLeft = Math.max(0, Math.ceil((windowEnd - now) / DAY_MS));
        return {
            baseline: null,
            reason: `building baseline (${before.length}/${BASELINE_MIN_STABLE_SESSIONS} early sessions)`,
            baselineBuilding: {
                sessionsInWindow: before.length,
                sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
                earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
                daysLeft,
                firstDate,
            },
        };
    }
    if (now < windowEnd) {
        const daysLeft = Math.ceil((windowEnd - now) / DAY_MS);
        return {
            baseline: null,
            reason: `building baseline (${daysLeft}d of early window left)`,
            baselineBuilding: {
                sessionsInWindow: before.length,
                sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
                earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
                daysLeft,
                firstDate,
            },
        };
    }
    const baseline = {
        version: BASELINE_VERSION,
        frozenAt: now,
        installTs,
        windowStart,
        windowEnd,
        stats: computeEraStats(before, openclawDir, proxy),
        proxy,
    };
    try {
        const tmp = baselinePath(openclawDir) + ".tmp";
        fs.writeFileSync(tmp, JSON.stringify(baseline), { mode: 0o600 });
        fs.renameSync(tmp, baselinePath(openclawDir));
    }
    catch {
        /* best effort */
    }
    return { baseline, reason: "frozen" };
}
// ---------------------------------------------------------------------------
// Savings-events aggregation (grouped by event_type, no allowlist)
// ---------------------------------------------------------------------------
/**
 * Friendly labels for known savings event types (mirrors Python
 * _SAVINGS_CATEGORY_LABELS in measure.py). Unknown types fall back to
 * title-case of the raw key.
 */
const SAVINGS_CATEGORY_LABELS = {
    resume_lean: "Lean resumes",
    checkpoint_restore: "Checkpoint restores",
    hint_followed: "Hints followed",
    verbosity_steer: "Verbosity steering [est]",
    tool_archive: "Tool replacements",
    structural_savings: "Structural (cumulative)",
};
function savingsCategoryLabel(eventType) {
    if (SAVINGS_CATEGORY_LABELS[eventType])
        return SAVINGS_CATEGORY_LABELS[eventType];
    // Graceful fallback: title-case the raw key (underscores -> spaces)
    return eventType
        .replace(/_/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
}
/**
 * Read savings-events.jsonl, group by event_type, and return per-category
 * totals + a grand total. No allowlist: every event_type in the file surfaces.
 * Returns an empty summary (not an error) when the file is missing. When `window`
 * is given, only events whose `timestamp` is within `now - days` are counted.
 */
function readSavingsEventsByCategory(openclawDir, window) {
    const dir = openclawDir
        ? path.join(openclawDir, "token-optimizer")
        : path.join(process.env.HOME ?? process.env.USERPROFILE ?? "", ".openclaw", "token-optimizer");
    const filePath = path.join(dir, "savings-events.jsonl");
    const root = openclawDir ?? path.join(process.env.HOME ?? process.env.USERPROFILE ?? "", ".openclaw");
    const sessionEvidence = (0, event_pricing_1.loadSavingsSessions)(root);
    let unpricedTokens = 0, unpricedEvents = 0, floorTokens = 0, floorUsd = 0;
    const byType = new Map();
    // F4: compute the lookback cutoff (ms epoch). Rows older than the cutoff are
    // skipped so the windowed sum matches the `days` lookback the caller scales by.
    const cutoff = window && window.days > 0
        ? (window.now ?? Date.now()) - window.days * DAY_MS
        : null;
    try {
        const raw = fs.readFileSync(filePath, "utf-8");
        const rows = [];
        for (const line of raw.split("\n")) {
            try {
                const row = JSON.parse(line);
                if (row && typeof row === "object")
                    rows.push(row);
            }
            catch { /* malformed row */ }
        }
        for (const row of (0, counted_events_1.countedEvents)(rows, "savings")) {
            const et = typeof row.event_type === "string" ? row.event_type : null;
            if (!et)
                continue;
            // Window filter: drop events outside the lookback. A row with a missing or
            // unparseable timestamp is treated as out-of-window (excluded) when a window
            // is requested, so an undated/stale event can't inflate the windowed sum.
            if (cutoff !== null) {
                const tsRaw = row.timestamp;
                const ts = typeof tsRaw === "string" ? Date.parse(tsRaw) : NaN;
                if (!Number.isFinite(ts) || ts < cutoff)
                    continue;
            }
            const tokens = typeof row.tokens_saved === "number" ? row.tokens_saved : 0;
            const session = typeof row.session_id === "string" ? sessionEvidence.get(row.session_id) : undefined;
            const storedBuckets = row.model_token_buckets && typeof row.model_token_buckets === "object" ? row.model_token_buckets : undefined;
            const evidence = storedBuckets ? { model: typeof row.model === "string" ? row.model : undefined, modelTokenBuckets: storedBuckets } : session ?? (typeof row.model === "string" ? { model: row.model } : undefined);
            const storedClass = (0, event_pricing_1.savingsEventClass)(row.token_class);
            const inferred = (0, event_pricing_1.priceSavingsEvent)(tokens, et, evidence, root, storedClass);
            const canReprice = row.pricing_status === "partial_or_unavailable" && Math.abs(row.unpriced_tokens) === Math.abs(tokens) && row.cost_saved_usd === 0 && !!evidence;
            const storedCost = typeof row.cost_saved_usd === "number" && Number.isFinite(row.cost_saved_usd) && !canReprice;
            const cost = storedCost ? row.cost_saved_usd : inferred.cost;
            const uncovered = canReprice ? inferred.unpricedTokens : typeof row.unpriced_tokens === "number" && Number.isFinite(row.unpriced_tokens)
                ? Math.abs(row.unpriced_tokens) : (typeof row.cost_saved_usd === "number" && (row.cost_saved_usd !== 0 || row.pricing_status === "priced") ? 0 : inferred.unpricedTokens);
            unpricedTokens += uncovered;
            if (uncovered > 0)
                unpricedEvents++;
            // Input-rate floor: priced tokens whose billed class was never recorded.
            // Output-class events are evidenced by type and never counted here.
            const noClass = storedClass === undefined && et !== "verbosity_steer" && et !== "verbosity_steer_measured";
            if (noClass) {
                floorUsd += et === "tool_archive_reexpand" ? Math.abs(cost) : cost;
                floorTokens += storedCost ? Math.abs(tokens) - uncovered : inferred.floorTokens;
            }
            const entry = byType.get(et) ?? { count: 0, tokensSaved: 0, costSavedUsd: 0 };
            entry.count++;
            entry.tokensSaved += et === "tool_archive_reexpand" ? Math.abs(tokens) : tokens;
            entry.costSavedUsd += et === "tool_archive_reexpand" ? Math.abs(cost) : cost;
            byType.set(et, entry);
        }
    }
    catch {
        /* file missing or unreadable — return empty summary */
    }
    const categories = Array.from(byType.entries())
        .map(([eventType, agg]) => ({
        eventType,
        label: savingsCategoryLabel(eventType),
        count: agg.count,
        tokensSaved: agg.tokensSaved,
        costSavedUsd: agg.costSavedUsd,
    }))
        .sort((a, b) => b.tokensSaved - a.tokensSaved);
    const archive = byType.get("tool_archive"), reexpand = byType.get("tool_archive_reexpand");
    const archivePricedNetUsd = archive || reexpand ? (archive?.costSavedUsd ?? 0) - (reexpand?.costSavedUsd ?? 0) : null;
    const otherCategories = categories.filter(c => c.eventType !== "tool_archive" && c.eventType !== "tool_archive_reexpand");
    const totalTokensSaved = otherCategories.reduce((s, c) => s + c.tokensSaved, 0)
        + Math.max(0, (archive?.tokensSaved ?? 0) - (reexpand?.tokensSaved ?? 0));
    const totalCostSavedUsd = otherCategories.reduce((s, c) => s + c.costSavedUsd, 0)
        + Math.max(0, archivePricedNetUsd ?? 0);
    const totalCount = categories.reduce((s, c) => s + c.count, 0);
    return { categories, totalTokensSaved, totalCostSavedUsd, totalCount, unpricedTokens, unpricedEvents, archivePricedNetUsd, inputRateFloorTokens: floorTokens, inputRateFloorUsd: floorUsd };
}
/**
 * Sum the MEASURED compression / volume-reduction savings (cost_saved_usd) over
 * the window, EXCLUDING estimated-tier categories (setup_optimization, mcp_cap,
 * hint_followed, verbosity_steer) — mirrors measure.py `_get_savings_summary` relocations. This is
 * the directly-metered floor of the compression add-back pool (#3); the only
 * estimated step is the reprice to the baseline input mix, applied by the caller.
 *
 * B6: net tool_archive_reexpand against tool_archive (re-popped results didn't
 * stay collapsed), floored at 0. The debit is not its own savings line.
 * Mirrors measure.py and OpenCode trends.ts getCompressionSavings.
 */
function measuredCompressionUsd(openclawDir, window) {
    return measuredSavingsFromSummary(readSavingsEventsByCategory(openclawDir, window));
}
function measuredSavingsFromSummary(summary) {
    const byType = new Map();
    for (const cat of summary.categories) {
        if (ESTIMATED_TIER_CATEGORIES.has(cat.eventType))
            continue;
        byType.set(cat.eventType, cat.costSavedUsd);
    }
    // Net re-expansions against tool_archive, floored at 0.
    const reexpand = byType.get("tool_archive_reexpand");
    if (reexpand) {
        byType.delete("tool_archive_reexpand");
        const ta = byType.get("tool_archive");
        if (ta) {
            byType.set("tool_archive", Math.max(0, ta - Math.abs(reexpand)));
        }
    }
    let total = 0;
    for (const v of byType.values()) {
        total += v;
    }
    return Math.max(0, total);
}
/**
 * Sum the ESTIMATED verbosity_steer savings (cost_saved_usd) over the window.
 * These are estimated output-token reductions from conciseness nudges — the
 * trigger is observed but the magnitude is not metered. Mirrors measure.py
 * `_get_savings_summary` which relocates verbosity_steer to the estimated tier.
 * The caller reprices to the baseline OUTPUT rate and adds as a separate pool.
 */
function estimatedVerbosityUsd(openclawDir, window) {
    const summary = readSavingsEventsByCategory(openclawDir, window);
    let total = 0;
    for (const cat of summary.categories) {
        if (cat.eventType !== "verbosity_steer")
            continue;
        total += cat.costSavedUsd;
    }
    return Math.max(0, total);
}
/**
 * Largest-remainder cent allocation. Rounds `total` to cents once, then
 * apportions those cents across `parts` so the returned parts always re-add to
 * the rounded total. Signed parts are handled by flooring toward -inf before
 * apportioning; `parts` need not already sum to `total` (any remainder surplus
 * lands on the first part so the sum always holds).
 */
function allocateCents(total, parts) {
    const cents = (x) => (Number.isFinite(x) ? x : 0) * 100;
    const totalCents = Math.round(cents(total));
    const out = parts.map((p) => Math.floor(cents(p)));
    let remainder = totalCents - out.reduce((a, b) => a + b, 0);
    const order = parts
        .map((p, i) => [cents(p) - Math.floor(cents(p)), i])
        .sort((a, b) => b[0] - a[0] || a[1] - b[1]);
    for (const [, i] of order) {
        if (remainder <= 0)
            break;
        out[i] += 1;
        remainder -= 1;
    }
    if (remainder !== 0 && out.length > 0)
        out[0] += remainder;
    return out.map((c) => c / 100);
}
function mixLabel(shares) {
    const top = Object.entries(shares).sort((a, b) => b[1] - a[1])[0];
    return top ? `${Math.round(top[1] * 100)}% ${top[0]}` : "n/a";
}
const NOT_READY = (status) => ({
    ready: false,
    status,
    monthlySavingsUsd: 0,
    savingsPerSession: 0,
    beforeCostPerSession: 0,
    afterCostPerSession: 0,
    beforeCacheHit: 0,
    afterCacheHit: 0,
    sessionsPerMonth: 0,
    trackedDays: 0,
    beforeMixLabel: "n/a",
    afterMixLabel: "n/a",
    cumulativeSavedUsd: 0,
    installDate: null,
    breakdown: [],
    counterfactualMonthlyUsd: 0,
    actualMonthlyUsd: 0,
    mainTransformationUsd: 0,
    subagentTransformationUsd: 0,
    compressionTransformationUsd: 0,
    compressionMeasuredUsd: 0,
    verbosityMeasuredUsd: 0,
    verbosityTransformationUsd: 0,
    transformationPct: 0,
    beforeOpus: 0,
    afterOpus: 0,
    premiumTokens: 0,
    premiumCostUsd: 0,
    baselineClassMixAvailable: false,
    baselineTtlMixAvailable: false,
});
/**
 * Compute the realized current-volume counterfactual transformation. `now` is
 * injectable for testing. See the module header for the full methodology.
 */
function computeRealizedSavings(openclawDir, days = 30, now = Date.now()) {
    let fresh = [];
    try {
        fresh = (0, session_parser_1.scanAllSessions)(openclawDir, 36500).map(toRecord);
    }
    catch {
        fresh = [];
    }
    const rawHistory = mergeAndPersistHistory(openclawDir, fresh);
    const excluded = new Map();
    const history = rawHistory.flatMap((r) => {
        if (r.input < MIN_INPUT_TOKENS || r.durationSec < MIN_DURATION_SECONDS)
            return [];
        if (!r.modelTokenBuckets || !Object.keys(r.modelTokenBuckets).length) {
            if ((0, event_pricing_1.resolvePricedModel)(r.model, openclawDir) !== null)
                return [r];
            excluded.set(r.sessionId, r.input + r.output + r.cacheRead + r.cacheWrite);
            return [];
        }
        const known = Object.fromEntries(Object.entries(r.modelTokenBuckets).filter(([model, b]) => {
            if ((0, event_pricing_1.resolvePricedModel)(model, openclawDir) !== null)
                return true;
            excluded.set(r.sessionId, (excluded.get(r.sessionId) ?? 0) + numClamp(b.input) + numClamp(b.output) + numClamp(b.cacheRead) + numClamp(b.cacheWrite));
            return false;
        }));
        const entries = Object.entries(known);
        if (!entries.length)
            return [];
        const sum = (field) => entries.reduce((n, [, b]) => n + numClamp(b[field]), 0);
        return [{ ...r, model: (0, event_pricing_1.resolvePricedModel)(r.model, openclawDir) !== null ? r.model : entries[0][0], modelTokenBuckets: known,
                input: sum("input"), output: sum("output"), cacheRead: sum("cacheRead"), cacheWrite: sum("cacheWrite"), cacheWrite1h: sum("cacheWrite1h"), cacheWrite5m: sum("cacheWrite5m") }];
    });
    if (history.length === 0) {
        const result = NOT_READY("no priced sessions yet");
        result.unpricedTokens = rawHistory.filter((r) => r.ts >= now - days * DAY_MS).reduce((n, r) => n + (excluded.get(r.sessionId) ?? 0), 0);
        return result;
    }
    // Preserve the stored baseline metadata for existing installs.
    const globalProxy = dominantPricedModel(history, openclawDir);
    const { baseline, reason, baselineBuilding } = getOrComputeBaseline(openclawDir, history, now, globalProxy);
    const installDate = new Date(history[0].ts).toISOString().slice(0, 10);
    if (!baseline) {
        const r = NOT_READY(reason);
        r.installDate = installDate;
        r.unpricedTokens = rawHistory.filter((row) => row.ts >= now - days * DAY_MS).reduce((n, row) => n + (excluded.get(row.sessionId) ?? 0), 0);
        if (baselineBuilding)
            r.baselineBuilding = baselineBuilding;
        return r;
    }
    // "After" = recent sessions in the lookback window, strictly after the
    // baseline window (cohort separation: never compare the user to themselves).
    const afterStart = Math.max(baseline.windowEnd, now - days * DAY_MS);
    const after = history.filter((r) => r.ts >= afterStart);
    const unpricedTokens = rawHistory.filter((r) => r.ts >= afterStart).reduce((n, r) => n + (excluded.get(r.sessionId) ?? 0), 0);
    if (after.length < AFTER_MIN_SESSIONS) {
        const r = NOT_READY(`building comparison (${after.length}/${AFTER_MIN_SESSIONS} recent sessions)`);
        r.installDate = installDate;
        r.unpricedTokens = unpricedTokens;
        r.beforeCostPerSession = baseline.stats.costPerSession;
        r.beforeMixLabel = mixLabel(baseline.stats.shares);
        return r;
    }
    const bs = baseline.stats;
    if (Object.keys(bs.shares).some((model) => !(0, event_pricing_1.resolvePricedModel)(model, openclawDir))) {
        const r = NOT_READY("baseline includes unpriced models");
        r.installDate = installDate;
        return r;
    }
    // Preserve the proxy field in existing frozen baselines.
    const proxy = baseline.proxy;
    // The "after" era's mix shares are needed for the ACTUAL arm. computeEraStats
    // is used here ONLY for the mix-share anchor (efficiency), not for volume.
    const as = computeEraStats(after, openclawDir, proxy);
    // EFFICIENCY ANCHORS from the frozen baseline (mix shares + pool cache-hit).
    // OpenClaw is non-Anthropic -> ALWAYS the user's OWN measured mix. NO 95% Opus
    // floor (never fabricate Opus they never ran). before_shares = baseline mix;
    // if the baseline mix is empty, fall back to the actual mix (routing lever -> 0).
    const beforeShares = Object.keys(bs.shares).length > 0 ? bs.shares : as.shares;
    const afterShares = as.shares;
    // CURRENT window = the `after` sessions, aggregated to confirm the
    // window is non-empty. The main pool is now priced per FROZEN typical session (below),
    // so current-window VOLUME is no longer priced directly.
    let F = 0, CR = 0, CW = 0;
    for (const r of after) {
        F += r.input;
        CR += r.cacheRead;
        CW += r.cacheWrite;
    }
    const totalIn = F + CR + CW;
    if (totalIn <= 0) {
        const r = NOT_READY("no recent billed volume");
        r.installDate = installDate;
        return r;
    }
    // Provider cache reuse is not attributed to Token Optimizer.
    const observedHit = tCacheHit(F, CR);
    const afterWindowDays = Math.max(1, (now - afterStart) / DAY_MS);
    // Window is `days`; scale aggregate window cost to a monthly figure.
    const monthlyScale = 30 / Math.max(1, days);
    const m = (x) => x * monthlyScale;
    // Price the frozen typical session in both arms, excluding premium shares.
    const tFi = bs.meanTokens.fi;
    const tCr = bs.meanTokens.cr;
    const tCw = bs.meanTokens.cw;
    const tOut = bs.meanTokens.out;
    const comparison = comparableMix(bs.classShares, as.classShares, beforeShares, afterShares, openclawDir);
    const ttlComparison = (kind) => comparableMix(bs.classShares && bs.ttlShares ? { ...bs.classShares, cw: bs.ttlShares[kind] } : bs.classShares, as.classShares && as.ttlShares ? { ...as.classShares, cw: as.ttlShares[kind] } : as.classShares, beforeShares, afterShares, openclawDir);
    const oneHour = ttlComparison("oneHour"), fiveMinute = ttlComparison("fiveMinute");
    let premiumTokens = 0, premiumCostUsd = 0;
    let comparableWrites = 0, comparableWrite1h = 0;
    const baselineTokens = { fi: tFi, cr: tCr, cw: tCw, out: tOut };
    const hasComparableWork = (r) => {
        const buckets = r.modelTokenBuckets && Object.keys(r.modelTokenBuckets).length ? r.modelTokenBuckets : { [r.model]: { input: r.input, output: r.output, cacheRead: r.cacheRead, cacheWrite: r.cacheWrite, cacheWrite1h: r.cacheWrite1h, cacheWrite5m: r.cacheWrite5m } };
        return Object.entries(buckets).some(([model, b]) => {
            if (!b)
                return false;
            const v = { fi: numClamp(b.input), cr: numClamp(b.cacheRead), cw: numClamp(b.cacheWrite), out: numClamp(b.output) };
            return CLASSES.some((c) => baselineTokens[c] > 0 && v[c] > 0 && classRate(model, c, openclawDir) <= comparison.ceiling[c] + 1e-15);
        });
    };
    const comparableAfter = after.filter(hasComparableWork);
    const sessionsPerMonth = (comparableAfter.length / afterWindowDays) * 30;
    for (const r of after) {
        const buckets = r.modelTokenBuckets && Object.keys(r.modelTokenBuckets).length ? r.modelTokenBuckets : { [r.model]: { input: r.input, output: r.output, cacheRead: r.cacheRead, cacheWrite: r.cacheWrite, cacheWrite1h: r.cacheWrite1h, cacheWrite5m: r.cacheWrite5m } };
        for (const [model, bucket] of Object.entries(buckets)) {
            if (!bucket)
                continue;
            const values = { fi: numClamp(bucket.input), cr: numClamp(bucket.cacheRead), cw: numClamp(bucket.cacheWrite), out: numClamp(bucket.output) };
            const costs = sessionClassCost({ ...r, model, input: values.fi, output: values.out, cacheRead: values.cr, cacheWrite: values.cw, cacheWrite1h: numClamp(bucket.cacheWrite1h), cacheWrite5m: numClamp(bucket.cacheWrite5m), modelTokenBuckets: undefined }, proxy, openclawDir);
            for (const c of CLASSES)
                if (baselineTokens[c] > 0 && classRate(model, c, openclawDir) > comparison.ceiling[c] + 1e-15) {
                    premiumTokens += values[c];
                    premiumCostUsd += costs[c];
                }
            if (classRate(model, "cw", openclawDir) <= comparison.ceiling.cw + 1e-15) {
                comparableWrites += values.cw;
                comparableWrite1h += Math.min(values.cw, numClamp(bucket.cacheWrite1h));
            }
        }
    }
    const write1hFraction = comparableWrites > 0 ? Math.min(1, comparableWrite1h / comparableWrites) : 0;
    const eFi = comparison.eligible.fi > 0 ? tFi : 0, eCr = comparison.eligible.cr > 0 ? tCr : 0;
    const eCw1h = oneHour.eligible.cw > 0 ? tCw * write1hFraction : 0;
    const eCw5m = fiveMinute.eligible.cw > 0 ? tCw * (1 - write1hFraction) : 0;
    const eOut = comparison.eligible.out > 0 ? tOut : 0;
    // Price the eligible current volume at the frozen baseline mix.
    // Both arms use the current cache-write TTL split.
    const oldCps = priceClasses(eFi, eCr, 0, eOut, comparison.before, beforeShares, proxy, openclawDir) +
        priceCacheWrite(eCw1h, eCw1h, 0, oneHour.before.cw, proxy, openclawDir) +
        priceCacheWrite(eCw5m, 0, eCw5m, fiveMinute.before.cw, proxy, openclawDir);
    // NOW / session uses the same eligible tokens at the current mix.
    const curCRs = eCr;
    const curFs = eFi;
    const nowCps = priceClasses(curFs, curCRs, 0, eOut, comparison.after, afterShares, proxy, openclawDir) +
        priceCacheWrite(eCw1h, eCw1h, 0, oneHour.after.cw, proxy, openclawDir) +
        priceCacheWrite(eCw5m, 0, eCw5m, fiveMinute.after.cw, proxy, openclawDir);
    if (!Number.isFinite(oldCps) || !Number.isFinite(nowCps) ||
        oldCps < 0 || nowCps < 0) {
        const r = NOT_READY("insufficient billed cost");
        r.installDate = installDate;
        return r;
    }
    // Monthly main arms = the per-session anchor x the monthly session count.
    const cfMonthlyMain = oldCps * sessionsPerMonth;
    const actualMonthlyMain = nowCps * sessionsPerMonth;
    // COMPRESSION ADD-BACK (#3): directly-metered tokens TO removed from context.
    // DISJOINT from the billed pool (already removed before billing). Reprice the
    // measured $ to the baseline input mix; actual for this pool is 0, so the whole
    // repriced value is transformation. Clamp >= 0.
    // F4: window the compression events to the SAME `days` lookback as the billed
    // volume (mirrors Python `_get_savings_summary(days=days)`), so a stale event
    // outside the window is not summed and then monthly-scaled.
    const compMeasured = measuredCompressionUsd(openclawDir, { days, now });
    const inAfter = inputRate(comparison.after.fi, proxy, openclawDir);
    const inBefore = inputRate(comparison.before.fi, proxy, openclawDir);
    const compReprice = inAfter > 0 ? inBefore / inAfter : 1;
    const compressionAddback = Math.max(0, compMeasured * compReprice);
    // MAIN transformation (monthly) = (old_cps - now_cps) x monthly session count, UNCLAMPED.
    // A net-negative main pool (now marginally costlier than the frozen baseline this period)
    // is carried honestly into the combined net below rather than clamped to 0 -- clamping it
    // while the "now vs old way" arms stayed honest is what let the headline assert a saving
    // beside net-negative arms (the honest-net invariant). The headline is now the honest net of the arms.
    const mainTransformation = cfMonthlyMain - actualMonthlyMain;
    // SUBAGENT pool (#2) = 0. OpenClaw has NO Claude-style sidechains (subagent
    // transcripts), so there is no separate sidechain pool to price. Documented gap.
    const subagentTransformation = 0;
    // VERBOSITY-STEER ADD-BACK (#4): estimated output tokens never produced due
    // to lean-output conciseness nudges. The main counterfactual holds output volume
    // constant (both arms price the same O), so the output reduction from nudges
    // is NOT captured by mainTransformation. These are estimated savings logged
    // to savings-events.jsonl but excluded from measuredCompressionUsd. Reprice
    // to the baseline OUTPUT rate: the old way would have produced verbose output
    // at the baseline model mix's output rate. Actual for this pool is 0, so the
    // whole repriced value is transformation. Mirrors measure.py verbosity_addback.
    const vsMeasured = estimatedVerbosityUsd(openclawDir, { days, now });
    const outAfter = pricePool(0, 0, 1_000_000, comparison.after.out, proxy, openclawDir);
    const outBefore = pricePool(0, 0, 1_000_000, comparison.before.out, proxy, openclawDir);
    const vsReprice = outAfter > 0 ? outBefore / outAfter : 1;
    const verbosityAddback = Math.max(0, vsMeasured * vsReprice);
    const compressionMonthly = m(compressionAddback);
    const verbosityMonthly = m(verbosityAddback);
    // Combined arms (headline spans the main pool + the compression add-back +
    // the verbosity add-back; both add-back pools' actual is 0, so their
    // counterfactual == their contribution).
    const actualMonthly = actualMonthlyMain;
    const counterfactualMonthly = cfMonthlyMain + compressionMonthly + verbosityMonthly;
    // HEADLINE = the honest net of the arms (== counterfactual - actual). Equivalent to
    // mainTransformation + subagent(0) + compression + verbosity, so the per-lever waterfall
    // below decomposes exactly this quantity. Can be <= 0 (efficiency flat / baseline cheaper);
    // the dashboard hides the hero in that case rather than clamping it positive.
    const net = counterfactualMonthly - actualMonthly;
    // Public contract: the headline is NEVER negative. When net <= 0 it reads 0 and the
    // dashboard hides the hero (mirrors measure.py's net_negative path); when shown (> 0) it
    // EQUALS counterfactual - actual exactly, so the top line and the arms can never disagree.
    const transformation = Math.max(0, net);
    const transformationPct = counterfactualMonthly > 0 ? transformation / counterfactualMonthly : 0;
    // --- Attribution breakdown (waterfall over the efficiency levers) ---
    // Morph the counterfactual into the actual one lever at a time. Routing first
    // (baseline footprint INCLUDING cache-write, repriced from baseline mix to the
    // actual mix). Compression is a separate lever.
    // Always decompose the main pool, even when it is net-negative, so the waterfall sums to
    // the honest net including a pool that grew (shown honestly as a "-$X" lever).
    const vRouteS = nowCps;
    const sRoute = (oldCps - vRouteS) * sessionsPerMonth; // before->after mix (incl. cache-write)
    // Display cents are allocated once off the signed net so the rendered levers
    // always add to the rendered headline; monthlyUsd keeps the exact raw values.
    const leverDisplay = allocateCents(net, [sRoute, compressionMonthly, verbosityMonthly]);
    const breakdown = [
        { key: "routing", label: "Model mix change (excluding premium choices)", monthlyUsd: sRoute, monthlyUsdDisplay: leverDisplay[0] },
        // Subagent lever omitted (OpenClaw has no sidechains; always 0).
        { key: "context_compression", label: "Lighter context (metered removals)", monthlyUsd: compressionMonthly, monthlyUsdDisplay: leverDisplay[1] },
        { key: "verbosity_steer", label: "Lean output nudges (less output, estimated)", monthlyUsd: verbosityMonthly, monthlyUsdDisplay: leverDisplay[2] },
    ];
    // Cumulative: apply the per-session transformation rate across every
    // post-baseline session (transformation per current session * total sessions).
    const perSessionTransformation = transformation / Math.max(1, comparableAfter.length);
    const allAfter = history.filter((r) => r.ts >= baseline.windowEnd && hasComparableWork(r));
    const cumulative = perSessionTransformation * allAfter.length;
    // Per-session arms (the dashboard reads before/after CPS): divide the MAIN-POOL
    // monthly arms by the current session count. The add-back pools (compression,
    // verbosity) are aggregate — not per-session in nature — so they stay out of
    // the per-session panel. Matches Python's before_cps = counterfactual_monthly / recent_n
    // where counterfactual_monthly is main-only.
    // Per-session arms = the FROZEN typical-session anchors directly (stable across runs).
    // before_cps depends only on frozen inputs; after_cps moves only with efficiency.
    const beforeCps = oldCps;
    const afterCps = nowCps;
    return {
        ready: true,
        status: "ok",
        monthlySavingsUsd: round2(transformation),
        savingsPerSession: round4(beforeCps - afterCps),
        beforeCostPerSession: round4(beforeCps),
        afterCostPerSession: round4(afterCps),
        beforeCacheHit: round4(tCacheHit(tFi, tCr)),
        afterCacheHit: round4(observedHit),
        sessionsPerMonth,
        trackedDays: Math.max(1, Math.floor(afterWindowDays)),
        beforeMixLabel: mixLabel(beforeShares),
        afterMixLabel: mixLabel(afterShares),
        cumulativeSavedUsd: round2(cumulative),
        installDate,
        breakdown,
        counterfactualMonthlyUsd: round2(counterfactualMonthly),
        actualMonthlyUsd: round2(actualMonthly),
        mainTransformationUsd: round2(mainTransformation),
        subagentTransformationUsd: subagentTransformation,
        compressionTransformationUsd: round2(compressionMonthly),
        compressionMeasuredUsd: round2(compMeasured),
        verbosityTransformationUsd: round2(verbosityMonthly),
        verbosityMeasuredUsd: round2(vsMeasured),
        transformationPct: round4(transformationPct),
        beforeOpus: round4(beforeShares.opus ?? 0),
        afterOpus: round4(afterShares.opus ?? 0),
        premiumTokens,
        premiumCostUsd: round2(premiumCostUsd),
        unpricedTokens,
        baselineClassMixAvailable: !!bs.classShares,
        baselineTtlMixAvailable: !!bs.ttlShares,
    };
}
function round2(x) {
    return Math.round(x * 100) / 100;
}
function round4(x) {
    return Math.round(x * 10000) / 10000;
}
//# sourceMappingURL=savings.js.map