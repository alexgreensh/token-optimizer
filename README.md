# Token Optimizer

Your Claude Code setup is probably burning 2x the tokens it needs to. This finds out exactly where.

## The Problem

Every message you send to Claude Code loads your entire config stack: system prompt, MCP tools, skills, commands, CLAUDE.md, MEMORY.md, and system reminders. Power users hit 40K+ tokens per first message without knowing it. A well-optimized setup? ~20K. That's a 2x difference you're paying for every single message.

Nobody measures this. Nobody audits it. You just keep paying.

## The Fix

One command. Six agents audit your setup in parallel. You get a prioritized fix list with exact token savings.

```
/token-optimizer
```

That's it.

## What It Finds

| Area | What It Catches |
|------|----------------|
| CLAUDE.md | Bloated config, content that should be skills, duplication with MEMORY.md |
| MEMORY.md | Overlap with CLAUDE.md, verbose history that should be condensed |
| Skills | Duplicates, archived skills still loading, unused domain skills |
| MCP Servers | Broken auth, unused servers, duplicate tools eating 100 tokens each |
| Commands | Rarely-used commands, merge candidates |
| Advanced | Missing .claudeignore, no hooks, poor cache structure, no monitoring |

## Real Numbers

**Before** (typical power user):
```
Core system:      12,200 tokens  (fixed)
MCP tools (178):  17,800 tokens
Skills (54):       5,400 tokens
Commands (29):     1,450 tokens
CLAUDE.md:         1,500 tokens
MEMORY.md:         1,400 tokens
System reminders:  2,000 tokens
                  ──────────────
TOTAL:            ~42,000 tokens per message
```

**After**:
```
Core system:      12,200 tokens  (fixed)
MCP tools (178):  17,800 tokens  (deferred)
Skills (34):       3,400 tokens
Commands (9):        450 tokens
CLAUDE.md:           600 tokens
MEMORY.md:           400 tokens
System reminders:  1,000 tokens
                  ──────────────
TOTAL:            ~24,000 tokens per message
```

**43% reduction. Every message. Forever.**

Plus behavioral changes (model selection, compaction hygiene, batch requests) add another 30-50% on top.

Community data:
- One user saved $50 on a 3-day project just by compacting every 15 messages
- Boris Cherny's optimization tips (Claude Code creator) got 8.5M views on X and 1,545 upvotes on Reddit
- A cache-audit post got 109 upvotes in 6 days. People want this.

## How It Works

**5 phases, fully automated:**

1. **Initialize**: Creates a coordination folder, takes a "before" snapshot
2. **Audit**: 6 parallel agents (haiku, cheap) scan CLAUDE.md, MEMORY.md, skills, MCP, commands, and advanced config
3. **Analyze**: Synthesis agent (sonnet) prioritizes findings into Quick Wins / Medium / Deep tiers
4. **Implement**: You choose what to fix. It creates backups, shows diffs, asks before touching anything
5. **Verify**: Re-measures everything. Shows before/after with exact token and cost savings

The skill practices what it preaches: haiku for data gathering (5x cheaper than Opus), sonnet only for reasoning, session folder pattern to prevent context overflow.

## Measurement Tool

Standalone Python script for measuring token overhead without running the full audit:

```bash
# Full report of current state
python3 scripts/measure.py report

# Save snapshots for comparison
python3 scripts/measure.py snapshot before
# ... make changes ...
python3 scripts/measure.py snapshot after
python3 scripts/measure.py compare
```

Reads real token counts from Claude Code session logs (JSONL) and estimates overhead from file sizes.

## vs Alternatives

| Tool | What It Does | What It Doesn't |
|------|-------------|-----------------|
| **Doing nothing** | Free | You're overpaying 2x on every message |
| **Manual audit** | Flexible | Takes hours. No measurement. Easy to miss things. |
| **ccusage** (10.4K stars) | Monitors spending | Doesn't diagnose. Doesn't fix. |
| **token-optimizer-mcp** | Caches MCP calls | One dimension only |
| **cache-audit** (Reddit) | Checks cache structure | Does ONE thing our Phase 4G does |
| **This** | Audits, diagnoses, fixes, measures | Requires Claude Code (obviously) |

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/alexgreensh/token-optimizer/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
ln -s ~/.claude/token-optimizer/skills/token-optimizer ~/.claude/skills/token-optimizer
```

Then start a Claude Code session and run `/token-optimizer`.

## What's Inside

```
skills/token-optimizer/SKILL.md     # Slim orchestrator (~130 lines)
references/agent-prompts.md         # All agent prompt templates
references/implementation-playbook.md   # Fix implementation details
references/optimization-checklist.md    # 20 optimization techniques
references/token-flow-architecture.md   # How Claude Code loads tokens
scripts/measure.py                  # Before/after measurement tool
examples/                           # .claudeignore, hooks, CLAUDE.md templates
```

## Key Stats

- Skills are **98% cheaper** than CLAUDE.md for the same content (frontmatter-only loading)
- Haiku agents are **5x cheaper** than Opus for data gathering
- Each MCP deferred tool costs **~100 tokens** in the menu
- Each skill costs **~100 tokens** at startup (frontmatter)
- Each command costs **~50 tokens** at startup
- Prompt caching gives **90% cost reduction** on stable prefixes >1024 tokens
- Manual /compact at 70% saves **40-82%** vs waiting for auto-compact

## Research

Built from comprehensive research across 60+ web sources, 100+ GitHub repos, 50+ Reddit threads, and 40+ YouTube videos. Includes Boris Cherny's official optimization tips (Claude Code creator), Anthropic docs on prompt caching, and community-validated techniques.

Full technical docs in `references/token-flow-architecture.md`.

## License

AGPL-3.0. See [LICENSE](LICENSE).
