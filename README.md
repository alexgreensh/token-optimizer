# Token Optimizer

**Your Claude Code setup is burning tokens you don't know about. This finds them.**

![Token Optimizer in action](assets/hero-terminal.svg)

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

The installer uses a symlink, so `cd ~/.claude/token-optimizer && git pull` gives you the latest version instantly.

## The Problem

Every message you send to Claude Code loads your entire config stack before you type a word: system prompt, tool definitions, skills, commands, CLAUDE.md, MEMORY.md, and system reminders.

How much this costs depends on your setup, and the measured numbers are worse than most people expect:

| Setup | Measured Overhead | Source |
|-------|------------------|--------|
| Zero MCP servers | ~11,600 tokens | [GitHub #3406](https://github.com/anthropics/claude-code/issues/3406) |
| 3 MCP servers | 42,600 tokens | [GitHub #11364](https://github.com/anthropics/claude-code/issues/11364) |
| 7 MCP servers | 67,300 tokens (34% of context) | [GitHub #11364](https://github.com/anthropics/claude-code/issues/11364) |
| Heavy MCP setup | 143,000 tokens (72% of context) | [Scott Spence](https://scottspence.com/posts/optimising-mcp-server-context-usage-in-claude-code) |

On a 200K context window, a typical power user is spending 20-40% of their context on overhead. That's less room for actual work, faster compaction cycles, and degraded response quality as the window fills up.

The culprit? Built-in tool definitions eat [12-17K tokens](https://github.com/Piebald-AI/claude-code-system-prompts) (unavoidable). Each MCP tool adds [300-850 tokens](https://dev.to/piotr_hajdas/mcp-token-limits-the-hidden-cost-of-tool-overload-2d5) on top. Skills, commands, CLAUDE.md, MEMORY.md pile on the rest.

Nobody audits this. Nobody measures it. You just keep paying.

## What It Does

![Before: 34% context overhead. After: 19%. 43% saved.](assets/before-after.svg)

One command. Six agents audit your setup in parallel. You get a prioritized fix list with exact token savings, sorted into Quick Wins, Medium effort, and Deep optimizations.

| Area | What It Catches |
|------|----------------|
| **CLAUDE.md** | Bloated config, content that should be skills, duplication with MEMORY.md |
| **MEMORY.md** | Overlap with CLAUDE.md, verbose history that should be condensed |
| **Skills** | Duplicates, archived skills still loading, unused domain skills |
| **MCP Servers** | Unused servers, duplicate tools, missing Tool Search |
| **Commands** | Rarely-used commands, merge candidates |
| **Advanced** | Missing .claudeignore, no hooks, poor cache structure, no monitoring |

## How It Works

![5-phase optimization flow](assets/how-it-works.svg)

| Phase | What Happens |
|-------|-------------|
| **Initialize** | Backs up your config files, creates coordination folder, takes a "before" snapshot |
| **Audit** | 6 parallel agents (haiku, 5x cheaper) scan your config, skills, MCP, and more |
| **Analyze** | Synthesis agent (sonnet) prioritizes into Quick Wins / Medium / Deep tiers |
| **Implement** | You choose what to fix. Creates backups, shows diffs, asks before touching anything |
| **Verify** | Re-measures everything. Shows before/after with exact token and cost savings |

The skill practices what it preaches: haiku for data gathering, sonnet only for reasoning, session folder pattern to prevent context overflow.

## Sourced Numbers

All stats below are from [Anthropic docs](https://code.claude.com/docs/en/costs), [Piebald-AI's system prompt tracking](https://github.com/Piebald-AI/claude-code-system-prompts) (v2.1.59), and community measurements linked above.

| What | Cost |
|------|------|
| Core system prompt | ~3K tokens (small, unavoidable) |
| Built-in tool definitions | 12-17K tokens (unavoidable) |
| Each MCP tool definition | 300-850 tokens |
| Each skill | ~100 tokens (frontmatter only) |
| Each command | ~50 tokens |
| [Tool Search](https://www.anthropic.com/engineering/advanced-tool-use) MCP reduction | **85%** (Anthropic-verified: 134K to 5K) |
| Prompt caching on stable prefixes | **90% cost reduction** |
| Manual `/compact` at 70% | **40-82% savings** vs auto-compact |

## Measurement Tool

Standalone Python script for measuring token overhead without running the full audit:

```bash
python3 ~/.claude/token-optimizer/scripts/measure.py report

# Save snapshots for comparison
python3 ~/.claude/token-optimizer/scripts/measure.py snapshot before
# ... make changes ...
python3 ~/.claude/token-optimizer/scripts/measure.py snapshot after
python3 ~/.claude/token-optimizer/scripts/measure.py compare
```

## vs Alternatives

| Tool | What It Does | Limitation |
|------|-------------|------------|
| **Manual audit** | Flexible | Takes hours. No measurement. Easy to miss things |
| **ccusage** | Monitors spending | Tells you what you spent, not *why* or how to fix it |
| **token-optimizer-mcp** | Caches MCP calls | One dimension only |
| **This** | Audits, diagnoses, fixes, measures | Requires Claude Code |

## What's Inside

```
skills/token-optimizer/
  SKILL.md                             Orchestrator (~155 lines)
  references/
    agent-prompts.md                   All 6 agent prompt templates
    implementation-playbook.md         Fix implementation details
    optimization-checklist.md          20 optimization techniques
    token-flow-architecture.md         How Claude Code loads tokens
  examples/
    claude-md-optimized.md             Optimized CLAUDE.md template
    claudeignore-template              .claudeignore starter
    hooks-starter.json                 Hook configuration example
scripts/measure.py                     Before/after measurement tool
install.sh                             One-command installer
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
