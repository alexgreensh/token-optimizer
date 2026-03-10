# Token Optimization Checklist

Comprehensive checklist of ALL optimization techniques.

---

## QUICK WINS (< 30 minutes each)

### 1. Check /cost and /context (0 minutes)
**Target**: Know your baseline before changing anything

**Actions**:
- [ ] Run `/cost` to see current session spending
- [ ] Run `/context` to see context fill level
- [ ] Note your per-message overhead (this IS your baseline)

**Why first**: You can't optimize what you don't measure. And these are built-in, zero effort.

---

### 2. Check Model Routing (5 minutes)
**Target**: Confirm you have model routing instructions in CLAUDE.md

**Quick check**: Does your CLAUDE.md tell Claude which model to use for subagents? If not, add the snippet from the Model Routing section below. One line, 50-75% savings on every multi-agent workflow.

**Expected savings**: 50-75% on automation costs (see full breakdown below)

---

### 3. CLAUDE.md Consolidation
**Target**: Slim to <800 tokens (~50-60 lines). Anthropic recommends CLAUDE.md under ~500 lines (code.claude.com/docs/en/costs). That official guidance is the minimum bar. Our <800 token target is the optimized ceiling.

**Actions**:
- [ ] Remove content that belongs in skills/commands (workflows, detailed configs)
- [ ] Remove content that duplicates MEMORY.md (paths, gotchas, personality)
- [ ] Move reference content to on-demand files (coding standards, tool configs)
- [ ] Condense personality spec to 1-2 lines (full spec can live in MEMORY.md)
- [ ] Apply tiered architecture (see below)

**Tiered Architecture Pattern**:
- **Tier 1 (always loaded, <800 tokens)**: Identity, critical rules, key paths, personality ONE-LINER
- **Tier 2 (skill/command, loaded on-demand)**: Workflows, domain docs, tool configs
- **Tier 3 (file reference, explicit only)**: Full guides, templates, detailed standards

**Expected savings**: 400-700 tokens/msg

---

### 4. MEMORY.md Deduplication
**Target**: Remove 100% overlap with CLAUDE.md

**Actions**:
- [ ] Remove Key Paths if already in CLAUDE.md (choose ONE source of truth)
- [ ] Remove personality spec if already in CLAUDE.md
- [ ] Condense verbose operational history to current rule only
- [ ] Keep only: Learnings, corrections, habit tracking

**Expected savings**: 400-800 tokens/msg

---

### 5. File Exclusion Rules
**Target**: Block unnecessary files from context via `permissions.deny`

See `examples/permissions-deny-template.json` for a ready-to-use template.

Add `permissions.deny` rules to `.claude/settings.json` (project-level) or `~/.claude/settings.json` (global):

```json
{
  "permissions": {
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./node_modules/**)",
      "Read(./dist/**)",
      "Read(./**/*.log)"
    ]
  }
}
```

**Why**: Files matching deny patterns are excluded from file discovery, search, and read operations. This is the official approach (replaces deprecated `ignorePatterns`). Security + token savings.

**CAUTION**: Deny rules affect ALL tools in ALL sessions for the scope they're applied to.
- Apply at **project level** (`.claude/settings.json`) first, not global. Easier to debug when something breaks.
- **Never deny `*.sqlite` or `*.db` globally** unless you're certain no tools need database access. Many plugins (session memory, search indexes, WhatsApp) read SQLite files.
- **Credential denies** (`.env`, `*.key`) are usually safe and desired, but will break any skill that reads API keys from those files at runtime.

**Expected savings**: Varies (500-2,000 tokens/msg if you frequently edit media/deps)

---

### 6. Archive Unused Skills
**Target**: Reduce skill menu overhead

**Actions**:
- [ ] Identify duplicate skills (similar names/descriptions)
- [ ] Identify unused domain skills
- [ ] Create backup: `mkdir -p ~/.claude/_backups/skills-archived-$(date +%Y%m%d)`
- [ ] Move unused skills: `mv ~/.claude/skills/[skill-name] ~/.claude/_backups/skills-archived-*/`

**CRITICAL**: Subfolder `_archived/` INSIDE skills/ still loads as namespace. Must move OUTSIDE skills/ entirely.

**Expected savings**: ~100 tokens per skill archived

---

### 7. Trim Commands
**Target**: Reduce command menu overhead

**Actions**:
- [ ] Identify rarely-used commands
- [ ] Merge similar commands if possible
- [ ] Archive to `~/.claude/_backups/commands-archived-$(date +%Y%m%d)/`

**Expected savings**: ~50 tokens per command archived

---

## MODEL ROUTING STRATEGY (Highest-ROI Behavioral Change)

Model routing is the single highest-ROI optimization for multi-agent workflows. It saves dollars (API users), rate limit quota (subscription users), and wall-clock time. One instruction in CLAUDE.md, 50-75% cost reduction on automation.

### Cost Math

| Model | Input $/1M | Output $/1M | Relative Cost (vs Haiku) |
|-------|-----------|-------------|--------------------------|
| Haiku | $0.25 | $1.25 | 1x |
| Sonnet | $3.00 | $15.00 | 12x |
| Opus | $15.00 | $75.00 | 60x |

*Pricing from anthropic.com/pricing. Check for current rates.*

Haiku is **12x cheaper** than Sonnet and **60x cheaper** than Opus per token. For tasks that don't require judgment or complex reasoning, every Opus call is 60x overspend.

### Task-to-Model Mapping

| Task Type | Model | Examples | Why |
|-----------|-------|----------|-----|
| Data gathering | haiku | File reading, counting, scanning, listing, presence checks | No judgment needed, pattern matching only |
| Light analysis | haiku | Formatting, simple transforms, status checks, inventory | Structured output from structured input |
| Moderate reasoning | sonnet | Code review, writing, synthesis, moderate debugging, content analysis | Requires judgment + coherence |
| Complex reasoning | opus | Architecture decisions, novel problems, cross-cutting analysis, multi-file synthesis | Needs full capability, nuanced tradeoffs |

### CLAUDE.md Snippet (Ready to Copy-Paste)

```markdown
## Agent Model Selection
Default subagents to haiku. Upgrade only when task requires judgment:
- haiku: file reading, data gathering, counting, scanning, formatting
- sonnet: analysis, code review, writing, moderate reasoning
- opus: architecture decisions, novel debugging, cross-cutting synthesis
```

### Worked Example: 5-Agent Workflow

A typical audit workflow dispatches 5 agents to scan files, count items, analyze content, and synthesize findings.

**Without routing (all Opus)**:
Each agent uses ~30K input + ~5K output tokens.
- 5 agents x 30K input x $15/1M = $2.25 input
- 5 agents x 5K output x $75/1M = $1.88 output
- **Total: ~$4.13**

**With routing (3 Haiku + 1 Sonnet + 1 Opus)**:
- 3 Haiku agents: 90K input x $0.25/1M + 15K output x $1.25/1M = $0.04
- 1 Sonnet agent: 30K input x $3/1M + 5K output x $15/1M = $0.17
- 1 Opus agent: 30K input x $15/1M + 5K output x $75/1M = $0.83
- **Total: ~$1.04**

**Savings: ~75% ($3.09 saved per workflow run)**

For subscription users (Max plan): model routing affects rate limits, not dollars. Haiku calls consume fewer quota units and are 3-5x faster. Routing means your session stays under rate limits longer and agents return results faster.

### What Routing Does NOT Save

- **Context window space**: Subagents inherit the full system prompt regardless of model. A Haiku agent gets the same ~30K token system prompt as an Opus agent.
- **System prompt overhead**: CLAUDE.md, skills, MCP tools all load for every subagent at full size.

Routing saves **dollars** (API users), **rate limit quota** (subscription users), and **wall-clock time** (everyone). It does not reduce context window consumption. That's what the config optimizations (CLAUDE.md slimming, skill archival, etc.) address.

### Detection: Is Your Setup Routing Efficiently?

Signs of inefficient routing:
- No model routing instructions in CLAUDE.md or MEMORY.md
- `measure.py trends` shows >70% of tokens going to Opus/Sonnet
- Subagent types include data-gathering patterns (Explore, general-purpose for file reads) running on Opus
- The Settings & Advanced auditor checks this automatically (see agent-prompts.md)

---

## MEDIUM EFFORT (1-3 hours, save 2,000-5,000 tokens)

### 8. MCP Server Audit
**Target**: Remove broken/unused MCP servers and their deferred tool listings

**First, check Tool Search status**:
- If ToolSearch is available in your session, Tool Search is active (default since Jan 2026)
- Tool Search means definitions are deferred (~15 tokens per tool name in menu, not 300-850 for full definitions)
- If Tool Search is NOT active, upgrading Claude Code is the single biggest optimization you can make

**How to audit**:
1. Check Claude Code config: `~/.claude/settings.json` (primary, mcpServers key)
2. Check Desktop config: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
3. Check plugin configs: `~/.claude/plugins/` (plugins can bundle MCP servers)
4. Count deferred tools in current session (system prompt shows count)

**Identify**:
- [ ] Broken servers (auth failed, deprecated APIs)
- [ ] Duplicate tools across servers AND plugins (same tool from multiple sources)
- [ ] Rarely-used servers (domain-specific, >10 tools, used <1x/month)
- [ ] Plugin-bundled MCP that duplicates standalone servers

**Expected savings**: ~15 tokens per deferred tool removed (with Tool Search active). Larger savings from removing full server instructions (~50-100 tokens per server).

---

### 9. Install qmd for Local Search
**Target**: 60-95% reduction on code exploration tasks

**Install**:
```bash
# Option 1: npm
npm install -g qmd

# Option 2: bun
bun install -g github:tobi/qmd
```

**Index your codebase**:
```bash
qmd index /path/to/codebase
```

**Add to CLAUDE.md** (1 line):
```
Before reading files, always try `qmd search [query]` or `qmd query [question]` first.
```

**Why**: Pre-indexes your files with hybrid search (keyword + vector). Claude queries the index instead of reading every file.

**Expected savings**: 60-95% on exploration sessions

---

### 10. Migrate CLAUDE.md Content to Skills
**Target**: Move domain-specific content to on-demand loading

**Pattern**: Skills load ~100 tokens at startup (frontmatter only). Full content loads on-demand. This is 98% cheaper than CLAUDE.md for same content.

**How**:
1. Create skill: `mkdir ~/.claude/skills/[name]`
2. Write SKILL.md with content
3. Remove from CLAUDE.md
4. Add 1-line reference in CLAUDE.md: "Full config: see /[name] skill"

**Expected savings**: ~500-1,000 tokens (depends on volume moved)

---

## DEEP OPTIMIZATION (power users)

### 11. Session Folder Pattern (Architecture Change)
**Target**: Prevent orchestrator context overflow

**Problem**: Multi-agent workflows load all agent outputs into main context. At 5-10K tokens per agent x 5 agents = 25-50K tokens in orchestrator.

**Solution**:
1. Orchestrator creates session folder: `/tmp/[task-name]-$(date +%Y%m%d-%H%M%S)/`
2. Agents write findings to files in session folder
3. Orchestrator receives ONLY: "Agent X completed, output at {path}"
4. Synthesis agent reads files directly
5. Orchestrator NEVER reads full agent outputs

**When to use**: Any task with 3+ subagents or agents producing >5K tokens output each.

---

### 12. Progressive Disclosure Pattern
**Target**: Load context incrementally, not all at once

**Pattern**:
- Phase 1: Load minimal context (identity, current state)
- Phase 2: Ask clarifying questions
- Phase 3: Load relevant context based on answers
- Phase 4: Execute

**Expected savings**: 30-50% on large context tasks

---

## BEHAVIORAL CHANGES (Free, highest cumulative impact)

These save more than config changes over a full day of usage.

### 13. Extended Thinking Awareness
**Target**: Avoid burning expensive thinking tokens unnecessarily

**What it is**: When extended thinking is enabled, Claude generates thousands of "thinking" tokens (output-priced, much more expensive than input). For Opus users, this can be the single largest cost factor.

**Actions**:
- [ ] Use `/model` to check if extended thinking is on
- [ ] Disable for simple tasks (file reading, quick edits, data gathering)
- [ ] Reserve for complex reasoning (architecture, debugging, novel problems)

**Expected savings**: Potentially more than all config changes combined for heavy Opus users

---

### 14. /compact and /clear Hygiene
**Target**: Keep context lean, extend productive session length

**Rules**:
- [ ] Run `/compact` at 50-70% context (auto-compact default is ~83%, which is past the quality degradation zone)
- [ ] Run `/compact` at natural breakpoints (after commit, after feature)
- [ ] Run `/clear` between unrelated topics (cheaper than compact, no summary overhead)
- [ ] Check `/context` periodically to know your fill level
- [ ] Or set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70` in settings.json env block to auto-compact at 70%

**Measured**: Community measurements show /compact can reduce conversation history from 77K to 4K tokens (18x reduction), freeing context from ~50% to 90%.

**Note**: /clear is often better than /compact when switching tasks. Compacting preserves conversation context as a summary. Clearing is cheaper and gives you a completely fresh window.

---

### 15. Batch Requests
**Target**: Reduce context re-sends

**DON'T**:
```
"Change button color" -> response -> "Make it bigger" -> response -> "Add shadow"
(3 messages = 3 full context sends)
```

**DO**:
```
"Change button color to navy, make it bigger (48px), add subtle shadow"
(1 message = 1 context send)
```

**Expected savings**: 2x-3x on multi-step tasks

---

### 16. Skip Confirmations
**Target**: Reduce message count

**DON'T**:
```
User: "Thanks!"
Claude: "You're welcome!"
(Claude just re-sent full context for a courtesy)
```

**Community stat**: One analysis showed 40% of tokens were confirmations and "looks good" messages.

---

### 17. Test Locally, Not Through Claude
**Target**: Avoid expensive test output in chat

**DON'T**: Let Claude run tests and dump 5,000 tokens of output.

**DO**: Run `pytest tests/` locally and paste any failures.

**Expected savings**: 5,000-50,000 tokens per test run

---

## ADVANCED (Power Users)

### 18. Prompt Caching Awareness
**Target**: Understand what caching does and doesn't fix

**Confirmed behavior**: Prompt caching IS active by default in Claude Code. Anthropic internal data shows 96-97% cache hit rate in active sessions. The team treats cache rate like uptime and declares incidents when it drops.

**Pricing**:
- Cache reads: 90% cheaper than normal input ($0.30/M vs $3.00/M for Sonnet)
- Cache writes: 25% surcharge on first request (5-min TTL) or 100% surcharge (1-hour TTL for Max plan)
- TTL: 5 minutes for Pro/API, 1 hour for Max plan. Timer resets with each active message.

**What gets cached**: System prompt (including CLAUDE.md), tool definitions, conversation history prefix up to last cache breakpoint.

**What breaks the cache** (avoid these mid-session):
- Adding/removing an MCP tool
- Switching models
- Editing CLAUDE.md mid-session
- Putting timestamps in system prompt
- Any change to content before a cache breakpoint

**What caching does NOT fix**:
- Context window SIZE (cached tokens still occupy your window)
- Rate limit quotas (cache reads count toward subscription limits)
- Quality degradation past 50-70% fill (lost-in-the-middle)
- Multi-agent amplification (each subagent inherits full overhead)

**Optimization**: Structure CLAUDE.md so stable sections come FIRST, volatile sections LAST. This maximizes cache prefix length.

See `examples/claude-md-optimized.md` for the pattern.

---

### 19. Multi-Project CLAUDE.md Strategy
**Target**: Different configs for different projects

**Pattern**:
- Global `~/.claude/CLAUDE.md`: Identity, personality, core rules (~500 tokens)
- Project `[repo]/CLAUDE.md`: Project-specific context, tech stack, conventions (~300 tokens)

**Why**: Global CLAUDE.md loads for ALL projects. Project CLAUDE.md loads only in that directory. Keep global minimal.

---

### 20. Hook-Based Optimizations
**Target**: Pre/post-session token management

See `examples/hooks-starter.json` for a ready-to-use template.

**Key hooks**:
- **PreCompact**: Guide compaction to preserve critical context
- **PostToolUse**: Trigger auto-formatters, save output tokens

---

### 21. Smart Compaction (v2.0)
**Target**: Preserve decisions, context, and state across compaction events

**What it does**: Captures structured session state before compaction fires, then restores the critical pieces after compaction completes. Decisions, error-fix sequences, agent state, and modified files survive the lossy compaction process.

**Actions**:
- [ ] Install Smart Compaction hooks: `python3 $MEASURE_PY setup-smart-compact --dry-run` (preview first)
- [ ] Run `python3 $MEASURE_PY setup-smart-compact` to install
- [ ] Generate project-specific Compact Instructions: `python3 $MEASURE_PY compact-instructions`
- [ ] Add the generated instructions to `.claude/settings.json` under `compactInstructions`

**What the hooks do**:
- **PreCompact**: Captures session state (decisions, files, errors, agent state) to a markdown checkpoint
- **SessionStart** (after compact): Injects the checkpoint back as context, filling gaps left by compaction
- **Stop/SessionEnd**: Saves a checkpoint when the session ends, enabling continuity in the next session

**Checkpoints stored in**: `~/.claude/token-optimizer/checkpoints/` (auto-cleaned, last 50 or 7 days)

**Expected impact**: Preserves 3-10 key decisions per compaction. Prevents "what were we doing?" restart loops after compaction. Enables session continuity across /clear and session death.

---

### 22. Context Quality Monitoring (v2.0)
**Target**: Measure content quality, not just quantity

**What it does**: Analyzes your session JSONL and scores how useful your context content is. A session at 60% with clean, relevant content performs differently from 60% stuffed with stale reads and duplicate injections.

**Actions**:
- [ ] Run `python3 $MEASURE_PY quality` on your current or recent session
- [ ] Check your score: 85+ is excellent, 70-84 good, 50-69 degraded, <50 critical
- [ ] Act on specific issues: stale reads, bloated results, duplicate reminders

**Quality signals**:
- Stale read ratio (files read then later edited, making the read content outdated)
- Tool result bloat (large results never referenced again)
- Duplicate content (repeated system reminders)
- Compaction depth (each compaction = information loss)
- Decision density (ratio of substantive to overhead exchanges)
- Agent efficiency (dispatch cost vs useful result size)

**Expected impact**: Quantifies when to /compact (score dropping) and validates that smart compaction is working (score improves post-compact).

---

## MONITORING & MEASUREMENT

### 23. Baseline Your Usage

```bash
# Measure current state
python3 $MEASURE_PY report

# Save snapshot before optimizing
python3 $MEASURE_PY snapshot before

# After optimization
python3 $MEASURE_PY snapshot after

# Compare
python3 $MEASURE_PY compare
```

Also track with `/cost` at end of each session and `measure.py trends` for historical data. The SessionEnd hook auto-collects usage into a local SQLite database.

---

### 24. Regular Audits
**Quarterly** (every 3 months):
- [ ] Re-run `/token-optimizer` (skills accumulate, CLAUDE.md grows back)
- [ ] Re-check MCP servers (you add new ones)

**Why**: Optimization entropy. Without discipline, configs grow back to original size.

---

## TOKEN FLOW REFERENCE

**Every message loads this stack** (with Tool Search active, default since Jan 2026):
```
├─ Core system prompt:          ~3,000 tokens  (fixed)
├─ Built-in tools (18+):      ~12,000 tokens  (fixed)
├─ MCP (Tool Search + names):  ~500 + ~15 tokens per deferred tool
├─ MCP server instructions:    ~50-100 tokens per server
├─ Skills frontmatter:          ~100 tokens x skill count
├─ Commands frontmatter:        ~50 tokens x command count
├─ CLAUDE.md (global):          Variable (target: <800)
├─ Project CLAUDE.md:           Variable (target: <300)
├─ MEMORY.md:                   Variable (target: <600)
├─ System reminders:            ~2,000 tokens (auto-injected, variable)
└─ Message + history:           Variable
```

**Irreducible floor**: ~15K tokens even with zero config (no CLAUDE.md, no skills, no MCP). This is core system + built-in tools alone.
**Baseline (well-optimized)**: ~21K tokens first message
**Power user (unoptimized)**: ~43K tokens first message
**Note**: Pre-Tool-Search (2025), unoptimized setups reached 40-80K+

---

## WORKED EXAMPLE: Power User Optimization

**Before** (unaudited power user, 3+ months of use, Tool Search active):
- Core system + built-in tools: ~15,000 tokens (fixed)
- MCP tools: ~9,000 tokens (deferred tools + server instructions)
- Skills (~60): ~6,000 tokens
- Commands (~60): ~3,000 tokens
- CLAUDE.md: ~3,500 tokens (grown organically, never trimmed)
- MEMORY.md: ~3,500 tokens (duplicates CLAUDE.md content)
- System reminders: ~3,000 tokens (no permissions.deny rules)
- **Total consumed: ~43,000 tokens/msg (22% of 200K)**
- **+ Autocompact buffer: ~33,000 tokens (16.5%, reserved)**
- **= Total unavailable: ~76,000 tokens (38% of 200K)**

**Config changes** (what the optimizer implements):
1. CLAUDE.md: 3,500 -> 800 tokens (progressive disclosure)
2. MEMORY.md: 3,500 -> 1,000 tokens (dedup with CLAUDE.md)
3. Skills: 60 -> 30 (30 archived, ~3,000 tokens saved)
4. Commands: 60 -> 25 (35 archived, ~1,800 tokens saved)
5. MCP: pruned unused servers (~3,000 tokens saved)
6. permissions.deny rules added (~2,000 tokens saved from file exclusion)
- **Config savings: ~15,000 tokens/msg (35% reduction in consumed overhead)**
- **After consumed: ~28,000 tokens/msg (14% of 200K)**
- **After unavailable (with buffer): ~61,000 tokens (30% of 200K)**

**Behavioral changes** (what the optimizer teaches):
- Agent model selection (haiku for data): 50-75% on automation
- /compact at 50-70%: up to 18x reduction in conversation history
- Extended thinking awareness: variable, potentially largest single factor
- Batching requests: 2-3x on multi-step tasks
- /clear between topics: prevents stale context accumulation
- **Behavioral savings: extend productive session length, reduce compaction cycles, improve output quality**

The config changes shrink your per-message overhead. The behavioral changes compound across every message, every session, every day. Together they are the full picture.

---

## ENVIRONMENT VARIABLES & SETTINGS (Tune your setup)

These are settings that affect token usage and context behavior. The optimizer audits current values and explains tradeoffs.

### 25. MAX_THINKING_TOKENS (default: 10,000)
**Target**: Understand thinking token budget

**What it is**: Controls the maximum tokens Claude spends on extended thinking (chain-of-thought reasoning). Extended thinking makes Claude smarter on complex problems but uses expensive output tokens.

**Actions**:
- [ ] Check current value: `CLAUDE_CODE_MAX_THINKING_TOKENS` in settings.json env block
- [ ] Observe thinking patterns in `/cost` output (thinking tokens are listed separately)
- [ ] Consider if thinking budget is being spent on simple tasks (file renames, quick edits don't need 10K thinking tokens)

**What the optimizer does**: Reports current value. Surfaces patterns where thinking budget was clearly wasted (e.g., 10K thinking tokens on a file rename). Does NOT suggest reducing it for complex reasoning tasks.

---

### 26. CLAUDE_CODE_MAX_OUTPUT_TOKENS (default: 16,384, max: 128,000)
**Target**: Understand output token budget

**What it is**: Maximum tokens Claude generates per response. If you hit truncation ("output was cut off"), this may need increasing. Higher values allow longer responses but increase cost.

**Actions**:
- [ ] Check current value in settings.json env block
- [ ] Note if you've seen truncation issues
- [ ] Default is fine for most users

**What the optimizer does**: Reports current value. Notes if user has hit truncation issues.

---

### 27. MAX_MCP_OUTPUT_TOKENS (default: 25,000)
**Target**: Understand MCP tool output limits

**What it is**: Maximum tokens a single MCP tool call can return. Tools returning max-length output may be sending more data than needed.

**Actions**:
- [ ] Check if any MCP tools consistently return very large outputs
- [ ] Consider if those tools have filtering options to reduce output size

**What the optimizer does**: Reports current value. Flags MCP tools that consistently return near-max output (potential tuning opportunity).

---

### 28. BASH_MAX_OUTPUT_LENGTH (default: system)
**Target**: Control bash output token consumption

**What it is**: Limits how much stdout/stderr from Bash tool calls gets captured into context. Verbose test runners or build logs can dump thousands of tokens.

**Actions**:
- [ ] Check if bash output is frequently truncated (might need raising)
- [ ] Check if verbose commands are filling context unnecessarily (might need lowering)

**What the optimizer does**: Reports current value. Flags if bash output appears frequently truncated.

---

### 29. ENABLE_TOOL_SEARCH (default: auto, active above threshold)
**Target**: Verify Tool Search is active (85% MCP savings)

**What it is**: Tool Search defers MCP tool definitions until actually needed. Instead of loading 300-850 tokens per tool upfront, only ~15 tokens per tool name is loaded. This is the single biggest MCP optimization.

**Actions**:
- [ ] Verify Tool Search is active in your session (look for ToolSearch in available tools)
- [ ] If not active, check Claude Code version and tool count threshold
- [ ] `ENABLE_TOOL_SEARCH=auto:N` sets the threshold to N tools (default auto)

**What the optimizer does**: Reports if Tool Search is active. If not, explains the 85% savings from enabling it. This is flagged as HIGH PRIORITY if missing.

---

### 30. CLAUDE_CODE_DISABLE_AUTO_MEMORY (default: not set)
**Target**: Audit auto-memory content quality, not the feature itself

**What it is**: Auto-memory writes learnings to MEMORY.md automatically. This is a valuable feature. The optimization target is the CONTENT it produces, not the feature.

**Actions**:
- [ ] Check MEMORY.md for duplicate entries (auto-memory sometimes writes the same insight twice)
- [ ] Check for entries that duplicate CLAUDE.md content
- [ ] Check for stale entries (rules that no longer apply)
- [ ] Condense verbose entries to current rule only

**What the optimizer does**: Audits MEMORY.md for duplicates, stale entries, and redundancy with CLAUDE.md. Cleans up the CONTENT. Does NOT suggest disabling the feature.

---

### 31. CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING (default: not set)
**Target**: Understand what adaptive thinking does

**What it is**: When set, disables Claude's ability to automatically adjust thinking depth based on task complexity. Normally Claude uses more thinking for hard problems and less for simple ones.

**Actions**:
- [ ] Check if set in settings.json env block
- [ ] Understand that disabling this means fixed thinking budget regardless of task

**What the optimizer does**: Reports if set. Explains what it does. No recommendation to disable.

---

### 32. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (default: ~83%)
**Target**: Control when auto-compaction triggers

**What it is**: Auto-compact triggers when context reaches this percentage of the window. The system default (~83%) is reasonable but power users who manage /compact manually may want to adjust or disable entirely.

**Already covered in item 14**, but listed here for completeness as a settings.json env var. Set to 70 for earlier compaction (better quality), or disable auto-compact entirely and manage /compact manually (recovers ~33K tokens of buffer space).

---

## ANTI-PATTERNS

- Don't add content to CLAUDE.md without asking "Can this be a skill or reference file?"
- Don't duplicate rules between CLAUDE.md and MEMORY.md
- Don't archive skills to subfolder inside skills/ (still loads)
- Don't use Opus agents for file reading (haiku is 60x cheaper, see Model Routing section)
- Don't wait for auto-compact (do it manually at 70%)
- Don't paste full error logs (paste relevant lines only)
- Don't run tests through Claude (run locally, paste failures only)
- Don't dump everything in global CLAUDE.md (project-specific goes in project CLAUDE.md)
- Don't leave extended thinking on for simple tasks (output tokens cost more)
- Don't assume MCP overhead is huge (Tool Search defers definitions since Jan 2026)
- Don't quote dollar savings to subscription users (talk context budget, not money)
