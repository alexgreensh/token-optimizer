/**
 * Realized savings engine for OpenCode — the CURRENT-VOLUME COUNTERFACTUAL.
 *
 * Parity with measure.py `_estimate_before_after_savings` and the OpenClaw port.
 * Prices the FROZEN pre-TO typical session (the per-session mean of the early
 * `before` window) two ways:
 *   OLD WAY / session = baseline mix + the typical session's own cache pattern.
 *                       Depends only on frozen inputs + prices -> a STABLE baseline
 *                       that does NOT re-roll with this period's workload.
 *   NOW / session     = the SAME frozen tokens, pool redistributed at the CURRENT
 *                       token classes and priced at the CURRENT mix. Changes reflect the model mix.
 * Per-session transformation = old_cps − now_cps. Monthly = that x the session count,
 * so volume re-enters ONLY as a multiplier — never as a confounder of the baseline.
 *
 * OpenCode persists every session to trends.db (session_log) with a pre-computed
 * cost_usd, but the counterfactual reprices the SAME volume at a DIFFERENT mix,
 * which the stored cost cannot supply. So we add a minimal per-class rate card
 * (pricing.ts) and price both arms from token volume directly.
 *
 * Three non-overlapping pools sum to the headline:
 *   1. Main model mix comparison (billed session_log volume). NO winsorization /
 *      outlier drop — heavy sessions are real volume that genuinely cost more
 *      at the baseline mix; dropping them is a pure definitional undercount.
 *   2. Subagent (sidechain) routing — Claude-only. OpenCode has no Claude-style
 *      sidechains (no is_sidechain column, no subagent transcripts), so this
 *      pool is 0. DOCUMENTED GAP.
 *   3. Compression add-back — tokens TO removed from context (tool_archive,
 *      structure_map, resume_lean, checkpoint_restore, delta_read). Real volume
 *      the old way would have re-read. Directly-metered (savings_events),
 *      repriced to the baseline input mix. Disjoint from the billed pool.
 *   4. Verbosity-steer add-back — estimated output tokens never produced due
 *      to lean-output conciseness nudges, repriced at the baseline output mix. The
 *      main counterfactual holds output volume constant, so this is a separate
 *      lever. Estimated tier (trigger observed, magnitude not metered).
 *
 * BASELINE = the frozen EARLY-window mix + pool cache-hit, used ONLY for
 * efficiency anchors (never volume). NO 95% Opus floor: OpenCode is a
 * non-Anthropic runtime, so the before-arm is priced at the user's OWN measured
 * baseline mix — never fabricated Opus they never ran.
 */
import { TrendsStore } from "./storage/trends.js";
import { price, price_cw, inputRatePerMTok, knownModelRate, type ModelMix } from "./pricing.js";

// Constants mirror the other platforms' baseline tunables.
const BASELINE_ONBOARDING_DAYS = 1;
const BASELINE_EARLY_WINDOW_DAYS = 30;
const BASELINE_MIN_STABLE_SESSIONS = 30;
const AFTER_MIN_SESSIONS = 10;
const DAY_MS = 86_400_000;
// Quality gates: exclude empty/transient sessions that inflate count and skew
// per-session costs (mirrors measure.py _MIN_INPUT_TOKENS / _MIN_DURATION_MINUTES).
const MIN_INPUT_TOKENS = 1000;
const MIN_DURATION_SECONDS = 60;

interface SessionRec {
  ts: number; // epoch ms
  model: string; // normalized pricing key
  fi: number; // fresh input
  cr: number; // cache read
  cw: number; // cache write
  out: number; // output
  cost: number; // stored cost_usd (display / not used in counterfactual math)
  durationSec: number; // session duration in seconds (quality gate)
  buckets: Array<{ model: string; fi: number; cr: number; cw: number; out: number }>;
  aggregateOnly: boolean;
}

function num(v: unknown): number {
  const n = typeof v === "number" ? v : parseFloat(String(v));
  return Number.isFinite(n) ? n : 0;
}

function toRec(row: Record<string, unknown>): SessionRec {
  const ts =
    num(row.created_at) > 0
      ? num(row.created_at) * 1000
      : Date.parse(String(row.date ?? "")) || 0;
  // Clamp every class >= 0 (corrupt-row protection, like measure.py's
  // _session_token_vector clamps). cache-write is a separate billed column in
  // session_log, kept distinct from fresh input.
  let raw: unknown = null;
  try { raw = typeof row.model_token_buckets === "string" ? JSON.parse(row.model_token_buckets) : row.model_token_buckets; } catch { /* use the legacy aggregate */ }
  const buckets: SessionRec["buckets"] = [];
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [model, value] of Object.entries(raw)) {
      if (!value || typeof value !== "object") continue;
      const b = value as Record<string, unknown>;
      buckets.push({ model, fi: Math.max(0, num(b.input)), cr: Math.max(0, num(b.cacheRead)),
        cw: Math.max(0, num(b.cacheWrite)), out: Math.max(0, num(b.output)) });
    }
  }
  const aggregateOnly = buckets.length === 0;
  if (aggregateOnly) buckets.push({ model: String(row.model ?? "unknown"), fi: Math.max(0, num(row.tokens_input)),
    cr: Math.max(0, num(row.tokens_cache_read)), cw: Math.max(0, num(row.tokens_cache_write)), out: Math.max(0, num(row.tokens_output)) });
  return {
    ts,
    model: String(row.model ?? "unknown"),
    fi: Math.max(0, num(row.tokens_input)),
    cr: Math.max(0, num(row.tokens_cache_read)),
    cw: Math.max(0, num(row.tokens_cache_write)),
    out: Math.max(0, num(row.tokens_output)),
    cost: num(row.cost_usd),
    durationSec: Math.max(0, num(row.duration_seconds)),
    buckets,
    aggregateOnly,
  };
}

/** Token-weighted model mix over a set of sessions (normalized pricing keys). */
function modelMix(recs: SessionRec[]): ModelMix {
  const byModel: Record<string, number> = {};
  let total = 0;
  for (const r of recs) {
    for (const b of r.buckets) {
      if (knownModelRate(b.model, "input") === null) continue;
      const t = b.fi + b.cr + b.cw + b.out;
      byModel[b.model] = (byModel[b.model] ?? 0) + t;
      total += t;
    }
  }
  if (total <= 0) return {};
  const mix: ModelMix = {};
  for (const [m, t] of Object.entries(byModel)) mix[m] = t / total;
  return mix;
}

type TokenClass = "fi" | "cr" | "cw" | "out";

/** Each token class has its own model distribution and price. */
function classMix(recs: SessionRec[], klass: TokenClass, fallback: ModelMix, ceiling = Infinity): ModelMix {
  const counts: ModelMix = {};
  let total = 0;
  for (const r of recs) {
    for (const b of r.buckets) {
      const rate = knownModelRate(b.model, rateClass[klass]);
      if (rate === null || rate > ceiling) continue;
      counts[b.model] = (counts[b.model] ?? 0) + b[klass];
      total += b[klass];
    }
  }
  if (total <= 0) return fallback;
  for (const model of Object.keys(counts)) counts[model] /= total;
  return counts;
}

const rateClass = { fi: "input", cr: "cacheRead", cw: "cacheWrite", out: "output" } as const;

function hasEligibleUsage(session: SessionRec, ceilings: Record<TokenClass, number>): boolean {
  return session.buckets.some((bucket) => (["fi", "cr", "cw", "out"] as const).some((klass) => {
    const rate = knownModelRate(bucket.model, rateClass[klass]);
    return bucket[klass] > 0 && rate !== null && rate <= ceilings[klass];
  }));
}

function classPrice(fi: number, cr: number, cw: number, out: number, mixes: Record<TokenClass, ModelMix>): number {
  return price(fi, 0, 0, mixes.fi) + price(0, cr, 0, mixes.cr) +
    price_cw(cw, mixes.cw) + price(0, 0, out, mixes.out);
}

export interface SavingsBreakdownItem {
  key: string;
  label: string;
  monthlyUsd: number;
}

/** Progress toward a frozen baseline (displayed when ready=false). */
export interface BaselineBuilding {
  /** Sessions collected in the early window so far. */
  sessionsInWindow: number;
  /** Sessions needed before the baseline locks in. */
  sessionsNeeded: number;
  /** Length of the early-window in days (matches BASELINE_EARLY_WINDOW_DAYS). */
  earlyWindowDays: number;
  /** Calendar days until the early window closes (0 once it has closed). */
  daysLeft: number;
  /** ISO date of the user's very first tracked session. */
  firstDate: string;
}

export interface RealizedSavings {
  ready: boolean;
  status: string;
  monthlySavingsUsd: number;
  premiumTokens: number;
  premiumCostUsd: number;
  premiumShare: number;
  aggregateBaseline: boolean;
  unpricedBaselineTokens: number;
  unpricedCurrentTokens: number;
  unpricedEventTokens: number;
  unpricedEventCount: number;
  /** Monthly $ at the user's CURRENT model mix + cache pattern ("now" arm). */
  actualMonthlyUsd: number;
  /** Monthly $ at the user's BASELINE mix + cache pattern ("the old way" arm). */
  counterfactualMonthlyUsd: number;
  /** Transformation as a fraction: (counterfactual − actual) / counterfactual. */
  transformationPct: number;
  /**
   * Metered compression floor — tokens TO removed from context, repriced to the
   * baseline input mix. This is the PROVEN subset of compressionAddback and is
   * NEVER summed into the transformation headline. Render it as a separate card.
   */
  compressionMeasuredUsd: number;
  /**
   * RAW metered compression $ over the lookback window, BEFORE the 30/days
   * monthly scaling that compressionMeasuredUsd carries. The dashboard's
   * young-install guard renders this as the cumulative "so far" figure.
   * Same INVARIANT as compressionMeasuredUsd: never summed into the headline.
   */
  compressionMeasuredWindowUsd: number;
  /**
   * RAW metered compression TOKENS saved over the lookback window (the
   * `totalTokensSaved` from TrendsStore.getCompressionSavings). Window-summed,
   * NOT monthly-scaled. Exposed so the Tokens Saved card can show a
   * token-denominated SAVED figure alongside the SPENT model-mix token total
   * (both share the same window). Never summed into any $ headline.
   */
  compressionMeasuredWindowTokens: number;
  /** Estimated verbosity-steer $ before the baseline-output reprice. */
  verbosityMeasuredUsd: number;
  /**
   * RAW estimated verbosity-steer $ over the lookback window, BEFORE the
   * 30/days monthly scaling that verbosityMeasuredUsd carries. The dashboard's
   * action-savings card renders this as the period-total estimated figure
   * (the actual selected-period savings_events sum), never a monthly run-rate.
   * Same INVARIANT as compressionMeasuredWindowUsd: never summed into the
   * transformation headline.
   */
  verbosityMeasuredWindowUsd: number;
  /** Repriced verbosity-steer $ (estimated output reduction at baseline mix). */
  verbosityTransformationUsd: number;
  savingsPerSession: number;
  beforeCostPerSession: number;
  afterCostPerSession: number;
  /** Baseline session's native cache-read fraction (0..1) -- explains why identical
   * tokens+mix can price differently across the arms (the cache-hit pricing contradiction). */
  beforeCacheHit: number;
  /** Current pool cache-hit rate (0..1). */
  afterCacheHit: number;
  sessionsPerMonth: number;
  /**
   * Days of post-baseline history behind the monthly projection (floor of the
   * after-window span, clamped >= 1). Drives the dashboard's young-install
   * guard: < 30 -> show the measured cumulative ("so far") instead of the
   * "/mo" run-rate. 0 when not ready.
   */
  trackedDays: number;
  beforeMixLabel: string;
  afterMixLabel: string;
  cumulativeSavedUsd: number;
  installDate: string | null;
  breakdown: SavingsBreakdownItem[];
  /**
   * Populated when ready=false and the user has at least one session. Gives the
   * dashboard enough data to render a progress card + bar instead of a dead end.
   * null when there are zero sessions (no installDate yet).
   */
  baselineBuilding: BaselineBuilding | null;
}

function mixLabel(mix: ModelMix): string {
  const top = Object.entries(mix).sort((a, b) => b[1] - a[1])[0];
  return top ? `${Math.round(top[1] * 100)}% ${top[0]}` : "n/a";
}

const NOT_READY = (status: string): RealizedSavings => ({
  ready: false,
  status,
  monthlySavingsUsd: 0,
  premiumTokens: 0,
  premiumCostUsd: 0,
  premiumShare: 0,
  aggregateBaseline: false,
  unpricedBaselineTokens: 0,
  unpricedCurrentTokens: 0,
  unpricedEventTokens: 0,
  unpricedEventCount: 0,
  actualMonthlyUsd: 0,
  counterfactualMonthlyUsd: 0,
  transformationPct: 0,
  compressionMeasuredUsd: 0,
  compressionMeasuredWindowUsd: 0,
  compressionMeasuredWindowTokens: 0,
  verbosityMeasuredUsd: 0,
  verbosityMeasuredWindowUsd: 0,
  verbosityTransformationUsd: 0,
  savingsPerSession: 0,
  beforeCostPerSession: 0,
  afterCostPerSession: 0,
  beforeCacheHit: 0,
  afterCacheHit: 0,
  sessionsPerMonth: 0,
  trackedDays: 0,
  beforeMixLabel: "n/a",
  afterMixLabel: "n/a",
  cumulativeSavedUsd: 0,
  installDate: null,
  breakdown: [],
  baselineBuilding: null,
});

/**
 * Compute realized savings via the current-volume counterfactual.
 *   `now`               injectable for testing.
 *   `rowsOverride`      bypasses the DB (raw session_log rows) for tests.
 *   `compressionOverride` injects the measured compression dollars when the DB
 *                       is bypassed (rowsOverride present); default 0.
 *   `compressionTokensOverride` injects the measured compression TOKENS saved
 *                       over the window when the DB is bypassed; default 0.
 *                       Plumbs through to `compressionMeasuredWindowTokens`.
 */
export function computeRealizedSavings(
  dataDir: string,
  days: number = 30,
  now: number = Date.now(),
  rowsOverride?: Array<Record<string, unknown>>,
  compressionOverride?: number,
  compressionTokensOverride?: number,
): RealizedSavings {
  let rows: Array<Record<string, unknown>> = rowsOverride ?? [];
  let measuredCompression = compressionOverride ?? 0;
  let measuredCompressionTokens = compressionTokensOverride ?? 0;
  let measuredVerbosity = 0;
  let unpricedEventTokens = 0;
  let unpricedEventCount = 0;

  if (!rowsOverride) {
    const store = new TrendsStore(dataDir);
    try {
      rows = store.getAllSessions();
      // Compression add-back (pool #3) reads metered savings_events for the window.
      // Both the $ (for the transformation headline) and the token count (for the
      // Tokens Saved card) come from the same query — neither is summed into the
      // other's headline.
      const comp = store.getCompressionSavings(days, now);
      measuredCompression = comp.totalCostSavedUsd;
      measuredCompressionTokens = comp.totalTokensSaved;
      unpricedEventTokens = comp.unpricedTokens;
      unpricedEventCount = comp.unpricedEvents;
      // Verbosity-steer add-back (pool #4) reads estimated savings_events.
      const verbosity = store.getVerbositySavingsDetail(days, now);
      measuredVerbosity = verbosity.costUsd;
      unpricedEventTokens += verbosity.unpricedTokens;
      unpricedEventCount += verbosity.unpricedEvents;
    } catch {
      rows = [];
      measuredCompression = 0;
      measuredCompressionTokens = 0;
      measuredVerbosity = 0;
    } finally {
      store.close();
    }
  }

  const notReady = (status: string): RealizedSavings => {
    const result = NOT_READY(status);
    result.compressionMeasuredWindowUsd = Math.max(0, measuredCompression);
    result.compressionMeasuredWindowTokens = Math.max(0, measuredCompressionTokens);
    result.verbosityMeasuredWindowUsd = Math.max(0, measuredVerbosity);
    result.unpricedEventTokens = unpricedEventTokens;
    result.unpricedEventCount = unpricedEventCount;
    return result;
  };

  const history = rows
    .map(toRec)
    .filter((r) => r.ts > 0)
    .filter((r) => r.fi >= MIN_INPUT_TOKENS && r.durationSec >= MIN_DURATION_SECONDS)
    .sort((a, b) => a.ts - b.ts);
  if (history.length === 0) return notReady("no sessions yet");

  const installTs = history[0].ts;
  const installDate = new Date(installTs).toISOString().slice(0, 10);
  const windowStart = installTs + BASELINE_ONBOARDING_DAYS * DAY_MS;
  const windowEnd = windowStart + BASELINE_EARLY_WINDOW_DAYS * DAY_MS;
  const before = history.filter((r) => r.ts >= windowStart && r.ts < windowEnd);

  if (before.length < BASELINE_MIN_STABLE_SESSIONS) {
    const daysLeft = Math.max(0, Math.ceil((windowEnd - now) / DAY_MS));
    const r = notReady(`building baseline (${before.length}/${BASELINE_MIN_STABLE_SESSIONS} early sessions)`);
    r.installDate = installDate;
    r.baselineBuilding = {
      sessionsInWindow: before.length,
      sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
      earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
      daysLeft,
      firstDate: installDate,
    };
    return r;
  }
  if (now < windowEnd) {
    const daysLeft = Math.ceil((windowEnd - now) / DAY_MS);
    const r = notReady(`building baseline (${daysLeft}d of early window left)`);
    r.installDate = installDate;
    r.baselineBuilding = {
      sessionsInWindow: before.length,
      sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
      earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
      daysLeft,
      firstDate: installDate,
    };
    return r;
  }

  // CURRENT window = recent sessions in lookback, strictly after the baseline
  // window (cohort separation). This is the EXACT volume held constant on both arms.
  const afterStart = Math.max(windowEnd, now - days * DAY_MS);
  const after = history.filter((r) => r.ts >= afterStart);

  // BASELINE efficiency anchors (mix + pool cache-hit), from the early window.
  const beforeMix = modelMix(before);

  if (after.length < AFTER_MIN_SESSIONS) {
    const r = notReady(`building comparison (${after.length}/${AFTER_MIN_SESSIONS} recent sessions)`);
    r.installDate = installDate;
    r.beforeMixLabel = mixLabel(beforeMix);
    // Baseline window is closed and frozen — daysLeft=0 here.
    r.baselineBuilding = {
      sessionsInWindow: before.length,
      sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
      earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
      daysLeft: 0,
      firstDate: installDate,
    };
    return r;
  }

  // Aggregate the CURRENT window billed token classes (SUM, not mean). NO
  // winsorization / outlier drop: a heavy session's tokens are real volume that
  // genuinely cost more at the baseline mix.
  let F = 0, CR = 0, CW = 0;
  for (const r of after) { F += r.fi; CR += r.cr; CW += r.cw; }
  // Numeric safety: clamp the aggregated totals to finite, non-negative values
  // (mirrors measure.py's _session_token_vector clamps). Per-row reads are already
  // clamped in toRec, but a corrupt/overflowing row could still poison the SUM
  // (e.g. a row injected via rowsOverride bypassing the DB type system). A single
  // bad total must never NaN/negative the headline.
  const clampTotal = (x: number): number => (Number.isFinite(x) && x > 0 ? x : 0);
  F = clampTotal(F); CR = clampTotal(CR); CW = clampTotal(CW);
  const totalIn = F + CR + CW;
  if (totalIn <= 0) {
    const r = notReady("no recent volume");
    r.installDate = installDate;
    r.beforeMixLabel = mixLabel(beforeMix);
    r.baselineBuilding = {
      sessionsInWindow: before.length,
      sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
      earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
      daysLeft: 0,
      firstDate: installDate,
    };
    return r;
  }

  const afterMix = modelMix(after);
  const beforeClassMix = Object.fromEntries(
    (["fi", "cr", "cw", "out"] as const).map((klass) => [klass, classMix(before, klass, beforeMix)]),
  ) as Record<TokenClass, ModelMix>;
  const ceilings = Object.fromEntries((["fi", "cr", "cw", "out"] as const).map((klass) => [klass,
    Math.max(0, ...Object.keys(beforeClassMix[klass]).map((model) => knownModelRate(model, rateClass[klass]) ?? 0)),
  ])) as Record<TokenClass, number>;
  const afterClassMix = Object.fromEntries(
    (["fi", "cr", "cw", "out"] as const).map((klass) => [klass, classMix(after, klass, beforeClassMix[klass], ceilings[klass])]),
  ) as Record<TokenClass, ModelMix>;
  let premiumTokens = 0, premiumCostUsd = 0, eligibleSessions = 0, pricedTokens = 0, unpricedCurrentTokens = 0;
  const unpricedBaselineTokens = before.reduce((total, session) => total + session.buckets.reduce((subtotal, b) =>
    subtotal + (["fi", "cr", "cw", "out"] as const).reduce((sum, klass) =>
      sum + (knownModelRate(b.model, rateClass[klass]) === null ? b[klass] : 0), 0), 0), 0);
  for (const session of after) {
    let total = 0;
    for (const b of session.buckets) {
      for (const klass of ["fi", "cr", "cw", "out"] as const) {
        const rate = knownModelRate(b.model, rateClass[klass]);
        if (rate === null) { unpricedCurrentTokens += b[klass]; continue; }
        total += b[klass];
        if (rate > ceilings[klass]) {
          premiumTokens += b[klass];
          premiumCostUsd += b[klass] * rate;
        }
      }
    }
    if (hasEligibleUsage(session, ceilings)) eligibleSessions++;
    pricedTokens += total;
  }

  // CURRENT pool cache-hit over FRESH+CACHE_READ (cache-write excluded) — the one
  // efficiency signal the "now" arm needs from the current window.
  const curPool = F + CR;
  const curHit = curPool > 0 ? CR / curPool : 0;

  // FROZEN typical session = the per-session MEAN of the early (`before`) window, a
  // fixed install-anchored cohort -> a stable, factual baseline. Both arms price THIS
  // session, so per-session figures move ONLY with efficiency, never with the current
  // period's workload volume. Mirrors measure.py _estimate_before_after_savings.
  const nBefore = Math.max(1, before.length);
  const pricedBaseline = (klass: TokenClass) => before.reduce((total, session) => total + session.buckets.reduce(
    (sum, b) => sum + (knownModelRate(b.model, rateClass[klass]) !== null ? b[klass] : 0), 0), 0) / nBefore;
  const tFi = pricedBaseline("fi");
  const tCr = pricedBaseline("cr");
  const tCw = pricedBaseline("cw");
  const tOut = pricedBaseline("out");
  const tPool = tFi + tCr; // frozen fresh + cache_read pool

  const afterWindowDays = Math.max(1, (now - afterStart) / DAY_MS);
  const sessionsPerMonth = (eligibleSessions / afterWindowDays) * 30;

  // MONTHLY SCALING: the token classes above are summed over the `days` window,
  // so every dollar figure derived from them is a WINDOW total, not a monthly one.
  // `days` is user-controllable (dashboard.ts clamps to [1,365]); at days=7 the raw
  // window sum is ~1/4 of the true monthly figure and at days=90 ~3x. Scale every
  // dollar OUTPUT by 30/days so the headline is monthly regardless of `days`,
  // mirroring openclaw/src/savings.ts (`monthlyScale = 30/max(1,days)`) and
  // measure.py's monthly conversion. `sessionsPerMonth` already annualizes; the
  // dollar aggregates did NOT — this closes that gap. At days=30, scale == 1, so
  // the locked days=30 worked example is byte-identical (no regression).
  const monthlyScale = 30 / Math.max(1, days);
  const m = (x: number): number => x * monthlyScale;

  // OLD WAY / session (counterfactual) = the typical session's native fresh/cache_read
  // split priced at the baseline mix + cache-write (5m, conservative) at the baseline
  // mix. Depends only on the frozen early window + prices -> stable across runs.
  const oldCps = classPrice(tFi, tCr, tCw, tOut, beforeClassMix);
  // NOW / session prices the same frozen token classes at the current mix.
  // Hold the baseline token classes fixed. Provider cache hits are not a
  // Token Optimizer saving, so changes in cache reuse do not enter this estimate.
  const nowCps = classPrice(tFi, tCr, tCw, tOut, afterClassMix);
  if (
    !Number.isFinite(oldCps) || !Number.isFinite(nowCps) ||
    oldCps <= 0 || nowCps <= 0
  ) {
    const r = notReady("insufficient pricing data");
    r.unpricedBaselineTokens = unpricedBaselineTokens;
    r.unpricedCurrentTokens = unpricedCurrentTokens;
    r.installDate = installDate;
    r.beforeMixLabel = mixLabel(beforeMix);
    r.baselineBuilding = {
      sessionsInWindow: before.length,
      sessionsNeeded: BASELINE_MIN_STABLE_SESSIONS,
      earlyWindowDays: BASELINE_EARLY_WINDOW_DAYS,
      daysLeft: 0,
      firstDate: installDate,
    };
    return r;
  }
  // Monthly main arms = the per-session anchor x the monthly session count.
  const cfMonthlyMain = oldCps * sessionsPerMonth;
  const actualMonthlyMain = nowCps * sessionsPerMonth;

  // COMPRESSION ADD-BACK (#3): the measured removed-token dollars repriced to the
  // baseline input mix (baseline_input_rate / current_input_rate). actual = 0 for
  // this pool (the tokens were never billed), so the whole repriced value is
  // transformation. Disjoint from the billed pool.
  // `measuredCompression` is already WINDOWED to `days` by getCompressionSavings
  // (cutoff = now - days*DAY), so it scales by the same 30/days as the billed arms.
  const inAfter = inputRatePerMTok(afterClassMix.fi);
  const inBefore = inputRatePerMTok(beforeClassMix.fi);
  const compReprice = inAfter > 0 ? inBefore / inAfter : 1;
  const compressionAddbackWindow = Math.max(0, measuredCompression * compReprice);

  // VERBOSITY-STEER ADD-BACK (#4): estimated output tokens never produced due
  // to lean-output conciseness nudges. The main counterfactual holds output volume
  // constant (both arms price the same O), so the output reduction from nudges
  // is NOT captured by mainTransformation. These are estimated savings logged
  // to savings_events but excluded from measuredCompression. Reprice to the
  // baseline OUTPUT rate: the old way would have produced verbose output at the
  // baseline model mix's output rate. Actual for this pool is 0, so the whole
  // repriced value is transformation. Mirrors measure.py verbosity_addback.
  const outAfter = price(0, 0, 1_000_000, afterClassMix.out);
  const outBefore = price(0, 0, 1_000_000, beforeClassMix.out);
  const vsReprice = outAfter > 0 ? outBefore / outAfter : 1;
  const verbosityAddbackWindow = Math.max(0, measuredVerbosity * vsReprice);

  // Monthly arms. Main arms come from the frozen per-session anchors x session count;
  // the add-back pools are still window sums scaled to monthly via m().
  const actualMonthly = actualMonthlyMain;
  const counterfactualMonthly = cfMonthlyMain;
  const compressionAddback = m(compressionAddbackWindow);
  const verbosityAddback = m(verbosityAddbackWindow);

  // MAIN transformation = counterfactual − actual, UNCLAMPED, monthly. A net-negative main
  // pool (now marginally costlier than the frozen baseline this period) is carried honestly
  // into the combined net below rather than clamped to 0 -- clamping it while the arms stayed
  // honest is what let the headline assert a saving beside net-negative arms (the honest-net invariant).
  const mainTransformation = counterfactualMonthly - actualMonthly;

  // SUBAGENT pool (#2) = 0. OpenCode has NO Claude-style sidechains: no
  // is_sidechain column on session_log, no separate subagent transcripts to read.
  // This pool is a DOCUMENTED GAP — when OpenCode exposes delegated/subagent
  // sessions distinctly, port _subagent_pool_savings here.
  const subagentTransformation = 0;

  // Combined net across the four disjoint pools (== combinedCf - actualMonthly). The HEADLINE
  // is this net, but never negative in the public contract: when net <= 0 it reads 0 and the
  // dashboard hides the hero (mirrors measure.py's net_negative path). When shown (> 0) it
  // EQUALS counterfactual - actual exactly, so the top line and the arms can never disagree.
  const net = mainTransformation + subagentTransformation + compressionAddback + verbosityAddback;
  const transformation = Math.max(0, net);

  // Per-session keys (dashboard reads them) = the FROZEN typical-session anchors
  // directly. before_* = "the old way" (stable across runs); after_* = "now" (moves
  // only with efficiency). recentN drives the cumulative figure below.
  const recentN = eligibleSessions;
  const beforeCps = oldCps;
  const afterCps = nowCps;

  // Cumulative is an extrapolation of the current transformation, including
  // its event add-backs, across comparable sessions with eligible model usage.
  const allAfter = history.filter((r) => r.ts >= windowEnd && hasEligibleUsage(r, ceilings));
  const perSessionTransformation = recentN > 0 ? transformation / recentN : 0;
  const cumulative = perSessionTransformation * allAfter.length;

  // --- Attribution breakdown: morph the counterfactual into the actual one lever
  // at a time so the UNROUNDED steps telescope to the headline. ---
  //   routing  = counterfactual − (cf footprint repriced at today's mix, incl CW)
  // Routing carries the cache-write reprice (#2). Compression is the third lever.
  // Levers are computed on the WINDOW arms then scaled by m() so they telescope to
  // the (monthly) main transformation.
  // Always decompose the main pool, even when it is net-negative, so the waterfall sums to
  // the honest net including a pool that grew (shown honestly as a "-$X" lever).
  const sRoute = (oldCps - nowCps) * sessionsPerMonth;

  const breakdown: SavingsBreakdownItem[] = [
    { key: "routing", label: "Eligible model mix change", monthlyUsd: sRoute },
    { key: "subagent_routing", label: "Cheaper subagents (no sidechains on OpenCode)", monthlyUsd: subagentTransformation },
    { key: "context_compression", label: "Lighter context (fewer re-reads, metered)", monthlyUsd: compressionAddback },
    { key: "verbosity_steer", label: "Lean output nudges (less output, estimated)", monthlyUsd: verbosityAddback },
  ]
    .filter((b) => b.key !== "subagent_routing" || b.monthlyUsd !== 0) // drop the always-0 sidechain lever
    .sort((a, b) => Math.abs(b.monthlyUsd) - Math.abs(a.monthlyUsd));

  // transformationPct: fraction of combined counterfactual spend eliminated.
  // The combined counterfactual = main cf + compression add-back + verbosity
  // add-back (both add-back pools' actual is 0, so their cf == their contribution).
  // Clamped [0,1].
  const combinedCf = counterfactualMonthly + compressionAddback + verbosityAddback;
  const transformationPct = combinedCf > 0
    ? Math.max(0, Math.min(1, transformation / combinedCf))
    : 0;

  // compressionMeasuredUsd: the RAW metered floor (before repricing to baseline mix).
  // This is the proven, event-by-event subset of compressionAddback. It is returned
  // as a SEPARATE field so the dashboard can render it in its own card, kept OUT of
  // the transformation headline. INVARIANT: never summed into monthlySavingsUsd.
  const compressionMeasuredUsd = m(Math.max(0, measuredCompression));
  // Raw window sum (pre-monthly-scale) for the young-install "so far" hero.
  const compressionMeasuredWindowUsd = Math.max(0, measuredCompression);

  // verbosityMeasuredUsd: the RAW estimated verbosity $ (before repricing to
  // baseline output mix). Also a separate field for the dashboard, kept OUT of
  // the transformation headline.
  const verbosityMeasuredUsd = m(Math.max(0, measuredVerbosity));
  // Raw window sum (pre-monthly-scale) for the action-savings card's period
  // total. This is the actual selected-period savings_events sum, never a
  // monthly run-rate or lifetime figure mislabeled as the lookback window.
  const verbosityMeasuredWindowUsd = Math.max(0, measuredVerbosity);

  return {
    ready: true,
    status: "ok",
    monthlySavingsUsd: transformation,
    premiumTokens,
    premiumCostUsd,
    premiumShare: pricedTokens > 0 ? premiumTokens / pricedTokens : 0,
    aggregateBaseline: before.some((r) => r.aggregateOnly),
    unpricedBaselineTokens,
    unpricedCurrentTokens,
    unpricedEventTokens,
    unpricedEventCount,
    actualMonthlyUsd: actualMonthly,
    counterfactualMonthlyUsd: combinedCf,
    transformationPct,
    compressionMeasuredUsd,
    compressionMeasuredWindowUsd,
    compressionMeasuredWindowTokens: Math.max(0, measuredCompressionTokens),
    verbosityMeasuredUsd,
    verbosityMeasuredWindowUsd,
    verbosityTransformationUsd: verbosityAddback,
    savingsPerSession: beforeCps - afterCps,
    beforeCostPerSession: beforeCps,
    afterCostPerSession: afterCps,
    beforeCacheHit: tPool > 0 ? tCr / tPool : 0,
    afterCacheHit: curHit,
    sessionsPerMonth,
    trackedDays: Math.max(1, Math.floor(afterWindowDays)),
    beforeMixLabel: mixLabel(beforeMix),
    afterMixLabel: mixLabel(afterMix),
    cumulativeSavedUsd: cumulative,
    installDate,
    breakdown,
    baselineBuilding: null, // not needed when ready=true
  };
}
