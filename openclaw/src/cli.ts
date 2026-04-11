#!/usr/bin/env node
/**
 * Token Optimizer CLI for OpenClaw.
 *
 * Usage:
 *   npx token-optimizer scan [--days 30] [--json]
 *   npx token-optimizer audit [--days 30] [--json]
 */

import { audit, scan, generateDashboard, doctor as runDoctor, checkpointTelemetry } from "./index";
import { AgentRun, totalTokens } from "./models";
import { findOpenClawDir } from "./session-parser";
import { auditContext } from "./context-audit";
import { scoreQuality } from "./quality";
import { captureSnapshot, detectDrift } from "./drift";
import { validateImpact, Strategy as ValidateStrategy } from "./validate";
import {
  V5_FEATURES,
  isV5Enabled,
  setV5,
  listV5Features,
  hasSeenWelcome,
  markWelcomeSeen,
  type V5FeatureId,
} from "./v5-features";
import { getCompressionSummary } from "./telemetry";
import { execFile, execFileSync } from "child_process";
import * as fs from "fs";
import * as path from "path";

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";

/** Redact home directory from paths to avoid leaking usernames in shared output */
function redactPaths(obj: unknown): unknown {
  return JSON.parse(
    JSON.stringify(obj, (_key, val) =>
      typeof val === "string" && val.startsWith(HOME)
        ? "~" + val.slice(HOME.length)
        : val
    )
  );
}

function printUsage(): void {
  console.log(`Token Optimizer for OpenClaw v2.3.0

Usage:
  token-optimizer scan         [--days N] [--json]   Scan sessions and show token usage
  token-optimizer audit        [--days N] [--json]   Detect waste patterns with $ savings
  token-optimizer dashboard    [--days N]             Generate HTML dashboard and open
  token-optimizer context      [--json]               Show context overhead breakdown
  token-optimizer quality      [--days N] [--json]    Show quality score breakdown
  token-optimizer git-context  [--json]               Suggest files based on git state
  token-optimizer drift        [--snapshot]            Config drift detection
  token-optimizer validate     [--days N] [--strategy auto|halves] [--json]  Before/after impact comparison
  token-optimizer detect                               Check if OpenClaw is installed
  token-optimizer doctor       [--json]               Check checkpoint health and plugin status
  token-optimizer checkpoint-stats [--days N] [--json]  Summarize local checkpoint telemetry
  token-optimizer v5 status    [--json]               Show v5 Active Compression status
  token-optimizer v5 info FEAT                         Describe a v5 feature
  token-optimizer v5 enable FEAT                       Enable a v5 feature
  token-optimizer v5 disable FEAT                      Disable a v5 feature
  token-optimizer v5 welcome                           Show the v5 welcome prompt

Options:
  --days N      Number of days to scan (default: 30)
  --json        Output as JSON for agent consumption
  --snapshot    Capture current config snapshot (drift command)`);
}

function cmdV5Status(json: boolean): void {
  const features = listV5Features();
  const summary = getCompressionSummary(30);
  if (json) {
    console.log(JSON.stringify({ features, summary }, null, 2));
    return;
  }

  console.log(`\nv5 Active Compression Status`);
  console.log("=".repeat(50));
  for (const f of features) {
    const state =
      f.status === "deferred"
        ? "deferred"
        : f.enabled
          ? "enabled"
          : "disabled";
    console.log(
      `  ${f.label.padEnd(22)} [${state.padEnd(8)}] risk=${f.risk} status=${f.status}`
    );
  }
  console.log();
  console.log(
    `  Last 30 days: ${summary.total_events} events, ${summary.total_tokens_saved.toLocaleString()} tokens saved (${(summary.overall_ratio * 100).toFixed(1)}%)`
  );
  if (Object.keys(summary.by_feature).length > 0) {
    console.log(`\n  By feature:`);
    for (const [id, data] of Object.entries(summary.by_feature)) {
      console.log(
        `    ${id.padEnd(22)} ${String(data.events).padStart(5)} events  ${data.tokens_saved.toLocaleString()} saved`
      );
    }
  }
  console.log();
}

function cmdV5Info(featureId: string): void {
  const feature = V5_FEATURES[featureId as V5FeatureId];
  if (!feature) {
    console.log(
      `Unknown v5 feature: ${featureId}\nKnown ids: ${Object.keys(V5_FEATURES).join(", ")}`
    );
    process.exit(1);
  }
  const enabled = isV5Enabled(featureId as V5FeatureId);
  console.log(`\n${feature.label} (${feature.id})`);
  console.log("=".repeat(50));
  console.log(`  Status:   ${feature.status}`);
  console.log(`  Risk:     ${feature.risk}`);
  console.log(`  Enabled:  ${enabled}`);
  console.log(`  Default:  ${feature.defaultEnabled}`);
  console.log(`\n  ${feature.description}\n`);
}

function cmdV5Toggle(featureId: string, on: boolean): void {
  const feature = V5_FEATURES[featureId as V5FeatureId];
  if (!feature) {
    console.log(
      `Unknown v5 feature: ${featureId}\nKnown ids: ${Object.keys(V5_FEATURES).join(", ")}`
    );
    process.exit(1);
  }
  if (feature.status === "deferred") {
    console.log(
      `${feature.label} is deferred in this release and cannot be toggled.`
    );
    process.exit(1);
  }
  setV5(featureId as V5FeatureId, on);
  console.log(`${feature.label} is now ${on ? "enabled" : "disabled"}.`);
}

function cmdV5Welcome(): void {
  const features = listV5Features();
  console.log(`\nWelcome to Token Optimizer v2.3.0!`);
  console.log("=".repeat(50));
  console.log(
    "v5 Active Compression is now live in OpenClaw. The low-risk features ship ON by default; the rest stay opt-in until you flip them on:\n"
  );
  for (const f of features) {
    const marker =
      f.status === "deferred" ? "[ - ]" : f.enabled ? "[ON ]" : "[off]";
    const note = f.status === "deferred" ? " (deferred — API gap)" : "";
    console.log(`  ${marker} ${f.label}${note}`);
    console.log(`        ${f.description}`);
  }
  console.log(
    `\n  Toggle with:   token-optimizer v5 enable <feature-id>`
  );
  console.log(
    `                 token-optimizer v5 disable <feature-id>`
  );
  console.log(
    `  Inspect with:  token-optimizer v5 info <feature-id>`
  );
  console.log();
  markWelcomeSeen("2.3.0");
}

function maybeShowWelcomeOnce(): void {
  // Fires once on first CLI invocation after an upgrade to v2.3.0. Stays
  // silent on every subsequent call. Any failure is ignored so a broken
  // state file never blocks real commands.
  try {
    if (!hasSeenWelcome("2.3.0")) {
      cmdV5Welcome();
    }
  } catch {
    // Never crash the CLI over a welcome prompt.
  }
}

function parseArgs(): { command: string; days: number; json: boolean; snapshot: boolean; strategy: ValidateStrategy } {
  const args = process.argv.slice(2);
  let command = "help";
  let days = 30;
  let json = false;
  let snapshot = false;
  let strategy: ValidateStrategy = "auto";

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--days" && i + 1 < args.length) {
      days = Math.max(1, Math.min(parseInt(args[++i], 10) || 30, 365));
    } else if (arg === "--json") {
      json = true;
    } else if (arg === "--snapshot") {
      snapshot = true;
    } else if (arg === "--strategy" && i + 1 < args.length) {
      const s = args[++i];
      if (s === "auto" || s === "halves") strategy = s;
    } else if (!arg.startsWith("-")) {
      command = arg;
    }
  }

  return { command, days, json, snapshot, strategy };
}

// (parseArgs defined above with printUsage)

function cmdDetect(json: boolean): void {
  const dir = findOpenClawDir();
  if (json) {
    console.log(
      JSON.stringify({
        found: !!dir,
        path: dir,
      })
    );
  } else if (dir) {
    console.log(`OpenClaw found: ${dir}`);
  } else {
    console.log(
      "OpenClaw not found. Checked: ~/.openclaw, ~/.clawdbot, ~/.moltbot"
    );
    process.exit(1);
  }
}

function cmdDoctor(json: boolean): void {
  const report = runDoctor();
  if (json) {
    console.log(JSON.stringify(report, null, 2));
    return;
  }

  console.log(`\nCheckpoint Doctor`);
  console.log("=".repeat(50));
  console.log(`Status: ${(report as { ok?: boolean }).ok ? "healthy" : "needs attention"}`);
  console.log(`Checkpoint root: ${(report as { checkpointRoot?: string }).checkpointRoot ?? "unknown"}`);
  console.log(`Sessions: ${(report as { sessionCount?: number }).sessionCount ?? 0}`);
  console.log(`Checkpoint files: ${(report as { checkpointCount?: number }).checkpointCount ?? 0}`);
  console.log(`Policy files: ${(report as { policyCount?: number }).policyCount ?? 0}`);
  console.log(`Pending triggers: ${(report as { pendingCount?: number }).pendingCount ?? 0}`);
  console.log(`Stored bytes: ${(report as { checkpointBytes?: number }).checkpointBytes ?? 0}`);
  console.log(`Recent events (7d): ${(report as { recentCheckpointEvents?: number }).recentCheckpointEvents ?? 0}`);
  console.log(`Last trigger: ${(report as { lastCheckpointTrigger?: string }).lastCheckpointTrigger ?? "none"}`);

  const issues = (report as { issues?: string[] }).issues ?? [];
  if (issues.length > 0) {
    console.log("\nIssues:");
    for (const issue of issues) {
      console.log(`  - ${issue}`);
    }
  }
}

function cmdCheckpointStats(days: number, json: boolean): void {
  const report = checkpointTelemetry(days);
  if (json) {
    console.log(JSON.stringify(report, null, 2));
    return;
  }

  console.log(`\nCheckpoint Telemetry (${days}d)`);
  console.log("=".repeat(50));
  console.log(`Enabled: ${((report as { enabled?: boolean }).enabled ?? false) ? "yes" : "no"}`);
  console.log(`Event log: ${(report as { eventLog?: string }).eventLog ?? "unknown"}`);
  console.log(`Total events: ${(report as { totalEvents?: number }).totalEvents ?? 0}`);
  console.log(`Recent events: ${(report as { recentEvents?: number }).recentEvents ?? 0}`);
  const byTrigger = (report as { byTrigger?: Record<string, number> }).byTrigger ?? {};
  if (Object.keys(byTrigger).length > 0) {
    console.log("\nBy trigger:");
    for (const [trigger, count] of Object.entries(byTrigger)) {
      console.log(`  ${trigger}: ${count}`);
    }
  }
  const lastEvent = (report as { lastEvent?: Record<string, unknown> | null }).lastEvent;
  if (lastEvent) {
    console.log("\nLast event:");
    console.log(`  ${(lastEvent.timestamp as string) ?? "unknown"}  ${(lastEvent.trigger as string) ?? "unknown"}  session=${(lastEvent.sessionId as string) ?? "unknown"}`);
  }
}

function cmdScan(days: number, json: boolean): void {
  const runs = scan(days);
  if (!runs) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }

  if (json) {
    console.log(JSON.stringify(redactPaths(runs), null, 2));
    return;
  }

  if (runs.length === 0) {
    console.log(`No sessions found in the last ${days} days.`);
    return;
  }

  console.log(`\nScanned ${runs.length} sessions (last ${days} days)\n`);

  // Summary by agent
  const byAgent = new Map<string, { count: number; cost: number; tokens: number }>();
  for (const run of runs) {
    const entry = byAgent.get(run.agentName) ?? { count: 0, cost: 0, tokens: 0 };
    entry.count++;
    entry.cost += run.costUsd;
    entry.tokens += totalTokens(run.tokens);
    byAgent.set(run.agentName, entry);
  }

  console.log("Agent            Sessions   Cost        Tokens");
  console.log("-----            --------   ----        ------");
  for (const [agent, data] of byAgent) {
    const name = agent.padEnd(16).slice(0, 16);
    const count = String(data.count).padStart(8);
    const cost = `$${data.cost.toFixed(2)}`.padStart(11);
    const tokens = formatTokens(data.tokens).padStart(13);
    console.log(`${name} ${count} ${cost} ${tokens}`);
  }

  const totalCost = runs.reduce((s, r) => s + r.costUsd, 0);
  const totalTok = runs.reduce((s, r) => s + totalTokens(r.tokens), 0);
  console.log(`\nTotal: $${totalCost.toFixed(2)} across ${formatTokens(totalTok)} tokens`);
}

function cmdAudit(days: number, json: boolean): void {
  const report = audit(days);
  if (!report) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }

  if (json) {
    console.log(JSON.stringify(redactPaths(report), null, 2));
    return;
  }

  console.log(`\nToken Optimizer Audit (last ${days} days)`);
  console.log("=".repeat(50));
  console.log(`Sessions scanned: ${report.totalSessions}`);
  console.log(`Agents found: ${report.agentsFound.join(", ") || "none"}`);
  if (report.totalCostUsd > 0) {
    console.log(`Total cost: $${report.totalCostUsd.toFixed(2)}`);
  } else {
    console.log(`Total cost: unknown (configure pricing in openclaw.json)`);
  }
  console.log(`Total tokens: ${formatTokens(report.totalTokens)}`);
  console.log();

  if (report.findings.length === 0) {
    console.log("No waste patterns detected. Your setup looks clean.");
    return;
  }

  console.log(`Found ${report.findings.length} waste pattern(s):`);
  console.log(`Potential monthly savings: $${report.monthlySavingsUsd.toFixed(2)}`);
  console.log();

  for (const finding of report.findings) {
    const icon = severityIcon(finding.severity);
    console.log(`${icon} [${finding.severity.toUpperCase()}] ${finding.wasteType}`);
    console.log(`   ${finding.description}`);
    if (finding.monthlyWasteUsd > 0) {
      console.log(`   Monthly waste: $${finding.monthlyWasteUsd.toFixed(2)}`);
    }
    console.log(`   Fix: ${finding.recommendation}`);
    console.log();
  }
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function severityIcon(s: string): string {
  switch (s) {
    case "critical": return "!!!";
    case "high": return " !!";
    case "medium": return "  !";
    default: return "  .";
  }
}

function cmdDashboard(days: number): void {
  const filepath = generateDashboard(days);
  if (!filepath) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }
  console.log(`Dashboard written to: ${filepath}`);
  // Open in default browser
  const opener = process.platform === "darwin" ? "open" : process.platform === "win32" ? "start" : "xdg-open";
  execFile(opener, [filepath], () => { /* ignore errors */ });
}

function cmdContext(json: boolean): void {
  const dir = findOpenClawDir();
  if (!dir) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }

  const result = auditContext(dir);

  if (json) {
    console.log(JSON.stringify(result, null, 2));
    return;
  }

  console.log(`\nContext Overhead Audit`);
  console.log("=".repeat(50));
  console.log(`Total overhead: ${formatTokens(result.totalOverhead)} tokens per message\n`);

  for (const comp of result.components) {
    const bar = "█".repeat(Math.min(40, Math.round((comp.tokens / result.totalOverhead) * 40)));
    const opt = comp.isOptimizable ? "" : " (fixed)";
    console.log(`  ${comp.name.padEnd(25)} ${formatTokens(comp.tokens).padStart(8)}  ${bar}${opt}`);
  }

  if (result.recommendations.length > 0) {
    console.log("\nRecommendations:");
    for (const rec of result.recommendations) {
      console.log(`  → ${rec}`);
    }
  }
}

function cmdQuality(days: number, json: boolean): void {
  const runs = scan(days);
  if (!runs) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }

  const dir = findOpenClawDir();
  const ctxAudit = dir ? auditContext(dir) : undefined;
  const report = scoreQuality(runs, ctxAudit);

  if (json) {
    console.log(JSON.stringify(report, null, 2));
    return;
  }

  console.log(`\nQuality Score: ${report.grade} (${report.score}/100) (${report.band})`);
  console.log("=".repeat(50));

  for (const sig of report.signals) {
    const bar = "█".repeat(Math.round(sig.score / 2.5));
    const pad = " ".repeat(Math.max(0, 40 - Math.round(sig.score / 2.5)));
    console.log(`  ${sig.name.padEnd(22)} ${String(sig.score).padStart(3)}  ${bar}${pad}  (${(sig.weight * 100).toFixed(0)}%)`);
  }

  if (report.recommendations.length > 0) {
    console.log("\nRecommendations:");
    for (const rec of report.recommendations) {
      console.log(`  → ${rec}`);
    }
  }
}

function cmdGitContext(json: boolean): void {
  function runGit(...args: string[]): string {
    try {
      return execFileSync("git", args, { encoding: "utf-8", timeout: 10000 }).trim();
    } catch {
      return "";
    }
  }

  const diffOutput = runGit("diff", "--name-only");
  const stagedOutput = runGit("diff", "--name-only", "--cached");
  const statusOutput = runGit("status", "--porcelain");

  const modified = new Set<string>();
  if (diffOutput) diffOutput.split("\n").forEach((f) => modified.add(f));
  if (stagedOutput) stagedOutput.split("\n").forEach((f) => modified.add(f));
  for (const line of (statusOutput || "").split("\n")) {
    if (line.startsWith("??")) modified.add(line.slice(3).trim());
  }

  if (modified.size === 0) {
    if (json) {
      console.log(JSON.stringify({ modified: [], test_companions: [], co_changed: [], import_chain: [] }, null, 2));
    } else {
      console.log("\nNo modified files detected. Run this after making changes.\n");
    }
    return;
  }

  // Test companion mapping
  const testCompanions: Array<{ source: string; test: string }> = [];
  for (const f of [...modified].sort()) {
    const ext = path.extname(f);
    const stem = path.basename(f, ext);
    const dir = path.dirname(f);
    if (stem.toLowerCase().includes("test") || stem.toLowerCase().includes("spec")) continue;
    const candidates = [
      `test_${stem}${ext}`, `${stem}_test${ext}`, `${stem}.test${ext}`, `${stem}.spec${ext}`,
      `tests/test_${stem}${ext}`, `__tests__/${stem}${ext}`,
      `${dir}/test_${stem}${ext}`, `${dir}/${stem}.test${ext}`, `${dir}/${stem}.spec${ext}`,
      `${dir}/__tests__/${stem}${ext}`,
    ];
    for (const c of candidates) {
      if (fs.existsSync(c) && !modified.has(c)) {
        testCompanions.push({ source: f, test: c });
        break;
      }
    }
  }

  // Co-change analysis from last 50 commits
  const logOutput = runGit("log", "--oneline", "--name-only", "-50", "--pretty=format:");
  const coChanged = new Map<string, number>();
  if (logOutput) {
    for (const block of logOutput.split("\n\n")) {
      const files = block.split("\n").map((l) => l.trim()).filter(Boolean);
      for (const mf of modified) {
        if (files.includes(mf)) {
          for (const cf of files) {
            if (cf !== mf && !modified.has(cf)) {
              coChanged.set(cf, (coChanged.get(cf) ?? 0) + 1);
            }
          }
        }
      }
    }
  }
  const topCo = [...coChanged.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10);

  const result = {
    modified: [...modified].sort(),
    test_companions: testCompanions,
    co_changed: topCo.map(([file, times]) => ({ file, times })),
    import_chain: [], // Simplified for OpenClaw CLI
  };

  if (json) {
    console.log(JSON.stringify(result, null, 2));
    return;
  }

  console.log(`\nGit Context Suggestions`);
  console.log("=".repeat(50));
  console.log(`Modified files (${modified.size}):`);
  for (const f of [...modified].sort()) console.log(`  ${f}`);

  if (testCompanions.length > 0) {
    console.log(`\nTest companions (add to context):`);
    for (const tc of testCompanions) console.log(`  ${tc.test}  (tests ${tc.source})`);
  }

  if (topCo.length > 0) {
    console.log(`\nFrequently co-changed:`);
    for (const [f, n] of topCo) console.log(`  ${f}  (${n}x in last 50 commits)`);
  }
  console.log();
}

function cmdValidate(days: number, strategy: ValidateStrategy, json: boolean): void {
  const runs = scan(days);
  if (!runs) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }

  const result = validateImpact(runs, strategy);

  if (json) {
    console.log(JSON.stringify(result, null, 2));
    return;
  }

  console.log(`\n  VALIDATE IMPACT (${result.strategy} strategy)`);
  console.log(`  Split: ${result.splitLabel}`);
  console.log("  " + "=".repeat(58));
  console.log(`\n  ${"Metric".padEnd(20)} ${"Before".padStart(10)}  ${"After".padStart(10)}  ${"Change".padStart(10)}`);
  console.log(`  ${"-".repeat(20)}  ${"-".repeat(10)}  ${"-".repeat(10)}  ${"-".repeat(10)}`);
  console.log(`  ${"Avg tokens/session".padEnd(20)} ${formatTokens(result.before.avgTokens).padStart(10)}  ${formatTokens(result.after.avgTokens).padStart(10)}  ${(result.deltas.tokensPct >= 0 ? "+" : "") + result.deltas.tokensPct + "%"}`.padStart(10));
  console.log(`  ${"Avg cost/session".padEnd(20)} ${"$" + result.before.avgCost.toFixed(4)}  ${"$" + result.after.avgCost.toFixed(4)}  ${(result.deltas.costPct >= 0 ? "+" : "") + result.deltas.costPct + "%"}`);
  console.log(`  ${"Avg messages".padEnd(20)} ${String(result.before.avgMessages).padStart(10)}  ${String(result.after.avgMessages).padStart(10)}  ${(result.deltas.messagesPct >= 0 ? "+" : "") + result.deltas.messagesPct + "%"}`);
  console.log(`  ${"Cache hit rate".padEnd(20)} ${result.before.avgCacheHitRate.toFixed(3).padStart(10)}  ${result.after.avgCacheHitRate.toFixed(3).padStart(10)}  ${(result.deltas.cacheHitPct >= 0 ? "+" : "") + result.deltas.cacheHitPct + "%"}`);

  const verdictLabel = { improved: "UP", regressed: "DOWN", no_change: "FLAT", insufficient_data: "?" };
  console.log(`\n  Verdict: ${result.verdict.toUpperCase()} (${verdictLabel[result.verdict]})`);
  console.log(`  Sessions: ${result.before.count} before, ${result.after.count} after\n`);
}

function cmdDrift(snapshot: boolean): void {
  const dir = findOpenClawDir();
  if (!dir) {
    console.error("OpenClaw not found.");
    process.exit(1);
  }

  if (snapshot) {
    const filepath = captureSnapshot(dir);
    console.log(`Snapshot saved: ${filepath}`);
    return;
  }

  const report = detectDrift(dir);

  if (!report.hasDrift) {
    console.log(`No drift detected since ${report.snapshotDate}.`);
    return;
  }

  console.log(`\nDrift detected since ${report.snapshotDate}:`);
  console.log("=".repeat(50));

  for (const change of report.changes) {
    const icon = change.type === "added" ? "+" : change.type === "removed" ? "-" : "~";
    console.log(`  ${icon} [${change.component}] ${change.details}`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const { command, days, json, snapshot, strategy } = parseArgs();

// First-run welcome: shows once after upgrading to v2.3.0, then stays quiet.
// Skipped when the command is a v5 subcommand (user already opted in) or
// a JSON output mode (parseable output).
if (!json && !command.startsWith("v5") && command !== "help") {
  maybeShowWelcomeOnce();
}

switch (command) {
  case "detect":
    cmdDetect(json);
    break;
  case "validate":
    cmdValidate(days, strategy, json);
    break;
  case "doctor":
    cmdDoctor(json);
    break;
  case "checkpoint-stats":
    cmdCheckpointStats(days, json);
    break;
  case "scan":
    cmdScan(days, json);
    break;
  case "audit":
    cmdAudit(days, json);
    break;
  case "dashboard":
    cmdDashboard(days);
    break;
  case "context":
    cmdContext(json);
    break;
  case "quality":
    cmdQuality(days, json);
    break;
  case "git-context":
    cmdGitContext(json);
    break;
  case "drift":
    cmdDrift(snapshot);
    break;
  case "v5": {
    // v5 subcommand — args: [v5, action, featureId?]
    const subAction = process.argv[3] ?? "status";
    const subFeature = process.argv[4] ?? "";
    switch (subAction) {
      case "status":
        cmdV5Status(json);
        break;
      case "info":
        if (!subFeature) {
          console.log("Usage: token-optimizer v5 info <feature-id>");
          process.exit(1);
        }
        cmdV5Info(subFeature);
        break;
      case "enable":
        if (!subFeature) {
          console.log("Usage: token-optimizer v5 enable <feature-id>");
          process.exit(1);
        }
        cmdV5Toggle(subFeature, true);
        break;
      case "disable":
        if (!subFeature) {
          console.log("Usage: token-optimizer v5 disable <feature-id>");
          process.exit(1);
        }
        cmdV5Toggle(subFeature, false);
        break;
      case "welcome":
        cmdV5Welcome();
        break;
      default:
        console.log(
          `Unknown v5 subcommand: ${subAction}\nSupported: status, info, enable, disable, welcome`
        );
        process.exit(1);
    }
    break;
  }
  default:
    printUsage();
}
