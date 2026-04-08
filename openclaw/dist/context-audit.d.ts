/**
 * Context Optimization Audit for OpenClaw.
 *
 * Scans all system prompt components and reports per-component token overhead.
 * Includes individual skill breakdown, MCP server scanning, and manage data.
 */
import { AgentRun } from "./models";
export interface ContextComponent {
    name: string;
    path: string;
    tokens: number;
    category: "config" | "personality" | "memory" | "skills" | "tools" | "agents" | "system";
    isOptimizable: boolean;
}
export interface SkillDetail {
    name: string;
    path: string;
    tokens: number;
    fullFileTokens: number;
    description: string;
    isArchived: boolean;
}
export interface McpServer {
    name: string;
    command: string;
    toolCount: number;
    isDisabled: boolean;
}
export interface ManageData {
    skills: {
        active: SkillDetail[];
        archived: SkillDetail[];
    };
    mcpServers: {
        active: McpServer[];
        disabled: McpServer[];
    };
}
export interface ContextAudit {
    totalOverhead: number;
    components: ContextComponent[];
    skills: SkillDetail[];
    mcpServers: McpServer[];
    recommendations: string[];
    manage: ManageData;
}
/**
 * Scans tool calls across all provided AgentRun sessions for Skill invocations.
 * Returns a Map of skillName -> invocation count.
 *
 * OpenClaw records tool calls in AgentRun.toolsUsed as an array of tool name strings.
 * Skill invocations appear as the skill name directly (e.g. "token-optimizer") or
 * prefixed with "skill:" or "Skill:" depending on the OpenClaw version.
 * We normalize by stripping known prefixes and lowercasing before matching.
 */
export declare function getSkillUsageHistory(sessions: AgentRun[]): Map<string, number>;
export declare function auditContext(openclawDir: string): ContextAudit;
//# sourceMappingURL=context-audit.d.ts.map