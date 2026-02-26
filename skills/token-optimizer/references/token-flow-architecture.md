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
|   (Source: Piebald-AI tracking, v2.1.59)            |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 2: MCP Tools (VARIABLE)                      |
|----------------------------------------------------|
| Tool Search (default since Jan 2026):                |
| - ToolSearch tool def           ~500 tokens          |
| - Deferred tool names           ~15 tokens each     |
| - Full definitions load on use only                  |
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
| Can't control, but .claudeignore helps              |
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
CLAUDE.md:               600 tokens
MEMORY.md:               400 tokens
Project CLAUDE.md:       200 tokens
System reminders:      1,000 tokens
---------------------------------
BASELINE:            ~20,700 tokens (10% of 200K)
```

### Unoptimized Setup (~25K baseline, Tool Search active)
```
Core system + tools: 15,000 tokens
MCP (ToolSearch +      1,700 tokens  (500 base + ~80 tools x 15)
  deferred names):
Skills (40):           4,000 tokens
Commands (20):         1,000 tokens
CLAUDE.md:             1,500 tokens
MEMORY.md:             1,200 tokens
Project CLAUDE.md:       200 tokens
System reminders:      2,000 tokens
---------------------------------
BASELINE:            ~26,600 tokens (13% of 200K)
```

**Difference**: ~5,900 tokens per message = 1.3x overhead
**Note**: Pre-Tool-Search (2025), MCP alone could add 40-80K tokens. Tool Search (default since Jan 2026) reduced this by ~85%.

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
| **System reminders** | Partial | Use .claudeignore to prevent file injection warnings. |
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
- **Use .claudeignore**: Prevents modified file warnings for ignored files
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
AUTO-COMPACT triggers at 75-83% fill (~150K-166K of 200K window)
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

**Recommendation**: Manually /compact at 70% to stay in peak zone.

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

**What we know** (as of early 2026):
- Claude caches prefixes >1024 tokens
- Requires 5+ min between uses of same prefix
- 90% cost reduction on cached portions
- Only works if prefix is IDENTICAL

**What's unclear in Claude Code**:
- Does it cache CLAUDE.md between messages? (Unknown)
- Does it cache system prompt? (Likely yes)
- Does it cache MCP tool definitions? (Unknown)
- Can we structure CLAUDE.md to maximize caching? (Unknown)

**Research status**: Ongoing. Community hasn't confirmed caching behavior in Claude Code specifically.

---

## Real Cost: Context Budget, Not Dollars

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
```
Opus input: $15 per 1M tokens
20K overhead x 100 msgs/day x 30 days = 60M tokens/mo = $900/mo overhead
35K overhead x 100 msgs/day x 30 days = 105M tokens/mo = $1,575/mo overhead
Savings from optimization: ~$675/mo
```

---

## Optimization Priority Matrix

| Component | Current Typical | Optimized Target | Impact x Effort |
|-----------|-----------------|------------------|-----------------|
| CLAUDE.md | 1,500 tokens | 600 tokens | HIGH x LOW |
| MEMORY.md | 1,400 tokens | 400 tokens | HIGH x LOW |
| Skills count | 4,000 tokens | 2,000 tokens | MEDIUM x MEDIUM |
| MCP deferred tools | 1,700 tokens | 1,000 tokens | LOW x MEDIUM |
| Commands | 1,000 tokens | 500 tokens | LOW x LOW |

**Start here**: CLAUDE.md + MEMORY.md (30 min effort, ~1,900 token savings)

---

## Real-World Example: Power User Setup (Tool Search Active)

**Before optimization**:
```
Core system + tools: 15,000 tokens (fixed, unavoidable)
MCP (ToolSearch +     1,700 tokens (Tool Search active, ~80 tools)
  deferred names):
Skills (~40):         4,000 tokens
Commands (~20):       1,000 tokens
CLAUDE.md:            1,500 tokens
MEMORY.md:            1,200 tokens
Project CLAUDE.md:      200 tokens
System reminders:     2,000 tokens
---------------------------------
BASELINE:           ~26,600 tokens (13% of 200K)
```

**After config optimization**:
```
Core system + tools: 15,000 tokens (fixed)
MCP (ToolSearch +     1,250 tokens (removed unused servers, ~50 tools)
  deferred names):
Skills (~25):         2,500 tokens (archived 15)
Commands (~10):         500 tokens (archived 10)
CLAUDE.md:              600 tokens (progressive disclosure)
MEMORY.md:              400 tokens (dedup'd with CLAUDE.md)
Project CLAUDE.md:      200 tokens (unchanged)
System reminders:     1,000 tokens (.claudeignore)
---------------------------------
BASELINE:           ~21,450 tokens (11% of 200K)

CONFIG SAVINGS: ~5,150 tokens/msg (19% reduction)
```

**Plus behavioral changes** (compound across every message):
- Agent model selection (haiku for data): 50-60% savings on automation
- /compact at 70%: 40-82% savings on long sessions
- Extended thinking awareness: variable, potentially largest factor
- Batching requests: 2-3x on multi-step tasks

**Config changes shrink overhead per message. Behavioral changes multiply across every session.**

---

## Further Reading

- **Official Docs**: https://docs.anthropic.com (prompt caching, context windows)
- **Tool Search**: Default since Jan 2026 (deferred tool loading, 85% MCP reduction)
- **Community**: r/ClaudeAI, r/anthropic (optimization tips)
