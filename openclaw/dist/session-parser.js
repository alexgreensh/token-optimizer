"use strict";
/**
 * OpenClaw session JSONL parser.
 *
 * Reads ~/.openclaw/agents/{agentId}/sessions/{sessionId}.jsonl
 * and normalizes into AgentRun objects.
 *
 * OpenClaw JSONL differences from Claude Code:
 * - Token fields: inputTokens, outputTokens, totalTokens (no cache breakdown)
 * - Agent-scoped: sessions live under agent directories
 * - No subagent nesting (agents are top-level)
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
exports.findOpenClawDir = findOpenClawDir;
exports.listAgents = listAgents;
exports.findSessionFiles = findSessionFiles;
exports.extractTopic = extractTopic;
exports.parseSession = parseSession;
exports.scanAllSessions = scanAllSessions;
exports.parseSessionTurns = parseSessionTurns;
exports.extractCostlyPrompts = extractCostlyPrompts;
exports.classifyCronRuns = classifyCronRuns;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const pricing_1 = require("./pricing");
const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const OPENCLAW_DIRS = [
    path.join(HOME, ".openclaw"),
    path.join(HOME, ".clawdbot"),
    path.join(HOME, ".moltbot"),
];
/**
 * Find the first existing OpenClaw data directory.
 */
function findOpenClawDir() {
    for (const dir of OPENCLAW_DIRS) {
        if (fs.existsSync(dir))
            return dir;
    }
    return null;
}
/**
 * Discover all agent directories under the OpenClaw data root.
 */
function listAgents(openclawDir) {
    const agentsDir = path.join(openclawDir, "agents");
    if (!fs.existsSync(agentsDir))
        return [];
    try {
        return fs
            .readdirSync(agentsDir, { withFileTypes: true })
            .filter((d) => d.isDirectory())
            .map((d) => d.name);
    }
    catch {
        return [];
    }
}
/**
 * Find all session JSONL files for a given agent, optionally filtered by age.
 *
 * Returns array of { filePath, agentName, sessionId, mtime } sorted newest-first.
 */
function findSessionFiles(openclawDir, agentName, days = 30) {
    const sessionsDir = path.join(openclawDir, "agents", agentName, "sessions");
    if (!fs.existsSync(sessionsDir))
        return [];
    const cutoff = Date.now() - days * 86400 * 1000;
    const results = [];
    try {
        const files = fs.readdirSync(sessionsDir);
        for (const file of files) {
            if (!file.endsWith(".jsonl"))
                continue;
            const filePath = path.join(sessionsDir, file);
            try {
                const stat = fs.statSync(filePath);
                if (stat.mtimeMs >= cutoff) {
                    results.push({
                        filePath,
                        agentName,
                        sessionId: path.basename(file, ".jsonl"),
                        mtime: stat.mtimeMs,
                    });
                }
            }
            catch {
                continue;
            }
        }
    }
    catch {
        return [];
    }
    results.sort((a, b) => b.mtime - a.mtime);
    return results;
}
/**
 * Parse a line of JSON safely. Returns null on any error.
 */
function parseLine(line) {
    try {
        const trimmed = line.trim();
        if (!trimmed)
            return null;
        return JSON.parse(trimmed);
    }
    catch {
        return null;
    }
}
/** Common prefixes to strip before extracting a topic. */
const TOPIC_STRIP_PREFIXES = [
    "implement the following plan:",
    "implement the following:",
    "implement this plan:",
    "you are implementing unit",
    "you are implementing",
    "complete the following:",
    "please implement:",
    "task:",
    "goal:",
];
/**
 * Extract a short human-readable topic from raw user message text.
 *
 * Strategy (mirrors Python _extract_topic):
 * 1. Strip common boilerplate prefixes (case-insensitive).
 * 2. Return the first markdown heading (## or #) if present.
 * 3. Otherwise return the first non-empty sentence (up to the first period/newline).
 * 4. Truncate to 120 characters.
 */
function extractTopic(text) {
    let s = text.trim();
    if (!s)
        return "";
    // Strip boilerplate prefixes (longest match first via sort)
    const lower = s.toLowerCase();
    for (const prefix of TOPIC_STRIP_PREFIXES) {
        if (lower.startsWith(prefix)) {
            s = s.slice(prefix.length).trimStart();
            break;
        }
    }
    // Look for first markdown heading
    const headingMatch = s.match(/^#{1,2}\s+(.+)/m);
    if (headingMatch) {
        const heading = headingMatch[1].trim();
        return heading.length > 120 ? heading.slice(0, 120) : heading;
    }
    // Fall back to first sentence (split on period or newline)
    const sentenceMatch = s.match(/^([^.\n]+)/);
    const sentence = sentenceMatch ? sentenceMatch[1].trim() : s;
    return sentence.length > 120 ? sentence.slice(0, 120) : sentence;
}
/**
 * Extract the text content from a user JSONL record.
 * Handles both string content and content-block arrays.
 */
function extractUserMessageText(record) {
    const msg = record.message;
    const content = msg?.content ?? record.content;
    if (typeof content === "string")
        return content;
    if (Array.isArray(content)) {
        const parts = [];
        for (const block of content) {
            if (typeof block === "string") {
                parts.push(block);
            }
            else if (typeof block === "object" &&
                block !== null &&
                block.type === "text") {
                const txt = block.text;
                if (typeof txt === "string")
                    parts.push(txt);
            }
        }
        return parts.join(" ").trim();
    }
    return "";
}
/**
 * Parse a single OpenClaw session JSONL file into an AgentRun.
 *
 * OpenClaw JSONL format:
 * - Each line is a JSON object with at minimum a "type" field
 * - Token data in assistant messages under "usage" or top-level fields
 * - Model ID in "model" field of assistant messages
 */
function parseSession(filePath, agentName, openclawDir) {
    let content;
    try {
        const stat = fs.statSync(filePath);
        if (stat.size > 50 * 1024 * 1024)
            return null; // Skip files >50MB to prevent OOM
        content = fs.readFileSync(filePath, "utf-8");
    }
    catch {
        return null;
    }
    const lines = content.split("\n");
    let totalInput = 0;
    let totalOutput = 0;
    let totalCacheRead = 0;
    let totalCacheWrite = 0;
    let totalCacheWrite1h = 0;
    let totalCacheWrite5m = 0;
    let messageCount = 0;
    const modelUsage = new Map();
    const toolsUsed = new Set();
    let firstTs = null;
    let lastTs = null;
    let firstUserText = null;
    for (const line of lines) {
        const record = parseLine(line);
        if (!record)
            continue;
        // Timestamp
        const tsRaw = record.timestamp;
        if (tsRaw) {
            try {
                const ts = new Date(tsRaw);
                if (!isNaN(ts.getTime())) {
                    if (!firstTs)
                        firstTs = ts;
                    lastTs = ts;
                }
            }
            catch {
                // skip bad timestamps
            }
        }
        const recType = record.type;
        // Count messages
        if (recType === "user" || recType === "assistant") {
            messageCount++;
        }
        // Capture first user message text for topic extraction
        if (recType === "user" && firstUserText === null) {
            const txt = extractUserMessageText(record);
            if (txt)
                firstUserText = txt;
        }
        // Extract token data from assistant messages
        if (recType === "assistant") {
            const msg = record.message;
            const usage = msg?.usage ??
                record.usage;
            if (usage) {
                // Prefer explicit cache fields when present, but tolerate older logs
                // that only expose input/output totals.
                const inp = usage.inputTokens ??
                    usage.input_tokens ??
                    0;
                const out = usage.outputTokens ??
                    usage.output_tokens ??
                    0;
                const cacheRead = usage.cacheReadInputTokens ??
                    usage.cache_read_input_tokens ??
                    0;
                const cacheCreation = usage.cacheCreation ??
                    usage.cache_creation ??
                    {};
                const cacheWrite1h = cacheCreation.ephemeral_1h_input_tokens ??
                    usage.ephemeral_1h_input_tokens ??
                    0;
                const cacheWrite5m = cacheCreation.ephemeral_5m_input_tokens ??
                    usage.ephemeral_5m_input_tokens ??
                    0;
                const cacheWrite = usage.cacheCreationInputTokens ??
                    usage.cache_creation_input_tokens ??
                    (cacheWrite1h + cacheWrite5m);
                totalInput += inp;
                totalOutput += out;
                totalCacheRead += cacheRead;
                totalCacheWrite += cacheWrite;
                totalCacheWrite1h += cacheWrite1h;
                totalCacheWrite5m += cacheWrite5m;
                // Track model usage
                const modelId = msg?.model ?? record.model ?? "unknown";
                const current = modelUsage.get(modelId) ?? 0;
                modelUsage.set(modelId, current + inp + out + cacheRead + cacheWrite);
            }
            // Extract tool usage
            const msgContent = msg?.content;
            if (Array.isArray(msgContent)) {
                for (const block of msgContent) {
                    if (typeof block === "object" &&
                        block !== null &&
                        block.type === "tool_use") {
                        const name = block.name;
                        if (typeof name === "string")
                            toolsUsed.add(name);
                    }
                }
            }
        }
    }
    if (messageCount === 0)
        return null;
    // Duration
    let durationSeconds = 0;
    if (firstTs && lastTs) {
        durationSeconds = Math.max(0, (lastTs.getTime() - firstTs.getTime()) / 1000);
    }
    // Dominant model
    let dominantModelRaw = "unknown";
    let maxUsage = 0;
    for (const [model, usage] of modelUsage) {
        if (usage > maxUsage) {
            maxUsage = usage;
            dominantModelRaw = model;
        }
    }
    const model = (0, pricing_1.normalizeModelName)(dominantModelRaw) ?? dominantModelRaw;
    const tokens = {
        input: totalInput,
        output: totalOutput,
        cacheRead: totalCacheRead,
        cacheWrite: totalCacheWrite,
    };
    // Determine outcome
    let outcome = "success";
    if (messageCount <= 2 && totalOutput < 200) {
        outcome = "abandoned";
    }
    else if (totalOutput < 100 && totalInput > 50_000) {
        outcome = "empty";
    }
    const costUsd = (0, pricing_1.calculateCost)(tokens, model, openclawDir);
    const topic = firstUserText ? extractTopic(firstUserText) : undefined;
    return {
        system: "openclaw",
        sessionId: path.basename(filePath, ".jsonl"),
        agentName,
        project: agentName, // OpenClaw scopes by agent, not project
        timestamp: firstTs ?? new Date(),
        durationSeconds,
        tokens,
        costUsd,
        model,
        runType: "manual",
        outcome,
        messageCount,
        toolsUsed: Array.from(toolsUsed).sort(),
        sourcePath: filePath,
        cacheWrite1hTokens: totalCacheWrite1h,
        cacheWrite5mTokens: totalCacheWrite5m,
        ...(topic ? { topic } : {}),
    };
}
/**
 * Load session-level token aggregates from sessions.json.
 * OpenClaw stores authoritative totals here (inputTokens, outputTokens, contextTokens).
 * Returns a map of sessionId -> { inputTokens, outputTokens, contextTokens }.
 */
function loadSessionIndex(openclawDir, agentName) {
    const result = new Map();
    const indexPath = path.join(openclawDir, "agents", agentName, "sessions", "sessions.json");
    try {
        if (!fs.existsSync(indexPath))
            return result;
        const stat = fs.statSync(indexPath);
        if (stat.size > 10_000_000)
            return result; // Skip huge index files
        const data = JSON.parse(fs.readFileSync(indexPath, "utf-8"));
        // sessions.json can be an array or object of session entries
        const entries = Array.isArray(data) ? data : Object.values(data);
        for (const entry of entries) {
            if (typeof entry !== "object" || entry === null)
                continue;
            const e = entry;
            const id = e.id ?? e.sessionId;
            if (!id)
                continue;
            result.set(id, {
                inputTokens: Number(e.inputTokens) || 0,
                outputTokens: Number(e.outputTokens) || 0,
                contextTokens: Number(e.contextTokens) || 0,
            });
        }
    }
    catch {
        // sessions.json not available or malformed
    }
    return result;
}
/**
 * Scan all agents and sessions within the given day window.
 *
 * Returns all parsed AgentRuns sorted by timestamp (newest first).
 */
function scanAllSessions(openclawDir, days = 30) {
    const agents = listAgents(openclawDir);
    const allRuns = [];
    for (const agent of agents) {
        const sessionIndex = loadSessionIndex(openclawDir, agent);
        const files = findSessionFiles(openclawDir, agent, days);
        for (const { filePath, agentName, sessionId } of files) {
            const run = parseSession(filePath, agentName, openclawDir);
            if (!run)
                continue;
            // If JSONL parsing yielded zero tokens, fall back to sessions.json
            if (run.tokens.input === 0 && run.tokens.output === 0) {
                const indexed = sessionIndex.get(sessionId);
                if (indexed) {
                    run.tokens.input = indexed.inputTokens;
                    run.tokens.output = indexed.outputTokens;
                    run.costUsd = (0, pricing_1.calculateCost)(run.tokens, run.model, openclawDir);
                }
            }
            allRuns.push(run);
        }
    }
    allRuns.sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime());
    return allRuns;
}
/**
 * Parse a single OpenClaw session JSONL file into a per-turn breakdown.
 *
 * Each TurnData represents one user→assistant exchange. User messages are
 * paired with the immediately following assistant response. Turns without
 * an assistant response (e.g. trailing user messages) are included with
 * zero token counts.
 *
 * Multi-provider token field handling:
 * - Claude (Anthropic): cache_read_input_tokens / cache_creation_input_tokens
 * - GPT-5 / OpenAI: cached_tokens inside usage.prompt_tokens_details
 * - Gemini / others: no cache fields, input/output only
 *
 * Returns TurnData[] sorted by timestamp ascending.
 * Malformed JSONL lines are silently skipped.
 */
function parseSessionTurns(filePath, openclawDir) {
    let content;
    try {
        const stat = fs.statSync(filePath);
        if (stat.size > 50 * 1024 * 1024)
            return []; // Skip files >50MB to prevent OOM
        content = fs.readFileSync(filePath, "utf-8");
    }
    catch {
        return [];
    }
    const lines = content.split("\n");
    const records = [];
    for (const line of lines) {
        const record = parseLine(line);
        if (!record)
            continue;
        const recType = record.type;
        if (recType !== "user" && recType !== "assistant")
            continue;
        // Skip sidechain messages (internal tool orchestration, not real user prompts)
        // Must match the same filter in extractCostlyPrompts to keep turn indexes aligned.
        if (recType === "user" && record.isSidechain === true)
            continue;
        const tsRaw = record.timestamp;
        let timestamp = null;
        if (tsRaw) {
            try {
                const ts = new Date(tsRaw);
                if (!isNaN(ts.getTime()))
                    timestamp = ts.toISOString();
            }
            catch {
                // keep null
            }
        }
        records.push({ type: recType, timestamp, record });
    }
    const turns = [];
    let turnIndex = 0;
    for (let i = 0; i < records.length; i++) {
        const current = records[i];
        // Only start a turn on a user message
        if (current.type !== "user")
            continue;
        // Look ahead for the next assistant message
        const nextAssistant = records[i + 1]?.type === "assistant"
            ? records[i + 1]
            : null;
        if (!nextAssistant) {
            // Trailing user message with no response yet — emit minimal turn
            turns.push({
                turnIndex: turnIndex++,
                role: "user",
                inputTokens: 0,
                outputTokens: 0,
                cacheRead: 0,
                cacheCreation: 0,
                model: "unknown",
                timestamp: current.timestamp,
                toolsUsed: [],
                costUsd: 0,
            });
            continue;
        }
        // Skip past the assistant message we're consuming
        i++;
        const assistantRecord = nextAssistant.record;
        const msg = assistantRecord.message;
        const usageRaw = msg?.usage ??
            assistantRecord.usage ??
            {};
        // --- Input tokens ---
        const inputTokens = usageRaw.inputTokens ??
            usageRaw.input_tokens ??
            0;
        // --- Output tokens ---
        const outputTokens = usageRaw.outputTokens ??
            usageRaw.output_tokens ??
            0;
        // --- Cache read ---
        // Claude: cache_read_input_tokens
        // OpenAI: usage.prompt_tokens_details.cached_tokens
        const promptDetails = usageRaw.prompt_tokens_details ?? {};
        const cacheRead = usageRaw.cacheReadInputTokens ??
            usageRaw.cache_read_input_tokens ??
            promptDetails.cached_tokens ??
            0;
        // --- Cache creation (write) ---
        // Claude: cache_creation_input_tokens or ephemeral buckets
        const cacheCreationObj = usageRaw.cacheCreation ??
            usageRaw.cache_creation ??
            {};
        const cacheWrite1h = cacheCreationObj.ephemeral_1h_input_tokens ??
            usageRaw.ephemeral_1h_input_tokens ??
            0;
        const cacheWrite5m = cacheCreationObj.ephemeral_5m_input_tokens ??
            usageRaw.ephemeral_5m_input_tokens ??
            0;
        const cacheCreation = usageRaw.cacheCreationInputTokens ??
            usageRaw.cache_creation_input_tokens ??
            (cacheWrite1h + cacheWrite5m);
        // --- Model ---
        const modelRaw = msg?.model ?? assistantRecord.model ?? "unknown";
        const model = (0, pricing_1.normalizeModelName)(modelRaw) ?? modelRaw;
        // --- Tools used ---
        const toolsUsed = [];
        const msgContent = msg?.content;
        if (Array.isArray(msgContent)) {
            for (const block of msgContent) {
                if (typeof block === "object" &&
                    block !== null &&
                    block.type === "tool_use") {
                    const name = block.name;
                    if (typeof name === "string" && !toolsUsed.includes(name)) {
                        toolsUsed.push(name);
                    }
                }
            }
        }
        // --- Cost ---
        const tokens = {
            input: inputTokens,
            output: outputTokens,
            cacheRead,
            cacheWrite: cacheCreation,
        };
        const costUsd = (0, pricing_1.calculateCost)(tokens, model, openclawDir);
        turns.push({
            turnIndex: turnIndex++,
            role: "assistant",
            inputTokens,
            outputTokens,
            cacheRead,
            cacheCreation,
            model,
            timestamp: nextAssistant.timestamp ?? current.timestamp,
            toolsUsed: toolsUsed.sort(),
            costUsd,
        });
    }
    // Sort by timestamp ascending; nulls go last
    turns.sort((a, b) => {
        if (!a.timestamp && !b.timestamp)
            return a.turnIndex - b.turnIndex;
        if (!a.timestamp)
            return 1;
        if (!b.timestamp)
            return -1;
        return a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0;
    });
    return turns;
}
/**
 * Extract the costliest user prompts from a single session JSONL file.
 *
 * Each entry pairs the user message text with the token/cost data from the
 * immediately following assistant response, mirroring Python's
 * `_extract_costly_prompts()`. Text is truncated to 120 characters.
 *
 * Sidechain messages and tool-result-only turns are skipped, matching the
 * Python implementation's filtering logic.
 *
 * @param filePath   Path to a `.jsonl` session file.
 * @param topN       Number of costliest prompts to return (default 5).
 * @param openclawDir  Optional OpenClaw data root for pricing config.
 * @returns CostlyPrompt[] sorted by costUsd descending, length <= topN.
 */
function extractCostlyPrompts(filePath, topN = 5, openclawDir) {
    // Get per-turn token/cost data from the existing parser
    const turns = parseSessionTurns(filePath, openclawDir);
    if (turns.length === 0)
        return [];
    // Re-read the file to extract user message text, walking in document order
    // so we can pair each user message with the matching turn by turnIndex.
    let content;
    try {
        const stat = fs.statSync(filePath);
        if (stat.size > 50 * 1024 * 1024)
            return [];
        content = fs.readFileSync(filePath, "utf-8");
    }
    catch {
        return [];
    }
    const lines = content.split("\n");
    // Build a turn iterator (turns are sorted ascending by timestamp/turnIndex)
    // We walk records in document order and pair user text with the next assistant turn.
    const turnsByIndex = new Map();
    for (const t of turns) {
        turnsByIndex.set(t.turnIndex, t);
    }
    let pendingPrompt = null;
    let turnIndexCounter = 0;
    const results = [];
    for (const line of lines) {
        const record = parseLine(line);
        if (!record)
            continue;
        const recType = record.type;
        if (recType === "user") {
            // Skip sidechain messages (internal tool orchestration, not real user prompts)
            if (record.isSidechain === true)
                continue;
            const msg = record.message;
            const rawContent = (msg?.content ?? record.content);
            // Extract text from the user message content
            let text = "";
            if (typeof rawContent === "string") {
                text = rawContent;
            }
            else if (Array.isArray(rawContent)) {
                const blocks = rawContent;
                // Skip if all items are tool_result blocks (not a real user prompt)
                const types = blocks
                    .filter((b) => typeof b === "object" && b !== null)
                    .map((b) => b.type);
                if (types.length > 0 && types.every((t) => t === "tool_result")) {
                    continue;
                }
                // Find the first text block
                for (const block of blocks) {
                    if (typeof block === "object" && block !== null) {
                        if (block.type === "text" && typeof block.text === "string") {
                            text = block.text;
                            break;
                        }
                    }
                    else if (typeof block === "string") {
                        text = block;
                        break;
                    }
                }
            }
            // Only track prompts with meaningful content (>5 chars, matching Python)
            if (text.length > 5) {
                const tsRaw = record.timestamp;
                let timestamp = null;
                if (tsRaw) {
                    try {
                        const ts = new Date(tsRaw);
                        if (!isNaN(ts.getTime()))
                            timestamp = ts.toISOString();
                    }
                    catch {
                        // keep null
                    }
                }
                pendingPrompt = { text: text.slice(0, 120), timestamp };
            }
        }
        else if (recType === "assistant" && pendingPrompt !== null) {
            // Find the matching turn by its position in the turn sequence
            const turn = turnsByIndex.get(turnIndexCounter);
            turnIndexCounter++;
            if (turn && turn.costUsd >= 0) {
                const freshInput = Math.max(0, turn.inputTokens - turn.cacheRead);
                results.push({
                    text: pendingPrompt.text,
                    tokensIn: turn.inputTokens,
                    tokensOut: turn.outputTokens,
                    cacheRead: turn.cacheRead,
                    cacheWrite: turn.cacheCreation,
                    freshInput,
                    costUsd: Math.round(turn.costUsd * 10000) / 10000,
                    model: turn.model,
                    timestamp: turn.timestamp ?? pendingPrompt.timestamp,
                });
            }
            pendingPrompt = null;
        }
    }
    results.sort((a, b) => b.costUsd - a.costUsd);
    return results.slice(0, topN);
}
/**
 * Classify runs as heartbeat/cron based on OpenClaw cron config.
 *
 * Reads ~/.openclaw/cron/ for heartbeat configurations and marks
 * matching agent runs accordingly.
 */
function classifyCronRuns(openclawDir, runs) {
    const cronDir = path.join(openclawDir, "cron");
    if (!fs.existsSync(cronDir))
        return;
    // Read cron config to identify heartbeat agents
    const cronAgents = new Set();
    try {
        const configPath = path.join(openclawDir, "config.json");
        if (fs.existsSync(configPath)) {
            const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
            const crons = config.cron ?? config.heartbeat ?? {};
            for (const key of Object.keys(crons)) {
                if (key === "__proto__" || key === "constructor" || key === "prototype")
                    continue;
                cronAgents.add(key);
            }
        }
    }
    catch {
        // No cron config, skip
    }
    // Also check cron directory for agent-named configs
    try {
        const cronFiles = fs.readdirSync(cronDir);
        for (const file of cronFiles) {
            if (file.endsWith(".json") || file.endsWith(".yaml")) {
                cronAgents.add(path.basename(file, path.extname(file)));
            }
        }
    }
    catch {
        // skip
    }
    // Mark matching runs
    for (const run of runs) {
        if (cronAgents.has(run.agentName)) {
            run.runType = "heartbeat";
        }
    }
}
//# sourceMappingURL=session-parser.js.map