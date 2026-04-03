import { AgentRun } from "./models";
import { ContextAudit } from "./context-audit";
export interface SessionCheckpointState {
    capturedFillBands: Set<number>;
    capturedQualityThresholds: Set<number>;
    capturedMilestones: Set<string>;
    lastCheckpointAt: number;
    lastEvaluatedAt: number;
    editWriteCount: number;
    editedFiles: Set<string>;
    editBatchMarkerWrites: number;
    editBatchMarkerFiles: number;
}
export interface RuntimeSnapshot {
    fillPct: number;
    qualityScore: number;
}
export interface CheckpointDecision {
    trigger: string;
    fillPct?: number;
    qualityScore?: number;
}
export interface CheckpointTelemetry {
    sessionId: string;
    messages?: Array<{
        role: string;
        content: string;
        timestamp?: string;
    }>;
    fillPct?: number;
    qualityScore?: number;
    toolName?: string;
    eventKind: "session-patch" | "tool-before" | "tool-after" | "session-end";
    activeAgents?: number;
    writeCount?: number;
    writeBurstCount?: number;
    contextWindow?: number;
    model?: string;
    timestamp?: number;
    agentId?: string;
    sessionFile?: string;
}
export interface CheckpointHealth {
    checkpointRoot: string;
    sessionCount: number;
    checkpointCount: number;
    policyCount: number;
    pendingCount: number;
    checkpointBytes: number;
    recentEventCount: number;
    lastTrigger?: string;
    issues: string[];
}
export interface CheckpointTelemetrySummary {
    enabled: boolean;
    eventLog: string;
    days: number;
    totalEvents: number;
    recentEvents: number;
    byTrigger: Record<string, number>;
    lastEvent: {
        timestamp?: string;
        sessionId?: string;
        trigger?: string;
        fillPct?: number;
        qualityScore?: number;
    } | null;
}
export type CheckpointTrigger = "compact" | "stop" | "stop-failure" | "end" | "milestone-pre-fanout" | "milestone-edit-batch" | `progressive-${number}` | `quality-${number}`;
export declare const FILL_BANDS: number[];
export declare const QUALITY_THRESHOLDS: number[];
export declare function checkpointSessionDir(sessionId: string): string;
export declare function checkpointManifestPath(sessionId: string): string;
export declare function getCheckpointState(sessionId: string): SessionCheckpointState;
export declare function clearCheckpointState(sessionId: string): void;
export declare function registerWriteEvent(sessionId: string, filePath?: string): SessionCheckpointState;
export declare function shouldEvaluateRuntimeState(sessionId: string, nowMs?: number): boolean;
export declare function markEvaluated(sessionId: string, nowMs?: number): void;
export declare function buildRuntimeSnapshot(run: AgentRun, contextAudit?: ContextAudit | null): RuntimeSnapshot;
export declare function maybeDecideSnapshotCheckpoint(sessionId: string, snapshot: RuntimeSnapshot, nowMs?: number): CheckpointDecision | null;
export declare function maybeDecidePreFanoutCheckpoint(sessionId: string, snapshot?: RuntimeSnapshot, nowMs?: number): CheckpointDecision | null;
export declare function maybeDecideEditBatchCheckpoint(sessionId: string, snapshot?: RuntimeSnapshot, nowMs?: number): CheckpointDecision | null;
export declare function recordCheckpointDecision(sessionId: string, trigger: string, nowMs?: number): void;
export declare function registerCheckpointCapture(sessionId: string, trigger: CheckpointTrigger, telemetry: Partial<CheckpointTelemetry>): void;
export declare function getCheckpointTelemetrySummary(days?: number): CheckpointTelemetrySummary;
export declare function getCheckpointFiles(sessionId: string): Array<{
    path: string;
    trigger: string;
    createdAt: number;
}>;
export declare function cleanupPolicyArtifacts(maxAgeDays?: number): number;
export declare function getCheckpointHealth(): CheckpointHealth;
//# sourceMappingURL=checkpoint-policy.d.ts.map