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
exports.resolvePricedModel = resolvePricedModel;
exports.loadSavingsSessions = loadSavingsSessions;
exports.priceSavingsEvent = priceSavingsEvent;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const pricing_1 = require("./pricing");
function resolvePricedModel(model, openclawDir) {
    const pricing = (0, pricing_1.getPricing)(openclawDir);
    const own = (key) => Object.prototype.hasOwnProperty.call(pricing, key);
    if (own(model))
        return model;
    const id = model.toLowerCase();
    const legacy = /(?:^|[/:.])claude-(\d+)(?:[-.](\d+))?-(opus|sonnet|haiku)(?=$|[-_:])/.exec(id);
    if (legacy) {
        const [, major, minor, family] = legacy;
        const exact = `${family}-${major}${minor ? `-${minor}` : ""}`;
        if (own(exact))
            return exact;
        if (family === "sonnet" && major === "3" && (!minor || minor === "5" || minor === "7") && own("sonnet-legacy")) {
            return "sonnet-legacy";
        }
        return null;
    }
    const generation = /(?:^|[^a-z])(fable|mythos|opus|sonnet|haiku)[-._]?(\d+)(?:[-._](\d{1,2})(?!\d))?/.exec(id);
    if (generation) {
        const [, family, major, minor] = generation;
        const exact = `${family}-${major}${minor ? `-${minor}` : ""}`;
        if (own(exact))
            return exact;
        if (family === "mythos") {
            const alias = `fable-${major}${minor ? `-${minor}` : ""}`;
            if (own(alias))
                return alias;
        }
        return null;
    }
    const key = (0, pricing_1.claudePricingKey)(model, pricing) ?? (0, pricing_1.pricedPrefixKey)(model, pricing) ?? (0, pricing_1.normalizeModelName)(model);
    return key && own(key) ? key : null;
}
function loadSavingsSessions(openclawDir) {
    const out = new Map();
    try {
        const rows = JSON.parse(fs.readFileSync(path.join(openclawDir, "token-optimizer", "session-history.json"), "utf8"));
        if (Array.isArray(rows))
            for (const row of rows)
                if (row && typeof row.sessionId === "string")
                    out.set(row.sessionId, row);
    }
    catch { /* session history not available */ }
    return out;
}
function priceSavingsEvent(tokens, eventType, session, openclawDir) {
    const amount = Number.isFinite(tokens) ? Math.abs(tokens) : 0;
    const sign = tokens < 0 ? -1 : 1;
    if (!session)
        return { cost: 0, unpricedTokens: amount };
    const output = eventType === "verbosity_steer" || eventType === "verbosity_steer_measured";
    const buckets = session.modelTokenBuckets;
    const weights = [];
    if (buckets && typeof buckets === "object") {
        for (const [model, bucket] of Object.entries(buckets)) {
            if (!bucket || typeof bucket !== "object")
                continue;
            const numeric = (v) => typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0;
            const weight = output ? numeric(bucket.output) : numeric(bucket.input) + numeric(bucket.cacheRead) + numeric(bucket.cacheWrite);
            if (weight > 0)
                weights.push([model, weight]);
        }
    }
    if (weights.length === 0 && session.model && !buckets)
        weights.push([session.model, 1]);
    const total = weights.reduce((sum, [, weight]) => sum + weight, 0);
    if (!total)
        return { cost: 0, unpricedTokens: amount };
    let cost = 0, unpricedTokens = 0;
    for (const [model, weight] of weights) {
        const part = amount * weight / total;
        const key = resolvePricedModel(model, openclawDir);
        if (!key) {
            unpricedTokens += part;
            continue;
        }
        const card = (0, pricing_1.getPricing)(openclawDir)[key];
        cost += part * (output ? card.output : card.input) * (0, pricing_1.tierMultiplier)((0, pricing_1.loadPricingTier)(openclawDir), key);
    }
    return { cost: cost * sign, unpricedTokens };
}
//# sourceMappingURL=event-pricing.js.map