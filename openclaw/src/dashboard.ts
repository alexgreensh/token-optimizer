/**
 * Dashboard generator for Token Optimizer OpenClaw plugin.
 *
 * Data aggregation (RL1) + HTML generation (RL2).
 * Produces a standalone HTML file at ~/.openclaw/token-optimizer/dashboard.html
 */

import * as fs from "fs";
import * as path from "path";
import { AgentRun, WasteFinding, AuditReport, totalTokens, Severity } from "./models";
import { QualityReport, contextWindowForModel, scoreSessionQuality, scoreToGrade } from "./quality";
import { ContextAudit, SkillDetail, McpServer, ManageData } from "./context-audit";
import { loadPricingTier, PRICING_TIER_LABELS } from "./pricing";

// ---------------------------------------------------------------------------
// Data interfaces
// ---------------------------------------------------------------------------

export interface DashboardData {
  generatedAt: string;
  daysScanned: number;
  contextWindow: number;
  overview: OverviewData;
  agents: AgentSummary[];
  waste: WasteFinding[];
  daily: DailyBucket[];
  models: ModelBucket[];
  severityCounts: Record<Severity, number>;
  quality: QualityReport | null;
  context: ContextAudit | null;
  sessions: SessionRow[];
  pricingTier: string;
  pricingTierLabel: string;
}

interface OverviewData {
  totalRuns: number;
  totalCost: number;
  totalTokens: number;
  allCostZero: boolean;
  monthlySavings: number;
  wasteCount: number;
  activeDays: number;
  unknownModelRuns: number;
}

interface AgentSummary {
  name: string;
  runs: number;
  cost: number;
  tokens: number;
  avgDuration: number;
  emptyPct: number;
  abandonedCount: number;
  models: Record<string, number>;
  dominantModel: string;
}

interface DailyBucket {
  date: string;
  cost: number;
  runs: number;
  tokens: number;
}

interface ModelBucket {
  model: string;
  cost: number;
  runs: number;
  tokens: number;
}

interface SessionRow {
  date: string;
  sessionId: string;
  agentName: string;
  model: string;
  tokens: number;
  cost: number;
  duration: number;
  messages: number;
  outcome: string;
  qualityScore: number;
  qualityGrade: string;
  qualityBand: string;
  cacheWrite1hTokens: number;
  cacheWrite5mTokens: number;
}

// ---------------------------------------------------------------------------
// RL1: Data aggregation
// ---------------------------------------------------------------------------

function aggregateByAgent(runs: AgentRun[]): AgentSummary[] {
  const map = new Map<string, AgentRun[]>();
  for (const r of runs) {
    const list = map.get(r.agentName) ?? [];
    list.push(r);
    map.set(r.agentName, list);
  }

  const summaries: AgentSummary[] = [];
  for (const [name, agentRuns] of map) {
    const cost = agentRuns.reduce((s, r) => s + r.costUsd, 0);
    const tokens = agentRuns.reduce((s, r) => s + totalTokens(r.tokens), 0);
    const totalDur = agentRuns.reduce((s, r) => s + r.durationSeconds, 0);
    const emptyCount = agentRuns.filter((r) => r.outcome === "empty").length;
    const abandonedCount = agentRuns.filter((r) => r.outcome === "abandoned").length;

    const models: Record<string, number> = {};
    for (const r of agentRuns) {
      models[r.model] = (models[r.model] ?? 0) + r.costUsd;
    }

    let dominantModel = "unknown";
    let maxCost = 0;
    for (const [m, c] of Object.entries(models)) {
      if (c > maxCost) { maxCost = c; dominantModel = m; }
    }

    summaries.push({
      name,
      runs: agentRuns.length,
      cost,
      tokens,
      avgDuration: agentRuns.length > 0 ? totalDur / agentRuns.length : 0,
      emptyPct: agentRuns.length > 0 ? (emptyCount / agentRuns.length) * 100 : 0,
      abandonedCount,
      models,
      dominantModel,
    });
  }

  summaries.sort((a, b) => b.cost - a.cost);
  return summaries;
}

function aggregateByDay(runs: AgentRun[]): DailyBucket[] {
  const map = new Map<string, { cost: number; runs: number; tokens: number }>();
  for (const r of runs) {
    const date = r.timestamp.toISOString().slice(0, 10);
    const entry = map.get(date) ?? { cost: 0, runs: 0, tokens: 0 };
    entry.cost += r.costUsd;
    entry.runs++;
    entry.tokens += totalTokens(r.tokens);
    map.set(date, entry);
  }

  const buckets: DailyBucket[] = [];
  for (const [date, data] of map) {
    buckets.push({ date, ...data });
  }
  buckets.sort((a, b) => a.date.localeCompare(b.date));
  return buckets;
}

function aggregateByModel(runs: AgentRun[]): ModelBucket[] {
  const map = new Map<string, { cost: number; runs: number; tokens: number }>();
  for (const r of runs) {
    const entry = map.get(r.model) ?? { cost: 0, runs: 0, tokens: 0 };
    entry.cost += r.costUsd;
    entry.runs++;
    entry.tokens += totalTokens(r.tokens);
    map.set(r.model, entry);
  }

  const buckets: ModelBucket[] = [];
  for (const [model, data] of map) {
    buckets.push({ model, ...data });
  }
  buckets.sort((a, b) => b.cost - a.cost);
  return buckets;
}

export function buildDashboardData(
  runs: AgentRun[],
  report: AuditReport,
  quality: QualityReport | null = null,
  context: ContextAudit | null = null
): DashboardData {
  const allCostZero = runs.every((r) => r.costUsd === 0);
  const unknownModelRuns = runs.filter(
    (r) => r.model === "unknown"
  ).length;
  const activeDays = new Set(
    runs.map((r) => r.timestamp.toISOString().slice(0, 10))
  ).size;

  // Determine dominant model's context window
  const modelCounts = new Map<string, number>();
  for (const r of runs) {
    modelCounts.set(r.model, (modelCounts.get(r.model) ?? 0) + 1);
  }
  let dominantModel = "sonnet";
  let maxCount = 0;
  for (const [model, count] of modelCounts) {
    if (count > maxCount) { maxCount = count; dominantModel = model; }
  }
  const contextWindow = contextWindowForModel(dominantModel);

  const severityCounts: Record<Severity, number> = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
  };
  for (const f of report.findings) {
    severityCounts[f.severity]++;
  }

  // Build session rows (most recent first, cap at 200 for dashboard)
  // Sort newest first, then take 200 for the Sessions tab
  const sortedRuns = [...runs].sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime());
  const sessions: SessionRow[] = sortedRuns.slice(0, 200).map((r) => {
    const sq = scoreSessionQuality(r);
    return {
      date: r.timestamp.toISOString().slice(0, 10),
      sessionId: r.sessionId.length > 12 ? r.sessionId.slice(0, 12) : r.sessionId,
      agentName: r.agentName,
      model: r.model,
      tokens: totalTokens(r.tokens),
      cost: r.costUsd,
      duration: r.durationSeconds,
      messages: r.messageCount,
      outcome: r.outcome,
      qualityScore: sq.score,
      qualityGrade: sq.grade,
      qualityBand: sq.band,
      cacheWrite1hTokens: r.cacheWrite1hTokens ?? 0,
      cacheWrite5mTokens: r.cacheWrite5mTokens ?? 0,
    };
  });

  // Pricing tier
  const pricingTier = loadPricingTier();
  const pricingTierLabel = PRICING_TIER_LABELS[pricingTier] ?? pricingTier;

  return {
    generatedAt: new Date().toISOString(),
    daysScanned: report.daysScanned,
    contextWindow,
    overview: {
      totalRuns: runs.length,
      totalCost: report.totalCostUsd,
      totalTokens: report.totalTokens,
      allCostZero,
      monthlySavings: report.monthlySavingsUsd,
      wasteCount: report.findings.length,
      activeDays,
      unknownModelRuns,
    },
    agents: aggregateByAgent(runs),
    waste: report.findings,
    daily: aggregateByDay(runs),
    models: aggregateByModel(runs),
    severityCounts,
    quality,
    context,
    sessions,
    pricingTier,
    pricingTierLabel,
  };
}

// ---------------------------------------------------------------------------
// RL2: HTML generation
// ---------------------------------------------------------------------------

/** Shell-escape a string for safe inclusion in shell commands. */
function shellEsc(s: string): string {
  // Only allow safe characters in skill/server names for commands
  if (/^[a-zA-Z0-9_.-]+$/.test(s)) return s;
  // Otherwise single-quote and escape embedded single quotes
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

/**
 * Log-scale bar width: makes small values visible while preserving order.
 * Maps [1, contextWindow] to [2%, 80%] on a log scale.
 */
function logBarPct(tokens: number, contextWindow: number): number {
  if (tokens <= 0) return 0;
  const logVal = Math.log10(Math.max(tokens, 1));
  const logMax = Math.log10(contextWindow);
  return Math.max(2, (logVal / logMax) * 80);
}

/** Escape HTML to prevent XSS */
function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtCost(n: number): string {
  if (n >= 1000) return `$${(n / 1000).toFixed(1)}k`;
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n > 0) return `$${n.toFixed(3)}`;
  return "$0";
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function fmtDuration(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function fmtNumber(n: number): string {
  return n.toLocaleString("en-US");
}

function severityColor(s: Severity): string {
  switch (s) {
    case "critical": return "var(--c-danger)";
    case "high": return "var(--c-warning)";
    case "medium": return "var(--c-accent-cyan)";
    default: return "var(--c-text-dim)";
  }
}

function modelColor(m: string): string {
  if (m.includes("opus")) return "#a855f7";
  if (m.includes("sonnet")) return "#3b82f6";
  if (m.includes("haiku")) return "#22c55e";
  if (m.includes("gpt-5.4") || m.includes("gpt-5.2")) return "#f472b6";
  if (m.includes("gpt-5")) return "#fb923c";
  if (m.includes("gpt-4")) return "#c084fc";
  if (m.includes("gemini")) return "#34d399";
  if (m.includes("deepseek")) return "#60a5fa";
  if (m.includes("qwen")) return "#fbbf24";
  if (m.includes("local")) return "#6b7280";
  return "#8b8fa0";
}

function qualityBand(score: number): { label: string; color: string; grade: string } {
  const grade = scoreToGrade(score);
  if (score >= 80) return { label: "Good", color: "var(--c-success)", grade };
  if (score >= 60) return { label: "Fair", color: "var(--c-warning)", grade };
  if (score >= 40) return { label: "Needs Work", color: "#fb923c", grade };
  return { label: "Poor", color: "var(--c-danger)", grade };
}

// ---------------------------------------------------------------------------
// HTML sections
// ---------------------------------------------------------------------------

function renderNav(data: DashboardData): string {
  const wasteCount = data.waste.length;
  const sc = data.severityCounts;
  const wasteBadge = wasteCount > 0
    ? `<span class="nav-badge">${
        (sc.critical > 0 ? `<span style="color:var(--c-danger)">${sc.critical}</span>/` : "") +
        (sc.high > 0 ? `<span style="color:var(--c-warning)">${sc.high}</span>/` : "") +
        `<span style="color:var(--c-accent-cyan)">${sc.medium + sc.low}</span>`
      }</span>`
    : "";

  return `<nav class="nav-col">
    <div>
      <div class="brand"><span></span> Token Optimizer</div>
      <div class="nav-menu">
        <a class="nav-item active" data-view="overview">Overview</a>
        <a class="nav-item" data-view="context">Context</a>
        <a class="nav-item" data-view="quality">Quality</a>
        <a class="nav-item" data-view="waste">Waste ${wasteBadge}</a>
        <a class="nav-item" data-view="agents">Agents</a>
        <a class="nav-item" data-view="sessions">Sessions</a>
        <a class="nav-item" data-view="daily">Daily</a>
        <div style="height:1px;background:var(--c-border);margin:var(--s-2) 0"></div>
        <a class="nav-item" data-view="manage">Manage</a>
      </div>
    </div>
    <div class="user-profile">generated: <i>${esc(data.generatedAt.slice(0, 16).replace("T", " "))}</i></div>
  </nav>`;
}

function renderOverview(data: DashboardData): string {
  const o = data.overview;
  const costDisplay = o.allCostZero ? "Unknown" : fmtCost(o.totalCost);
  const costQualifier = o.unknownModelRuns > 0 && !o.allCostZero
    ? `<div class="stat-card-qualifier">excludes ${o.unknownModelRuns} unknown-model runs</div>`
    : "";
  const qualityScore = data.quality
    ? `<div class="stat-card">
        <div class="stat-card-value" style="color:${qualityBand(data.quality.score).color}">${qualityBand(data.quality.score).grade}</div>
        <div class="stat-card-label">Quality Score</div>
        <div class="stat-card-qualifier">${data.quality.score}/100 (${qualityBand(data.quality.score).label})</div>
      </div>`
    : "";

  return `<div class="view active" id="view-overview">
    <div class="section-header">
      <div class="label">OpenClaw Token Audit</div>
      <h1>Overview</h1>
      <p>Agent token usage, cost analysis, and waste detection for your OpenClaw setup.</p>
    </div>

    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-card-value">${fmtNumber(o.totalRuns)}</div>
        <div class="stat-card-label">Total Runs</div>
      </div>
      <div class="stat-card">
        <div class="stat-card-value">${esc(costDisplay)}</div>
        <div class="stat-card-label">Total Cost</div>
        ${costQualifier}
      </div>
      ${qualityScore}
      <div class="stat-card">
        <div class="stat-card-value" style="color:var(--c-success)">${fmtCost(o.monthlySavings)}/mo</div>
        <div class="stat-card-label">Potential Savings</div>
      </div>
    </div>

    ${data.context ? renderContextOverviewBar(data.context, data) : ""}

    ${data.agents.length > 0 ? renderAgentCards(data.agents.slice(0, 6)) : ""}

    ${data.waste.length > 0 ? `
      <div class="card">
        <div class="card-header"><span>Top Waste Patterns</span></div>
        ${data.waste.slice(0, 3).map(renderWasteCardCompact).join("")}
      </div>
    ` : ""}
  </div>`;
}

function renderContextOverviewBar(ctx: ContextAudit, data: DashboardData): string {
  const total = ctx.totalOverhead;
  if (total === 0) return "";

  const comps = ctx.components.slice(0, 5);
  const ctxW = data.contextWindow;

  return `<div class="card">
    <div class="card-header"><span>Context Overhead</span><span style="color:var(--c-text-dim);font-family:var(--font-mono);font-size:13px">${fmtTokens(total)} of ${fmtTokens(ctxW)} (${((total / ctxW) * 100).toFixed(1)}%)</span></div>
    ${comps.map((c) => {
      const barW = logBarPct(c.tokens, ctxW);
      return `<div class="bar-row">
        <span class="bar-row-label">${esc(c.name)}</span>
        <div class="bar-row-track"><div class="bar-row-fill" style="width:${barW}%"></div></div>
        <span class="bar-row-value">${fmtTokens(c.tokens)}</span>
      </div>`;
    }).join("")}
  </div>`;
}

function renderAgentCards(agents: AgentSummary[]): string {
  return `<div class="dashboard-grid">
    ${agents.map((a) => `<div class="card">
      <div class="card-header"><span>${esc(a.name.length > 30 ? a.name.slice(0, 30) + "..." : a.name)}</span><span style="color:var(--c-accent-cyan);font-family:var(--font-mono);font-size:14px">${fmtCost(a.cost)}</span></div>
      <div class="mini-stats">
        <div class="mini-stat-item"><div class="mini-val">${a.runs}</div><div class="mini-label">runs</div></div>
        <div class="mini-stat-item"><div class="mini-val">${fmtDuration(a.avgDuration)}</div><div class="mini-label">avg</div></div>
        <div class="mini-stat-item"><div class="mini-val">${a.emptyPct.toFixed(0)}%</div><div class="mini-label">empty</div></div>
        <div class="mini-stat-item"><div class="mini-val">${esc(a.dominantModel)}</div><div class="mini-label">model</div></div>
      </div>
    </div>`).join("")}
  </div>`;
}

function renderContext(data: DashboardData): string {
  const ctx = data.context;
  if (!ctx) {
    return `<div class="view" id="view-context">
      <div class="section-header">
        <div class="label">Context Analysis</div>
        <h1>Context</h1>
        <p>Per-component token overhead injected into every API call.</p>
      </div>
      <div class="empty-state">No OpenClaw config found. Install the plugin and run an audit first.</div>
    </div>`;
  }

  const ctxW = data.contextWindow;
  const activeSkills = ctx.skills.filter((s) => !s.isArchived);
  const activeMcp = ctx.mcpServers.filter((s) => !s.isDisabled);
  const overheadPct = ((ctx.totalOverhead / ctxW) * 100).toFixed(1);

  return `<div class="view" id="view-context">
    <div class="section-header">
      <div class="label">Context Analysis</div>
      <h1>Context</h1>
      <p>Per-component token overhead injected into every API call. Bars show % of ${fmtTokens(ctxW)} context window.</p>
    </div>

    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-card-value">${overheadPct}%</div>
        <div class="stat-card-label">Context Used</div>
        <div class="stat-card-qualifier">${fmtTokens(ctx.totalOverhead)} of ${fmtTokens(ctxW)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-card-value">${activeSkills.length}</div>
        <div class="stat-card-label">Active Skills</div>
        <div class="stat-card-qualifier">${fmtTokens(activeSkills.reduce((s, sk) => s + sk.tokens, 0))} tokens</div>
      </div>
      <div class="stat-card">
        <div class="stat-card-value">${activeMcp.length}</div>
        <div class="stat-card-label">MCP Servers</div>
      </div>
      <div class="stat-card">
        <div class="stat-card-value">${ctx.components.length}</div>
        <div class="stat-card-label">Components</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span>Token Breakdown by Component</span><span class="label">% of context window</span></div>
      ${ctx.components.map((c) => {
        const barW = logBarPct(c.tokens, ctxW);
        const pctOfWindow = ((c.tokens / ctxW) * 100).toFixed(1);
        return `<div class="bar-row">
          <span class="bar-row-label">${esc(c.name)}</span>
          <div class="bar-row-track"><div class="bar-row-fill" style="width:${barW}%"></div></div>
          <span class="bar-row-value">${fmtTokens(c.tokens)} <span style="color:var(--c-text-dim);font-size:11px">(${pctOfWindow}%)</span></span>
        </div>`;
      }).join("")}
    </div>

    ${activeSkills.length > 0 ? `
      <div class="card">
        <div class="card-header"><span>Skills (${activeSkills.length} active)</span><span class="label">${fmtTokens(activeSkills.reduce((s, sk) => s + sk.tokens, 0))} total</span></div>
        ${activeSkills.map((sk) => {
          const barW = logBarPct(sk.tokens, ctxW);
          return `<div class="bar-row">
            <span class="bar-row-label" title="${esc(sk.description)}">${esc(sk.name)}</span>
            <div class="bar-row-track"><div class="bar-row-fill" style="width:${barW}%"></div></div>
            <span class="bar-row-value">${sk.tokens} tok</span>
          </div>`;
        }).join("")}
      </div>
    ` : ""}

    ${activeMcp.length > 0 ? `
      <div class="card">
        <div class="card-header"><span>MCP Servers (${activeMcp.length} active)</span></div>
        ${activeMcp.map((srv) => `<div style="display:flex;align-items:center;gap:var(--s-2);padding:6px 0;border-bottom:1px solid var(--c-border)">
          <span style="font-size:13px;font-family:var(--font-mono);color:var(--c-text-main);flex:1">${esc(srv.name)}</span>
          <span style="font-size:12px;font-family:var(--font-mono);color:var(--c-text-dim)">${srv.toolCount > 0 ? srv.toolCount + " tools" : ""}</span>
        </div>`).join("")}
      </div>
    ` : ""}

    ${ctx.recommendations.length > 0 ? `
      <div class="card">
        <div class="card-header"><span>Recommendations</span></div>
        ${ctx.recommendations.map((r) => `<div class="rec-item">${esc(r)}</div>`).join("")}
      </div>
    ` : ""}
  </div>`;
}

function renderQuality(data: DashboardData): string {
  const q = data.quality;
  if (!q) {
    return `<div class="view" id="view-quality">
      <div class="section-header">
        <div class="label">Quality Assessment</div>
        <h1>Quality</h1>
        <p>Multi-signal quality scoring for your OpenClaw usage patterns.</p>
      </div>
      <div class="empty-state">Run an audit with quality scoring enabled to see results here.</div>
    </div>`;
  }

  const band = qualityBand(q.score);

  return `<div class="view" id="view-quality">
    <div class="section-header">
      <div class="label">Quality Assessment</div>
      <h1>Quality</h1>
      <p>Multi-signal quality scoring for your OpenClaw usage patterns.</p>
    </div>

    <div style="text-align:center;margin:var(--s-4) 0">
      <div style="font-family:var(--font-mono);font-size:72px;font-weight:500;color:${band.color};text-shadow:0 0 20px ${band.color}">${band.grade}</div>
      <div style="font-size:28px;font-weight:500;font-family:var(--font-mono);color:${band.color}">${q.score}/100</div>
      <div style="font-size:16px;color:${band.color};text-transform:uppercase;letter-spacing:0.2em">${esc(band.label)}</div>
    </div>

    <div class="card">
      <div class="card-header"><span>Signal Breakdown</span></div>
      ${q.signals.map((sig) => {
        const barColor = sig.score >= 70 ? "var(--c-success)" : sig.score >= 40 ? "var(--c-warning)" : "var(--c-danger)";
        return `<div class="bar-row" style="margin-bottom:var(--s-3)">
          <span class="bar-row-label" style="min-width:180px">${esc(sig.name)} <span style="color:var(--c-text-dim);font-size:11px">(${(sig.weight * 100).toFixed(0)}%)</span></span>
          <div class="bar-row-track"><div class="bar-row-fill" style="width:${sig.score}%;background:${barColor}"></div></div>
          <span class="bar-row-value">${sig.score}</span>
        </div>
        <div style="font-size:13px;color:var(--c-text-dim);margin:-8px 0 var(--s-3) 0;padding-left:188px">${esc(sig.description)}</div>`;
      }).join("")}
    </div>

    ${q.recommendations.length > 0 ? `
      <div class="card">
        <div class="card-header"><span>Recommendations</span></div>
        ${q.recommendations.map((r) => `<div class="rec-item">${esc(r)}</div>`).join("")}
      </div>
    ` : ""}
  </div>`;
}

function renderWasteCardCompact(f: WasteFinding): string {
  return `<div style="padding:var(--s-2) 0;border-bottom:1px solid var(--c-border)">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div><span class="waste-severity ${f.severity}">${f.severity}</span>
      <span style="font-size:13px;color:var(--c-text-dim)">${esc(f.agentName || "all agents")}</span></div>
      ${f.monthlyWasteUsd > 0 ? `<span class="waste-savings">${fmtCost(f.monthlyWasteUsd)}/mo</span>` : ""}
    </div>
    <div style="font-size:14px;margin-top:4px">${esc(f.description)}</div>
  </div>`;
}

function renderWaste(data: DashboardData): string {
  if (data.waste.length === 0) {
    return `<div class="view" id="view-waste">
      <div class="section-header">
        <div class="label">Waste Detection</div>
        <h1>Waste</h1>
        <p>Token and cost waste patterns detected across your OpenClaw agents.</p>
      </div>
      <div class="empty-state">No waste patterns detected. Your setup looks clean.</div>
    </div>`;
  }

  return `<div class="view" id="view-waste">
    <div class="section-header">
      <div class="label">Waste Detection</div>
      <h1>Waste</h1>
      <p>Token and cost waste patterns detected across your OpenClaw agents.</p>
    </div>

    ${data.waste.map((f) => {
      const escapedSnippet = esc(f.fixSnippet).replace(/\n/g, "&#10;");
      return `<div class="waste-card ${f.severity}">
        <div class="waste-header">
          <div>
            <span class="waste-severity ${f.severity}">${f.severity}</span>
            <span class="waste-confidence">${(f.confidence * 100).toFixed(0)}%</span>
            <span class="waste-system">${esc(f.agentName || "all agents")}</span>
          </div>
          ${f.monthlyWasteUsd > 0 ? `<span class="waste-savings">${fmtCost(f.monthlyWasteUsd)}/mo</span>` : ""}
        </div>
        <div class="waste-desc">${esc(f.description)}</div>
        <div class="waste-rec">${esc(f.recommendation)}</div>
        <div class="waste-fix-container">
          <div class="waste-fix">${esc(f.fixSnippet)}</div>
          <button class="copy-fix-btn" data-snippet="${escapedSnippet}">Copy Fix</button>
        </div>
      </div>`;
    }).join("")}
  </div>`;
}

function renderAgents(data: DashboardData): string {
  if (data.agents.length === 0) {
    return `<div class="view" id="view-agents">
      <div class="section-header">
        <div class="label">Agent Analysis</div>
        <h1>Agents</h1>
        <p>Per-agent breakdown of cost, tokens, and model usage.</p>
      </div>
      <div class="empty-state">No agent sessions found.</div>
    </div>`;
  }

  return `<div class="view" id="view-agents">
    <div class="section-header">
      <div class="label">Agent Analysis</div>
      <h1>Agents</h1>
      <p>Per-agent breakdown of cost, tokens, and model usage.</p>
    </div>

    ${data.agents.map((a) => {
      const modelEntries = Object.entries(a.models).sort((x, y) => y[1] - x[1]);
      const multiModel = modelEntries.length > 1;

      // Only show stacked bar when there are multiple models to compare
      let modelMixHtml = "";
      if (multiModel) {
        const totalModelCost = Object.values(a.models).reduce((s, c) => s + c, 0) || 1;
        const segments = modelEntries.map(([m, c]) => {
          const pct = (c / totalModelCost) * 100;
          const pctStr = pct.toFixed(1);
          const color = modelColor(m);
          const label = pct >= 8 ? `<span class="segment-label">${Math.round(pct)}%</span>` : "";
          return `<div class="model-segment" style="width:${pctStr}%;background:${color};position:relative;overflow:hidden" data-tt-model="${esc(m)}" data-tt-pct="${pctStr}" data-tt-cost="${fmtCost(c)}">${label}</div>`;
        }).join("");
        const legend = modelEntries
          .map(([m]) => `<div class="model-legend-item"><div class="model-legend-dot" style="background:${modelColor(m)}"></div>${esc(m)}</div>`)
          .join("");
        modelMixHtml = `<div class="model-bar">${segments}</div><div class="model-legend">${legend}</div>`;
      }

      return `<div class="card">
        <div class="card-header">
          <span>${esc(a.name.length > 40 ? a.name.slice(0, 40) + "..." : a.name)}</span>
          <span style="color:var(--c-accent-cyan);font-family:var(--font-mono);font-size:16px">${fmtCost(a.cost)}</span>
        </div>
        <div class="mini-stats">
          <div class="mini-stat-item"><div class="mini-val">${a.runs}</div><div class="mini-label">runs</div></div>
          <div class="mini-stat-item"><div class="mini-val">${fmtDuration(a.avgDuration)}</div><div class="mini-label">avg duration</div></div>
          <div class="mini-stat-item"><div class="mini-val">${a.emptyPct.toFixed(0)}%</div><div class="mini-label">empty</div></div>
          <div class="mini-stat-item"><div class="mini-val">${a.abandonedCount}</div><div class="mini-label">abandoned</div></div>
          <div class="mini-stat-item"><div class="mini-val">${fmtTokens(a.tokens)}</div><div class="mini-label">tokens</div></div>
          <div class="mini-stat-item"><div class="mini-val">${esc(a.dominantModel)}</div><div class="mini-label">model</div></div>
        </div>
        ${modelMixHtml}
      </div>`;
    }).join("")}

    <div class="card">
      <div class="card-header"><span>Top Agents by Cost</span></div>
      ${data.agents.slice(0, 10).map((a) => `<div class="proj-row">
        <div class="proj-name">${esc(a.name.length > 30 ? a.name.slice(0, 30) + "..." : a.name)}</div>
        <div class="proj-stat">${a.runs} runs</div>
        <div class="proj-stat">${fmtTokens(a.tokens)}</div>
        <div class="proj-cost">${fmtCost(a.cost)}</div>
      </div>`).join("")}
    </div>
  </div>`;
}

function renderDaily(data: DashboardData): string {
  const d = data.daily;
  if (d.length === 0) {
    return `<div class="view" id="view-daily">
      <div class="section-header">
        <div class="label">Daily Trends</div>
        <h1>Daily</h1>
        <p>Day-by-day cost and usage trends.</p>
      </div>
      <div class="empty-state">No daily data available.</div>
    </div>`;
  }

  const useCost = !data.overview.allCostZero;
  const maxCost = Math.max(...d.map((b) => b.cost), 0.01);
  const maxRuns = Math.max(...d.map((b) => b.runs), 1);
  const maxTokens = Math.max(...d.map((b) => b.tokens), 1);
  const firstDate = d[0].date;
  const lastDate = d[d.length - 1].date;

  const costChartData = d.map((b) => ({
    date: b.date,
    value: useCost ? b.cost : b.tokens,
    runs: b.runs,
  }));
  const maxVal = useCost ? maxCost : maxTokens;

  return `<div class="view" id="view-daily">
    <div class="section-header">
      <div class="label">Daily Trends</div>
      <h1>Daily</h1>
      <p>Day-by-day ${useCost ? "cost" : "token usage"} and run count trends.</p>
    </div>

    <div class="card">
      <div class="card-header"><span>Daily ${useCost ? "Cost" : "Tokens"}</span></div>
      <div class="chart-with-axis">
        <div class="y-axis" id="daily-cost-y">
          <div class="y-axis-label">${useCost ? fmtCost(maxVal) : fmtTokens(maxVal)}</div>
          <div class="y-axis-label">${useCost ? fmtCost(maxVal * 0.66) : fmtTokens(maxVal * 0.66)}</div>
          <div class="y-axis-label">${useCost ? fmtCost(maxVal * 0.33) : fmtTokens(maxVal * 0.33)}</div>
          <div class="y-axis-label">0</div>
        </div>
        <div class="chart-area">
          <div class="y-grid-lines">
            <div class="y-grid-line" style="bottom:100%"></div>
            <div class="y-grid-line" style="bottom:66%"></div>
            <div class="y-grid-line" style="bottom:33%"></div>
          </div>
          <div class="bar-chart" id="daily-cost-chart">
            ${costChartData.map((b) => {
              const h = maxVal > 0 ? (b.value / maxVal) * 100 : 0;
              return `<div class="bar" style="height:${Math.max(h, 1)}%" data-tt-date="${b.date}" data-tt-val="${useCost ? fmtCost(b.value) : fmtTokens(b.value)}" data-tt-runs="${b.runs}"></div>`;
            }).join("")}
          </div>
          <div class="bar-label"><span>${esc(firstDate)}</span><span>${esc(lastDate)}</span></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span>Daily Runs</span></div>
      <div class="chart-with-axis">
        <div class="y-axis">
          <div class="y-axis-label">${maxRuns}</div>
          <div class="y-axis-label">${Math.round(maxRuns * 0.66)}</div>
          <div class="y-axis-label">${Math.round(maxRuns * 0.33)}</div>
          <div class="y-axis-label">0</div>
        </div>
        <div class="chart-area">
          <div class="y-grid-lines">
            <div class="y-grid-line" style="bottom:100%"></div>
            <div class="y-grid-line" style="bottom:66%"></div>
            <div class="y-grid-line" style="bottom:33%"></div>
          </div>
          <div class="bar-chart" id="daily-runs-chart">
            ${d.map((b) => {
              const h = maxRuns > 0 ? (b.runs / maxRuns) * 100 : 0;
              return `<div class="bar bar-secondary" style="height:${Math.max(h, 1)}%" data-tt-date="${b.date}" data-tt-val="${b.runs} runs" data-tt-runs="${b.runs}"></div>`;
            }).join("")}
          </div>
          <div class="bar-label"><span>${esc(firstDate)}</span><span>${esc(lastDate)}</span></div>
        </div>
      </div>
    </div>
  </div>`;
}

function renderSessions(data: DashboardData): string {
  const sessions = data.sessions;
  if (sessions.length === 0) {
    return `<div class="view" id="view-sessions">
      <div class="section-header">
        <div class="label">Session History</div>
        <h1>Sessions</h1>
        <p>Individual session log with cost, tokens, and outcome.</p>
      </div>
      <div class="empty-state">No sessions found.</div>
    </div>`;
  }

  // Group by date
  const byDate = new Map<string, SessionRow[]>();
  for (const s of sessions) {
    const list = byDate.get(s.date) ?? [];
    list.push(s);
    byDate.set(s.date, list);
  }

  const outcomeColor = (o: string): string => {
    switch (o) {
      case "success": return "var(--c-success)";
      case "abandoned": return "var(--c-warning)";
      case "empty": return "var(--c-danger)";
      case "failure": return "var(--c-danger)";
      default: return "var(--c-text-dim)";
    }
  };
  const ttlMixLabel = (r: SessionRow): string => {
    const ttlTotal = r.cacheWrite1hTokens + r.cacheWrite5mTokens;
    if (ttlTotal <= 0) return "n/a";
    const pct1h = Math.round((r.cacheWrite1hTokens / ttlTotal) * 100);
    const pct5m = Math.max(0, 100 - pct1h);
    return `${pct1h}/${pct5m} 1h/5m`;
  };

  return `<div class="view" id="view-sessions">
    <div class="section-header">
      <div class="label">Session History</div>
      <h1>Sessions</h1>
      <p>${sessions.length} sessions (most recent ${data.daysScanned} days)</p>
    </div>

    ${Array.from(byDate.entries()).map(([date, rows]) => {
      const dayCost = rows.reduce((s, r) => s + r.cost, 0);
      const dayTokens = rows.reduce((s, r) => s + r.tokens, 0);
      return `<div class="card">
        <div class="card-header">
          <span>${esc(date)}</span>
          <span style="font-family:var(--font-mono);font-size:13px;color:var(--c-text-dim)">${rows.length} sessions, ${fmtCost(dayCost)}, ${fmtTokens(dayTokens)}</span>
        </div>
        <div style="overflow-x:auto">
          <table class="session-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Model</th>
                <th>Messages</th>
                <th>Tokens</th>
                <th>TTL Mix</th>
                <th>Cost</th>
                <th>Duration</th>
                <th>Outcome</th>
                <th>Quality</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map((r) => {
                const qBorderStyle = r.qualityScore >= 80
                  ? "border-left:4px solid var(--c-success)"
                  : r.qualityScore >= 60
                  ? ""
                  : r.qualityScore >= 40
                  ? "border-left:4px solid var(--c-warning)"
                  : "border-left:4px solid var(--c-danger)";
                return `<tr style="${qBorderStyle}">
                <td>${esc(r.agentName.length > 20 ? r.agentName.slice(0, 20) + "..." : r.agentName)}</td>
                <td>${esc(r.model)}</td>
                <td>${r.messages}</td>
                <td>${fmtTokens(r.tokens)}</td>
                <td style="font-family:var(--font-mono);font-size:11px;color:var(--c-text-dim)">${esc(ttlMixLabel(r))}</td>
                <td style="color:var(--c-accent-cyan)">${fmtCost(r.cost)}</td>
                <td>${fmtDuration(r.duration)}</td>
                <td><span style="color:${outcomeColor(r.outcome)}">${esc(r.outcome)}</span></td>
                <td><span style="color:${qualityBand(r.qualityScore).color}">${r.qualityGrade} (${r.qualityScore})</span></td>
              </tr>`;
              }).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
    }).join("")}
  </div>`;
}

function renderManage(data: DashboardData): string {
  const ctx = data.context;
  if (!ctx) {
    return `<div class="view" id="view-manage">
      <div class="section-header">
        <div class="label">Control Panel</div>
        <h1>Manage</h1>
        <p>Toggle skills and MCP servers on/off. Commands are copied to clipboard for you to paste into your agent chat.</p>
      </div>
      <div class="empty-state">No OpenClaw config found.</div>
    </div>`;
  }

  const manage = ctx.manage;
  const activeSkills = manage.skills.active;
  const archivedSkills = manage.skills.archived;
  const activeMcp = manage.mcpServers.active;
  const disabledMcp = manage.mcpServers.disabled;
  const totalSkillTokens = activeSkills.reduce((s, sk) => s + sk.tokens, 0);

  return `<div class="view" id="view-manage">
    <div class="section-header">
      <div class="label">Control Panel</div>
      <h1>Manage</h1>
    </div>

    <div class="manage-banner">
      <div class="manage-banner-icon">&#x2398;</div>
      <div>
        <div class="manage-banner-title">Toggle → Copy → Paste into agent chat</div>
        <div class="manage-banner-desc">Flipping a toggle copies a command to your clipboard. Paste it into your OpenClaw agent chat and the agent will apply it.</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span>Active Skills (${activeSkills.length})</span><span class="label">${fmtTokens(totalSkillTokens)} tokens/session</span></div>
      ${activeSkills.length === 0 ? '<div class="empty-state" style="padding:var(--s-3)">No skills installed.</div>' : `
        <div style="max-height:500px;overflow-y:auto">
          ${activeSkills.map((sk) => `<div class="manage-row">
            <label class="manage-toggle">
              <input type="checkbox" checked data-manage-cmd="mv ~/.openclaw/skills/${esc(shellEsc(sk.name))} ~/.openclaw/skills/_archived/${esc(shellEsc(sk.name))}" data-manage-name="${esc(sk.name)}">
              <span class="manage-slider"></span>
            </label>
            <div class="manage-info">
              <div class="manage-label">${esc(sk.name)}</div>
              <div class="manage-desc">${esc(sk.description)}</div>
              <div style="font-size:11px;color:var(--c-text-dim);margin-top:2px">${sk.tokens} tokens</div>
            </div>
          </div>`).join("")}
        </div>
      `}
    </div>

    <div class="card">
      <div class="card-header"><span>Archived Skills (${archivedSkills.length})</span><span class="label">inactive, zero overhead</span></div>
      ${archivedSkills.length === 0 ? '<div class="empty-state" style="padding:var(--s-3);font-size:13px">Nothing archived yet. Toggle an active skill off to move it here.</div>' : `
        <div style="max-height:400px;overflow-y:auto">
          ${archivedSkills.map((sk) => `<div class="manage-row" style="opacity:0.7">
            <label class="manage-toggle">
              <input type="checkbox" data-manage-cmd="mv ~/.openclaw/skills/_archived/${esc(shellEsc(sk.name))} ~/.openclaw/skills/${esc(shellEsc(sk.name))}" data-manage-name="${esc(sk.name)}">
              <span class="manage-slider"></span>
            </label>
            <div class="manage-info">
              <div class="manage-label">${esc(sk.name)}</div>
              <div class="manage-desc">${esc(sk.description)}</div>
            </div>
          </div>`).join("")}
        </div>
      `}
    </div>

    ${activeMcp.length > 0 || disabledMcp.length > 0 ? `
      <div class="card">
        <div class="card-header"><span>Active MCP Servers (${activeMcp.length})</span></div>
        ${activeMcp.length === 0 ? '<div class="empty-state" style="padding:var(--s-3)">No MCP servers configured.</div>' : `
          ${activeMcp.map((srv) => `<div class="manage-row">
            <label class="manage-toggle">
              <input type="checkbox" checked data-manage-cmd="# Disable ${esc(srv.name)} in config.json" data-manage-name="${esc(srv.name)}">
              <span class="manage-slider"></span>
            </label>
            <div class="manage-info">
              <div class="manage-label">${esc(srv.name)}</div>
              <div style="font-size:11px;color:var(--c-text-dim);margin-top:2px">${esc(srv.command)}${srv.toolCount > 0 ? ` (${srv.toolCount} tools)` : ""}</div>
            </div>
          </div>`).join("")}
        `}
      </div>

      ${disabledMcp.length > 0 ? `
        <div class="card">
          <div class="card-header"><span>Disabled MCP Servers (${disabledMcp.length})</span><span class="label">inactive</span></div>
          ${disabledMcp.map((srv) => `<div class="manage-row" style="opacity:0.7">
            <label class="manage-toggle">
              <input type="checkbox" data-manage-cmd="# Enable ${esc(srv.name)} in config.json" data-manage-name="${esc(srv.name)}">
              <span class="manage-slider"></span>
            </label>
            <div class="manage-info">
              <div class="manage-label">${esc(srv.name)}</div>
            </div>
          </div>`).join("")}
        </div>
      ` : ""}
    ` : ""}

  </div>`;
}

function renderSidebar(data: DashboardData): string {
  const o = data.overview;
  const sc = data.severityCounts;

  return `<aside class="config-col">
    <div class="section-title">Fleet Summary</div>
    <div class="summary-metric"><span class="num">${fmtNumber(o.totalRuns)}</span> runs</div>
    <div class="summary-metric"><span class="num">${o.allCostZero ? "?" : fmtCost(o.totalCost)}</span> total</div>
    <div class="summary-metric"><span class="num">${o.activeDays}</span> active days</div>
    <div class="summary-metric"><span class="num">${fmtTokens(o.totalTokens)}</span> tokens</div>

    <div class="section-title">Waste Breakdown</div>
    <div class="summary-metric" style="font-size:14px">
      ${sc.critical > 0 ? `<div style="margin-bottom:4px"><span style="color:var(--c-danger)">${sc.critical}</span> critical</div>` : ""}
      ${sc.high > 0 ? `<div style="margin-bottom:4px"><span style="color:var(--c-warning)">${sc.high}</span> high</div>` : ""}
      ${sc.medium > 0 ? `<div style="margin-bottom:4px"><span style="color:var(--c-accent-cyan)">${sc.medium}</span> medium</div>` : ""}
      ${sc.low > 0 ? `<div style="margin-bottom:4px"><span style="color:var(--c-text-dim)">${sc.low}</span> low</div>` : ""}
      ${o.wasteCount === 0 ? `<div style="color:var(--c-success)">Clean</div>` : ""}
    </div>
    <div class="summary-metric">Savings: <span class="num">${fmtCost(o.monthlySavings)}/mo</span></div>

    <div class="section-title">Quick Commands</div>
    <div style="font-family:var(--font-mono);font-size:12px;line-height:2">
      <div class="quick-cmd">npx token-optimizer scan</div>
      <div class="quick-cmd">npx token-optimizer audit</div>
      <div class="quick-cmd">npx token-optimizer context</div>
      <div class="quick-cmd">npx token-optimizer quality</div>
      <div class="quick-cmd">npx token-optimizer drift</div>
      <div class="quick-cmd">npx token-optimizer dashboard</div>
    </div>

    <div class="version-footer">
      <div>Built by <a href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener">Alex Greenshpun</a></div>
      <div class="social-icons">
        <a class="social-link" href="https://github.com/alexgreensh/token-optimizer" target="_blank" rel="noopener">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.08-.73.08-.73 1.2.08 1.84 1.23 1.84 1.23 1.07 1.83 2.81 1.3 3.5 1 .1-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 016.02 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.65 4.65 0 011.23 3.22c0 4.61-2.81 5.63-5.48 5.92.42.36.81 1.1.81 2.22v3.29c0 .32.22.7.82.58A12.01 12.01 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
        </a>
        <a class="social-link" href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.34V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.46v6.28zM5.34 7.43a2.06 2.06 0 110-4.13 2.06 2.06 0 010 4.13zm1.78 13.02H3.56V9h3.56v11.45zM22.23 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.23 0z"/></svg>
        </a>
      </div>
    </div>
  </aside>`;
}

// ---------------------------------------------------------------------------
// Full HTML document
// ---------------------------------------------------------------------------

function renderCSS(): string {
  return `*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; outline: none; }
:root {
  --c-bg: #0a0b10;
  --c-surface: #13151a;
  --c-surface-hover: #1c1f26;
  --c-accent-cyan: #00f0ff;
  --c-accent-blue: #0066ff;
  --c-accent-glow: rgba(0, 240, 255, 0.4);
  --c-text-main: #ffffff;
  --c-text-dim: #7d8ca3;
  --c-border: rgba(255, 255, 255, 0.08);
  --c-success: #22c55e;
  --c-warning: #f59e0b;
  --c-danger: #ef4444;
  --font-sans: 'Space Grotesk', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --s-1: 4px; --s-2: 8px; --s-3: 16px; --s-4: 24px; --s-5: 32px; --s-6: 64px;
  --glow-sm: 0 0 10px var(--c-accent-glow);
  --glow-text: 0 0 8px rgba(0, 240, 255, 0.6);
}
body {
  background-color: var(--c-bg);
  color: var(--c-text-main);
  font-family: var(--font-sans);
  font-weight: 300;
  font-size: 18px;
  line-height: 1.5;
  min-height: 100vh;
  overflow-x: hidden;
  -webkit-font-smoothing: antialiased;
  background-image: radial-gradient(circle at 50% 0%, #1a253a 0%, var(--c-bg) 60%);
}
body::before {
  content: "";
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  background-image:
    linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
  z-index: 0;
}
h1, h2, h3, h4 { font-weight: 400; }
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--c-bg); }
::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #555; }

.layout {
  display: grid;
  grid-template-columns: 260px 1fr 340px;
  height: 100vh;
  width: 100vw;
  max-width: 1800px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}

/* NAV */
.nav-col {
  padding: var(--s-4);
  border-right: 1px solid var(--c-border);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: linear-gradient(90deg, transparent, rgba(0,0,0,0.2));
  backdrop-filter: blur(10px);
}
.brand {
  font-family: var(--font-sans);
  font-weight: 700;
  font-size: 24px;
  margin-bottom: var(--s-5);
  display: flex;
  align-items: center;
  gap: var(--s-2);
  color: var(--c-text-main);
  letter-spacing: -0.02em;
}
.brand span {
  width: 12px; height: 12px;
  background: var(--c-accent-cyan);
  border-radius: 2px;
  display: inline-block;
  box-shadow: var(--glow-sm);
}
.nav-menu { display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  padding: 12px var(--s-2);
  cursor: pointer;
  opacity: 0.6;
  transition: all 0.3s cubic-bezier(0.2, 0.8, 0.2, 1);
  text-decoration: none;
  color: var(--c-text-main);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: var(--font-sans);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-size: 15px;
  border-left: 2px solid transparent;
}
.nav-item:hover, .nav-item.active {
  opacity: 1;
  background: linear-gradient(90deg, rgba(0,240,255,0.05), transparent);
  border-left-color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}
.nav-item.active { font-weight: 600; }
.nav-badge {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--c-accent-cyan);
  border: 1px solid var(--c-accent-cyan);
  padding: 0 4px;
  border-radius: 2px;
  text-shadow: 0 0 5px var(--c-accent-cyan);
}
.user-profile {
  font-size: 15px;
  color: var(--c-text-dim);
  border-top: 1px solid var(--c-border);
  padding-top: var(--s-3);
  font-family: var(--font-mono);
}
.user-profile i { font-style: italic; color: var(--c-accent-cyan); }

/* MAIN */
.main-col {
  padding: var(--s-5);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--s-5);
}
.view { display: none; }
.view.active { display: flex; flex-direction: column; gap: var(--s-5); }
.section-header { margin-bottom: var(--s-2); }
.label {
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  font-weight: 500;
}
.section-header h1 {
  font-family: var(--font-sans);
  font-weight: 300;
  font-size: 48px;
  margin: var(--s-2) 0;
  line-height: 1;
  letter-spacing: -0.02em;
  background: linear-gradient(180deg, #fff, #aaa);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.section-header p {
  font-size: 12px;
  color: var(--c-text-dim);
  max-width: 460px;
  line-height: 1.6;
  font-family: var(--font-mono);
}

/* CARDS */
.card {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: var(--s-4);
  transition: border-color 0.3s;
}
.card:hover { border-color: rgba(255,255,255,0.15); }
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--s-3);
  font-size: 15px;
}
.card-header span:first-child { font-weight: 500; }
.dashboard-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--s-3);
}

/* STAT CARDS */
.stat-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--s-3);
  margin-bottom: var(--s-4);
}
.stat-card {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: var(--s-4);
  text-align: center;
  position: relative;
  overflow: hidden;
  transition: border-color 0.3s;
}
.stat-card:hover { border-color: rgba(0, 240, 255, 0.2); }
.stat-card::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--c-accent-cyan), transparent);
  opacity: 0.3;
}
.stat-card-value {
  font-family: var(--font-mono);
  font-size: 32px;
  font-weight: 500;
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}
.stat-card-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--c-text-dim);
  margin-top: var(--s-1);
}
.stat-card-qualifier {
  font-size: 10px;
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  margin-top: 2px;
  opacity: 0.6;
}

/* MINI STATS */
.mini-stats {
  display: flex;
  gap: var(--s-4);
  margin-top: var(--s-3);
  padding-top: var(--s-3);
  border-top: 1px solid var(--c-border);
  flex-wrap: wrap;
}
.mini-stat-item { display: flex; flex-direction: column; }
.mini-val { font-family: var(--font-mono); font-size: 16px; color: var(--c-text-main); }
.mini-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--c-text-dim); }

/* WASTE */
.waste-card {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: var(--s-4);
  margin-bottom: var(--s-3);
  border-left: 3px solid var(--c-text-dim);
  transition: border-color 0.3s, transform 0.2s;
}
.waste-card:hover { transform: translateY(-1px); border-color: rgba(255,255,255,0.15); }
.waste-card.critical { border-left-color: var(--c-danger); }
.waste-card.high { border-left-color: var(--c-warning); }
.waste-card.medium { border-left-color: var(--c-accent-cyan); }
.waste-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--s-2);
}
.waste-severity {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 2px 8px;
  border-radius: 3px;
  margin-right: var(--s-2);
}
.waste-severity.critical { background: rgba(239,68,68,0.2); color: var(--c-danger); }
.waste-severity.high { background: rgba(245,158,11,0.2); color: var(--c-warning); }
.waste-severity.medium { background: rgba(0,240,255,0.1); color: var(--c-accent-cyan); }
.waste-severity.low { background: rgba(125,140,163,0.1); color: var(--c-text-dim); }
.waste-confidence {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--c-text-dim);
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--c-border);
  padding: 1px 6px;
  border-radius: 3px;
  margin-left: 6px;
}
.waste-system { font-size: 13px; color: var(--c-text-dim); }
.waste-desc { font-size: 16px; margin-bottom: var(--s-2); }
.waste-rec { font-size: 14px; color: var(--c-text-dim); margin-bottom: var(--s-2); }
.waste-savings {
  font-family: var(--font-mono);
  font-size: 16px;
  color: var(--c-success);
  text-shadow: 0 0 6px rgba(34,197,94,0.4);
}
.waste-fix-container { position: relative; margin-top: var(--s-2); }
.waste-fix {
  padding: var(--s-2) var(--s-3);
  background: rgba(0,0,0,0.3);
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--c-text-dim);
  white-space: pre-wrap;
  line-height: 1.6;
  overflow-x: auto;
  max-height: 200px;
}
.copy-fix-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  background: transparent;
  border: 1px solid var(--c-text-dim);
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
  z-index: 2;
}
.copy-fix-btn:hover {
  border-color: var(--c-accent-cyan);
  color: var(--c-accent-cyan);
  box-shadow: 0 0 8px rgba(0,240,255,0.2);
}
.copy-fix-btn.copied {
  border-color: #10b981;
  color: #10b981;
  box-shadow: 0 0 10px rgba(16,185,129,0.3);
}

/* CHARTS */
.chart-with-axis { display: flex; gap: 0; padding: var(--s-2) 0; }
.y-axis {
  width: 56px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: flex-end;
  padding-right: 8px;
  height: 120px;
}
.y-axis-label {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--c-text-dim);
  line-height: 1;
}
.chart-area { flex: 1; position: relative; min-width: 0; }
.y-grid-lines {
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 120px;
  pointer-events: none;
}
.y-grid-line {
  position: absolute;
  left: 0; right: 0;
  height: 1px;
  background: rgba(255,255,255,0.04);
}
.bar-chart {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 120px;
  padding-top: var(--s-2);
}
.bar-chart:hover .bar { opacity: 0.4; }
.bar-chart:hover .bar:hover { opacity: 1; box-shadow: 0 0 8px rgba(0,240,255,0.3); }
.bar {
  flex: 1;
  background: var(--c-accent-cyan);
  border-radius: 2px 2px 0 0;
  min-width: 4px;
  opacity: 0.7;
  transition: opacity 0.2s;
  cursor: pointer;
}
.bar-secondary { background: #3b82f6; }
.bar-label {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  margin-top: var(--s-1);
}

/* BAR ROWS (horizontal) */
.bar-row {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  padding: 6px 0;
}
.bar-row-label {
  min-width: 120px;
  font-size: 13px;
  font-family: var(--font-mono);
  color: var(--c-text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar-row-track {
  flex: 1;
  height: 8px;
  background: rgba(255,255,255,0.04);
  border-radius: 4px;
  overflow: hidden;
}
.bar-row-fill {
  height: 100%;
  background: var(--c-accent-cyan);
  border-radius: 4px;
  transition: width 0.5s ease;
}
.bar-row-value {
  min-width: 60px;
  text-align: right;
  font-size: 13px;
  font-family: var(--font-mono);
  color: var(--c-accent-cyan);
}

/* MODEL BAR */
.model-bar {
  display: flex;
  height: 32px;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(255,255,255,0.04);
  margin-top: var(--s-2);
  margin-bottom: var(--s-2);
}
.model-segment { transition: width 0.5s; position: relative; overflow: hidden; }
.segment-label {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 500;
  color: rgba(255,255,255,0.9);
  text-shadow: 0 1px 3px rgba(0,0,0,0.5);
  white-space: nowrap;
  pointer-events: none;
}
.model-legend {
  display: flex;
  gap: var(--s-3);
  flex-wrap: wrap;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--c-text-dim);
}
.model-legend-item { display: flex; align-items: center; gap: 6px; }
.model-legend-dot { width: 8px; height: 8px; border-radius: 2px; }

/* PROJ ROWS */
.proj-row {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  padding: var(--s-2) 0;
  border-bottom: 1px solid var(--c-border);
  font-size: 14px;
}
.proj-row:last-child { border-bottom: none; }
.proj-name { flex: 1; font-weight: 400; }
.proj-stat { color: var(--c-text-dim); font-family: var(--font-mono); font-size: 13px; min-width: 80px; text-align: right; }
.proj-cost { font-family: var(--font-mono); font-size: 13px; color: var(--c-accent-cyan); min-width: 70px; text-align: right; }

/* RECOMMENDATIONS */
.rec-item {
  padding: var(--s-2) 0;
  border-bottom: 1px solid var(--c-border);
  font-size: 14px;
  color: var(--c-text-dim);
  line-height: 1.6;
}
.rec-item:last-child { border-bottom: none; }

/* RIGHT PANEL */
.config-col {
  border-left: 1px solid var(--c-border);
  padding: var(--s-4);
  background: rgba(10,11,16,0.8);
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
  overflow-y: auto;
}
.section-title {
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  margin-bottom: var(--s-3);
  color: var(--c-text-dim);
  padding-bottom: var(--s-1);
  border-bottom: 1px solid var(--c-border);
  font-family: var(--font-mono);
}
.summary-metric {
  font-family: var(--font-mono);
  font-size: 18px;
  color: var(--c-text-dim);
  margin-bottom: var(--s-3);
}
.summary-metric .num {
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}
.version-footer {
  margin-top: auto;
  padding-top: var(--s-4);
  border-top: 1px solid var(--c-border);
  font-size: 13px;
  color: var(--c-text-dim);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.version-footer a { color: var(--c-accent-cyan); text-decoration: none; }
.social-icons { display: flex; gap: var(--s-2); }
.social-link { color: var(--c-text-dim); transition: color 0.2s; }
.social-link:hover { color: var(--c-accent-cyan); }

/* QUICK COMMANDS */
.quick-cmd {
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  transition: background 0.2s, color 0.2s;
}
.quick-cmd:hover { background: rgba(0,240,255,0.05); color: var(--c-accent-cyan); }
.quick-cmd.copied { color: #10b981; }

/* TOOLTIP */
.chart-tooltip {
  position: fixed;
  z-index: 200;
  background: #1c1f26;
  border: 1px solid rgba(0,240,255,0.3);
  border-radius: 8px;
  padding: 10px 14px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s ease;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--c-text-main);
  box-shadow: 0 4px 20px rgba(0,0,0,0.5), 0 0 15px rgba(0,240,255,0.1);
  max-width: 240px;
  line-height: 1.5;
  white-space: nowrap;
}
.chart-tooltip.visible { opacity: 1; }
.tt-label { color: var(--c-text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 4px; }
.tt-value { color: var(--c-accent-cyan); font-size: 16px; font-weight: 500; text-shadow: var(--glow-text); }
.tt-secondary { color: var(--c-text-dim); font-size: 12px; margin-top: 2px; }

/* PENDING CHANGES BAR */
.manage-pending-bar {
  position: fixed;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%) translateY(100%);
  background: var(--c-surface);
  border: 1px solid var(--c-accent-cyan);
  border-bottom: none;
  border-radius: 12px 12px 0 0;
  padding: 16px 32px;
  display: flex;
  align-items: center;
  gap: 24px;
  z-index: 150;
  transition: transform 0.3s ease;
  box-shadow: 0 -4px 30px rgba(0, 240, 255, 0.15);
}
.manage-pending-bar.visible { transform: translateX(-50%) translateY(0); }
.manage-pending-bar span {
  font-family: var(--font-mono);
  font-size: 16px;
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}
.manage-pending-bar button {
  background: var(--c-accent-cyan);
  color: var(--c-bg);
  border: none;
  padding: 10px 24px;
  border-radius: 8px;
  font-family: var(--font-mono);
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}
.manage-pending-bar button:hover {
  box-shadow: 0 0 20px rgba(0, 240, 255, 0.5);
  transform: scale(1.02);
}

/* TOAST */
.toast {
  position: fixed;
  bottom: 32px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--c-accent-cyan);
  color: var(--c-bg);
  padding: 14px 28px;
  border-radius: 10px;
  font-family: var(--font-mono);
  font-size: 16px;
  font-weight: 500;
  opacity: 0;
  transition: opacity 0.3s;
  pointer-events: none;
  z-index: 100;
  box-shadow: 0 4px 24px rgba(0, 240, 255, 0.4);
}
.toast.visible { opacity: 1; }

/* SESSION TABLE */
.session-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-family: var(--font-mono);
}
.session-table th {
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--c-text-dim);
  padding: var(--s-1) var(--s-2);
  border-bottom: 1px solid var(--c-border);
  font-weight: 500;
}
.session-table td {
  padding: 6px var(--s-2);
  border-bottom: 1px solid rgba(255,255,255,0.03);
  color: var(--c-text-dim);
  white-space: nowrap;
}
.session-table tr:hover td { background: rgba(0,240,255,0.02); }

/* MANAGE BANNER */
.manage-banner {
  display: flex;
  align-items: center;
  gap: var(--s-4);
  padding: var(--s-4) var(--s-5);
  background: linear-gradient(135deg, rgba(0, 240, 255, 0.08), rgba(0, 102, 255, 0.06));
  border: 1px solid rgba(0, 240, 255, 0.25);
  border-radius: 12px;
  margin-bottom: var(--s-4);
}
.manage-banner-icon {
  font-size: 36px;
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
  flex-shrink: 0;
}
.manage-banner-title {
  font-size: 18px;
  font-weight: 500;
  color: var(--c-text-main);
  margin-bottom: 4px;
}
.manage-banner-desc {
  font-size: 14px;
  color: var(--c-text-dim);
  line-height: 1.5;
}

/* MANAGE TOGGLES */
.manage-row {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  padding: 10px var(--s-3);
  border-bottom: 1px solid var(--c-border);
}
.manage-row:last-child { border-bottom: none; }
.manage-toggle {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
  flex-shrink: 0;
}
.manage-toggle input { opacity: 0; width: 0; height: 0; }
.manage-slider {
  position: absolute;
  cursor: pointer;
  top: 0; left: 0; right: 0; bottom: 0;
  background: #333;
  border-radius: 12px;
  transition: 0.3s;
}
.manage-slider:before {
  content: "";
  position: absolute;
  height: 18px; width: 18px;
  left: 3px; bottom: 3px;
  background: white;
  border-radius: 50%;
  transition: 0.3s;
}
.manage-toggle input:checked + .manage-slider { background: var(--c-accent-cyan); }
.manage-toggle input:checked + .manage-slider:before { transform: translateX(20px); background: var(--c-bg); }
.manage-info { flex: 1; min-width: 0; }
.manage-label { font-size: 14px; font-family: var(--font-mono); }
.manage-desc { font-size: 12px; color: var(--c-text-dim); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.empty-state {
  text-align: center;
  padding: var(--s-6) var(--s-4);
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  font-size: 14px;
}

@media (max-width: 1100px) {
  .layout { grid-template-columns: 1fr; height: auto; }
  .nav-col { display: none; }
  .config-col { display: none; }
  .stat-row { grid-template-columns: repeat(2, 1fr); }
  .y-axis { width: 44px; }
  .y-axis-label { font-size: 10px; }
}`;
}

function renderJS(): string {
  return `(function() {
  // Nav switching
  document.querySelectorAll('.nav-item[data-view]').forEach(function(item) {
    item.addEventListener('click', function(e) {
      e.preventDefault();
      var view = this.getAttribute('data-view');
      document.querySelectorAll('.view').forEach(function(v) { v.classList.remove('active'); });
      document.querySelectorAll('.nav-item').forEach(function(n) { n.classList.remove('active'); });
      var target = document.getElementById('view-' + view);
      if (target) target.classList.add('active');
      this.classList.add('active');
      var main = document.querySelector('.main-col');
      if (main) main.scrollTop = 0;
    });
  });

  // Custom tooltip
  var tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  document.body.appendChild(tooltip);

  function showTooltip(e, html) {
    tooltip.innerHTML = html;
    tooltip.classList.add('visible');
    positionTooltip(e);
  }
  function positionTooltip(e) {
    var x = e.clientX + 12;
    var y = e.clientY - 10;
    var rect = tooltip.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 16) x = e.clientX - rect.width - 12;
    if (y + rect.height > window.innerHeight - 16) y = e.clientY - rect.height - 10;
    if (y < 8) y = 8;
    tooltip.style.left = x + 'px';
    tooltip.style.top = y + 'px';
  }
  function hideTooltip() { tooltip.classList.remove('visible'); }

  // Bind bar tooltips
  document.querySelectorAll('.bar[data-tt-date]').forEach(function(bar) {
    bar.addEventListener('mouseover', function(e) {
      showTooltip(e,
        '<div class="tt-label">' + this.dataset.ttDate + '</div>' +
        '<div class="tt-value">' + this.dataset.ttVal + '</div>' +
        '<div class="tt-secondary">' + this.dataset.ttRuns + ' runs</div>'
      );
    });
    bar.addEventListener('mousemove', positionTooltip);
    bar.addEventListener('mouseout', hideTooltip);
  });

  // Bind model segment tooltips
  document.querySelectorAll('.model-segment[data-tt-model]').forEach(function(seg) {
    seg.addEventListener('mouseover', function(e) {
      showTooltip(e,
        '<div class="tt-label">' + this.dataset.ttModel + '</div>' +
        '<div class="tt-value">' + this.dataset.ttPct + '%</div>' +
        '<div class="tt-secondary">' + this.dataset.ttCost + '</div>'
      );
    });
    seg.addEventListener('mousemove', positionTooltip);
    seg.addEventListener('mouseout', hideTooltip);
  });

  // Copy Fix buttons
  function flashBtn(btn) {
    var original = btn.textContent;
    btn.classList.add('copied');
    btn.textContent = 'Copied!';
    setTimeout(function() {
      btn.classList.remove('copied');
      btn.textContent = original;
    }, 2000);
  }
  function fallbackCopy(text, btn) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); if (btn) flashBtn(btn); }
    catch(e) { if (btn) { btn.textContent = 'Failed'; setTimeout(function() { btn.textContent = 'Copy Fix'; }, 2000); } }
    document.body.removeChild(ta);
  }
  document.querySelectorAll('.copy-fix-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var snippet = this.getAttribute('data-snippet');
      if (!snippet) return;
      snippet = snippet.replace(/&#10;/g, '\\n').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(snippet).then(function() { flashBtn(btn); }).catch(function() { fallbackCopy(snippet, btn); });
      } else {
        fallbackCopy(snippet, btn);
      }
    });
  });

  // Manage toggles: accumulate changes, copy all at once
  var pendingChanges = {};

  function updatePendingBar() {
    var bar = document.getElementById('manage-pending-bar');
    var keys = Object.keys(pendingChanges);
    if (keys.length === 0) {
      if (bar) bar.classList.remove('visible');
      return;
    }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'manage-pending-bar';
      bar.className = 'manage-pending-bar';
      bar.innerHTML = '<span id="pending-count"></span><button id="pending-copy-btn">Copy All to Clipboard</button>';
      document.body.appendChild(bar);
      document.getElementById('pending-copy-btn').addEventListener('click', copyAllPending);
    }
    document.getElementById('pending-count').textContent = keys.length + ' change' + (keys.length > 1 ? 's' : '') + ' ready';
    bar.classList.add('visible');
  }

  function copyAllPending() {
    var keys = Object.keys(pendingChanges);
    if (keys.length === 0) return;
    var allCmds = keys.map(function(k) { return pendingChanges[k]; }).join('\\n');
    var header = '# Token Optimizer: ' + keys.length + ' change' + (keys.length > 1 ? 's' : '') + '\\n# Paste this into your agent chat and ask it to run these commands\\n\\n';
    var fullText = header + allCmds;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(fullText).then(function() {
        showToast(keys.length + ' command' + (keys.length > 1 ? 's' : '') + ' copied! Paste into your agent chat.');
      }).catch(function() {
        fallbackCopy(fullText, null);
        showToast(keys.length + ' command' + (keys.length > 1 ? 's' : '') + ' copied!');
      });
    } else {
      fallbackCopy(fullText, null);
      showToast(keys.length + ' command' + (keys.length > 1 ? 's' : '') + ' copied!');
    }
  }

  document.querySelectorAll('.manage-toggle input').forEach(function(input) {
    // Track original checked state so we know if user is changing or reverting
    input._origChecked = input.checked;
    input.addEventListener('change', function() {
      var cmd = this.getAttribute('data-manage-cmd');
      var name = this.getAttribute('data-manage-name') || cmd;
      if (!cmd) return;
      // If the toggle moved AWAY from its original state, queue the command.
      // If it moved BACK to its original state, remove it (user changed their mind).
      if (this.checked !== this._origChecked) {
        pendingChanges[name] = cmd;
      } else {
        delete pendingChanges[name];
      }
      updatePendingBar();
    });
  });

  // Toast helper
  function showToast(msg) {
    var t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('visible');
    setTimeout(function() { t.classList.remove('visible'); }, 2000);
  }

  // Quick commands copy
  document.querySelectorAll('.quick-cmd').forEach(function(el) {
    el.addEventListener('click', function() {
      var cmd = this.textContent;
      var self = this;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).then(function() {
          self.classList.add('copied');
          setTimeout(function() { self.classList.remove('copied'); }, 1500);
        });
      }
    });
  });
})();`;
}

export function generateDashboardHtml(data: DashboardData): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Token Optimizer - OpenClaw</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;500&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
${renderCSS()}
</style>
</head>
<body>

<div class="layout">
  ${renderNav(data)}

  <main class="main-col">
    ${renderOverview(data)}
    ${renderContext(data)}
    ${renderQuality(data)}
    ${renderWaste(data)}
    ${renderAgents(data)}
    ${renderSessions(data)}
    ${renderDaily(data)}
    ${renderManage(data)}
  </main>

  ${renderSidebar(data)}
</div>

<div class="toast" id="toast"></div>

<script>
${renderJS()}
</script>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// File output
// ---------------------------------------------------------------------------

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const DASHBOARD_DIR = path.join(HOME, ".openclaw", "token-optimizer");
const DASHBOARD_PATH = path.join(DASHBOARD_DIR, "dashboard.html");

export function writeDashboard(data: DashboardData): string {
  fs.mkdirSync(DASHBOARD_DIR, { recursive: true });
  const html = generateDashboardHtml(data);
  fs.writeFileSync(DASHBOARD_PATH, html, { encoding: "utf-8", mode: 0o600 });
  return DASHBOARD_PATH;
}

export function getDashboardPath(): string {
  return DASHBOARD_PATH;
}
