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
    errorMessage?: string;
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