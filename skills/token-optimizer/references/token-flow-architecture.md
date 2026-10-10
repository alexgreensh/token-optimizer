# Token Flow Architecture: How Claude Code Loads Context

Understanding how tokens flow through Claude Code is critical for optimization. This document maps the complete loading sequence.

---

## Contents

- [The Loading Sequence (Every Message)](#the-loading-sequence-every-message)
- [Token Budget Breakdown (Typical Setup)](#token-budget-breakdown-typical-setup)
- [What You Can Control (Optimization Targets)](#what-you-can-control-optimization-targets)
- [Progressive Loading (How Skills/Commands Work)](#progressive-loading-how-skillscommands-work)
- [The Hidden Tax: System Reminders](#the-hidden-tax-system-reminders)
- [Subagent Context Inheritance](#subagent-context-inheritance)
- [Context Window Lifecycle](#context-window-lifecycle)
- [The 1,000 Token Rule](#the-1000-token-rule)
- [Caching Behavior (Prompt Caching)](#caching-behavior-prompt-caching)
- [Real Cost: Context Budget, Not Dollars](#real-cost-context-budget-not-dollars)
- [Model Cost Comparison (Why Routing Matters)](#model-cost-comparison-why-routing-matters)
- [Optimization Priority Matrix](#optimization-priority-matrix)
- [Real-World Example: Unaudited Power User (Tool Search Active)](#real-world-example-unaudited-power-user-tool-search-active)
- [Additional Config Features (Token Impact)](#additional-config-features-token-impact)
- [Cache Economics and Compaction](#cache-economics-and-compaction)
- [Further Reading](#further-reading)

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
|   (Source: the /context output; varies by build)    |
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
| Tool Search (on by default):                         |
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
| - ~/.claude/CLAUDE.md          ~2,000-5,000 tokens  |
|   (Anthropic: under 200 lines; ~300/~4,500 internal)|
| - ~/.claude/projects/.../      ~1,500-3,000 tokens  |
|   MEMORY.md (200-line auto-load cap)                |
| - [repo]/CLAUDE.md             ~10-1,500 tokens     |
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

**Note**: Percentages assume a 1M-token context window, the default on current Claude models (Haiku 5.5, Sonnet 5+, Opus 4.7+; Sonnet 4.6 and Opus 4.6 reach 1M only through their `[1m]` variants). On a 200K window, these same absolute numbers represent 5x higher percentages (multiply by 5).

### Well-Optimized Setup (~23K baseline, Tool Search active)
```
Core system + tools: 15,000 tokens
MCP (ToolSearch +      1,000 tokens  (500 base + ~30 tools x 15)
  deferred names):
Skills (20):           2,000 tokens
Commands (10):           500 tokens
CLAUDE.md:             2,500 tokens  (~170 lines, well under 200-line Anthropic guidance)
MEMORY.md:             1,500 tokens  (~100 lines, well under 200-line cap)
System reminders:      1,000 tokens
---------------------------------
BASELINE:            ~23,500 tokens (2.4% of 1M)
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
CONSUMED:            ~43,000 tokens (4.3% of 1M)
= UNAVAILABLE:       ~43,000 tokens (4.3% of 1M), plus auto-compact headroom
```

**Difference**: ~19,500 tokens consumed per message = 1.8x overhead vs optimized
**Note**: Before Tool Search, MCP alone could add 40-80K tokens; Tool Search (on by default) reduced this by ~85%. This "unaudited" baseline is a power user who has been adding to their config for 3+ months without auditing. When auto-compact is enabled (the default), Claude Code reserves extra headroom inside the compact window, so the true unavailable figure is higher still; on 1M models compaction fires at about 967K tokens by default.

---

## What You Can Control (Optimization Targets)

### HIGH IMPACT (Always Loaded)

| Component | Control Level | Optimization Method |
|-----------|---------------|---------------------|
| **CLAUDE.md** | Full | Slim to under 200 lines (Anthropic guidance; code.claude.com/docs/en/memory). Internal heuristic: ~300 lines / ~4,500 tokens. Move content to skills. Apply tiered architecture. |
| **MEMORY.md** | Full | Stay under 200-line auto-load cap (~3,000 tokens). Remove duplication with CLAUDE.md. |
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
- **Use `permissions.deny`** (narrowly): Exclude files Claude never needs (secrets, `node_modules`, build output). Keep rules narrow, a broad deny on a path Claude actively wants causes repeated "permission denied" feedback that accumulates in context and costs tokens.
- **Don't edit unnecessary files**: Each edit = potential injection
- **Be aware**: You can't disable these entirely, but you can avoid triggering them

---

## Subagent Context Inheritance

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
AUTO-COMPACT fires at the model's compact window: about 967K tokens by
    default on 1M models, near the context limit on 200K models
    |
Context compressed (lossy)
    |
Continue until /clear or session end
```

### Context Fill Degradation
Token Optimizer's quality score follows a published long-context retrieval curve for Claude models, by share of the window filled (an estimate, not a measurement):

| Window filled | Estimated retrieval quality |
|---------------|-----------------------------|
| 0-10% | 98 to 96 |
| 10-25% | 96 to 93 |
| 25-50% | 93 to 88 |
| 50-70% | 88 to 80 |
| 70-100% | 80 to 76 |

**Recommendation**: Compact manually at phase boundaries instead of waiting. Auto-compact on 1M models fires at about 967K tokens by default, well past where quality has dropped, so set a lower per-model window with `/autocompact <n>` (100K-1M, saved per model; `/autocompact auto` restores the tuned window) or the `autoCompactWindow` setting. `measure.py compact-advice` estimates from the user's own history whether an earlier window pays off.

Token Optimizer auto-removes `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` if found (undocumented env var with inverted semantics that causes premature compaction).

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

**Prompt caching is on by default in Claude Code.** Disable it with `DISABLE_PROMPT_CACHING=1`.
- Cache order: tools first, then system prompt, then messages (chronological)
- Main-conversation default TTL: 1 hour on subscription billing, 5 minutes on usage credits or an API key. The `promptCacheTtl` setting, the `CLAUDE_CODE_PROMPT_CACHE_TTL` env var, `ENABLE_PROMPT_CACHING_1H`, and `FORCE_PROMPT_CACHING_5M` control it (see the controls table in token-coach's quick-reference.md). The timer resets with each active message.
- Subagents default to 5 minutes; `subagentPromptCacheTtl`, `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`, or per-agent `experimental.cacheTtl` can raise that to 1 hour.

**Pricing**:
- Cache reads: 0.1x base input (90% cheaper)
- Cache writes: 1.25x base input for a 5-minute cache, 2x for a 1-hour cache
- Minimum cacheable prompt size varies by model; see the prompt caching docs

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
- Quality: lost-in-the-middle degradation grows as the window fills, regardless of caching
- Multi-agent amplification: each subagent inherits full overhead at full size

**Structuring for cache hits**: Stable sections first (identity, rules), volatile sections last. This maximizes the cached prefix length.

---

## Real Cost: Context Budget, Not Dollars

**Official Anthropic data** (code.claude.com/docs/en/costs): Average cost is ~$6/dev/day ($100-200/mo with Sonnet). Background token usage (hooks, auto-memory) typically under $0.04/session.

Most Claude Code users are on Max subscriptions ($100-200/month), not per-token API pricing. The real cost of overhead is not dollars. It is context budget:

### Why Overhead Hurts (Even on Subscription)
```
1. FASTER CONTEXT FILL
   20K overhead = 10% of a 200K window (2% of 1M) gone before you type
   35K overhead = 18% of 200K (3.5% of 1M). Compaction arrives that much sooner.

2. MORE COMPACTION CYCLES
   Each compaction is lossy. More compactions = more context lost.
   A session with 35K overhead compacts ~2x more often than 20K.

3. QUALITY DEGRADATION
   Retrieval quality slips as the window fills (see the curve above).
   With 35K overhead you reach any given fill level after fewer messages.

4. BEHAVIORAL MULTIPLIER
   Every message re-sends the overhead. 100 messages/day
   at 35K overhead = 3.5M tokens of overhead alone.
   At 20K overhead = 2.0M tokens. That's 1.5M tokens freed
   for actual work content.
```

### For API Users (Per-Token Pricing)

Price the token math below at your model's current input rate from the platform pricing page; the rates change often enough that this guide quotes tokens, not dollars.

**Without caching** (worst case, e.g. cache misses from inactivity):
```
20K overhead x 100 msgs/day x 30 days = 60M tokens/mo billed at full input
35K overhead x 100 msgs/day x 30 days = 105M tokens/mo billed at full input
Optimization removes ~45M tokens/mo of that full-price billing.
```

**With caching** (typical for active sessions):
```
Cached reads cost 10% of base input price.
  20K overhead: ~2M effective tokens/mo | 35K overhead: ~3.5M effective tokens/mo
Savings from optimization scale with your model's input rate; cache reads keep them at one-tenth of full price.
```

**The honest framing**: For subscription users (Max, Pro), dollar cost is irrelevant. The real impact is context window space, rate limit quota burn, and quality degradation from fuller context.

---

## Model Cost Comparison (Why Routing Matters)

Routing subagents to the right model tier is the highest-ROI behavioral change. The verified floor: Haiku 5.5 input is $0.10/MTok up to 100K prompt tokens and $0.50/MTok above that (5x on long prompts, per the platform pricing page). Sonnet and Opus bill higher per token; check anthropic.com/pricing for current rates rather than relying on any table here.

**Worked shape**: 5-agent workflow (file scanning + analysis + synthesis):
- **All Opus**: 5 agents x (30K input + 5K output) at Opus's input/output rates
- **Routed** (3 Haiku + 1 Sonnet + 1 Opus): the same tokens, but the three data-gathering agents bill at Haiku's input rate, the cheapest tier
- The routed mix costs a fraction of all-Opus; quote exact dollars from the current pricing page, not from memory

For subscription users (Max plan): model routing affects rate limits, not dollars. Lighter models consume fewer quota units, so routing keeps your session under rate limits longer.

**What routing does NOT save**: Context window space. Subagents inherit the full system prompt regardless of model. A Haiku agent gets the same ~30K token overhead as an Opus agent. Routing saves dollars and rate limits. Config optimizations (CLAUDE.md slimming, skill archival) save context window space.

See `optimization-checklist.md` for the full Model Routing Strategy section with task-to-model mapping and a ready-to-paste CLAUDE.md snippet.

---

## Optimization Priority Matrix

| Component | Unaudited Typical | Optimized Target | Savings | Impact x Effort |
|-----------|-------------------|------------------|---------|-----------------|
| MCP tools | 9,000 tokens | 6,000 tokens | -3,000 | HIGH x MEDIUM |
| CLAUDE.md | 3,500 tokens | 2,500 tokens (~170 lines) | -1,000 | HIGH x LOW |
| Skills (60 -> 30) | 6,000 tokens | 3,000 tokens | -3,000 | MEDIUM x MEDIUM |
| MEMORY.md | 3,500 tokens | 2,000 tokens (~130 lines) | -1,500 | HIGH x LOW |
| System reminders | 3,000 tokens | 1,000 tokens | -2,000 | MEDIUM x LOW |
| Commands (60 -> 25) | 3,000 tokens | 1,200 tokens | -1,800 | LOW x LOW |

**Start here**: CLAUDE.md + MEMORY.md (30 min effort, ~2,500 token savings)

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
CONSUMED:           ~43,000 tokens (4.3% of 1M)
= UNAVAILABLE:      ~43,000 tokens (4.3% of 1M) plus auto-compact headroom
```

**After config optimization**:
```
Core system + tools: 15,000 tokens (fixed)
MCP tools:            6,000 tokens (pruned unused servers)
Skills (~30):         3,000 tokens (archived 30)
Commands (~25):       1,200 tokens (archived 35)
CLAUDE.md:            2,500 tokens (~170 lines, under 200-line Anthropic guidance)
MEMORY.md:            2,000 tokens (~130 lines, under 200-line cap)
System reminders:     1,000 tokens (permissions.deny)
---------------------------------
CONSUMED:           ~30,700 tokens (3.1% of 1M)
= UNAVAILABLE:      ~30,700 tokens (3.1% of 1M) plus auto-compact headroom

CONFIG SAVINGS: ~12,300 tokens/msg (29% reduction in consumed overhead)
```

**At 100 messages/day, that's 1.2M tokens of overhead saved daily.**

Prompt caching means the dollar savings are modest (cached tokens cost 10% of base). But the context window space savings are real: you hit compaction later, quality stays higher longer, and each subagent inherits ~12,000 fewer tokens of overhead.

**Plus behavioral changes** (compound across every message):
- Agent model selection (haiku for data): the largest per-token cut on automation
- /compact at phase boundaries: the biggest reductions come from compacting right after a bulky research or exploration phase
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

Skills inject `name` + `description` into always-on context every turn (progressive disclosure: the body loads only on invoke). Dropping a skill's description from that always-on context recovers its per-turn description cost — quantify it with the per-skill token cost `measure.py report` already computes, not a flat constant. Descriptions are also dropped after `/compact` and never re-injected, so the savings persist across compaction. Claude Code gives **two distinct levers** to do this, and they are NOT the same thing:

**1. `skillOverrides` in settings (the true "name-only" middle mode).** `.claude/settings.local.json` `skillOverrides` maps a skill name to one of four visibility states (the `/skills` menu writes it for you — highlight a skill, press `Space` to cycle, `Enter` to save). Per the official docs (code.claude.com/docs/en/skills.md, "Override skill visibility from settings"):

| Value | Listed to Claude | In `/` menu |
| :-- | :-- | :-- |
| `"on"` (default) | Name **and** description | Yes |
| `"name-only"` | **Name only** (description hidden) | Yes |
| `"user-invocable-only"` | Hidden from Claude | Yes |
| `"off"` | Hidden | Hidden |

`"name-only"` is the real name-visible/description-hidden middle mode: the model still sees the skill's **name** (so it can still invoke it by name) but not its description, so it stops auto-triggering on description match while you keep the token savings. This is the right choice for a skill you want to stay discoverable-by-name without paying for its description or letting it fire automatically.

**2. `disable-model-invocation: true` in SKILL.md frontmatter (fully hidden from the model).** This **removes the skill from the model's context entirely** — not even the name is visible; only the USER can invoke it via `/name`. Official docs, verbatim: *"Description not in context, full skill loads when you invoke"* and *"They stay completely out of context until you invoke them with /name"* (context-window.md). Use it for skills only the user ever triggers explicitly (e.g. one that launches a browser tab, where auto-triggering on a guess is intrusive). It is a **binary** frontmatter flag (advertised, or fully hidden) — the graded middle states live in `skillOverrides` above, not in the flag. Equivalent settings values are `"user-invocable-only"` / `"off"`.

Do NOT relabel `disable-model-invocation` as "name-only" — that term is Claude Code's `skillOverrides` state (name kept), and `disable-model-invocation` hides the name too. Keep skills whose value is proactive auto-triggering on the default `"on"`.

**Cross-harness caveat**: both levers are **Claude-Code-specific**. Codex / OpenCode / Copilot skills keep `name` + `description` resident regardless; there is no per-skill "hide description" toggle there (the equivalent is a different primitive — a command / prompt-file — or a full disable via `[[skills.config]] enabled = false` on Codex). Other harnesses ignore the unknown frontmatter key (harmless, not an error).

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
    "CLAUDE_CODE_MAX_THINKING_TOKENS": "10000",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384",
    "MAX_MCP_OUTPUT_TOKENS": "25000",
    "ENABLE_TOOL_SEARCH": "auto"
  }
}
```

See `optimization-checklist.md` items 23-30 for what each does and how the optimizer audits them.

---

## Cache Economics and Compaction

Prompt caching is the foundation of Claude Code's cost model. Cached reads cost 10% of full input price. When compaction fires, the entire conversation is replaced with a summary, invalidating the cache prefix. Every token after compaction gets billed at full price until the new cache warms up.

**The compaction tax**: A single compaction in a 100K-token session re-bills ~90K tokens at full input price instead of the cached rate, and multiple compactions compound this.

**Mitigation strategies** (see optimization-checklist.md item 8):
1. **Delay compaction**: Keep context lean (the optimizer's core job). Fewer tokens = later compaction = fewer rebuilds.
2. **Context Editing API** (API users): `clear_tool_uses_20250919` and `clear_thinking_20251015` surgically evict stale content without triggering full compaction. Cache prefix survives.
3. **Smart Compaction**: PreCompact checkpoint + Compact Instructions + SessionStart restore minimizes wasted post-compaction turns (which compound the cost).
4. **Strategic cache breakpoints**: Place `cache_control` breakpoints before editable content. The 20-block lookback window means partial invalidation, not total.

---

## Further Reading

- **Official Docs**: https://docs.anthropic.com (prompt caching, context windows, context editing)
- **Official Costs**: https://code.claude.com/docs/en/costs (average spend, agent-team multiplier, background overhead data)
- **Model config**: https://code.claude.com/docs/en/model-config (context windows and auto-compaction defaults)
- **Tool Search**: on by default (deferred tool loading, ~85% MCP reduction)
