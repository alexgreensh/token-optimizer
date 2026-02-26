# Agent Prompt Templates

All agent prompts for the Token Optimizer skill. The orchestrator (SKILL.md) dispatches these agents with `COORD_PATH` set to the session coordination folder.

---

## Phase 1: Audit Agents (dispatch ALL in parallel, model="haiku")

### 1. CLAUDE.md Auditor

```
Task(
  description="CLAUDE.md Auditor - Token Optimizer",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the CLAUDE.md Auditor.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/audit/claudemd.md

**Your job**: Analyze global CLAUDE.md for token waste.

1. Find CLAUDE.md:
   - Check ~/.claude/CLAUDE.md (global config)
   - Check current project root CLAUDE.md (if exists)

2. Measure:
   - Line count
   - Estimated tokens (~15 tokens per line of prose, ~8 for YAML/lists)
   - Sections (break down by heading)

3. Identify optimization targets:
   - Content that belongs in skills/commands (workflows, tool configs, detailed standards)
   - Duplication with MEMORY.md (check ~/.claude/projects/-Users-*/memory/MEMORY.md)
   - Verbose sections (>50 lines)
   - Cache structure: Is static content first, volatile content last? (Prompt caching needs stable prefixes)
   - @imports pattern: Could detailed sections reference files in .claude/docs/ instead?

4. Write findings to {COORD_PATH}/audit/claudemd.md:
   # CLAUDE.md Audit

   **Location**: [path]
   **Size**: X lines, ~Y tokens

   ## Sections
   | Section | Lines | ~Tokens | Optimization Potential |
   |---------|-------|---------|------------------------|

   ## Tiered Content (should be moved)
   - [Section name]: Move to [skill/command/reference file]

   ## Duplication
   - [What overlaps with MEMORY.md]

   ## Estimated Savings
   ~X tokens/message if optimized

Task complete when file is written."""
)
```

---

### 2. MEMORY.md Auditor

```
Task(
  description="MEMORY.md Auditor - Token Optimizer",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the MEMORY.md Auditor.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/audit/memorymd.md

**Your job**: Analyze MEMORY.md for waste and duplication.

1. Find MEMORY.md:
   - Check ~/.claude/projects/-Users-*/memory/MEMORY.md

2. Measure:
   - Line count
   - Estimated tokens (~15 tokens per line)
   - Sections

3. Identify:
   - Content that duplicates CLAUDE.md (paths, personality, gotchas)
   - Verbose operational history (should be condensed to current rule only)
   - Content better stored in semantic memory MCP (if mcp__memory-semantic tools exist)

4. Write findings to {COORD_PATH}/audit/memorymd.md:
   # MEMORY.md Audit

   **Location**: [path]
   **Size**: X lines, ~Y tokens

   ## Duplication with CLAUDE.md
   - [Section]: X lines duplicate

   ## Verbose Sections
   - [Section]: Can be condensed from X to Y lines

   ## Estimated Savings
   ~X tokens/message if optimized

Task complete when file is written."""
)
```

---

### 3. Skills Auditor

```
Task(
  description="Skills Auditor - Token Optimizer",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the Skills Auditor.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/audit/skills.md

**Your job**: Inventory skills and identify overhead.

1. Find skills:
   ls -la ~/.claude/skills/

2. Count:
   - Total skills (count directories with SKILL.md)
   - Frontmatter overhead (~100 tokens per skill)

3. Identify:
   - Duplicate skills (similar names/descriptions)
   - Archived skills still in skills/ (should be in _backups/)
   - Unused domain skills (e.g., 5 n8n skills but user doesn't do n8n work)

4. Write findings to {COORD_PATH}/audit/skills.md:
   # Skills Audit

   **Total skills**: X
   **Estimated menu overhead**: ~Y tokens (X x 100)

   ## Potential Duplicates
   - [skill1] / [skill2]: [why similar]

   ## Archive Candidates
   - [skill]: [reason]

   ## Estimated Savings
   ~X tokens if Y skills archived

Task complete when file is written."""
)
```

---

### 4. MCP Auditor

```
Task(
  description="MCP Auditor - Token Optimizer",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the MCP Auditor.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/audit/mcp.md

**Your job**: Inventory MCP servers and deferred tools.

1. Check MCP config:
   - Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json
   - Claude Code user config: ~/.config/claude/user_config.json (if exists)

2. Count deferred tools:
   - Look at current session for "Available deferred tools" in system prompt
   - Each deferred tool ~ 100 tokens in menu

3. Identify optimization targets:
   - Servers with broken auth (tools won't work anyway)
   - Rarely-used servers (>10 tools but domain-specific)
   - Duplicate servers (same tools from multiple sources)

4. Write findings to {COORD_PATH}/audit/mcp.md:
   # MCP Audit

   **Deferred tools count**: X
   **Estimated menu overhead**: ~Y tokens (X x 100)

   ## Servers Inventory
   | Server | Tools | Status | Usage |
   |--------|-------|--------|-------|

   ## Broken/Unused Servers
   - [server]: [reason to disable]

   ## Estimated Savings
   ~X tokens if Y servers disabled

Task complete when file is written."""
)
```

---

### 5. Commands Auditor

```
Task(
  description="Commands Auditor - Token Optimizer",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the Commands Auditor.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/audit/commands.md

**Your job**: Inventory commands and measure overhead.

1. Find commands:
   ls -la ~/.claude/commands/

2. Count:
   - Total commands (count subdirectories)
   - Frontmatter overhead (~50 tokens per command)

3. Identify:
   - Rarely-used commands
   - Commands that could merge
   - Archived commands still in commands/ (should be in _backups/)

4. Write findings to {COORD_PATH}/audit/commands.md:
   # Commands Audit

   **Total commands**: X
   **Estimated menu overhead**: ~Y tokens (X x 50)

   ## Archive Candidates
   - [command]: [reason]

   ## Estimated Savings
   ~X tokens if Y commands archived

Task complete when file is written."""
)
```

---

### 6. Hooks & Advanced Auditor

```
Task(
  description="Hooks & Advanced Auditor - Token Optimizer",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the Hooks & Advanced Auditor.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/audit/advanced.md

**Your job**: Check for advanced optimization opportunities.

1. Hooks configuration:
   - Check ~/.claude/settings.json for hooks config
   - Check .claude/settings.json (project-level)
   - Check for PreCompact, SessionStart, PostToolUse hooks
   - If no hooks: flag as HIGH PRIORITY opportunity

2. Prompt caching structure:
   - Read CLAUDE.md and check if static content comes FIRST (cacheable)
   - Dynamic/volatile content should be LAST
   - Prompt caching needs stable prefixes >1024 tokens, 5-min TTL
   - 90% cost reduction on cached content

3. .claudeignore status:
   - Check if ~/.claude/.claudeignore exists
   - Check for project-level .claudeignore
   - If missing: flag as HIGH PRIORITY

4. Token monitoring:
   - Check if ccusage is installed: which ccusage
   - Check for OTLP telemetry config
   - Check if /context command awareness exists in CLAUDE.md

5. Plan mode awareness:
   - Check if CLAUDE.md mentions plan mode / Shift+Tab
   - Plan mode = 50-70% fewer iteration cycles

6. Write findings to {COORD_PATH}/audit/advanced.md:
   # Advanced Optimizations Audit

   ## Hooks Configuration
   **Status**: [Not configured / Partially configured / Configured]
   **Hooks found**: [list]
   **Missing high-value hooks**:
   - PreCompact: Guide compaction to preserve key context
   - SessionStart: Re-inject critical context after compaction
   - PostToolUse: Auto-format code after edits (saves output tokens)

   ## Prompt Caching Structure
   **CLAUDE.md structure**: [Static-first / Mixed / Not optimized]
   **Issue**: [describe if volatile content breaks cache prefix]

   ## .claudeignore
   **Status**: [Exists / Missing]
   **Coverage**: [Good / Needs expansion]

   ## Token Monitoring
   **ccusage installed**: [Yes / No]
   **Telemetry**: [Enabled / Not configured]

   ## Plan Mode
   **Documented**: [Yes / No]

   ## Estimated Savings
   - Hooks: ~10-20% reduction in wasted context
   - Cache optimization: Up to 90% on repeated prefix content
   - Monitoring: Enables data-driven optimization

Task complete when file is written."""
)
```

---

## Phase 2: Synthesis Agent (model="sonnet")

```
Task(
  description="Token Optimizer Synthesis",
  subagent_type="general-purpose",
  model="sonnet",
  prompt=f"""You are the Synthesis Agent for Token Optimizer.

Coordination folder: {COORD_PATH}
Input: Read ALL files in {COORD_PATH}/audit/
Output: {COORD_PATH}/analysis/optimization-plan.md

**Your job**: Synthesize audit findings into a prioritized action plan.

1. Read all audit files
2. Calculate total baseline overhead
3. Prioritize optimizations by impact x effort
4. Create tiered plan (Quick Wins, Medium Effort, Deep Optimization)

Output format:
# Token Optimization Plan

## Baseline (Current State)
- CLAUDE.md: X tokens
- MEMORY.md: Y tokens
- Skills menu: Z tokens
- MCP menu: A tokens
- Commands menu: B tokens
**Total per-message overhead**: ~TOTAL tokens

## Quick Wins (< 1 hour, high impact)
- [ ] [Action]: [savings estimate]

## Medium Effort (1-3 hours, medium-high impact)
- [ ] [Action]: [savings estimate]

## Deep Optimization (3+ hours, medium impact)
- [ ] [Action]: [savings estimate]

## Behavioral Changes (free, high impact over time)
- [ ] [Habit]: [savings estimate]

## Projected Savings
- Quick Wins: X tokens/msg (Y%)
- All implemented: Z tokens/msg (W%)

Task complete when file is written."""
)
```

---

## Phase 5: Verification Agent (model="haiku")

```
Task(
  description="Token Optimizer Verification",
  subagent_type="general-purpose",
  model="haiku",
  prompt=f"""You are the Verification Agent.

Coordination folder: {COORD_PATH}
Output file: {COORD_PATH}/verification/results.md

**Your job**: Measure post-optimization state.

1. Re-measure:
   - CLAUDE.md size (lines + estimated tokens)
   - MEMORY.md size
   - Skills count
   - MCP deferred tools count
   - Commands count

2. Calculate savings:
   - Before (from audit files)
   - After (current measurement)
   - Delta (tokens saved per message)
   - Percentage reduction

3. Write to {COORD_PATH}/verification/results.md:
   # Optimization Results

   ## Before -> After
   | Component | Before | After | Saved |
   |-----------|--------|-------|-------|
   | CLAUDE.md | X tokens | Y tokens | Z tokens |
   | MEMORY.md | X tokens | Y tokens | Z tokens |
   | Skills menu | X tokens | Y tokens | Z tokens |
   | MCP menu | X tokens | Y tokens | Z tokens |

   **Total Savings**: ~X tokens/message (Y% reduction)

   ## Cost Impact
   Assuming 100 messages/day:
   - Daily savings: ~X tokens
   - Monthly savings: ~Y tokens
   - Est. monthly cost reduction: $Z (at Opus pricing)

Task complete when file is written."""
)
```
