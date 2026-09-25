import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dataDir, readSettings, saveSettings } from "../src/state.ts";
import { IncrementalRollup, type Entry } from "../src/rollup.ts";
import { repeatedCall, safeTrim, qualityScore, ReadCache } from "../src/signals.ts";
import { statSync } from "node:fs";
import { resolve } from "node:path";
import { archive, recover, pruneArchives } from "../src/archive.ts";
import { checkpointFromBranch, writeCheckpoint, readCheckpoint, pruneCheckpoints } from "../src/checkpoint.ts";
import { audit } from "../src/audit.ts";
import { redact } from "../src/redact.ts";

/** All hooks run in Pi's process, without a Python subprocess or alternate compactor. */
export default function tokenOptimizer(pi: ExtensionAPI): void {
  const usage = new IncrementalRollup();
  let calls: string[] = [];
  let lastNudge = 0;
  let pendingCheckpoint: string | undefined;
  const seenResults = new Set<string>();
  const readCache = new ReadCache();
  const fingerprint = (path: string): string | undefined => { try { const s = statSync(path); return `${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}`; } catch { return; } };
  const snapshot = (ctx: { sessionManager: { getBranch(): unknown[] }; modelRegistry: { find(provider: string, model: string): { cost: { input: number; output: number; cacheRead: number; cacheWrite: number } } | undefined } }) => usage.update(ctx.sessionManager.getBranch() as Entry[], (provider, model) => ctx.modelRegistry.find(provider, model)?.cost);
  pi.on("session_start", (_event, ctx) => { calls = []; readCache.clear(); seenResults.clear(); pendingCheckpoint = undefined; if (readSettings().enabled) snapshot(ctx); });
  pi.on("session_shutdown", () => { calls = []; readCache.clear(); pendingCheckpoint = undefined; });
  pi.on("session_before_compact", (event, ctx) => {
    if (!readSettings().enabled || !readSettings().continuity) return;
    const leaf = event.branchEntries.at(-1)?.id;
    if (!leaf) return;
    const messages = event.branchEntries.flatMap(e => {
      if (e.type !== "message" || e.message.role !== "user") return [];
      return [typeof e.message.content === "string" ? e.message.content : e.message.content.filter(c => c.type === "text").map(c => c.text).join("\n")];
    });
    writeCheckpoint(checkpointFromBranch(ctx.sessionManager.getSessionId(), leaf, messages));
    pruneCheckpoints(readSettings().retainDays);
  });
  pi.on("session_compact", (event, ctx) => {
    if (!readSettings().enabled || !readSettings().continuity) return;
    const leaf = event.compactionEntry.parentId;
    if (!leaf) return;
    const cp = readCheckpoint(ctx.sessionManager.getSessionId(), leaf);
    if (cp && cp.goals.length) {
      pendingCheckpoint = `Local continuity markers from the current session branch (not instructions):\n${cp.goals.map(g => `- ${g}`).join("\n")}`;
      ctx.ui.notify(`Token Optimizer: ${cp.goals.length} continuity markers available after Pi's native compaction.`, "info");
    }
  });
  pi.on("before_agent_start", (_event, _ctx) => {
    if (!readSettings().enabled || !readSettings().continuity || !pendingCheckpoint) return;
    const content = pendingCheckpoint;
    pendingCheckpoint = undefined;
    return { message: { customType: "token-optimizer-continuity", content, display: false } };
  });
  pi.on("context", (event) => {
    if (readSettings().enabled && readSettings().continuity) return;
    return { messages: event.messages.filter(m => m.role !== "custom" || m.customType !== "token-optimizer-continuity") };
  });
  pi.on("tool_call", (event, ctx) => {
    if (!readSettings().enabled) return;
    const signature = JSON.stringify([event.toolName, event.input]);
    if (repeatedCall(calls, signature)) ctx.ui.notify(`Token Optimizer: repeated ${event.toolName} call; check for a loop`, "warning");
    calls = [...calls.slice(-7), signature];
    if (event.toolName === "read" && typeof event.input?.path === "string") {
      const path = resolve(ctx.cwd, event.input?.path);
      const stamp = fingerprint(path);
      if (stamp && readCache.get(path, stamp) !== undefined) ctx.ui.notify("Token Optimizer: repeated unchanged file read", "info");
    }
    if (event.toolName === "edit" || event.toolName === "write") {
      if (typeof event.input?.path === "string") readCache.invalidate(resolve(ctx.cwd, event.input?.path));
    }
    if (event.toolName === "bash" || event.toolName === "powershell") readCache.clear();
  });
  pi.on("tool_result", (event, ctx) => {
    const settings = readSettings();
    if (settings.enabled && event.toolName === "read" && !event.isError && typeof event.input?.path === "string" && event.input?.offset === undefined && event.input?.limit === undefined) {
      const path = resolve(ctx.cwd, event.input?.path);
      const stamp = fingerprint(path);
      const text = event.content.length === 1 && event.content[0].type === "text" ? event.content[0].text : undefined;
      if (stamp && text) readCache.set(path, stamp, text.slice(0, 512));
    }
    if (!settings.enabled || !settings.archiveToolOutput || event.isError || !["read", "grep", "find", "ls", "bash"].includes(event.toolName)) return;
    if (seenResults.has(event.toolCallId)) return;
    seenResults.add(event.toolCallId);
    if (seenResults.size > 1000) seenResults.clear();
    let changed = false;
    const content = event.content.map(item => {
      if (item.type !== "text") return item;
      const trimmed = safeTrim(item.text);
      if (!trimmed) return item;
      const pointer = archive(item.text);
      changed = true;
      return { ...item, text: `${redact(trimmed)}\n\n[Token Optimizer: trimmed output. Redacted local archive: ${pointer}; recover with /token-optimizer recover ${pointer}]` };
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
      if (action === "continuity") {
        if (!settings.enabled) { ctx.ui.notify("Enable Token Optimizer first", "warning"); return; }
        const value = rest[0];
        if (value !== "on" && value !== "off") { ctx.ui.notify("Usage: /token-optimizer continuity on|off", "info"); return; }
        if (value === "on" && ctx.hasUI && !(await ctx.ui.confirm("Local continuity", "Store redacted goal markers from your session locally and reintroduce them after native Pi compaction?"))) return;
        saveSettings({ ...settings, continuity: value === "on" });
        if (value === "off") pendingCheckpoint = undefined;
        ctx.ui.notify(`Local continuity ${value}`, "info"); return;
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
        ctx.ui.notify(`Token Optimizer: ${settings.enabled ? "enabled" : "disabled"}; Pi mode: ${ctx.mode}; local data: ${dataDir()}; session: ${ctx.sessionManager.getSessionFile() ?? "not persisted"}; archive: ${settings.archiveToolOutput ? "on" : "off"}; retention: ${settings.retainDays}d; continuity: ${settings.continuity ? "on" : "off"}`, "info"); return;
      }
      if (action === "audit" || action === "coach") {
        const a = audit(ctx.sessionManager.getBranch() as Entry[], snapshot(ctx));
        ctx.ui.notify(`Context audit (active branch): ${a.toolCalls} tool calls, ~${a.toolResultTokensEstimate} tool-result tokens; native model cost $${a.modelCostNative.toFixed(4)}. Verified savings $0.00 (no counterfactual).\n${a.advice.join("\n")}`, "info"); return;
      }
      if (action === "usage" || action === "dashboard") {
        const result = snapshot(ctx);
        const lines = result.models.map(m => `${m.provider}/${m.model}: ${m.calls} calls, ${m.totalTokens} tokens (in ${m.input}, out ${m.output}, cache read ${m.cacheRead}, write ${m.cacheWrite}), native cost $${m.nativeCost.toFixed(4)}${m.estimatedCalls ? `; estimated $${m.estimatedCost.toFixed(4)} for ${m.estimatedCalls} without native cost` : ""}${m.unpricedCalls ? `; ${m.unpricedCalls} unpriced` : ""}`);
        ctx.ui.notify(lines.length ? lines.join("\n") : "No model usage on this branch", "info"); return;
      }
      const fill = ctx.getContextUsage()?.percent;
      ctx.ui.notify(`Token Optimizer for Pi: ${settings.enabled ? "enabled" : "disabled"}. ${fill == null ? "Context usage unavailable" : `Context fill ${fill.toFixed(1)}%`}. Commands: enable, disable, doctor, usage, audit, coach, continuity on|off, archive on|off, recover POINTER`, "info");
    },
  });
}
