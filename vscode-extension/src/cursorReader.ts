// Pure mapping of a Cursor hook tally (the per-conversation JSON the Cursor
// bridge writes under <cursor home>/token-optimizer/sessions/) into a Snapshot.
// vscode-free and unit-testable, mirroring cacheReader.ts's posture: defensive
// parsing, honest absence, never a fabricated value.
//
// Honesty contract (shared-interface contract v2 §4): fill comes ONLY from a
// real host occupancy snapshot. On Cursor today that is the last preCompact
// entry's context_tokens / context_window_size (or its context_usage_percent).
// A lifetime token sum is NEVER a fill numerator. When no snapshot exists the
// fill is omitted, not estimated. Cursor exposes no per-session usage limits
// and no cost data, so rateLimits stay null and nothing here derives dollars.
import { Snapshot, emptySnapshot } from './types';

// Mirrors the staleness rule for Claude's quality cache (cacheReader.ts): a
// tally untouched for 5 minutes shows its grade as cached.
const STALE_TALLY_SECONDS = 300;
// Matches statusline.js's one-week sanity bound on session durations.
const MAX_DURATION_SECONDS = 604800;

// Shown in the tooltip/panel so Cursor data is never mistaken for the fuller
// Claude pipeline's output.
export const CURSOR_SOURCE_LABEL =
  'Cursor hook tally · ContextQ from context fill only · no native token totals';

export interface CursorSnapshotOptions {
  nowMs: number;
  scoped: boolean; // resolved via the window's workspace folder?
}

function isPlainObject(v: any): boolean {
  return v != null && typeof v === 'object' && !Array.isArray(v);
}

function clampPct(n: number): number {
  return Math.max(0, Math.min(100, Math.round(n)));
}

// Same grade letters the extension uses everywhere (cacheReader.gradeFor).
function gradeFor(score: number): string {
  if (score >= 90) return 'S';
  if (score >= 80) return 'A';
  if (score >= 70) return 'B';
  if (score >= 55) return 'C';
  if (score >= 40) return 'D';
  return 'F';
}

// Fill-only score, mirroring cursor_session.py's documented fallback formula
// (score = 100 - fill*50) so the status bar and the CLI statusline agree.
function fillScore(pct: number): number {
  return Math.max(0, Math.round(100 - pct / 2));
}

function safeNumber(v: any): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// The most recent preCompact entry carrying usable occupancy numbers.
function lastCompactionFill(compactions: any): number | null {
  if (!Array.isArray(compactions)) return null;
  for (let i = compactions.length - 1; i >= 0; i--) {
    const entry = compactions[i];
    if (!isPlainObject(entry)) continue;
    const used = safeNumber(entry.context_tokens);
    const window = safeNumber(entry.context_window_size);
    if (used != null && window != null && used > 0 && window > 0) {
      return clampPct((used / window) * 100);
    }
    const pct = safeNumber(entry.context_usage_percent);
    if (pct != null && pct > 0) return clampPct(pct);
  }
  return null;
}

// The tally records per-model turn COUNTS with no recency, so after a
// mid-session model switch the most-frequent model may not be the current one.
// Only a single recorded model is a safe display; anything richer needs a
// last_model tally field (requested from the bridge owner).
function primaryModel(models: any): string | null {
  if (!isPlainObject(models)) return null;
  const names = Object.keys(models).filter((k) => k);
  return names.length === 1 ? names[0] : null;
}

export function buildCursorSnapshot(tally: any, opts: CursorSnapshotOptions): Snapshot {
  const snap = emptySnapshot();
  snap.runtimeName = 'cursor';
  snap.sourceLabel = CURSOR_SOURCE_LABEL;
  snap.scoped = opts.scoped;

  if (!isPlainObject(tally)) return snap;
  const conversationId = typeof tally.conversation_id === 'string' ? tally.conversation_id : '';
  if (!conversationId) return snap;

  snap.hasData = true;
  snap.model = primaryModel(tally.models);

  const nowSec = opts.nowMs / 1000;
  const updatedAt = safeNumber(tally.updated_at);
  const stale = updatedAt == null || nowSec - updatedAt > STALE_TALLY_SECONDS;

  // Context fill: last preCompact occupancy snapshot only (see header).
  const fill = lastCompactionFill(tally.compactions);
  if (fill != null) {
    snap.fillPct = fill;
    snap.fillSource = 'cursor-tally';
    const score = fillScore(fill);
    snap.contextQ = { score, grade: gradeFor(score), stale };
  }

  if (Array.isArray(tally.compactions)) {
    snap.compactions = tally.compactions.length;
  }

  // Duration: live sessions tick from first_ts; finalized sessions show their
  // recorded span. Never negative, never absurd.
  const firstTs = safeNumber(tally.first_ts);
  if (firstTs != null && firstTs > 0) {
    const end = tally.final && updatedAt != null ? updatedAt : nowSec;
    const dur = Math.floor(end - firstTs);
    if (dur > 0 && dur < MAX_DURATION_SECONDS) snap.durationSec = dur;
  }

  // The bridge's context-growth nudge level (thresholds 30/80 tool calls) is
  // the closest Cursor analogue to the Claude tool-fatigue warning.
  const nudgeLevel = safeNumber(tally.nudge_level) ?? 0;
  const toolCalls = safeNumber(tally.tool_calls) ?? 0;
  if (nudgeLevel >= 2) {
    snap.toolWarning = { level: 'CRITICAL', value: toolCalls };
  } else if (nudgeLevel >= 1) {
    snap.toolWarning = { level: 'WARNING', value: toolCalls };
  }

  // rateLimits stay null: Cursor exposes no per-session limit data. Eff stays
  // null: the tally feeds no efficiency signal yet.
  return snap;
}
