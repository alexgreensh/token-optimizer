<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center"><strong>Run <code>/context</code> on a fresh Claude Code session. See how much is already gone.<br>Find the ghost tokens, the invisible overhead, the context window tax. Get it back.</strong></p>

![Token Optimizer in action](skills/token-optimizer/assets/hero-terminal.svg)

## Install

### Plugin (recommended)

In Claude Code:

```
/plugin marketplace add alexgreensh/token-optimizer
/plugin install token-optimizer@alexgreensh-token-optimizer
```

Auto-updates when you restart Claude Code. Requires Claude Code 1.0.33+.

### Script installer

```bash
curl -fsSL https://raw.githubusercontent.com/alexgreensh/token-optimizer/main/install.sh | bash
```

Updates: `cd ~/.claude/token-optimizer && git pull`.

### Manual

```bash
git clone https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
ln -sf ~/.claude/token-optimizer/skills/token-optimizer ~/.claude/skills/token-optimizer
```

### Migrating from script to plugin

If you installed via the script and want to switch to the plugin (for auto-updates):

```bash
# Remove skill (handles both symlink and directory)
if [ -L ~/.claude/skills/token-optimizer ]; then
  rm -f ~/.claude/skills/token-optimizer
else
  rm -rf ~/.claude/skills/token-optimizer
fi
rm -rf ~/.claude/token-optimizer            # remove clone (optional)
```

Then install the plugin using the commands above.

---

Then start Claude Code and run:

```
/token-optimizer
```

## The Problem

Every message you send to Claude Code re-sends everything: system prompt, tool definitions, MCP servers, skills, commands, CLAUDE.md, MEMORY.md, and system reminders. The API is stateless. No memory between messages. The full stack, replayed every time. These are the ghost tokens: invisible overhead that eats your context window before you type a word.

Prompt caching makes this [cheap](https://code.claude.com/docs/en/costs) (90% cost reduction on cached tokens). But cheap doesn't mean small. Those tokens still fill your context window, count toward rate limits, and degrade output quality past 50-70% fill.

The more you've customized Claude Code, the worse it gets.

![Where your context window goes](skills/token-optimizer/assets/user-profiles.svg)

### Where it all goes

Your 200K context window gets eaten from multiple directions:

**Fixed overhead** (everyone pays, can't change): System prompt (~3K tokens) plus built-in tool definitions (12-17K tokens). About 8-10% of your window, gone before anything else loads. Common misconception: the "system prompt" is often reported as ~3K tokens. But built-in tools load alongside it every message. The real irreducible floor is ~15K, not ~3K. Posts quoting the base prompt alone understate overhead by 5x.

**Autocompact buffer**: When autocompact is on (the default), Claude Code reserves headroom for compaction. In practice, roughly 30-35K tokens (~16% of your window) sit empty. Run `/context` on a fresh session to see the exact number.

**MCP tools**: The biggest variable. Anthropic's own engineering team [measured 134K tokens consumed by tool definitions](https://www.anthropic.com/engineering/advanced-tool-use) before optimization. [Tool Search](https://www.anthropic.com/engineering/advanced-tool-use) (activates automatically when MCP tools exceed [~10% of context](https://code.claude.com/docs/en/costs)) reduced this by 85%, but MCP servers still add up: each deferred tool costs ~15 tokens, plus server instructions.

**Your config stack** (what this tool optimizes): CLAUDE.md that's grown organically. MEMORY.md that duplicates half of it. 50+ skills you installed and forgot. Commands you never use. [`@imports`](https://code.claude.com/docs/en/memory) pulling in files you didn't realize. [`.claude/rules/`](https://code.claude.com/docs/en/memory) adding up quietly. No `.claudeignore` to block system reminder injection.

A real power user's baseline overhead: **~43,000 tokens** (22% of the 200K window). Add the autocompact buffer and **~38% is unavailable** before you type a single word.

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

### Persistent Dashboard

The dashboard auto-regenerates after every session (via the SessionEnd hook). It shows Trends and Health tabs with your latest usage data. The full audit dashboard (with optimization recommendations) requires running `/token-optimizer`.

**Bookmarkable URL (macOS):**

```bash
python3 $MEASURE_PY setup-daemon
# => http://localhost:24842/
```

This installs a tiny background server (~2MB memory) that starts when you log in. Bookmark the URL and check your dashboard anytime. It always shows the latest data because the SessionEnd hook regenerates the file after every session.

Only accessible from your machine (localhost). Uninstall: `python3 $MEASURE_PY setup-daemon --uninstall`

**Other options:**

```bash
# Open the file directly (no server needed)
open ~/.claude/_backups/token-optimizer/dashboard.html

# Serve over HTTP (headless/remote machines)
python3 $MEASURE_PY dashboard --serve

# Remote access via SSH tunnel
ssh -L 8080:localhost:8080 your-server
# Then open http://localhost:8080/dashboard.html locally
```

To regenerate manually: `python3 $MEASURE_PY dashboard`.

**Full audit dashboard** (after running `/token-optimizer`):

```bash
python3 $MEASURE_PY dashboard --coord-path PATH --serve
```

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

- **You hit compaction sooner.** Compaction is lossy. Every cycle throws away context.
- **Rate limits burn faster.** Cache reads still count toward your subscription quota.
- **Quality degrades.** Performance drops as context fills, especially past 70%.
- **Agents multiply it.** Every subagent loads its own copy of your full config stack. Dispatch 5 agents and that overhead loads 5 times, each in a fresh 200K window. [Agent teams use ~7x more tokens in plan mode](https://code.claude.com/docs/en/costs) than standard sessions. Reducing per-agent overhead from 43K to 28K saves 75K tokens across those 5 agents.

## Measurement Tool

Standalone script. No dependencies. Python 3.8+.

The path depends on how you installed. Set it once:

```bash
# Auto-detect (works for both plugin and script/manual installs):
if [ -f ~/.claude/skills/token-optimizer/scripts/measure.py ]; then
  MEASURE_PY=~/.claude/skills/token-optimizer/scripts/measure.py
else
  MEASURE_PY=$(find ~/.claude/plugins/cache -path "*/token-optimizer/scripts/measure.py" 2>/dev/null | head -1)
fi
[ -z "$MEASURE_PY" ] || [ ! -f "$MEASURE_PY" ] && { echo "measure.py not found. Is Token Optimizer installed?"; exit 1; }
```

```bash
python3 $MEASURE_PY report

# Save snapshots for before/after comparison
python3 $MEASURE_PY snapshot before
# ... make changes ...
python3 $MEASURE_PY snapshot after
python3 $MEASURE_PY compare
```

## Usage Analytics

The optimizer doesn't just audit your config once. It tracks how you actually use Claude Code over time, so you can spot patterns, catch waste, and make informed decisions about what to keep and what to archive.

Two commands power this: `trends` for usage patterns and `health` for session hygiene. Both work from the CLI and appear as interactive tabs in the persistent dashboard (auto-refreshed after every session) and in the full audit dashboard.

### Automatic Collection

Add a SessionEnd hook and usage data collects itself. The setup command auto-detects measure.py's path regardless of install method:

```bash
python3 $MEASURE_PY setup-hook --dry-run   # preview the change
python3 $MEASURE_PY setup-hook             # install it
```

Or add manually to `~/.claude/settings.json` (adjust the path to match your install):

```json
{
  "hooks": {
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/measure.py collect --quiet && python3 /path/to/measure.py dashboard --quiet"
      }]
    }]
  }
}
```

Every session end: collects the JSONL log into a local SQLite database (`~/.claude/_backups/token-optimizer/trends.db`), then regenerates the persistent dashboard. No external services. No API calls. Your data stays on your machine.

You can also collect manually or backfill older sessions:

```bash
# Collect last 90 days of sessions (default)
python3 $MEASURE_PY collect

# Backfill a longer history
python3 $MEASURE_PY collect --days 180
```

Collection is idempotent. Running it twice on the same sessions won't double-count anything.

### Usage Trends

```bash
python3 $MEASURE_PY trends
python3 $MEASURE_PY trends --days 7
python3 $MEASURE_PY trends --json
```

Scans your session history and shows:

**Skills usage**: Which skills you actually invoke vs. which sit idle loading frontmatter every session. This is the most actionable insight. If you have 59 skills installed but only use 8 in the last 30 days, that's 51 skills costing ~100 tokens each, every session, for nothing.

**Model mix**: Your opus/sonnet/haiku split across all sessions. If you see 90% opus, you're probably overspending on data-gathering agents that would work fine on haiku.

**Daily breakdown**: Per-day session count, token volume, and which skills were used. In the dashboard, each day expands to show individual sessions with duration, message count, cache hit rate, and skills used.

```
USAGE TRENDS (last 30 days)
  Sessions: 70 | Avg duration: 340 min

SKILLS
  Used (8 of 59 installed):
    morning .................. 28 sessions
    evening-auto ............. 25 sessions
    recall ................... 12 sessions

  Never used (last 30 days):
    api-docs, condition-based-waiting, ...
    (51 skills, ~5,100 tokens overhead)

MODEL MIX
  sonnet ████████████████████░░░░░ 63%  3.4M tokens
  opus   ████████████░░░░░░░░░░░░░ 22%  1.2M tokens
  haiku  ███████░░░░░░░░░░░░░░░░░░ 15%  800K tokens
```

### Clickable Skill Details

In the dashboard, every skill listed in trends is clickable. Click a skill name and it expands to show:

- **Description**: What the skill does (from SKILL.md frontmatter)
- **Frontmatter tokens**: How much it costs per session just sitting in the menu
- **File structure**: What files the skill contains (SKILL.md, references/, scripts/, etc.)

Never-used skills link directly to the Quick Wins tab so you can archive them in one step.

### Session Health

```bash
python3 $MEASURE_PY health
```

Detects running Claude Code processes and flags problems:

- **Stale sessions** (24h+): Still running but probably forgotten. Long sessions accumulate context bloat.
- **Zombie sessions** (48h+): Almost certainly orphaned. Safe to kill.
- **Outdated versions**: Running an older Claude Code version than what's installed. Restart to get fixes.
- **Automated processes**: Lists any launchd/cron jobs running Claude.

```
SESSION HEALTH CHECK
  Installed version: 2.1.63

RUNNING SESSIONS (2)
  PID 521     (2d 8h ago)  v2.1.62  OUTDATED  ZOMBIE
  PID 91719   (1d 2h ago)  v2.1.63  STALE

RECOMMENDATIONS
  - 1 session running older version. Restart to get latest fixes.
  - 2 sessions running 24+ hours. Check if still needed.
```

### Dashboard Analytics Tabs

Trends and health appear as dedicated tabs in both the persistent dashboard (`measure.py dashboard`) and the full audit dashboard (`measure.py dashboard --coord-path PATH`). The Trends tab includes:

- Date range selector (7/14/30 days + calendar date picker)
- Interactive daily breakdown table (click a day to expand individual sessions)
- Skills usage bars with clickable detail panels
- Model mix visualization with cost-saving context

The persistent dashboard defaults to the Trends tab and hides empty audit sections. The full audit dashboard shows all tabs. The right panel collapses on analytics tabs since they're informational, giving the data more room.

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
    dashboard.html                     Interactive dashboard (optimization + analytics)
    dashboard-overview.png             Dashboard screenshot
    logo.svg                           Animated ASCII logo
    hero-terminal.svg                  Terminal demo
    before-after.svg                   Token breakdown comparison
    how-it-works.svg                   5-phase flow diagram
    user-profiles.svg                  Context usage by setup type
  references/
    agent-prompts.md                   8 agent prompt templates
    implementation-playbook.md         Fix implementation details (4A-4L)
    optimization-checklist.md          30 optimization techniques
    token-flow-architecture.md         How Claude Code loads tokens
  examples/
    claude-md-optimized.md             Optimized CLAUDE.md template
    claudeignore-template              .claudeignore starter
    hooks-starter.json                 Hook configuration example
  scripts/
    measure.py                         Measurement, trends, health & collection tool
install.sh                             One-command installer
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
