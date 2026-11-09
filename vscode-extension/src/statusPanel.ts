// An expanded, theme-matched webview that mirrors the status-line data in a
// readable panel. Opened by clicking the status bar. Live-updates: the extension
// posts a PanelModel on every refresh and the webview re-renders. Buttons post
// messages back so the panel can drive the same commands as the tooltip links.
import * as crypto from 'crypto';
import * as vscode from 'vscode';
import { PanelModel } from './format';

export type PanelAction = 'openDashboard' | 'refresh' | 'install' | 'installDocs';

const VALID_ACTIONS = new Set<PanelAction>(['openDashboard', 'refresh', 'install', 'installDocs']);

export class StatusPanel {
  private panel: vscode.WebviewPanel | undefined;
  private last: PanelModel | undefined;

  constructor(private onAction: (action: PanelAction) => void) {}

  // Create or reveal the panel, then paint the latest model.
  show(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside, true);
      if (this.last) this.update(this.last);
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      'tokenOptimizerStatus',
      'Token Optimizer',
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: true, retainContextWhenHidden: false }
    );
    this.panel.webview.html = this.html();
    this.panel.webview.onDidReceiveMessage((m: { action?: string }) => {
      // Runtime allowlist — the TS type is erased, so don't let a compromised
      // webview drive an arbitrary tokenOptimizer.* command.
      if (m && typeof m.action === 'string' && VALID_ACTIONS.has(m.action as PanelAction)) {
        this.onAction(m.action as PanelAction);
      }
    });
    this.panel.onDidDispose(() => {
      this.panel = undefined;
    });
    if (this.last) this.update(this.last);
  }

  // Push fresh data to the panel if it's open (cheap no-op otherwise).
  update(model: PanelModel): void {
    this.last = model;
    void this.panel?.webview.postMessage(model);
  }

  dispose(): void {
    this.panel?.dispose();
    this.panel = undefined;
  }

  private html(): string {
    const nonce = makeNonce();
    const csp = `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';`;
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style nonce="${nonce}">
  :root { color-scheme: light dark; }
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
         background: var(--vscode-editor-background); padding: 18px 22px; font-size: 13px; }
  h1 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
  .sub { color: color-mix(in srgb, var(--vscode-foreground) 68%, transparent); font-size: 12px; margin-bottom: 18px; }
  .bar { font-family: var(--vscode-editor-font-family, monospace); letter-spacing: -1px; }
  /* A brighter grey than descriptionForeground (which reads too faint) — mixed
     from the theme foreground so it adapts to light/dark themes. */
  .muted { color: color-mix(in srgb, var(--vscode-foreground) 68%, transparent); }
  .row { display: flex; justify-content: space-between; align-items: baseline;
         padding: 7px 0; border-bottom: 1px solid var(--vscode-panel-border); }
  .row .k { color: color-mix(in srgb, var(--vscode-foreground) 68%, transparent); }
  .row .v { font-weight: 500; text-align: right; }
  .grade { font-weight: 600; }
  .g-S,.g-A { color: var(--vscode-charts-green, #4ec94e); }
  .g-B,.g-C { color: var(--vscode-charts-yellow, #d7b500); }
  .g-D,.g-F { color: var(--vscode-charts-red, #e35d5d); }
  .pending { color: color-mix(in srgb, var(--vscode-foreground) 60%, transparent); font-style: italic; }
  .usage-status { display:inline-block; margin-left:6px; padding:1px 6px; border-radius:8px; font-size:11px;
          background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); font-style: normal; }
  .usage-status.cached { background: var(--vscode-inputValidation-warningBackground, #5a4a1a); }
  .warn { color: var(--vscode-charts-orange, #d18616); }
  .pill { display:inline-block; padding:1px 7px; border-radius:9px; font-size:11px;
          background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
  .actions { margin-top: 18px; display: flex; gap: 8px; flex-wrap: wrap; }
  button { font-family: inherit; font-size: 12px; padding: 5px 11px; border: none; border-radius: 4px;
           cursor: pointer; background: var(--vscode-button-secondaryBackground, #3a3d41);
           color: var(--vscode-button-secondaryForeground, #fff); }
  button.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  button:hover { opacity: .9; }
  .empty { color: var(--vscode-descriptionForeground); margin-top: 24px; }
  .banner { background: var(--vscode-inputValidation-warningBackground, #5a4a1a);
            border: 1px solid var(--vscode-inputValidation-warningBorder, #b89500);
            color: var(--vscode-foreground); padding: 8px 11px; border-radius: 5px;
            font-size: 12px; margin-bottom: 14px; }
  .usagebar { height: 6px; border-radius: 3px; background: var(--vscode-panel-border); overflow:hidden; width:120px; display:inline-block; vertical-align:middle; margin-left:8px; }
  .usagefill { height: 100%; background: var(--vscode-charts-blue, #4e8ec9); }
  .panel-footer { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--vscode-panel-border);
                  display: flex; align-items: center; gap: 14px; }
  .gh-star { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; font-size: 12px;
             text-decoration: none; border-radius: 5px; border: 1px solid var(--vscode-button-border, transparent);
             background: var(--vscode-button-secondaryBackground, #3a3d41); color: var(--vscode-button-secondaryForeground, #fff); }
  .gh-star:hover { background: var(--vscode-button-secondaryHoverBackground, #45494e); }
  .panel-social { display: inline-flex; gap: 12px; align-items: center; margin-left: auto; }
  .panel-social a { color: color-mix(in srgb, var(--vscode-foreground) 62%, transparent); display: inline-flex; transition: color .15s; }
  .panel-social a:hover { color: var(--vscode-foreground); }
  /* ---- install funnel ---- */
  .fn-hero { font-size: 21px; font-weight: 700; letter-spacing: -.015em; line-height: 1.15; margin: 6px 0 4px;
             color: var(--vscode-charts-green, #4ec94e); }
  .fn-hero-sub { font-size: 12.5px; color: color-mix(in srgb, var(--vscode-foreground) 74%, transparent);
                 margin: 0 0 16px; line-height: 1.5; }
  .fn-lead { font-size: 12.5px; color: color-mix(in srgb, var(--vscode-foreground) 78%, transparent); margin: 2px 0 16px; line-height: 1.5; }
  .levels { display: flex; flex-direction: column; gap: 10px; margin-bottom: 18px; }
  .level { display: flex; gap: 10px; align-items: flex-start; }
  .level .ic { font-size: 15px; line-height: 1.2; flex: 0 0 auto; }
  .level .lt { font-weight: 600; }
  .level .ld { color: color-mix(in srgb, var(--vscode-foreground) 66%, transparent); font-size: 12px; }
  .savings { background: color-mix(in srgb, var(--vscode-charts-green, #4ec94e) 12%, transparent);
             border: 1px solid color-mix(in srgb, var(--vscode-charts-green, #4ec94e) 40%, transparent);
             border-radius: 7px; padding: 11px 13px; margin-bottom: 18px; font-size: 12.5px; line-height: 1.5; }
  .savings b { color: var(--vscode-charts-green, #4ec94e); }
  .savings .fine { display:block; margin-top:7px; color: color-mix(in srgb, var(--vscode-foreground) 60%, transparent); font-size: 11px; }
  table.tiers { width:100%; border-collapse:collapse; margin:9px 0 3px; }
  table.tiers td { padding:5px 0; border-top:1px solid color-mix(in srgb, var(--vscode-foreground) 16%, transparent); font-size:12px; }
  table.tiers tr:first-child td { border-top:none; }
  .tiers .tr-sub { color: color-mix(in srgb, var(--vscode-foreground) 52%, transparent); font-size:11px; margin-left:5px; }
  .tiers .tr-amt { text-align:right; font-weight:600; color: var(--vscode-charts-green, #4ec94e); white-space:nowrap; }
  .inst-label { font-weight: 600; font-size: 12px; margin: 14px 0 6px; }
  .inst-tab { font-size: 11px; color: color-mix(in srgb, var(--vscode-foreground) 60%, transparent); margin: 10px 0 4px; text-transform: uppercase; letter-spacing: .04em; }
  pre.cmd { background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.12)); border-radius: 5px;
            padding: 8px 10px; margin: 0 0 6px; overflow-x: auto; font-family: var(--vscode-editor-font-family, monospace);
            font-size: 12px; user-select: all; -webkit-user-select: all; white-space: pre; }
</style>
</head>
<body>
<div id="app"><div class="empty">Waiting for an active session…</div></div>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const gradeClass = (g) => 'grade g-' + esc(g);
  function row(k, vHtml) { return '<div class="row"><span class="k">' + esc(k) + '</span><span class="v">' + vHtml + '</span></div>'; }
  function usage(pct) { return '<span class="usagebar"><span class="usagefill" style="width:' + Math.max(0,Math.min(100,pct)) + '%"></span></span>'; }
  function usageLimit(w) {
    const status = esc(w.status || 'verified');
    const reset = w.reset ? 'resets ' + esc(w.reset) + ' · ' : '';
    const age = w.age ? ' · ' + esc(w.age) : '';
    return w.pct + '% ' + usage(w.pct) + '<br><span class="pending">' + reset + '<span class="usage-status ' + status + '" title="' + esc(w.detail || '') + '">' + status + '</span>' + age + '</span>';
  }
  function wireButtons(app) {
    for (const b of app.querySelectorAll('button[data-act]')) {
      b.addEventListener('click', () => vscode.postMessage({ action: b.getAttribute('data-act') }));
    }
  }
  function funnelHtml() {
    let h = '';
    h += '<h1>Token Optimizer</h1>';
    h += '<div class="fn-hero">Track the tokens you save.</div>';
    h += '<div class="fn-hero-sub">Three levels of optimization that cut token waste — while keeping output quality intact.</div>';
    h += '<div class="levels">';
    h += '<div class="level"><span class="ic">🧱</span><span><span class="lt">Structural waste</span><br><span class="ld">Bloated context, repeated file reads, and giant tool outputs — mapped and compressed. The savings survive compaction.</span></span></div>';
    h += '<div class="level"><span class="ic">⚡</span><span><span class="lt">Runtime waste</span><br><span class="ld">Right-sizes the model per turn, so routine work runs off the priciest model and you spend premium tokens only when they matter.</span></span></div>';
    h += '<div class="level"><span class="ic">🧭</span><span><span class="lt">Behavioral waste</span><br><span class="ld">A coach that nudges lighter habits, so your average session gets cheaper over time and stays there.</span></span></div>';
    h += '</div>';
    h += '<div class="savings"><b>See your savings</b> in the Token Optimizer dashboard after installing the CLI plugin.</div>';
    h += '<div class="inst-label">Install the CLI plugin (30 seconds):</div>';
    h += '<div class="inst-tab">Claude Code</div>';
    h += '<pre class="cmd">/plugin marketplace add alexgreensh/token-optimizer\\n/plugin install token-optimizer@alexgreensh-token-optimizer</pre>';
    h += '<div class="inst-tab">Codex</div>';
    h += '<pre class="cmd">codex plugin marketplace add alexgreensh/token-optimizer</pre>';
    h += '<div class="actions">';
    h += '<button class="primary" data-act="install">Copy Claude Code command</button>';
    h += '<button data-act="installDocs">All platforms &amp; docs</button>';
    h += '</div>';
    return h;
  }
  const RUNTIME_DISPLAY = { claude: 'Claude Code', copilot: 'GitHub Copilot', cursor: 'Cursor' };
  const runtimeDisplay = (m) => RUNTIME_DISPLAY[m && m.runtimeName] || 'Claude Code';
  function render(m) {
    const app = document.getElementById('app');
    if (m && m.funnel) { app.innerHTML = funnelHtml(); wireButtons(app); return; }
    if (!m || !m.hasData) { app.innerHTML = '<div class="empty">No active ' + esc(runtimeDisplay(m)) + ' session in this window yet.</div>'; return; }
    let h = '';
    h += '<h1>Token Optimizer</h1>';
    h += '<div class="sub">' + esc(m.model || runtimeDisplay(m)) + (m.effort ? ' · ' + esc(m.effort) : '') + '</div>';
    if (m.sourceLabel) h += '<div class="sub">' + esc(m.sourceLabel) + '</div>';
    if (!m.scoped) h += '<div class="banner">⚠️ No folder open — showing the most recent session globally. Open a folder so this reflects this window\\'s session.</div>';
    if (m.fillPct != null) h += row('Context', '<span class="bar">' + esc(m.fillBar) + '</span> ' + m.fillPct + '%' + (m.fillSource === 'jsonl' ? ' <span class="pill">panel</span>' : ''));
    if (m.lastCompaction) h += row('Last compaction', esc(m.lastCompaction));
    if (m.contextQ) h += row('ContextQ', '<span class="' + gradeClass(m.contextQ.grade) + '">' + esc(m.contextQ.grade) + ' (' + m.contextQ.score + ')</span>' + (m.contextQ.stale ? ' <span class="pending">cached</span>' : ''));
    if (m.eff) h += row('Efficiency', '<span class="' + gradeClass(m.eff.grade) + '">' + esc(m.eff.grade) + ' (' + m.eff.score + ')</span>');
    if (m.qualityPending) h += row('ContextQ / Eff', '<span class="pending">warming up…</span>');
    if (m.warnings && m.warnings.length) h += row('Warnings', '<span class="warn">' + m.warnings.map(esc).join(', ') + '</span>');
    if (m.compactions) {
      const rec = m.compactions.count > 0 ? ' (observing)' : '';
      h += row('Compactions', m.compactions.count + rec);
    }
    if (m.duration) h += row('Duration', esc(m.duration));
    if (m.agents && m.agents.length) h += row('Agents', m.agents.map(esc).join('<br>'));
    if (m.fiveHour) h += row('5-hour limit', usageLimit(m.fiveHour));
    if (m.sevenDay) h += row('7-day limit', usageLimit(m.sevenDay));
    h += '<div class="actions">';
    h += '<button class="primary" data-act="openDashboard">Open full dashboard</button>';
    h += '<button data-act="refresh">Refresh</button>';
    h += '</div>';
    h += '<div class="panel-footer">';
    h += '<a class="gh-star" href="https://github.com/alexgreensh/token-optimizer" target="_blank" rel="noopener" title="Star Token Optimizer on GitHub">';
    h += '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.5l2.9 6.06 6.6.59-5 4.38 1.5 6.47L12 16.98 5.99 20.5l1.5-6.47-5-4.38 6.6-.59L12 2.5z"/></svg>Star on GitHub</a>';
    h += '<span class="panel-social">';
    h += '<a href="https://github.com/alexgreensh/token-optimizer" target="_blank" rel="noopener" title="GitHub"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.08-.73.08-.73 1.2.08 1.84 1.23 1.84 1.23 1.07 1.83 2.81 1.3 3.5 1 .1-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 016.02 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.65 4.65 0 011.23 3.22c0 4.61-2.81 5.63-5.48 5.92.42.36.81 1.1.81 2.22v3.29c0 .32.22.7.82.58A12.01 12.01 0 0024 12c0-6.63-5.37-12-12-12z"/></svg></a>';
    h += '<a href="https://x.com/alexgreensh" target="_blank" rel="noopener" title="X (Twitter)"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24h-6.66l-5.214-6.817-5.97 6.817H1.673l7.73-8.835L1.254 2.25h6.83l4.713 6.231 5.447-6.231zm-1.161 17.52h1.833L7.084 4.126H5.117l11.966 15.644z"/></svg></a>';
    h += '<a href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener" title="LinkedIn"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.34V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.46v6.28zM5.34 7.43a2.06 2.06 0 110-4.13 2.06 2.06 0 010 4.13zm1.78 13.02H3.56V9h3.56v11.45zM22.23 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.23 0z"/></svg></a>';
    h += '</span></div>';
    app.innerHTML = h;
    wireButtons(app);
  }
  window.addEventListener('message', (e) => render(e.data));
</script>
</body>
</html>`;
  }
}

function makeNonce(): string {
  return crypto.randomBytes(18).toString('base64').replace(/[^a-zA-Z0-9]/g, '');
}
