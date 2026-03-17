#!/usr/bin/env node
/**
 * Token Optimizer CLI for OpenClaw.
 *
 * Usage:
 *   npx token-optimizer scan [--days 30] [--json]
 *   npx token-optimizer audit [--days 30] [--json]
 */

import { audit, scan, generateDashboard } from "./index";
import { AgentRun, totalTokens } from "./models";
import { findOpenClawDir } from "./session-parser";
import { auditContext } from "./context-audit";
import { scoreQuality } from "./quality";
import { captureSnapshot, detectDrift } from "./drift";
import { exec } from "child_process";

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
  console.log(`Token Optimizer for OpenClaw v1.1.0

Usage:
  token-optimizer scan      [--days N] [--json]   Scan sessions and show token usage
  token-optimizer audit     [--days N] [--json]   Detect waste patterns with $ savings
  token-optimizer dashboard [--days N]             Generate HTML dashboard and open
  token-optimizer context   [--json]               Show context overhead breakdown
  token-optimizer quality   [--days N] [--json]    Show quality score breakdown
  token-optimizer drift     [--snapshot]            Config drift detection
  token-optimizer detect                            Check if OpenClaw is installed

Options:
  --days N      Number of days to scan (default: 30)
  --json        Output as JSON for agent consumption
  --snapshot    Capture current config snapshot (drift command)`);
}

function parseArgs(): { command: string; days: number; json: boolean; snapshot: boolean } {
  const args = process.argv.slice(2);
  let command = "help";
  let days = 30;
  let json = false;
  let snapshot = false;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--days" && i + 1 < args.length) {
      days = Math.max(1, Math.min(parseInt(args[++i], 10) || 30, 365));
    } else if (arg === "--json") {
      json = true;
    } else if (arg === "--snapshot") {
      snapshot = true;
    } else if (!arg.startsWith("-")) {
      command = arg;
    }
  }

  return { command, days, json, snapshot };
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
  exec(`${opener} "${filepath}"`, () => { /* ignore errors */ });
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

  console.log(`\nQuality Score: ${report.score}/100 (${report.band})`);
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

const { command, days, json, snapshot } = parseArgs();

switch (command) {
  case "detect":
    cmdDetect(json);
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
  case "drift":
    cmdDrift(snapshot);
    break;
  default:
    printUsage();
}
