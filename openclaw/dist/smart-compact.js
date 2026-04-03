"use strict";
/**
 * Smart Compaction v2: intelligent extraction + last N messages fallback.
 *
 * v1: capture last N messages as markdown.
 * v2: extract decisions, errors, file modifications, and user instructions
 *     to preserve the most relevant context in fewer tokens.
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
exports.loadMessagesFromSessionFile = loadMessagesFromSessionFile;
exports.captureCheckpoint = captureCheckpoint;
exports.restoreCheckpoint = restoreCheckpoint;
exports.captureCheckpointV2 = captureCheckpointV2;
exports.cleanupCheckpoints = cleanupCheckpoints;
const crypto = __importStar(require("crypto"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const checkpoint_policy_1 = require("./checkpoint-policy");
const DEFAULT_RECENT_MESSAGES = 10;
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
function checkpointRootDir() {
    return path.dirname((0, checkpoint_policy_1.checkpointSessionDir)("__root_probe__"));
}
function ensureSafeCheckpointRootForWrites() {
    const root = checkpointRootDir();
    if (!path.isAbsolute(root)) {
        throw new Error("Checkpoint root is not absolute");
    }
    if (!fs.existsSync(root)) {
        fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    }
    const stat = fs.lstatSync(root);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
        throw new Error("Checkpoint root is unsafe");
    }
    return fs.realpathSync(root);
}
function safeSessionDir(sessionId) {
    const safe = sanitizeSessionId(sessionId);
    const dir = (0, checkpoint_policy_1.checkpointSessionDir)(safe);
    const root = ensureSafeCheckpointRootForWrites();
    if (fs.existsSync(dir)) {
        const stat = fs.lstatSync(dir);
        if (stat.isSymbolicLink() || !stat.isDirectory()) {
            throw new Error("Checkpoint session directory is unsafe");
        }
    }
    const resolved = fs.existsSync(dir)
        ? fs.realpathSync(dir)
        : path.join(root, safe);
    if (!isWithinDir(root, resolved)) {
        throw new Error("Path traversal detected");
    }
    return resolved;
}
function safeCheckpointPath(sessionId, filename) {
    const dir = safeSessionDir(sessionId);
    const filepath = path.join(dir, filename);
    if (fs.existsSync(filepath)) {
        const stat = fs.lstatSync(filepath);
        if (stat.isSymbolicLink() || !stat.isFile()) {
            throw new Error("Checkpoint file is unsafe");
        }
    }
    const resolved = path.resolve(filepath);
    if (!isWithinDir(path.resolve(dir), resolved)) {
        throw new Error("Path traversal detected");
    }
    return resolved;
}
function safeManifestPath(sessionId) {
    const dir = safeSessionDir(sessionId);
    const manifestPath = path.join(dir, "manifest.jsonl");
    if (fs.existsSync(manifestPath)) {
        const stat = fs.lstatSync(manifestPath);
        if (stat.isSymbolicLink() || !stat.isFile()) {
            throw new Error("Checkpoint manifest is unsafe");
        }
    }
    const resolved = path.resolve(manifestPath);
    if (!isWithinDir(path.resolve(dir), resolved)) {
        throw new Error("Checkpoint manifest escapes session directory");
    }
    return resolved;
}
function checkpointFilename(timestamp, trigger) {
    const cleanTrigger = trigger.replace(/[^a-zA-Z0-9_.-]/g, "_");
    return `${timestamp}-${cleanTrigger}.md`;
}
function stableFingerprint(parts) {
    const payload = parts
        .map((part) => (part === undefined || part === null ? "" : String(part)))
        .join("|");
    return crypto.createHash("sha256").update(payload).digest("hex");
}
function buildSemanticDigest(sessionId, messages, options) {
    const recent = (messages ?? []).slice(-DEFAULT_RECENT_MESSAGES);
    const contentDigest = stableFingerprint(recent.flatMap((msg) => [
        msg.role,
        msg.timestamp ?? "",
        msg.content.slice(0, 600),
    ]));
    return stableFingerprint([
        sanitizeSessionId(sessionId),
        options.trigger ?? "compact",
        options.reason ?? "",
        options.fillPct?.toFixed(3) ?? "",
        options.qualityScore?.toFixed(0) ?? "",
        options.toolName ?? "",
        options.eventKind ?? "",
        options.activeAgents ?? "",
        options.writeCount ?? "",
        options.writeBurstCount ?? "",
        options.contextWindow ?? "",
        options.model ?? "",
        contentDigest,
    ]);
}
function loadMessagesFromSessionFile(sessionFile) {
    try {
        const lines = fs.readFileSync(sessionFile, "utf-8").split("\n");
        const messages = [];
        for (const line of lines) {
            if (!line.trim())
                continue;
            try {
                const record = JSON.parse(line);
                const type = typeof record.type === "string" ? record.type : "";
                if (type !== "user" && type !== "assistant")
                    continue;
                const message = record.message;
                const rawContent = message?.content;
                let content = "";
                if (typeof rawContent === "string") {
                    content = rawContent;
                }
                else if (Array.isArray(rawContent)) {
                    content = rawContent
                        .map((block) => {
                        if (!block || typeof block !== "object")
                            return "";
                        const entry = block;
                        if (entry.type === "text" && typeof entry.text === "string")
                            return entry.text;
                        return "";
                    })
                        .filter(Boolean)
                        .join("\n");
                }
                messages.push({
                    role: type,
                    content,
                    timestamp: typeof record.timestamp === "string" ? record.timestamp : undefined,
                });
            }
            catch {
                continue;
            }
        }
        return messages.length > 0 ? messages : undefined;
    }
    catch {
        return undefined;
    }
}
function buildCheckpointHeader(sessionId, generatedAt, messages, options) {
    const messageCount = messages?.length ?? 0;
    const header = [
        "# Session Checkpoint",
        `> Captured at ${generatedAt}`,
        `> Session: ${sanitizeSessionId(sessionId)}`,
        `> Trigger: ${options.trigger ?? "compact"}`,
    ];
    if (options.reason)
        header.push(`> Reason: ${options.reason}`);
    if (typeof options.fillPct === "number")
        header.push(`> Fill: ${options.fillPct.toFixed(0)}%`);
    if (typeof options.qualityScore === "number")
        header.push(`> Quality: ${Math.round(options.qualityScore)}/100`);
    if (typeof options.activeAgents === "number")
        header.push(`> Active agents: ${options.activeAgents}`);
    if (typeof options.writeCount === "number")
        header.push(`> Writes seen: ${options.writeCount}`);
    if (typeof options.writeBurstCount === "number")
        header.push(`> Write burst: ${options.writeBurstCount}`);
    if (typeof options.contextWindow === "number")
        header.push(`> Context window: ${options.contextWindow}`);
    if (options.model)
        header.push(`> Model: ${options.model}`);
    header.push(`> Messages preserved: ${messageCount}`);
    header.push("");
    return header;
}
function buildCheckpointBody(sessionId, messages, maxMessages, options) {
    const generatedAt = new Date().toISOString();
    const recent = (messages ?? []).slice(-maxMessages);
    const lines = buildCheckpointHeader(sessionId, generatedAt, messages, options);
    if (recent.length === 0) {
        lines.push("## Checkpoint Summary");
        lines.push("");
        lines.push("No transcript messages were available at capture time.");
        lines.push("");
    }
    else {
        for (const msg of recent) {
            const role = msg.role === "user" ? "User" : "Assistant";
            const ts = msg.timestamp ? ` (${msg.timestamp})` : "";
            lines.push(`## ${role}${ts}`);
            lines.push("");
            const content = msg.content.length > 2000
                ? msg.content.slice(0, 2000) + "\n\n[...truncated]"
                : msg.content;
            lines.push(content);
            lines.push("");
        }
    }
    return {
        body: lines.join("\n"),
        semanticDigest: buildSemanticDigest(sessionId, messages, options),
    };
}
function appendCheckpointManifest(sessionId, entry) {
    const manifestPath = safeManifestPath(sessionId);
    fs.mkdirSync(path.dirname(manifestPath), { recursive: true, mode: 0o700 });
    const fd = fs.openSync(manifestPath, "a", 0o600);
    try {
        fs.writeSync(fd, JSON.stringify(entry) + "\n");
    }
    finally {
        fs.closeSync(fd);
    }
}
function triggerPriority(trigger) {
    if (!trigger)
        return 100;
    if (trigger === "milestone-pre-fanout")
        return 1000;
    if (trigger === "milestone-edit-batch")
        return 950;
    if (trigger.startsWith("quality-")) {
        const threshold = Number.parseInt(trigger.split("-")[1] ?? "", 10);
        return Number.isNaN(threshold) ? 900 : 900 - threshold;
    }
    if (trigger.startsWith("progressive-")) {
        const band = Number.parseInt(trigger.split("-")[1] ?? "", 10);
        return Number.isNaN(band) ? 700 : 800 - band;
    }
    switch (trigger) {
        case "compact":
            return 400;
        case "session-end":
        case "end":
            return 300;
        case "session-start":
            return 250;
        case "stop":
            return 200;
        case "stop-failure":
            return 175;
        default:
            return 100;
    }
}
function readLastManifestEntry(sessionId) {
    let manifestPath;
    try {
        manifestPath = safeManifestPath(sessionId);
    }
    catch {
        return null;
    }
    if (!fs.existsSync(manifestPath))
        return null;
    try {
        const lines = fs.readFileSync(manifestPath, "utf-8").split("\n").filter(Boolean);
        const last = lines[lines.length - 1];
        if (!last)
            return null;
        return JSON.parse(last);
    }
    catch {
        return null;
    }
}
function writeCheckpointArtifact(session, maxMessages, options) {
    const sessionId = sanitizeSessionId(session.sessionId);
    const messages = session.messages;
    const trigger = options.trigger ?? "compact";
    fs.mkdirSync(safeSessionDir(sessionId), { recursive: true, mode: 0o700 });
    const { body, semanticDigest } = buildCheckpointBody(sessionId, messages, maxMessages, {
        ...options,
        trigger,
    });
    const lastEntry = readLastManifestEntry(sessionId);
    if (lastEntry &&
        lastEntry.semanticDigest === semanticDigest &&
        lastEntry.trigger === trigger) {
        return typeof lastEntry.file === "string" ? lastEntry.file : null;
    }
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = checkpointFilename(timestamp, trigger);
    const filepath = safeCheckpointPath(sessionId, filename);
    try {
        fs.writeFileSync(filepath, body, { encoding: "utf-8", mode: 0o600 });
    }
    catch {
        return null;
    }
    appendCheckpointManifest(sessionId, {
        file: filepath,
        filename,
        trigger,
        reason: options.reason ?? null,
        semanticDigest,
        fillPct: typeof options.fillPct === "number" ? options.fillPct : null,
        qualityScore: typeof options.qualityScore === "number" ? options.qualityScore : null,
        messageCount: messages?.length ?? 0,
        createdAt: new Date().toISOString(),
    });
    (0, checkpoint_policy_1.registerCheckpointCapture)(sessionId, trigger, {
        sessionId,
        messages,
        fillPct: options.fillPct,
        qualityScore: options.qualityScore,
        toolName: options.toolName,
        eventKind: options.eventKind,
        activeAgents: options.activeAgents,
        writeCount: options.writeCount,
        writeBurstCount: options.writeBurstCount,
        contextWindow: options.contextWindow,
        model: options.model,
    });
    return filepath;
}
function captureCheckpoint(session, maxMessages = 20, options = {}) {
    return writeCheckpointArtifact(session, maxMessages, options);
}
function restoreCheckpoint(sessionId) {
    try {
        const entries = (0, checkpoint_policy_1.getCheckpointFiles)(sessionId);
        if (entries.length === 0)
            return null;
        const ranked = entries
            .map((entry) => ({
            ...entry,
            priority: triggerPriority(entry.trigger),
        }))
            .sort((a, b) => {
            if (a.priority !== b.priority)
                return b.priority - a.priority;
            return b.createdAt - a.createdAt;
        });
        for (const entry of ranked) {
            try {
                return fs.readFileSync(entry.path, "utf-8");
            }
            catch {
                continue;
            }
        }
        return null;
    }
    catch {
        return null;
    }
}
const DECISION_PATTERNS = [
    /\bI'll\b/i, /\bLet's\b/i, /\bdecided\b/i, /\bchoosing\b/i,
    /\bgoing with\b/i, /\busing\b/i, /\bswitching to\b/i,
];
const ERROR_PATTERNS = [
    /\bError[:!]/i, /\bfailed\b/i, /\bexception\b/i, /\bstack trace\b/i,
    /\btraceback\b/i, /\bTypeError\b/, /\bSyntaxError\b/, /\bReferenceError\b/,
    /\bENOENT\b/, /\bEACCES\b/, /\bconnection refused\b/i,
];
const FILE_CHANGE_PATTERNS = [
    /\bwrit(?:e|ing|ten)\b/i, /\bedit(?:ed|ing)?\b/i, /\bcreated?\b/i,
    /\bmodif(?:y|ied|ying)\b/i, /\btool_use\b/,
];
const INSTRUCTION_PATTERNS = [
    /\balways\b/i, /\bnever\b/i, /\bmake sure\b/i, /\bdon't\b/i,
    /\bdo not\b/i, /\bmust\b/i, /\bshould\b/i, /\bprefer\b/i,
];
function matchesAny(text, patterns) {
    return patterns.some((p) => p.test(text));
}
function extractIntelligent(messages) {
    const ctx = {
        decisions: [],
        errors: [],
        fileChanges: [],
        userInstructions: [],
    };
    for (const msg of messages) {
        const content = msg.content;
        if (!content)
            continue;
        // Truncate very long messages for pattern matching
        const sample = content.slice(0, 3000);
        if (msg.role === "assistant") {
            if (matchesAny(sample, DECISION_PATTERNS)) {
                // Extract the decision sentence (first matching line)
                const lines = sample.split("\n");
                for (const line of lines) {
                    if (matchesAny(line, DECISION_PATTERNS) && line.length > 10 && line.length < 500) {
                        ctx.decisions.push(line.trim());
                        break;
                    }
                }
            }
            if (matchesAny(sample, ERROR_PATTERNS)) {
                const lines = sample.split("\n");
                const errorLines = [];
                for (const line of lines) {
                    if (matchesAny(line, ERROR_PATTERNS) && line.length < 300) {
                        errorLines.push(line.trim());
                        if (errorLines.length >= 3)
                            break;
                    }
                }
                if (errorLines.length > 0) {
                    ctx.errors.push(errorLines.join("\n"));
                }
            }
            if (matchesAny(sample, FILE_CHANGE_PATTERNS)) {
                const lines = sample.split("\n");
                for (const line of lines) {
                    if (matchesAny(line, FILE_CHANGE_PATTERNS) && line.length > 10 && line.length < 300) {
                        ctx.fileChanges.push(line.trim());
                        break;
                    }
                }
            }
        }
        if (msg.role === "user" && matchesAny(sample, INSTRUCTION_PATTERNS)) {
            const lines = sample.split("\n");
            for (const line of lines) {
                if (matchesAny(line, INSTRUCTION_PATTERNS) && line.length > 10 && line.length < 500) {
                    ctx.userInstructions.push(line.trim());
                    break;
                }
            }
        }
    }
    // Deduplicate
    ctx.decisions = [...new Set(ctx.decisions)].slice(0, 10);
    ctx.errors = [...new Set(ctx.errors)].slice(0, 5);
    ctx.fileChanges = [...new Set(ctx.fileChanges)].slice(0, 10);
    ctx.userInstructions = [...new Set(ctx.userInstructions)].slice(0, 10);
    return ctx;
}
/**
 * v2 checkpoint: intelligent extraction + recent messages fallback.
 * Produces a more focused checkpoint than v1's raw last-N dump.
 */
function captureCheckpointV2(session, maxRecentMessages = 10, options = {}) {
    const messages = session.messages;
    if (!messages || messages.length === 0) {
        return writeCheckpointArtifact(session, maxRecentMessages, options);
    }
    try {
        fs.mkdirSync(safeSessionDir(session.sessionId), { recursive: true, mode: 0o700 });
    }
    catch {
        return null;
    }
    const extracted = extractIntelligent(messages);
    const recent = messages.slice(-maxRecentMessages);
    const bodyOptions = {
        ...options,
        trigger: options.trigger ?? "compact",
    };
    const lines = buildCheckpointHeader(sanitizeSessionId(session.sessionId), new Date().toISOString(), messages, bodyOptions);
    lines[0] = "# Session Checkpoint (v2)";
    if (extracted.userInstructions.length > 0) {
        lines.push("## User Instructions");
        lines.push("");
        for (const inst of extracted.userInstructions) {
            lines.push(`- ${inst}`);
        }
        lines.push("");
    }
    if (extracted.decisions.length > 0) {
        lines.push("## Key Decisions");
        lines.push("");
        for (const dec of extracted.decisions) {
            lines.push(`- ${dec}`);
        }
        lines.push("");
    }
    if (extracted.errors.length > 0) {
        lines.push("## Errors Encountered");
        lines.push("");
        for (const err of extracted.errors) {
            lines.push("```");
            lines.push(err);
            lines.push("```");
            lines.push("");
        }
    }
    if (extracted.fileChanges.length > 0) {
        lines.push("## File Changes");
        lines.push("");
        for (const fc of extracted.fileChanges) {
            lines.push(`- ${fc}`);
        }
        lines.push("");
    }
    lines.push("## Recent Messages");
    lines.push("");
    for (const msg of recent) {
        const role = msg.role === "user" ? "User" : "Assistant";
        const ts = msg.timestamp ? ` (${msg.timestamp})` : "";
        lines.push(`### ${role}${ts}`);
        lines.push("");
        const content = msg.content.length > 1500
            ? msg.content.slice(0, 1500) + "\n\n[...truncated]"
            : msg.content;
        lines.push(content);
        lines.push("");
    }
    const digest = buildSemanticDigest(session.sessionId, messages, bodyOptions);
    const lastEntry = readLastManifestEntry(sanitizeSessionId(session.sessionId));
    if (lastEntry && lastEntry.semanticDigest === digest && lastEntry.trigger === bodyOptions.trigger) {
        return typeof lastEntry.file === "string" ? lastEntry.file : null;
    }
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = checkpointFilename(timestamp, bodyOptions.trigger ?? "compact");
    const filepath = safeCheckpointPath(session.sessionId, filename);
    try {
        fs.writeFileSync(filepath, lines.join("\n"), { encoding: "utf-8", mode: 0o600 });
    }
    catch {
        return null;
    }
    appendCheckpointManifest(sanitizeSessionId(session.sessionId), {
        file: filepath,
        filename,
        trigger: bodyOptions.trigger ?? "compact",
        reason: bodyOptions.reason ?? null,
        semanticDigest: digest,
        fillPct: typeof bodyOptions.fillPct === "number" ? bodyOptions.fillPct : null,
        qualityScore: typeof bodyOptions.qualityScore === "number" ? bodyOptions.qualityScore : null,
        messageCount: messages.length,
        createdAt: new Date().toISOString(),
    });
    (0, checkpoint_policy_1.registerCheckpointCapture)(sanitizeSessionId(session.sessionId), (bodyOptions.trigger ?? "compact"), {
        sessionId: sanitizeSessionId(session.sessionId),
        messages,
        fillPct: bodyOptions.fillPct,
        qualityScore: bodyOptions.qualityScore,
        toolName: bodyOptions.toolName,
        eventKind: bodyOptions.eventKind,
        activeAgents: bodyOptions.activeAgents,
        writeCount: bodyOptions.writeCount,
        writeBurstCount: bodyOptions.writeBurstCount,
        contextWindow: bodyOptions.contextWindow,
        model: bodyOptions.model,
    });
    return filepath;
}
function cleanupCheckpoints(maxAgeDays = 7) {
    return (0, checkpoint_policy_1.cleanupPolicyArtifacts)(maxAgeDays);
}
//# sourceMappingURL=smart-compact.js.map