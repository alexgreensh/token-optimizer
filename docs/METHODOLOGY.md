# Token Optimizer — Savings Methodology

**Purpose.** This document defines exactly how every savings number Token Optimizer
reports is calculated: the data source, the formula, the assumptions, and the honesty
caveats. It is written to be defensible to a skeptical reader (e.g. a POC client deciding
what they gained). Nothing here is a marketing estimate; every number traces to the user's
own measured sessions priced at published rates.

Last verified: 2026-06-05. Pricing cross-checked against Anthropic's published rates.

---

## 0. Principles (the rules every number obeys)

1. **Three tiers, never summed across the wrong boundary.**
   - **Measured** — billable events Token Optimizer directly metered (a compression ran, a
     route changed). 100% attributable, conservative.
   - **Estimated** — counterfactuals grounded in the user's own behavior (a cohort, an
     observed repetition count). Each carries a sample size and, where applicable, a
     confidence label. Shown split out, never passed off as measured.
   - **Opportunity** — avoidable waste the user *could* reclaim but Token Optimizer does not
     yet prevent. Never folded into "what you're saving."
   Measured + Estimated may headline *together* only when the split is always visible.
   Opportunity is always a separate figure.
2. **Never fake a realized dollar.** A dollar is "realized/measured" only when (a) Token
   Optimizer intervened, (b) behavior changed, and (c) we measured the reduction against a
   baseline. Anything failing all three is Estimated or Opportunity.
3. **Grounded in the user's own data.** No shared default multipliers. Every baseline is the
   user's own measured history; every cohort is their own sessions.
4. **Conservative on every judgment call.** Where a choice exists, we pick the one that
   understates rather than inflates (documented per-element below).

---

## 1. Pricing rates (the foundation)

All costs are **API-equivalent** value: what the usage *would* cost at published per-token
API rates. On a flat subscription the user is not billed per token, so this is reclaimed
pay-as-you-go-equivalent value, not a refund.

Rates (per 1M tokens), tier `anthropic`, verified against Anthropic's published pricing
(2026-05-26 snapshot):

| Model | Input | Output | Cache read (0.1×) | Cache write 5m (1.25×) | Cache write 1h (2×) |
|---|---|---|---|---|---|
| Opus 4.x (4.8/4.7/4.6) | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 |
| Sonnet 4.6 | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 |
| Haiku 4.5 | $1.00 | $5.00 | $0.10 | $1.25 | $2.00 |

- The legacy $15/$75 Opus rate was **Opus 3** (retired). Using it would overstate ~3×.
- OpenAI/Codex and Gemini models use their own provider rate cards (`_get_model_cost`).
- Cache reads are priced at **0.1×** input and cache writes at **1.25×** (5-minute TTL,
  the common case) — exactly Anthropic's published cache multipliers.
- Implemented in `_get_model_cost(model, input, output, cache_read, cache_create, tier)`.

---

## 2. Per-session token decomposition

Each session in `session_log` stores `input_tokens` (total billed input = fresh + cache_read
+ cache_write), `output_tokens`, `cache_create_5m_tokens`, `cache_create_1h_tokens`, and
`cache_hit_rate` (= cache_read / total billed input). We reconstruct the four billed classes
exactly (`_session_token_vector`):

```
cache_write (cw) = cache_create_5m + cache_create_1h
cache_read  (cr) = input_tokens × cache_hit_rate
fresh_input (fi) = max(0, input_tokens × (1 − cache_hit_rate) − cache_write)
output      (o)  = output_tokens
```

`cache_hit_rate` is clamped to [0,1]. This decomposition is exact (the three input classes
sum back to total billed input). **Why it matters:** in real Claude Code usage the bulk of
token volume (commonly 80%+, rising with session length) is cache-reads — the same prefix
(CLAUDE.md, skills, tool defs, conversation history) re-read on every turn. The dashboard reports
each user's own measured cache-hit rate. Cache-reads are cheap per token (0.1×) but enormous in volume, so they
dominate cost. The whole savings story is largely about cache-read *volume* falling.

**Per-session cost** (`cost_per_session`): price the class vector at the era's REAL model
mix. For each priced model in the mix, price the vector at that model's rate card via
`_get_model_cost`, then blend by the model's share (renormalized over the priced models so
an unpriced entry never drags the cost toward zero):
```
cost = Σ_model (share_model / Σ priced shares) × cost_model(vec)
```
This is provider-agnostic: an Anthropic era blends Opus/Sonnet/Haiku; a Codex era blends
gpt-5-codex / gpt-5.x / mini by their measured shares. The same blend is applied to both the
before and after windows, so the comparison is fair. If a mix has no priced models at all, the
session is priced at a single runtime-default model (Codex → gpt-5-codex, else Sonnet).

---

## 3. THE TRANSFORMATION (headline counterfactual)

**Claim:** *Your current activity, run the way it ran before Token Optimizer, would cost
$X/month more than it does now.*

This captures the whole footprint collapse together — lighter sessions (fewer context
re-reads), smarter routing (less Opus), trimmed structural prefix, fewer cache-drop reloads —
because all of those land in the per-session token vector and the model mix.

### 3.1 The baseline (the "before" anchor) — `_compute_baseline_state`

The baseline is **a typical pre-optimization session, measured from the user's own earliest
real sessions**, captured once and frozen:

- **Window:** skip the first install-day (`_BASELINE_ONBOARDING_DAYS = 1`), then the next
  `_BASELINE_EARLY_WINDOW_DAYS = 30` days. A month-scale window (hundreds of sessions) makes
  the per-session mean stable and matches the period's actual per-session bill.
- **Estimator: winsorized mean** (`_winsorized_mean_session`). Each session's *total* tokens
  are capped at the window's 99th percentile (by scaling its whole class vector down), then
  averaged across all sessions. This caps a one-off bulk operation (a full-codebase index or
  vault import that bills hundreds of millions of cache-read tokens in a single session)
  while keeping **every** genuine heavy working session.
  - **Why not a plain mean:** robust against a single pathological session. (In practice the
    winsorization moves the result only modestly — over a 30-day window the plain mean is
    already stable.)
  - **Why not a median or a 10% trimmed mean:** the median (and aggressive trimming) discard
    the heavy sessions, which is exactly where the cost — and the savings — live. That
    *understates*. (We tested this: median collapsed the figure ~50× below the user's actual
    measured per-session cost.)
  - **Why not the earliest-N-sessions cohort:** a 60-session cohort lets ~5 marathon sessions
    (8% of the window) dominate the mean. Over a 30-day window those same marathons are ~1%
    and the mean is stable. The fix to an earlier over-estimate was the *window*, not trimming.
- **Baseline model mix:** the baseline stores the full pre-TO model-mix shares
  (`model_shares`). For an Anthropic user with an explicit pre-TO value
  (`pretool_baseline.json`, `opus_share_source = "pretool_baseline"`) it is the recorded
  Opus split; otherwise it is the earliest measured mix (`"robust_earliest"`) — which for a
  Codex user is their early gpt-* mix. The before/after prices each side at its own stored
  mix, so the transformation works on any provider, not just Anthropic.
- **New users:** the baseline freezes automatically once the 30-day window has fully elapsed
  AND ≥ `_BASELINE_MIN_STABLE_SESSIONS` (30) sessions exist — until then the transformation
  is hidden (no fake number). **Existing users** (installed before any baseline was captured):
  the same computation runs over their earliest real sessions and freezes now. This is why it
  works for POC clients with prior history.
- **Frozen + versioned:** stored in `baseline_state.json`, atomic write, `version` field. If
  the estimator changes (`_BASELINE_VERSION` bumps), stale baselines are recomputed and
  re-frozen rather than silently reused. Captured once so the anchor is stable run-to-run.

### 3.2 The "after" (current) profile

The same winsorized-mean class vector over the trailing 30 days, priced at the **measured
current Opus share** (`_model_mix_shares`).

### 3.3 The formula

```
savings_per_session  = baseline_cost_per_session − current_cost_per_session
monthly_savings_usd  = savings_per_session × current_sessions_per_month
```

`current_sessions_per_month = recent_session_count / window_days × 30`.

This scales the **per-session** efficiency gain by **current** activity: "your current monthly
session volume, each priced the old heavy/high-Opus way, vs actual." Scaling by per-session
(not by raw monthly token totals) isolates *efficiency* from *volume* — if the user does more
work after install we do not inflate the number; if they do less we do not over-credit it.

### 3.4 Attribution, the mechanism and its limits

The per-session footprint decline is what Token Optimizer is built to produce: it surfaces
context-quality decline and cache-drop risk (status bar, quality score, nudges), and acting on
those signals (starting a fresh session before context rots, before the cache TTL expires) is
what yields lighter sessions, fewer cache-miss reloads, and a lower Opus share. The
counterfactual ("had I kept running heavy sessions at 94% Opus and eaten cache misses") is the
avoided cost.

Stated honestly: this is a single-user before/after on the user's own data, not a controlled
experiment. We cannot rigorously separate Token Optimizer's effect from background factors
(model-version cost changes, a slower vs an intense month, the user's own skill growth). What
the timing supports is that the footprint decline is consistent with Token Optimizer's signals
driving earlier context resets, which is the mechanism it is designed to produce. We present it
as a strong attribution, not a proven cause.

### 3.5 Honesty caveats (surfaced with the number)

These are documented here and surfaced in the dashboard's "How we work this number out"
explainer beside the figure.

- **Counterfactual, not a bill.** It assumes the user would have run as many sessions at the
  old cost. If session volume grew a lot after install (cheaper sessions can invite more usage),
  a portion of the figure reflects that volume growth, not pure per-session efficiency. When
  current session volume is much higher than the baseline era's, the number leans optimistic;
  the dashboard's explainer notes this. Conversely the earliest captured window is already slightly
  post-install, so the true pre-optimization era was likely heavier (conservative the other way).
- **Clean comparison needs separation.** The "before" (frozen baseline window) and the "after"
  (recent window) must not overlap, or a recently installed user is compared to themselves. The
  code clamps the after-window to start at the baseline window's end; until enough post-baseline
  sessions exist, the transformation is hidden rather than shown as a near-zero or self-referential
  number. In practice this means a clean figure appears once the user has activity beyond their
  first ~30 days.
- **API-equivalent.** On a flat subscription this is reclaimed pay-as-you-go-equivalent value,
  not money refunded.
- **Some is workflow choice.** Most of the per-session decline is genuine efficiency (the
  per-session cost fell independent of volume), driven by Token Optimizer's signals; a portion
  reflects the user choosing leaner workflows.

---

## 4. The breakdown ("Where it comes from") — `breakdown` in `_estimate_before_after_savings`

A waterfall decomposition of the monthly figure into the levers that produced it. Sequential
attribution: morph the baseline session into the current session **one lever at a time**,
crediting each the incremental cost it removes. The five unrounded steps telescope exactly to
`savings_per_session` (no residual term); after per-lever rounding the displayed lines
reconcile to the headline within a few cents.

Order is fixed and disclosed (sequential attribution is order-dependent): **routing first**
(held at the baseline token mix), then the footprint collapse priced at today's mix, split by
token class heaviest-first:

| Lever | What it is |
|---|---|
| `routing` | Cost removed by the model-mix shift, holding tokens fixed (e.g. Anthropic 95%→68% Opus, or Codex moving off a pricier GPT tier). A negative value means the mix moved to costlier models. |
| `context_rereads` | Cost removed by the cache-read volume collapse (lighter sessions) — usually the largest |
| `structural` | Cost removed by the trimmed cache-write / structural prefix |
| `fresh_input` | Change in fresh-input cost |
| `output` | Change in output cost |

`waterfall_index` (0–4) preserves the causal order for machine consumers; the list is sorted
largest-first for display. A **negative** lever means that class grew (a cost increase); it is
labelled with a "(added cost)" phrasing so a "-$X" line never reads as a saving.

---

## 5. Measured / realized tier (directly metered)

### 5.1 Model routing — `_compute_model_routing_savings` (realized portion)
Compare the **current** model mix against the install-era baseline (snapshot `model_mix`, else
earliest `model_daily` window). REALIZED = current token volume priced at the baseline mix,
minus actual current cost. This is the rare pillar that shows a genuine realized win when the
user moved tokens off Opus (e.g. 94% → 67%). Rates blend each model's input + output $/MTok by
the **measured** output fraction (`SUM(output)/SUM(input+output)`); pricing at the input rate
alone understated routing 1.5–2× because output is up to 5× input. POTENTIAL (a conservative
share of remaining Opus routed to Sonnet at the rate delta) is reported in the Opportunity tier.

### 5.2 Runtime compression events (measured)
Logged compression events (delta reads, quality nudges, loop output compression, bash output
compression, tool-result archiving). These are billed-event-grounded reductions Token Optimizer
performed; summed into the measured total.

### 5.3 Structural prefix — `structural_detail`
Measured against the install snapshot (`snapshot_before.json`): tokens trimmed from the
per-turn prefix (CLAUDE.md, skills, MCP, MEMORY.md) below the captured baseline, priced at the
input rate and compounded across the window. Reads $0 (not negative) when nothing is trimmed
below baseline yet.

### 5.4 Progressive disclosure (tool-archive) — `_progressive_disclosure_summary`
When a large tool result is replaced by a pointer (`archive_result.py`), the net tokens that
stayed collapsed are a measured win. Re-expansions (`expand_archived`) log a debit that is
netted against the original credit (floored at 0) so a re-popped result never over-credits.

---

## 6. Estimated tier (counterfactual, cohort- or count-grounded)

Each carries a sample size; cohort estimators carry a confidence label and a minimum-sample
gate so a thin cohort never shows a number. None is ever summed into the measured total.

### 6.1 Uncaptured runtime — `_estimate_uncaptured_runtime`
Compression that runs inside sub-agent dispatches is not attributed back to the parent session,
so it never lands in `compression_events`. Estimated as measured per-session runtime savings ×
sub-agent dispatch count × **0.5** attribution haircut. Labelled estimated, shown separately.

### 6.2 Loop prevention — `_estimate_behavioral_savings`
When loop detection fires it compresses the repeated output (counted as runtime) AND stops a
runaway loop that would have burned more iterations. The avoided continuation is never billed.
We do **not** fabricate a multiplier. The avoided continuation is estimated as one more
equivalent looping span: the measured looped token volume multiplied by a continuation factor of
1.0 (a deliberate floor, since a loop caught after N repeats would plausibly have run at least
one comparable span more before another guard or the context limit stopped it). The observed
repetition `count=N` is recorded and shown alongside for transparency; it does **not** scale the
dollar figure.

### 6.3 Contamination-exit (heeded-nudge cohort) — `_estimate_contamination_exit_savings`
The flagship behavioral estimate, built on a **natural control group**: sessions where a
quality nudge fired and the user acted (compacted/cleared) = HEEDED, vs fired-but-ignored. The
two cohorts differ only in whether the nudge was heeded, so the delta in per-session rework
signal (stale-read waste, §7.1) is the mess a heeded session avoided. Reported with both cohort
sizes + a confidence label, gated behind a minimum sample.

### 6.4 Continuity handover — `_estimate_handover_rerun_savings`
Same cohort method applied to continuity: sessions that resumed via a restored checkpoint vs
those that did not, comparing the per-session rework signal. A lower figure for restored
sessions is the rework a handover avoided. Estimated tier; selection bias possible (restored
sessions may differ), so shown with sample sizes, never as a hard number.

---

## 7. Opportunity tier (reclaimable, NOT realized)

Shown as a separate "could save" figure. These count waste that *already happened* or value
*still on the table* — Token Optimizer does not yet prevent them, so counting them as savings
would claim money that was actually spent.

### 7.1 Reclaimable stale reads — `_estimate_stale_reads_reclaimable`
Sums `session_log.stale_waste_tokens`: reads that slipped through (re-read after write, or
far-distance stale) and were billed. Priced at the input rate; reports the contributing session
count as a sample size. Reclaimable by avoiding redundant re-reads, `.contextignore`, or
compacting.

### 7.2 Cache drops — `_estimate_cache_drop_savings`
When a session idles past the cache TTL, the prefix expires and the next turn re-pays a full
cache-write. Sessions whose max call-gap exceeds the TTL almost certainly ate ≥1 reload.
Estimated as `drops × per_session_prefix × 5m-write-rate`. Shown in **tokens** (provider-neutral;
the dollar value is Anthropic-specific because OpenAI/Codex cache writes are free). Reclaimable
by compacting before breaks or using the 1h cache.

### 7.3 Output waste — `_estimate_output_waste`
Full-file Writes that could have been Edits re-emit the whole file at the output rate (the
priciest class, $25/MTok on Opus). Estimated as a conservative share of Write calls × a typical
per-rewrite output delta, priced at the output rate. A coaching opportunity (use Edit over
Write), never a forced cap.

### 7.4 Model-routing potential — `_compute_model_routing_savings` (potential portion)
A conservative share (`_ROUTABLE_OPUS_FRACTION`, default 0.3) of remaining Opus tokens routed
to Sonnet at the rate delta — the routing opportunity still on the table.

---

## 8. Cumulative since install — `_savings_since_install`
The full merged savings recomputed over the whole window since the install date so the measured
event tiers sum from day one. Split into measured vs estimated. Opportunity items (cache drop,
output waste) are **excluded** here — counting them would claim money that was spent.

---

## 9. Context Quality Score (referenced by the cohort estimators)
A 0–100 score from six JSONL-derived signals: stale reads, bloated tool results, duplicate
reads, compaction depth, decision density, agent efficiency. Averaged across sessions, with a
rolling window and fill warnings so the score reflects current context health rather than
diluting over a long session. It is the signal whose decline drives the quality nudges that the
contamination-exit cohort (§6.3) measures.

---

## 10. Known limitations (stated, not hidden)
- The transformation is a **counterfactual**, not a billed amount (§3.5), and an attribution,
  not a controlled experiment (§3.4).
- Cohort estimators (§6.3/6.4) can carry **selection bias**; labelled estimated, shown with
  sample sizes.
- Cache-drop dollars are **Anthropic-specific**; shown in tokens for provider neutrality.
- Pre-install waste is **not retroactively measurable** for cohort signals that began logging
  at install, those read 0 on historical sessions and populate forward only.
- **Sub-agent token attribution.** A session's `input_tokens` includes its sub-agents' input,
  but `cache_hit_rate` is the parent session's only. For a session with heavy sub-agent use,
  the decomposition applies the parent hit rate to the combined input, attributing some
  sub-agent tokens to the cheap cache-read class. This is directionally conservative for a
  single era but, if sub-agent usage grew after install, can slightly overstate the delta.
  Bounded by the sub-agent share of tokens; a candidate for a future per-class sub-agent split.
- **Winsorization on small windows.** Capping the top 1% means, for a window with very few
  sessions, only the single heaviest session is capped. For a sparse user (around the 30-session
  minimum) who had several pathological bulk-op sessions in their first 30 days, the cap may not
  neutralize all of them, so the "before" can read modestly high. For a normally active user the
  30-day window holds hundreds of sessions and a few spikes are diluted to near-nothing. The
  per-session mean is most stable on month-scale windows, which is why the window, not the cap,
  is the primary stabilizer.
- **Short after-windows are not extrapolated.** A monthly figure is only shown once at least a
  week of post-baseline activity exists, so a one-day burst is never blown up into a month.
- The baseline's earliest window is already slightly post-install, so it is a **conservative**
  proxy for the true pre-TO era.

---

## Appendix — POC usage
For a client proof-of-concept: install Token Optimizer, let the 30-day baseline window accrue
(or, for a client with prior history, the baseline is estimated from their earliest real
sessions immediately), then read the transformation + measured tiers. The baseline is the
client's *own* sessions at their *own* pre-optimization mix, frozen, so the gain is defensible
as "your activity, your old way, vs now." Always present it with the §3.5 caveats — the honesty
is the credibility.
