/**
 * Smart Compaction v2: intelligent extraction + last N messages fallback.
 *
 * v1: capture last N messages as markdown.
 * v2: extract decisions, errors, file modifications, and user instructions
 *     to preserve the most relevant context in fewer tokens.
 */
import { type CheckpointTrigger } from "./checkpoint-policy";
export interface CheckpointCaptureOptions {
    trigger?: CheckpointTrigger | string;
    reason?: string;
    fillPct?: number;
    qualityScore?: number;
    toolName?: string;
    eventKind?: string;
    activeAgents?: number;
    writeCount?: number;
    writeBurstCount?: number;
    contextWindow?: number;
    model?: string;
}
export declare function loadMessagesFromSessionFile(sessionFile: string): Array<{
    role: string;
    content: string;
    timestamp?: string;
}> | undefined;
export declare function captureCheckpoint(session: {
    sessionId: string;
    messages?: Array<{
        role: string;
        content: string;
        timestamp?: string;
    }>;
}, maxMessages?: number, options?: CheckpointCaptureOptions): string | null;
export declare function restoreCheckpoint(sessionId: string): string | null;
/**
 * v2 checkpoint: intelligent extraction + recent messages fallback.
 * Produces a more focused checkpoint than v1's raw last-N dump.
 */
export declare function captureCheckpointV2(session: {
    sessionId: string;
    messages?: Array<{
        role: string;
        content: string;
        timestamp?: string;
    }>;
}, maxRecentMessages?: number, options?: CheckpointCaptureOptions): string | null;
export declare function cleanupCheckpoints(maxAgeDays?: number): number;
//# sourceMappingURL=smart-compact.d.ts.map