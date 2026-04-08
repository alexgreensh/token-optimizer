/**
 * Coach module for OpenClaw Token Optimizer.
 *
 * Generates a holistic "health score" for an OpenClaw setup by analyzing
 * context overhead, skill usage, model routing, and session patterns.
 * Produces structured data consumed by the Coach tab in the dashboard.
 */
import { AgentRun, CostlyPrompt, WasteFinding } from "./models";
import { ContextAudit } from "./context-audit";
import { AgentCostBreakdown } from "./dashboard";
export interface CoachPattern {
    name: string;
    detail: string;
    severity?: "high" | "medium" | "low";
    fix?: string;
    savings?: string;
    earned?: boolean;
}
export interface CoachData {
    health_score: number;
    grade: string;
    patterns_good: CoachPattern[];
    patterns_bad: CoachPattern[];
    costly_prompts: CostlyPrompt[];
    agent_costs: AgentCostBreakdown[];
    snapshot: {
        total_overhead: number;
        context_window: number;
        overhead_pct: number;
        usable_tokens: number;
        skill_count: number;
        mcp_server_count: number;
    };
}
/**
 * Generate structured coach data for the dashboard Coach tab.
 *
 * Scoring logic:
 * - Base score: 75
 * - Earned bonuses (+5 each):
 *   - Lean SOUL.md (under 2% of context window)
 *   - Usage tracking active (sessions exist)
 * - Penalties:
 *   - Unused skills >80% ratio AND count >20: -7 (high severity)
 *   - Unused skills >60% ratio AND count >15: -3 (low severity)
 *   - Oversized SOUL.md (>3% of context): -5
 *   - Too many MCP servers (>20 AND >3% of context): -5
 *   - Poor model routing (>85% expensive model): -5
 * - Clamped to 0-100, graded via scoreToGrade()
 */
export declare function generateCoachData(audit: ContextAudit, sessions: AgentRun[], costlyPrompts: CostlyPrompt[], agentCosts: AgentCostBreakdown[], unusedSkillFindings: WasteFinding[]): CoachData;
//# sourceMappingURL=coach.d.ts.map