export interface TokenBreakdown {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
}
export declare function totalTokens(t: TokenBreakdown): number;
export type RunType = "manual" | "heartbeat" | "cron";
export type Outcome = "success" | "failure" | "empty" | "abandoned";
export type Severity = "low" | "medium" | "high" | "critical";
export interface AgentRun {
    system: "openclaw";
    sessionId: string;
    agentName: string;
    project: string;
    timestamp: Date;
    durationSeconds: number;
    tokens: TokenBreakdown;
    costUsd: number;
    model: string;
    runType: RunType;
    outcome: Outcome;
    messageCount: number;
    toolsUsed: string[];
    sourcePath: string;
    cacheWrite1hTokens?: number;
    cacheWrite5mTokens?: number;
    errorMessage?: string;
    /** First user message topic, extracted and truncated to 120 chars. */
    topic?: string;
}
export interface WasteFinding {
    system: "openclaw";
    agentName: string;
    wasteType: string;
    tier: number;
    severity: Severity;
    confidence: number;
    description: string;
    monthlyWasteUsd: number;
    monthlyWasteTokens: number;
    recommendation: string;
    fixSnippet: string;
    evidence: Record<string, unknown>;
}
export interface TurnData {
    turnIndex: number;
    role: string;
    inputTokens: number;
    outputTokens: number;
    cacheRead: number;
    cacheCreation: number;
    model: string;
    timestamp: string | null;
    toolsUsed: string[];
    costUsd: number;
}
export interface CostlyPrompt {
    /** User prompt text, truncated to 120 characters. */
    text: string;
    /** Total input tokens billed for the subsequent assistant turn (fresh + cache_read + cache_write). */
    tokensIn: number;
    /** Output tokens for the subsequent assistant turn. */
    tokensOut: number;
    /** Cache-read tokens consumed by this turn. */
    cacheRead: number;
    /** Cache-write tokens created by this turn. */
    cacheWrite: number;
    /** Fresh (non-cached) input tokens: tokensIn minus cacheRead. */
    freshInput: number;
    /** Estimated USD cost for this turn. */
    costUsd: number;
    /** Normalized model name (e.g. "sonnet", "gpt-5.2"). */
    model: string;
    /** ISO 8601 timestamp of the turn, or null if unavailable. */
    timestamp: string | null;
}
/** Models considered expensive (should not be used for heartbeat/cron tasks). */
export declare const EXPENSIVE_MODELS: Set<string>;
export interface AuditReport {
    scannedAt: Date;
    daysScanned: number;
    agentsFound: string[];
    totalSessions: number;
    totalCostUsd: number;
    totalTokens: number;
    findings: WasteFinding[];
    monthlySavingsUsd: number;
}
//# sourceMappingURL=models.d.ts.map