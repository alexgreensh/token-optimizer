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
| R1 | **One headline, four parts.** The monthly total is logged actions + realized model routing + repeat reads avoided + other estimates. Nothing else is added. | `test_action_savings_components.py` |
| R2 | **Parts add up to the total, to the cent.** Round once (largest remainder), never each part on its own. | `test_savings_display_cents.py` |
| R3 | **What-ifs stay outside.** "Could save" items, cache drops and the routing what-if are never in the headline. | `test_action_savings_components.py`, `test_opportunity_routing_split.py` |
| R4 | **Since install uses the same recipe.** The all-time figure is the same four parts summed over the install window. It can never be a single slice. | `test_since_install_all_in.py` |
| R5 | **Longer never means smaller.** For non-negative parts, a longer window is at least the shorter one, and since install is at least the last 30 days. | `test_savings_invariants.py` |
| R6 | **Every request counted once.** Streamed and replayed requests fold to one by request id, across sessions. | `test_usage_counted_once.py` |
| R7 | **Only real work counts.** Test-suite and benchmark sessions, screenshots priced as text, and twin rows from two installs are left out of every total. | `test_counted_views.py` |
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
results that stayed collapsed, checkpoint restores, structure maps, quality and
verbosity nudges with an observed before/after, loop detection, cross-turn dedup.

- Source: `savings_events` and `compression_events`, read through the counted views
  (`counted_savings_events`, `counted_compression_events`).
- **Left out by the views (R7):**
  - sessions written by test suites and benchmarks (`_FIXTURE_SESSION_GLOBS`)
  - image tool results (`_MEDIA_TOOL_GLOBS`): images bill by pixels, not base64 length
  - twin rows: the same row twice within 3 seconds (`_COUNTED_TWIN_WINDOW_SECONDS`)
  - re-expansions of media archives (R8), matched by tool name or, for older rows, by
    session, time and size (`_media_reexpand_pair_sql`)
- Re-expanding an archived result reverses its credit (`tool_archive_reexpand`).
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

### 3.4 Other estimates (estimated)

Each key below is one line in the report. All are priced at the window's input-rate mix
(R11) unless noted.

| Key | What it estimates |
|---|---|
| `resume_lean_estimated` | Cold resumes avoided by a lean restore (priced like its logged event) |
| `uncaptured_runtime` | Compression inside sub-agent runs that is never logged as an event |
| `behavioral_estimate` | One more loop span avoided per caught loop |
| `mcp_cap_estimated` | Large MCP results capped (priced like its logged event) |
| `handover_rerun` | Re-work avoided by a continuity handover, from your own session cohorts |
| `contamination_exit` | Sessions that heeded a fresh-session nudge, from your own cohorts |
| `verbosity_steer` | Output shortened by a concise-output nudge, before an observed measurement exists |
| `hint_followed` | Searches avoided by a followed context hint (used once any hint fires) |
| `retrieval_serve` | Cohort estimate for avoided searches, used only before any hint fires |

`hint_followed` and `retrieval_serve` are never both counted.

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
| Your transformation | Cost per unit of work now vs the old way. Within ±5% it reads "no measurable change" (`_TRANSFORMATION_FLAT_BAND`) | Cross-check, never the headline |
| Token card | Metered tokens plus four estimate pools (MCP caps, hints, verbosity, lean resumes). Repeat reads and routing are not in it | Its own scope, labelled |
| Plan card | Extra work per token from saved context, against your live rate-limit windows. Hidden below a 2% change | Its own scope, labelled |

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
- **Every volume window** uses `session_activity`, a projection of the stored
  per-day, per-model billing classes. A session crossing a cutoff contributes
  only its in-window requests. `model_daily` and `daily_stats` use those same
  slices. `session_log.date` means last known activity day, with separate
  `first_activity_day`, `last_activity_day`, `transcript_mtime` and `collected_at`.
  File modification time and collection time never supply missing activity dates.
- Messages, tools, skills and dispatches use dated records when available.
  Historical metadata without dated records uses a **lifetime session cohort**
  on the last known activity day. Duration, quality, stale-waste and historical
  gap statistics are cohort properties, not measurements of work performed that
  day. Daily session details label lifetime metadata separately from dated tokens
  and dollars. Cache-drop and stale-waste opportunities inherit this cohort limit.
- Missing or invalid dates remain **incomplete coverage** and contribute no
  invented volume to a period. Lifetime session-cost queries still show retained
  estimates. Date queries sum only `on_date`; recent queries sum `in_window`, while
  each entry keeps its explicitly separate lifetime total. Missing transcripts can
  still supply dated history from retained daily buckets. Adapter totals without
  request dates or proven single-day activity remain incomplete.
- The one-time `activity_dating_v1` migration repairs stored dates backward as
  well as forward and rebuilds all four aggregate tables. Old derived baselines,
  workload anchors and weekly caches are retained as `.pre-activity-v1` files and
  rebuilt. Older snapshot model mixes cannot serve as activity-based evidence;
  explicit user model-mix settings and raw savings/compression event dates stay
  intact. Retention follows ingestion time, so repairing an old activity date
  does not itself evict the session.
- OpenCode retains recording time as metadata and stores model buckets by UTC
  activity day. Its dashboard and savings windows use those slices; unknown
  timestamps remain incomplete. Python uses local calendar days. Baseline and
  recent populations count distinct sessions after selecting activity, so crossing
  midnight cannot manufacture extra sessions.

Regression coverage: `test_activity_dating.py`, `test_usage_counted_once.py`,
`test_dashboard_data.py`, `test_session_cost.py`, `test_savings_invariants.py` and
OpenCode's `trends.activity-dating.test.ts`.

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

## 9. Known limits

- Routing and estimates are estimates. They are labelled and kept separate from
  measured parts.
- Rows from before per-turn evidence was recorded use session-mix or input-rate floors,
  disclosed in the report.
- Rows with no known price stay unpriced, so totals are priced subtotals.
- The headline is an API-rate equivalent, not a bill reconciliation.
