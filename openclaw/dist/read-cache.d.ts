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
/**
 * Append a savings event to ~/.openclaw/token-optimizer/savings-events.jsonl.
 * Shape matches Python's _log_savings_event() row (timestamp, event_type,
 * tokens_saved, session_id, detail). Best-effort: never throws.
 *
 * OpenClaw savings events are kept separate from the Python trends.db so each
 * platform owns its own storage; the categories (checkpoint_restore,
 * hint_followed) are the same strings the Python dashboard uses, enabling
 * consistent cross-platform labelling if they are ever merged.
 */
export declare function logSavingsEvent(eventType: string, tokensSaved: number, sessionId: string, detail?: string, pricing?: {
    openclawDir?: string;
    model?: string;
    tokenClass?: string;
}): void;
/**
 * Sum tokensEst for files read at least MIN_COUNT times this session,
 * capped at 200 000 tokens and limited to 25 most-recently-accessed entries.
 *
 * Session-SCOPED, not agent-scoped: reads are cached under
 * `<agentId>-<session>.json` where agentId is often "unknown"/per-agent, but the
 * compact handler doesn't know that agentId. So we aggregate every
 * `*-<session>.json` cache for this session, which is robust to the agent key
 * and matches the intent ("what did THIS session read repeatedly").
 *
 * Mirrors Python SessionStore.get_active_read_tokens(limit=25, min_read_count=2).
 * Returns 0 on any error (floor fallback will be used instead).
 */
export declare function getActiveReadTokens(_agentId: string, sessionId: string): number;
/**
 * Record that a continuity hint surfaced these file paths to the session.
 * Idempotent: if a path already has an UNCREDITED entry, do NOT reset its
 * servedAt or credited flag (preserves the original freshness window).
 * Already-credited entries are not re-inserted so a re-serve of the same hint
 * can't resurrect a spent credit.
 *
 * Cap at 25 paths (matches Python record_hint_serve).
 */
export declare function recordHintServe(sessionId: string, filePaths: string[]): void;
/**
 * Check if filePath was hinted to this session within the freshness window
 * and not yet credited. If so, mark it credited and return true (caller logs
 * the savings event). Returns false in all other cases. Credit is permanent:
 * once marked, re-reads of the same file can never re-credit.
 *
 * Mirrors Python SessionStore.claim_hint_follow().
 */
export declare function claimHintFollow(sessionId: string, filePath: string): boolean;
/**
 * Drop every delta cache entry for a session. Exported so index.ts can
 * wire this into agent:stop / session:end events if OpenClaw ever exposes
 * them. Safe to call on an unknown sessionId (no-op).
 */
export declare function clearDeltaCacheForSession(sessionId: string): void;
export interface ReadToolInput {
    file_path?: string;
    offset?: number;
    limit?: number;
}
export interface ToolEventData {
    toolName: string;
    toolInput: ReadToolInput;
    agentId: string;
    sessionId: string;
}
/**
 * Resolve read-cache mode (warn/block) with the same two-layer rule as
 * isReadCacheDisabled: env var wins, config file is the fallback when env is
 * absent. Previously env-only, so a user's config-file read_cache_mode was
 * silently ignored and defaulted to "warn".
 */
export declare function resolveReadCacheMode(): string;
export declare function handleReadBefore(event: ToolEventData): {
    block: boolean;
    message: string;
} | null;
/**
 * Handle agent:tool:after for Edit/Write events (cache invalidation).
 */
export declare function handleWriteAfter(event: ToolEventData): void;
/**
 * Forget what one agent has read in a session (called on compaction).
 *
 * Reads are cached per `<agentId>-<session>.json`. Only the compacting agent's
 * file is removed, so a sibling agent in the same session keeps its own read
 * state. The in-memory delta baselines are left alone: they are keyed by
 * session, and a baseline is only consulted for a file that still has a read
 * entry, so the next read of a cleared file re-seeds it.
 */
export declare function clearCache(agentId: string, sessionId: string): void;
//# sourceMappingURL=read-cache.d.ts.map