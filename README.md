<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center">
  <a href="https://github.com/alexgreensh/token-optimizer/releases/latest"><img src="https://img.shields.io/github/v/release/alexgreensh/token-optimizer?label=version&color=green" alt="Latest stable version"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/releases"><img src="https://img.shields.io/github/release-date/alexgreensh/token-optimizer?label=last%20release&color=blue" alt="Last Release"></a>
  <a href="https://github.com/alexgreensh/token-optimizer"><img src="https://img.shields.io/badge/Claude_Code-Plugin-blueviolet" alt="Claude Code Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/openclaw"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Falexgreensh%2Ftoken-optimizer%2Fmain%2Fopenclaw%2Fpackage.json&query=%24.version&prefix=v&label=OpenClaw&color=brightgreen" alt="OpenClaw version"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/opencode"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Falexgreensh%2Ftoken-optimizer%2Fmain%2Fopencode%2Fpackage.json&query=%24.version&prefix=v&label=OpenCode&color=58a6ff" alt="OpenCode version"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/docs/codex.md"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Falexgreensh%2Ftoken-optimizer%2Fmain%2F.codex-plugin%2Fplugin.json&query=%24.version&prefix=v&label=Codex&color=orange" alt="Codex version"></a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/cuts%20context%20waste-3fb950" alt="Cuts context waste">
  <img src="https://img.shields.io/badge/survives%20compaction-checkpoint%20%2B%20restore-58a6ff" alt="Survives compaction">
  <img src="https://img.shields.io/badge/saves%20real%20%24-every%20session-2ea043" alt="Saves real dollars every session">
  <img src="https://img.shields.io/badge/live%20dashboard-tokens%20%2B%20%24%20%2B%20turns-8B5CF6?logo=chartdotjs&logoColor=white" alt="Live dashboard">
  <img src="https://img.shields.io/badge/context%20quality-live%20score-blue" alt="Live context quality score">
  <img src="https://img.shields.io/badge/tests-passing-brightgreen" alt="Tests passing">
</p>
<p align="center">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/telemetry-none-brightgreen" alt="Zero Telemetry">
  <img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg" alt="License: PolyForm Noncommercial"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/stargazers"><img src="https://img.shields.io/github/stars/alexgreensh/token-optimizer" alt="GitHub Stars"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/commits/main"><img src="https://img.shields.io/github/commit-activity/m/alexgreensh/token-optimizer" alt="Commit Activity"></a>
  <a href="https://linkedin.com/in/alexgreensh"><img src="https://img.shields.io/badge/LinkedIn-Connect-0A66C2?logo=linkedin&logoColor=white" alt="Connect on LinkedIn"></a>
</p>
<p align="center">
  <a href="https://github.com/sponsors/alexgreensh"><img src="https://img.shields.io/badge/%E2%99%A5%20Support%20this%20project%20to%20keep%20it%20open%20source-ea4aaa?style=for-the-badge&logo=githubsponsors&logoColor=white" alt="Support this project to keep it open source"></a>
</p>

<h2 align="center">Cut the tokens you waste. Keep the work you'd lose.</h2>

<p align="center"><em>It runs automatically in the background. You keep working as usual. Run the audit when you want the full picture.</em></p>

<p align="center">
  <a href="https://alexgreensh.github.io/token-optimizer/"><img src="https://img.shields.io/badge/%F0%9F%93%96%20Read%20the%20Docs-alexgreensh.github.io%2Ftoken--optimizer-e85329?style=for-the-badge&logoColor=white" alt="Read the documentation"></a>
  <a href="https://alexgreensh.github.io/token-optimizer/start/quickstart/"><img src="https://img.shields.io/badge/Quickstart-2%20minutes-3fb950?style=for-the-badge" alt="Quickstart in 2 minutes"></a>
</p>

## The 30-Second Version

Token Optimizer cuts the tokens your AI coding assistant wastes, keeps your work alive across sessions and compactions, and shows you where every dollar went on a live dashboard. **Most of it runs automatically. You install it, run the audit once, and the hooks do the rest.**

**Why not just use Headroom or RTK?** They compress command output, which covers 15-25% of your context. Token Optimizer covers that plus the other 75%: bloated configs, unused skills, stale memory, compaction loss, model misrouting, behavioral waste. Every saving is cache-safe and measured. The dashboard updates after every session, automatically.

Works on **Claude Code** (CLI and VS Code), **OpenCode**, **OpenClaw**, **Codex**, **Hermes**, and **GitHub Copilot** (beta). Windsurf and Cursor are next on the roadmap.

<p align="center">
  <img src="skills/token-optimizer/assets/hero-terminal.svg" alt="Token Optimizer Quick Scan" width="800">
</p>

## Install

**Claude Code (recommended):**

```
/plugin marketplace add alexgreensh/token-optimizer
/plugin install token-optimizer@alexgreensh-token-optimizer
```

Then in Claude Code: `/token-optimizer`

> **Enable auto-update after installing.** Claude Code ships third-party marketplaces with auto-update off by default. `/plugin` → **Marketplaces** tab → select `alexgreensh-token-optimizer` → **Enable auto-update**. One-time, 10 seconds.
>
> After install, run `/token-optimizer` once to set up hooks. From there, everything runs automatically: compression, checkpoints, quality scoring, dashboard updates. You don't need to run any command again unless you want an audit.

<details>
<summary><b>Other platforms and install methods</b></summary>

**Codex:**
```bash
codex plugin marketplace add alexgreensh/token-optimizer
```
Then in the Codex TUI: `/plugins` and install Token Optimizer. See [`docs/codex.md`](docs/codex.md).

**OpenCode:** add `token-optimizer-opencode` to the `plugin` array in your `opencode.json`:
```jsonc
{ "$schema": "https://opencode.ai/config.json", "plugin": ["token-optimizer-opencode"] }
```
See [`opencode/README.md`](opencode/README.md).

**OpenClaw:**
```bash
openclaw plugins install github:alexgreensh/token-optimizer
```
See [`openclaw/README.md`](openclaw/README.md).

**Hermes:**
```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --hermes
```
See [`hermes/README.md`](hermes/README.md).

**GitHub Copilot (beta):**
```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --copilot
```
See [`docs/copilot.md`](docs/copilot.md).

**macOS/Linux script install (alternative to plugin):**
```bash
tmp="$(mktemp -d)"
release_json="$(curl -fsSL https://api.github.com/repos/alexgreensh/token-optimizer/releases/latest)"
tag="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])' <<<"$release_json")"
git clone --branch "$tag" --depth 1 https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
bash ~/.claude/token-optimizer/install.sh
rm -rf "$tmp"
```

**Windows users:** Use the plugin install only. Do not run `install.sh` on Windows. If you hit `EBUSY` errors, close all Claude Code and Git Bash windows, kill lingering `git.exe` processes, delete `C:\Users\<you>\.claude\token-optimizer` and `C:\Users\<you>\.claude\plugins\marketplaces\alexgreensh-token-optimizer`, then retry.

**If `install.sh` fails with `$'\r': command not found`** (a clone made before LF line endings were enforced converted the script to CRLF), strip the carriage returns once and re-run — the repo now ships a `.gitattributes` that prevents this on fresh clones:
```bash
sed -i 's/\r$//' ~/.claude/token-optimizer/install.sh
# already have the repo? re-normalize line endings in place:
git -C ~/.claude/token-optimizer add --renormalize . && git -C ~/.claude/token-optimizer checkout -- .
```

</details>

<details>
<summary>Uninstall</summary>

Token Optimizer is additive and reversible. Every runtime has a clean uninstall
that removes only what we installed, leaving your own hooks, config, and session
data intact. Full per-runtime steps live in **[docs/uninstall.md](docs/uninstall.md)**.

Quickest path (Claude Code plugin install):

```
/plugin uninstall token-optimizer@alexgreensh-token-optimizer
```

</details>

## What You Get

**Runs automatically, every session, you do nothing:**

- 🔄 **Smart Compaction**: checkpoints before auto-compact, restores after
- 🗄️ **Session Continuity**: cross-session hints, cold-resume, checkpoint scoring
- 📦 **Active Compression**: 9 features, all on by default (delta diffs, skeletons, bash/search compression, lean-output nudges, quality nudges, loop detection, activity mode, decision extraction)
- 📊 **Quality Scoring**: 7 signals, real-time, letter grades S–F
- 🗃️ **Session Database**: SQLite, 15 tables, full audit trail, zero network
- 🔍 **Progressive Disclosure**: large outputs archived, expand on demand
- 🧠 **Context Intel Digest**: post-compaction re-orientation without re-reads
- 🔀 **Model Routing Nudges**: steers to the right tier for the task

**When you ask for it:**

- 🩺 `/token-optimizer`: full audit with guided fixes
- 📈 `/token-coach`: 30-day trend analysis with specific fixes
- ⚡ `quick`: 10-second health check
- 🔧 `doctor`: installation check
- 💰 `savings`: dollar savings report
- 📋 `report`: per-component token breakdown
- 🌐 `dashboard`: open the full dashboard
- 📝 `memory-review`: MEMORY.md structural audit
- 📂 `expand`: retrieve archived tool result
- 🔙 `resume-lean`: reopen a cold session

Install, run `/token-optimizer` once, everything else runs automatically.

## How It's Different

Most token tools compress command output. That covers 15-25% of your context. The other 75% goes untouched.

**Compression coverage.** Headroom and RTK compress bash and command output. Token Optimizer compresses eight surfaces of the output stack. Status: 🟢 supported, 🟡 partial, 🔴 not supported.

| Compression surface | Token Optimizer | Headroom | RTK |
|---|---|---|---|
| **Bash / command output** (git, tests, lint, build, logs) | 🟢 60+ patterns, credential-safe; 564 → 115 tokens on a pytest run | 🟢 6 algorithms | 🟢 100+ filters |
| **Search / grep output** | 🟢 Top hits plus a count; 500 lines → 20 | 🔴 | 🔴 |
| **Tabular / JSON output** (jq, yq, csvtool, mlr) | 🟢 Value-preserving columnar | 🟢 SmartCrusher | 🔴 |
| **File re-reads, delta mode** | 🟢 Diff only; 2,000-token re-read → ~50 | 🔴 | 🔴 |
| **File re-reads, structure map** | 🟢 Skeleton of signatures and imports; 720KB → 250 tokens | 🔴 | 🔴 |
| **Large tool results** (over 4K chars) | 🟢 Archived to disk, expandable on demand | 🔴 | 🔴 |
| **Model output verbosity** | 🟢 10-15% typical, up to 30-41% measured, cache-safe | 🔴 | 🔴 |
| **Structural context** (configs, skills, MCP, memory) | 🟢 Per-component audit, each source scored | 🔴 | 🔴 |

RTK reaches the first surface. Headroom reaches the first and the third. Token Optimizer covers all eight, then keeps going into what happens around compression:

- **Three kinds of waste, not one.** Structural (bloated configs, unused skills, stale memory), runtime (verbose output, re-reads), and behavioral (model misrouting, cache expiry, retry loops). [How each works →](https://alexgreensh.github.io/token-optimizer/features/active-compression/)
- **Savings survive compaction.** Checkpoints before auto-compact, restores after. Without this, compression savings vanish the moment compaction fires.
- **Measures whether it helped.** Before/after token deltas, dollar savings across four pricing tiers, quality scores that track degradation. Not just "we compressed stuff."
- **Zero baseline overhead.** External process, no always-on instructions in your context, no MCP server, no dependencies, no telemetry.

<p align="center">
  <img src="skills/token-optimizer/assets/automated-flow.svg" alt="How Token Optimizer works automatically every session" width="900">
</p>

|  | Token Optimizer | Headroom | RTK | context-mode | `/context` |
|---|---|---|---|---|---|
| **Compaction survival** | 🟢 Progressive checkpoints, restore, tool output digest | 🔴 | 🔴 | 🟡 Session guide only | 🔴 |
| **Session continuity** | 🟢 Cross-session hints, cold-resume, checkpoint scoring | 🔴 | 🔴 | 🟡 Session guide | 🔴 |
| **Model routing and behavioral coaching** | 🟢 11 detectors, subagent cost breakdown, anti-patterns | 🔴 | 🔴 | 🔴 | 🟡 Basic suggestions |
| **Keep-Warm (cache TTL refresh)** | 🟢 Opt-in ping before cache expiry, tripwire auto-off | 🔴 | 🔴 | 🔴 | 🔴 |
| **Historical trend analysis** | 🟢 30-day trends, quality/cost/cache/duration correlation, model-switch detection | 🔴 | 🔴 | 🔴 | 🔴 |
| **Loop and spin detection** | 🟢 Catches behavioral loops before they burn | 🔴 | 🔴 | 🔴 | 🔴 |
| **Context quality scoring** | 🟢 7-signal quality score with grades | 🔴 | 🔴 | 🔴 | 🟡 Capacity % only |
| **Structural waste audit** | 🟢 Deep per-component (CLAUDE.md, skills, MCP, memory) | 🔴 | 🔴 | 🔴 | 🟡 Summary only |
| **CLAUDE.md and MEMORY.md health** | 🟢 8 auditors + attention-curve scoring | 🔴 | 🔴 | 🔴 | 🔴 |
| **Measures if compression helped** | 🟢 Local telemetry, before/after tokens, dollar savings | 🔴 | 🟡 `rtk gain` (token counts only) | 🔴 | 🔴 |
| **Fleet-level cross-agent analysis** | 🟢 | 🔴 | 🔴 | 🔴 | 🔴 |
| **Cache-safe** | 🟢 Never modifies existing context prefix | 🟡 Proxy mode rewrites in-flight | 🟢 Pre-shell only | 🟡 MCP overhead | 🟢 |
| **Zero baseline context overhead** | 🟢 External process, no context injection | 🔴 Injects instructions | 🟢 Shell-level only | 🔴 MCP server overhead | 🟢 Native |
| **Zero runtime dependencies** | 🟢 Pure stdlib (Python/TypeScript) | 🟡 Python + Rust + optional model | 🟢 Single Rust binary | 🟡 SQLite adapter required | 🟢 N/A |
| **Zero telemetry** | 🟢 | 🟢 | 🟡 Opt-in | 🟡 Varies | 🟢 |
| **Multi-platform** | 🟢 Claude Code, VS Code, Codex, OpenClaw, OpenCode, Hermes, Copilot | 🟢 Claude Code, Cursor, Codex, Aider, Copilot | 🟢 14 integrations | 🟢 15 integrations | 🔴 Claude Code only |

Every claim is tested against real sessions and a 57-fixture compression suite you can run yourself. **[Full benchmark methodology and results →](BENCHMARK.md)**

## The Dashboard

![Token Optimizer Dashboard](skills/token-optimizer/assets/dashboard-demo.gif)

One HTML page, auto-regenerates after every session via the SessionEnd hook, no manual trigger needed. The bookmarkable URL `http://localhost:24842/token-optimizer` is opt-in: it only works after you run `python3 skills/token-optimizer/scripts/measure.py setup-daemon` to start the local server. Until then, open the file path that `measure.py dashboard` prints on the `  Dashboard: ` line.

Per-turn token breakdowns, cost across four pricing tiers, cache analysis with TTL mix and hit rate, quality scores overlaid on every session, subagent cost breakdown, savings tracker with four non-overlapping pools. Zero setup after install. [Full dashboard docs →](https://alexgreensh.github.io/token-optimizer/reference/dashboard/)

## What It Saves

Savings come from four non-overlapping pools, tracked in two tiers:

| Pool | What it covers |
|---|---|
| Model routing + caching | Leaner prefix, lighter model mix, cache-write as a routing lever |
| Subagent routing | Sidechain cost optimization (Claude Code only) |
| Compression add-back | Tokens removed by delta mode, structure map, bash/search compression |
| Lean-output add-back | Output tokens never produced due to conciseness nudges |

**Two numbers, kept separate:**

- **Counted (\~$313/mo)**, logged action by action. Every time Token Optimizer swapped in a lighter model, trimmed a bulky result, or skipped a repeat read, it added it up: smarter habits \~$260/mo, while-you-work compression \~$53/mo. This is the slice metered event by event, so it is smaller and exact.
- **Big picture (\~$1,877/mo, \~18%)**, the full counterfactual. Had you worked the way you did before Token Optimizer (\~95% Opus), you would have paid about \~$10,585/mo versus \~$8,708/mo now. The gap is mostly a lighter model mix (95% Opus down to 60%, \~$1,076/mo for main routing + caching), plus cheaper subagents (\~$741/mo) and the metered compression add-back (\~$60/mo).

These numbers are never summed. Counted is the floor with hard receipts. Big picture is a model priced against your frozen pre-Token-Optimizer baseline. [See the full methodology →](BENCHMARK.md)

<p align="center">
  <img src="skills/token-optimizer/assets/real-savings.svg" alt="30-day savings report: ~$313 counted, ~$1,877 big picture" width="900">
</p>

Based on 684 sessions over 30 days (snapshot ending 2026-06-15), priced against a frozen pre-Token-Optimizer baseline (\~95% Opus). Your number is your own. [See the methodology →](BENCHMARK.md)

<p align="center">
  <img src="skills/token-optimizer/assets/user-profiles.svg" alt="What happens inside a 1M session" width="800">
</p>

## Active Compression

Nine features that actively reduce context, all on by default, all automatic, all toggleable from the dashboard or CLI.

Under the hood, **PreToolUse hooks** intercept every Read and Bash call before it enters your context. If the file was already read, only the diff comes back. If it's a code file re-read, a structural skeleton replaces the full content. If it's a CLI command, the output is compressed. **PostToolUse hooks** archive the full original to disk and log a compression event to SQLite. Nothing is lost, and everything is retrievable. **You do nothing. The hooks handle it all.**

![Active Compression overview](skills/token-optimizer/assets/active-compression-hero.svg)

| Feature | What it does | Savings |
|---|---|---|
| Delta Mode | Re-reads return only what changed | ~20% on re-reads |
| Structure Map | Unchanged file re-reads return a structural skeleton | ~30% (up to 99% per file) |
| Bash Compression | CLI output condensed to essentials | ~10% |
| Search Compression | grep/web results condensed to top hits + counts | ~15% |
| Lean-Output Nudges | Steers model to concise output when context fills | 10-15% typical, up to 30-41% output reduction |
| Quality Nudges | Warns when context quality drops | Prevents compaction loss |
| Loop Detection | Catches retry loops before they burn tokens | Measured per loop |
| Activity Mode | Adapts compaction to your session phase | Prevents decision loss |
| Decision Extraction | Preserves decisions across compactions | Prevents decision drift |

Toggle from the dashboard Manage tab, CLI (`measure.py v5 enable|disable <feature>`), or env vars. The `v5` verb is a legacy command name that controls current features.

[Read how each feature works →](https://alexgreensh.github.io/token-optimizer/features/active-compression/)

<details>
<summary><b>Per-feature details</b></summary>

### Delta Mode

When the AI re-reads a file after editing it, the Read call returns only the diff. 65%+ of Read calls in real sessions are re-reads. A 2,000-token file re-read becomes a 50-token diff.

![Delta Mode: smart re-reads](skills/token-optimizer/assets/delta-mode.svg)

Disable: `TOKEN_OPTIMIZER_READ_CACHE_DELTA=0`

### Structure Map

When Claude re-reads a code file it already saw, the Read call is blocked and replaced with a structural summary: function signatures, class hierarchies, imports. A 720KB Python file (180,000 tokens) becomes a 250-token skeleton.

Disable: `TOKEN_OPTIMIZER_READ_CACHE_MODE=shadow`

### First-Read Skeleton

On the **first** read of a large **code** file (Python or TypeScript, 16KB–256KB) in a history-validated cohort, the Read returns a structural skeleton (signatures/imports) instead of the full file, with a one-line notice and the full content always one step away: `expand <key>`, a ranged Read (`offset`/`limit`), or a direct Edit. The original is archived before any substitution, fail-open — if archiving can't happen, the full file is served.

This applies to **code only**. Markdown and other prose are **not** skeletoned on first read (as of v5.11.27): a headings-only outline drops load-bearing prose, so docs always come back complete. A runtime tripwire auto-demotes a code cohort back to measure-only if its live edit-rate climbs.

Disable serving (keep measurement): `TOKEN_OPTIMIZER_FIRST_READ_ACTIVE=0`. Disable entirely: `TOKEN_OPTIMIZER_FIRST_READ_SHADOW=0`. Both are also visible and toggleable via `measure.py v5 status` and the dashboard Manage tab.

### Bash Output Compression

Rewrites common CLI commands to return compressed summaries. Covers lint, log tails, tree, docker pull, long listings, build output, and test runners. A 564-token pytest output becomes 115 tokens.

![Bash Output Compression](skills/token-optimizer/assets/bash-compression.svg)

Disable: `TOKEN_OPTIMIZER_BASH_COMPRESS=0`

### Search Result Compression

When the AI runs grep, rg, or web searches that return long result lists, the output is condensed to the top hits plus a count. A 500-line grep result becomes 20 lines plus a summary.

Disable: `TOKEN_OPTIMIZER_BASH_COMPRESS_SEARCH=0`

### Lean-Output Nudges

When context fills past 25% and quality drops, a short nudge tells the model to reason deeply but keep visible output lean. Live A/B testing showed a 10-15% typical reduction in output tokens, up to 30-41%, on real prompts. Cache-safe: injected as `additionalContext`, never modifies the existing prefix.

Disable: `TOKEN_OPTIMIZER_VERBOSITY_STEER=0`

### Quality Nudges

Watches context quality in real time. When the score drops 15+ points or crosses below 60, an inline note enters the context. Claude sees it on the next turn and surfaces the warning or adjusts behavior. Cooldown of 5 minutes, max 3 per session.

Disable: `TOKEN_OPTIMIZER_QUALITY_NUDGES=0`

### Loop Detection

Catches the AI getting stuck on a retry loop. Compares the last 4 user messages and last 5 tool results for similarity. Fires at confidence ≥0.7, session cap of 2 notes. Savings measured from actual loop turn content.

Disable: `TOKEN_OPTIMIZER_LOOP_DETECTION=0`

### Activity Mode Detection

Classifies your session into one of five modes (code, debug, review, infra, general) using the last 10 tool calls. The mode feeds into compaction guidance so PRESERVE/DROP priorities adapt to what you're doing.

### Decision Extraction

Detects decision statements in real-time from tool outputs and stores them in the session database. At compaction time, these decisions are injected as CRITICAL DECISIONS that the compaction summary must preserve verbatim. Capped at 10 per session.

### Measuring real savings

All compression features log to a local SQLite table. Nothing leaves your machine.

```bash
python3 measure.py compression-stats --days 30
```

</details>

## The Session Database

Everything Token Optimizer does is backed by two local SQLite databases. Nothing leaves your machine. Zero network calls.

<p align="center">
  <img src="skills/token-optimizer/assets/session-database-flow.svg" alt="Session database flow: tool calls compressed, archived, logged to SQLite, retrievable" width="900">
</p>

**Per-session DB** (`~/.claude/token-optimizer/snapshots/session-store/<session>.db`) holds 8 tables tracking file reads, tool outputs, command outputs, cached content, context intel events, activity log, decision log, and hint serves. WAL mode for concurrent read/write from hook processes. 50MB cap per session.

**Trends DB** (`~/.claude/token-optimizer/snapshots/trends.db`) holds 7 tables tracking session history, daily aggregates, skill/model/subagent usage, savings events, and compression events. Indexed by session UUID for O(log n) joins. This is what powers the dashboard, coach mode, and 30-day trend analysis.

Every compression event, every saving, every quality measurement is a row you can query. The dashboard is a read-only view of this data. `measure.py compression-stats` is a SQL query. Your data is your data.

## Progressive Disclosure

Large tool results (>4KB) are archived to disk and replaced with a short preview plus a retrieval pointer. The full output survives compaction. When the model needs it, it pulls the original via `expand`, with no command re-run and no lost output.

This isn't just storage. The system tracks how many results were archived vs re-expanded, so you can see the net tokens that stayed collapsed. Re-expansions are netted out of the savings total, so you only count what actually stayed compressed.

```bash
python3 measure.py expand --list          # List archived tool results
python3 measure.py expand <tool-use-id>   # Retrieve a specific result
```

Some MCP tools are *documented* to return large verbatim payloads (source fetchers, doc retrievers) where a metadata preview defeats the point. Allowlist those so their full result reaches context instead of being replaced (it's still archived, so `expand` works). Set a comma-separated list of `fnmatch` globs matched against the full tool name:

`TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS="mcp__context7__*,*github_repository_file"`

Default is empty — nothing is exempt unless you opt a tool in.

## Session Continuity

Compression matters, but the most important thing Token Optimizer does is keep your work alive across sessions and compactions, automatically.

When you end a session, Token Optimizer checkpoints your state to SQLite: active task, key decisions, modified files, git branch, recent reads. When you start a new session, it scores all recent checkpoints against your prompt and surfaces the most relevant one as a hint. You resume with context, not from zero.

**What happens automatically:**

- **Cross-session hints**: relevance-scored checkpoints surface when you start a new session. The hint includes the prior session's task, decisions, files, and branch. All fenced as RECOVERED DATA, never instructions.
- **Cold-resume-lean**: reopen a stale session without paying the full transcript cost. Token Optimizer reconstructs a lean context from its checkpoint. No LLM call, no full-transcript cold-resume. Token-free reconstruction from SQLite.
- **Hint-follow measurement**: when a continuity hint surfaces file paths and the model subsequently reads one, Token Optimizer credits an avoided exploratory search. Measured causality, not a guess.

```bash
python3 measure.py resume-lean                    # list reopenable cold sessions
python3 measure.py resume-lean <#|session_id> --print  # emit lean context block
```

Compression savings only stick if your session survives compaction. Session continuity is what makes that happen.

## Smart Compaction

When auto-compact fires, 60-70% of your conversation vanishes. Decisions, error-fix sequences, agent state, all gone.

Token Optimizer checkpoints your session before compaction and restores what the summary dropped, automatically, via hooks. It also injects a **context intel digest**: heuristic summaries of large tool outputs the model already processed (file paths touched, errors seen, line counts). After compaction, the model knows what it saw without re-reading everything.

**Activity mode detection** classifies your session in real time (code, debug, review, infra, general) using the last 10 tool calls. The mode feeds into compaction guidance so PRESERVE/DROP priorities adapt to what you're doing right now, not a generic heuristic.

**Decision extraction** captures decision statements from tool outputs as they happen and stores them in the session DB. At compaction time, these are injected as CRITICAL DECISIONS the summary must preserve verbatim. Capped at 10 per session.

Compression savings only stick if your session survives compaction. Saving tokens on `git status` doesn't help if the next auto-compact wipes out the decision that made you run it.

<p align="center">
  <img src="skills/token-optimizer/assets/quality-nudges-loops.svg" alt="Quality Nudges and Loop Detection in action" width="800">
</p>

<details>
<summary><b>How Smart Compaction works</b></summary>

### Progressive Checkpoints

Captures session state at multiple thresholds: 20%, 35%, 50%, 65%, 80% context fill, plus quality drops below 80, 70, 50, and 40. Also snapshots before agent fan-out and after large edit batches. On restore, picks the richest eligible checkpoint.

### Context Intel Digest

After compaction, Token Optimizer injects a digest of the session's largest tool outputs: file paths touched, errors detected, line counts. Generated heuristically in <30ms, no LLM call. The model re-orients without re-reading everything.

```bash
python3 measure.py setup-smart-compact    # checkpoint + restore hooks
```

</details>

## Output Tokens: Lean vs Verbose

Output tokens are the most expensive part of your session. They cost 5x more than input tokens on Opus and are billed per generation, not per cache read. A verbose response to a simple question burns dollars you never needed to spend.

Token Optimizer handles this automatically with **lean-output nudges**. When your context fills past 25% and quality starts dropping, a short nudge tells the model to reason deeply but keep visible output lean. Live A/B testing showed a **10-15% typical reduction in output tokens, up to 30-41%**, on real prompts.

**How it works:**

- The nudge is injected as `additionalContext`, never modifying the existing prefix, so your cache stays intact
- It only fires when context is filling up, not when you have plenty of room
- The model still thinks through the problem; it just produces a more concise visible answer
- Disable any time: `TOKEN_OPTIMIZER_VERBOSITY_STEER=0`

This is one of the 9 active compression features, and it's the one that saves on the output side, where tokens cost the most.

## Quality Scoring

The quality score tracks two things: **Resource Health** (how close you are to the degradation cliff) and **Session Efficiency** (whether your tokens are doing useful work). Letter grades from S to F make triage instant.

As context fills, quality drops. MRCR falls from 93% to 76% between 256K and 1M context. Your AI gets measurably dumber as the window fills. The quality score shows you exactly when that happens.

![Real session quality breakdown](skills/token-optimizer/assets/quality-example.svg)

The status bar shifts color as quality degrades: green, yellow, orange, red. When quality drops below 75, session duration appears as a warning. Running subagents show with their model and elapsed time.

![Status Bar Degradation](skills/token-optimizer/assets/status-bar.svg)

```bash
python3 measure.py setup-quality-bar      # one-time install
```

[Read how scoring works →](https://alexgreensh.github.io/token-optimizer/features/quality-signals/)

<details>
<summary><b>Quality score details</b></summary>

| Score | Signals | What it means |
|--------|--------|----------------|
| **Resource Health** | Context fill, compaction depth, absolute waste tokens | How close you are to the degradation cliff |
| **Session Efficiency** | Stale reads, bloated results, decision density, agent efficiency | Whether the session is using tokens well right now |

| Grade | Range | Meaning |
|-------|-------|---------|
| **S** | 90-100 | Peak efficiency |
| **A** | 80-89 | Healthy, minor optimization possible |
| **B** | 70-79 | Degradation starting |
| **C** | 55-69 | Significant waste |
| **D** | 40-54 | Serious problems |
| **F** | 0-39 | Context is rotting, immediate action needed |

**Quality bar disappeared?** Running Claude Code's `/statusline` overwrites Token Optimizer's entry. SessionStart auto-restores it. Just start a new session and it's back.

**Want it off for good?**
```bash
python3 measure.py setup-quality-bar --uninstall
```

</details>

## Coach Mode

```
> /token-coach
```

Tell it your goal. Get back specific, prioritized fixes with exact token savings. It reads 30 days of your session data and surfaces what no single session can show: quality drifting down, sessions getting longer, cache hit rates falling, cost per session climbing.

Every insight is grounded in your actual numbers. "Your short sessions score 68 vs 60 for long ones" hits differently than "consider shorter sessions." Coach Mode also identifies project-level optimization opportunities (skills you never use, MCP servers that load eagerly, CLAUDE.md patterns that break your cache) and teaches you how to fix them so future sessions start leaner.

[Read about Coach Mode →](https://alexgreensh.github.io/token-optimizer/features/coach-mode/)

<details>
<summary><b>Coach Mode details</b></summary>

### Historical patterns detected

| Pattern | What it detects |
|---|---|
| Quality drift | Average quality dropping week over week |
| Session duration creep | Sessions getting longer, filling context faster |
| Cache degradation | Cache hit rate falling (and whether model switches cause it) |
| Grade distribution | Too many D-grade sessions piling up |
| Cost awareness | Cost per session climbing, with routing advice |
| Duration-quality correlation | Short sessions scoring higher, suggesting you break up long ones |
| Compression gap | Shadow-only savings far exceeding active compression |
| Model switching | Frequent mid-session model switches invalidating the prompt cache |

### Waste detectors

11 automated detectors analyze your session patterns:

| Detector | What it catches |
|---|---|
| PDF/binary ingestion | Large files consuming context |
| Web search overhead | Too many web results dumped into context |
| Retry churn | Same tool retried 3+ times with errors |
| Tool cascade | 3+ consecutive tool errors |
| Looping | Repeated similar messages |
| Overpowered model | Opus used for simple edits |
| Weak model | Haiku on complex tasks |
| Bad decomposition | Monolithic 500+ word prompts |
| Wasteful thinking | Extended thinking >2x output for small edits |
| Output waste | Verbose responses to simple operations |
| Cache instability | CLAUDE.md patterns that break the prompt cache |

### Keep-Warm

Opt-in feature for API-billed sessions. Issues a tiny cache-read ping just before your prompt cache entry would expire, refreshing its TTL. Costs ~0.1x of the prefix versus the 1.25-2x re-write you'd pay on resume. Tripwire auto-off if pings stop paying for themselves.

```bash
python3 measure.py keepwarm-enable          # opt in (API billing only)
python3 measure.py keepwarm-report            # net savings, spend, tripwire state
python3 measure.py keepwarm-disable           # opt out any time
```

### Fleet Auditor

Scans across Claude Code, Codex, and custom transcript setups to find idle burns, model misrouting, and config bloat with dollar savings per finding.

### CLAUDE.md Routing Injection

Generate model routing instructions from your actual usage data and inject them into CLAUDE.md. 48-hour staleness guard auto-removes stale advice.

```bash
python3 measure.py inject-routing --dry-run   # Preview
python3 measure.py inject-routing              # Inject
```

</details>

## FAQ

<details>
<summary><b>🔒 Can it degrade my context quality?</b></summary>

No. Structural optimization only removes genuinely unused components. Active Compression controls can be disabled with a single command or env var. The quality scoring system tracks degradation in real time.

</details>

<details>
<summary><b>💾 Does it break the prompt cache?</b></summary>

No. Token Optimizer never touches content already in your context. It works on new content entering your window and on the compaction boundary. Your cache prefix stays intact, which means it saves you money twice: less input per turn, and cheaper cache reads on every turn forward.

</details>

<details>
<summary><b>📡 Does it send any data anywhere?</b></summary>

No analytics, no telemetry endpoint, no product data leaves your machine. Measurement events are local SQLite rows you own. Zero network calls.

</details>

<details>
<summary><b>⚠️ Can it hurt my session?</b></summary>

No. All hooks are non-blocking with fail-open design. If a Token Optimizer script errors, your command runs normally.

</details>

<details>
<summary><b>📦 Any runtime dependencies?</b></summary>

No. Pure Python stdlib on Claude Code and Codex. TypeScript with zero runtime deps on OpenCode and OpenClaw.

</details>

<details>
<summary><b>🔐 How does install.sh verify file integrity?</b></summary>

Resolves the latest GitHub Release tag, checks out that tag, fetches CHECKSUMS.sha256 from the same release, and verifies every script file. Out-of-band verification means a compromised commit cannot swap both code and checksums simultaneously.

</details>

## All Commands

<details>
<summary><b>Show all commands</b></summary>

| Command | What it does | Docs |
|---|---|---|
| `/token-optimizer` | Full audit with 6 parallel agents, guided fixes | [→](https://alexgreensh.github.io/token-optimizer/start/quickstart/) |
| `/token-coach` | 30-day trend analysis, prioritized fixes | [→](https://alexgreensh.github.io/token-optimizer/features/coach-mode/) |
| `quick` | 10-second health check | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `doctor` | Installation check, score out of 10 | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `dashboard` | Open the HTML dashboard | [→](https://alexgreensh.github.io/token-optimizer/reference/dashboard/) |
| `savings` | Dollar savings report | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `report` | Per-component token breakdown | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `quality` | Context-quality analysis of live session | [→](https://alexgreensh.github.io/token-optimizer/features/quality-signals/) |
| `trends` | Skill adoption, model mix, overhead over time | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `compression-stats` | Measured savings from active compression | [→](https://alexgreensh.github.io/token-optimizer/features/active-compression/) |
| `memory-review` | MEMORY.md structural audit | [→](https://alexgreensh.github.io/token-optimizer/features/memory-health/) |
| `git-context` | Suggest files for your current diff | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `drift` | Side-by-side comparison vs your last snapshot | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `conversation` | Per-message token and cost breakdown | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `pricing-tier` | View or switch pricing tiers | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `expand` | Retrieve an archived tool result (progressive disclosure) | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |
| `resume-lean` | Reopen a cold session with token-free reconstruction | [→](https://alexgreensh.github.io/token-optimizer/reference/cli/) |

[Full CLI reference →](https://alexgreensh.github.io/token-optimizer/reference/cli/)

</details>

## License

**PolyForm Noncommercial 1.0.0**. Source-available. Personal, research, educational, and non-commercial use requires no license purchase.

### Personal / hobby / research / education?
Go for it. Full source, runs locally, no license purchase needed.

### Small team (under 5 people OR under $20k/month revenue)?
Small teams get a no-cost commercial license automatically. Just use it.

### Started personal, now it's turning into a business?
Your past use is totally fine. The license has a built-in 32-day grace period after any written notice. Reach out for a commercial license when you're ready.

### Larger company / commercial use?
Contact [Alex Greenshpun](https://linkedin.com/in/alexgreensh) or me@alexgreenshpun.com.

---

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
