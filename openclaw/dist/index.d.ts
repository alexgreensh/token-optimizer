/**
 * Token Optimizer for OpenClaw - Plugin Entry Point
 *
 * Uses definePluginEntry() to register with the OpenClaw plugin system:
 * - api.registerService() for the token-optimizer service
 * - api.on() for lifecycle events
 * - api.logger for structured logging
 */
import { AuditReport, AgentRun } from "./models";
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
 * Generate the HTML dashboard, write to disk, return the file path.
 */
export declare function generateDashboard(days?: number): string | null;
export declare function doctor(): Record<string, unknown>;
export declare function checkpointTelemetry(days?: number): CheckpointTelemetrySummary;
declare const _default: PluginEntryOptions;
export default _default;
//# sourceMappingURL=index.d.ts.map