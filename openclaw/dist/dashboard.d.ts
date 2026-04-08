/**
 * Dashboard generator for Token Optimizer OpenClaw plugin.
 *
 * Data aggregation (RL1) + HTML generation (RL2).
 * Produces a standalone HTML file at ~/.openclaw/token-optimizer/dashboard.html
 */
import { AgentRun, WasteFinding, AuditReport, Severity } from "./models";
import { QualityReport } from "./quality";
import { ContextAudit } from "./context-audit";
import { CoachData } from "./coach";
export interface AgentCostBreakdown {
    name: string;
    runs: number;
    cost: number;
    tokens: number;
    role: "orchestrator" | "worker" | "unknown";
}
export interface DashboardData {
    generatedAt: string;
    daysScanned: number;
    contextWindow: number;
    overview: OverviewData;
    agents: AgentSummary[];
    agentCosts: AgentCostBreakdown[];
    waste: WasteFinding[];
    daily: DailyBucket[];
    models: ModelBucket[];
    severityCounts: Record<Severity, number>;
    quality: QualityReport | null;
    context: ContextAudit | null;
    sessions: SessionRow[];
    pricingTier: string;
    pricingTierLabel: string;
    coach: CoachData | null;
}
interface OverviewData {
    totalRuns: number;
    totalCost: number;
    totalTokens: number;
    allCostZero: boolean;
    monthlySavings: number;
    wasteCount: number;
    activeDays: number;
    unknownModelRuns: number;
}
interface AgentSummary {
    name: string;
    runs: number;
    cost: number;
    tokens: number;
    avgDuration: number;
    emptyPct: number;
    abandonedCount: number;
    models: Record<string, number>;
    dominantModel: string;
}
interface DailyBucket {
    date: string;
    cost: number;
    runs: number;
    tokens: number;
}
interface ModelBucket {
    model: string;
    cost: number;
    runs: number;
    tokens: number;
}
interface SessionRow {
    date: string;
    sessionId: string;
    agentName: string;
    model: string;
    tokens: number;
    cost: number;
    duration: number;
    messages: number;
    outcome: string;
    qualityScore: number;
    qualityGrade: string;
    qualityBand: string;
    cacheWrite1hTokens: number;
    cacheWrite5mTokens: number;
}
/**
 * Build a per-agent cost breakdown with orchestrator/worker/unknown role
 * classification.
 *
 * Role heuristics:
 * - "orchestrator": agent has at least one tool_use call for "Agent" or "Task"
 *   tools (i.e., it spawns sub-agents).
 * - "worker": agent name appears as a spawn target in another agent's toolsUsed.
 *   Workers never themselves spawn agents.
 * - "unknown": no parent-child relationship detectable (single agent or no data).
 *
 * When no parent-child relationships are detectable at all, every agent is
 * classified "unknown" and the list is simply cost-ranked.
 */
export declare function buildAgentCostBreakdown(runs: AgentRun[]): AgentCostBreakdown[];
export declare function buildDashboardData(runs: AgentRun[], report: AuditReport, quality?: QualityReport | null, context?: ContextAudit | null, coach?: CoachData | null): DashboardData;
export declare function generateDashboardHtml(data: DashboardData): string;
export declare function writeDashboard(data: DashboardData): string;
export declare function getDashboardPath(): string;
export {};
//# sourceMappingURL=dashboard.d.ts.map