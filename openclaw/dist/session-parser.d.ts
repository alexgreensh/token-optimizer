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
import { AgentRun, TurnData, CostlyPrompt } from "./models";
/**
 * Find the first existing OpenClaw data directory.
 */
export declare function findOpenClawDir(): string | null;
/**
 * Discover all agent directories under the OpenClaw data root.
 */
export declare function listAgents(openclawDir: string): string[];
/**
 * Find all session JSONL files for a given agent, optionally filtered by age.
 *
 * Returns array of { filePath, agentName, sessionId, mtime } sorted newest-first.
 */
export declare function findSessionFiles(openclawDir: string, agentName: string, days?: number): Array<{
    filePath: string;
    agentName: string;
    sessionId: string;
    mtime: number;
}>;
/**
 * Extract a short human-readable topic from raw user message text.
 *
 * Strategy (mirrors Python _extract_topic):
 * 1. Strip common boilerplate prefixes (case-insensitive).
 * 2. Return the first markdown heading (## or #) if present.
 * 3. Otherwise return the first non-empty sentence (up to the first period/newline).
 * 4. Truncate to 120 characters.
 */
export declare function extractTopic(text: string): string;
/**
 * Parse a single OpenClaw session JSONL file into an AgentRun.
 *
 * OpenClaw JSONL format:
 * - Each line is a JSON object with at minimum a "type" field
 * - Token data in assistant messages under "usage" or top-level fields
 * - Model ID in "model" field of assistant messages
 */
export declare function parseSession(filePath: string, agentName: string, openclawDir?: string): AgentRun | null;
/**
 * Scan all agents and sessions within the given day window.
 *
 * Returns all parsed AgentRuns sorted by timestamp (newest first).
 */
export declare function scanAllSessions(openclawDir: string, days?: number): AgentRun[];
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
export declare function parseSessionTurns(filePath: string, openclawDir?: string): TurnData[];
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
export declare function extractCostlyPrompts(filePath: string, topN?: number, openclawDir?: string): CostlyPrompt[];
/**
 * Classify runs as heartbeat/cron based on OpenClaw cron config.
 *
 * Reads ~/.openclaw/cron/ for heartbeat configurations and marks
 * matching agent runs accordingly.
 */
export declare function classifyCronRuns(openclawDir: string, runs: AgentRun[]): void;
//# sourceMappingURL=session-parser.d.ts.map