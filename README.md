<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center">
  <a href="https://github.com/alexgreensh/token-optimizer/releases"><img src="https://img.shields.io/badge/version-4.4.0-green" alt="Version 4.4.0"></a>
  <a href="https://github.com/alexgreensh/token-optimizer"><img src="https://img.shields.io/badge/Claude_Code-Plugin-blueviolet" alt="Claude Code Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/openclaw"><img src="https://img.shields.io/badge/OpenClaw-Plugin-brightgreen" alt="OpenClaw Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg" alt="License: PolyForm Noncommercial"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/stargazers"><img src="https://img.shields.io/github/stars/alexgreensh/token-optimizer" alt="GitHub Stars"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/commits/main"><img src="https://img.shields.io/github/last-commit/alexgreensh/token-optimizer" alt="Last Commit"></a>
  <img src="https://img.shields.io/badge/python-3.8+-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <a href="https://linkedin.com/in/alexgreensh"><img src="https://img.shields.io/badge/LinkedIn-Connect-0A66C2?logo=linkedin&logoColor=white" alt="Connect on LinkedIn"></a>
  <a href="https://github.com/sponsors/alexgreensh"><img src="https://img.shields.io/badge/Sponsor-%E2%9D%A4-ff69b4.svg?labelColor=262626" alt="Sponsor"></a>
</p>

<h2 align="center">Your AI is getting dumber and you can't see it.</h2>

<p align="center"><em>Find the ghost tokens. Survive compaction. Track the quality decay.</em></p>

<p align="center">
Opus 4.6 drops from 93% to 76% accuracy across a 1M context window. Compaction loses 60-70% of your conversation. Ghost tokens burn through your plan limits on every single message. Token Optimizer tracks the degradation, cuts the waste, checkpoints your decisions before compaction fires, and tells you what to fix.
</p>

<p align="center">
  <img src="skills/token-optimizer/assets/hero-terminal.svg" alt="Token Optimizer Quick Scan" width="800">
</p>

## Install

```bash
/plugin marketplace add alexgreensh/token-optimizer
/plugin install token-optimizer@alexgreensh-token-optimizer
```

Then in Claude Code: `/token-optimizer`

> **Please enable auto-update after installing.** Claude Code ships third-party marketplaces with auto-update **off by default**, and plugin authors cannot change that default. So you won't get bug fixes automatically unless you turn it on. In Claude Code: `/plugin` → **Marketplaces** tab → select `alexgreensh-token-optimizer` → **Enable auto-update**. One-time, 10 seconds, and you'll never miss a fix again. Token Optimizer also prints a one-time reminder on your first SessionStart so you don't forget.

Also available as a script install, which auto-updates daily via `git pull --ff-only` with no toggles required:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
bash ~/.claude/token-optimizer/install.sh
```

Works on Claude Code and [OpenClaw](#openclaw-plugin). Each platform gets its own native plugin (Python for Claude Code, TypeScript for OpenClaw). No bridging, no shared runtime, zero cross-platform dependencies.

## What makes this different?

`/context` tells you your context is 73% full. Token Optimizer tells you WHY,
shows you which 12K tokens are wasted on skills you never use, finds the 47
orphaned topic files in your MEMORY.md that Claude can't see, checkpoints your
decisions before compaction destroys them, and gives you a quality score that
tracks how much dumber your AI is getting as the session goes on.

One shows the dashboard light. The other opens the hood.

## Why install this first?

Every Claude Code session starts with invisible overhead: system prompt, tool definitions, skills, MCP servers, CLAUDE.md, MEMORY.md. A typical power user burns 50-70K tokens before typing a word.

At 200K context, that's 25-35% gone. At 1M, it's "only" 5-7%, but the problems compound:

- **Quality degrades as context fills.** MRCR drops from 93% to 76% across 256K to 1M. Your AI gets measurably dumber with every message.
- **You hit rate limits faster.** Ghost tokens count toward your plan's usage caps on every message, cached or not. 50K overhead x 100 messages = 5M tokens burned on nothing.
- **Compaction is catastrophic.** 60-70% of your conversation gone per compaction. After 2-3 compactions: 88-95% cumulative loss. And each compaction means re-sending all that overhead again.
- **Higher effort = faster burn.** More thinking tokens per response means you hit compaction sooner, which means more total tokens consumed across the session.

Token Optimizer tracks all of this. Quality score, degradation bands, compaction loss, drift detection. Zero context tokens consumed (runs as external Python).

![What happens inside a 1M session](skills/token-optimizer/assets/user-profiles.svg)

> **"But doesn't removing tokens hurt the model?"** No. Token Optimizer removes structural waste (duplicate configs, unused skill frontmatter, bloated files), not useful context. It also actively *measures* quality: the 7-signal quality score tells you if your session is degrading, and Smart Compaction checkpoints your decisions before auto-compact fires. Most users see quality scores *improve* after optimization because the model has more room for real work.

---

## What It Does

One command. Six parallel agents audit your entire setup. Prioritized fixes with exact token savings. Everything backed up before any change.

![How Token Optimizer works](skills/token-optimizer/assets/how-it-works.svg)

You see diffs. You approve each fix. Nothing irreversible.

---

## What questions can you ask?

| Command | What You Get |
|---------|-------------|
| `quick` | **"Am I in trouble?"** 10-second answer: context health, degradation risk, biggest token offenders, which model to use. |
| `doctor` | **"Is everything installed correctly?"** Score out of 10. Broken hooks, missing components, exact fix commands. |
| `drift` | **"Has my setup grown?"** Side-by-side comparison vs your last snapshot. Catches config creep before it costs you. |
| `quality` | **"How healthy is this session?"** 7-signal analysis of your live conversation. Stale reads, wasted tokens, compaction damage. |
| `report` | **"Where are my tokens going?"** Full per-component breakdown. Every skill, every MCP server, every config file. |
| `conversation` | **"What happened each turn?"** Per-message token + cost breakdown with spike detection. |
| `pricing-tier` | **"What am I paying?"** View or switch between Anthropic/Vertex/Bedrock pricing tiers. |
| `kill-stale` | **"Clean up zombies."** Terminate headless sessions running 12+ hours. |
| `git-context` | **"What files matter right now?"** Test companions, co-changed files, import chains for your current git diff. |
| `trends` | **"What's actually being used?"** Skill adoption, model mix, overhead trajectory over time. |
| `coach` | **"Where do I start?"** Health score with earned vs neutral signals. Detects anti-patterns, shows what's working, flags what's not. |
| `memory-review` | **"Is my MEMORY.md broken?"** Structural audit: orphaned files, broken links, invisible entries past line 200, duplicate rules, stale content. Shows exactly what to fix and how many tokens you'd save. |
| `dashboard` | **"Show me everything."** Interactive HTML dashboard with all analytics, CLAUDE.md/MEMORY.md health cards, staleness warnings. |
| `savings` | **"How much have I saved?"** Cumulative dollar savings from optimizations, checkpoint restores, and archives. |
| `attention-score` | **"Is my CLAUDE.md well-structured?"** Scores sections against the attention curve, flags critical rules in low-attention zones. |
| `jsonl-inspect` | **"What's in this session?"** Record counts, token distribution, top 10 largest records, compaction markers. |
| `expand` | **"Get that result back."** Retrieves tool results archived before compaction. Never re-run a command twice. |
| `/token-optimizer` | **"Fix it for me."** Interactive audit with 6 parallel agents. Guided fixes with diffs and backups. |

---

## Quality Scoring

Seven signals, weighted to reflect real-world impact:

| Signal | Weight | What It Means For You |
|--------|--------|----------------|
| **Context fill** | 20% | How close are you to the degradation cliff? Based on published MRCR benchmarks. |
| **Stale reads** | 20% | Files you read earlier have changed. Your AI is working with outdated info. |
| **Bloated results** | 20% | Tool outputs that were never used. Wasting context on noise. |
| **Compaction depth** | 15% | Each compaction loses 60-70% of your conversation. After 2: 88% gone. |
| **Duplicates** | 10% | The same system reminders injected over and over. Pure waste. |
| **Decision density** | 8% | Are you having a real conversation or is it mostly overhead? |
| **Agent efficiency** | 7% | Are your subagents pulling their weight or just burning tokens? |

### Efficiency Grades

Every quality score includes a letter grade for quick triage. The status line shows something like `ContextQ:A(82)`, and the same grade appears in the dashboard, coach tab, and CLI output.

| Grade | Range | Meaning |
|-------|-------|---------|
| **S** | 90-100 | Peak efficiency. Everything is clean. |
| **A** | 80-89 | Healthy. Minor optimization possible. |
| **B** | 70-79 | Degradation starting. Worth investigating. |
| **C** | 60-69 | Significant waste. Coach mode will help. |
| **D** | 50-59 | Serious problems. Multiple anti-patterns likely. |
| **F** | 0-49 | Context is rotting. Immediate action needed. |

### Degradation Bands

The status bar shifts color as your context fills:

- Green (<50% fill): peak quality zone
- Yellow (50-70%): degradation starting
- Orange (70-80%): quality dropping
- Red (80%+): severe, consider /clear

### What Degradation Actually Looks Like

This is a real session. 708 messages, 2 compactions, 88% of the original context gone. Without the quality score, you'd have no idea.

![Real session quality breakdown](skills/token-optimizer/assets/quality-example.svg)

---

## Smart Compaction: Don't Lose Your Work

When auto-compact fires, 60-70% of your conversation vanishes. Decisions, error-fix sequences, agent state: gone. Smart Compaction saves all of it as checkpoints before compaction, then restores what the summary dropped.

```bash
python3 measure.py setup-smart-compact    # checkpoint + restore hooks
```

### Progressive Checkpoints

Rather than waiting for emergency compaction, Token Optimizer captures session state at multiple thresholds: `20%`, `35%`, `50%`, `65%`, and `80%` context fill, plus quality drops below `80`, `70`, `50`, and `40`. It also snapshots before agent fan-out and after large edit batches. On restore, it picks the richest eligible checkpoint, not just the most recent one.

Background guards handle one-shot threshold capture, cooldown suppression, and deterministic extraction. No LLM calls in the checkpoint path.

### Tool Result Archive

The PostToolUse hook archives large tool results (>4KB) to disk. After compaction, use `expand <tool-use-id>` to retrieve any archived result instead of re-running the command. MCP tool results over 8KB get automatically trimmed with an expand hint.

```bash
python3 measure.py expand --list                 # List all archived tool results
python3 measure.py expand <tool-use-id>          # Retrieve a specific archived result
```

### Session Continuity

Sessions auto-checkpoint on end, /clear, and crashes. On a fresh session, Token Optimizer offers a pointer to the most recent relevant checkpoint instead of auto-injecting old context.

Enable optional local-only checkpoint telemetry to see whether checkpoints are firing and which triggers are active:

```bash
TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 python3 measure.py checkpoint-stats --days 7
```

---

## Live Quality Bar: Know Before It's Too Late

A glance at your terminal tells you if you're in trouble. Colors shift from green to red as quality degrades. When quality drops below 75, session duration appears as a warning. Running subagents show with their model and elapsed time so you can spot misrouted models.

![Status Bar Degradation](skills/token-optimizer/assets/status-bar.svg)

```bash
python3 measure.py setup-quality-bar      # one-time install
```

**My quality bar disappeared, how do I get it back?** Running Claude Code's built-in `/statusline` rewrites the `statusLine` key in `~/.claude/settings.json` and silently overwrites Token Optimizer's entry. SessionStart detects this and **auto-restores** the quality bar. Just start a new session and it's back. You'll see a one-line notice explaining what happened.

**I really don't want the quality bar anymore, how do I turn it off for good?** Run:

```bash
python3 measure.py setup-quality-bar --uninstall
```

This removes the components **and** writes `quality_bar_disabled: true` to `~/.claude/token-optimizer/config.json`. The opt-out is sticky across sessions. SessionStart will not auto-restore it. You can also just tell Claude Code in natural language: _"remove the Token Optimizer statusline"_, and Claude will run the uninstall command for you.

**I changed my mind, bring it back.** Run `python3 measure.py setup-quality-bar`. Explicit install clears the opt-out flag automatically.

**I want to keep my own custom statusline and also see the quality score.** The custom-statusline path is still respected when you run `setup-quality-bar` directly. You'll get integration instructions for reading `~/.claude/token-optimizer/quality-cache.json` from your own script instead.

---

## Interactive Dashboard

After the audit, you get an interactive HTML dashboard.

![Token Optimizer Dashboard](skills/token-optimizer/assets/dashboard-overview.png)

Every component is clickable. Expand any item to see why it matters, what the trade-offs are, and what changes. Toggle the fixes you want, and copy a ready-to-paste optimization prompt.

### What the Dashboard Shows

Click any session to see a per-turn breakdown of input, output, and cache tokens for each API call, with spike detection highlighting context jumps. Each session shows estimated API cost across four pricing tiers (Anthropic API, Vertex Global, Vertex Regional, AWS Bedrock). Set your tier once and all costs update.

Cache visualization uses stacked bars to show input vs output vs cache-read vs cache-write split, with TTL mix (`1h` vs `5m`) shown alongside hit metrics. Session rows include pacing metrics (time between calls) so you can see whether a thread was steady or stop-start. Hover help on columns explains `Cache`, `TTL`, `Pacing`, `Cache R`, and `Cache W` without jargon.

Color-coded quality scores overlay every session: green for healthy, yellow for degrading, red for trouble. Session drill-downs key off stable session identity for consistent expansion.

### Persistent Dashboard

The dashboard auto-regenerates after every session (via the SessionEnd hook). Bookmark it and it's always up to date.

```bash
python3 measure.py setup-daemon     # Bookmarkable URL at http://localhost:24842/token-optimizer
python3 measure.py dashboard --serve # One-time serve over HTTP
```

---

## Usage Analytics

**Trends**: Which skills do you actually invoke vs just having installed? Which models are you using? How has your overhead changed over time?

**Session Health**: Catches stale sessions (24h+), zombie sessions (48h+), and outdated configurations before they cause problems.

```bash
python3 measure.py setup-hook       # Enable session tracking (one-time)
python3 measure.py trends           # Usage patterns over time
python3 measure.py health           # Session hygiene check
python3 measure.py plugin-cleanup   # Detect duplicate skills + archive local/plugin overlaps
```

---

## Memory Health: Your MEMORY.md Is Probably Broken

Claude auto-loads the first 200 lines of MEMORY.md every session. Everything after line 200 is silently truncated. The tokens still count against your window, but Claude never sees the content. Most power users don't know this is happening.

`memory-review` scans your MEMORY.md structurally and tells you what's wrong:

- **Orphaned topic files**: files in your memory directory that nothing links to
- **Broken links**: index entries pointing to files that don't exist
- **Invisible entries**: content below line 200 that Claude can't see (and the topic files they point to)
- **Inline content**: notes that should be in topic files, wasting index budget
- **Duplicate rules**: rules already in CLAUDE.md (which loads in full regardless)
- **Stale entries**: resolved/superseded content still taking up space
- **Task leakage**: TODO lists and checklists that belong in a task tracker

```bash
python3 measure.py memory-review                        # Full structural audit
python3 measure.py memory-review --json                 # Machine-readable for dashboards
python3 measure.py memory-review --apply                # Show actionable fixes
python3 measure.py memory-review --stale-days 90        # Custom staleness threshold
```

The dashboard shows CLAUDE.md Health and MEMORY.md Health cards on the Overview tab, with line count, orphan count, and status at a glance.

For contradiction detection (two rules saying opposite things), run the audit in a Claude session. The tool extracts all NEVER/ALWAYS/MUST rules from both files. Claude reviews them semantically in context, no extra LLM call needed.

---

## Coach Mode: Not Sure Where to Start?

```
> /token-coach
```

Tell it your goal. Get back specific, prioritized fixes with exact token savings. Detects 8 named anti-patterns (The Kitchen Sink, The Hoarder, The Monolith...) and recommends multi-agent design patterns that actually save context.

### Waste Detectors

9 automated detectors analyze your session patterns and surface actionable findings:

| Detector | What it catches |
|---|---|
| PDF/binary ingestion | Large files consuming context (warns with token estimate) |
| Web search overhead | Too many web results dumped into context |
| Retry churn | Same tool retried 3+ times with errors |
| Tool cascade | 3+ consecutive tool errors in a chain |
| Looping | Repeated similar messages (stuck model) |
| Overpowered model | Opus used for simple edits (with "if Sonnet: $X saved") |
| Weak model | Haiku on complex tasks needing a stronger model |
| Bad decomposition | Monolithic 500+ word prompts doing too much |
| Wasteful thinking | Extended thinking >2x output for small edits |

### Subagent Cost Breakdown

See exactly how much your subagents cost: total spend, % of combined budget, and top offenders ranked by cost. Flags when subagents consume >30% of total.

### Costly Prompt Ranking

See which prompts cost the most: pairs each user message with the cost of the response, ranks top 5. Shows what you asked, not just totals.

### CLAUDE.md Routing Injection

Generate model routing instructions from your actual usage data and inject them into CLAUDE.md. Claude reads these every session and routes accordingly. A 48-hour staleness guard auto-removes stale advice. Run on demand when you want to update your routing guidance.

```bash
python3 measure.py inject-routing --dry-run   # Preview what would be injected
python3 measure.py inject-routing              # Inject (with approval)
```

---

## Read-Cache and Context Tools

### PreToolUse Read-Cache

Detects redundant file reads and optionally blocks them with structural digests. Default ON in warn mode. Saves 8-30% tokens from read deduplication.

```bash
# Read-cache is ON by default (warn mode). To disable:
export TOKEN_OPTIMIZER_READ_CACHE=0               # Disable
export TOKEN_OPTIMIZER_READ_CACHE_MODE=block       # Upgrade to block mode

# Read-cache management
python3 measure.py read-cache-stats --session ID   # Cache stats for a session
python3 measure.py read-cache-clear                # Clear all caches
```

Opt out entirely with `TOKEN_OPTIMIZER_READ_CACHE=0` or config `{"read_cache_enabled": false}`. Upgrade to `TOKEN_OPTIMIZER_READ_CACHE_MODE=block` after gaining confidence.

### Git-Aware Context

Analyzes your working tree to suggest files that should be in context: test companions, frequently co-changed files from last 50 commits, and import chains for Python/JS/TS.

```bash
python3 measure.py git-context                     # Suggest files for current changes
python3 measure.py git-context --json              # Machine-readable output
```

### .contextignore

Block files from being read with gitignore-style patterns. Supports project root `.contextignore` and global `~/.claude/.contextignore`. Hard block regardless of read-cache mode. This is provided by Token Optimizer, not a built-in Claude Code feature.

```
# Block build artifacts and lockfiles
dist/**
node_modules/**
package-lock.json
yarn.lock
*.min.js
*.min.css
```

### Attention Optimizer

Scores CLAUDE.md against the U-shaped attention curve. Flags critical rules (NEVER/ALWAYS/MUST) sitting in the low-attention zone (30-70% position). Generates a reordered version that moves critical rules to high-attention zones.

```bash
python3 measure.py attention-score               # Score CLAUDE.md attention placement
python3 measure.py attention-optimize --dry-run  # Preview optimized section order
```

### JSONL Toolkit

Three utilities for session JSONL files: `jsonl-inspect` (stats, record counts, largest records), `jsonl-trim` (replace large tool results with placeholders), `jsonl-dedup` (detect and remove duplicate system reminders). All use streaming I/O and atomic writes.

```bash
python3 measure.py jsonl-inspect                 # Stats on current session JSONL
python3 measure.py jsonl-trim --dry-run          # Preview trimming large tool results
python3 measure.py jsonl-dedup --dry-run         # Preview removing duplicate reminders
```

### Savings Tracking

Tracks cumulative dollar savings from setup optimization, checkpoint restores, and tool archiving.

```bash
python3 measure.py savings                      # Dollar savings report (last 30 days)
```

---

## How It Compares

| Capability | Token Optimizer | `/context` (built-in) | context-mode |
|---|---|---|---|
| Startup overhead audit | Deep (per-component) | Summary (v2.1.74+) | No |
| Quality degradation tracking | MRCR-based bands | Basic capacity % | No |
| Guided remediation | Yes, with token estimates | Basic suggestions | No |
| Runtime output containment | No | No | Yes (98% reduction) |
| Smart compaction survival | Progressive checkpoints + restore | No | Session guide |
| Tool result archive | Yes (cross-compaction recovery) | No | No |
| Dollar savings tracking | Yes (per-category breakdown) | No | No |
| JSONL session toolkit | Inspect, trim, dedup | No | No |
| Attention curve optimizer | Yes (CLAUDE.md reordering) | No | No |
| Waste pattern detection | 9 automated detectors | No | No |
| Subagent cost breakdown | Yes (ranked by $) | No | No |
| Costly prompt ranking | Yes (top 5 by cost) | No | No |
| Model recommendation | Yes (Sonnet vs Opus by context) | No | No |
| CLAUDE.md advice injection | Yes (with 48h TTL) | No | No |
| MEMORY.md structural audit | 8 detectors + fix suggestions | No | No |
| CLAUDE.md/MEMORY.md health cards | Dashboard overview | No | No |
| Validate optimization impact | Before/after comparison | No | No |
| Usage trends + dashboard | SQLite + interactive HTML | No | Session stats |
| Per-turn cost analytics | Yes (4 pricing tiers) | No | No |
| Compaction loss tracking | Yes (cumulative % lost) | No | Partial |
| Multi-platform | Claude Code + OpenClaw | Claude Code | 6 platforms |
| Context tokens consumed | 0 (Python script) | ~200 tokens | MCP overhead |

`/context` shows capacity. Token Optimizer fixes the causes.
context-mode prevents runtime floods. Token Optimizer prevents structural waste.

---

## VS Code Users

Using Claude Code in the VS Code extension? Most of Token Optimizer works identically:

| Feature | CLI | VS Code Extension |
|---------|-----|-------------------|
| Smart Compaction (checkpoint + restore) | Works | Works |
| Quality tracking + session data | Works | Works |
| All hooks (SessionEnd, PreCompact, etc.) | Works | Works |
| Dashboard (localhost:24842/token-optimizer) | Works | Works |
| Status line (quality bar in terminal) | Works | Not available |

**The status line is CLI-only.** The VS Code extension doesn't support Claude Code's `statusLine` setting. This is a Claude Code limitation, not a Token Optimizer limitation.

**Best options for VS Code:**
- **Dashboard**: Bookmark `http://localhost:24842/token-optimizer` for always-current analytics. Run `python3 measure.py setup-daemon` to enable auto-refresh after every session.
- **Integrated terminal**: Run `claude` in VS Code's built-in terminal to get the full CLI experience, including the quality bar.
- **VS Code extension**: On the roadmap. [Follow #3](https://github.com/alexgreensh/token-optimizer/issues/3) for updates.

> **Note on `--bare` mode**: Running Claude Code with the `--bare` flag (for scripted/CI usage) skips all hooks and plugin sync. Token Optimizer's Smart Compaction, quality tracking, and session data collection require hooks and won't activate in `--bare` mode. This is expected, `--bare` is designed for lightweight scripted calls.

---

## OpenClaw Plugin

Native TypeScript plugin for OpenClaw agent systems. Zero Python dependency. Works with any model (Claude, GPT-5, Gemini, DeepSeek, local via Ollama). Reads your OpenClaw pricing config for accurate cost tracking, falls back to built-in rates for 20+ models.

```bash
# From GitHub (recommended)
openclaw plugins install github:alexgreensh/token-optimizer

# From ClawHub
openclaw plugins install token-optimizer

# From source
git clone https://github.com/alexgreensh/token-optimizer
cd token-optimizer/openclaw && npm install && npm run build
openclaw plugins install ./
```

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

**What it does:** Session parsing, cost calculation, waste detection (9 detectors including unused skill detection), Coach tab with health scoring, per-turn token breakdown with cache analysis, costly prompt ranking, agent cost analysis (orchestrator vs worker), topic extraction, and Smart Compaction (checkpoint/restore across compaction events).

**What's different from Claude Code:** The OpenClaw plugin includes its own 7-signal ContextQ with signals native to OpenClaw's architecture (Message Efficiency, Compression Opportunity, Model Routing, etc.) rather than a direct port of Claude Code's signals. The Coach tab adapts scoring to OpenClaw concepts (SOUL.md instead of CLAUDE.md, agent configs instead of hooks). Works with any model: Claude, GPT-5, Gemini, DeepSeek, local via Ollama.

See [`openclaw/README.md`](openclaw/README.md) for full docs.

---

## License

**PolyForm Noncommercial 1.0.0**. Free for personal, research, educational, and non-commercial use. Commercial use requires a separate license. Contact [Alex Greenshpun](https://linkedin.com/in/alexgreensh) for commercial licensing.

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
