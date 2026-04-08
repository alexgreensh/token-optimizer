/**
 * Token Optimizer for OpenClaw - Plugin Entry Point
 *
 * Uses definePluginEntry() to register with the OpenClaw plugin system:
 * - api.registerService() for the token-optimizer service
 * - api.on() for lifecycle events
 * - api.logger for structured logging
 */
import { parseSessionTurns, extractCostlyPrompts } from "./session-parser";
import { AuditReport, AgentRun, CostlyPrompt } from "./models";
export type { AgentCostBreakdown, DashboardData } from "./dashboard";
export { generateCoachData } from "./coach";
export type { CoachData, CoachPattern } from "./coach";
import { type CheckpointTelemetrySummary } from "./checkpoint-policy";
interface OpenClawApi {
    registerService(name: string, service: Record<string, unknown>): void;
    on(event: string, handler: (...args: unknown[]) => void): void;
    logger: {
        info(msg: string, ...args: unknown[]): void;
        warn(msg: string, ...args: unknown[]): void;
        error(msg: string, ...args: unknown[]): void;
    };
}
interface PluginEntryOptions {
    id: string;
    name: string;
    description: string;
    register: (api: OpenClawApi) => void;
}
/**
 * Run a full audit: scan sessions, classify cron runs, detect waste.
 */
export declare function audit(days?: number): AuditReport | null;
/**
 * Scan sessions only (no waste detection). Returns raw AgentRun data.
 */
export declare function scan(days?: number): AgentRun[] | null;
/**
 * Parse a session JSONL file into per-turn token/cost data.
 *
 * Each entry in the returned array represents one user→assistant exchange,
 * with token counts, tools called, model used, and cost for that turn.
 * Returns an empty array if the file cannot be read or has no valid turns.
 */
export { parseSessionTurns };
export { extractTopic } from "./session-parser";
/**
 * Extract the top N costliest user prompts from a session JSONL file.
 *
 * Pairs each user message text with the token/cost data from the subsequent
 * assistant turn. Sidechain messages and tool-result-only turns are skipped.
 * Text is truncated to 120 characters.
 *
 * Returns CostlyPrompt[] sorted by costUsd descending, length <= topN (default 5).
 */
export { extractCostlyPrompts };
export type { CostlyPrompt };
/**
 * Generate the HTML dashboard, write to disk, return the file path.
 */
export declare function generateDashboard(days?: number): string | null;
export declare function doctor(): Record<string, unknown>;
export declare function checkpointTelemetry(days?: number): CheckpointTelemetrySummary;
/**
 * Returns a skill-name -> invocation-count map built from tool call history
 * across all provided sessions. Use alongside auditContext().skills to feed
 * detectUnusedSkills().
 */
export { getSkillUsageHistory } from "./context-audit";
/**
 * Returns WasteFinding objects for installed skills that have zero invocations.
 * Pass auditContext().skills.active.map(s => s.name) as `installed`, and
 * getSkillUsageHistory(sessions) as `usageMap`.
 */
export { detectUnusedSkills } from "./waste-detectors";
declare const _default: PluginEntryOptions;
export default _default;
//# sourceMappingURL=index.d.ts.map