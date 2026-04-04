"use strict";
/**
 * Token Optimizer - Read Cache for OpenClaw.
 *
 * Intercepts Read tool calls via agent:tool:before events to detect redundant reads.
 * Default ON (warn mode). Opt out via TOKEN_OPTIMIZER_READ_CACHE=0 env var
 * or config.json {"read_cache_enabled": false}.
 *
 * Modes:
 *   warn  (default) - logs redundant read, does NOT block
 *   block           - returns digest instead of re-reading
 *
 * Security:
 *   - Path canonicalization via path.resolve()
 *   - 0o600 permissions on cache files
 *   - mtime re-verification on every cache hit
 *   - Binary file skip
 *   - .contextignore support (hard block)
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
exports.handleReadBefore = handleReadBefore;
exports.handleWriteAfter = handleWriteAfter;
exports.clearCache = clearCache;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const os = __importStar(require("os"));
// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const HOME = os.homedir();
const CACHE_DIR = path.join(HOME, ".openclaw", "token-optimizer", "read-cache");
const MAX_CACHE_ENTRIES = 500;
const MAX_CONTEXTIGNORE_PATTERNS = 200;
const BINARY_EXTENSIONS = new Set([
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".wasm", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".jar",
    ".sqlite", ".db", ".sqlite3",
]);
// ---------------------------------------------------------------------------
// .contextignore
// ---------------------------------------------------------------------------
let _contextignorePatterns = null;
function loadContextignorePatterns() {
    if (_contextignorePatterns !== null)
        return _contextignorePatterns;
    const patterns = [];
    // Project-level .contextignore
    const projectIgnore = path.resolve(".contextignore");
    if (fs.existsSync(projectIgnore)) {
        try {
            const lines = fs.readFileSync(projectIgnore, "utf-8").split("\n");
            for (const line of lines) {
                const trimmed = line.trim();
                if (trimmed && !trimmed.startsWith("#"))
                    patterns.push(trimmed);
            }
        }
        catch { /* ignore */ }
    }
    // Global .contextignore
    const globalIgnore = path.join(HOME, ".openclaw", ".contextignore");
    if (fs.existsSync(globalIgnore)) {
        try {
            const lines = fs.readFileSync(globalIgnore, "utf-8").split("\n");
            for (const line of lines) {
                const trimmed = line.trim();
                if (trimmed && !trimmed.startsWith("#"))
                    patterns.push(trimmed);
            }
        }
        catch { /* ignore */ }
    }
    _contextignorePatterns = patterns.slice(0, MAX_CONTEXTIGNORE_PATTERNS);
    return _contextignorePatterns;
}
/**
 * Simple glob match using minimatch-style logic (fnmatch equivalent).
 * Supports * and ** patterns. Pre-compiled regex cache avoids ~1,200
 * regex compilations per session.
 */
const _fnmatchCache = new Map();
function fnmatch(filepath, pattern) {
    let re = _fnmatchCache.get(pattern);
    if (!re) {
        const regex = pattern
            .replace(/[.+^${}()|[\]\\]/g, "\\$&")
            .replace(/\*\*/g, "{{GLOBSTAR}}")
            .replace(/\*/g, "[^/]*")
            .replace(/\?/g, "[^/]")
            .replace(/\{\{GLOBSTAR\}\}/g, ".*");
        re = new RegExp(`^${regex}$`);
        _fnmatchCache.set(pattern, re);
    }
    return re.test(filepath);
}
function isContextignored(filePath) {
    const patterns = loadContextignorePatterns();
    if (patterns.length === 0)
        return false;
    const basename = path.basename(filePath);
    for (const pattern of patterns) {
        if (fnmatch(filePath, pattern) || fnmatch(basename, pattern))
            return true;
    }
    return false;
}
// ---------------------------------------------------------------------------
// Structural digests
// ---------------------------------------------------------------------------
function digestPython(content) {
    const lines = content.split("\n");
    const parts = [];
    for (let i = 0; i < lines.length && parts.length < 50; i++) {
        const stripped = lines[i].trim();
        if (stripped.startsWith("class ")) {
            parts.push(`L${i + 1}: ${stripped.split("(")[0].split(":")[0]}`);
        }
        else if (stripped.startsWith("def ")) {
            parts.push(`L${i + 1}: ${stripped.split("(")[0]}`);
        }
        else if (stripped.startsWith("import ") || stripped.startsWith("from ")) {
            parts.push(`L${i + 1}: ${stripped}`);
        }
    }
    return parts.length > 0 ? parts.join("\n") : `${lines.length} lines`;
}
function digestJavaScript(content) {
    const lines = content.split("\n");
    const parts = [];
    for (let i = 0; i < lines.length && parts.length < 50; i++) {
        const stripped = lines[i].trim();
        if (/^(export\s+)?(class|interface|type|enum)\s+/.test(stripped)) {
            parts.push(`L${i + 1}: ${stripped.split("{")[0].trim()}`);
        }
        else if (/^(export\s+)?(async\s+)?function\s+/.test(stripped)) {
            parts.push(`L${i + 1}: ${stripped.split("{")[0].trim()}`);
        }
        else if (/^export\s+(default\s+)?(const|let|var)\s+/.test(stripped)) {
            parts.push(`L${i + 1}: ${stripped.split("=")[0].trim()}`);
        }
    }
    return parts.length > 0 ? parts.join("\n") : `${lines.length} lines`;
}
function digestFallback(content) {
    const lines = content.split("\n");
    const n = lines.length;
    if (n <= 6)
        return `${n} lines`;
    const first = lines.slice(0, 3).join("\n");
    const last = lines.slice(-3).join("\n");
    return `${n} lines\nFirst 3:\n${first}\nLast 3:\n${last}`;
}
function generateDigest(filePath, content) {
    const lines = content.split("\n");
    if (lines.length > 10000)
        return `${lines.length} lines (too large for structural digest)`;
    const ext = path.extname(filePath).toLowerCase();
    if (ext === ".py")
        return digestPython(content);
    if ([".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"].includes(ext))
        return digestJavaScript(content);
    return digestFallback(content);
}
function cachePath(agentId, sessionId) {
    const safeAgent = agentId.replace(/[^a-zA-Z0-9_-]/g, "") || "default";
    const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
    return path.join(CACHE_DIR, `${safeAgent}-${safeSession}.json`);
}
function loadCache(agentId, sessionId) {
    const cp = cachePath(agentId, sessionId);
    if (!fs.existsSync(cp))
        return { files: {} };
    try {
        const data = JSON.parse(fs.readFileSync(cp, "utf-8"));
        if (!data || !data.files)
            throw new Error("invalid");
        return data;
    }
    catch {
        try {
            fs.unlinkSync(cp);
        }
        catch { /* ignore */ }
        return { files: {} };
    }
}
function saveCache(agentId, sessionId, cache) {
    const files = cache.files;
    const keys = Object.keys(files);
    if (keys.length > MAX_CACHE_ENTRIES) {
        const sorted = keys.sort((a, b) => (files[a].lastAccess ?? 0) - (files[b].lastAccess ?? 0));
        const toRemove = keys.length - MAX_CACHE_ENTRIES;
        for (let i = 0; i < toRemove; i++)
            delete files[sorted[i]];
    }
    const cp = cachePath(agentId, sessionId);
    const dir = path.dirname(cp);
    if (!fs.existsSync(dir))
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const tmp = cp + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(cache), { mode: 0o600 });
    fs.renameSync(tmp, cp);
}
function logDecision(decision, filePath, reason, sessionId) {
    const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
    const dir = path.join(CACHE_DIR, "decisions");
    if (!fs.existsSync(dir))
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const logPath = path.join(dir, `${safeSession}.jsonl`);
    const entry = JSON.stringify({ ts: Date.now() / 1000, decision, file: filePath, reason, session: sessionId });
    try {
        fs.appendFileSync(logPath, entry + "\n", { mode: 0o600 });
    }
    catch { /* ignore */ }
}
/**
 * Handle agent:tool:before for Read events.
 * Returns { block: true, message: string } to block, or null to allow.
 */
function isReadCacheDisabled() {
    const envVal = process.env.TOKEN_OPTIMIZER_READ_CACHE;
    if (envVal === "0")
        return true;
    if (envVal === undefined) {
        // Env var missing (possibly stripped). Check config file.
        const configPath = path.join(HOME, ".openclaw", "token-optimizer", "read-cache", "config.json");
        try {
            if (fs.existsSync(configPath)) {
                const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
                if (config.read_cache_enabled === false)
                    return true;
            }
        }
        catch { /* ignore */ }
    }
    return false;
}
function handleReadBefore(event) {
    if (isReadCacheDisabled())
        return null;
    const mode = (process.env.TOKEN_OPTIMIZER_READ_CACHE_MODE ?? "warn").toLowerCase();
    const rawPath = event.toolInput.file_path ?? "";
    if (!rawPath)
        return null;
    const filePath = path.resolve(rawPath);
    const { agentId, sessionId } = event;
    // .contextignore check (hard block)
    if (isContextignored(filePath)) {
        logDecision("block", filePath, "contextignore", sessionId);
        return {
            block: true,
            message: `[Token Optimizer] File blocked by .contextignore: ${path.basename(filePath)}\nRemove the pattern from .contextignore if you need access.`,
        };
    }
    // Skip binary files
    if (BINARY_EXTENSIONS.has(path.extname(filePath).toLowerCase()))
        return null;
    const cache = loadCache(agentId, sessionId);
    const entry = cache.files[filePath];
    const offset = event.toolInput.offset ?? 0;
    const limit = event.toolInput.limit ?? 0;
    if (!entry) {
        // First read: cache it
        let mtime = 0;
        let tokensEst = 0;
        try {
            const stat = fs.statSync(filePath);
            mtime = stat.mtimeMs / 1000;
            tokensEst = Math.max(1, Math.floor(stat.size / 4));
        }
        catch {
            return null;
        }
        cache.files[filePath] = { mtime, offset, limit, tokensEst, readCount: 1, lastAccess: Date.now() / 1000, digest: "" };
        saveCache(agentId, sessionId, cache);
        logDecision("allow", filePath, "first_read", sessionId);
        return null;
    }
    // Check staleness: mtime + range
    let currentMtime = 0;
    try {
        currentMtime = fs.statSync(filePath).mtimeMs / 1000;
    }
    catch {
        delete cache.files[filePath];
        saveCache(agentId, sessionId, cache);
        logDecision("allow", filePath, "file_changed_or_deleted", sessionId);
        return null;
    }
    const mtimeMatch = Math.abs(currentMtime - entry.mtime) < 0.001;
    const rangeMatch = entry.offset === offset && entry.limit === limit;
    if (!(mtimeMatch && rangeMatch)) {
        entry.mtime = currentMtime;
        entry.offset = offset;
        entry.limit = limit;
        entry.readCount++;
        entry.lastAccess = Date.now() / 1000;
        entry.digest = "";
        saveCache(agentId, sessionId, cache);
        logDecision("allow", filePath, "file_modified_or_different_range", sessionId);
        return null;
    }
    // Redundant read
    entry.readCount++;
    entry.lastAccess = Date.now() / 1000;
    if (!entry.digest) {
        try {
            const content = fs.readFileSync(filePath, "utf-8");
            entry.digest = generateDigest(filePath, content);
        }
        catch {
            entry.digest = "(unable to generate digest)";
        }
    }
    saveCache(agentId, sessionId, cache);
    if (mode === "block") {
        logDecision("block", filePath, `redundant_read_${entry.readCount}`, sessionId);
        return {
            block: true,
            message: `[Token Optimizer] File already in context (read #${entry.readCount}, unchanged).\nStructural digest of ${path.basename(filePath)}:\n${entry.digest}\n\nTo re-read, edit the file first or use a different offset/limit.`,
        };
    }
    logDecision("warn", filePath, `redundant_read_${entry.readCount}`, sessionId);
    return null;
}
/**
 * Handle agent:tool:after for Edit/Write events (cache invalidation).
 */
function handleWriteAfter(event) {
    if (!["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(event.toolName))
        return;
    const rawPath = event.toolInput.file_path ?? "";
    if (!rawPath)
        return;
    const filePath = path.resolve(rawPath);
    const cache = loadCache(event.agentId, event.sessionId);
    if (cache.files[filePath]) {
        delete cache.files[filePath];
        saveCache(event.agentId, event.sessionId, cache);
    }
}
/**
 * Clear all caches (called on compact).
 */
function clearCache(agentId, sessionId) {
    const cp = cachePath(agentId, sessionId);
    try {
        fs.unlinkSync(cp);
    }
    catch { /* ignore */ }
    // Also remove per-session decisions file
    const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
    const dp = path.join(CACHE_DIR, "decisions", `${safeSession}.jsonl`);
    try {
        fs.unlinkSync(dp);
    }
    catch { /* ignore */ }
}
//# sourceMappingURL=read-cache.js.map