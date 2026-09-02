// Pure presentation: Snapshot -> display strings. No `vscode` import, so the
// exact rendered text is unit-testable. statusBar.ts wraps the tooltip string
// into a MarkdownString and applies status-bar colors.
import { RateWindow, Snapshot, UsageLimitDisplayStatus, UsageLimitStatus } from './types';

export interface RenderOptions {
  nowMs: number;
}

// ---- small helpers ----

export function fillBar(pct: number | null, width = 10): string {
  if (pct == null || !Number.isFinite(pct)) return '─'.repeat(width);
  // Match the terminal status line's decile flooring (statusline.js) exactly,
  // so the bar shows the same number of blocks in both surfaces.
  const filled = Math.max(0, Math.min(width, Math.floor((pct / 100) * width)));
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}

// Escape characters that would break out of a markdown table cell or, worse,
// inject a clickable command: link into the isTrusted tooltip. Applied to every
// value that originates from cache files (model, effort, agent fields).
export function escapeMd(s: string): string {
  return s.replace(/[\\`*_[\]()<>|#]/g, '\\$&').replace(/\r?\n/g, ' ');
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// Clock part only, e.g. "6:40pm".
export function formatClock(epochSec: number | null): string {
  if (epochSec == null || epochSec <= 0) return '';
  const d = new Date(epochSec * 1000);
  if (isNaN(d.getTime())) return '';
  let h = d.getHours();
  const m = d.getMinutes();
  const ap = h >= 12 ? 'pm' : 'am';
  h = h % 12;
  if (h === 0) h = 12;
  return `${h}:${String(m).padStart(2, '0')}${ap}`;
}

// Smart reset label. For a near reset (the 5h window) shows just the time; for a
// reset days out (the 7d window) shows the date AND how many days remain, so a
// week-long limit isn't reported as a bare time-of-day.
export function formatReset(epochSec: number | null, nowMs: number): string {
  if (epochSec == null || epochSec <= 0) return '';
  const d = new Date(epochSec * 1000);
  if (isNaN(d.getTime())) return '';
  const time = formatClock(epochSec);
  const deltaSec = epochSec - nowMs / 1000;
  if (deltaSec <= 0) return 'now';

  // Under ~18h: time is enough (covers the 5-hour window), noting "tomorrow"
  // when it crosses midnight.
  if (deltaSec < 18 * 3600) {
    const now = new Date(nowMs);
    const crossesDay = d.getDate() !== now.getDate() || d.getMonth() !== now.getMonth();
    return crossesDay ? `${time} tomorrow` : time;
  }

  // Days out: date + remaining days (covers the 7-day window).
  const inDays = Math.max(1, Math.round(deltaSec / 86400));
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${time} · in ${inDays}d`;
}

export function formatDuration(sec: number | null): string {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return '';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h${m}m` : `${m}m`;
}

// Codicon reflecting health band, used in the status bar text + tooltip.
export function healthIcon(score: number | null): string {
  if (score == null) return '$(circle-outline)';
  if (score >= 85) return '$(pass-filled)';
  if (score >= 75) return '$(info)';
  if (score >= 50) return '$(warning)';
  return '$(error)';
}

// ---- status bar item texts (two adjacent items) ----

// The extension is running but the Token Optimizer CLI plugin has never written
// data on this machine — surface the install funnel instead of a bare "--%".
export function isFunnel(s: Snapshot): boolean {
  return !s.hasData && !s.pluginDetected;
}

export function primaryItemText(s: Snapshot): string {
  if (isFunnel(s)) return '$(rocket) Save tokens';
  const fill = s.fillPct != null ? `${s.fillPct}%` : '--%';
  if (!s.contextQ) return `$(pulse) ${fill}`;
  return `$(pulse) ${fill}  ${healthIcon(s.contextQ.score)} ${s.contextQ.grade}`;
}

export function secondaryItemText(s: Snapshot): string {
  const parts: string[] = [];
  if (s.eff) parts.push(`Eff ${s.eff.grade}`);
  const five = s.rateLimits?.fiveHour;
  if (five) {
    const status = usageStatus(five, s.rateLimitsStale);
    const tag = status === 'estimated' ? ' est' : status === 'stale' ? ' cached' : '';
    parts.push(`5h ${Math.round(five.usedPercentage)}%${tag}`);
  }
  if (parts.length === 0) return '';
  return `$(dashboard) ${parts.join('  ')}`;
}

// ---- rich hover tooltip (markdown) ----

const DASHBOARD_CMD = 'command:tokenOptimizer.openDashboard';
const INSTALL_CMD = 'command:tokenOptimizer.showStatus';

export function buildTooltip(s: Snapshot, opts: RenderOptions): string {
  const lines: string[] = [];
  const title = s.model
    ? `**Token Optimizer** · ${escapeMd(s.model)}${s.effort ? ` · ${escapeMd(s.effort)}` : ''}`
    : '**Token Optimizer**';
  lines.push(title);
  lines.push('');

  if (isFunnel(s)) {
    lines.push('_Save 15–20% on every session — three levels of optimization, quality kept intact._');
    lines.push('');
    lines.push('Scales with use: ~**$150/mo** light → up to **~$1,900/mo** power (API-equivalent, metered on real data).');
    lines.push('');
    lines.push(`**[Install Token Optimizer →](${INSTALL_CMD})**`);
    return lines.join('\n');
  }

  if (!s.hasData) {
    lines.push('_No active Claude Code session detected yet._');
    lines.push('');
    lines.push(`[Open dashboard](${DASHBOARD_CMD})`);
    return lines.join('\n');
  }

  lines.push('| | |');
  lines.push('|---|---|');

  if (s.fillPct != null) {
    const src = s.fillSource === 'jsonl' ? ' _(panel)_' : '';
    lines.push(`| Context | \`${fillBar(s.fillPct)}\` ${s.fillPct}%${src} |`);
  }
  if (s.contextQ) {
    lines.push(`| ContextQ | ${escapeMd(String(s.contextQ.grade))} (${escapeMd(String(s.contextQ.score))})${s.contextQ.stale ? ' _(cached)_' : ''} |`);
  }
  if (s.eff) {
    lines.push(`| Efficiency | ${escapeMd(String(s.eff.grade))} (${escapeMd(String(s.eff.score))}) |`);
  }
  // Say so when fill is present but scores aren't yet — reads as "pending" not "broken".
  if (qualityPending(s)) {
    lines.push(`| ContextQ / Eff | _warming up…_ |`);
  }

  const warn = warningParts(s);
  if (warn.length) lines.push(`| Warnings | ${warn.join(', ')} |`);

  if (s.compactions != null) {
    if (s.compactions > 0) {
      const loss = s.compactionLossPct != null ? ` (~${s.compactionLossPct}% lost)` : '';
      lines.push(`| Compactions | ${s.compactions}${loss} |`);
    } else {
      lines.push(`| Compactions | 0 |`);
    }
  }

  const dur = formatDuration(s.durationSec);
  if (dur) lines.push(`| Duration | ${dur} |`);

  if (s.agents.length > 0) {
    const agentStr = s.agents
      .map((a) => {
        const base = `${escapeMd(a.model)}:${escapeMd(a.description)}`.trim();
        return a.elapsed ? `${base} (${a.elapsed})` : base;
      })
      .join(', ');
    lines.push(`| Agents | ${agentStr} |`);
  }

  // Usage limits
  const five = s.rateLimits?.fiveHour;
  const seven = s.rateLimits?.sevenDay;
  if (five) {
    lines.push(`| 5h limit | ${usageWindowText(five, opts.nowMs, s.rateLimitsStale)} |`);
  }
  if (seven) {
    lines.push(`| 7d limit | ${usageWindowText(seven, opts.nowMs, s.rateLimitsStale)} |`);
  }

  // Honesty: with no folder open there's no way to scope to THIS window's
  // session, so the data is a best-effort global guess. Say so plainly.
  if (!s.scoped) {
    lines.push('');
    lines.push('⚠️ _No folder open — showing the most recent session globally. Open a folder so this reflects this window\'s session._');
  }

  lines.push('');
  lines.push(`[Open dashboard](${DASHBOARD_CMD})`);
  return lines.join('\n');
}

// Structured, render-ready view model for the expanded webview panel. Computing
// it here (not in webview JS) keeps all formatting logic in one tested place.
export interface PanelModel {
  hasData: boolean;
  funnel: boolean; // extension present but CLI plugin missing -> show install funnel
  scoped: boolean;
  model: string | null;
  effort: string | null;
  fillPct: number | null;
  fillBar: string;
  fillSource: string | null;
  contextQ: { score: number; grade: string; stale: boolean } | null;
  eff: { score: number; grade: string } | null;
  qualityPending: boolean;
  warnings: string[];
  compactions: { count: number; lossPct: number | null } | null;
  duration: string;
  agents: string[];
  fiveHour: PanelUsageWindow | null;
  sevenDay: PanelUsageWindow | null;
}

export interface PanelUsageWindow {
  pct: number;
  reset: string;
  status: UsageLimitDisplayStatus;
  age: string;
  detail: string;
}

export function buildPanelModel(s: Snapshot, opts: RenderOptions): PanelModel {
  return {
    hasData: s.hasData,
    funnel: isFunnel(s),
    scoped: s.scoped,
    model: s.model,
    effort: s.effort,
    fillPct: s.fillPct,
    fillBar: fillBar(s.fillPct, 20),
    fillSource: s.fillSource,
    contextQ: s.contextQ,
    eff: s.eff,
    qualityPending: qualityPending(s),
    warnings: warningParts(s),
    compactions:
      s.compactions != null ? { count: s.compactions, lossPct: s.compactionLossPct } : null,
    duration: formatDuration(s.durationSec),
    agents: s.agents.map((a) => `${a.model}:${a.description}${a.elapsed ? ` (${a.elapsed})` : ''}`),
    fiveHour: s.rateLimits?.fiveHour ? panelUsageWindow(s.rateLimits.fiveHour, opts.nowMs, s.rateLimitsStale) : null,
    sevenDay: s.rateLimits?.sevenDay ? panelUsageWindow(s.rateLimits.sevenDay, opts.nowMs, s.rateLimitsStale) : null,
  };
}

// Fill arrives before the quality hook writes scores for a fresh session.
function qualityPending(s: Snapshot): boolean {
  return s.hasData && !s.contextQ && !s.eff;
}

function warningParts(s: Snapshot): string[] {
  const parts: string[] = [];
  if (s.fillWarning) {
    const bang = s.fillWarning.level === 'CRITICAL' ? '!' : '';
    parts.push(`Fill ${escapeMd(String(s.fillWarning.value))}%${bang}`);
  }
  if (s.toolWarning) {
    const bang = s.toolWarning.level === 'CRITICAL' ? '!' : '';
    parts.push(`Tools ${escapeMd(String(s.toolWarning.value))}${bang}`);
  }
  return parts;
}

function usageStatus(w: RateWindow, fallbackStale: boolean): UsageLimitStatus {
  return w.freshness ?? (fallbackStale ? 'stale' : 'verified');
}

function usageWindowText(w: RateWindow, nowMs: number, fallbackStale: boolean): string {
  const status = usageStatus(w, fallbackStale);
  const reset = usageReset(w, nowMs, status);
  const parts = [`${Math.round(w.usedPercentage)}%`];
  if (reset) parts.push(`resets ${reset}`);
  const age = formatAge(w.ageSeconds ?? null);
  if (status === 'stale') parts.push(age ? `cached ${age} ago` : 'cached');
  else parts.push(status);
  return parts.join(' · ');
}

function panelUsageWindow(w: RateWindow, nowMs: number, fallbackStale: boolean): PanelUsageWindow {
  const status = usageStatus(w, fallbackStale);
  const age = status === 'stale' ? formatAge(w.ageSeconds ?? null) : '';
  return {
    pct: Math.round(w.usedPercentage),
    reset: usageReset(w, nowMs, status),
    status: displayStatus(status),
    age: age ? `updated ${age} ago` : '',
    detail: usageDetail(status, age),
  };
}

function usageReset(w: RateWindow, nowMs: number, status: UsageLimitStatus): string {
  const reset = formatReset(w.resetsAt, nowMs);
  if (status === 'stale' && reset === 'now') return '';
  return reset;
}

function usageDetail(status: UsageLimitStatus, age: string): string {
  if (status === 'verified') return 'Captured from Claude statusline';
  if (status === 'estimated') return 'Estimated from local transcript usage';
  return age ? `Last captured value, updated ${age} ago` : 'Last captured value';
}

function displayStatus(status: UsageLimitStatus): UsageLimitDisplayStatus {
  return status === 'stale' ? 'cached' : status;
}

function formatAge(ageSeconds: number | null): string {
  if (ageSeconds == null || !Number.isFinite(ageSeconds) || ageSeconds < 0) return '';
  if (ageSeconds < 90) return `${Math.round(ageSeconds)}s`;
  const minutes = Math.round(ageSeconds / 60);
  if (minutes < 90) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}
