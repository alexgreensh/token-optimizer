# Token Optimizer: how every savings number is measured

This is the single source of truth for every dollar and token figure Token Optimizer
shows: the CLI report, the dashboard, the status bar, the README snapshot and the
benchmark pages. If code and this document disagree, one of them is a bug. Change both
in the same commit.

Function names point to `skills/token-optimizer/scripts/measure.py`. The rules in
section 1 are enforced by tests; a rule without a test is a wish.

---

## 1. The rules (each one has a test)

| # | Rule | Enforced by |
|---|---|---|
| R1 | **One action headline.** The monthly total is logged actions, write-class premium, repeat reads and eligible estimates. Routing is separate. | `test_action_savings_components.py` |
| R2 | **Parts add up to the total, to the cent.** Round once (largest remainder), never each part on its own. | `test_savings_display_cents.py` |
| R3 | **What-ifs stay outside.** "Could save" items, cache drops and the routing what-if are never in the headline. | `test_action_savings_components.py`, `test_opportunity_routing_split.py` |
| R4 | **Since install separates evidence.** Show measured and estimated action savings separately, with stated-baseline routing in its own estimated band. | `test_since_install_all_in.py` |
| R5 | **Longer never means smaller.** For non-negative parts, a longer window is at least the shorter one, and since install is at least the last 30 days. | `test_savings_invariants.py` |
| R6 | **Every request counted once.** Streamed and replayed requests fold to one by request id, across sessions. | `test_usage_counted_once.py` |
| R7 | **Only real work counts.** Synthetic fixtures, screenshots priced as text, and twin rows are excluded. Benchmark sessions with transcript-proven API usage count. | `test_counted_views.py`, `test_transcript_backfill.py` |
| R8 | **A debit mirrors its credit.** If a credit is left out (screenshot archive), its reversal (re-expanding that screenshot) is left out too. | `test_counted_views.py` |
| R9 | **Price by token class and exact model.** Fresh input, cache read, 5-minute cache write, 1-hour cache write and output each have their own rate, for the model generation that ran. | `test_counted_pricing_integrity.py` |
| R10 | **Unknown is $0 and said out loud.** A model or class with no known price is never given a guessed price. Its tokens are listed as unpriced. | `test_counted_pricing_integrity.py` |
| R11 | **Estimates follow the window's model mix.** Estimated parts are priced at the usage-weighted input rate of the window, not the model of the latest session. | `test_since_install_all_in.py` |
| R12 | **A vendor price cut is not routing.** Routing compares on one reference card per model family; the generation price gap is shown separately. | `test_routing_class_pricing.py`, `test_opportunity_routing_split.py` |
| R13 | **Upgrades re-measure all history once, then say so.** Until it finishes, affected figures carry "so far" and a sessions-left count. | `test_upgrade_drain_and_remeasure.py` |
| R14 | **History is never deleted.** Ledger rows stay; views decide what counts. | `test_counted_pricing_integrity.py`, `test_counted_cumulative.py` |
| R15 | **This document lists every part.** Every headline part and estimate key in the code is named below. | `test_savings_invariants.py` |

---

## 2. Pricing

- **Rates.** Published API rates per model and token class (section 1, R9). On a flat
  plan, dollars mean capacity freed inside your limits, not cash refunded.
- **Exact model first.** A row is priced at the model it ran on, matched by exact id or
  dated snapshot (`_match_price_alias`). Route prefixes such as Bedrock, Vertex,
  OpenRouter or Copilot are stripped first.
- **Session mix when the row has no model.** Older rows without a model use the model
  mix that session recorded.
- **Unknown stays unknown (R10).** No family or "default" fallback price.
- **One-shot removals** (a tool result archived, a command output compressed) are priced
  at the billed class the next call shows. When no billed class is on record, they use
  the input rate as a stated minimum.

---

## 3. The monthly headline (all-in)

Built by `_action_savings_components` for a window of N days (30 by default). Shown in
the CLI as `ALL IN` and on the dashboard as the hero number.

### 3.1 Logged actions (measured)

Token changes Token Optimizer metered itself: compressed command output, archived tool
results that stayed collapsed, structure maps, quality nudges, cross-turn dedup.
Verbosity nudges are not here: their before/after has no control, so they sit in
the estimated tier (section 3.4) at their net-of-control size.

"Measured" means observed token changes and observed actions only. A mechanism whose
trigger was seen but whose token value is inferred — the working set a checkpoint
restore would have re-read, the continuation a loop warning would have prevented, the
search a followed hint would have replaced, the context a fresh session would have
carried — is an estimate, and lives in section 3.4, never here.

- Source: `savings_events` and `compression_events`, read through the counted views
  (`counted_savings_events`, `counted_compression_events`).
- **Left out by the views (R7):**
  - sessions written by test suites and benchmarks (`_FIXTURE_SESSION_GLOBS`)
  - image tool results (`_MEDIA_TOOL_GLOBS`): images bill by pixels, not base64 length
  - twin rows: the same row twice within 3 seconds (`_COUNTED_TWIN_WINDOW_SECONDS`)
  - re-expansions of media archives (R8), matched by tool name or, for older rows, by
    session, time and size (`_media_reexpand_pair_sql`)
- Re-expanding an archived result reverses its credit (`tool_archive_reexpand`).
  Coverage is a growing union per archived item: a bounded `expand` window debits
  only characters no earlier re-expansion of that item already covered (the detail
  records the debited char ranges); a full expand debits whatever is still
  uncovered, or the whole stored response when nothing was returned before.
- Verbosity is folded per API request, never per transcript line.

### 3.2 Model routing (estimated)

What moving work off premium models saved, compared with your own mix before install.

- **Baseline:** the frozen pre-install model mix (`pretool_baseline`).
- **Current:** your mix over the window, from usage counted once (R6).
- **Price list:** one reference card per family, the newest of each (R12). The gap
  between the cards you actually ran and that list is reported as
  `generation_price_delta_usd`, never as savings.
- **Left out:** premium models you chose (for example Fable), models with no known price,
  and usage without a token-class split.
- **Coverage:** unsplit usage is reported separately and stays outside comparison
  coverage. With no comparable workload, routing is unavailable. The plan card,
  the Savings tab, the since-install card and the CLI report disclose the excluded
  unsplit token count, including when routing is unavailable.
- **Pending:** while sessions in the window still need re-measuring, routing shows
  "pending" and is not added (R13).

### 3.3 Repeat reads avoided (modeled)

Removed context that the next calls would otherwise have re-read from cache.

- Each removal is followed through the transcript from the second call after it to the
  next real compaction, with turns deduped (`_counted_cumulative_summary`).
- Each turn is priced at that turn's cache-read rate. Rows without per-turn evidence
  use the session's recorded model mix.
- Results are stored in `counted_reread` keyed by event, so they survive transcript
  rotation. A row is re-scored when the pricing version changes.
- Attribution is by removal date: a removal on day D counts in D's window even if the
  re-reads it avoided happened later.
- The first call's removal is already in logged actions and is not added again.
- The counted card (`counted_cumulative`) only follows removal classes whose
  magnitude was metered (`_COUNTED_SE_EVENT_TYPES`). Counterfactual classes are
  deliberately excluded so an inferred size never compounds into repeat-read
  dollars: `resume_lean`, `checkpoint_restore`, `mcp_cap` and `hint_followed`
  (plus the output-side `verbosity_*` nudges: a shorter reply IS re-read on every
  later call until compaction, and `verbosity_output_estimated` scores that carry
  turn by turn, net of the control, in the estimated tier). Their estimated-tier values still appear in section 3.4; they just do
  not ride the counted annuity. Removing `checkpoint_restore` from the counted
  set is why `counted_cumulative` / `reread_usd` can visibly drop for installs
  that used restores — the credit moved tiers, it was not deleted.

### 3.4 Other estimates (estimated)

Each key below is one line in the report. All are priced at the window's input-rate mix
(R11) unless noted.

| Key | What it estimates |
|---|---|
| `resume_lean_estimated` | Unverified historical proxy, excluded from totals. |
| `resume_lean_verified` | Receiver-verified reload and carry estimate, capped at the old session's compaction window and the receiver's next compaction. |
| `checkpoint_restore_estimated` | Unverified restore estimate, excluded from totals. |
| `structural_estimated` | Realized setup cleanup estimated from dated archives or owner-attested trims, priced at each eligible call's cache-read rate. |
| `keepwarm_estimated` | Keep-warm, net and signed. The cache-rewrite cost avoided by verified warm resumes, minus the full cost of every warm-up ping (pings that never led to a return, and manual warm-ups booked from the Claude Code mod, included). A paused session is credited once however many warm-ups it received. Can be negative, and is then subtracted. |
| `mod_fresh_estimated` | Start-fresh actions taken from the Claude Code mod. The dropped old context (less the handoff) is priced at the cache-read rate for a continuation estimated from your own earlier sessions (cohort median, capped at 10 turns, needs a cohort of 5). Counted as 0 when `handover_rerun` is already positive, so the two overlapping counterfactuals are never stacked. |
| `expand_overhead` | Estimated expansion-command and reread overhead, subtracted from action savings. |
| `uncaptured_runtime` | Compression inside sub-agent runs that is never logged as an event |
| `behavioral_estimate` | One more loop span avoided per caught loop |
| `mcp_cap_estimated` | Unsupported flat cap estimate, excluded from totals. |
| `handover_rerun` | Re-work avoided by a continuity handover, from your own session cohorts |
| `contamination_exit` | Sessions that heeded a fresh-session nudge, from your own cohorts |
| `verbosity_steer` | Output shortened by a concise-output nudge, before an observed measurement exists |
| `verbosity_output_estimated` | Nudged replies against matched replies in sessions TO ran in but never nudged (same position, reply count, running mean, context size), plus the later rereads of that difference until compaction. Signed; credits only a positive net, else $0 with the per-nudge figure and its 95% interval shown. Recomputed at most daily by a user-run or detached collect, never on the hook path. The booked `verbosity_steer_measured` rows are kept for reference and never credited |
| `hint_followed` | Searches avoided by a followed context hint. The hint->read pair is observed; the avoided-search size is a fixed estimate (used once any hint fires) |
| `retrieval_serve` | Cohort estimate for avoided searches, used only before any hint fires |

`hint_followed` and `retrieval_serve` are never both counted.

Overlap note: `handover_rerun` (B7) estimates re-work avoided *beyond* a
continuity handover's restored context — stale re-reads of material the
handover did not carry — while `checkpoint_restore_estimated` estimates the
working-set re-reads a checkpoint restore let the session skip. On installs
that use both mechanisms the two pools can describe adjacent slices of the
same avoided re-read; they are summed as separate estimates and are not netted
against each other.

Loop-detection warnings log the observed loop volume as a `tier=estimated`
compression event (it is spend that already happened, not a removal), and it never
reaches the measured headline; the avoided continuation is `behavioral_estimate`.
Non-savings warning and advisory features are also omitted from compression
coverage buckets. Their diagnostic events remain stored.

The fresh-session nudge's dollar figure is a separate counterfactual estimate shown
in the nudge message itself. It reports the recurring per-turn saving — warm
context the continued session would keep re-sending at the cache-read rate,
minus the working set the fresh session must carry anyway — against the one-time
costs of starting fresh (the lean block's cache-write handover plus rebuild
re-reads at the input rate). The message states the per-turn saving and the
break-even turns (`ceil(one_time / per_turn)`), labelled `est.`; no horizon is
assumed, so no session-total dollar figure is claimed. When the per-turn saving
is zero or negative the message omits the dollar clause rather than inventing a
payback period.
The lean-block floor used by OpenCode is a lower bound on rebuild size, making
per-turn savings an upper bound and payback turns a lower bound. The Python
nudge stays within 160 characters and does not claim a checkpoint was saved.

---

## 4. Since install (all-time)

`_savings_since_install` runs the headline recipe (section 3) over the window from the
install date to today (R4). The install date is the first baseline snapshot, or the first
recorded session.

- The dashboard card reads "Saved since <date>" and lists the same four parts.
- The context-only subtotal (initial removals + repeat reads, `counted_cumulative`) is
  shown inside its breakdown. It is never labelled as all-time savings.
- While older sessions are re-measured, the card says "so far" with a sessions-left count.

---

## 5. Outside the headline

| Figure | What it is | Rule |
|---|---|---|
| Could save | Full-file writes that could be edits, stale re-reads | Opportunity, never added |
| Cache drops | Sessions idle past the cache window that re-wrote the prefix | Opportunity, never added |
| Routing what-if | If about 20% of Opus work ran on the current Sonnet: fresh input and output only (`_ROUTABLE_OPUS_FRACTION`) | Assumption, never added |
| Your transformation | Cost per unit of work now vs the old way. Its removed-token and estimated-volume add-backs reprice at section 3.2's standardized baseline-to-actual ratio (`_routing_mix_reprice_ratio`), the same factor the Savings surfaces publish, computed over the same window (days or since-reset) as the dollars it multiplies; pending or unavailable routing reprices neutrally and the card discloses the neutral basis (`compression_reprice_basis`). Within ±5% it reads "no measurable change" (`_TRANSFORMATION_FLAT_BAND`) | Cross-check, never the headline |
| Token card | Metered tokens plus five estimate pools (MCP caps, hints, verbosity, lean resumes, checkpoint restores). Repeat reads and routing are not in it | Its own scope, labelled |
| Plan card | Extra work per token: saved context times the model-mix factor, against your live rate-limit windows. The model-mix factor is section 3.2's standardized baseline cost divided by its standardized actual cost (`_runway_routing_lever`): same baseline, reference cards, class weights and exclusions. Above 1 the mix got lighter, below 1 heavier. While routing is pending or unavailable the card says so and leaves it out of the factor. Hidden below a 2% change. The mix-coverage line names the window the snapshot computed over (`routing_window_days`) | Its own scope, labelled |

---

## 6. Usage (what you spent)

- Parsed from transcripts by `collect_sessions` into `session_log`, `model_daily` and
  per-day usage.
- A request is counted once by request id, even when it streams in pieces or replays in
  another session (R6). The parser version is stored per session
  (`_USAGE_PARSER_VERSION`), so a parser fix re-reads only what it changed.
- **Consumed tokens and the plan denominator** are deduplicated fresh input plus
  output from top-level requests, sliced by the calendar day of activity. Cache
  reads and writes are excluded. The separate `reported_*` fields retain the raw
  `/stats` observation count for explicitly labelled diagnostics only.
  Top-level means the parent transcript's own requests: `parent_daily_usage_json`
  supplies it directly. Rows collected before parent provenance existed fall back
  to the combined parent+delegated map; those rows are counted in
  `combined_fallback_rows`, the basis string says "combined", and `complete` is
  false — legacy combined usage is never certified as top-level. The reported
  model mix uses the same parent-then-combined selection and reports its own
  `model_mix_combined_fallback_rows`.
- **Every volume window** uses `session_activity`, a projection of the stored
  per-day, per-model billing classes. A session crossing a cutoff contributes
  only its in-window requests. `model_daily` and `daily_stats` use those same
  slices. `session_log.date` means last known activity day, with separate
  `first_activity_day`, `last_activity_day`, `transcript_mtime` and `collected_at`.
  Every writer bounds the union of combined usage, parent usage and metadata
  days, including the DB-free JSONL fallback. The trends prefilter keeps NULL
  bounds and rows whose last activity reaches the window; an empty parent map
  does not exempt an old row from filtering.
  File modification time and collection time never supply missing activity dates.
  The projection unions billing days with `daily_metrics_json` days: a day with
  dated user/tool/skill activity but no billed usage still emits a row carrying
  its dated metadata and zero tokens, so metadata-only activity never disappears
  from windows or rebuilt aggregates.
- Messages, tools, skills and dispatches use dated records when available.
  Historical metadata without dated records uses a **lifetime session cohort**
  on the last known activity day. Duration, quality, stale-waste and historical
  gap statistics are cohort properties, not measurements of work performed that
  day. Daily session details label lifetime metadata separately from dated tokens
  and dollars. Cache-drop and stale-waste opportunities inherit this cohort limit.
- Per-day API-call counts come from `daily_calls_json`. When that evidence was
  never recorded the day contributes NULL, not a measured zero, and the
  projection's `api_calls_known` flag exposes the gap. The migration allocates a
  session's whole call count to its day only when the session has exactly one
  proven activity day and all retained usage/metadata buckets have valid dates
  and object values; invalid buckets and multi-day sessions keep unknown.
  Work-unit figures (`_price_parent_window`, the structural compounding formula)
  disclose `api_calls_unknown` sessions, and the api_call work unit is only used
  when both eras have complete call evidence — otherwise the unit falls back to
  messages rather than pricing a lower bound as measured.
- Missing or invalid dates remain **incomplete coverage** and contribute no
  invented volume to a period. Lifetime session-cost queries still show retained
  estimates. Date queries sum only `on_date`; recent queries sum `in_window`, while
  each entry keeps its explicitly separate lifetime total. Missing transcripts can
  still supply dated history from retained daily buckets. Adapter totals without
  request dates or proven single-day activity remain incomplete.
- The one-time `activity_dating_v1` migration repairs stored dates backward as
  well as forward and rebuilds all four aggregate tables. Before rewriting it
  journals the old `session_log` dates and `daily_calls_json`, keyed by
  `jsonl_path`, in `activity_dating_v1_journal` and copies
  the four aggregate tables to `*_pre_activity_v1` inside the same transaction;
  `rollback_activity_dating` restores those fields and aggregates and clears
  the marker. This is a **developer-only safety net**, with no CLI, for checking
  a migration on a database copy before any recollection or new writes. It
  restores snapshot dates even for recollected rows and discards subsequent
  aggregate days, so it is not a supported downgrade or live recovery path.
  A migration failure, including a write lock at `BEGIN IMMEDIATE`, rolls back,
  logs the error and returns a usable unmigrated connection for retry on the
  next init. Old derived
  baselines, workload anchors and weekly caches always leave their live names —
  a taken `.pre-activity-v1` archive moves the active copy to the next free
  `.pre-activity-v1.N`, so a stale file cannot survive — and rebuilt anchors
  carry an `activity_basis` marker their readers enforce. Older snapshot model
  mixes cannot serve as activity-based evidence; explicit user model-mix
  settings and raw savings/compression event dates stay intact. Retention
  follows ingestion time, so repairing an old activity date does not itself
  evict the session.
- OpenCode retains recording time as metadata and stores model buckets by UTC
  activity day. Its dashboard and savings windows use those slices; unknown
  timestamps remain incomplete. Python uses local calendar days. Baseline and
  recent populations count distinct sessions after selecting activity, so crossing
  midnight cannot manufacture extra sessions.

Regression coverage: `test_activity_dating.py`, `test_usage_counted_once.py`,
`test_dashboard_data.py`, `test_session_cost.py`, `test_savings_invariants.py`,
`test_trends_window_filter.py` and OpenCode's `trends.activity-dating.test.ts`.

---

## 7. Upgrades

- After an update that changes pricing or parsing, a background pass re-reads all
  history since install once (`_run_upgrade_drain`, `_upgrade_drain_days`). It runs
  detached, holds a lock, and records completion per version.
- Until it finishes, affected figures say "re-measuring (N sessions left)" or "so far"
  (R13). Numbers are not final until it completes.

---

## 8. Before any number goes public

Run these on the real figures before a README, benchmark, release note or reply quotes
them:

1. **Sum check.** Parts add up to the total to the cent (R2).
2. **Window check.** Run 30, 90 and since install. Totals grow with the window (R5),
   and since install divided by months is close to the monthly figure.
3. **Category diff.** Compare each category with the previous release on the same data.
   Explain every change over 5%.
4. **Leak check.** No test-suite or benchmark sessions in the counted totals (R7).
5. **Pending check.** No figure is quoted while re-measuring is still in progress (R13).
6. **Copy check.** When measuring on a copy of real data, the copy must keep the
   install snapshot, a fresh setup-size cache, and transcript paths that point at the
   copy. Otherwise structural savings, routing and re-measuring read wrong.

---

## 9. Quality score (a figure, but not a savings figure)

The 0–100 "quality" number is a **resource-pressure indicator**, not an accuracy measure. In score schema v3, the base remains fill (5/7) plus absolute waste (2/7), multiplied by continuity: `0.45 + 0.55 × 0.90 ** n_eff`. Each compaction contributes a size factor, clamped to 0.25–1.0 using tokens removed divided by 150,000, multiplied by an evidence factor: 0.5 for a delivered restore and clean follow-up, 0.75 for either a delivered restore awaiting measurement or a clean follow-up without one, 1.0 when unknown, and 1.5 when recovery needs attention.

This is a **prior**, not a fitted measure of information loss. Compaction can lose details and repeated summaries compound that risk, so the score uses a bounded, evidence-sensitive term. The earlier paired-window audit found no support for a fixed per-count penalty. Fit the retention rate and evidence factors on paired-window data before treating these values as calibrated.

Recovery evidence remains provisional and its public panel remains observational. Score versions are never compared for nudge baselines; a v2 cache does not seed v3 comparisons.

- **Size remedies share one gate.** `/compact`, `/clear` and "start fresh"
  recommendations — including the 300–400K context-size advisory (default
  350K, once per session, eligibility not instruction) — require ≥300K
  **current** context tokens from real telemetry (`_absolute_context_tokens`).
  Missing or contradictory telemetry means no recommendation. The score
  itself still prints below 300K; only the remedy is gated. On windows of
  200K or less, including default Sonnet and most Codex/Cursor sessions,
  compact and fresh-session remedies therefore never appear.
- **Advisories are bookkeeping, not savings.** `context_advisory_shadow` and
  `context_size_advisory` belong to `_NON_SAVINGS_COMPRESSION_FEATURES` and
  log with `tier="estimated"`. Shadow evaluations use one per-session marker
  holding the latest hourly bucket. The live advisory requires a real user
  prompt or assistant end-of-turn, plus a restorable checkpoint saved within
  30 minutes and since the latest compaction boundary. Unknown timing or body
  validity suppresses the advisory. Checkpoint validity is connected through
  `_advisory_checkpoint_has_restorable_body`, the merge integration hook for
  checkpoint-state's `_checkpoint_has_restorable_body`.

## 10. Known limits

- Checkpoint task state is deterministic transcript evidence. Constraints are scanned
  from full redacted user texts and the original objective before sentence caps;
  the objective retains up to 1,500 characters. Missing semantic fields stay unknown.
  Verified facts require a recognized verification command target and a pass marker
  in the result's summary tail. An exit code alone records a completed check.
- Compaction DROP guidance needs an explicit reference in completed requirements or
  dead ends, with no open-work reference. Files edited in the session store are
  protected even after the latest checkpoint; unknown dependencies retain the
  conservative repeated-read fallback.
- Checkpoint restore content budgets total **6,300 characters**: **800** for the pinned
  Next Action (or labelled last assistant statement), **4,000** for the body and
  **1,500** for provenance-tagged earlier facts, plus truncation markers and fences.
  The estimated `checkpoint_restore` credit is sized from the working set, not from
  this payload length. This task-state path serves Claude Code, Codex, Cowork and
  Hermes. OpenCode and OpenClaw retain their own checkpoint implementations; this
  task-state change is not ported to them.
- The latest redacted user message is capped at **4,000 characters** in the
  active-task field for Claude Code and Codex. Constraint extraction still scans
  the full message, including constraints after that cap. `make` contributes a
  verified fact only with an explicit test/check/build/lint/all target and a
  passing result marker.
- Each archived output's search index keeps at most **256 KiB** of UTF-8 head
  and tail text, including its truncation marker. The archive remains the full
  recovery source; the smaller search copy protects later session-store writes.

- Routing and estimates are estimates. They are labelled and kept separate from
  measured parts. "Measured" covers observed token changes and observed actions
  only: an observed trigger does not make a counterfactual magnitude measured.
- Rows from before per-turn evidence was recorded use session-mix or input-rate floors,
  disclosed in the report.
- Rows with no known price stay unpriced, so totals are priced subtotals.
- The headline is an API-rate equivalent, not a bill reconciliation.
