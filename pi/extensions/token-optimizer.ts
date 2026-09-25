import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dataDir, readSettings, saveSettings } from "../src/state.js";

export default function tokenOptimizer(pi: ExtensionAPI): void {
  pi.registerCommand("token-optimizer", {
    description: "Token Optimizer status, consent, and diagnostics",
    handler: async (args, ctx) => {
      const action = args.trim().toLowerCase();
      const settings = readSettings();
      if (action === "enable" || action === "disable") {
        if (action === "enable" && ctx.hasUI && !(await ctx.ui.confirm("Token Optimizer", "Store local session metrics for Token Optimizer? No data is shared."))) return;
        saveSettings({ ...settings, enabled: action === "enable" });
        ctx.ui.notify(`Token Optimizer ${action === "enable" ? "enabled" : "disabled"}`, "info");
        return;
      }
      if (action === "doctor") {
        ctx.ui.notify(`Token Optimizer: ${settings.enabled ? "enabled" : "disabled"}; Pi mode: ${ctx.mode}; local data: ${dataDir()}; session: ${ctx.sessionManager.getSessionFile() ?? "not persisted"}`, "info");
        return;
      }
      const usage = ctx.getContextUsage();
      ctx.ui.notify(`Token Optimizer for Pi: ${settings.enabled ? "enabled" : "disabled"}. ${usage?.percent == null ? "Context usage unavailable" : `Context fill ${usage.percent.toFixed(1)}%`}. /token-optimizer enable | disable | doctor`, "info");
    },
  });
}
