// Unit tests for the Cursor tally reader (U8, R12).
// Pins the honesty contract: fill only from preCompact occupancy snapshots,
// grade derived from fill alone, no rate limits, no fabricated fields.
import { describe, it, expect } from 'vitest';
import { buildCursorSnapshot, CURSOR_SOURCE_LABEL } from '../src/cursorReader';

const NOW = 1_700_000_600_000; // ms

function tally(overrides: any = {}): any {
  return {
    conversation_id: 'conv-abc123',
    first_ts: 1_700_000_000,
    updated_at: 1_700_000_600,
    final: false,
    turns: 5,
    tool_calls: 9,
    models: { 'claude-sonnet': 7 },
    compactions: [
      { context_tokens: 80_000, context_window_size: 200_000, context_usage_percent: 40, ts: 1_700_000_500 },
    ],
    cwd: '/repo/demo',
    workspace_roots: ['/repo/demo'],
    ...overrides,
  };
}

describe('buildCursorSnapshot', () => {
  it('maps a healthy tally: model, fill, grade, compactions, duration', () => {
    const snap = buildCursorSnapshot(tally(), { nowMs: NOW, scoped: true });
    expect(snap.hasData).toBe(true);
    expect(snap.runtimeName).toBe('cursor');
    expect(snap.sourceLabel).toBe(CURSOR_SOURCE_LABEL);
    expect(snap.model).toBe('claude-sonnet'); // sole recorded model
    expect(snap.fillPct).toBe(40);
    expect(snap.fillSource).toBe('cursor-tally');
    // fill-only score: 100 - 40/2 = 80 -> grade A per the extension's scale
    expect(snap.contextQ).toEqual({ score: 80, grade: 'A', stale: false });
    expect(snap.compactions).toBe(1);
    expect(snap.durationSec).toBe(600);
    expect(snap.toolWarning).toBeNull();
    expect(snap.rateLimits).toBeNull(); // Cursor exposes no per-session limits
    expect(snap.eff).toBeNull();
  });

  it('prefers context_tokens/window over context_usage_percent', () => {
    const snap = buildCursorSnapshot(
      tally({
        compactions: [
          { context_tokens: 100_000, context_window_size: 200_000, context_usage_percent: 99 },
        ],
      }),
      { nowMs: NOW, scoped: true }
    );
    expect(snap.fillPct).toBe(50);
  });

  it('falls back to context_usage_percent when token fields are absent', () => {
    const snap = buildCursorSnapshot(
      tally({ compactions: [{ context_usage_percent: 40.4 }] }),
      { nowMs: NOW, scoped: true }
    );
    expect(snap.fillPct).toBe(40);
  });

  it('uses the LAST usable compaction entry', () => {
    const snap = buildCursorSnapshot(
      tally({
        compactions: [
          { context_tokens: 10_000, context_window_size: 200_000 },
          { context_tokens: 0, context_window_size: 0 }, // unusable: skipped
          { context_tokens: 150_000, context_window_size: 200_000 },
        ],
      }),
      { nowMs: NOW, scoped: true }
    );
    expect(snap.fillPct).toBe(75);
  });

  it('omits fill and ContextQ when no occupancy snapshot exists', () => {
    const snap = buildCursorSnapshot(tally({ compactions: [] }), { nowMs: NOW, scoped: true });
    expect(snap.hasData).toBe(true); // the session is real
    expect(snap.fillPct).toBeNull();
    expect(snap.fillSource).toBeNull();
    expect(snap.contextQ).toBeNull();
  });

  it('never derives fill from lifetime token fields', () => {
    // A tally carrying cumulative-ish numbers but no compaction snapshot must
    // show no fill: lifetime sums are not occupancy (contract v2 §4).
    const snap = buildCursorSnapshot(
      tally({ compactions: [], total_input_tokens: 190_000, input_tokens: 190_000 }),
      { nowMs: NOW, scoped: true }
    );
    expect(snap.fillPct).toBeNull();
    expect(snap.contextQ).toBeNull();
  });

  it('marks the grade stale when the tally is old', () => {
    const snap = buildCursorSnapshot(tally({ updated_at: 1_700_000_600 - 301 }), {
      nowMs: NOW,
      scoped: true,
    });
    expect(snap.contextQ?.stale).toBe(true);
  });

  it('maps nudge levels to tool warnings', () => {
    const warn = buildCursorSnapshot(tally({ nudge_level: 1, tool_calls: 35 }), {
      nowMs: NOW,
      scoped: true,
    });
    expect(warn.toolWarning).toEqual({ level: 'WARNING', value: 35 });
    const crit = buildCursorSnapshot(tally({ nudge_level: 2, tool_calls: 85 }), {
      nowMs: NOW,
      scoped: true,
    });
    expect(crit.toolWarning).toEqual({ level: 'CRITICAL', value: 85 });
  });

  it('finalized sessions show their recorded span, not a ticking duration', () => {
    const snap = buildCursorSnapshot(
      tally({ final: true, first_ts: 1_700_000_000, updated_at: 1_700_000_200 }),
      { nowMs: NOW + 86_400_000, scoped: true }
    );
    expect(snap.durationSec).toBe(200);
  });

  it('rejects corrupt input cleanly', () => {
    for (const bad of [null, undefined, 42, 'x', [], {}, { conversation_id: '' }]) {
      const snap = buildCursorSnapshot(bad, { nowMs: NOW, scoped: false });
      expect(snap.hasData).toBe(false);
      expect(snap.runtimeName).toBe('cursor'); // empty state still names the runtime
    }
  });

  it('tolerates missing optional fields without fabricating them', () => {
    const snap = buildCursorSnapshot(
      { conversation_id: 'conv-bare01' },
      { nowMs: NOW, scoped: false }
    );
    expect(snap.hasData).toBe(true);
    expect(snap.model).toBeNull();
    expect(snap.fillPct).toBeNull();
    expect(snap.compactions).toBeNull();
    expect(snap.durationSec).toBeNull();
    expect(snap.toolWarning).toBeNull();
  });

  it('shows no model when the tally records more than one (no recency data)', () => {
    // models{} is turn counts with no timestamps: after a mid-session switch
    // the most-frequent model may not be current, so no single name is safe.
    const snap = buildCursorSnapshot(
      tally({ models: { 'claude-sonnet': 7, 'gpt-5': 2 } }),
      { nowMs: NOW, scoped: true }
    );
    expect(snap.model).toBeNull();
    expect(snap.hasData).toBe(true);
  });
});
