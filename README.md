<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center">
  <a href="https://github.com/alexgreensh/token-optimizer/releases"><img src="https://img.shields.io/badge/version-5.6.6-green" alt="Version 5.6.6"></a>
  <a href="https://github.com/alexgreensh/token-optimizer"><img src="https://img.shields.io/badge/Claude_Code-Plugin-blueviolet" alt="Claude Code Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/openclaw"><img src="https://img.shields.io/badge/OpenClaw-v2.4.1-brightgreen" alt="OpenClaw v2.4.1"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/docs/codex-beta.md"><img src="https://img.shields.io/badge/Codex-v0.1.0--beta-orange" alt="Codex v0.1.0-beta"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg" alt="License: PolyForm Noncommercial"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/stargazers"><img src="https://img.shields.io/github/stars/alexgreensh/token-optimizer" alt="GitHub Stars"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/commits/main"><img src="https://img.shields.io/github/last-commit/alexgreensh/token-optimizer" alt="Last Commit"></a>
  <img src="https://img.shields.io/badge/python-3.8+-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/dependencies-zero-green" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/telemetry-none-green" alt="Zero telemetry">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <a href="https://linkedin.com/in/alexgreensh"><img src="https://img.shields.io/badge/LinkedIn-Connect-0A66C2?logo=linkedin&logoColor=white" alt="Connect on LinkedIn"></a>
  <a href="https://github.com/sponsors/alexgreensh"><img src="https://img.shields.io/badge/Sponsor-%E2%9D%A4-ff69b4.svg?labelColor=262626" alt="Sponsor"></a>
</p>

<h2 align="center">Your AI is getting dumber and you can't see it.</h2>

<p align="center"><em>Save tokens. Survive compaction. Measure the proof.</em></p>

<p align="center">
<strong>Most token tools only touch one slice of the problem.</strong>
</p>
<p align="center">
They compress command output, which covers 15-25% of your context on a good day. The other 75-85% (bloated configs, unused skills, duplicate system prompts, stale memory, plus the 60-70% you lose on every compaction) goes untouched.
</p>
<p align="center">
Token Optimizer covers all of it, keeps your work alive across compactions, measures whether the optimization actually helped, and gives you a <strong>live dashboard</strong> that shows every token, every dollar, and every turn, auto-updated after every session. Runs fully local. Zero context tokens used. Zero runtime dependencies.
</p>
<p align="center">
Works on <strong>Claude Code</strong>, <strong>OpenClaw</strong>, and <strong>Codex</strong> (beta) today. Windsurf, Cursor, and more on the way.
</p>

<p align="center">
  <img src="skills/token-optimizer/assets/hero-terminal.svg" alt="Token Optimizer Quick Scan" width="800">
</p>

## Install

**Recommended on every platform (macOS, Linux, Windows):**

```
/plugin marketplace add alexgreensh/token-optimizer
/plugin install token-optimizer@alexgreensh-token-optimizer
```

Then in Claude Code: `/token-optimizer`

> **Please enable auto-update after installing.** Claude Code ships third-party marketplaces with auto-update **off by default**, and plugin authors cannot change that default. So you won't get bug fixes automatically unless you turn it on. In Claude Code: `/plugin` → **Marketplaces** tab → select `alexgreensh-token-optimizer` → **Enable auto-update**. One-time, 10 seconds, and you'll never miss a fix again. Token Optimizer also prints a one-time reminder on your first SessionStart so you don't forget.

<details>
<summary><h3>Windows users: read this first</h3></summary>

The plugin install above is the **only** path you should use on Windows. Do **not** also run the `install.sh` script described below — that's a bash installer for macOS/Linux/WSL, and combining the two creates an `EBUSY: resource busy or locked` error because Git Bash holds Windows file handles open while the plugin system is trying to clone.

**Repo size note**: our repo is ~3 MB (218 files, ~2,700 git objects). If your `/plugin marketplace add` attempt seems to be downloading gigabytes, it's not us — cancel and check whether Claude Code is cloning a different URL or network state. You can verify by cloning manually: `git clone --bare https://github.com/alexgreensh/token-optimizer.git` should finish in under a second and produce a ~2.6 MB directory.

If you've already hit the EBUSY error:

1. Close every Claude Code window and Git Bash terminal.
2. Open Task Manager and end any lingering `git.exe` processes.
3. Delete both folders if they exist:
   - `C:\Users\<you>\.claude\token-optimizer`
   - `C:\Users\<you>\.claude\plugins\marketplaces\alexgreensh-token-optimizer`
4. If Windows still refuses to delete (file in use), reboot, then delete.
5. Open a fresh Claude Code window and run the two `/plugin` commands above.

**Manual ZIP fallback** (if plugin install repeatedly fails): download [the repo ZIP](https://github.com/alexgreensh/token-optimizer/archive/refs/heads/main.zip) (~800 KB), extract to `C:\Users\<you>\.claude\token-optimizer\`, then run `python measure.py setup-quality-bar` from that directory. Note: on Windows the command is `python`, not `python3`.

</details>

<details>
<summary><h3>macOS / Linux only: script install (alternative)</h3></summary>

If you prefer a script-managed install on macOS or Linux, this works too and auto-updates daily via `git pull --ff-only`. **Do not run this on Windows, and do not run it alongside the plugin install above on any platform.** Pick one method.

```bash
git clone https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
bash ~/.claude/token-optimizer/install.sh
```

Works on Claude Code and [OpenClaw](#openclaw-plugin). Each platform has its own native plugin (Python for Claude Code, TypeScript for OpenClaw). No bridging, no shared runtime, zero cross-platform dependencies.

</details>

<details>
<summary><h3>Codex (beta)</h3></summary>

Token Optimizer works on OpenAI Codex (CLI and Desktop). Same core engine, adapted for AGENTS.md, GPT-5.x models, and Codex's hook surface. This is a **beta** -- core audit, coaching, dashboard, and fleet scanning work. Some advanced features (Delta Mode, Structure Map, invisible Bash compression) are waiting on upstream Codex hook parity.

```bash
codex plugin marketplace add alexgreensh/token-optimizer
```

Then in the Codex TUI: `/plugins` and install Token Optimizer. Ask for it conversationally: "Run Token Optimizer".

After install, set up hooks and the bookmarkable dashboard:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD"
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py setup-daemon
```

Dashboard: `http://localhost:24843/token-optimizer` (separate port from Claude Code's 24842, both can run side by side).

Auto-updates on startup via `git ls-remote`. Manual: `codex plugin marketplace upgrade`.

See [`docs/codex-beta.md`](docs/codex-beta.md) for the full feature parity table, hook profiles, and Codex model pricing.

</details>

<details>
<summary><h3>OpenClaw</h3></summary>

Native TypeScript plugin for OpenClaw agent systems. Zero Python dependency, zero runtime dependencies, zero telemetry. Works with any model your gateway is configured against: Claude, GPT-5, Gemini, DeepSeek, local via Ollama.

```bash
# From GitHub (recommended)
openclaw plugins install github:alexgreensh/token-optimizer

# From ClawHub
openclaw plugins install token-optimizer
```

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

See [`openclaw/README.md`](openclaw/README.md) for full docs.

</details>

---

## Full Visibility: See Every Token, Every Dollar, Every Turn

Most tools tell you your context is full. Token Optimizer shows you exactly where every token went, how much each turn cost, which skills and MCP servers actually fired, and which ones are just sitting there eating your budget.

![Token Optimizer Dashboard](skills/token-optimizer/assets/dashboard-demo.gif)

One single-file HTML dashboard. Auto-regenerates after every session via the SessionEnd hook. Bookmark `http://localhost:24842/token-optimizer` and it's always current. Zero tokens from your context, zero network calls, zero setup after install.

### What the dashboard tracks

- **Per-turn token breakdown** for every API call: input, output, cache-read, cache-write, with spike detection highlighting context jumps
- **Cache analysis**: stacked bars showing input vs output vs cache-read vs cache-write split, with TTL mix (`1h` vs `5m`) and hit rate alongside
- **Pacing metrics** between calls so you can see whether a thread was steady or stop-start
- **Cost across 4 pricing tiers**: Anthropic API, Vertex Global, Vertex Regional, AWS Bedrock. Set your tier once and every session updates
- **Color-coded quality scores** overlaid on every session: green healthy, yellow degrading, red trouble
- **Subagent cost breakdown**: orchestrator vs worker spend, top offenders ranked by cost, flags when subagents consume over 30%
- **Top 5 costliest prompts** per session, pairing each user message with the cost of the response
- **Skill adoption trends**: which skills you actually invoke vs just having installed
- **Model mix over time**: Opus, Sonnet, Haiku breakdown across every session
- **CLAUDE.md and MEMORY.md health cards** on the Overview tab with line count, orphan count, and status at a glance
- **Drift detection**: config snapshots compared across time so you catch creep before it costs you
- **Savings tracker**: cumulative dollars saved from optimizations, checkpoint restores, and archives

`/context` shows a capacity bar. Proxy compressors print a terminal report. Token Optimizer shows the receipts, auto-updated, at zero context cost.

### Launch it

```bash
python3 measure.py setup-daemon           # Bookmarkable URL at http://localhost:24842/token-optimizer
python3 measure.py dashboard --serve      # One-time serve over HTTP
```

Throughout this README, whenever a feature mentions it's also visible on the dashboard, that means it lives inside this same HTML page. One place, everything tracked.

---

## What Makes This Different

### Two kinds of token waste, and most tools only fix one

**Runtime waste**: verbose command output that floods your context. Covers maybe 15-25% of what you're burning. This is what proxy compressors handle.

**Structural waste**: bloated CLAUDE.md, unused skills, duplicate system reminders, stale MEMORY.md, invisible entries past line 200, dead MCP servers. Covers the other 75-85%. Almost nobody touches this.

Token Optimizer handles both. And because it also checkpoints your session before compaction fires and restores what the summary dropped, the savings actually stick instead of vanishing the moment auto-compact kicks in.

### Fully local, zero dependencies, zero telemetry

Pure Python stdlib on Claude Code and Codex. Pure Node stdlib on OpenClaw. Nothing to `pip install`, nothing to `npm install` at runtime, no analytics endpoint, no phone-home. Every measurement is a local SQLite write to a file you own under your runtime home, such as `~/.claude/_backups/token-optimizer/trends.db` or `~/.codex/_backups/token-optimizer/trends.db`. You can inspect it, export it, or delete it.

### Zero context tokens consumed

Token Optimizer runs as an external process. It doesn't inject instructions into your context, it doesn't add MCP overhead, and it never eats into your window. Your full 1M budget stays fully yours.

### `/context` shows the dashboard light. Token Optimizer opens the hood.

`/context` tells you that your context is 73% full. Token Optimizer tells you which 12K are wasted on skills you never use, flags 47 orphaned MEMORY.md topic files Claude can't see, checkpoints your decisions before compaction destroys them, and gives you a quality score that tracks how much dumber your AI is getting as the session wears on.

---

## Real Savings

One real snapshot from 30 days of heavy Opus use: 942 sessions, 6.13B input tokens, 90% Opus, 82% cache hit rate.

<p align="center">
  <img src="skills/token-optimizer/assets/real-savings.svg" alt="Monthly savings breakdown across Token Optimizer features" width="800">
</p>

**$1,500 to $2,500 per month** for a heavy user at these volumes. Input savings alone come to around $590. The rest is output and thinking tokens saved by catching loops, landing `/compact` at the right moment, and avoiding rebuilds after bad compactions.

Lighter users see proportional savings. Structural audit wins (unused skills, duplicate configs, orphaned memory entries) are immediate regardless of volume, and they compound because a smaller prefix means a smaller cache-read bill on every single turn that follows.

---

## Trust & Safety FAQ

<details>
<summary>🎯 <strong>Can Token Optimizer degrade my context quality?</strong></summary>

No. Structural optimization only removes genuinely unused components (skills you never invoke, duplicate configs, orphaned memory entries). Active Compression features are independently toggleable, and the lossy ones (like Bash Compression) can be disabled with a single command or env var. The 7-signal quality score actively tracks degradation, so if anything ever hurt quality, the score would show it.
</details>

<details>
<summary>💾 <strong>Does it break the prompt cache?</strong></summary>

No, and this matters. The prompt cache depends on a stable prefix. Any tool that edits or removes blocks already in your conversation invalidates the cache and costs you **more**, not less.

Token Optimizer never touches content that's already in your context. It works on new content entering your window (compression), and on what happens before and after compaction (checkpoints and restore). Your cache prefix stays intact, which means Token Optimizer actually saves you money twice:

1. **Less input per turn.** Fewer structural tokens means a smaller context, so every message processes faster and cheaper.
2. **Cheaper cache reads on every turn forward.** A smaller stable prefix means a smaller cache-read bill on every subsequent message. This compounds across the session.

Be careful with tools that claim to "clean up" your context mid-session. If they modify or remove existing conversation blocks, they break your cache. The cost of re-sending a full prefix at uncached rates on the next 50 messages easily wipes out whatever they saved you.
</details>

<details>
<summary>🔒 <strong>Does it send any data anywhere?</strong></summary>

No network calls. No analytics. No opt-out telemetry because there's nothing to opt out of. Every event is a local SQLite row. You can `sqlite3` it, export it, delete it, or never look at it. It's yours.
</details>

<details>
<summary>🛟 <strong>Can it hurt my session?</strong></summary>

No. All hooks are non-blocking with fail-open design. If a Token Optimizer script ever errors, your command runs normally. Compression features are all individually toggleable. Checkpoints are additive. Quality scoring is read-only measurement.
</details>

<details>
<summary>📦 <strong>Does it have any runtime dependencies?</strong></summary>

No. Pure Python stdlib on Claude Code and Codex. Pure Node stdlib on OpenClaw. Nothing to `pip install`, nothing to `npm install` at runtime. What you clone is everything it needs.
</details>

<details>
<summary>🧰 <strong>Which platforms does it support?</strong></summary>

Claude Code and OpenClaw today, with native plugins for each. Codex support is in beta, with a Python adapter for chat-first status, coaching, dashboard refresh, and fleet scans.

Windsurf and Cursor are next on the roadmap. Full Codex parity is waiting on upstream hook/cache surfaces for invisible read substitution, structure-map substitution, and compact lifecycle hooks.
</details>

---

## Why install this first

Every Claude Code session starts with invisible overhead: system prompt, tool definitions, skills, MCP servers, CLAUDE.md, MEMORY.md. A typical power user burns 50-70K tokens before typing a word.

With Opus 4.6 and Sonnet 4.6 now at 1M context, that feels like breathing room. The problems still compound:

- **Quality degrades as context fills.** MRCR drops from 93% to 76% between 256K and 1M. Your AI gets measurably dumber as the window fills.
- **Rate limits hit faster.** Ghost tokens count toward your plan's usage caps on every message, cached or not. 50K overhead times 100 messages is 5M tokens burned on nothing.
- **Compaction is catastrophic.** 60-70% of your conversation gone per compaction. After 2-3 compactions, you've lost 88-95%. And each compaction means re-sending all that overhead again.
- **Higher effort means faster burn.** More thinking tokens per response means you hit compaction sooner, which means more total tokens across the session.
- **You can't fix what you can't see.** Without per-turn visibility into cache hits, model mix, and subagent spend, every "it feels slow" guess costs money. The dashboard shows exactly which turn was the expensive one.

Token Optimizer tracks all of this. Quality score, degradation bands, compaction loss, drift detection, per-turn cost across four pricing tiers, and skill-and-MCP attribution for every session. Zero context tokens consumed.

![What happens inside a 1M session](skills/token-optimizer/assets/user-profiles.svg)

> **"But doesn't removing tokens hurt the model?"** No. Token Optimizer only touches what's safe to touch. Structural optimization removes genuinely unused components (duplicate configs, unused skill frontmatter, orphaned memory entries), never the conversation itself. Active Compression works on new content entering your window (smart re-reads, credential-safe command summaries) and on the compaction boundary (checkpoints before auto-compact, restore after). Nothing already in your context gets edited or removed, which means your prompt cache stays intact. The 7-signal quality score tracks degradation in real time, and most users see scores improve after optimization because the model has more room for real work.

---

## Smart Compaction and Session Continuity

When auto-compact fires, 60-70% of your conversation vanishes. Decisions, error-fix sequences, agent state, all gone.

Smart Compaction catches all of it as checkpoints before compaction fires, then restores what the summary dropped. It also injects a digest of large tool outputs the model previously processed, so after compaction the model knows what it already saw without re-reading everything from scratch. Sessions pick up where you left off, even after a crash or /clear. Checkpoint history and compaction loss per session are also visible on the dashboard.

Compression savings only stick if your session survives the compaction. Saving tokens on `git status` doesn't help if the next auto-compact wipes out the decision that made you run `git status` in the first place. Smart Compaction closes that loop: checkpoint your decisions, restore them after compaction, and remind the model what outputs it already processed so it doesn't waste tokens re-reading them.

```bash
python3 measure.py setup-smart-compact    # checkpoint + restore hooks
```

### Progressive Checkpoints

Instead of waiting for emergency compaction, Token Optimizer captures session state at multiple thresholds: `20%`, `35%`, `50%`, `65%`, and `80%` context fill, plus quality drops below `80`, `70`, `50`, and `40`. It also snapshots before agent fan-out and after large edit batches. On restore, it picks the richest eligible checkpoint, not just the most recent one.

Background guards handle one-shot threshold capture, cooldown suppression, and deterministic extraction. No LLM calls in the checkpoint path.

### Tool Result Archive (model-aware, no manual lookups)

Large tool results (>4KB) get archived to disk automatically. In your conversation, the full result is replaced with a short preview plus an inline hint like `[Full result archived (12,400 chars). Use 'expand abc123' to retrieve.]`

That hint is visible to Claude, not just you. So after a compaction (when the original tool result has been summarized away), if the model needs the full output again to answer your next question, it invokes `expand abc123` itself and the archived content comes back through the CLI. No command re-run, no lost output, no context cost in the meantime.

You can run `expand` yourself too when you want to see a specific archived result, but the primary flow is automatic: the model sees the hint, the model asks for the bytes, the bytes come back.

```bash
python3 measure.py expand --list                 # List all archived tool results
python3 measure.py expand <tool-use-id>          # Retrieve a specific archived result manually
```

### Session Continuity

Sessions auto-checkpoint on end, /clear, and crashes. On a fresh session, Token Optimizer drops a short in-context pointer to the most recent relevant checkpoint, so Claude can pull the right prior state on its own if the new conversation needs it. No auto-replay of stale context, no user action required, just a breadcrumb the model can follow when it matters.

Enable optional local-only checkpoint telemetry to see whether checkpoints are firing and which triggers are active:

```bash
TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 python3 measure.py checkpoint-stats --days 7
```

---

## Quality Scoring

Seven signals, weighted to reflect real-world impact:

| Signal | Weight | What It Means For You |
|--------|--------|----------------|
| **Context fill** | 20% | How close are you to the degradation cliff? Based on published MRCR benchmarks. |
| **Stale reads** | 20% | Files you read earlier have changed. Your AI is working with outdated info. |
| **Bloated results** | 20% | Tool outputs that were never used. Wasting context on noise. |
| **Compaction depth** | 15% | Each compaction loses 60-70% of your conversation. After 2, 88% is gone. |
| **Duplicates** | 10% | The same system reminders injected over and over. Pure waste. |
| **Decision density** | 8% | Are you having a real conversation, or is it mostly overhead? |
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

Real session. 708 messages, 2 compactions, 88% of the original context gone. Without the quality score, you'd have no idea.

![Real session quality breakdown](skills/token-optimizer/assets/quality-example.svg)

---

## Active Compression (v5)

Token Optimizer no longer just measures context bloat. It actively reduces it. Seven features target specific waste patterns, each with honest risk assessment and dashboard toggles.

![v5 Active Compression overview](skills/token-optimizer/assets/v5-hero.svg)

**On by default**: Quality Nudges, Loop Detection, Delta Mode, Structure Map, Bash Compression (16 handlers), Activity Mode Detection, Decision Extraction.

All features are independently toggleable from the Manage tab in the dashboard, via CLI (`measure.py v5 enable|disable <feature>`), or with environment variables.

| Feature | Default | Potential Savings | Risk |
|---|---|---|---|
| Quality Nudges | ON | Measured per-compact (fill% recovery) | None |
| Loop Detection | ON | Measured per-loop (actual turn content) | None |
| Delta Mode | ON | ~20% (smart re-reads) | Low |
| Structure Map | ON (soft-block) | ~30% (large file re-reads, up to 99% per file) | Low |
| Bash Compression | ON | ~10% (CLI output) | Low |
| Activity Mode | ON | Adapts compaction to session phase | None |
| Decision Extraction | ON | Preserves decisions across compactions | None |

> **Privacy note**: Every feature runs 100% on your machine. Nothing is ever sent anywhere. No analytics endpoint, no phone-home, no cloud sync. "Measurement" and "beta telemetry" always mean local-only SQLite writes to a file you own, and you can inspect, export, or delete that file at any time. Token Optimizer has zero network calls by design.

![Quality Nudges and Loop Detection in action](skills/token-optimizer/assets/v5-nudges-loops.svg)

### Quality Nudges (ON by default, fully automatic)

Watches your context quality in real time. When the score drops 15+ points or crosses below 60, an inline system note enters the context that reads something like `[Token Optimizer] Quality dropped to 58. Consider /compact to protect context.`

Claude sees that note on the next turn and surfaces the warning to you naturally, or adjusts behavior on its own. You don't have to watch a dashboard or remember thresholds. The nudge shows up right where decisions get made, with zero setup after install.

**Value**: catches context rot early so /compact lands at the right moment, before you lose decisions to compaction.

**How it works**: runs inside the existing quality-cache hook on every UserPromptSubmit. Cooldown of 5 minutes between nudges, max 3 per session. Suppressed on the first check after a compaction, so you don't get warned about quality you just fixed.

**Risk**: none. Only adds a short note to context, never removes anything.

### Loop Detection (ON by default, fully automatic)

Catches the AI getting stuck on a retry loop before it burns through tokens. When similarity crosses the threshold, a short inline note lands in the context flagging the loop so the model breaks out of it, with no user action needed. Savings are measured from the actual content of the looping turns, not estimated.

**Value**: post-hoc detectors found that loop sessions average 47K wasted tokens. Real-time detection prevents this. Every caught loop logs the measured token cost of the loop turns to your local telemetry.

**How it works**: compares the last 4 user messages and last 5 tool results for similarity. Fires at confidence ≥0.7 with a session cap of 2 notes. Uses fixed message templates and never echoes user content back.

**Risk**: none. Only adds a short note.

![Delta Mode: smart re-reads](skills/token-optimizer/assets/v5-delta-mode.svg)

### Delta Mode (ON by default, your biggest single win)

When the AI re-reads a file after editing it, the Read call returns only what changed instead of the whole file. Fully automatic, no configuration, no user action. 65%+ of Read calls in real sessions are re-reads, which makes this the highest-impact v5 feature.

**Value**: typical sessions re-read the same file 2-5 times. Delta mode sends only the diff. A 2,000-token file re-read becomes a 50-token diff, for 97% savings on that specific read.

**How it works**: stores file content (up to 50KB per file) in a local cache on first read. On re-read with changed mtime, computes a unified diff via Python's `difflib` (stdlib, no git dependency). Falls back to full re-read if the diff exceeds 1,500 chars or either file exceeds 2,000 lines. Scoped to explicit full-file reads so narrow `offset`/`limit` requests are never served a whole-file diff. `.env` and credential files are excluded from caching.

**Risk**: low. If the AI needed the full file to understand the change in context, the diff alone might not be enough. Fails open on large changes and big files. Set `TOKEN_OPTIMIZER_READ_CACHE_DELTA=0` to disable.

### Structure Map (ON in soft-block mode, your biggest win on large files)

When Claude re-reads a code file it already saw this session, the Read call is blocked and replaced with a compact structural summary: function signatures, class hierarchies, imports, and module docstrings. A 720KB Python file (180,000 tokens) becomes a 250-token skeleton. Works on Python files up to 800KB/20K lines and JS/TS files up to 400KB/5K lines.

**Value**: code-heavy sessions re-read the same large files 3-17 times. Structure Map compresses every re-read after the first by 95-99%. On a 180K-token file re-read 5 times, that's ~900K tokens saved in a single session.

**How it works**: on first read, caches the file content and generates an AST-based summary (Python) or regex-based summary (JS/TS). On subsequent reads of the same unchanged file, returns the summary via `additionalContext` and blocks the full re-read. Falls back to full read on files below 1,000 tokens, generated/minified files, partial-range reads, or if the AST parse fails.

**Measurement**: enable `measure.py v5 enable structure_map_beta` or `TOKEN_OPTIMIZER_STRUCTURE_MAP=beta` to log compression events to your local SQLite for `compression-stats`. Nothing sent anywhere.

**Risk**: low. The model works from the summary instead of full source. For files where implementation details matter (not just structure), the model can request a full read. Disable with `TOKEN_OPTIMIZER_READ_CACHE_MODE=shadow`.

![Bash Output Compression: git status and pytest before/after](skills/token-optimizer/assets/v5-bash-compression.svg)

### Bash Output Compression (ON by default, lossy)

Rewrites common CLI commands to return compressed summaries instead of verbose output. v5.1.0 ships seven new handlers covering the command families that eat the most context: lint (rule-code grouping for eslint, ruff, flake8, shellcheck, rubocop, golangci-lint), log tails (adjacent-duplicate collapse), tree (depth-2 truncation), docker build and pull (progress filtering), long listings (pip list, npm ls, docker ps, with top-N plus tail marker), JS/TS/Go build output (error-and-summary view), and test runner routing (cypress, playwright, mocha, karma all route through the unified pytest compressor).

Together with the existing git and pytest handlers, that's full coverage for ~90% of the verbose CLI output real sessions produce.

**Value**: strips hundreds of lines of test/build/git output down to just the essentials. A 564-token pytest output becomes 115 tokens. A 60-file `ls -la` truncates to 50. Best for sessions with lots of CLI commands.

**How it works**: a PreToolUse hook (`bash_hook.py`) intercepts safe read-only commands, tokenizes them with `shlex.split()`, checks against a whitelist, and rewrites them via `updatedInput` to route through a compression wrapper (`bash_compress.py`). Categorically excludes compound commands (anything with `;`, `&&`, `||`, `|`, `$()`, backticks, `>`, `>>`), sudo, and interactive flags.

**Security**: `shell=True` is never used. Credentials (AWS keys, GitHub PATs, Slack tokens, Stripe keys, OpenAI keys, HTTP basic-auth URLs) are scanned pre-compression and preserved verbatim. Multilingual error lines survive the preservation path. Partial output on timeout is returned raw, never compressed.

**How to disable**: `measure.py v5 disable bash_compress` or `TOKEN_OPTIMIZER_BASH_COMPRESS=0`

**Risk**: low. Compression is lossy by design. For routine checks this is fine. For careful diff review or debugging specific test failures, disable temporarily with the command above.

### Activity Mode Detection (ON by default, v5.6)

Classifies your session into one of five modes (code, debug, review, infra, general) using a sliding window of the last 10 tool calls. The mode label feeds into compaction guidance so PRESERVE/DROP priorities adapt to what you're actually doing: debug mode preserves error signals and stack traces, code mode preserves edited files and their tests, review mode keeps findings and decisions while dropping full file contents.

**How it works**: the PostToolUse hook classifies each tool call into a bucket (edit, read, bash_infra, bash_git, web, etc.) and stores it in the per-session SQLite. Mode classification runs on every tool call with zero latency impact (single INSERT + bounded SELECT). The activity log auto-prunes at 30 rows.

**Risk**: none. Mode detection is read-only context, never modifies or blocks anything.

### Decision Extraction (ON by default, v5.6)

Detects decision statements ("chose X because Y", "going with Z over W", "switched to") in real-time from tool outputs and stores them incrementally in the session database. At compaction time, these decisions are injected as CRITICAL DECISIONS that the compaction summary must preserve verbatim. Combined with the new anchored compact state (which persists intent, changes, decisions, and errors across compaction cycles), this prevents the decision drift that makes post-compaction sessions lose context.

**How it works**: regex-based extraction on the PostToolUse path (runs only on outputs >500 chars). Uses atomic read-modify-write (SQLite BEGIN IMMEDIATE) to prevent lost updates under concurrent hooks. Capped at 10 decisions per session.

**Risk**: none. Only adds structured data to the compaction guidance, never removes anything.

### Managing v5 features

Three ways to control these features:

```bash
# CLI
python3 measure.py v5 status                    # show all features with current state
python3 measure.py v5 enable delta_mode         # turn a feature on
python3 measure.py v5 disable bash_compress     # turn a feature off
python3 measure.py v5 info delta_mode           # show full details for one feature
python3 measure.py v5 welcome                   # show the first-run welcome screen
python3 measure.py compression-stats            # see actual measured savings from local telemetry
```

```bash
# Environment variables (override config.json, for CI/scripts)
TOKEN_OPTIMIZER_QUALITY_NUDGES=0        # kill switch for nudges
TOKEN_OPTIMIZER_LOOP_DETECTION=0        # kill switch for loop detection
TOKEN_OPTIMIZER_READ_CACHE_DELTA=1      # enable delta mode
TOKEN_OPTIMIZER_BASH_COMPRESS=0         # disable bash compression
TOKEN_OPTIMIZER_STRUCTURE_MAP=beta      # enable beta telemetry
```

**Dashboard**: Open `token-dashboard` and the Manage tab. Active Compression (v5) is the first section. Toggles apply instantly to new tool calls, no Claude Code restart needed. Each feature shows what it does, its value, how it works, its risk level, and its impact estimate.

**First-run welcome**: on your first session after installing v5, you'll see a one-time welcome screen explaining each feature, its default state, and how to toggle it. Stored in `config.json` so it only shows once.

### Measuring real savings (all local)

All v5 features log to a `compression_events` SQLite table stored locally on your machine at `~/.claude/_backups/token-optimizer/trends.db`. Nothing leaves your system.

```bash
python3 measure.py compression-stats --days 30
```

Output shows total events per feature, tokens saved, compression ratio, and quality preservation rate. The `verified` flag distinguishes exact measurements (delta mode knows the precise before/after) from estimates (structure map is heuristic).

---

## Live Quality Bar

A glance at your terminal tells you if you're in trouble. Colors shift from green to red as quality degrades. When quality drops below 75, session duration appears as a warning. Running subagents show with their model and elapsed time so you can spot misrouted models.

![Status Bar Degradation](skills/token-optimizer/assets/status-bar.svg)

```bash
python3 measure.py setup-quality-bar      # one-time install
```

**My quality bar disappeared, how do I get it back?** Running Claude Code's built-in `/statusline` rewrites the `statusLine` key in `~/.claude/settings.json` and silently overwrites Token Optimizer's entry. SessionStart detects this and **auto-restores** the quality bar. Just start a new session and it's back. You'll see a one-line notice explaining what happened.

**I really don't want the quality bar anymore, how do I turn it off for good?**

```bash
python3 measure.py setup-quality-bar --uninstall
```

This removes the components and writes `quality_bar_disabled: true` to `~/.claude/token-optimizer/config.json`. The opt-out is sticky across sessions. SessionStart will not auto-restore it. You can also just tell Claude Code in natural language: _"remove the Token Optimizer statusline"_, and Claude will run the uninstall command for you.

**I changed my mind, bring it back.** Run `python3 measure.py setup-quality-bar`. Explicit install clears the opt-out flag automatically.

**I want to keep my own custom statusline and also see the quality score.** The custom-statusline path is still respected when you run `setup-quality-bar` directly. You'll get integration instructions for reading `~/.claude/token-optimizer/quality-cache.json` from your own script instead.

---

## Coach Mode and Fleet Auditor

Token Optimizer is not just reactive. It's also proactive.

### Coach Mode

```
> /token-coach
```

Tell it your goal. Get back specific, prioritized fixes with exact token savings. Detects 8 named anti-patterns (The Kitchen Sink, The Hoarder, The Monolith, and more) and recommends multi-agent design patterns that actually save context.

**Building a new project?** Run `/token-coach` before writing your first `CLAUDE.md` or Codex `AGENTS.md`. Start with a clean, optimized setup instead of accumulating waste for months and fixing it later.

### Waste Detectors

11 automated detectors analyze your session patterns and surface actionable findings:

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
| Output waste | Verbose responses to simple operations, repeated explanations |
| Cache instability | CLAUDE.md patterns that break Anthropic's prompt cache prefix |

### Fleet Auditor

Managing multiple agent systems? Fleet Auditor scans across Claude Code, Codex, OpenClaw, and custom setups to find idle burns, model misrouting, and config bloat with dollar savings per finding. One command, one report, every ecosystem.

### Subagent Cost Breakdown

See exactly how much your subagents cost: total spend, % of combined budget, and top offenders ranked by cost. Flags when subagents consume >30% of total. Also visible per session on the dashboard, with orchestrator-vs-worker split.

### Costly Prompt Ranking

See which prompts cost the most: pairs each user message with the cost of the response, ranks top 5. Shows what you asked, not just totals.

### CLAUDE.md Routing Injection

Generate model routing instructions from your actual usage data and inject them into CLAUDE.md. Claude reads these every session and routes accordingly. A 48-hour staleness guard auto-removes stale advice.

```bash
python3 measure.py inject-routing --dry-run   # Preview what would be injected
python3 measure.py inject-routing              # Inject (with approval)
```

---

## Dashboard: Post-Audit Walkthrough

The Full Visibility dashboard up top auto-tracks every session. After you run `/token-optimizer` and the 6-agent audit finishes, the same dashboard opens on an audit-focused view where every component is clickable. Expand any item to see why it matters, the trade-offs, and what would change. Toggle the fixes you want, copy a ready-to-paste optimization prompt, and apply with approval.

Hover help on every column explains `Cache`, `TTL`, `Pacing`, `Cache R`, and `Cache W` without jargon. Session drill-downs key off stable session identity for consistent expansion across refreshes.

---

## What questions can you ask?

| Command | What You Get |
|---------|-------------|
| `quick` | **"Am I in trouble?"** 10-second answer: context health, degradation risk, biggest token offenders, which model to use. |
| `doctor` | **"Is everything installed correctly?"** Score out of 10. Broken hooks, missing components, exact fix commands. |
| `drift` | **"Has my setup grown?"** Side-by-side comparison vs your last snapshot. Catches config creep before it costs you. |
| `quality` | **"How healthy is this session?"** 7-signal analysis of your live conversation. Stale reads, wasted tokens, compaction damage. |
| `report` | **"Where are my tokens going?"** Full per-component breakdown. Every skill, every MCP server, every config file. |
| `conversation` | **"What happened each turn?"** Per-message token and cost breakdown with spike detection. |
| `pricing-tier` | **"What am I paying?"** View or switch between Anthropic, Vertex, and Bedrock pricing tiers. |
| `kill-stale` | **"Clean up zombies."** Terminate headless sessions running 12+ hours. |
| `git-context` | **"What files matter right now?"** Test companions, co-changed files, import chains for your current git diff. |
| `trends` | **"What's actually being used?"** Skill adoption, model mix, overhead trajectory over time. |
| `coach` | **"Where do I start?"** Health score with earned vs neutral signals. Detects anti-patterns. |
| `memory-review` | **"Is my MEMORY.md broken?"** Structural audit: orphaned files, broken links, invisible entries past line 200, duplicate rules. |
| `dashboard` | **"Show me everything."** Interactive HTML dashboard with all analytics and health cards. |
| `savings` | **"How much have I saved?"** Cumulative dollar savings from optimizations, checkpoint restores, and archives. |
| `attention-score` | **"Is my CLAUDE.md well-structured?"** Scores sections against the attention curve, flags critical rules in low-attention zones. |
| `jsonl-inspect` | **"What's in this session?"** Record counts, token distribution, top 10 largest records, compaction markers. |
| `expand` | **"Get that result back."** Retrieves a tool result the model archived automatically. Usually the model calls this itself when it needs the full output again, but you can also run it manually. |
| `/token-optimizer` | **"Fix it for me."** Interactive audit with 6 parallel agents. Guided fixes with diffs and backups. |

---

## How It Compares

| Capability | Token Optimizer | `/context` | context-mode | Proxy compressors |
|---|---|---|---|---|
| Structural waste audit | Deep, per-component | Summary only | No | No |
| Quality degradation tracking | 7-signal score with grades | Capacity % only | No | No |
| Compaction survival | Progressive checkpoints, restore, plus tool output digest | No | Session guide only | No |
| Runtime output compression | 16 CLI handlers, credential-safe, individually toggleable | No | Yes | Yes, always-on (cannot disable) |
| Measures if compression actually helped | Yes, local telemetry with before/after tokens | No | No | No |
| Read deduplication and smart diff on re-reads | Yes | No | No | No |
| Behavioral coaching and model routing | 11 detectors, cost-ranked subagent breakdown | Basic suggestions | No | No |
| CLAUDE.md and MEMORY.md structural health | 8 auditors plus attention-curve scoring | No | No | No |
| Fleet-level waste detection across agents | Yes | No | No | No |
| Zero context tokens consumed | Yes, external process | Adds ~200 tokens | MCP overhead | Injects instructions into context |
| Zero runtime dependencies | Yes, pure stdlib | N/A | Varies | External binary |
| Zero telemetry | Yes | Yes | Varies | Opt-out telemetry |
| Works across platforms | Claude Code, Codex beta, and OpenClaw (Windsurf and Cursor coming) | Claude Code only | Several platforms | Several platforms |

A few notes on the compression column: proxy tools quote big compression ratios on the commands they handle best, like `git status` or `tree`. Those numbers are real for those specific commands, but they cover only 15-25% of what you're actually burning. Everything else (configs, skills, memory, compaction loss) stays untouched. And most proxy compressors inject their own instructions into your context, which costs tokens on the way in.

Token Optimizer handles the same runtime output with 30+ command families (git, pytest, lint, logs, tree, docker progress, package listings, JS/TS/Go builds, cypress/playwright/mocha/karma test runners), plus the other 75-85% that proxies don't touch, plus measurement so you can see whether any of it actually helped on your sessions.

### A word on cache safety

Some tools claim to reduce tokens by modifying or removing blocks already in your conversation. That breaks the prompt cache. When the stable prefix changes, every subsequent turn re-sends the full prefix at uncached input rates instead of the heavily discounted cache-read rate. The "savings" from removing a few thousand tokens easily get wiped out by the cache invalidation cost on the next 50 messages.

Token Optimizer never modifies content already in your context. Structural optimization runs between sessions. Active Compression works on new content entering your window, or on the compaction boundary. Your cache prefix stays intact.

---

## Memory Health: Your MEMORY.md Is Probably Broken

Claude auto-loads the first 200 lines of MEMORY.md every session. Everything after line 200 is silently truncated. The tokens still count against your window, but Claude never sees the content. Most power users don't know this is happening.

`memory-review` scans your MEMORY.md structurally and tells you what's wrong:

- **Orphaned topic files**: files in your memory directory that nothing links to
- **Broken links**: index entries pointing to files that don't exist
- **Invisible entries**: content below line 200 that Claude can't see
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

## Read-Cache and Context Tools

### PreToolUse Read-Cache (automatic deduplication)

Detects redundant file reads automatically. On the first re-read of an unchanged file, returns a structural code summary (function signatures, class hierarchy, imports) instead of the full source. A 180,000-token file re-read becomes a 250-token skeleton. Works on Python files up to 800KB and JS/TS files up to 400KB. Default ON in soft-block mode. Saves 8-30% tokens from read deduplication across a typical session, with 95%+ compression on large code files.

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

Analyzes your working tree to suggest files that should be in context: test companions, frequently co-changed files from the last 50 commits, and import chains for Python/JS/TS.

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

Tracks cumulative dollar savings from setup optimization, checkpoint restores, and tool archiving. Also surfaced on the dashboard's savings tile so you can watch the number climb over weeks.

```bash
python3 measure.py savings                      # Dollar savings report (last 30 days)
```

---

## Usage Analytics

**Trends**: Which skills do you actually invoke vs just having installed? Which models are you using? How has your overhead changed over time?

**Session Health**: Catches stale sessions (24h+), zombie sessions (48h+), and outdated configurations before they cause problems.

```bash
python3 measure.py setup-hook       # Enable session tracking (one-time)
python3 measure.py trends           # Usage patterns over time
python3 measure.py health           # Session hygiene check
python3 measure.py plugin-cleanup   # Detect duplicate skills and archive local/plugin overlaps
```

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

> **Note on `--bare` mode**: Running Claude Code with the `--bare` flag (for scripted/CI usage) skips all hooks and plugin sync. Token Optimizer's Smart Compaction, quality tracking, and session data collection require hooks and won't activate in `--bare` mode. This is expected. `--bare` is designed for lightweight scripted calls.

---

## Other Platforms

### OpenClaw

Native TypeScript plugin with session audits, 10 waste detectors, coach mode, Smart Compaction, and interactive dashboard adapted for OpenClaw's architecture. Works with any model (Claude, GPT-5, Gemini, DeepSeek, local via Ollama). Install instructions in the [Install section above](#openclaw). Full docs: [`openclaw/README.md`](openclaw/README.md).

### Codex (Beta)

Python adapter for OpenAI Codex (CLI and Desktop). Same core engine, adapted for AGENTS.md, GPT-5.x models, intelligence levels, and Codex's hook surface. Install instructions in the [Install section above](#codex-beta). Full docs with feature parity table, hook profiles, and model pricing: [`docs/codex-beta.md`](docs/codex-beta.md).

---

## License

**PolyForm Noncommercial 1.0.0**. Source-available. Personal, research, educational, and non-commercial use requires no license purchase.

_This FAQ is informational guidance, not a modification of the license terms. Last updated: April 2026._

### 🧑‍💻 Personal / hobby / research / education?
Go for it. Full source, runs locally, no license purchase needed. That's the whole point.

### 🏢 Small team (under 5 people OR under $20k/month revenue)?
Small teams get a no-cost commercial license automatically. Just use it.
If you want to [sponsor the project](https://github.com/sponsors/alexgreensh) or buy me a coffee, not required, but always appreciated ☕

### 🔄 Started personal, now it's turning into a business?
Your past use is totally fine. The license has a built-in 32-day grace period after any written notice, so there's plenty of runway.
When you're ready, just reach out for a commercial license. Terms are reasonable and size-appropriate.

### 🏗️ Larger company / commercial use?
Let's talk. Contact [Alex Greenshpun](https://linkedin.com/in/alexgreensh) or me@alexgreenshpun.com.

---

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
