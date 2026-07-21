import { existsSync, writeFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { randomBytes } from "node:crypto";
import { TrendsStore } from "../storage/trends.js";
import { scoreToGrade, scoreToBand } from "../util/grade.js";
import { computeRealizedSavings } from "../savings.js";

export interface DashboardOptions {
  dataDir: string;
  outputPath?: string;
  days?: number;
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function num(v: unknown): number {
  const n = Number(v);
  return isNaN(n) ? 0 : n;
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(Math.round(n));
}

function gradeColor(grade: string): string {
  switch (grade) {
    case "S": return "#a855f7";
    case "A": return "#22c55e";
    case "B": return "#3b82f6";
    case "C": return "#eab308";
    case "D": return "#f97316";
    case "F": return "#ef4444";
    default: return "#6b7280";
  }
}

export function generateDashboard(opts: DashboardOptions): string {
  const days = opts.days ?? 30;
  const store = new TrendsStore(opts.dataDir);

  let sessions: Array<Record<string, unknown>> = [];
  let dailyStats: Array<Record<string, unknown>> = [];
  try {
    sessions = store.getRecentSessions(days);
    dailyStats = store.getDailyStats(days);
  } finally {
    store.close();
  }

  // Realized before/after savings (opens its own short-lived store connection).
  const savings = computeRealizedSavings(opts.dataDir, days);
  const fmtCost = (n: number): string =>
    !Number.isFinite(n) ? "$0" : n >= 1000 ? `$${(n / 1000).toFixed(1)}k` : `$${n.toFixed(2)}`;

  const totalSessions = sessions.length;
  const avgRH = totalSessions > 0
    ? sessions.reduce((s, r) => s + num(r.resource_health), 0) / totalSessions
    : 0;
  const avgSE = totalSessions > 0
    ? sessions.reduce((s, r) => s + num(r.session_efficiency), 0) / totalSessions
    : 0;
  const totalToolCalls = sessions.reduce((s, r) => s + num(r.tool_calls), 0);
  const totalCompactions = sessions.reduce((s, r) => s + num(r.compactions), 0);
  const totalDuration = sessions.reduce((s, r) => s + num(r.duration_seconds), 0);

  const rhGrade = scoreToGrade(Math.round(avgRH));
  const seGrade = scoreToGrade(Math.round(avgSE));
  const rhBand = scoreToBand(Math.round(avgRH));

  // Per-render nonce so the one inline script doesn't need 'unsafe-inline'.
  // (Enforced when served over http://; Chromium ignores meta-CSP on file://,
  // which is why output escaping above is the primary XSS defense.)
  const nonce = randomBytes(16).toString("base64");

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; connect-src 'self' https://api.github.com;">
<title>Token Optimizer - OpenCode Dashboard</title>
<style>
:root {
  --bg: #0d1117; --bg-card: #161b22; --bg-hover: #1c2128;
  --border: #30363d; --text: #e6edf3; --text-dim: #8b949e;
  --accent: #58a6ff; --success: #3fb950; --warning: #d29922;
  --danger: #f85149; --purple: #a855f7;
  --radius: 8px; --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-6: 24px;
}
/* Light theme — activation order (no FOUC): localStorage 'to-theme' > prefers-color-scheme:light > dark default.
   All color tokens re-derived for light backgrounds; secondary text (#242b35) pushed near-black so small
   description text stays legible (canonical complaint: washed-out grey at 10-13px in light mode). */
[data-theme="light"] {
  --bg: #eef1f6; --bg-card: #ffffff; --bg-hover: #e4e9f1;
  --border: rgba(14,22,34,0.14); --text: #0e1622; --text-dim: #242b35;
  /* Teal accent verified WCAG AA (4.5:1) on white. */
  --accent: #07697f;
  /* Status colors re-derived for AA legibility on light background. */
  --success: #1a7f37; --warning: #9a6700; --danger: #cf222e; --purple: #7c3aed;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; line-height: 1.5; }
/* Focus ring — visible for keyboard users; removed from mouse/touch paths by :focus-visible semantics. */
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
/* Respect user motion preference. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
.container { max-width: 1200px; margin: 0 auto; padding: var(--s-6); }
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--s-6); padding-bottom: var(--s-4); border-bottom: 1px solid var(--border); }
.header h1 { font-size: 20px; font-weight: 600; }
.header .sub { color: var(--text-dim); font-size: 13px; }
.nav { display: flex; gap: var(--s-2); margin-bottom: var(--s-6); flex-wrap: wrap; align-items: center; }
.nav a { padding: var(--s-2) var(--s-3); border-radius: var(--radius); color: var(--text-dim); text-decoration: none; font-size: 13px; cursor: pointer; transition: all 0.15s; }
.nav a:hover { background: var(--bg-hover); color: var(--text); }
.nav a.active { background: var(--accent); color: #fff; }
/* Theme toggle — placed in nav row; shows moon icon in dark mode (click to go light) and sun icon in light mode. */
.theme-toggle {
  margin-left: auto; display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-dim); background: var(--bg-card);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 5px 10px; cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.theme-toggle:hover { color: var(--text); border-color: var(--accent); }
.theme-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.theme-toggle-icon { width: 14px; height: 14px; display: block; }
.icon-sun { display: none; }
.icon-moon { display: block; }
[data-theme="light"] .icon-sun { display: block; }
[data-theme="light"] .icon-moon { display: none; }
.view { display: none; }
.view.active { display: block; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--s-4); margin-bottom: var(--s-6); }
.stat { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--s-4); }
.stat-value { font-size: 28px; font-weight: 700; margin-bottom: var(--s-1); font-variant-numeric: tabular-nums; }
.stat-label { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
.stat-sub { font-size: 11px; color: var(--text-dim); margin-top: var(--s-1); }
table { width: 100%; border-collapse: collapse; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
th { text-align: left; padding: var(--s-3) var(--s-4); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-dim); background: var(--bg-hover); border-bottom: 1px solid var(--border); }
td { padding: var(--s-3) var(--s-4); border-bottom: 1px solid var(--border); font-size: 13px; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--bg-hover); }
.grade { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; font-weight: 700; font-size: 13px; color: #fff; }
.section-title { font-size: 16px; font-weight: 600; margin-bottom: var(--s-4); }
.chart-bar { height: 6px; border-radius: 3px; background: var(--border); margin: var(--s-1) 0; overflow: hidden; }
.chart-bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
.empty { text-align: center; padding: var(--s-6); color: var(--text-dim); }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
.oc-footer { margin-top: var(--s-6); padding-top: var(--s-4); border-top: 1px solid var(--border);
             display: flex; align-items: center; gap: 16px; color: var(--text-dim); font-size: 13px; }
.oc-footer .byline { opacity: 0.85; }
.oc-footer a { color: var(--accent); text-decoration: none; }
.gh-star { display: inline-flex; align-items: center; gap: 6px; padding: 4px 11px; font-size: 12px;
           color: var(--accent); border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card);
           transition: border-color 0.15s, background 0.15s; }
.gh-star:hover { border-color: var(--accent); background: var(--bg-hover); }
.gh-star-count { font-variant-numeric: tabular-nums; padding-left: 7px; border-left: 1px solid var(--border); color: var(--text); }
.oc-social { display: inline-flex; gap: 13px; align-items: center; margin-left: auto; }
.oc-social a { color: var(--text-dim); display: inline-flex; transition: color 0.15s; }
.oc-social a:hover { color: var(--text); }
</style>
<!-- No-FOUC theme boot: reads localStorage 'to-theme', falls back to prefers-color-scheme:light, defaults to dark.
     Must run before first paint so CSS vars resolve correctly on frame 1. -->
<script nonce="${nonce}">
(function () {
  try {
    var stored = null;
    try { stored = window.localStorage.getItem('to-theme'); } catch (e) {}
    var theme;
    if (stored === 'light' || stored === 'dark') {
      theme = stored;
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
      theme = 'light';
    } else {
      theme = 'dark';
    }
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  } catch (e) { /* dark default already applies */ }
})();
</script>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>Token Optimizer</h1>
      <div class="sub">OpenCode Dashboard &middot; Last ${days} days &middot; ${totalSessions} sessions</div>
    </div>
    <div class="sub">Generated ${esc(new Date().toISOString().slice(0, 16).replace("T", " "))}</div>
  </div>

  <div class="nav">
    <a class="active" data-view="overview">Overview</a>
    <a data-view="savings">Savings</a>
    <a data-view="quality">Quality Trends</a>
    <a data-view="sessions">Sessions</a>
    <a data-view="daily">Daily Stats</a>
    <button type="button" id="theme-toggle" class="theme-toggle"
            aria-pressed="false" aria-label="Toggle light and dark theme"
            title="Toggle light / dark theme">
      <svg class="theme-toggle-icon icon-moon" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round"
           stroke-linejoin="round" aria-hidden="true">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
      <svg class="theme-toggle-icon icon-sun" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round"
           stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="5"/>
        <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
      </svg>
      <span class="theme-toggle-label">Dark</span>
    </button>
  </div>

  <!-- OVERVIEW -->
  <div class="view active" id="view-overview">
    <div class="stats">
      <div class="stat">
        <div class="stat-value">${totalSessions}</div>
        <div class="stat-label">Total Sessions</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:${gradeColor(rhGrade)}">${esc(rhGrade)}</div>
        <div class="stat-label">Avg Resource Health</div>
        <div class="stat-sub">${Math.round(avgRH)}/100 (${esc(rhBand)})</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:${gradeColor(seGrade)}">${esc(seGrade)}</div>
        <div class="stat-label">Avg Session Efficiency</div>
        <div class="stat-sub">${Math.round(avgSE)}/100</div>
      </div>
      <div class="stat">
        <div class="stat-value">${esc(fmtNum(totalToolCalls))}</div>
        <div class="stat-label">Total Tool Calls</div>
      </div>
      <div class="stat">
        <div class="stat-value">${totalCompactions}</div>
        <div class="stat-label">Compactions</div>
      </div>
      <div class="stat">
        <div class="stat-value">${Math.round(totalDuration / 60)}m</div>
        <div class="stat-label">Total Session Time</div>
      </div>
    </div>

    ${totalSessions === 0 ? '<div class="empty">No sessions recorded yet. Start using OpenCode with the Token Optimizer plugin to see data here.</div>' : ""}

    ${dailyStats.length > 0 ? `
    <div class="section-title">Daily Activity (Last ${days} Days)</div>
    <table>
      <thead><tr><th>Date</th><th>Sessions</th><th>Avg Quality</th><th>Grade</th></tr></thead>
      <tbody>
        ${dailyStats.map((d) => {
          const avgQ = num(d.avg_resource_health);
          const g = scoreToGrade(Math.round(avgQ));
          return `<tr>
            <td>${esc(String(d.date))}</td>
            <td>${num(d.sessions)}</td>
            <td>
              <div style="display:flex;align-items:center;gap:8px">
                <span>${Math.round(avgQ)}/100</span>
                <div class="chart-bar" style="flex:1"><div class="chart-bar-fill" style="width:${Math.min(100, Math.round(avgQ))}%;background:${gradeColor(g)}"></div></div>
              </div>
            </td>
            <td><span class="grade" style="background:${gradeColor(g)}">${esc(g)}</span></td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
    ` : ""}
  </div>

  <!-- SAVINGS -->
  <div class="view" id="view-savings">
    <div class="section-title">Token Optimizer &middot; Savings</div>

    ${!savings.ready ? (() => {
      // New-user view: render baseline-building progress card instead of a dead end.
      const bb = savings.baselineBuilding;
      if (bb) {
        const sNeed = bb.sessionsNeeded;
        const sHave = Math.min(sNeed, bb.sessionsInWindow);
        const dLeft = bb.daysLeft;
        const pct = sNeed > 0 ? Math.min(100, Math.round(sHave / sNeed * 100)) : 0;
        return `
    <div style="background:var(--bg-card);border:1px solid var(--accent);border-radius:var(--radius);padding:var(--s-6);margin-bottom:var(--s-4);box-shadow:0 0 0 1px rgba(88,166,255,0.12);">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--accent);margin-bottom:var(--s-2);">Your savings baseline is still building</div>
      <div style="font-size:14px;color:var(--text-dim);line-height:1.6;margin-bottom:var(--s-3);">
        Token Optimizer measures savings against <strong>your own</strong> pre-optimization baseline, frozen from your first ${bb.earlyWindowDays} days of real sessions. It never uses anyone else's numbers.
      </div>
      <div style="font-size:14px;color:var(--text);margin-bottom:var(--s-3);">
        <strong>${sHave} of ~${sNeed} sessions</strong> collected in your baseline window${dLeft > 0 ? `, about <strong>${dLeft} day${dLeft === 1 ? "" : "s"}</strong> until it locks in` : ""}.
        Until then, the Sessions view shows your current usage.
      </div>
      <div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden;">
        <div style="height:100%;width:${pct}%;background:var(--accent);border-radius:4px;transition:width 0.3s;"></div>
      </div>
      <div style="margin-top:var(--s-2);font-size:11px;color:var(--text-dim);">${pct}% complete &middot; first tracked session: ${esc(bb.firstDate)}</div>
    </div>`;
      }
      // Absolute zero state (no sessions at all).
      return `
    <div class="empty">
      No sessions recorded yet — install the Token Optimizer plugin and start coding to see savings here.
    </div>`;
    })() : `
    <!-- TRANSFORMATION HERO: the big picture estimated (old way vs now). -->
    <!-- INVARIANT: compressionMeasuredUsd is rendered below as a SEPARATE card    -->
    <!-- and is NEVER summed into monthlySavingsUsd. Do not change this.           -->
    <!-- Issue #87: the hero shows only on a GENUINE net win (monthlySavingsUsd>0), -->
    <!-- so the clamped headline can never sit beside net-negative arms. When the   -->
    <!-- net is <= 0 we show a neutral "roughly flat" card; the measured floor below -->
    <!-- still reports realized savings.                                            -->
    ${savings.monthlySavingsUsd > 0 ? `
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:var(--s-6);margin-bottom:var(--s-4);">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-2);">The big picture &middot; estimated</div>
      <div style="display:flex;align-items:baseline;gap:var(--s-2);flex-wrap:wrap;margin-bottom:var(--s-3);">
        <span style="font-family:monospace;font-size:52px;font-weight:700;line-height:1;color:var(--success)">${fmtCost(savings.monthlySavingsUsd)}</span>
        <span style="font-size:20px;color:var(--text-dim);font-family:monospace;">/mo${savings.transformationPct > 0 ? ` &mdash; ~${Math.round(savings.transformationPct * 100)}% lighter` : ""}</span>
      </div>
      <div style="font-size:13px;color:var(--text-dim);line-height:1.6;margin-bottom:var(--s-4);">
        Had you worked this period the way you did before Token Optimizer, you'd have paid about
        <strong style="color:var(--text)">${fmtCost(savings.monthlySavingsUsd)} more</strong>
        &mdash; est. <strong style="color:var(--text)">${fmtCost(savings.actualMonthlyUsd)}</strong> now vs
        <strong style="color:var(--text)">${fmtCost(savings.counterfactualMonthlyUsd)}</strong> the old way.
        Your volume is held constant on both sides, so this is pure efficiency, not workload growth.
      </div>
      <!-- Old way vs now grid -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:var(--s-4);padding:var(--s-4);background:var(--bg-hover);border-radius:var(--radius);margin-bottom:var(--s-4);">
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-1);">The old way</div>
          <div style="font-family:monospace;font-size:22px;font-weight:700;color:var(--text)">${fmtCost(savings.beforeCostPerSession)}<span style="font-size:12px;color:var(--text-dim)">/session</span></div>
          <div style="font-size:12px;color:var(--text-dim);margin-top:var(--s-1)">${esc(savings.beforeMixLabel)}${Math.round(savings.beforeCacheHit * 100) !== Math.round(savings.afterCacheHit * 100) ? ` &middot; ${Math.round(savings.beforeCacheHit * 100)}% cache reuse` : ""}</div>
        </div>
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-1);">Now</div>
          <div style="font-family:monospace;font-size:22px;font-weight:700;color:var(--success)">${fmtCost(savings.afterCostPerSession)}<span style="font-size:12px;color:var(--text-dim)">/session</span></div>
          <div style="font-size:12px;color:var(--text-dim);margin-top:var(--s-1)">${esc(savings.afterMixLabel)}${Math.round(savings.beforeCacheHit * 100) !== Math.round(savings.afterCacheHit * 100) ? ` &middot; ${Math.round(savings.afterCacheHit * 100)}% cache reuse` : ""}</div>
        </div>
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-1);">${savings.savingsPerSession >= 0 ? "Cut per session" : "Added per session"}</div>
          <div style="font-family:monospace;font-size:22px;font-weight:700;color:${savings.savingsPerSession >= 0 ? "var(--success)" : "var(--danger)"}">${savings.savingsPerSession >= 0 ? "" : "+"}${fmtCost(Math.abs(savings.savingsPerSession))}</div>
          <div style="font-size:12px;color:var(--text-dim);margin-top:var(--s-1);">main-work routing &amp; caching &middot; ~${Math.round(savings.sessionsPerMonth)} sessions/mo</div>
        </div>
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-1);">Saved to date</div>
          <div style="font-family:monospace;font-size:22px;font-weight:700;color:var(--success)">${fmtCost(savings.cumulativeSavedUsd)}</div>
          <div style="font-size:12px;color:var(--text-dim);margin-top:var(--s-1);">all sessions since baseline</div>
        </div>
      </div>
      <!-- Waterfall breakdown: levers telescope to the headline. -->
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-2);">Where it comes from</div>
      <table>
        <thead><tr><th>Lever</th><th>Est. $/month</th></tr></thead>
        <tbody>
          ${savings.breakdown.filter((b) => Math.abs(b.monthlyUsd) >= 0.005).map((b) => `<tr>
            <td>${esc(b.label)}</td>
            <td style="font-family:monospace;color:${b.monthlyUsd >= 0 ? "var(--success)" : "var(--danger)"}">${b.monthlyUsd >= 0 ? "" : "+"}${fmtCost(Math.abs(b.monthlyUsd))}/mo</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>
    ` : `
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:var(--s-6);margin-bottom:var(--s-4);">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-2);">At or above your baseline this period</div>
      <div style="font-size:13px;color:var(--text-dim);line-height:1.6;">
        Your recent sessions cost about what your frozen baseline would have
        (est. <strong style="color:var(--text)">${fmtCost(savings.actualMonthlyUsd)}</strong> now vs
        <strong style="color:var(--text)">${fmtCost(savings.counterfactualMonthlyUsd)}</strong> the old way).
        The measured floor below is what Token Optimizer has counted directly.
      </div>
    </div>
    `}

    <!-- MEASURED FLOOR card: the proven, event-by-event subset. -->
    <!-- SEPARATE from the transformation hero. Never summed into the headline.     -->
    ${savings.compressionMeasuredUsd >= 0.005 ? `
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:var(--s-4) var(--s-4) var(--s-3);margin-bottom:var(--s-4);">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-dim);margin-bottom:var(--s-2);">Counted directly &middot; measured to date</div>
      <div style="display:flex;align-items:baseline;gap:var(--s-2);">
        <span style="font-family:monospace;font-size:32px;font-weight:700;color:var(--text)">${fmtCost(savings.compressionMeasuredUsd)}</span>
        <span style="font-size:14px;color:var(--text-dim);font-family:monospace;">/mo</span>
      </div>
      <div style="font-size:12px;color:var(--text-dim);margin-top:var(--s-2);line-height:1.6;">
        Tokens TO removed from your context (tool archives, delta reads, structure maps), as metered, before the baseline-mix reprice.
        This is the proven, event-by-event floor &mdash; a subset of the transformation estimate above, not added to it.
      </div>
      <div style="font-size:12px;color:var(--text-dim);margin-top:var(--s-1);">
        measuring since ${savings.installDate ? esc(savings.installDate) : "your first tracked session"} &mdash; your first tracked session, not necessarily install day
      </div>
    </div>
    ` : ""}

    <!-- OPPORTUNITY panel: "save more" (amber). -->
    <!-- Realizable savings inputs: OpenCode pipeline does not yet expose            -->
    <!-- unused-skill pruning ($) or model-routing potential ($) as separate fields. -->
    <!-- Scaffolding for when those inputs become available; currently shows a       -->
    <!-- one-action prompt toward the full /token-optimizer skill flow.              -->
    <div style="background:var(--bg-card);border:1px solid var(--warning);border-radius:var(--radius);padding:var(--s-4) var(--s-4) var(--s-3);margin-bottom:var(--s-4);box-shadow:0 0 0 1px rgba(210,153,34,0.14);">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--warning);margin-bottom:var(--s-2);">Money on the table &middot; opportunity</div>
      <div style="font-size:13px;color:var(--text-dim);line-height:1.6;margin-bottom:var(--s-3);">
        Real savings you have <strong style="color:var(--text)">not</strong> captured yet &mdash; on top of what you are already saving.
        OpenCode's pipeline does not yet expose per-opportunity $ figures (unused-skill pruning,
        model-routing potential, cache-drop cost), so this panel cannot show a dollar total.
        Run the skill below to surface all actionable opportunities.
      </div>
      <div style="padding:var(--s-3) var(--s-4);background:rgba(210,153,34,0.08);border:1px solid var(--warning);border-radius:var(--radius);font-family:monospace;font-size:13px;color:var(--text);">
        Run <span style="color:var(--warning);">/token-optimizer</span> and follow its suggestions to claim the rest &rarr;
      </div>
    </div>
    `}
  </div>

  <!-- QUALITY TRENDS -->
  <div class="view" id="view-quality">
    <div class="section-title">Quality Score Trends</div>
    ${sessions.length === 0 ? '<div class="empty">No quality data yet.</div>' : `
    <table>
      <thead><tr><th>Date</th><th>Session</th><th>Resource Health</th><th>Session Efficiency</th><th>Mode</th><th>Tool Calls</th><th>Compactions</th></tr></thead>
      <tbody>
        ${[...sessions].reverse().map((s) => {
          const rh = num(s.resource_health);
          const se = num(s.session_efficiency);
          const rhG = scoreToGrade(Math.round(rh));
          const seG = scoreToGrade(Math.round(se));
          return `<tr>
            <td>${esc(String(s.date))}</td>
            <td style="font-family:monospace;font-size:11px">${esc(String(s.session_id).slice(0, 8))}</td>
            <td><span class="grade" style="background:${gradeColor(rhG)}">${esc(rhG)}</span> ${Math.round(rh)}</td>
            <td><span class="grade" style="background:${gradeColor(seG)}">${esc(seG)}</span> ${Math.round(se)}</td>
            <td><span class="tag" style="background:var(--bg-hover)">${esc(String(s.mode ?? "general"))}</span></td>
            <td>${num(s.tool_calls)}</td>
            <td>${num(s.compactions)}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
    `}
  </div>

  <!-- SESSIONS -->
  <div class="view" id="view-sessions">
    <div class="section-title">Session History</div>
    ${sessions.length === 0 ? '<div class="empty">No sessions recorded yet.</div>' : `
    <table>
      <thead><tr><th>Date</th><th>Session ID</th><th>Model</th><th>Duration</th><th>Health</th><th>Efficiency</th><th>Tools</th><th>Mode</th></tr></thead>
      <tbody>
        ${sessions.map((s) => {
          const rh = num(s.resource_health);
          const se = num(s.session_efficiency);
          const dur = num(s.duration_seconds);
          const rhG = scoreToGrade(Math.round(rh));
          const seG = scoreToGrade(Math.round(se));
          return `<tr>
            <td>${esc(String(s.date))}</td>
            <td style="font-family:monospace;font-size:11px">${esc(String(s.session_id).slice(0, 12))}</td>
            <td>${esc(String(s.model ?? "unknown"))}</td>
            <td>${dur > 60 ? Math.round(dur / 60) + "m" : Math.round(dur) + "s"}</td>
            <td><span class="grade" style="background:${gradeColor(rhG)}">${esc(rhG)}</span> ${Math.round(rh)}</td>
            <td><span class="grade" style="background:${gradeColor(seG)}">${esc(seG)}</span> ${Math.round(se)}</td>
            <td>${num(s.tool_calls)}</td>
            <td><span class="tag" style="background:var(--bg-hover)">${esc(String(s.mode ?? ""))}</span></td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
    `}
  </div>

  <!-- DAILY STATS -->
  <div class="view" id="view-daily">
    <div class="section-title">Daily Aggregates</div>
    ${dailyStats.length === 0 ? '<div class="empty">No daily data yet.</div>' : `
    <table>
      <thead><tr><th>Date</th><th>Sessions</th><th>Avg Resource Health</th><th>Avg Efficiency</th></tr></thead>
      <tbody>
        ${dailyStats.map((d) => {
          const avgRH2 = num(d.avg_resource_health);
          const avgSE2 = num(d.avg_session_efficiency);
          return `<tr>
            <td>${esc(String(d.date))}</td>
            <td>${num(d.sessions)}</td>
            <td>${Math.round(avgRH2)}/100 (${esc(scoreToBand(Math.round(avgRH2)))})</td>
            <td>${Math.round(avgSE2)}/100</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
    `}
  </div>

  <footer class="oc-footer">
    <span class="byline">Built by <a href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener">Alex Greenshpun</a></span>
    <a class="gh-star" href="https://github.com/alexgreensh/token-optimizer" target="_blank" rel="noopener" title="Star Token Optimizer on GitHub">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.5l2.9 6.06 6.6.59-5 4.38 1.5 6.47L12 16.98 5.99 20.5l1.5-6.47-5-4.38 6.6-.59L12 2.5z"/></svg>
      <span>Star on GitHub</span>
      <span class="gh-star-count" data-gh-stars hidden></span>
    </a>
    <span class="oc-social">
      <a href="https://github.com/alexgreensh/token-optimizer" target="_blank" rel="noopener" title="GitHub"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.08-.73.08-.73 1.2.08 1.84 1.23 1.84 1.23 1.07 1.83 2.81 1.3 3.5 1 .1-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 016.02 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.65 4.65 0 011.23 3.22c0 4.61-2.81 5.63-5.48 5.92.42.36.81 1.1.81 2.22v3.29c0 .32.22.7.82.58A12.01 12.01 0 0024 12c0-6.63-5.37-12-12-12z"/></svg></a>
      <a href="https://x.com/alexgreensh" target="_blank" rel="noopener" title="X (Twitter)"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24h-6.66l-5.214-6.817-5.97 6.817H1.673l7.73-8.835L1.254 2.25h6.83l4.713 6.231 5.447-6.231zm-1.161 17.52h1.833L7.084 4.126H5.117l11.966 15.644z"/></svg></a>
      <a href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener" title="LinkedIn"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.34V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.46v6.28zM5.34 7.43a2.06 2.06 0 110-4.13 2.06 2.06 0 010 4.13zm1.78 13.02H3.56V9h3.56v11.45zM22.23 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.23 0z"/></svg></a>
    </span>
  </footer>
</div>

<script nonce="${nonce}">
document.querySelectorAll('.nav a').forEach(a => {
  a.addEventListener('click', () => {
    document.querySelectorAll('.nav a').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    a.classList.add('active');
    document.getElementById('view-' + a.dataset.view).classList.add('active');
  });
});
// Theme toggle wiring. The boot script in <head> already applied the correct
// theme before first paint (no FOUC); here we sync aria-pressed + label and wire the click.
(function setupThemeToggle() {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }
  function syncButton() {
    var light = currentTheme() === 'light';
    btn.setAttribute('aria-pressed', light ? 'true' : 'false');
    var label = btn.querySelector('.theme-toggle-label');
    if (label) label.textContent = light ? 'Light' : 'Dark';
  }
  function applyTheme(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    try { window.localStorage.setItem('to-theme', theme); } catch (e) {}
    syncButton();
  }
  btn.addEventListener('click', function() {
    applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
  });
  // Follow OS preference live only while the user hasn't made an explicit choice.
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: light)');
    var onChange = function(e) {
      var stored = null;
      try { stored = window.localStorage.getItem('to-theme'); } catch (err) {}
      if (stored === 'light' || stored === 'dark') return;
      if (e.matches) {
        document.documentElement.setAttribute('data-theme', 'light');
      } else {
        document.documentElement.removeAttribute('data-theme');
      }
      syncButton();
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
  syncButton();
})();
// Live GitHub star count (public CORS endpoint; degrades silently to no count).
(function () {
  function fmt(n) { return (typeof n !== 'number' || !isFinite(n) || n < 0) ? null : (n >= 1000 ? (n / 1000).toFixed(1).replace(/\\.0$/, '') + 'k' : String(n)); }
  function paint(label) { document.querySelectorAll('[data-gh-stars]').forEach(function (el) { el.textContent = label; el.hidden = false; }); }
  try { var c = sessionStorage.getItem('to-gh-stars'); if (c) { paint(c); return; } } catch (e) {}
  fetch('https://api.github.com/repos/alexgreensh/token-optimizer', { headers: { Accept: 'application/vnd.github+json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { var l = d && fmt(d.stargazers_count); if (!l) return; paint(l); try { sessionStorage.setItem('to-gh-stars', l); } catch (e) {} })
    .catch(function () {});
})();
</script>
</body>
</html>`;

  return html;
}

export function writeDashboard(opts: DashboardOptions): string {
  const outputPath = opts.outputPath ?? join(opts.dataDir, "dashboard.html");
  const dir = dirname(outputPath);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });

  const html = generateDashboard(opts);
  writeFileSync(outputPath, html, "utf-8");
  return outputPath;
}
