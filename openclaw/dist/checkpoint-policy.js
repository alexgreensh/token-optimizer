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
exports.QUALITY_THRESHOLDS = exports.FILL_BANDS = void 0;
exports.checkpointSessionDir = checkpointSessionDir;
exports.checkpointManifestPath = checkpointManifestPath;
exports.getCheckpointState = getCheckpointState;
exports.clearCheckpointState = clearCheckpointState;
exports.registerWriteEvent = registerWriteEvent;
exports.shouldEvaluateRuntimeState = shouldEvaluateRuntimeState;
exports.markEvaluated = markEvaluated;
exports.buildRuntimeSnapshot = buildRuntimeSnapshot;
exports.maybeDecideSnapshotCheckpoint = maybeDecideSnapshotCheckpoint;
exports.maybeDecidePreFanoutCheckpoint = maybeDecidePreFanoutCheckpoint;
exports.maybeDecideEditBatchCheckpoint = maybeDecideEditBatchCheckpoint;
exports.recordCheckpointDecision = recordCheckpointDecision;
exports.registerCheckpointCapture = registerCheckpointCapture;
exports.getCheckpointTelemetrySummary = getCheckpointTelemetrySummary;
exports.getCheckpointFiles = getCheckpointFiles;
exports.cleanupPolicyArtifacts = cleanupPolicyArtifacts;
exports.getCheckpointHealth = getCheckpointHealth;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const quality_1 = require("./quality");
exports.FILL_BANDS = [20, 35, 50, 65, 80];
exports.QUALITY_THRESHOLDS = [80, 70, 50, 40];
const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const CHECKPOINT_ROOT = path.join(HOME, ".openclaw", "token-optimizer", "checkpoints");
const CHECKPOINT_COOLDOWN_MS = Number.parseInt(process.env.TOKEN_OPTIMIZER_CHECKPOINT_COOLDOWN_MS ?? "90000", 10);
const EVALUATION_COOLDOWN_MS = Number.parseInt(process.env.TOKEN_OPTIMIZER_RUNTIME_EVALUATION_COOLDOWN_MS ?? "30000", 10);
const EDIT_BATCH_WRITE_THRESHOLD = Number.parseInt(process.env.TOKEN_OPTIMIZER_EDIT_BATCH_WRITE_THRESHOLD ?? "4", 10);
const EDIT_BATCH_FILE_THRESHOLD = Number.parseInt(process.env.TOKEN_OPTIMIZER_EDIT_BATCH_FILE_THRESHOLD ?? "3", 10);
const CHECKPOINT_TELEMETRY_ENABLED = ["1", "true", "yes", "on"].includes((process.env.TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY ?? "0").toLowerCase());
const STATE_FILENAME = "policy-state.json";
const EVENTS_FILENAME = "checkpoint-events.jsonl";
const states = new Map();
function sanitizeSessionId(id) {
    const clean = id.replace(/[^a-zA-Z0-9_-]/g, "_");
    if (!clean || clean === "." || clean === "..")
        return "invalid-session";
    return clean;
}
function isWithinDir(root, candidate) {
    const relative = path.relative(root, candidate);
    return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}
function resolvedCheckpointRoot() {
    if (!HOME || !fs.existsSync(CHECKPOINT_ROOT))
        return null;
    try {
        const stat = fs.lstatSync(CHECKPOINT_ROOT);
        if (stat.isSymbolicLink())
            return null;
        return fs.realpathSync(CHECKPOINT_ROOT);
    }
    catch {
        return null;
    }
}
function resolveSafeExistingCheckpointDir(dirPath) {
    const root = resolvedCheckpointRoot();
    if (!root || !fs.existsSync(dirPath))
        return null;
    try {
        const stat = fs.lstatSync(dirPath);
        if (stat.isSymbolicLink() || !stat.isDirectory())
            return null;
        const realDir = fs.realpathSync(dirPath);
        return isWithinDir(root, realDir) ? realDir : null;
    }
    catch {
        return null;
    }
}
function resolveSafeExistingCheckpointFile(filePath, allowedDir) {
    if (!fs.existsSync(filePath))
        return null;
    try {
        const stat = fs.lstatSync(filePath);
        if (stat.isSymbolicLink() || !stat.isFile())
            return null;
        const realFile = fs.realpathSync(filePath);
        return isWithinDir(allowedDir, realFile) ? realFile : null;
    }
    catch {
        return null;
    }
}
function ensureSafeCheckpointRootForWrites() {
    if (!HOME) {
        throw new Error("Home directory is not set");
    }
    if (!fs.existsSync(CHECKPOINT_ROOT)) {
        fs.mkdirSync(CHECKPOINT_ROOT, { recursive: true, mode: 0o700 });
    }
    const stat = fs.lstatSync(CHECKPOINT_ROOT);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
        throw new Error("Checkpoint root is unsafe");
    }
    return fs.realpathSync(CHECKPOINT_ROOT);
}
function ensureSafeCheckpointDirForWrites(sessionId) {
    const root = ensureSafeCheckpointRootForWrites();
    const safeSession = sanitizeSessionId(sessionId);
    const dir = checkpointSessionDir(safeSession);
    if (fs.existsSync(dir)) {
        const stat = fs.lstatSync(dir);
        if (stat.isSymbolicLink() || !stat.isDirectory()) {
            throw new Error("Checkpoint session directory is unsafe");
        }
    }
    else {
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    }
    const realDir = fs.existsSync(dir)
        ? fs.realpathSync(dir)
        : path.join(root, safeSession);
    if (!isWithinDir(root, realDir)) {
        throw new Error("Checkpoint session directory escapes checkpoint root");
    }
    return realDir;
}
function safeCheckpointFilePathForWrite(dir, filename) {
    const target = path.join(dir, filename);
    if (fs.existsSync(target)) {
        const stat = fs.lstatSync(target);
        if (stat.isSymbolicLink() || !stat.isFile()) {
            throw new Error("Checkpoint file is unsafe");
        }
        const realFile = fs.realpathSync(target);
        if (!isWithinDir(dir, realFile)) {
            throw new Error("Checkpoint file escapes checkpoint session directory");
        }
        return realFile;
    }
    const resolved = path.resolve(target);
    if (!isWithinDir(dir, resolved)) {
        throw new Error("Checkpoint file escapes checkpoint session directory");
    }
    return resolved;
}
function safeCheckpointRootFilePathForWrite(filename) {
    const root = ensureSafeCheckpointRootForWrites();
    const target = path.join(root, filename);
    if (fs.existsSync(target)) {
        const stat = fs.lstatSync(target);
        if (stat.isSymbolicLink() || !stat.isFile()) {
            throw new Error("Checkpoint root file is unsafe");
        }
        const realFile = fs.realpathSync(target);
        if (!isWithinDir(root, realFile)) {
            throw new Error("Checkpoint root file escapes checkpoint root");
        }
        return realFile;
    }
    const resolved = path.resolve(target);
    if (!isWithinDir(root, resolved)) {
        throw new Error("Checkpoint root file escapes checkpoint root");
    }
    return resolved;
}
function checkpointSessionDir(sessionId) {
    return path.join(CHECKPOINT_ROOT, sanitizeSessionId(sessionId));
}
function checkpointManifestPath(sessionId) {
    return path.join(checkpointSessionDir(sessionId), "manifest.jsonl");
}
function checkpointStatePath(sessionId) {
    return path.join(checkpointSessionDir(sessionId), STATE_FILENAME);
}
function persistState(sessionId, state) {
    const dir = ensureSafeCheckpointDirForWrites(sessionId);
    const statePath = safeCheckpointFilePathForWrite(dir, STATE_FILENAME);
    fs.writeFileSync(statePath, JSON.stringify({
        capturedFillBands: [...state.capturedFillBands],
        capturedQualityThresholds: [...state.capturedQualityThresholds],
        capturedMilestones: [...state.capturedMilestones],
        lastCheckpointAt: state.lastCheckpointAt,
        lastEvaluatedAt: state.lastEvaluatedAt,
        editWriteCount: state.editWriteCount,
        editedFiles: [...state.editedFiles],
        editBatchMarkerWrites: state.editBatchMarkerWrites,
        editBatchMarkerFiles: state.editBatchMarkerFiles,
    }, null, 2), { encoding: "utf-8", mode: 0o600 });
}
function loadPersistedState(sessionId) {
    const sessionDir = resolveSafeExistingCheckpointDir(checkpointSessionDir(sessionId));
    if (!sessionDir)
        return null;
    const statePath = resolveSafeExistingCheckpointFile(path.join(sessionDir, STATE_FILENAME), sessionDir);
    if (!statePath)
        return null;
    try {
        const raw = JSON.parse(fs.readFileSync(statePath, "utf-8"));
        return {
            capturedFillBands: new Set(raw.capturedFillBands ?? []),
            capturedQualityThresholds: new Set(raw.capturedQualityThresholds ?? []),
            capturedMilestones: new Set(raw.capturedMilestones ?? []),
            lastCheckpointAt: raw.lastCheckpointAt ?? 0,
            lastEvaluatedAt: raw.lastEvaluatedAt ?? 0,
            editWriteCount: raw.editWriteCount ?? 0,
            editedFiles: new Set(raw.editedFiles ?? []),
            editBatchMarkerWrites: raw.editBatchMarkerWrites ?? 0,
            editBatchMarkerFiles: raw.editBatchMarkerFiles ?? 0,
        };
    }
    catch {
        return null;
    }
}
function getCheckpointState(sessionId) {
    const existing = states.get(sessionId);
    if (existing)
        return existing;
    const persisted = loadPersistedState(sessionId);
    if (persisted) {
        states.set(sessionId, persisted);
        return persisted;
    }
    const created = {
        capturedFillBands: new Set(),
        capturedQualityThresholds: new Set(),
        capturedMilestones: new Set(),
        lastCheckpointAt: 0,
        lastEvaluatedAt: 0,
        editWriteCount: 0,
        editedFiles: new Set(),
        editBatchMarkerWrites: 0,
        editBatchMarkerFiles: 0,
    };
    states.set(sessionId, created);
    return created;
}
function clearCheckpointState(sessionId) {
    states.delete(sessionId);
}
function registerWriteEvent(sessionId, filePath) {
    const state = getCheckpointState(sessionId);
    state.editWriteCount += 1;
    if (filePath)
        state.editedFiles.add(filePath);
    return state;
}
function shouldEvaluateRuntimeState(sessionId, nowMs = Date.now()) {
    const state = getCheckpointState(sessionId);
    return nowMs - state.lastEvaluatedAt >= EVALUATION_COOLDOWN_MS;
}
function markEvaluated(sessionId, nowMs = Date.now()) {
    const state = getCheckpointState(sessionId);
    state.lastEvaluatedAt = nowMs;
}
function checkpointCooldownActive(state, nowMs) {
    return nowMs - state.lastCheckpointAt < CHECKPOINT_COOLDOWN_MS;
}
function buildRuntimeSnapshot(run, contextAudit) {
    const ctxWindow = (0, quality_1.contextWindowForModel)(run.model);
    const dynamicFill = ctxWindow > 0 ? (run.tokens.input / ctxWindow) * 100 : 0;
    const overheadFill = contextAudit ? (contextAudit.totalOverhead / ctxWindow) * 100 : 0;
    const fillPct = Math.max(dynamicFill, overheadFill);
    const qualityScore = (0, quality_1.scoreSessionQuality)(run).score;
    return {
        fillPct,
        qualityScore,
    };
}
function maybeDecideSnapshotCheckpoint(sessionId, snapshot, nowMs = Date.now()) {
    const state = getCheckpointState(sessionId);
    if (checkpointCooldownActive(state, nowMs))
        return null;
    let targetBand = null;
    for (const band of exports.FILL_BANDS) {
        if (snapshot.fillPct >= band && !state.capturedFillBands.has(band)) {
            targetBand = band;
        }
    }
    if (targetBand !== null) {
        return {
            trigger: `progressive-${targetBand}`,
            fillPct: snapshot.fillPct,
            qualityScore: snapshot.qualityScore,
        };
    }
    for (const threshold of exports.QUALITY_THRESHOLDS) {
        if (snapshot.qualityScore < threshold && !state.capturedQualityThresholds.has(threshold)) {
            return {
                trigger: `quality-${threshold}`,
                fillPct: snapshot.fillPct,
                qualityScore: snapshot.qualityScore,
            };
        }
    }
    return null;
}
function maybeDecidePreFanoutCheckpoint(sessionId, snapshot, nowMs = Date.now()) {
    const state = getCheckpointState(sessionId);
    if (checkpointCooldownActive(state, nowMs) || state.capturedMilestones.has("pre-fanout")) {
        return null;
    }
    return {
        trigger: "milestone-pre-fanout",
        fillPct: snapshot?.fillPct,
        qualityScore: snapshot?.qualityScore,
    };
}
function maybeDecideEditBatchCheckpoint(sessionId, snapshot, nowMs = Date.now()) {
    const state = getCheckpointState(sessionId);
    if (checkpointCooldownActive(state, nowMs))
        return null;
    const writeDelta = state.editWriteCount - state.editBatchMarkerWrites;
    const fileDelta = state.editedFiles.size - state.editBatchMarkerFiles;
    if (writeDelta < EDIT_BATCH_WRITE_THRESHOLD &&
        fileDelta < EDIT_BATCH_FILE_THRESHOLD) {
        return null;
    }
    return {
        trigger: "milestone-edit-batch",
        fillPct: snapshot?.fillPct,
        qualityScore: snapshot?.qualityScore,
    };
}
function recordCheckpointDecision(sessionId, trigger, nowMs = Date.now()) {
    const state = getCheckpointState(sessionId);
    state.lastCheckpointAt = nowMs;
    if (trigger.startsWith("progressive-")) {
        const band = Number.parseInt(trigger.split("-")[1] ?? "", 10);
        if (!Number.isNaN(band))
            state.capturedFillBands.add(band);
        persistState(sessionId, state);
        return;
    }
    if (trigger.startsWith("quality-")) {
        const threshold = Number.parseInt(trigger.split("-")[1] ?? "", 10);
        if (!Number.isNaN(threshold))
            state.capturedQualityThresholds.add(threshold);
        persistState(sessionId, state);
        return;
    }
    if (trigger === "milestone-pre-fanout") {
        state.capturedMilestones.add("pre-fanout");
        persistState(sessionId, state);
        return;
    }
    if (trigger === "milestone-edit-batch") {
        state.editBatchMarkerWrites = state.editWriteCount;
        state.editBatchMarkerFiles = state.editedFiles.size;
    }
    persistState(sessionId, state);
}
function registerCheckpointCapture(sessionId, trigger, telemetry) {
    appendCheckpointEvent(sessionId, trigger, telemetry);
    recordCheckpointDecision(sessionId, trigger);
}
function appendCheckpointEvent(sessionId, trigger, telemetry) {
    if (!CHECKPOINT_TELEMETRY_ENABLED)
        return;
    try {
        const eventPath = safeCheckpointRootFilePathForWrite(EVENTS_FILENAME);
        const event = {
            timestamp: new Date().toISOString(),
            platform: "openclaw",
            sessionId: sanitizeSessionId(sessionId),
            trigger,
            fillPct: telemetry.fillPct,
            qualityScore: telemetry.qualityScore,
            toolName: telemetry.toolName,
            eventKind: telemetry.eventKind,
            model: telemetry.model,
        };
        const fd = fs.openSync(eventPath, "a", 0o600);
        try {
            fs.writeSync(fd, JSON.stringify(event) + "\n");
        }
        finally {
            fs.closeSync(fd);
        }
    }
    catch {
        // Telemetry is best-effort only.
    }
}
function getCheckpointTelemetrySummary(days = 7) {
    const root = resolvedCheckpointRoot();
    const eventLog = root ? resolveSafeExistingCheckpointFile(path.join(root, EVENTS_FILENAME), root) : null;
    const cutoff = Date.now() - days * 86400 * 1000;
    const events = [];
    if (eventLog) {
        try {
            const lines = fs.readFileSync(eventLog, "utf-8").split("\n").filter(Boolean);
            for (const line of lines) {
                try {
                    const event = JSON.parse(line);
                    const ts = typeof event.timestamp === "string" ? Date.parse(event.timestamp) : NaN;
                    if (Number.isNaN(ts))
                        continue;
                    events.push({ ...event, _ts: ts });
                }
                catch {
                    continue;
                }
            }
        }
        catch {
            // ignore
        }
    }
    const recent = events.filter((event) => (event._ts ?? 0) >= cutoff);
    const byTrigger = {};
    for (const event of recent) {
        const trigger = typeof event.trigger === "string" ? event.trigger : "unknown";
        byTrigger[trigger] = (byTrigger[trigger] ?? 0) + 1;
    }
    recent.sort((a, b) => (b._ts ?? 0) - (a._ts ?? 0));
    const last = recent[0];
    return {
        enabled: CHECKPOINT_TELEMETRY_ENABLED,
        eventLog: root ? path.join(root, EVENTS_FILENAME) : path.join(CHECKPOINT_ROOT, EVENTS_FILENAME),
        days,
        totalEvents: events.length,
        recentEvents: recent.length,
        byTrigger: Object.fromEntries(Object.entries(byTrigger).sort(([a], [b]) => a.localeCompare(b))),
        lastEvent: last
            ? {
                timestamp: typeof last.timestamp === "string" ? last.timestamp : undefined,
                sessionId: typeof last.sessionId === "string" ? last.sessionId : undefined,
                trigger: typeof last.trigger === "string" ? last.trigger : undefined,
                fillPct: typeof last.fillPct === "number" ? last.fillPct : undefined,
                qualityScore: typeof last.qualityScore === "number" ? last.qualityScore : undefined,
            }
            : null,
    };
}
function getCheckpointFiles(sessionId) {
    const sessionDir = resolveSafeExistingCheckpointDir(checkpointSessionDir(sessionId));
    if (!sessionDir)
        return [];
    const manifestPath = resolveSafeExistingCheckpointFile(path.join(sessionDir, "manifest.jsonl"), sessionDir);
    if (!manifestPath)
        return [];
    const entries = [];
    try {
        const lines = fs.readFileSync(manifestPath, "utf-8").split("\n").filter(Boolean);
        for (const line of lines) {
            try {
                const entry = JSON.parse(line);
                if (!entry.file || !entry.trigger || !entry.createdAt)
                    continue;
                const createdAt = Date.parse(entry.createdAt);
                if (Number.isNaN(createdAt))
                    continue;
                const safeFile = resolveSafeExistingCheckpointFile(entry.file, sessionDir);
                if (!safeFile)
                    continue;
                entries.push({ path: safeFile, trigger: entry.trigger, createdAt });
            }
            catch {
                continue;
            }
        }
    }
    catch {
        return [];
    }
    return entries;
}
function cleanupPolicyArtifacts(maxAgeDays = 7) {
    const root = resolvedCheckpointRoot();
    if (!root)
        return 0;
    const cutoff = Date.now() - maxAgeDays * 86400 * 1000;
    let cleaned = 0;
    for (const dir of fs.readdirSync(root)) {
        const fullDir = path.join(root, dir);
        const safeDir = resolveSafeExistingCheckpointDir(fullDir);
        if (!safeDir)
            continue;
        for (const file of fs.readdirSync(safeDir)) {
            const filePath = path.join(safeDir, file);
            const safeFile = resolveSafeExistingCheckpointFile(filePath, safeDir);
            if (!safeFile)
                continue;
            try {
                const fileStat = fs.statSync(safeFile);
                if (fileStat.mtimeMs < cutoff) {
                    fs.rmSync(safeFile, { force: true });
                    cleaned += 1;
                }
            }
            catch {
                continue;
            }
        }
        try {
            if (fs.readdirSync(safeDir).length === 0)
                fs.rmdirSync(safeDir);
        }
        catch {
            // ignore
        }
    }
    return cleaned;
}
function getCheckpointHealth() {
    const issues = [];
    let sessionCount = 0;
    let checkpointCount = 0;
    let policyCount = 0;
    let checkpointBytes = 0;
    const telemetry = getCheckpointTelemetrySummary(7);
    if (!HOME)
        issues.push("Home directory is not set.");
    const root = resolvedCheckpointRoot();
    if (!fs.existsSync(CHECKPOINT_ROOT)) {
        issues.push("Checkpoint root does not exist yet.");
    }
    else if (!root) {
        issues.push("Checkpoint root is invalid or symlinked.");
    }
    else {
        for (const dir of fs.readdirSync(root)) {
            const fullDir = path.join(root, dir);
            const safeDir = resolveSafeExistingCheckpointDir(fullDir);
            if (!safeDir) {
                issues.push(`Skipped unsafe checkpoint directory: ${fullDir}`);
                continue;
            }
            sessionCount += 1;
            for (const file of fs.readdirSync(safeDir)) {
                const filePath = path.join(safeDir, file);
                const safeFile = resolveSafeExistingCheckpointFile(filePath, safeDir);
                if (!safeFile) {
                    issues.push(`Skipped unsafe checkpoint file: ${filePath}`);
                    continue;
                }
                try {
                    const fileStat = fs.statSync(safeFile);
                    checkpointBytes += fileStat.size;
                }
                catch {
                    continue;
                }
                if (file.endsWith(".md"))
                    checkpointCount += 1;
                if (file === STATE_FILENAME)
                    policyCount += 1;
            }
        }
    }
    return {
        checkpointRoot: CHECKPOINT_ROOT,
        sessionCount,
        checkpointCount,
        policyCount,
        pendingCount: 0,
        checkpointBytes,
        recentEventCount: telemetry.recentEvents,
        lastTrigger: telemetry.lastEvent?.trigger,
        issues,
    };
}
//# sourceMappingURL=checkpoint-policy.js.map