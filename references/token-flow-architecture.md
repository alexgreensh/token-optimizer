# Token Flow Architecture: How Claude Code Loads Context

Understanding how tokens flow through Claude Code is critical for optimization. This document maps the complete loading sequence.

---

## The Loading Sequence (Every Message)

When you send a message to Claude Code, this is what loads:

```
MESSAGE SEND
    |
+-----------------------------------------------------+
| PHASE 1: Core System (FIXED, ~12,200 tokens)       |
|----------------------------------------------------|
| - System prompt base           ~2,800 tokens        |
| - Built-in tools (18)          ~9,400 tokens        |
|   Read, Write, Edit, Bash, Grep, Glob, etc.        |
+-----------------------------------------------------+
    |
+-----------------------------------------------------+
| PHASE 2: MCP Tools (VARIABLE)                      |
|----------------------------------------------------|
| Tool Search Mode (v2.1.7+):                         |
| - Deferred tool listings       ~100 tokens each     |
| - Full definitions load on use                      |
|                                                     |
| Example:                                            |
| - 50 deferred tools = ~5,000 tokens                 |
| - 178 deferred tools = ~17,800 tokens               |
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

### Well-Optimized Setup (~15-20K baseline)
```
Core system:        12,200 tokens
MCP tools (30):      3,000 tokens
Skills (20):         2,000 tokens
Commands (10):         500 tokens
CLAUDE.md:             600 tokens
MEMORY.md:             400 tokens
Project CLAUDE.md:     200 tokens
System reminders:    1,000 tokens
---------------------------------
BASELINE:          ~19,900 tokens

First message:       1,000 tokens
---------------------------------
TOTAL:             ~20,900 tokens
```

### Unoptimized Setup (~30-35K baseline)
```
Core system:        12,200 tokens
MCP tools (178):    17,800 tokens
Skills (54):         5,400 tokens
Commands (29):       1,450 tokens
CLAUDE.md:           1,500 tokens
MEMORY.md:           1,400 tokens
Project CLAUDE.md:     100 tokens
System reminders:    2,000 tokens
---------------------------------
BASELINE:          ~41,850 tokens

First message:       1,000 tokens
---------------------------------
TOTAL:             ~42,850 tokens
```

**Difference**: 21,950 tokens per message = 2.1x overhead

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
    - Full core system (~12,200 tokens)
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

## Token Math: Cost Impact

### Per-Message Overhead
```
Optimized (20K baseline):
  20,000 tokens x $0.015 per 1M input tokens (Opus) = $0.0003/message

Unoptimized (42K baseline):
  42,000 tokens x $0.015 per 1M input tokens (Opus) = $0.00063/message

Difference: $0.00033/message
```

### Monthly Impact (100 messages/day)
```
Optimized:   100 x 30 = 3,000 messages/mo x $0.0003 = $0.90/mo
Unoptimized: 100 x 30 = 3,000 messages/mo x $0.00063 = $1.89/mo

Monthly savings: $0.99/mo (52% reduction)
```

**Note**: This is INPUT tokens only. Output tokens add to cost. For heavy users (1,000 msg/day), savings = ~$10-20/mo.

---

## Optimization Priority Matrix

| Component | Current Typical | Optimized Target | Impact x Effort |
|-----------|-----------------|------------------|-----------------|
| CLAUDE.md | 1,500 tokens | 600 tokens | HIGH x LOW |
| MEMORY.md | 1,400 tokens | 400 tokens | HIGH x LOW |
| MCP servers | 17,800 tokens | 5,000 tokens | HIGH x MEDIUM |
| Skills count | 5,400 tokens | 2,000 tokens | MEDIUM x MEDIUM |
| Commands | 1,450 tokens | 500 tokens | LOW x LOW |

**Start here**: CLAUDE.md + MEMORY.md (30 min effort, ~1,900 token savings)

---

## Real-World Example: Power User Setup

**Before optimization**:
```
Core system:        12,200 tokens
MCP tools (178):    17,800 tokens
Skills (54):         5,400 tokens
Commands (80+):      2,800 tokens
CLAUDE.md:           1,500 tokens
MEMORY.md:           1,400 tokens
Project CLAUDE.md:     100 tokens
System reminders:    2,000 tokens
---------------------------------
BASELINE:          ~43,200 tokens
```

**After optimization**:
```
Core system:        12,200 tokens (can't change)
MCP tools (178):    17,800 tokens (action deferred)
Skills (53):         5,300 tokens (archived 1 duplicate)
Commands (36):         800 tokens (archived 20+ unused)
CLAUDE.md:             950 tokens (45% reduction)
MEMORY.md:             850 tokens (57% reduction)
Project CLAUDE.md:     100 tokens (already minimal)
System reminders:    2,000 tokens (can't change)
---------------------------------
BASELINE:          ~40,000 tokens

SAVINGS: ~3,200 tokens/msg (7% reduction)
```

**Plus behavioral changes**:
- Agent model selection (haiku for data): 50-60% savings on automation
- /compact at 70%: 40-82% savings on long sessions
- qmd for local search: 60-95% savings on exploration

**Total estimated impact**: 40-50% overall cost reduction

---

## Further Reading

- **Official Docs**: https://docs.anthropic.com (prompt caching, context windows)
- **Tool Search**: Introduced v2.1.7 (deferred tool loading)
- **Community**: r/ClaudeAI, r/anthropic (optimization tips)
