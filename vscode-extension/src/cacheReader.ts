// Pure parsing of Token Optimizer's on-disk cache files into a Snapshot.
// Takes already-read file contents (strings) so it stays vscode-free and fully
// unit-testable; the actual fs reads live in dataSource.
import { Snapshot, RateLimits, AgentInfo, RateWindow, emptySnapshot } from './types';
import { parseRateWindow } from './rateWindow';
import { sanitizeSessionId } from './paths';
import { windowForModel } from './jsonlTail';

const STALE_QUALITY_SECONDS = 300; // mirror statusline.js: score older than 5min => stale

export interface RawInputs {
  qualityJson: string | null;
  liveFillJson: string | null;
  rateLimitsJson: string | null;
  estimatedRateLimits?: RateLimits | null;
  jsonlTokens: number | null; // raw context tokens from the transcript tail
  jsonlModel: string | null;
  effort: string | null;
  sessionId: string | null; // to confirm the cache belongs to the active session
  scoped: boolean; // resolved via the window's workspace folder?
  nowMs: number;
  staleAfterSeconds: number; // for rate-limit staleness labeling
}

const MAX_AGENT_DESC = 120;

function isPlainObject(v: any): boolean {
  return v != null && typeof v === 'object' && !Array.isArray(v);
}

function clampScore(n: number): number {
  return Math.max(0, Math.min(100, Math.round(n)));
}

// Match statusline.js agent-elapsed formatting: "4m30s" / "45s".
function elapsedSince(startTime: any, nowMs: number): string | null {
  if (typeof startTime !== 'string') return null;
  const start = Date.parse(startTime);
  if (!Number.isFinite(start)) return null;
  const secs = Math.floor((nowMs - start) / 1000);
  if (secs < 0) return null;
  return secs >= 60 ? `${Math.floor(secs / 60)}m${secs % 60}s` : `${secs}s`;
}

function safeParse(json: string | null): any {
  if (!json) return null;
  try {
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function gradeFor(score: number): string {
  if (score >= 90) return 'S';
  if (score >= 80) return 'A';
  if (score >= 70) return 'B';
  if (score >= 55) return 'C';
  if (score >= 40) return 'D';
  return 'F';
}

export function buildSnapshot(inputs: RawInputs): Snapshot {
  const snap = emptySnapshot();
  const q = safeParse(inputs.qualityJson);
  const live = safeParse(inputs.liveFillJson);
  const sidecarRateLimits = parseRateLimitsSidecar(
    inputs.rateLimitsJson,
    inputs.nowMs,
    inputs.staleAfterSeconds
  );

  snap.model = inputs.jsonlModel;
  snap.effort = inputs.effort;
  snap.scoped = inputs.scoped;

  // ---- Context fill ----
  // The only fully reliable per-session fill = the transcript's own token count
  // divided by the REAL context window. The token count comes from the JSONL
  // tail (accurate per-session); the window size comes from the quality-cache's
  // `model_context_window` (so 1M-context sessions aren't mis-scored against
  // 200k). We do NOT trust:
  //   - the global live-fill.json unless its session_id matches (it's the last
  //     terminal's fill, leaks across windows), and
  //   - the quality-cache's own fill_pct, which measure.py can write as 0 when it
  //     can't attribute fill (same global-leak problem on the plugin side).
  // Order: matched live-fill (authoritative) → JSONL tokens ÷ window → qc.fill_pct.
  const liveSessionId =
    live && live.session_id ? sanitizeSessionId(String(live.session_id)) : null;
  const liveMatchesSession =
    live &&
    typeof live.used_percentage === 'number' &&
    !!liveSessionId &&
    !!inputs.sessionId &&
    liveSessionId === inputs.sessionId;
  const windowTokens =
    isPlainObject(q) &&
    typeof q.model_context_window === 'number' &&
    Number.isFinite(q.model_context_window) &&
    q.model_context_window > 0
      ? q.model_context_window
      : windowForModel(inputs.jsonlModel);
  const jsonlFill =
    inputs.jsonlTokens != null && Number.isFinite(inputs.jsonlTokens)
      ? clampScore((inputs.jsonlTokens / windowTokens) * 100)
      : null;
  // Only trust a quality-cache fill that rounds above 0 — measure.py writes 0
  // when it couldn't attribute fill, which we'd rather skip than display.
  const qcFillRounded =
    isPlainObject(q) && typeof q.fill_pct === 'number' && Number.isFinite(q.fill_pct)
      ? clampScore(q.fill_pct)
      : null;
  const qcFill = qcFillRounded != null && qcFillRounded > 0 ? qcFillRounded : null;
  if (liveMatchesSession) {
    snap.fillPct = clampScore(live.used_percentage);
    snap.fillSource = 'live-fill';
  } else if (jsonlFill != null) {
    snap.fillPct = jsonlFill;
    snap.fillSource = 'jsonl';
  } else if (qcFill != null) {
    snap.fillPct = qcFill;
    snap.fillSource = 'quality';
  }

  // ---- Quality scores ----
  // Require a real object: a JSON array/scalar is truthy but carries no fields,
  // and would otherwise flip hasData on with everything null ("connected" lie).
  if (isPlainObject(q)) {
    snap.hasData = true;

    // Only trust per-session details (duration, agents) when the cache actually
    // belongs to the resolved session — mirrors statusline.js cacheMatchesSession.
    const cacheMatchesSession =
      typeof q.session_file === 'string' &&
      !!inputs.sessionId &&
      q.session_file.includes(inputs.sessionId);

    const rh = typeof q.resource_health === 'number' ? q.resource_health : q.score;
    if (typeof rh === 'number' && Number.isFinite(rh)) {
      const score = clampScore(rh);
      const grade = q.resource_health_grade || q.grade || gradeFor(score);
      let stale = false;
      const ts = q.timestamp ? Date.parse(q.timestamp) : NaN;
      if (isNaN(ts) || (inputs.nowMs - ts) / 1000 > STALE_QUALITY_SECONDS) stale = true;
      snap.contextQ = { score, grade, stale };
    }

    if (typeof q.session_efficiency === 'number' && Number.isFinite(q.session_efficiency)) {
      const score = clampScore(q.session_efficiency);
      snap.eff = { score, grade: q.session_efficiency_grade || gradeFor(score) };
    }

    if (q.fill_warning && q.fill_warning.level) {
      snap.fillWarning = {
        level: q.fill_warning.level,
        value: Math.round(q.fill_warning.fill_pct || 0),
      };
    }

    if (q.tool_call_warning && q.tool_call_warning.level) {
      snap.toolWarning = {
        level: q.tool_call_warning.level,
        value: q.tool_calls || 0,
      };
    }

    if (typeof q.compactions === 'number') {
      snap.compactions = q.compactions;
      // Measured recovery status only (score_version >= 2). Compaction count
      // alone never implies a fixed loss percentage.
      const rs = q.recovery_status;
      snap.compactionRecovery = typeof rs === 'string' && rs ? rs : null;
    }

    if (cacheMatchesSession && typeof q.session_start_ts === 'number' && q.session_start_ts > 0) {
      const elapsed = Math.floor(inputs.nowMs / 1000 - q.session_start_ts);
      if (elapsed > 0 && elapsed < 604800) snap.durationSec = elapsed;
    }

    if (cacheMatchesSession && Array.isArray(q.active_agents)) {
      snap.agents = q.active_agents
        .filter((a: any) => a && a.status === 'running')
        .slice(0, 3)
        .map(
          (a: any): AgentInfo => ({
            model: stripControl(String(a.model || '?')).slice(0, MAX_AGENT_DESC),
            description: stripControl(String(a.description || '')).slice(0, MAX_AGENT_DESC),
            elapsed: elapsedSince(a.start_time, inputs.nowMs),
          })
        );
    }
  }

  if (snap.fillPct != null) snap.hasData = true;

  // ---- Rate limits ----
  // Fresh sidecar = verified. Stale sidecar can be replaced by a local
  // transcript estimate; otherwise show the stale value with age.
  const sidecarHasStale = !!sidecarRateLimits && anyStale(sidecarRateLimits);
  if (sidecarRateLimits && (!sidecarHasStale || !inputs.estimatedRateLimits)) {
    snap.rateLimits = sidecarRateLimits;
    snap.rateLimitsStale = sidecarHasStale;
    snap.hasData = true;
  } else if (inputs.estimatedRateLimits) {
    snap.rateLimits = inputs.estimatedRateLimits;
    snap.rateLimitsStale = false;
    snap.hasData = true;
  }

  return snap;
}

export function parseRateLimitsSidecar(
  json: string | null,
  nowMs: number,
  staleAfterSeconds: number
): RateLimits | null {
  const rl = safeParse(json);
  if (!rl) return null;
  const fiveHour = parseRateWindow(rl.five_hour);
  const sevenDay = parseRateWindow(rl.seven_day);
  if (!fiveHour && !sevenDay) return null;
  const ts = typeof rl.timestamp === 'number' && Number.isFinite(rl.timestamp) ? rl.timestamp : 0;
  const ageSec = ts ? Math.max(0, Math.round((nowMs - ts) / 1000)) : null;
  const stale = ageSec == null || ageSec > staleAfterSeconds;
  return {
    fiveHour: annotateWindow(fiveHour, stale, ageSec),
    sevenDay: annotateWindow(sevenDay, stale, ageSec),
    timestamp: ts,
    source: 'statusline',
  };
}

function annotateWindow(
  window: RateWindow | null,
  stale: boolean,
  ageSeconds: number | null
): RateWindow | null {
  if (!window) return null;
  return {
    ...window,
    freshness: stale ? 'stale' : 'verified',
    source: 'statusline',
    ageSeconds,
  };
}

function anyStale(limits: RateLimits): boolean {
  return [limits.fiveHour, limits.sevenDay].some((w) => w?.freshness === 'stale');
}

function stripControl(s: string): string {
  // Drop ANSI escapes and control chars an agent description might carry.
  // eslint-disable-next-line no-control-regex
  return s.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/[\x00-\x1f]/g, '');
}
