import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dataDir, readSettings, saveSettings } from "../src/state.ts";
import { IncrementalRollup, type Entry } from "../src/rollup.ts";
import { repeatedCall, safeTrim, qualityScore } from "../src/signals.ts";
import { archive, recover, pruneArchives } from "../src/archive.ts";
import { checkpointFromBranch, writeCheckpoint, readCheckpoint } from "../src/checkpoint.ts";

/** All hooks run in Pi's process, without a Python subprocess or alternate compactor. */
export default function tokenOptimizer(pi: ExtensionAPI): void {
  const usage = new IncrementalRollup();
  let calls: string[] = [];
  let lastNudge = 0;
  const snapshot = (ctx: { sessionManager: { getBranch(): unknown[] } }) => usage.update(ctx.sessionManager.getBranch() as Entry[]);
  pi.on("session_start", (_event, ctx) => { calls = []; if (readSettings().enabled) snapshot(ctx); });
  pi.on("session_shutdown", () => { calls = []; });
  pi.on("session_before_compact", (event, ctx) => {
    if (!readSettings().enabled) return;
    const leaf = event.branchEntries.at(-1)?.id;
    if (!leaf) return;
    const messages = event.branchEntries.flatMap(e => {
      if (e.type !== "message" || e.message.role !== "user") return [];
      return [typeof e.message.content === "string" ? e.message.content : e.message.content.filter(c => c.type === "text").map(c => c.text).join("\n")];
    });
    writeCheckpoint(checkpointFromBranch(ctx.sessionManager.getSessionId(), leaf, messages));
  });
  pi.on("session_compact", (event, ctx) => {
    if (!readSettings().enabled) return;
    const leaf = event.compactionEntry.parentId;
    if (!leaf) return;
    const cp = readCheckpoint(ctx.sessionManager.getSessionId(), leaf);
    if (cp && cp.goals.length) ctx.ui.notify(`Token Optimizer: local checkpoint has ${cp.goals.length} goal/decision markers. Pi's native compaction remains in control.`, "info");
  });
  pi.on("tool_call", (event, ctx) => {
    if (!readSettings().enabled) return;
    const signature = JSON.stringify([event.toolName, event.input]);
    if (repeatedCall(calls, signature)) ctx.ui.notify(`Token Optimizer: repeated ${event.toolName} call; check for a loop`, "warning");
    calls = [...calls.slice(-7), signature];
  });
  pi.on("tool_result", (event, ctx) => {
    const settings = readSettings();
    if (!settings.enabled || !settings.archiveToolOutput || event.isError || !["read", "grep", "find", "ls", "bash"].includes(event.toolName)) return;
    let changed = false;
    const content = event.content.map(item => {
      if (item.type !== "text") return item;
      const trimmed = safeTrim(item.text);
      if (!trimmed) return item;
      const pointer = archive(item.text);
      changed = true;
      return { ...item, text: `${trimmed}\n\n[Token Optimizer: trimmed output. Redacted local archive: ${pointer}; recover with /token-optimizer recover ${pointer}]` };
    });
    if (changed) pruneArchives(settings.retainDays);
    return changed ? { content } : undefined;
  });
  pi.on("message_end", (event, ctx) => {
    if (!readSettings().enabled || event.message.role !== "assistant") return;
    const text = event.message.content.filter(c => c.type === "text").map(c => c.text).join("\n");
    const score = qualityScore(text);
    if (score < 70) ctx.ui.notify(`Token Optimizer: response quality signal ${score}/100 (heuristic)`, "warning");
  });
  pi.on("turn_end", (_event, ctx) => {
    if (!readSettings().enabled) return;
    snapshot(ctx);
    const fill = ctx.getContextUsage()?.percent;
    if (fill != null && fill >= 75 && Date.now() - lastNudge > 180000) {
      lastNudge = Date.now();
      ctx.ui.notify(`Token Optimizer: context ${fill.toFixed(0)}% full. Pi controls compaction; consider /compact at a clean boundary.`, "warning");
    }
  });
  pi.registerCommand("token-optimizer", {
    description: "Token Optimizer status, consent, diagnostics and local usage",
    handler: async (args, ctx) => {
      const [action, ...rest] = args.trim().split(/\s+/);
      const settings = readSettings();
      if (action === "enable" || action === "disable") {
        if (action === "enable" && ctx.hasUI && !(await ctx.ui.confirm("Token Optimizer", "Store local session metrics for Token Optimizer? No data is shared."))) return;
        saveSettings({ ...settings, enabled: action === "enable" });
        ctx.ui.notify(`Token Optimizer ${action === "enable" ? "enabled" : "disabled"}`, "info");
        return;
      }
      if (action === "archive") {
        if (!settings.enabled) { ctx.ui.notify("Enable Token Optimizer first", "warning"); return; }
        const value = rest[0];
        if (value !== "on" && value !== "off") { ctx.ui.notify("Usage: /token-optimizer archive on|off", "info"); return; }
        if (value === "on" && ctx.hasUI && !(await ctx.ui.confirm("Local archives", "Store redacted, truncated tool outputs locally for recovery?"))) return;
        saveSettings({ ...settings, archiveToolOutput: value === "on" });
        ctx.ui.notify(`Local tool-output archives ${value}`, "info"); return;
      }
      if (action === "recover") {
        if (!settings.enabled || !settings.archiveToolOutput) { ctx.ui.notify("Local archives are off", "warning"); return; }
        try { ctx.ui.notify(recover(rest[0] || ""), "info"); }
        catch { ctx.ui.notify("Archive not found or recovery pointer invalid", "error"); }
        return;
      }
      if (action === "doctor") {
        ctx.ui.notify(`Token Optimizer: ${settings.enabled ? "enabled" : "disabled"}; Pi mode: ${ctx.mode}; local data: ${dataDir()}; session: ${ctx.sessionManager.getSessionFile() ?? "not persisted"}; archive: ${settings.archiveToolOutput ? "on" : "off"}; retention: ${settings.retainDays}d`, "info"); return;
      }
      if (action === "usage" || action === "dashboard") {
        const result = snapshot(ctx);
        const lines = result.models.map(m => `${m.provider}/${m.model}: ${m.calls} calls, ${m.totalTokens} tokens (in ${m.input}, out ${m.output}, cache read ${m.cacheRead}, write ${m.cacheWrite}), native cost $${m.nativeCost.toFixed(4)}${m.unpricedCalls ? `; ${m.unpricedCalls} unpriced` : ""}`);
        ctx.ui.notify(lines.length ? lines.join("\n") : "No model usage on this branch", "info"); return;
      }
      const fill = ctx.getContextUsage()?.percent;
      ctx.ui.notify(`Token Optimizer for Pi: ${settings.enabled ? "enabled" : "disabled"}. ${fill == null ? "Context usage unavailable" : `Context fill ${fill.toFixed(1)}%`}. Commands: enable, disable, doctor, usage, archive on|off, recover POINTER`, "info");
    },
  });
}
