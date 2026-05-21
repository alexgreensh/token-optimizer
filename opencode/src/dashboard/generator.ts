import { existsSync, writeFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { TrendsStore } from "../storage/trends.js";
import { scoreToGrade, scoreToBand } from "../util/grade.js";

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

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<title>Token Optimizer - OpenCode Dashboard</title>
<style>
:root {
  --bg: #0d1117; --bg-card: #161b22; --bg-hover: #1c2128;
  --border: #30363d; --text: #e6edf3; --text-dim: #8b949e;
  --accent: #58a6ff; --success: #3fb950; --warning: #d29922;
  --danger: #f85149; --purple: #a855f7;
  --radius: 8px; --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-6: 24px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; line-height: 1.5; }
.container { max-width: 1200px; margin: 0 auto; padding: var(--s-6); }
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--s-6); padding-bottom: var(--s-4); border-bottom: 1px solid var(--border); }
.header h1 { font-size: 20px; font-weight: 600; }
.header .sub { color: var(--text-dim); font-size: 13px; }
.nav { display: flex; gap: var(--s-2); margin-bottom: var(--s-6); flex-wrap: wrap; }
.nav a { padding: var(--s-2) var(--s-3); border-radius: var(--radius); color: var(--text-dim); text-decoration: none; font-size: 13px; cursor: pointer; transition: all 0.15s; }
.nav a:hover { background: var(--bg-hover); color: var(--text); }
.nav a.active { background: var(--accent); color: #fff; }
.view { display: none; }
.view.active { display: block; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--s-4); margin-bottom: var(--s-6); }
.stat { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--s-4); }
.stat-value { font-size: 28px; font-weight: 700; margin-bottom: var(--s-1); }
.stat-label { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
.stat-sub { font-size: 11px; color: var(--text-dim); margin-top: var(--s-1); }
table { width: 100%; border-collapse: collapse; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
th { text-align: left; padding: var(--s-3) var(--s-4); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-dim); background: var(--bg-hover); border-bottom: 1px solid var(--border); }
td { padding: var(--s-3) var(--s-4); border-bottom: 1px solid var(--border); font-size: 13px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--bg-hover); }
.grade { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; font-weight: 700; font-size: 13px; color: #fff; }
.section-title { font-size: 16px; font-weight: 600; margin-bottom: var(--s-4); }
.chart-bar { height: 6px; border-radius: 3px; background: var(--border); margin: var(--s-1) 0; overflow: hidden; }
.chart-bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
.empty { text-align: center; padding: var(--s-6); color: var(--text-dim); }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
</style>
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
    <a data-view="quality">Quality Trends</a>
    <a data-view="sessions">Sessions</a>
    <a data-view="daily">Daily Stats</a>
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
</div>

<script>
document.querySelectorAll('.nav a').forEach(a => {
  a.addEventListener('click', () => {
    document.querySelectorAll('.nav a').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    a.classList.add('active');
    document.getElementById('view-' + a.dataset.view).classList.add('active');
  });
});
</script>
</body>
</html>`;

  return html;
}

export function writeDashboard(opts: DashboardOptions): string {
  const outputPath = opts.outputPath ?? join(opts.dataDir, "token-optimizer", "dashboard.html");
  const dir = dirname(outputPath);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });

  const html = generateDashboard(opts);
  writeFileSync(outputPath, html, "utf-8");
  return outputPath;
}
