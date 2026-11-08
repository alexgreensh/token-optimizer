// Pure mapping of a Cursor hook tally (the per-conversation JSON the Cursor
// bridge writes under <cursor home>/token-optimizer/sessions/) into a Snapshot.
// vscode-free and unit-testable, mirroring cacheReader.ts's posture: defensive
// parsing, honest absence, never a fabricated value.
//
// Honesty contract (shared-interface contract v2 §4): a fill number may only
// come from a CURRENT host occupancy snapshot. On Cursor today the only
// recorded occupancy is the last preCompact entry — the peak that TRIGGERED
// that compaction, measured at the entry's own ts. After the compaction the
// real occupancy dropped, and later hook events bump only the tally's
// updated_at, so this number is history, not current fill. It is therefore
// shown as "last compaction" context with its measurement time, NEVER as the
// Context bar, and ContextQ is not derived from it. A lifetime token sum is
// never a fill numerator either. Cursor exposes no per-session usage limits
// and no cost data, so rateLimits stay null and nothing here derives dollars.
import { Snapshot, emptySnapshot } from './types';

// Matches statusline.js's one-week sanity bound on session durations.
const MAX_DURATION_SECONDS = 604800;

// Shown in the tooltip/panel so Cursor data is never mistaken for the fuller
// Claude pipeline's output.
export const CURSOR_SOURCE_LABEL =
  'Cursor hook tally · current context fill unknown · no native token totals';

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

function safeNumber(v: any): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// The most recent preCompact entry's recorded occupancy: the peak that
// triggered that compaction, with its OWN measurement timestamp (never the
// tally's updated_at, which any later hook event refreshes). Historical only.
function lastCompaction(compactions: any): { pct: number; ts: number | null } | null {
  if (!Array.isArray(compactions)) return null;
  for (let i = compactions.length - 1; i >= 0; i--) {
    const entry = compactions[i];
    if (!isPlainObject(entry)) continue;
    const ts = safeNumber(entry.ts);
    const used = safeNumber(entry.context_tokens);
    const window = safeNumber(entry.context_window_size);
    if (used != null && window != null && used > 0 && window > 0) {
      return { pct: clampPct((used / window) * 100), ts };
    }
    const pct = safeNumber(entry.context_usage_percent);
    if (pct != null && pct > 0) return { pct: clampPct(pct), ts };
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

  // The last compaction's occupancy is HISTORY (see header): surfaced as a
  // labeled fact with its own measurement time, never as the current Context
  // bar, and ContextQ stays null — no current-quality score can be derived
  // from a pre-compaction peak.
  snap.lastCompaction = lastCompaction(tally.compactions);

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
