# Token Flow Architecture: How Claude Code Loads Context

Understanding how tokens flow through Claude Code is critical for optimization. This document maps the complete loading sequence.

---

## The Loading Sequence (Every Message)

When you send a message to Claude Code, this is what loads:

```
MESSAGE SEND
    |
+-----------------------------------------------------+
| PHASE 1: Core System (FIXED, ~15,000 tokens)       |
|----------------------------------------------------|
| - System prompt base           ~3,000 tokens        |
| - Built-in tools (18+)       ~12,000 tokens         |
|   Read, Write, Edit, Bash, Grep, Glob, Task, etc.  |
|   (Source: /context output, Claude Code v2.1.59)     |
|                                                     |
| NOTE: The "system prompt" is often reported as      |
| ~3,000 tokens. But built-in tool definitions        |
| (~12,000 tokens) load alongside it every message.   |
| The real fixed floor is ~15,000, not ~3,000.        |
| Posts quoting the base prompt alone understate       |
| overhead by 5x.                                     |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 2: MCP Tools (VARIABLE)                      |
|----------------------------------------------------|
| Tool Search (default since Jan 2026):                |
| - ToolSearch tool def           ~500 tokens          |
| - Deferred tool names           ~15 tokens each     |
| - Full definitions load on use only                  |
| - Auto-triggers when MCP tools exceed 10% of context |
| - 85% reduction vs pre-Tool-Search (Anthropic data) |
|                                                     |
| WITHOUT Tool Search (old versions, <10K threshold): |
| - Full definitions upfront     ~300-850 tokens each  |
| - 50 tools = ~25,000-42,500 tokens                   |
|                                                     |
| WITH Tool Search (current default):                  |
| - 50 deferred tools = ~1,250 tokens                  |
| - 100 deferred tools = ~2,000 tokens                 |
| - 178 deferred tools = ~3,170 tokens                 |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 3: Skills & Commands (VARIABLE)              |
|----------------------------------------------------|
| - Skills: frontmatter only     ~100 tokens each     |
|   (full SKILL.md loads on invoke)                   |
| - Commands: frontmatter only   ~50 tokens each      |
|                                                     |
| Example:                                            |
| - 54 skills = ~5,400 tokens                         |
| - 29 commands = ~1,450 tokens                       |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 4: User Configuration (VARIABLE)             |
|----------------------------------------------------|
| ALWAYS LOADED (every message):                      |
| - ~/.claude/CLAUDE.md          ~800-2,000 tokens    |
| - ~/.claude/projects/.../      ~600-1,400 tokens    |
|   MEMORY.md                                         |
| - [repo]/CLAUDE.md             ~10-500 tokens       |
|                                                     |
| ** OPTIMIZATION TARGET: These load EVERY message    |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 5: System Reminders (AUTO-INJECTED)          |
|----------------------------------------------------|
| - Modified files warning       ~500-3,000 tokens    |
| - Budget warnings              ~100 tokens          |
| - Tool-specific reminders      Variable             |
|                                                     |
| Can't control, but permissions.deny rules help      |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 6: Conversation History                      |
|----------------------------------------------------|
| - Your message                 Variable             |
| - Previous messages            Variable             |
|   (up to context limit)                             |
+-----------------------------------------------------+
    |
CLAUDE PROCESSES
    |
RESPONSE GENERATED
```

---

## Token Budget Breakdown (Typical Setup)

### Well-Optimized Setup (~20K baseline, Tool Search active)
```
Core system + tools: 15,000 tokens
MCP (ToolSearch +      1,000 tokens  (500 base + ~30 tools x 15)
  deferred names):
Skills (20):           2,000 tokens
Commands (10):           500 tokens
CLAUDE.md:               800 tokens
MEMORY.md:               400 tokens
System reminders:      1,000 tokens
---------------------------------
BASELINE:            ~20,700 tokens (10% of 200K)
```

### Unaudited Setup (~43K consumed, Tool Search active)
```
Core system + tools: 15,000 tokens
MCP tools:            9,000 tokens  (deferred tools + server instructions)
Skills (60):          6,000 tokens
Commands (60):        3,000 tokens
CLAUDE.md:            3,500 tokens  (250 lines. A 700-line file = 12K)
MEMORY.md:            3,500 tokens
System reminders:     3,000 tokens  (no permissions.deny rules)
---------------------------------
CONSUMED:            ~43,000 tokens (22% of 200K)
+ Autocompact buffer: 33,000 tokens (16.5%, reserved not consumed)
= UNAVAILABLE:       ~76,000 tokens (38% of 200K)
```

**Difference**: ~22,300 tokens consumed per message = 2.1x overhead vs optimized
**Total unavailable difference**: ~56,000 tokens (38% vs 10% for optimized with autocompact off)
**Note**: Pre-Tool-Search (2025), MCP alone could add 40-80K tokens. Tool Search (default since Jan 2026) reduced this by ~85%. This "unaudited" baseline is a power user who has been adding to their config for 3+ months without auditing. The autocompact buffer (33K) is reserved on every fresh session when autocompact is enabled (the default).

---

## What You Can Control (Optimization Targets)

### HIGH IMPACT (Always Loaded)

| Component | Control Level | Optimization Method |
|-----------|---------------|---------------------|
| **CLAUDE.md** | Full | Slim to <800 tokens. Move content to skills. Apply tiered architecture. |
| **MEMORY.md** | Full | Remove duplication with CLAUDE.md. Condense verbose sections. |
| **Project CLAUDE.md** | Full | Keep project-specific only. No duplication with global. |

### MEDIUM IMPACT (Menu Overhead)

| Component | Control Level | Optimization Method |
|-----------|---------------|---------------------|
| **Skills count** | Full | Archive unused skills. Merge duplicates. |
| **Commands count** | Full | Archive unused commands. Merge similar ones. |
| **MCP servers** | Full | Disable broken/unused servers. Tool Search already defers definitions. |

### LOW IMPACT (Can't Control Directly)

| Component | Control Level | Optimization Method |
|-----------|---------------|---------------------|
| **Core system** | None | Fixed by Claude Code. Accept it. |
| **System reminders** | Partial | Use `permissions.deny` rules to exclude files from context. |
| **Tool definitions** | Partial | Tool Search defers most. Can't reduce further without disabling tools. |

---

## Progressive Loading (How Skills/Commands Work)

### Skills
```
AT STARTUP (always loaded):
---
name: morning
description: "Your daily briefing..."
---
(~100 tokens for frontmatter)

WHEN INVOKED (/morning):
[Full SKILL.md content loads]
[Reference files load if Read calls made]
(+5,000-20,000 tokens depending on skill)
```

**Implication**: Skills are 98% cheaper than CLAUDE.md for same content.

### Commands
```
AT STARTUP (always loaded):
Namespace listing + description
(~50 tokens per command)

WHEN INVOKED (/my-command):
[Command executes, may load files]
(Variable additional tokens)
```

---

## The Hidden Tax: System Reminders

System reminders are auto-injected by Claude Code when certain conditions occur:

### When They Trigger
| Condition | Reminder | Token Cost |
|-----------|----------|------------|
| You edited a file | "File was modified" warning | ~500-2,000 |
| Approaching budget | Budget warning | ~100 |
| Reading malware-like code | Security warning | ~200 |
| Tool-specific context | Tool guidance | ~100-500 |

### How to Reduce
- **Use `permissions.deny`**: Exclude files from discovery and read operations
- **Don't edit unnecessary files**: Each edit = potential injection
- **Be aware**: You can't disable these entirely, but you can avoid triggering them

---

## Subagent Context Inheritance (CRITICAL)

When you dispatch a subagent via the Task tool, it inherits the FULL system prompt.

```
Main Session Context: 30,000 tokens
    |
Task(description="Research agent")
    |
Subagent receives:
    - Full core system (~15,000 tokens)
    - Full MCP tools (all deferred tool listings)
    - Full skills/commands frontmatter
    - Full CLAUDE.md
    - Full MEMORY.md
    - Task description
    ---------------------------------
    TOTAL: ~30,000+ tokens BEFORE doing any work
```

**Anthropic's own data**: Agent teams use approximately 7x more tokens than standard sessions (source: code.claude.com/docs/en/costs). Subagents automatically load "CLAUDE.md, MCP servers, and skills" (direct quote from Anthropic docs), confirming full system prompt inheritance.

**Implication**: If you dispatch 5 subagents in a single session:
- Each inherits ~30K tokens
- 5 x 30K = 150K tokens just for setup
- This is BEFORE they read any files or do work

**Optimization**: Session folder pattern
- Subagents write findings to files
- Orchestrator never reads full outputs
- Synthesis agent reads files directly
- Prevents orchestrator context overflow

---

## Context Window Lifecycle

```
SESSION START
    |
Message 1: 20,000 tokens baseline + 1,000 message = 21,000 total
    |
Message 2: 21,000 previous + new message + response = ~35,000 total
    |
Message 3: 35,000 previous + new message + response = ~50,000 total
    |
...context grows...
    |
AUTO-COMPACT triggers near context limits. Often too late for best quality
    |
Context compressed (lossy)
    |
Continue until /clear or session end
```

### Context Fill Degradation
| Fill Level | Quality Impact |
|------------|----------------|
| 0-30% | Peak performance |
| 30-50% | Normal operation |
| 50-70% | Minor degradation (subtle) |
| 70-85% | Noticeable cutting corners |
| 85%+ | Hallucinations, drift, forgetfulness |

**Recommendation**: Manually /compact at 50-70% to stay in peak zone. Auto-compact triggers near context limits, past the quality degradation threshold. Set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70` to auto-compact earlier.

---

## The 1,000 Token Rule

**Rule of thumb from research**:
- 1 line of prose ~ 15 tokens
- 1 line of YAML/lists ~ 8 tokens
- 1 line of code ~ 10 tokens

**Examples**:
- 50-line CLAUDE.md prose section = ~750 tokens
- 100-line skill frontmatter (YAML) = ~800 tokens
- 200-line Python file = ~2,000 tokens

**Use for estimation**: "This section is 40 lines of prose, so ~600 tokens. Worth it?"

---

## Caching Behavior (Prompt Caching)

**Confirmed active in Claude Code** (as of Feb 2026):
- Prompt caching is ON by default. Disable with `DISABLE_PROMPT_CACHING=1`.
- Anthropic internal data: 96-97% cache hit rate in active sessions
- The team treats cache rate like uptime and declares incidents when it drops
- Cache order: tools first, then system prompt, then messages (chronological)

**Pricing**:
- Cache reads: 90% cheaper than base input ($0.30/M vs $3.00/M for Sonnet)
- Cache writes: 25% surcharge (5-min TTL) or 100% surcharge (1-hour TTL for Max plan)
- TTL: 5 minutes for Pro/API, 1 hour for Max plan. Timer resets with each active message.
- Minimum cacheable size: 1,024 tokens (Sonnet/Opus), 2,048 tokens (Haiku)

**What gets cached**: System prompt (including CLAUDE.md), tool definitions, conversation history prefix up to last cache breakpoint.

**What breaks the cache** (critical, avoid mid-session):
- Adding/removing an MCP tool ("all 18K+ tokens after that have to be reprocessed")
- Switching models mid-session ("caches are per model")
- Editing CLAUDE.md mid-session
- Timestamps or dynamic content in system prompt
- Any change to content before a cache breakpoint

**What caching does NOT fix** (why optimization still matters):
- Context window SIZE: cached tokens still occupy your window
- Rate limits: cache reads count toward subscription usage quotas
- Quality: lost-in-the-middle degradation starts at 50-70% fill regardless of caching
- Multi-agent amplification: each subagent inherits full overhead at full size

**Structuring for cache hits**: Stable sections first (identity, rules), volatile sections last. This maximizes the cached prefix length.

---

## Real Cost: Context Budget, Not Dollars

**Official Anthropic data** (code.claude.com/docs/en/costs): Average cost is ~$6/dev/day ($100-200/mo with Sonnet). Background token usage (hooks, auto-memory) typically under $0.04/session.

Most Claude Code users are on Max subscriptions ($100-200/month), not per-token API pricing. The real cost of overhead is not dollars. It is context budget:

### Why Overhead Hurts (Even on Subscription)
```
1. FASTER CONTEXT FILL
   20K overhead = 10% of context gone before you type
   35K overhead = 18% gone. You hit compaction 18% sooner.

2. MORE COMPACTION CYCLES
   Each compaction is lossy. More compactions = more context lost.
   A session with 35K overhead compacts ~2x more often than 20K.

3. QUALITY DEGRADATION
   Claude's performance degrades as context fills:
   0-50%:  Peak performance
   50-70%: Minor degradation
   70%+:   Noticeable quality loss, cutting corners
   With 35K overhead, you reach 70% after fewer messages.

4. BEHAVIORAL MULTIPLIER
   Every message re-sends the overhead. 100 messages/day
   at 35K overhead = 3.5M tokens of overhead alone.
   At 20K overhead = 2.0M tokens. That's 1.5M tokens freed
   for actual work content.
```

### For API Users (Per-Token Pricing)

**Without caching** (worst case, e.g. cache misses from inactivity):
```
Opus input: $15 per 1M tokens
20K overhead x 100 msgs/day x 30 days = 60M tokens/mo = $900/mo overhead
35K overhead x 100 msgs/day x 30 days = 105M tokens/mo = $1,575/mo overhead
Savings from optimization: ~$675/mo
```

**With caching** (typical, 96-97% cache hit rate):
```
Cached reads cost 10% of base input price.

Opus cached input: $1.50/1M (10% of $15)
  20K overhead: ~$0.03/msg | 35K overhead: ~$0.05/msg
  Savings from optimization: ~$68/mo

Sonnet cached input: $0.30/1M (10% of $3)
  20K overhead: ~$0.006/msg | 35K overhead: ~$0.011/msg
  Savings from optimization: ~$14/mo
```

**The honest framing**: For subscription users (Max, Pro), dollar cost is irrelevant. The real impact is context window space, rate limit quota burn, and quality degradation from fuller context.

---

## Model Cost Comparison (Why Routing Matters)

Routing subagents to the right model tier is the highest-ROI behavioral change. The cost differentials are massive:

| Model | Input $/1M | Output $/1M | Relative Cost (vs Haiku) |
|-------|-----------|-------------|--------------------------|
| Haiku | $0.25 | $1.25 | 1x |
| Sonnet | $3.00 | $15.00 | 12x input, 12x output |
| Opus | $15.00 | $75.00 | 60x input, 60x output |

*Pricing from anthropic.com/pricing. Check for current rates.*

**Worked example**: 5-agent workflow (file scanning + analysis + synthesis):
- **All Opus**: 5 agents x (30K input x $15/1M + 5K output x $75/1M) = ~$4.13
- **Routed** (3 Haiku + 1 Sonnet + 1 Opus): ~$1.04
- **Savings: ~75%**

For subscription users (Max plan): model routing affects rate limits, not dollars. Haiku calls consume fewer quota units and return results 3-5x faster. Routing means your session stays under rate limits longer.

**What routing does NOT save**: Context window space. Subagents inherit the full system prompt regardless of model. A Haiku agent gets the same ~30K token overhead as an Opus agent. Routing saves dollars and rate limits. Config optimizations (CLAUDE.md slimming, skill archival) save context window space.

See `optimization-checklist.md` for the full Model Routing Strategy section with task-to-model mapping and a ready-to-paste CLAUDE.md snippet.

---

## Optimization Priority Matrix

| Component | Unaudited Typical | Optimized Target | Savings | Impact x Effort |
|-----------|-------------------|------------------|---------|-----------------|
| MCP tools | 9,000 tokens | 6,000 tokens | -3,000 | HIGH x MEDIUM |
| CLAUDE.md | 3,500 tokens | 800 tokens | -2,700 | HIGH x LOW |
| Skills (60 -> 30) | 6,000 tokens | 3,000 tokens | -3,000 | MEDIUM x MEDIUM |
| MEMORY.md | 3,500 tokens | 1,000 tokens | -2,500 | HIGH x LOW |
| System reminders | 3,000 tokens | 1,000 tokens | -2,000 | MEDIUM x LOW |
| Commands (60 -> 25) | 3,000 tokens | 1,200 tokens | -1,800 | LOW x LOW |

**Start here**: CLAUDE.md + MEMORY.md (30 min effort, ~5,200 token savings)

---

## Real-World Example: Unaudited Power User (Tool Search Active)

**Before optimization** (typical after 3+ months of use):
```
Core system + tools: 15,000 tokens (fixed, unavoidable)
MCP tools:            9,000 tokens (deferred tools + server instructions)
Skills (~60):         6,000 tokens
Commands (~60):       3,000 tokens
CLAUDE.md:            3,500 tokens (grown organically, never trimmed)
MEMORY.md:            3,500 tokens (duplicates CLAUDE.md content)
System reminders:     3,000 tokens (no permissions.deny rules)
---------------------------------
CONSUMED:           ~43,000 tokens (22% of 200K)
+ Autocompact buffer: 33,000 tokens (16.5%, reserved)
= UNAVAILABLE:      ~76,000 tokens (38% of 200K)
```

**After config optimization**:
```
Core system + tools: 15,000 tokens (fixed)
MCP tools:            6,000 tokens (pruned unused servers)
Skills (~30):         3,000 tokens (archived 30)
Commands (~25):       1,200 tokens (archived 35)
CLAUDE.md:              800 tokens (progressive disclosure)
MEMORY.md:            1,000 tokens (dedup'd with CLAUDE.md)
System reminders:     1,000 tokens (permissions.deny)
---------------------------------
CONSUMED:           ~28,000 tokens (14% of 200K)
+ Autocompact buffer: 33,000 tokens (16.5%, reserved)
= UNAVAILABLE:      ~61,000 tokens (30% of 200K)

CONFIG SAVINGS: ~15,000 tokens/msg (35% reduction in consumed overhead)
CONTEXT RECOVERED: 38% -> 30% unavailable (8% of window freed)
```

**At 100 messages/day, that's 1.5M tokens of overhead saved daily.**

**With autocompact OFF** (advanced users who manage /compact manually):
```
CONSUMED:           ~28,000 tokens (14% of 200K)
+ No buffer:               0 tokens
= UNAVAILABLE:      ~28,000 tokens (14% of 200K)

TOTAL RECOVERY vs unoptimized with buffer: 38% -> 14% = 24% of context freed
```

Prompt caching means the dollar savings are modest (cached tokens cost 10% of base). But the context window space savings are real: you hit compaction later, quality stays higher longer, and each subagent inherits 15,000 fewer tokens of overhead.

**Plus behavioral changes** (compound across every message):
- Agent model selection (haiku for data): 50-75% savings on automation
- /compact at 50-70%: up to 18x reduction in conversation history
- Extended thinking awareness: variable, potentially largest factor
- Batching requests: 2-3x on multi-step tasks

**Config changes shrink overhead per message. Behavioral changes multiply across every session.**

---

## Additional Config Features (Token Impact)

### `.claude/rules/` Directory (Path-Scoped Rules)

Rules files in `.claude/rules/*.md` support `paths:` frontmatter for directory-scoped loading. Each rule file loads similarly to CLAUDE.md content (~15 tokens/line of prose).

```
.claude/rules/
  backend.md          # paths: ["src/backend/**"]
  frontend.md         # paths: ["src/frontend/**"]
  testing.md          # paths: ["tests/**"]
  general.md          # no paths = always loaded
```

**Token impact**: Rules without `paths:` frontmatter load every message (same as CLAUDE.md). Rules with `paths:` load only when working in matching directories. Measure total rules content with `measure.py report`.

**Optimization**: Audit `.claude/rules/` for stale rules, duplicates, and rules that should have path scoping but don't.

### `CLAUDE.local.md` (Project-Local, Gitignored)

A gitignored version of project CLAUDE.md. Always loaded alongside CLAUDE.md when present. Used for local overrides, personal preferences, or environment-specific config that shouldn't be committed.

**Token impact**: Adds to CLAUDE.md overhead every message. Must be audited alongside CLAUDE.md.

### `.claude/settings.local.json` (Local Settings Overlay)

Local settings file that overlays `.claude/settings.json`. Can contain env var overrides, permission changes. Not committed to git.

**Token impact**: Indirect. Can override env vars like `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `MAX_THINKING_TOKENS`, etc. The optimizer should check for its existence and report any token-relevant overrides.

### `@imports` in CLAUDE.md

CLAUDE.md supports `@path/to/file.md` imports that pull external file content into the always-loaded context. This can silently add thousands of tokens.

```markdown
# My CLAUDE.md
@docs/coding-standards.md
@docs/api-reference.md
```

**Token impact**: Each imported file's full content loads every message. A 200-line coding standards doc = ~3,000 tokens added silently.

**Optimization**: Grep CLAUDE.md for `@` patterns. Resolve paths. Estimate total imported content. Move large imports to skills or reference files.

### `disable-model-invocation: true` in Skill Frontmatter

Skills can set `disable-model-invocation: true` to prevent being invoked by the model (only user can invoke). This doesn't change token cost but is useful context: skills without this flag can be auto-invoked, which triggers full skill content loading.

### Compact Instructions Section in CLAUDE.md

CLAUDE.md can include a section that guides what gets preserved during context compaction. This influences what survives /compact and auto-compact.

```markdown
## Compact Instructions
When compacting this conversation, always preserve:
- Current task context and progress
- File paths being modified
- Test results and error messages
```

**Token impact**: Small (the section itself is ~50-100 tokens). But the behavioral impact is significant: it controls what survives compaction, affecting quality of continued sessions.

### `/rewind` Command

Targeted compaction alternative. Instead of full /compact (which summarizes everything), /rewind removes specific recent turns. Better for "that didn't work, let me try again" situations.

**Token impact**: More precise context management. Less lossy than full /compact.

### Context Loading Hierarchy (13 Levels)

Full priority order for what Claude Code loads:

```
1.  Core system prompt (fixed)
2.  Built-in tool definitions (fixed)
3.  MCP tool definitions (deferred via Tool Search)
4.  MCP server instructions
5.  Plugin-bundled skills/commands
6.  User skills frontmatter (~/.claude/skills/)
7.  User commands frontmatter (~/.claude/commands/)
8.  Global CLAUDE.md (~/.claude/CLAUDE.md)
9.  Project CLAUDE.md ([repo]/CLAUDE.md)
10. CLAUDE.local.md ([repo]/CLAUDE.local.md)
11. .claude/rules/*.md (path-matched rules)
12. MEMORY.md (~/.claude/projects/.../memory/MEMORY.md)
13. System reminders (auto-injected)
```

Items 1-5 are largely fixed or deferred. Items 6-12 are your optimization targets. Item 13 is partially controlled via `permissions.deny` rules.

### Settings.json Environment Variables (Token-Relevant)

The `env` block in `~/.claude/settings.json` can set several token-relevant variables:

```json
{
  "env": {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
    "CLAUDE_CODE_MAX_THINKING_TOKENS": "10000",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384",
    "MAX_MCP_OUTPUT_TOKENS": "25000",
    "ENABLE_TOOL_SEARCH": "auto"
  }
}
```

See `optimization-checklist.md` items 23-30 for what each does and how the optimizer audits them.

---

## Further Reading

- **Official Docs**: https://docs.anthropic.com (prompt caching, context windows)
- **Official Costs**: https://code.claude.com/docs/en/costs ($6/dev/day average, 7x agent multiplier, background overhead data)
- **Tool Search**: Default since Jan 2026 (deferred tool loading, 85% MCP reduction)
- **Piebald-AI/claude-code-system-prompts** (3.9K stars): Most comprehensive open-source tracking of Claude Code's actual prompt content. 110+ prompt strings tracked. Useful for verifying system prompt sizes and tool definition token counts.
- **Community**: r/ClaudeAI, r/anthropic (optimization tips)
- **Reddit validation**: r/ClaudeAI thread confirming "65K tokens with all features enabled, ~12K with every feature disabled" (corroborates our 15K-43K range)
- **Taras Tsugrii analysis**: "Claude Code uses 3x more tokens than Codex" (competitive context for token efficiency)
