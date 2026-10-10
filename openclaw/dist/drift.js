"use strict";
/**
 * Drift Detection for OpenClaw.
 *
 * Snapshots the OpenClaw config at a point in time.
 * Later, diffs against current state to catch config creep.
 */
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
exports.captureSnapshot = captureSnapshot;
exports.detectDrift = detectDrift;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const redact_1 = require("./redact");
// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------
const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const SNAPSHOT_DIR = path.join(HOME, ".openclaw", "token-optimizer", "snapshots");
// ---------------------------------------------------------------------------
// Snapshot capture
// ---------------------------------------------------------------------------
function countDir(dir, ext) {
    if (!fs.existsSync(dir))
        return { count: 0, names: [] };
    try {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        const filtered = ext
            ? entries.filter((e) => e.name.endsWith(ext))
            : entries.filter((e) => e.isDirectory());
        return { count: filtered.length, names: filtered.map((e) => e.name).sort() };
    }
    catch {
        return { count: 0, names: [] };
    }
}
function fileSize(filePath) {
    try {
        if (!fs.existsSync(filePath))
            return 0;
        return fs.statSync(filePath).size;
    }
    catch {
        return 0;
    }
}
function readModelConfig(openclawDir) {
    const configPath = path.join(openclawDir, "config.json");
    try {
        if (!fs.existsSync(configPath))
            return "";
        const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
        const models = config.models ?? config.model ?? {};
        return JSON.stringify(models);
    }
    catch {
        return "";
    }
}
function captureSnapshot(openclawDir) {
    const skills = countDir(path.join(openclawDir, "skills"));
    const agents = countDir(path.join(openclawDir, "agents"));
    const crons = countDir(path.join(openclawDir, "cron"), ".json");
    const snapshot = {
        capturedAt: new Date().toISOString(),
        skillCount: skills.count,
        agentCount: agents.count,
        cronCount: crons.count,
        soulMdSize: fileSize(path.join(openclawDir, "SOUL.md")),
        memoryMdSize: fileSize(path.join(openclawDir, "MEMORY.md")),
        agentsMdSize: fileSize(path.join(openclawDir, "AGENTS.md")),
        toolsMdSize: fileSize(path.join(openclawDir, "TOOLS.md")),
        // Directory entry names and config text are user-derived strings persisted
        // verbatim; a credential-shaped name must never land in a snapshot raw.
        // Redaction is deterministic, so drift diffs still compare correctly.
        modelConfig: (0, redact_1.redact)(readModelConfig(openclawDir)),
        skills: skills.names.map(redact_1.redact),
        agents: agents.names.map(redact_1.redact),
    };
    fs.mkdirSync(SNAPSHOT_DIR, { recursive: true, mode: 0o700 });
    const filename = `snapshot-${snapshot.capturedAt.slice(0, 10)}.json`;
    const filePath = path.join(SNAPSHOT_DIR, filename);
    fs.writeFileSync(filePath, JSON.stringify(snapshot, null, 2), { encoding: "utf-8", mode: 0o600 });
    return filePath;
}
// ---------------------------------------------------------------------------
// Drift detection
// ---------------------------------------------------------------------------
function loadLatestSnapshot() {
    if (!fs.existsSync(SNAPSHOT_DIR))
        return null;
    try {
        const files = fs.readdirSync(SNAPSHOT_DIR)
            .filter((f) => f.startsWith("snapshot-") && f.endsWith(".json"))
            .sort()
            .reverse();
        if (files.length === 0)
            return null;
        const content = fs.readFileSync(path.join(SNAPSHOT_DIR, files[0]), "utf-8");
        return JSON.parse(content);
    }
    catch {
        return null;
    }
}
function buildCurrentSnapshot(openclawDir) {
    const skills = countDir(path.join(openclawDir, "skills"));
    const agents = countDir(path.join(openclawDir, "agents"));
    const crons = countDir(path.join(openclawDir, "cron"), ".json");
    return {
        capturedAt: new Date().toISOString(),
        skillCount: skills.count,
        agentCount: agents.count,
        cronCount: crons.count,
        soulMdSize: fileSize(path.join(openclawDir, "SOUL.md")),
        memoryMdSize: fileSize(path.join(openclawDir, "MEMORY.md")),
        agentsMdSize: fileSize(path.join(openclawDir, "AGENTS.md")),
        toolsMdSize: fileSize(path.join(openclawDir, "TOOLS.md")),
        modelConfig: readModelConfig(openclawDir),
        skills: skills.names,
        agents: agents.names,
    };
}
function diffArrays(label, prev, curr) {
    const changes = [];
    const prevSet = new Set(prev);
    const currSet = new Set(curr);
    for (const name of curr) {
        if (!prevSet.has(name)) {
            changes.push({ component: label, type: "added", details: name });
        }
    }
    for (const name of prev) {
        if (!currSet.has(name)) {
            changes.push({ component: label, type: "removed", details: name });
        }
    }
    return changes;
}
function diffSize(component, prevSize, currSize, threshold = 100) {
    const diff = currSize - prevSize;
    if (Math.abs(diff) < threshold)
        return null;
    const direction = diff > 0 ? "grew" : "shrank";
    const absDiff = Math.abs(diff);
    return {
        component,
        type: "changed",
        details: `${direction} by ${absDiff} bytes (${prevSize} -> ${currSize})`,
    };
}
function detectDrift(openclawDir) {
    const prev = loadLatestSnapshot();
    if (!prev) {
        return {
            hasDrift: false,
            snapshotDate: "none",
            changes: [{
                    component: "snapshot",
                    type: "added",
                    details: "No previous snapshot found. Run with --snapshot first.",
                }],
        };
    }
    const curr = buildCurrentSnapshot(openclawDir);
    const changes = [];
    // Skills diff
    changes.push(...diffArrays("Skills", prev.skills, curr.skills));
    // Agents diff
    changes.push(...diffArrays("Agents", prev.agents, curr.agents));
    // Cron count
    if (curr.cronCount !== prev.cronCount) {
        const diff = curr.cronCount - prev.cronCount;
        changes.push({
            component: "Cron configs",
            type: diff > 0 ? "added" : "removed",
            details: `${Math.abs(diff)} config(s) ${diff > 0 ? "added" : "removed"} (${prev.cronCount} -> ${curr.cronCount})`,
        });
    }
    // File size diffs
    const sizeDiffs = [
        diffSize("SOUL.md", prev.soulMdSize, curr.soulMdSize),
        diffSize("MEMORY.md", prev.memoryMdSize, curr.memoryMdSize),
        diffSize("AGENTS.md", prev.agentsMdSize, curr.agentsMdSize),
        diffSize("TOOLS.md", prev.toolsMdSize, curr.toolsMdSize),
    ];
    for (const d of sizeDiffs) {
        if (d)
            changes.push(d);
    }
    // Model config diff
    if (prev.modelConfig !== curr.modelConfig) {
        changes.push({
            component: "Model config",
            type: "changed",
            details: "Model configuration has changed since last snapshot.",
        });
    }
    return {
        hasDrift: changes.length > 0,
        snapshotDate: prev.capturedAt.slice(0, 10),
        changes,
    };
}
//# sourceMappingURL=drift.js.map