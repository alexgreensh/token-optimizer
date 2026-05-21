import { tool, type ToolDefinition } from "@opencode-ai/plugin";
import { writeDashboard } from "../dashboard/generator.js";

export function createDashboardTool(getDataDir: () => string): ToolDefinition {
  return tool({
    description:
      "Generate and open the Token Optimizer dashboard. Shows quality trends, session history, " +
      "and daily stats in an interactive HTML page.",
    args: {
      days: tool.schema.number().optional().describe("Number of days to include (default 30)"),
    },
    async execute(args) {
      const dataDir = getDataDir();
      const days = Math.max(1, Math.min(args.days ?? 30, 365));

      try {
        const outputPath = writeDashboard({ dataDir, days });

        const { execFileSync } = await import("node:child_process");
        const platform = process.platform;
        if (platform === "darwin") {
          execFileSync("open", [outputPath]);
        } else if (platform === "linux") {
          try { execFileSync("xdg-open", [outputPath]); } catch { execFileSync("sensible-browser", [outputPath]); }
        } else if (platform === "win32") {
          execFileSync("cmd", ["/c", "start", "", outputPath]);
        }

        return {
          title: "Dashboard Generated",
          output: `Dashboard written to ${outputPath} and opened in browser.\n\nShowing ${days} days of session data.`,
        };
      } catch (err) {
        return {
          title: "Dashboard Error",
          output: `Failed to generate dashboard: ${err instanceof Error ? err.message : String(err)}`,
        };
      }
    },
  });
}
