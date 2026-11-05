// Shared data shapes for the Token Optimizer companion.
// Kept free of any `vscode` import so every consumer is unit-testable off-host.

export type UsageLimitStatus = 'verified' | 'estimated' | 'stale';
export type UsageLimitDisplayStatus = 'verified' | 'estimated' | 'cached';
export type UsageLimitSource = 'statusline' | 'transcript-estimate';

export interface RateWindow {
  usedPercentage: number;
  resetsAt: number | null; // unix epoch seconds
  freshness?: UsageLimitStatus;
  source?: UsageLimitSource;
  ageSeconds?: number | null;
}

export interface RateLimits {
  fiveHour: RateWindow | null;
  sevenDay: RateWindow | null;
  timestamp: number; // ms since epoch — when this data was captured
  // statusline = authoritative sidecar; transcript-estimate = local estimate
  // calibrated from recent transcript usage.
  source: UsageLimitSource | 'mixed';
}

export interface AgentInfo {
  model: string;
  description: string;
  elapsed: string | null; // e.g. "4m30s", computed at snapshot time
}

export interface ContextQ {
  score: number;
  grade: string;
  stale: boolean;
}

export interface Warning {
  level: 'WARNING' | 'CRITICAL';
  value: number; // fill pct or tool count, depending on warning
}

// A normalized, render-ready view of the active Claude Code session.
export interface Snapshot {
  hasData: boolean;
  // false => the extension is running but the Token Optimizer CLI plugin has
  // never written data on any known runtime (~/.claude or ~/.copilot). Drives
  // the install funnel instead of the "waiting for a session" empty state.
  // Defaults true so a transient read error never nags an existing user.
  pluginDetected: boolean;
  model: string | null;
  effort: string | null; // 'lo' | 'med' | 'hi'
  fillPct: number | null;
  fillSource: 'live-fill' | 'quality' | 'jsonl' | null;
  contextQ: ContextQ | null;
  eff: { score: number; grade: string } | null;
  fillWarning: Warning | null;
  toolWarning: Warning | null;
  compactions: number | null;
  /** Measured post-compaction recovery status (score_version >= 2); null when unknown. */
  compactionRecovery: string | null;
  durationSec: number | null;
  agents: AgentInfo[];
  rateLimits: RateLimits | null;
  rateLimitsStale: boolean; // true when shown limits are older than the stale threshold
  scoped: boolean; // true when resolved via the window's workspace folder (vs global fallback)
}

export function emptySnapshot(): Snapshot {
  return {
    hasData: false,
    pluginDetected: true,
    model: null,
    effort: null,
    fillPct: null,
    fillSource: null,
    contextQ: null,
    eff: null,
    fillWarning: null,
    toolWarning: null,
    compactions: null,
    compactionRecovery: null,
    durationSec: null,
    agents: [],
    rateLimits: null,
    rateLimitsStale: false,
    scoped: false,
  };
}
