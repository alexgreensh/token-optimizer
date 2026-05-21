import type { Plugin, Hooks, PluginInput, PluginOptions } from "@opencode-ai/plugin";
import type { Event } from "@opencode-ai/sdk";
import { SessionStore } from "./storage/session-store.js";
import { TrendsStore } from "./storage/trends.js";
import { resolveConfig } from "./util/env.js";
import { contextWindowForModel } from "./util/context-window.js";
import { computeQualityScore, enforceMonotonicity, type QualityResult } from "./quality/scoring.js";
import { logToolUse, type SessionMode } from "./activity/tracker.js";
import { summarizeLargeOutput, resetIntelCooldown } from "./activity/intel.js";
import { generateCompactionContext } from "./compaction/dynamic-instructions.js";
import { captureCheckpoint, pruneCheckpoints } from "./compaction/checkpoint.js";
import { restoreCheckpoint } from "./continuity/restore.js";
import { checkQualityNudge } from "./nudges/quality-nudge.js";
import { detectLoop } from "./nudges/loop-detection.js";
import { createTokenStatusTool } from "./tools/token-status.js";
import { createDashboardTool } from "./tools/dashboard.js";

const QUALITY_THROTTLE_MS = 2 * 60 * 1000;
const MAX_RECENT_MESSAGES = 20;

type SessionCreatedEvent = Extract<Event, { type: "session.created" }>;
type SessionDeletedEvent = Extract<Event, { type: "session.deleted" }>;

export const TokenOptimizerPlugin: Plugin = async (
  ctx: PluginInput,
  options?: PluginOptions,
) => {
  const config = resolveConfig(options);
  const dataDir = ctx.directory;

  let currentSessionId = "";
  let sessionStore: SessionStore | null = null;
  let trendsStore: TrendsStore | null = null;
  let lastQuality: QualityResult | null = null;
  let lastQualityTime = 0;
  let sessionStartTime = Date.now();
  let currentModel: string | undefined;
  let recentUserMessages: string[] = [];
  let continuityInjected = false;

  function getOrCreateStore(sessionId: string): SessionStore {
    if (sessionStore && currentSessionId === sessionId) return sessionStore;
    sessionStore?.close();
    currentSessionId = sessionId;
    sessionStore = new SessionStore(dataDir, sessionId);
    return sessionStore;
  }

  function getTrendsStore(): TrendsStore {
    if (!trendsStore) trendsStore = new TrendsStore(dataDir);
    return trendsStore;
  }

  function maybeComputeQuality(store: SessionStore, fillPct: number): QualityResult | null {
    const now = Date.now();
    if (now - lastQualityTime < QUALITY_THROTTLE_MS && lastQuality) return lastQuality;

    try {
      const contextWindow = contextWindowForModel(currentModel ?? "");
      const result = computeQualityScore(store, fillPct, currentModel, contextWindow, config);

      const cache = store.getQualityCache();
      const enforced = enforceMonotonicity(
        result,
        cache?.resource_health ?? null,
        cache?.compactions ?? 0,
        store.getCompactionCount(),
      );

      store.writeQualityCache({
        resource_health: enforced.resourceHealth,
        session_efficiency: enforced.sessionEfficiency,
        fill_pct: fillPct,
        compactions: store.getCompactionCount(),
        tool_calls: store.getToolCallCount(),
        last_nudge_time: cache?.last_nudge_time ?? 0,
        nudge_count: cache?.nudge_count ?? 0,
        data: cache?.data ?? null,
      });

      lastQuality = enforced;
      lastQualityTime = now;
      return enforced;
    } catch (err) {
      // Engage throttle on failure to prevent retry storms
      lastQualityTime = now;
      console.warn("[Token Optimizer] Quality scoring error:", err);
      return lastQuality;
    }
  }

  function collectSystemWarnings(store: SessionStore): string[] {
    const warnings: string[] = [];

    if (!lastQuality) return warnings;

    if (config.features.qualityNudges) {
      const cache = store.getQualityCache();
      const nudge = checkQualityNudge(store, lastQuality.resourceHealth, cache?.resource_health ?? null);
      if (nudge.shouldNudge && nudge.message) {
        warnings.push(nudge.message);
        store.writeQualityCache({
          resource_health: cache?.resource_health ?? lastQuality.resourceHealth,
          session_efficiency: cache?.session_efficiency ?? lastQuality.sessionEfficiency,
          fill_pct: cache?.fill_pct ?? lastQuality.fillPct,
          compactions: cache?.compactions ?? 0,
          tool_calls: cache?.tool_calls ?? 0,
          last_nudge_time: Date.now() / 1000,
          nudge_count: (cache?.nudge_count ?? 0) + 1,
          data: cache?.data ?? null,
        });
      }
    }

    if (config.features.loopDetection && recentUserMessages.length >= 3) {
      const loop = detectLoop(recentUserMessages);
      if (loop.detected && loop.message) {
        warnings.push(loop.message);
      }
    }

    if (lastQuality.fillWarning) {
      warnings.push(`[Token Optimizer] ${lastQuality.fillWarning.level}: ${lastQuality.fillWarning.message}`);
    }

    if (lastQuality.toolCallWarning) {
      warnings.push(`[Token Optimizer] ${lastQuality.toolCallWarning.level}: ${lastQuality.toolCallWarning.message}`);
    }

    if (lastQuality.regimeChange) {
      warnings.push(`[Token Optimizer] ${lastQuality.regimeChange.message}`);
    }

    return warnings;
  }

  function extractMessageText(input: { message?: unknown }): string {
    const msg = input as Record<string, unknown>;
    if (typeof msg.message === "string") return msg.message;
    if (msg.message && typeof msg.message === "object") {
      const m = msg.message as Record<string, unknown>;
      if (typeof m.content === "string") return m.content;
      if (Array.isArray(m.content)) {
        return m.content
          .map((b: unknown) => {
            if (typeof b === "string") return b;
            if (b && typeof b === "object" && "text" in b) return String((b as Record<string, unknown>).text);
            return "";
          })
          .join(" ");
      }
    }
    return "";
  }

  const hooks: Hooks = {
    tool: {
      token_status: createTokenStatusTool(() => ({
        store: sessionStore,
        lastQuality,
        sessionId: currentSessionId,
      })),
      token_dashboard: createDashboardTool(() => dataDir),
    },

    async "chat.message"(input, _output) {
      try {
        const store = getOrCreateStore(input.sessionID);

        if (input.model) {
          currentModel = input.model.modelID ?? (input.model as Record<string, unknown>).id as string | undefined;
        }

        const text = extractMessageText(input as unknown as { message?: unknown });
        if (text) {
          recentUserMessages.push(text.slice(0, 1000));
          while (recentUserMessages.length > MAX_RECENT_MESSAGES) {
            recentUserMessages.shift();
          }
        }

        const idx = store.incrementOperationIndex();
        const isSubstantive = text.split(/\s+/).length > 10;
        store.recordMessage(idx, "user", text.length, isSubstantive);

        const fillPct = estimateFillFromSession(store, currentModel);
        maybeComputeQuality(store, fillPct);
      } catch (err) {
        console.warn("[Token Optimizer] chat.message hook error:", err);
      }
    },

    async "tool.execute.before"(input, _output) {
      try {
        const store = getOrCreateStore(input.sessionID);

        if (input.tool === "Read" || input.tool === "file_read") {
          const filePath = typeof _output.args === "object" && _output.args?.file_path;
          if (typeof filePath === "string") {
            const idx = store.incrementOperationIndex();
            store.recordRead(idx, filePath);
          }
        }
      } catch (err) {
        console.warn("[Token Optimizer] tool.execute.before hook error:", err);
      }
    },

    async "tool.execute.after"(input, output) {
      try {
        const store = getOrCreateStore(input.sessionID);
        const idx = store.incrementOperationIndex();
        store.incrementToolCallCount();

        const toolName = input.tool;
        const resultSize = output.output?.length ?? 0;
        const isFailure = /\b(?:error|exception|failed|denied|ENOENT)\b/i.test(output.output ?? "");

        store.recordToolResult(idx, toolName, resultSize, isFailure);

        if (toolName === "Edit" || toolName === "Write" || toolName === "file_write" || toolName === "file_edit") {
          const filePath = typeof input.args === "object" && input.args?.file_path;
          if (typeof filePath === "string") {
            store.recordWrite(idx, filePath);
          }
        }

        if (toolName === "Agent" || toolName === "TaskCreate") {
          const promptSize = typeof input.args === "object" && typeof input.args?.prompt === "string"
            ? input.args.prompt.length
            : 0;
          store.recordAgentDispatch(idx, promptSize, resultSize);
        }

        if (config.features.activityTracking) {
          const command = typeof input.args === "object" && typeof input.args?.command === "string"
            ? input.args.command
            : "";
          logToolUse(store, toolName, command, isFailure, resultSize);
        }

        if (resultSize > 8192) {
          summarizeLargeOutput(output.output ?? "");
        }

        // Record both the tool result AND an assistant message (the tool invocation
        // itself is an assistant action). Without assistant messages, the bloated_results
        // signal can never detect referenced results.
        store.recordMessage(idx, "tool_result", resultSize, resultSize > 100);
        const assistantIdx = store.incrementOperationIndex();
        store.recordMessage(assistantIdx, "assistant", resultSize, true);
      } catch (err) {
        console.warn("[Token Optimizer] tool.execute.after hook error:", err);
      }
    },

    async "experimental.chat.system.transform"(input, output) {
      try {
        if (!input.sessionID) return;
        const store = getOrCreateStore(input.sessionID);

        if (input.model) {
          currentModel = input.model.id;
        }

        if (!continuityInjected && config.features.continuity) {
          const firstMsg = recentUserMessages[0];
          if (firstMsg) {
            continuityInjected = true;
            const match = restoreCheckpoint(dataDir, firstMsg, currentSessionId, config);
            if (match) {
              const cappedContent = match.content.slice(0, 2000);
              output.system.push(
                `[Token Optimizer] Restored context from prior session (${match.mode} mode, relevance: ${Math.round(match.score * 100)}%):\n${cappedContent}`,
              );
            }
          }
        }

        const warnings = collectSystemWarnings(store);
        for (const w of warnings) {
          output.system.push(w);
        }
      } catch (err) {
        console.warn("[Token Optimizer] system.transform hook error:", err);
      }
    },

    async "experimental.session.compacting"(input, output) {
      try {
        if (!config.features.smartCompaction) return;

        const store = getOrCreateStore(input.sessionID);
        const mode = (store.getMeta("current_mode") as SessionMode) ?? "general";

        const recentReads = store.getRecentReads(20);
        const recentWrites = store.getRecentWrites(20);
        const allPaths = new Set([...recentReads.map((r) => r.path), ...recentWrites.map((w) => w.path)]);
        const activeFiles = [...allPaths].slice(0, 15);

        const fillPct = lastQuality?.fillPct ?? null;
        const qualityScore = lastQuality?.resourceHealth ?? null;

        captureCheckpoint(store, input.sessionID, "compaction", mode, qualityScore, fillPct);

        const context = generateCompactionContext(mode, activeFiles, qualityScore, fillPct);
        output.context.push(...context);
      } catch (err) {
        console.warn("[Token Optimizer] compacting hook error:", err);
      }
    },

    async "experimental.compaction.autocontinue"(input, _output) {
      try {
        const store = getOrCreateStore(input.sessionID);
        store.incrementCompaction();
        store.resetSignalAccumulators();
        resetIntelCooldown();

        lastQuality = null;
        lastQualityTime = 0;

        const fillPct = estimateFillFromSession(store, currentModel);
        maybeComputeQuality(store, fillPct);
      } catch (err) {
        console.warn("[Token Optimizer] autocontinue hook error:", err);
      }
    },

    async event(input) {
      try {
        const event = input.event;

        if (event.type === "session.created") {
          const created = event as SessionCreatedEvent;
          const sessionId = created.properties?.info?.id;
          if (sessionId) {
            getOrCreateStore(sessionId);
            sessionStartTime = Date.now();
            recentUserMessages = [];
            continuityInjected = false;
            lastQuality = null;
            lastQualityTime = 0;
            currentModel = undefined;
            resetIntelCooldown();
          }
        }

        if (event.type === "session.deleted") {
          const deleted = event as SessionDeletedEvent;
          const endedSessionId = deleted.properties?.info?.id;

          if (sessionStore && currentSessionId && currentSessionId === endedSessionId) {
            const storeRef = sessionStore;
            try {
              const mode = (storeRef.getMeta("current_mode") as SessionMode) ?? "general";
              try { captureCheckpoint(storeRef, currentSessionId, "session_end", mode, lastQuality?.resourceHealth ?? null, lastQuality?.fillPct ?? null); } catch {}

              if (config.features.trends) {
                try {
                  const trends = getTrendsStore();
                  const cache = storeRef.getQualityCache();
                  trends.recordSession({
                    sessionId: currentSessionId,
                    project: ctx.project.id ?? null,
                    model: currentModel ?? null,
                    tokensInput: 0,
                    tokensOutput: 0,
                    tokensCacheRead: 0,
                    tokensCacheWrite: 0,
                    costUsd: 0,
                    resourceHealth: cache?.resource_health ?? null,
                    sessionEfficiency: cache?.session_efficiency ?? null,
                    toolCalls: storeRef.getToolCallCount(),
                    compactions: storeRef.getCompactionCount(),
                    mode,
                    durationSeconds: Math.round((Date.now() - sessionStartTime) / 1000),
                  });
                } catch {}
              }

              try { pruneCheckpoints(storeRef, config); } catch {}
            } finally {
              storeRef.close();
              sessionStore = null;
              trendsStore?.close();
              trendsStore = null;
            }
          }
        }
      } catch (err) {
        console.warn("[Token Optimizer] event hook error:", err);
      }
    },
  };

  return hooks;
};

function estimateFillFromSession(store: SessionStore, model?: string): number {
  const cache = store.getQualityCache();
  if (cache?.fill_pct !== null && cache?.fill_pct !== undefined) {
    return cache.fill_pct;
  }
  const messages = store.getRecentMessages(100);
  const results = store.getRecentToolResults(100);
  const totalChars = messages.reduce((s, m) => s + m.text_length, 0)
    + results.reduce((s, r) => s + r.result_size, 0);
  const estimatedTokens = totalChars / 4;
  const ctxWindow = contextWindowForModel(model ?? "");
  return Math.min(1, ctxWindow > 0 ? estimatedTokens / ctxWindow : 0);
}
