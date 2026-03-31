/**
 * Waste pattern detectors for OpenClaw agent sessions.
 *
 * Ported from fleet.py's detector classes. Each detector analyzes
 * AgentRun data and returns WasteFinding objects with confidence,
 * severity, monthly $ waste, and actionable fix snippets.
 *
 * Detectors implemented:
 * 1. HeartbeatModelWaste - expensive model for cron/heartbeat tasks
 * 2. HeartbeatOverFrequency - interval < 5 min across 3+ runs
 * 3. EmptyRuns - high input, near-zero output
 * 4. StaleCronConfig - dead paths in cron/hook commands
 * 5. SessionHistoryBloat - context growing without compaction
 * 6. LoopDetection - many messages with near-zero output
 * 7. AbandonedSessions - 1-2 messages then stopped
 * 8. GhostTokenQJL - QJL-inspired sketch clustering for ghost run detection
 * 9. ToolLoadingOverhead - sessions loading many tools without compact view (v2026.3.24+)
 */
import { AgentRun, WasteFinding } from "./models";
type DetectorFn = (runs: AgentRun[], config: Record<string, unknown>) => WasteFinding[];
export declare const ALL_DETECTORS: Array<{
    name: string;
    tier: number;
    fn: DetectorFn;
}>;
/**
 * Run all detectors against the given runs and config.
 * Returns all findings sorted by monthly waste (highest first).
 */
export declare function runAllDetectors(runs: AgentRun[], config?: Record<string, unknown>): WasteFinding[];
export {};
//# sourceMappingURL=waste-detectors.d.ts.map