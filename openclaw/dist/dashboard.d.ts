/**
 * Dashboard generator for Token Optimizer OpenClaw plugin.
 *
 * Data aggregation (RL1) + HTML generation (RL2).
 * Produces a standalone HTML file at ~/.openclaw/token-optimizer/dashboard.html
 */
import { AgentRun, WasteFinding, AuditReport, Severity } from "./models";
import { QualityReport } from "./quality";
import { ContextAudit } from "./context-audit";
export interface DashboardData {
    generatedAt: string;
    daysScanned: number;
    contextWindow: number;
    overview: OverviewData;
    agents: AgentSummary[];
    waste: WasteFinding[];
    daily: DailyBucket[];
    models: ModelBucket[];
    severityCounts: Record<Severity, number>;
    quality: QualityReport | null;
    context: ContextAudit | null;
    sessions: SessionRow[];
    pricingTier: string;
    pricingTierLabel: string;
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
export declare function buildDashboardData(runs: AgentRun[], report: AuditReport, quality?: QualityReport | null, context?: ContextAudit | null): DashboardData;
export declare function generateDashboardHtml(data: DashboardData): string;
export declare function writeDashboard(data: DashboardData): string;
export declare function getDashboardPath(): string;
export {};
//# sourceMappingURL=dashboard.d.ts.map