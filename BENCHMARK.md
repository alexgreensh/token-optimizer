# Token Optimizer: Benchmark Report

This observational report uses one **30-day snapshot ending 2026-11-01** (2026-10-02 to 2026-11-01), generated on 2026-11-01. It reports an all-in **priced subtotal of about $598.49** and **24.83M tokens saved**. Pricing is incomplete: unpriced repeat-read credits and debits are excluded, and the complete net dollar value is unavailable. Your results will differ.

The independent **Terminal-Bench floor** (the full 81-task Terminal-Bench 2 suite, plugin on versus off, multiple trials with confidence intervals) and [controlled A/B](CONTROLLED-A-B-BENCHMARK.md) remain separate studies. Their results are not added to this snapshot. [All three reports](https://token-optimizer.dev/reference/benchmarks/).

## All-in value, with evidence labels

| Component | API-rate-equivalent value | Evidence |
|---|---:|---|
| Logged actions | **$79.06** | Metered token changes, valued at API rates; 4,974 events. |
| Model routing | **$422.56** | Estimated reference-card comparison; all of this component is estimated. |
| Repeat reads avoided | **$61.21** | Modeled from observed turns and known per-turn rates; priced contributions only. |
| Other estimates | **$35.66** | Eight selected estimate categories, listed below. |
| **All-in priced subtotal** | **≈ $598.49** | **Pricing is incomplete; the full net dollar value is unavailable.** |

The four components add up to the subtotal to the cent. Unpriced repeat-read credits and debits are excluded, so this subtotal is not a lower bound. It is an API-rate equivalent, not a subscription refund.

## The same snapshot's token card

| Token-card component | Tokens | Share of spent + saved |
|---|---:|---:|
| Spent, provider-stats-compatible | 285,934,623 | 92.01% |
| Saved, metered | 9,866,280 | 3.17% |
| Saved, estimated | 14,966,284 | 4.82% |
| **Spent + saved** | **310,767,187** | **100%** |

Percentage shares are rounded to two decimals and add up to 100%.

The token card reports **24,832,564 saved tokens (24.83M)**, or **7.99%**, displayed as **8%** on the dashboard. The formula is `saved / (spent + saved)`, not `saved / spent`.

Its estimated slice is 14,946,284 lean-resume tokens, 15,000 MCP-cap tokens, 5,000 hint-served tokens and 0 verbosity-nudge tokens. Modeled reread tokens and routing are outside this token-card total. The dollar subtotal includes their priced contributions and the other estimates below; the two headlines have different scopes.

## Logged actions

| Mechanism | Events | Tokens | Priced value |
|---|---:|---:|---:|
| Observed post-nudge output change | 648 | 2,659,960 | $52.2794 |
| Tool-output archive | 399 | 39,058 | $0.0000 |
| Checkpoint restores | 208 | 166,371 | $0.6481 |
| Structure maps | 1 | 5,939 | $0.0238 |
| Quality nudges | 57 | 694,535 | $1.3891 |
| Bash pipeline compression | 3,418 | 5,986,663 | $23.6986 |
| Git output compression | 40 | 214,135 | $0.7257 |
| Loop detection | 98 | 70,807 | $0.2862 |
| Build output compression | 104 | 28,709 | $0.0061 |
| Cross-turn dedup | 1 | 103 | $0.0004 |
| **Total** | **4,974** | **9,866,280** | **$79.0574** |

These are recorded token changes, not proof of causal attribution. In particular, a post-nudge output change is an observed before/after comparison. Dollar cells contain the recorded priced amount; a zero dollar cell does not establish a free or known-zero-rate event. The four-decimal category values add up to the total.

## Model routing

The **$422.56 routing component is entirely estimated**. It compares the frozen pre-tool model mix with current usage using fixed generation reference cards and token classes. It does not treat a change in a model's published price as a routing saving, and it does not establish that the optimizer caused every model choice. Unpriced token classes do not acquire a fallback family price. Routing is included once in the all-in subtotal.

## Modeled repeat reads and pricing coverage

| Removal-cohort measure | Value |
|---|---:|
| Stored removal events | 4,368 |
| Net tokens removed at the time | 6,413,423 |
| Stored modeled reread tokens, signed | 316,871,023 |
| Priced reread contribution | $61.208855 |
| Unpriced reread credits | 2,193,970 tokens |
| Unpriced reread debits | 156,846 tokens |
| Historical events with unverifiable prices | 305 |
| Eligible events with unmeasured trajectories | 103 |
| Known removal credits in unmeasured events | 27,178 tokens |
| Known removal debits in unmeasured events | 0 tokens |

The absolute unpriced coverage is **2,350,816 tokens**; credits and debits do not cancel that uncertainty. The 305 incomplete historical refreshes are retained, not deleted or treated as priced zero. Only known contributions enter the subtotal.

The 103 events with unmeasured trajectories have known removal amounts, but their reread counts and dollar values are unavailable. Those removal tokens are not added to the unpriced reread count or valued as zero.

Rereads start at the second post-removal call and stop at the next real compaction, with the observed trajectory held fixed. Known model and prompt-class cards price each turn. The first call's one-shot removal value is not added again to logged actions. This is a removal cohort: its boundaries are **2026-10-02 01:16:30 UTC to 2026-11-01 01:16:30 UTC**, and later rereads remain attributed to their removal date. It is not a claim that every reread occurred inside that exact time window.

## Other estimates, all eight categories

| Estimate | Recorded volume | Estimated tokens | Value |
|---|---:|---:|---:|
| Lean session resumes | 64 resumes | 14,946,284 | $27.9481 |
| Sub-agent runtime compression | 1,566 dispatches | 1,370,884 | $6.8544 |
| Loops prevented | 98 events | 70,807 | $0.3540 |
| MCP output caps | 3 caps | 15,000 | $0.0600 |
| Contamination exit | 1 heeded session | 0 | $0.0000 |
| Handover reruns avoided | 72 restored sessions | 87,632 | $0.4382 |
| Hint-served reads | 1 hint used | 5,000 | $0.0100 |
| Verbosity nudges | 1,059 nudges | 0 | $0.0000 |
| **Total** | | | **$35.6647** |

All eight selected categories are included, including the two zero-valued estimates. Hint-served reads are selected because a hint event exists; the alternative retrieval-serve estimate is not added. These token columns are not summed into the token-card headline, which includes only its four named estimate pools.

## Scope and measurement

The snapshot contains **1,640 main work sessions and 3 delegated sessions**, 1,643 in total, from Claude Code. The token card's provider-stats-compatible spent denominator is fresh input plus reported streamed assistant output from top-level sessions, without streaming deduplication. Request-deduplicated usage is a different metric. Some action token counts use text-length proxies; dollars are API-rate equivalents, not subscription cash savings.

Measured token changes, modeled rereads and estimated routing/behavior remain distinguishable within the all-in subtotal. Missing prices stay unpriced. New verified history survives transcript rotation; unverifiable legacy prices stay quarantined while their stored token evidence remains available.

Pricing uses recognized model and token-class cards. Premium modes or service modifiers absent from recorded usage are not reconstructed, so these figures are not a complete bill reconciliation. Sub-agent estimates also depend on the runtime's available telemetry; this Claude Code snapshot does not establish equivalent coverage on other platforms.

Shared read-time filters exclude test fixtures, media-only archive events and duplicate-install telemetry from counted views. Source ledger rows remain intact. Opportunity estimates, setup projections and whole-workload transformation estimates are not added to this action subtotal.

## Separate compression checks

<details>
<summary>Compression safety, the 87-fixture suite</summary>

Before trusting compression on real output, we validate it never removes what the model needs. The suite holds **87 fixtures across 22 categories**. Each fixture defines raw output, a must-preserve list, a must-not-contain list (catches hallucination), and a minimum ratio. It passes only when all three hold.

| Category | # | What's tested |
|---|---|---|
| test_exts | 10 | test-runner variants across file extensions |
| build | 8 | cargo, make, webpack, tsc, gradle |
| git | 7 | status, log, diff, merge conflicts |
| lint | 7 | eslint, ruff, clippy, pylint |
| logs | 7 | nginx, docker, systemd, application |
| 🔄 tee_on_failure | 5 | failed commands keep full output |
| json | 4 | value-preserving structured compression |
| csv | 4 | columnar compression |
| stack_trace | 4 | frame-limited tracebacks |
| cloud_cli | 4 | aws, gcloud, az output |
| search_results | 4 | grep and web search condensed to top hits |
| 🔒 security | 3 | AWS keys, GitHub PATs, Slack tokens (must NOT be stripped) |
| tree | 3 | nested directory structures |
| progress | 3 | download and install progress bars |
| list | 3 | long listings |
| k8s | 3 | kubectl output |
| test_runner | 2 | pytest, jest, go test |
| ⚠️ error | 2 | non-zero exit, permission denied (must pass through raw) |
| package_install | 1 | pip and package managers |
| directory_listing | 1 | large `ls` output |
| npm_install | 1 | npm ci and install |
| test | 1 | generic test output |

The security, error and tee-on-failure groups are the load-bearing guardrails. Compression never costs you a credential, an error message, or the output of a failed command.

Reproduce from the repository checkout: `python3 skills/token-optimizer/scripts/benchmark.py`

</details>

## Skeleton and quality methods

Large code files can be served as a recoverable skeleton only after a cohort meets its history gates: edit-within-five-turns below 15%, at least 20 reads and at least five sessions. A live tripwire demotes an active cohort when its edit rate exceeds 15%. Markdown stays measure-only by policy. The requested file is served in full; related-file skeletons use an archived original and fail open when archiving fails. Current cohort statistics are available from `measure.py cohorts status --json`; they are not included in this savings total.

Session quality uses context fill, stale reads, bloated results, compaction depth, decision density, agent efficiency and waste signals. Signals vary by platform. This snapshot does not supply a grade histogram or a new historical skeleton-corpus valuation, so neither is presented as a current result.

## Reproduce on your own data

```bash
# From the repository checkout: dashboard and action components
python3 skills/token-optimizer/scripts/measure.py dashboard
python3 skills/token-optimizer/scripts/measure.py savings
python3 skills/token-optimizer/scripts/measure.py savings --json

# Compression events and cohort state
python3 skills/token-optimizer/scripts/measure.py compression-stats --days 30 --json
python3 skills/token-optimizer/scripts/measure.py cohorts status --json

# Separate deterministic compression fixtures
python3 skills/token-optimizer/scripts/benchmark.py
python3 skills/token-optimizer/scripts/benchmark.py --json
```

The dashboard and CLI report your own window and coverage. Keep the evidence labels and unavailable-price notes when sharing the figures.
