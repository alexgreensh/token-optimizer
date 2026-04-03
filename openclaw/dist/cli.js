#!/usr/bin/env node
"use strict";
/**
 * Token Optimizer CLI for OpenClaw.
 *
 * Usage:
 *   npx token-optimizer scan [--days 30] [--json]
 *   npx token-optimizer audit [--days 30] [--json]
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const index_1 = require("./index");
const models_1 = require("./models");
const session_parser_1 = require("./session-parser");
const context_audit_1 = require("./context-audit");
const quality_1 = require("./quality");
const drift_1 = require("./drift");
const child_process_1 = require("child_process");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
/** Redact home directory from paths to avoid leaking usernames in shared output */
function redactPaths(obj) {
    return JSON.parse(JSON.stringify(obj, (_key, val) => typeof val === "string" && val.startsWith(HOME)
        ? "~" + val.slice(HOME.length)
        : val));
}
function printUsage() {
    console.log(`Token Optimizer for OpenClaw v1.3.1

Usage:
  token-optimizer scan         [--days N] [--json]   Scan sessions and show token usage
  token-optimizer audit        [--days N] [--json]   Detect waste patterns with $ savings
  token-optimizer dashboard    [--days N]             Generate HTML dashboard and open
  token-optimizer context      [--json]               Show context overhead breakdown
  token-optimizer quality      [--days N] [--json]    Show quality score breakdown
  token-optimizer git-context  [--json]               Suggest files based on git state
  token-optimizer drift        [--snapshot]            Config drift detection
  token-optimizer detect                               Check if OpenClaw is installed
  token-optimizer doctor       [--json]               Check checkpoint health and plugin status
  token-optimizer checkpoint-stats [--days N] [--json]  Summarize local checkpoint telemetry

Options:
  --days N      Number of days to scan (default: 30)
  --json        Output as JSON for agent consumption
  --snapshot    Capture current config snapshot (drift command)`);
}
function parseArgs() {
    const args = process.argv.slice(2);
    let command = "help";
    let days = 30;
    let json = false;
    let snapshot = false;
    for (let i = 0; i < args.length; i++) {
        const arg = args[i];
        if (arg === "--days" && i + 1 < args.length) {
            days = Math.max(1, Math.min(parseInt(args[++i], 10) || 30, 365));
        }
        else if (arg === "--json") {
            json = true;
        }
        else if (arg === "--snapshot") {
            snapshot = true;
        }
        else if (!arg.startsWith("-")) {
            command = arg;
        }
    }
    return { command, days, json, snapshot };
}
// (parseArgs defined above with printUsage)
function cmdDetect(json) {
    const dir = (0, session_parser_1.findOpenClawDir)();
    if (json) {
        console.log(JSON.stringify({
            found: !!dir,
            path: dir,
        }));
    }
    else if (dir) {
        console.log(`OpenClaw found: ${dir}`);
    }
    else {
        console.log("OpenClaw not found. Checked: ~/.openclaw, ~/.clawdbot, ~/.moltbot");
        process.exit(1);
    }
}
function cmdDoctor(json) {
    const report = (0, index_1.doctor)();
    if (json) {
        console.log(JSON.stringify(report, null, 2));
        return;
    }
    console.log(`\nCheckpoint Doctor`);
    console.log("=".repeat(50));
    console.log(`Status: ${report.ok ? "healthy" : "needs attention"}`);
    console.log(`Checkpoint root: ${report.checkpointRoot ?? "unknown"}`);
    console.log(`Sessions: ${report.sessionCount ?? 0}`);
    console.log(`Checkpoint files: ${report.checkpointCount ?? 0}`);
    console.log(`Policy files: ${report.policyCount ?? 0}`);
    console.log(`Pending triggers: ${report.pendingCount ?? 0}`);
    console.log(`Stored bytes: ${report.checkpointBytes ?? 0}`);
    console.log(`Recent events (7d): ${report.recentCheckpointEvents ?? 0}`);
    console.log(`Last trigger: ${report.lastCheckpointTrigger ?? "none"}`);
    const issues = report.issues ?? [];
    if (issues.length > 0) {
        console.log("\nIssues:");
        for (const issue of issues) {
            console.log(`  - ${issue}`);
        }
    }
}
function cmdCheckpointStats(days, json) {
    const report = (0, index_1.checkpointTelemetry)(days);
    if (json) {
        console.log(JSON.stringify(report, null, 2));
        return;
    }
    console.log(`\nCheckpoint Telemetry (${days}d)`);
    console.log("=".repeat(50));
    console.log(`Enabled: ${(report.enabled ?? false) ? "yes" : "no"}`);
    console.log(`Event log: ${report.eventLog ?? "unknown"}`);
    console.log(`Total events: ${report.totalEvents ?? 0}`);
    console.log(`Recent events: ${report.recentEvents ?? 0}`);
    const byTrigger = report.byTrigger ?? {};
    if (Object.keys(byTrigger).length > 0) {
        console.log("\nBy trigger:");
        for (const [trigger, count] of Object.entries(byTrigger)) {
            console.log(`  ${trigger}: ${count}`);
        }
    }
    const lastEvent = report.lastEvent;
    if (lastEvent) {
        console.log("\nLast event:");
        console.log(`  ${lastEvent.timestamp ?? "unknown"}  ${lastEvent.trigger ?? "unknown"}  session=${lastEvent.sessionId ?? "unknown"}`);
    }
}
function cmdScan(days, json) {
    const runs = (0, index_1.scan)(days);
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
    const byAgent = new Map();
    for (const run of runs) {
        const entry = byAgent.get(run.agentName) ?? { count: 0, cost: 0, tokens: 0 };
        entry.count++;
        entry.cost += run.costUsd;
        entry.tokens += (0, models_1.totalTokens)(run.tokens);
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
    const totalTok = runs.reduce((s, r) => s + (0, models_1.totalTokens)(r.tokens), 0);
    console.log(`\nTotal: $${totalCost.toFixed(2)} across ${formatTokens(totalTok)} tokens`);
}
function cmdAudit(days, json) {
    const report = (0, index_1.audit)(days);
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
    }
    else {
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
function formatTokens(n) {
    if (n >= 1_000_000)
        return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000)
        return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}
function severityIcon(s) {
    switch (s) {
        case "critical": return "!!!";
        case "high": return " !!";
        case "medium": return "  !";
        default: return "  .";
    }
}
function cmdDashboard(days) {
    const filepath = (0, index_1.generateDashboard)(days);
    if (!filepath) {
        console.error("OpenClaw not found.");
        process.exit(1);
    }
    console.log(`Dashboard written to: ${filepath}`);
    // Open in default browser
    const opener = process.platform === "darwin" ? "open" : process.platform === "win32" ? "start" : "xdg-open";
    (0, child_process_1.execFile)(opener, [filepath], () => { });
}
function cmdContext(json) {
    const dir = (0, session_parser_1.findOpenClawDir)();
    if (!dir) {
        console.error("OpenClaw not found.");
        process.exit(1);
    }
    const result = (0, context_audit_1.auditContext)(dir);
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
function cmdQuality(days, json) {
    const runs = (0, index_1.scan)(days);
    if (!runs) {
        console.error("OpenClaw not found.");
        process.exit(1);
    }
    const dir = (0, session_parser_1.findOpenClawDir)();
    const ctxAudit = dir ? (0, context_audit_1.auditContext)(dir) : undefined;
    const report = (0, quality_1.scoreQuality)(runs, ctxAudit);
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
function cmdGitContext(json) {
    function runGit(...args) {
        try {
            return (0, child_process_1.execSync)(`git ${args.join(" ")}`, { encoding: "utf-8", timeout: 10000 }).trim();
        }
        catch {
            return "";
        }
    }
    const diffOutput = runGit("diff", "--name-only");
    const stagedOutput = runGit("diff", "--name-only", "--cached");
    const statusOutput = runGit("status", "--porcelain");
    const modified = new Set();
    if (diffOutput)
        diffOutput.split("\n").forEach((f) => modified.add(f));
    if (stagedOutput)
        stagedOutput.split("\n").forEach((f) => modified.add(f));
    for (const line of (statusOutput || "").split("\n")) {
        if (line.startsWith("??"))
            modified.add(line.slice(3).trim());
    }
    if (modified.size === 0) {
        if (json) {
            console.log(JSON.stringify({ modified: [], test_companions: [], co_changed: [], import_chain: [] }, null, 2));
        }
        else {
            console.log("\nNo modified files detected. Run this after making changes.\n");
        }
        return;
    }
    // Test companion mapping
    const testCompanions = [];
    for (const f of [...modified].sort()) {
        const ext = path.extname(f);
        const stem = path.basename(f, ext);
        const dir = path.dirname(f);
        if (stem.toLowerCase().includes("test") || stem.toLowerCase().includes("spec"))
            continue;
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
    const coChanged = new Map();
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
    for (const f of [...modified].sort())
        console.log(`  ${f}`);
    if (testCompanions.length > 0) {
        console.log(`\nTest companions (add to context):`);
        for (const tc of testCompanions)
            console.log(`  ${tc.test}  (tests ${tc.source})`);
    }
    if (topCo.length > 0) {
        console.log(`\nFrequently co-changed:`);
        for (const [f, n] of topCo)
            console.log(`  ${f}  (${n}x in last 50 commits)`);
    }
    console.log();
}
function cmdDrift(snapshot) {
    const dir = (0, session_parser_1.findOpenClawDir)();
    if (!dir) {
        console.error("OpenClaw not found.");
        process.exit(1);
    }
    if (snapshot) {
        const filepath = (0, drift_1.captureSnapshot)(dir);
        console.log(`Snapshot saved: ${filepath}`);
        return;
    }
    const report = (0, drift_1.detectDrift)(dir);
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
    default:
        printUsage();
}
//# sourceMappingURL=cli.js.map