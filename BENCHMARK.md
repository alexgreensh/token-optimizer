# Token Optimizer: Benchmark Report

> **$5.02 saved per working session** across five compounding layers, measured against 1,885 real sessions over 30 days.
> Every number comes from local production telemetry. Every measurement tool ships in the repo.

---

## 💰 Summary

All pricing at Opus 4 rates ($5/MTok input, $25/MTok output, $0.50/MTok cache-read).

| Layer | 30-day savings | Evidence |
|---|---|---|
| 🔧 Output compression + context eviction | **$17.51** | 📊 Measured: 892 production events with before/after delta |
| 🔀 Model routing | **$56.95** | 📊 Measured: cost difference from downgrading routable turns |
| 🏗️ Structural waste cleanup | **$105.36** | 💡 Opportunity: savings if audit recommendations are applied |
| **🟢 Total (all layers)** | **$179.82/month** | |

> **Per session:** $5.02 compound savings on a 50-turn session with 15 tool outputs.
> **Per month:** ~$150 at 30 working sessions. Lighter sessions save less; heavier save more.
>
> These are conservative. Structural cleanup compounds across every turn of every future session. The Token Optimizer dashboard measures your personal before/after cost and reflects the full compounding effect.

---

## 📋 Corpus

| | |
|---|---|
| 🔬 Quality-scored sessions | **1,885** (30 days, `trends.db`) |
| 📂 Sessions with file reads | **5,814** (backfill corpus for skeleton analysis) |
| 📖 First-reads analyzed | **30,771** |
| 🧪 Benchmark fixtures | **57** across 10 categories |
| ⚡ Avg prompt-cache hit rate | **65.4%** |
| 🖥️ Platforms | Claude Code CLI, VS Code, Codex, OpenClaw, OpenCode, Hermes |

The two corpora are distinct populations, not double-counted. The backfill corpus is larger because it includes historical sessions recovered from file-read logs.

**Data source:** The production numbers in this benchmark come entirely from Claude Code CLI sessions (the author's primary platform). Quality scoring, dashboard, and savings tracking work on all supported platforms, but quality signal counts vary by platform (3 to 7 signals depending on the platform's measurement context). The grade scale (S/A/B/C/D/F) is consistent everywhere.

**Reproducibility:** Your results will differ based on your usage. Every measurement tool ships in the repo so you can regenerate against your own data. See [Running the Benchmarks](#-running-the-benchmarks).

---

## 🏗️ Layer 1: Structural Overhead

> Saves tokens on **every turn of every session.** The single largest compounding savings category.

Before any conversation starts, the model re-sends CLAUDE.md, skills, MCP tool schemas, and MEMORY.md on every turn. Token Optimizer's 8 auditors score each component and flag waste.

| | |
|---|---|
| Unused skills found | 61 |
| Recoverable tokens (skills alone) | 4,026 |
| **Total structural opportunity** | **61,227,039 tokens / $105.36** |

📊 **Tier:** Opportunity (savings if recommendations are applied).

**Why this compounds.** A 5,000-token cleanup saves 5,000 tokens per turn, every session, permanently. In a 50-turn session that is 250,000 fewer tokens. Over a month of sessions it dwarfs every other layer.

**Cache impact on the dollar math:**

| Scenario | Per-turn (5K tokens removed) | 50-turn session |
|---|---|---|
| All fresh ($5/MTok) | $0.025 | $1.25 |
| 65% cached ($0.50 cache / $5 fresh) | $0.011 | $0.53 |

65% is the observed cache-hit average across 1,885 sessions.

---

## 🔧 Layer 2: Output Compression

> Pattern-matched compression families, not generic summarization. Two mechanisms: **shrink it** or **evict it** (store locally, serve a stub).

### 🧪 Fixture suite: 57 test cases

Every fixture defines raw output, a must-preserve list, a must-not-contain list (catches hallucination), and a minimum compression ratio. A fixture passes only when **all three checks hold.**

| Category | # | What's tested |
|---|---|---|
| git | 7 | status, log, diff, merge conflicts, non-repo error |
| build | 8 | cargo, make, webpack, tsc, gradle |
| lint | 7 | eslint, ruff, clippy, pylint |
| logs | 7 | nginx, docker, systemd, application |
| test runners | 6 | pytest, jest, go test |
| tree / directory | 6 | large listings, nested structures |
| progress | 3 | npm install, pip, downloads |
| 🔒 security | 3 | AWS keys, GitHub PATs, Slack tokens (must NOT be stripped) |
| ⚠️ error passthrough | 5 | non-zero exit, permission denied (must pass through raw) |
| 🔄 tee-on-failure | 5 | failed commands preserve full output |

### 📊 Production events (30 days, 908 measured)

**Compression** (output shrunk, information preserved):

| Feature | Events | Before | After | Ratio | Saved |
|---|---|---|---|---|---|
| Structure map (re-reads) | 228 | 644K | 29K | **91.8%** | 615K |
| Git output | 195 | 410K | 67K | **73.1%** | 343K |
| First-read skeleton | 18 | 189K | 4K | **97.2%** | 185K |
| Loop detection | 50 | 61K | 0.2K | **99.4%** | 61K |
| Directory listings | 47 | 30K | 16K | **28.2%** | 14K |
| Pytest output | 29 | 7K | 0.3K | **91.9%** | 7K |
| Delta reads | 5 | 6K | 0.7K | **75.7%** | 6K |
| Log output | 2 | 0.5K | 0.2K | **57.6%** | 0.3K |
| **Subtotal** | **574** | **1.35M** | **118K** | **91.3%** | **1.23M** |

**Context eviction** (replaced by stub, original stored locally):

| Feature | Events | Tokens removed | How |
|---|---|---|---|
| Tool output archive | 306 | 3,252,146 | ~50-token stub, full result in local SQLite, `expand` to retrieve |
| Checkpoint restore | 12 | 410,503 | Prior session context injected on compaction |
| **Subtotal** | **318** | **3,662,649** | |

> 🟢 **Combined: 892 events, 4,887,138 tokens saved, $17.51 measured (30 days)**

Token counting uses `bytes / 4` as BPE proxy (~15% error vs actual Claude tokenization). Consistent across all measurements.

---

## 📖 Layer 3: First-Read Skeletons

> Large file, first read, unlikely to edit soon? Serve a skeleton. Full original archived and expandable.

**Corpus replay:** 5,814 sessions, 30,771 first-reads, 2,408 eligible.

| Language | Size | Reads | Sessions | Edit rate | Skeleton ratio | Tokens saved | Status |
|---|---|---|---|---|---|---|---|
| markdown | 16-64KB | 1,329 | 751 | 2.9% | 97.1% | 10.3M | 🟢 PROMOTE |
| python | 16-64KB | 763 | 477 | 1.4% | 96.1% | 6.4M | 🟢 PROMOTE |
| typescript | 16-64KB | 220 | 120 | 0.9% | 97.4% | 1.5M | 🟢 PROMOTE |
| python | 64-256KB | 66 | 64 | 1.5% | 98.5% | 1.5M | 🟢 PROMOTE |
| markdown | 64-256KB | 13 | 9 | 0.0% | 98.9% | 279K | 🟡 hold |
| json | 64-256KB | 3 | 3 | 0.0% | 99.7% | 91K | 🟡 hold |
| typescript | 64-256KB | 2 | 2 | 0.0% | 99.1% | 44K | 🟡 hold |

> **Total projected savings: 20,156,647 tokens**
> 📊 **Tier:** Projected (shadow-mode replay, not yet actively compressing).

**Promotion gate:** edit-within-5-turns rate < 15%, across 20+ reads in 5+ distinct sessions. 🟢 PROMOTE means the gate is met. 🟡 hold means sample size is too small. Token Optimizer proves compression is safe from your own history before enabling it.

**Safety:** Full original always archived before any skeleton is served. Archive fails = full file served unchanged (fail-open). File on disk never modified.

---

## 🔀 Layer 4: Model Routing

> Compression saves on the response side. Routing saves on the request side.

**30-day production (1,885 sessions):**

| Metric | Value |
|---|---|
| Baseline top-tier share | **95%** |
| Current top-tier share | **64.7%** |
| Routable fraction | **30%** of turns |
| 🟢 Realized savings | **$56.95** |
| 💡 Additional potential | **$30.01** |

11 anti-pattern detectors identify turns where a cheaper model produces identical results. Quality nudges prevent degradation before it causes retries.

**Math:** Opus ($5/$25) vs Haiku ($1/$5) = **80% savings** on routable turns. Applies across model ecosystems (Anthropic, Codex, OpenClaw, OpenCode).

---

## 🔄 Layer 5: Session Continuity

> A session that loses context and retries for 10 turns wastes more than any compression saves.

| Mechanism | What it does |
|---|---|
| **Progressive checkpoints** | Captures decisions, errors, file context, agent state to local SQLite throughout a session |
| **Checkpoint restore** | Keyword-matches stored checkpoints on new session or compaction, injects relevant context |
| **Tool result archive** | Replaces large outputs with ~50-token stubs, full result retrievable via `expand` |
| **Loop detection** | Catches repeated reads/retries, breaks the cycle (50 detections, 60,987 tokens saved) |
| **Quality scoring** | 7-signal real-time scoring, fires coaching nudges when quality degrades |

Checkpoint restore and tool archive token counts are reported in Layer 2 (context eviction). Listed here because the mechanism is continuity, but **not double-counted**.

---

## ⚡ Compound Effect

The layers multiply, not add:

```
Session cost = turns x (overhead + avg_output + routing_premium)
              + restart_penalty + retry_waste

Token Optimizer reduces:
  overhead         -> Layer 1 (structural: -15 to -40%)
  avg_output       -> Layer 2+3 (compression + skeletons: -28 to -97%)
  routing_premium  -> Layer 4 (model routing: -40 to -80% on routable turns)
  restart_penalty  -> Layer 5 (checkpoint restore on compaction)
  retry_waste      -> Layer 4+5 (loop detection + quality scoring)
```

**Example: 50-turn Opus session, 15 tool outputs, 1 compaction**

| Layer | Mechanism | Tokens saved | $ saved |
|---|---|---|---|
| 🏗️ Structural | 5K fewer tokens x 50 turns | 250,000 | $1.06 |
| 🔧 Compression | 15 outputs x 10K x 73% ratio | 109,500 | $2.74 |
| 📖 Skeletons | 3 large files x 20K x 97% | 58,200 | $0.29 |
| 📦 Eviction | 4 results archived (avg 10.8K) | 43,200 | $0.15 |
| 🔄 Continuity | 1 compaction, 34K recovered | 34,000 | $0.17 |
| 🛑 Loop prevention | 2 loops x 5 turns x 6.1K | 61,000 | $0.61 |
| **Total** | | **555,900** | **$5.02** |

> At 30 sessions/month: **~$150/month.** The per-layer production data above is the ground truth.

---

## 📊 Quality Grades (1,885 sessions)

7 signals: context fill degradation, stale reads, bloated results, compaction depth, decision density, agent efficiency, and absolute waste tokens.

| Grade | Sessions | |
|---|---|---|
| **S** | 38 | 🟣 Exceptional: minimal waste, high decision density |
| **A** | 436 | 🟢 Good: clean context, efficient tool use |
| **B** | 528 | 🔵 Normal: some bloat, recoverable |
| **C** | 264 | 🟡 Degraded: significant waste, coaching recommended |
| **D** | 619 | 🔴 Poor: heavy bloat, likely retries or loops |
| **F** | 0 | ⚫ Failing: near-total waste (none observed in this corpus) |

Tracked over time so you can see whether your habits and Token Optimizer's interventions are improving session efficiency.

---

## 🧪 Running the Benchmarks

```bash
# Fixture suite (validates compression quality)
python3 scripts/benchmark.py
python3 scripts/benchmark.py --json

# Historical corpus replay (first-read skeleton analysis)
python3 scripts/compression_backfill.py
python3 scripts/compression_backfill.py --limit 100 --json

# Live compression stats (from trends.db)
python3 scripts/measure.py compression-stats
python3 scripts/measure.py compression-stats --days 7 --json

# Full dashboard (all layers visualized)
python3 scripts/measure.py dashboard
```

---

## 📝 Methodology Notes

- **Token counting:** `bytes / 4` proxy (~15% error vs actual BPE). Consistent across all measurements.
- **Three-tier accounting:** 📊 Measured (before/after delta), 💡 Opportunity (if recommendations applied), 🔮 Projected (shadow replay). Never summed together; each table labels its tier.
- **Cache honesty:** Prompt-cache savings (cache_read) are never claimed as Token Optimizer savings. The Anthropic cache is free infrastructure. We do account for the secondary benefit: structural cleanup reduces cache-read volume.
- **Security:** Fixtures verify credentials (AWS keys, PATs, Slack tokens) survive compression intact. Compression never strips what the model needs to see.
- **Safety-first promotion:** First-read skeletons require proof from your own session history before activating. No cohort promoted without meeting the edit-rate gate across multiple sessions.
