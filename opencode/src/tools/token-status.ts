import { tool, type ToolDefinition } from "@opencode-ai/plugin";
import type { SessionStore } from "../storage/session-store.js";
import type { QualityResult } from "../quality/scoring.js";
import { scoreToBand } from "../util/grade.js";

export function createTokenStatusTool(getState: () => {
  store: SessionStore | null;
  lastQuality: QualityResult | null;
  sessionId: string;
}): ToolDefinition {
  return tool({
    description:
      "Report current context health: quality scores, fill percentage, activity mode, top warnings. " +
      "Use when you want to check context health before deciding whether to compact or start a new session.",
    args: {
      detail: tool.schema.boolean().optional().describe("Include per-signal breakdown"),
    },
    async execute(args) {
      const state = getState();

      if (!state.store || !state.lastQuality) {
        return {
          title: "Token Status",
          output: "No quality data available yet. Quality scoring starts after the first tool call.",
        };
      }

      const q = state.lastQuality;
      const mode = state.store.getMeta("current_mode") ?? "general";

      const lines: string[] = [
        `## Context Health Report`,
        "",
        `**Resource Health**: ${Math.round(q.resourceHealth)}/100 (${q.resourceHealthGrade})`,
        `**Session Efficiency**: ${Math.round(q.sessionEfficiency)}/100 (${q.sessionEfficiencyGrade})`,
        `**Context Fill**: ${Math.round(q.fillPct * 100)}% | **Band**: ${scoreToBand(q.resourceHealth)}`,
        `**Activity Mode**: ${mode}`,
        `**Tool Calls**: ${q.toolCalls} | **Compactions**: ${state.store.getCompactionCount()}`,
      ];

      if (q.fillWarning) {
        lines.push("", `**${q.fillWarning.level}**: ${q.fillWarning.message}`);
      }
      if (q.toolCallWarning) {
        lines.push(`**${q.toolCallWarning.level}**: ${q.toolCallWarning.message}`);
      }

      if (args.detail) {
        lines.push("", "### Signal Breakdown", "");
        lines.push("| Signal | Score | Detail |");
        lines.push("|--------|-------|--------|");
        for (const [name, bd] of Object.entries(q.breakdown)) {
          const displayName = name.replace(/_/g, " ");
          lines.push(`| ${displayName} | ${bd.score}/100 | ${bd.detail} |`);
        }
      }

      return {
        title: "Token Status",
        output: lines.join("\n"),
      };
    },
  });
}
