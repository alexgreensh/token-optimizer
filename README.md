<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center">
  <a href="https://github.com/alexgreensh/token-optimizer/releases"><img src="https://img.shields.io/badge/version-2.4.1-green" alt="Version 2.4.0"></a>
  <a href="https://github.com/alexgreensh/token-optimizer"><img src="https://img.shields.io/badge/Claude_Code-Plugin-blueviolet" alt="Claude Code Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/openclaw"><img src="https://img.shields.io/badge/OpenClaw-Plugin-brightgreen" alt="OpenClaw Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/LICENSE"><img src="https://img.shields.io/github/license/alexgreensh/token-optimizer" alt="License"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/stargazers"><img src="https://img.shields.io/github/stars/alexgreensh/token-optimizer" alt="GitHub Stars"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/commits/main"><img src="https://img.shields.io/github/last-commit/alexgreensh/token-optimizer" alt="Last Commit"></a>
  <img src="https://img.shields.io/badge/python-3.8+-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
</p>

<h2 align="center">Your AI is getting dumber and you can't see it.</h2>

<p align="center"><em>Find the ghost tokens. Survive compaction. Track the quality decay.</em></p>

<p align="center">
Opus 4.6 drops from 93% to 76% accuracy across a 1M context window. Compaction loses 60-70% of your conversation. Ghost tokens burn through your plan limits on every single message. Token Optimizer tracks the degradation, cuts the waste, checkpoints your decisions before compaction fires, and tells you what to fix.
</p>

<p align="center">
  <img src="skills/token-optimizer/assets/hero-terminal.svg" alt="Token Optimizer Quick Scan" width="800">
</p>

## Now Multi-Platform

Token Optimizer ships native plugins for multiple AI agent systems. One repo, per-platform plugins, shared waste detection patterns.

| Platform | Status | Install |
|----------|--------|---------|
| **Claude Code** | Stable (v2.4.7) | `/plugin marketplace add alexgreensh/token-optimizer` |
| **OpenClaw** | v1.0.0 | `openclaw plugins install token-optimizer-openclaw` |

Each platform gets its own native plugin (Python for Claude Code, TypeScript for OpenClaw). No bridging, no shared runtime, zero cross-platform dependencies.

- **Claude Code**: Interactive dashboard, quality scoring, smart compaction, session trends
- **OpenClaw**: CLI-first audit with waste detection, dollar savings, and fix snippets. Supports any model (Claude, GPT-5, Gemini, DeepSeek, local via Ollama) with configurable pricing.

---

## Install: Claude Code (3 lines)

```bash
# Plugin (recommended, auto-updates)
/plugin marketplace add alexgreensh/token-optimizer
/plugin install token-optimizer@alexgreensh-token-optimizer
```

Or script installer:

```bash
curl -fsSL https://raw.githubusercontent.com/alexgreensh/token-optimizer/main/install.sh | bash
```

Then in Claude Code: `/token-optimizer`

## Why install this first?

Every Claude Code session starts with invisible overhead: system prompt, tool definitions, skills, MCP servers, CLAUDE.md, MEMORY.md. A typical power user burns 50-70K tokens before typing a word.

At 200K context, that's 25-35% gone. At 1M, it's "only" 5-7%, but the problems compound:

- **Quality degrades as context fills.** MRCR drops from 93% to 76% across 256K to 1M. Your AI gets measurably dumber with every message.
- **You hit rate limits faster.** Ghost tokens count toward your plan's usage caps on every message, cached or not. 50K overhead × 100 messages = 5M tokens burned on nothing.
- **Compaction is catastrophic.** 60-70% of your conversation gone per compaction. After 2-3 compactions: 88-95% cumulative loss. And each compaction means re-sending all that overhead again.
- **Higher effort = faster burn.** More thinking tokens per response means you hit compaction sooner, which means more total tokens consumed across the session.

Token Optimizer tracks all of this. Quality score, degradation bands, compaction loss, drift detection. Zero context tokens consumed (runs as external Python).

> **"But doesn't removing tokens hurt the model?"** No. Token Optimizer removes structural waste (duplicate configs, unused skill frontmatter, bloated files), not useful context. It also actively *measures* quality: the 7-signal quality score tells you if your session is degrading, and Smart Compaction checkpoints your decisions before auto-compact fires. Most users see quality scores *improve* after optimization because the model has more room for real work.

---

### NEW in v2.4: Degradation Intelligence

| Command | What You Get |
|---------|-------------|
| `quick` | **"Am I in trouble?"** 10-second answer: context health, degradation risk, biggest token offenders, which model to use. |
| `doctor` | **"Is everything installed correctly?"** Score out of 10. Broken hooks, missing components, exact fix commands. |
| `drift` | **"Has my setup grown?"** Side-by-side comparison vs your last snapshot. Catches config creep before it costs you. |
| `quality` | **"How healthy is this session?"** 7-signal analysis of your live conversation. Stale reads, wasted tokens, compaction damage. |
| `report` | **"Where are my tokens going?"** Full per-component breakdown. Every skill, every MCP server, every config file. |
| `/token-optimizer` | **"Fix it for me."** Interactive audit with 6 parallel agents. Guided fixes with diffs and backups. |

### Quality Scoring (7 signals)

| Signal | Weight | What It Means For You |
|--------|--------|----------------|
| **Context fill** | 20% | How close are you to the degradation cliff? Based on published MRCR benchmarks. |
| **Stale reads** | 20% | Files you read earlier have changed. Your AI is working with outdated info. |
| **Bloated results** | 20% | Tool outputs that were never used. Wasting context on noise. |
| **Compaction depth** | 15% | Each compaction loses 60-70% of your conversation. After 2: 88% gone. |
| **Duplicates** | 10% | The same system reminders injected over and over. Pure waste. |
| **Decision density** | 8% | Are you having a real conversation or is it mostly overhead? |
| **Agent efficiency** | 7% | Are your subagents pulling their weight or just burning tokens? |

Degradation bands in the status bar:
- Green (<50% fill): peak quality zone
- Yellow (50-70%): degradation starting
- Orange (70-80%): quality dropping
- Red (80%+): severe, consider /clear

### What Degradation Actually Looks Like

This is a real session. 708 messages, 2 compactions, 88% of the original context gone. Without the quality score, you'd have no idea.

![Real session quality breakdown](skills/token-optimizer/assets/quality-example.svg)

### Smart Compaction

Auto-compaction is lossy. Smart Compaction checkpoints decisions, errors, and agent state before it fires, then restores what the summary dropped.

```bash
python3 $MEASURE_PY setup-smart-compact    # checkpoint + restore hooks
python3 $MEASURE_PY setup-quality-bar      # live quality score in status bar
```

---

## How It Compares

| Capability | Token Optimizer | `/context` (built-in) | context-mode |
|---|---|---|---|
| Startup overhead audit | Deep (per-component) | Summary (v2.1.74+) | No |
| Quality degradation tracking | MRCR-based bands | Basic capacity % | No |
| Guided remediation | Yes, with token estimates | Basic suggestions | No |
| Runtime output containment | No | No | Yes (98% reduction) |
| Smart compaction survival | Checkpoint + restore | No | Session guide |
| Model recommendation | Yes (Sonnet vs Opus by context) | No | No |
| Usage trends + dashboard | SQLite + interactive HTML | No | Session stats |
| Compaction loss tracking | Yes (cumulative % lost) | No | Partial |
| Multi-platform | Claude Code + OpenClaw (more coming) | Claude Code | 6 platforms |
| Context tokens consumed | 0 (Python script) | ~200 tokens | MCP overhead |

`/context` shows capacity. Token Optimizer fixes the causes.
context-mode prevents runtime floods. Token Optimizer prevents structural waste.

---

## The Problem

Every message you send to Claude Code re-sends everything: system prompt, tool definitions, MCP servers, skills, commands, CLAUDE.md, MEMORY.md, and system reminders. The API is stateless. These are the ghost tokens: invisible overhead that eats your context window before you type a word.

Prompt caching makes this [cheaper](https://code.claude.com/docs/en/costs) (90% cost reduction on cached tokens). But cheaper doesn't mean free, and it doesn't mean small. Those tokens still fill your context window, still count toward your plan's rate limits on every message, and still degrade output quality. On Claude Max or Pro, ghost tokens eat into the same usage caps you need for actual work.

The more you've customized Claude Code, the worse it gets. And at 1M, the real problem isn't startup overhead, it's the compounding cost: degradation as the window fills, plus rate limit burn from overhead you never see.

![What happens inside a 1M session](skills/token-optimizer/assets/user-profiles.svg)

### Where it all goes

**Fixed overhead** (everyone pays): System prompt (~3K tokens) plus built-in tool definitions (12-17K tokens). About 8-10% at 200K, or 1.5-2% at 1M.

**Autocompact buffer**: ~30-35K tokens (~16%) reserved for compaction headroom.

**MCP tools**: The biggest variable. Anthropic's team [measured 134K tokens consumed by tool definitions](https://www.anthropic.com/engineering/advanced-tool-use) before optimization. [Tool Search](https://www.anthropic.com/engineering/advanced-tool-use) reduced this by 85%, but servers still add up.

**Your config stack** (what this tool optimizes): CLAUDE.md that's grown organically. MEMORY.md that duplicates half of it. 50+ skills you installed and forgot. Commands you never use. [`@imports`](https://code.claude.com/docs/en/memory). [`.claude/rules/`](https://code.claude.com/docs/en/memory). No `permissions.deny` rules.

## What This Does

One command. Six parallel agents audit your entire setup. Prioritized fixes with exact token savings. Everything backed up before any change.

![How Token Optimizer works](skills/token-optimizer/assets/how-it-works.svg)

You see diffs. You approve each fix. Nothing irreversible.

### What it audits

| Area | What It Catches |
|------|----------------|
| **CLAUDE.md** | Content that should be skills or reference files. Duplication with MEMORY.md. [`@imports`](https://code.claude.com/docs/en/memory). Poor cache structure. |
| **MEMORY.md** | Overlap with CLAUDE.md. Verbose entries. Content past the [200-line auto-load cap](https://code.claude.com/docs/en/memory). |
| **Skills** | Unused skills loading frontmatter (~100 tokens each). Duplicates. Wrong directory. |
| **MCP Servers** | Broken/unused servers. Duplicate tools. Missing [Tool Search](https://www.anthropic.com/engineering/advanced-tool-use). |
| **Commands** | Rarely-used commands (~50 tokens each). |
| **Rules & Advanced** | [`.claude/rules/`](https://code.claude.com/docs/en/memory) overhead. Missing `permissions.deny`. No hooks. |

### The fix: progressive disclosure

| Where | Token Cost | What Goes Here |
|-------|-----------|----------------|
| **CLAUDE.md** | Every message (~800 token target) | Identity, critical rules, key paths |
| **Skills & references** | ~100 tokens in menu, full when invoked | Workflows, configs, standards |
| **Project files** | Zero until read | Guides, templates, documentation |

## Interactive Dashboard

After the audit, you get an interactive HTML dashboard.

![Token Optimizer Dashboard](skills/token-optimizer/assets/dashboard-overview.png)

Every component is clickable. Expand any item to see why it matters, what the trade-offs are, and what changes. Toggle the fixes you want, and copy a ready-to-paste optimization prompt.

### Persistent Dashboard

The dashboard auto-regenerates after every session (via the SessionEnd hook).

```bash
python3 $MEASURE_PY setup-daemon     # Bookmarkable URL at http://localhost:24842/
python3 $MEASURE_PY dashboard --serve # One-time serve over HTTP
```

## Enable Session Tracking

```bash
python3 $MEASURE_PY setup-hook --dry-run   # preview
python3 $MEASURE_PY setup-hook             # install
```

Adds a SessionEnd hook that collects usage stats after each session (~2 seconds, all data local).

## Usage Analytics: See What's Actually Being Used

**Trends**: Which skills do you actually invoke vs just having installed? Which models are you using? How has your overhead changed over time?

**Session Health**: Catches stale sessions (24h+), zombie sessions (48h+), and outdated configurations before they cause problems.

```bash
python3 $MEASURE_PY trends              # usage patterns over time
python3 $MEASURE_PY health              # session hygiene check
```

## Coach Mode: Not Sure Where to Start?

```
> /token-coach
```

Tell it your goal. Get back specific, prioritized fixes with exact token savings. Detects 8 named anti-patterns (The Kitchen Sink, The Hoarder, The Monolith...) and recommends multi-agent design patterns that actually save context.

## v2.0+: Active Session Intelligence

### Smart Compaction: Don't Lose Your Work

When auto-compact fires, 60-70% of your conversation vanishes. Decisions, error-fix sequences, agent state: gone. Smart Compaction saves all of it as checkpoints before compaction, then restores what the summary dropped.

```bash
python3 $MEASURE_PY setup-smart-compact    # one-time install
```

### Live Quality Bar: Know Before It's Too Late

A glance at your terminal tells you if you're in trouble. Colors shift from green to red as quality degrades.

![Status Bar Degradation](skills/token-optimizer/assets/status-bar.svg)

```bash
python3 $MEASURE_PY setup-quality-bar      # one-time install
```

### Session Continuity: Pick Up Where You Left Off

Sessions auto-checkpoint on end, /clear, and crashes. Start a new session on the same topic and it injects the relevant context automatically.

## All Commands

Standalone Python script. No dependencies. Python 3.8+. Zero context tokens consumed.

```bash
python3 $MEASURE_PY quick                # Am I in trouble? (start here)
python3 $MEASURE_PY doctor               # Is everything installed right?
python3 $MEASURE_PY drift                # Has my setup grown since last check?
python3 $MEASURE_PY quality current      # How healthy is this session?
python3 $MEASURE_PY report               # Where are my tokens going?
python3 $MEASURE_PY dashboard            # Visual dashboard (HTML)
python3 $MEASURE_PY trends               # What's actually being used?
python3 $MEASURE_PY collect              # Build usage database
```

## OpenClaw Plugin

Native TypeScript plugin for OpenClaw agent systems. Zero Python dependency. Works with any model (Claude, GPT-5, Gemini, DeepSeek, local via Ollama). Reads your OpenClaw pricing config for accurate cost tracking, falls back to built-in rates for 20+ models.

```bash
# Install from npm
openclaw plugins install token-optimizer-openclaw

# Or use the CLI directly
npx token-optimizer scan --days 30
npx token-optimizer audit --json
```

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

**What it does:** Session parsing, cost calculation, waste detection (heartbeat model waste, empty runs, over-frequency, stale configs, session bloat, loops, abandoned sessions), and Smart Compaction (checkpoint/restore across compaction events).

**What's different from Claude Code:** The OpenClaw plugin does not yet include context quality scoring (the 7-signal ContextQ metric). Quality scoring requires platform-specific session analysis that's being built for OpenClaw v1.1.

See [`openclaw/README.md`](openclaw/README.md) for full docs.

---

## What's Inside

```
skills/token-optimizer/
  SKILL.md                             Orchestrator (phases 0-5 + v2.0 actions)
  assets/
    dashboard.html                     Interactive dashboard
    logo.svg                           Animated ASCII logo
    hero-terminal.svg                  Terminal demo
  references/
    agent-prompts.md                   8 agent prompt templates
    implementation-playbook.md         Fix implementation details
    optimization-checklist.md          32 optimization techniques
    token-flow-architecture.md         How Claude Code loads tokens
  examples/
    claude-md-optimized.md             Optimized CLAUDE.md template
    permissions-deny-template.json     permissions.deny starter
    hooks-starter.json                 Hook configuration
  scripts/
    measure.py                         Core engine (audit, quality, smart compact, trends, health, quick, doctor, drift)
    statusline.js                      Status line (degradation-aware colors)
skills/token-coach/
  SKILL.md                             Coaching orchestrator
openclaw/                                OpenClaw native plugin (TypeScript)
  src/                                 7 source modules (models, parser, detectors, compaction, CLI)
  skills/token-optimizer/SKILL.md      OpenClaw skill
  hooks/smart-compact/HOOK.md          Compaction hooks
  openclaw.plugin.json                 Plugin manifest
install.sh                             One-command installer
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
