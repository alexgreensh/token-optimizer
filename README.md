<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center"><strong>Run <code>/context</code> on a fresh Claude Code session. See how much is already gone.<br>This tool shows you where it went and gets it back.</strong></p>

![Token Optimizer in action](skills/token-optimizer/assets/hero-terminal.svg)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/alexgreensh/token-optimizer/main/install.sh | bash
```

Then start Claude Code and run:

```
/token-optimizer
```

Or manually:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
ln -s ~/.claude/token-optimizer/skills/token-optimizer ~/.claude/skills/token-optimizer
```

Updates: `cd ~/.claude/token-optimizer && git pull`. The installer uses a symlink, so the skill always loads from the repo.

## The Problem

Every message you send to Claude Code re-sends everything: system prompt, tool definitions, MCP servers, skills, commands, CLAUDE.md, MEMORY.md, and system reminders. The API is stateless. No memory between messages. The full stack, replayed every time.

Prompt caching makes this [cheap](https://code.claude.com/docs/en/costs) (90% cost reduction on cached tokens). But cheap doesn't mean small. Those tokens still fill your context window, count toward rate limits, and degrade output quality past 50-70% fill.

The more you've customized Claude Code, the worse it gets.

![Where your context window goes](skills/token-optimizer/assets/user-profiles.svg)

### Where it all goes

Your 200K context window gets eaten from multiple directions:

**Fixed overhead** (everyone pays, can't change): System prompt (~3K tokens) plus built-in tool definitions (12-17K tokens). About 8-10% of your window, gone before anything else loads.

**Autocompact buffer**: When autocompact is on (the default), Claude Code reserves headroom for compaction. In practice, roughly 30-35K tokens (~16% of your window) sit empty. Run `/context` on a fresh session to see the exact number.

**MCP tools**: The biggest variable. Anthropic's own engineering team [measured 134K tokens consumed by tool definitions](https://www.anthropic.com/engineering/advanced-tool-use) before optimization. [Tool Search](https://www.anthropic.com/engineering/advanced-tool-use) (activates automatically when MCP tools exceed [~10% of context](https://code.claude.com/docs/en/costs)) reduced this by 85%, but MCP servers still add up: each deferred tool costs ~15 tokens, plus server instructions.

**Your config stack** (what this tool optimizes): CLAUDE.md that's grown organically. MEMORY.md that duplicates half of it. 50+ skills you installed and forgot. Commands you never use. [`@imports`](https://code.claude.com/docs/en/memory) pulling in files you didn't realize. [`.claude/rules/`](https://code.claude.com/docs/en/memory) adding up quietly. No `.claudeignore` to block system reminder injection.

A real power user's session baseline: **~43,000 tokens consumed per message** (22% of the 200K window). Add the autocompact buffer and **~38% is unavailable** before typing a single word.

Every subagent you spawn gets its own 200K window and loads the same full stack. Five parallel agents means five copies of that overhead, each starting ~30% full before doing any work.

## What This Does

One command. Six parallel agents audit your entire setup. You get a prioritized list of exactly what's eating your context and how to fix it.

```
> /token-optimizer

[Token Optimizer] Backing up config...
Dispatching 6 audit agents...

YOUR SETUP
Per-message overhead:  ~43,000 tokens
Context used:          38% before your first message

QUICK WINS
  Slim CLAUDE.md + MEMORY.md:      -5,200 tokens/msg
  Archive unused skills + commands: -4,800 tokens/msg
  Prune MCP + add .claudeignore:    -5,000 tokens/msg

Total: ~15,000 tokens/msg recovered

Ready to implement? Everything backed up first.
```

Everything gets backed up before any change. You see diffs. You approve each fix. Nothing irreversible.

### What it audits

| Area | What It Catches |
|------|----------------|
| **CLAUDE.md** | Content that should be skills or reference files. Duplication with MEMORY.md. [`@imports`](https://code.claude.com/docs/en/memory) pulling in more than you realize. Poor cache structure. |
| **MEMORY.md** | Overlap with CLAUDE.md. Verbose entries. Content past the [200-line auto-load cap](https://code.claude.com/docs/en/memory). |
| **Skills** | Unused skills still loading frontmatter (~100 tokens each). Duplicates. Archived skills in the wrong directory still loading. |
| **MCP Servers** | Broken/unused servers. Duplicate tools across servers and plugins. Missing [Tool Search](https://www.anthropic.com/engineering/advanced-tool-use). |
| **Commands** | Rarely-used commands inflating the menu (~50 tokens each). |
| **Rules & Advanced** | [`.claude/rules/`](https://code.claude.com/docs/en/memory) overhead. Missing `.claudeignore`. No hooks. No monitoring. |

### The fix: progressive disclosure

Not everything needs to load every message. The optimizer moves content to where it costs the least:

| Where | Token Cost | What Goes Here |
|-------|-----------|----------------|
| **CLAUDE.md** | Every message (~800 token target) | Identity, critical rules, key paths |
| **Skills & references** | ~100 tokens in menu, full content only when invoked | Workflows, configs, detailed standards |
| **Project files** | Zero until explicitly read | Guides, templates, documentation |

A bloated CLAUDE.md doesn't need deleting. Coding standards move to a reference file. A deployment workflow becomes a skill. Same functionality, fraction of the per-message cost.

## Typical Results

Results depend on your setup. Heavier setups save more.

**Config cleanup** (what the tool directly changes):

| Starting Point | Typical Recovery |
|----------------|-----------------|
| Power user (50+ skills, 3+ MCP servers, bloated config) | 5-15% of context window |
| No Tool Search (disabled or not triggered) | [134K → ~8.7K tokens](https://www.anthropic.com/engineering/advanced-tool-use) (85% reduction in MCP overhead) |
| Lighter setup (few skills, 1 MCP server) | 3-8% |

**Advanced option**: Disabling autocompact and managing `/compact` manually recovers an additional ~16% of your window. The optimizer explains the tradeoff and helps you decide.

**Behavioral savings** (free, compound across every session):

| Habit | Why It Matters |
|-------|---------------|
| `/compact` at 50-70% instead of waiting for auto-compact | Better output quality, fewer hallucinations |
| [Haiku for data-gathering agents](https://code.claude.com/docs/en/costs) | 5x cheaper than Opus for file reads and counting |
| `/clear` between unrelated topics | Fresh context, no stale information dragging quality down |
| Batch requests into one message | Each message re-sends your full config stack |
| [Plan mode](https://code.claude.com/docs/en/best-practices) for complex tasks | Prevents expensive re-work from wrong initial direction |

## Interactive Dashboard

After the audit, you get an interactive HTML dashboard that breaks down exactly where your tokens go and what you can do about it.

![Token Optimizer Dashboard](skills/token-optimizer/assets/dashboard-overview.png)

Every component is clickable. Expand any item to see why it matters, what the trade-offs are, and what changes. Toggle the fixes you want, and copy a ready-to-paste optimization prompt.

## How It Works

![5-phase optimization flow](skills/token-optimizer/assets/how-it-works.svg)

| Phase | What Happens |
|-------|-------------|
| **Initialize** | Backs up your config, takes a "before" snapshot |
| **Audit** | 6 parallel agents scan everything (sonnet for judgment, haiku for counting) |
| **Analyze** | Synthesis agent (opus) prioritizes fixes by impact |
| **Implement** | You choose what to fix. Diffs and approval before every change |
| **Verify** | Re-measures everything, shows before/after with exact savings |

Right model for each job. Session folder pattern keeps agent output from flooding your context.

## Why It Matters Even With Caching

Prompt caching cuts cost by 90%. But it doesn't shrink your context window.

- **You hit compaction sooner** — compaction is lossy, every cycle throws away context
- **Rate limits burn faster** — cache reads still count toward your subscription quota
- **Quality degrades** — performance drops as context fills, especially past 70%
- **Agents multiply it** — every subagent loads its own copy of your full config stack. Dispatch 5 agents and that overhead loads 5 times, each in a fresh 200K window. [Agent teams use ~7x more tokens in plan mode](https://code.claude.com/docs/en/costs) than standard sessions. Reducing per-agent overhead from 43K to 28K saves 75K tokens across those 5 agents

## Measurement Tool

Standalone script. No dependencies. Python 3.8+.

```bash
python3 ~/.claude/skills/token-optimizer/scripts/measure.py report

# Save snapshots for before/after comparison
python3 ~/.claude/skills/token-optimizer/scripts/measure.py snapshot before
# ... make changes ...
python3 ~/.claude/skills/token-optimizer/scripts/measure.py snapshot after
python3 ~/.claude/skills/token-optimizer/scripts/measure.py compare
```

## vs Alternatives

| Tool | What It Does | Limitation |
|------|-------------|------------|
| **Manual audit** | Flexible | Takes hours. No measurement. Easy to miss things. |
| **ccusage** | Monitors spending | Shows cost, not context waste or how to fix it. |
| **token-optimizer-mcp** | Caches MCP calls | One dimension only. |
| **This** | Audits, diagnoses, fixes, measures | Requires Claude Code. |

## What's Inside

```
skills/token-optimizer/
  SKILL.md                             Orchestrator
  assets/
    dashboard.html                     Interactive optimization dashboard
    logo.svg                           Animated ASCII logo
    hero-terminal.svg                  Terminal demo
    before-after.svg                   Token breakdown comparison
    how-it-works.svg                   5-phase flow diagram
    user-profiles.svg                  Context usage by setup type
  references/
    agent-prompts.md                   8 agent prompt templates
    implementation-playbook.md         Fix implementation details (4A-4K)
    optimization-checklist.md          30 optimization techniques
    token-flow-architecture.md         How Claude Code loads tokens
  examples/
    claude-md-optimized.md             Optimized CLAUDE.md template
    claudeignore-template              .claudeignore starter
    hooks-starter.json                 Hook configuration example
  scripts/
    measure.py                         Before/after measurement tool
install.sh                             One-command installer
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
