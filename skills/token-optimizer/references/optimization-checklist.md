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

### 2. Agent Model Selection Rule (5 minutes)
**Target**: 50-60% cost reduction on automation/agents

**Add to CLAUDE.md or MEMORY.md**:
```markdown
When dispatching subagents, ALWAYS specify model parameter:
- haiku: file reading, data gathering, counting, scanning
- sonnet: analysis, synthesis, writing, moderate reasoning
- opus: architecture, novel reasoning, complex debugging
Default to haiku. Upgrade only if task requires it.
```

**Why quick win**: One line in CLAUDE.md, 50-60% savings on every multi-agent workflow. This saves more than most config changes combined.

**Expected savings**: 50-60% on automation costs

---

### 3. CLAUDE.md Consolidation
**Target**: Slim to <800 tokens (~50-60 lines)

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

### 5. .claudeignore Creation
**Target**: Block unnecessary files from context injection

See `examples/claudeignore-template` for a ready-to-use template.

Copy to `~/.claude/.claudeignore` (global) or `.claudeignore` (project-level).

**Why**: Prevents system reminders from injecting modified files you don't need. Security + token savings.

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
**Target**: 40-82% savings on long sessions

**Rules**:
- [ ] Run `/compact` at 70% context (don't wait for auto-compact at 75-83%)
- [ ] Run `/compact` at natural breakpoints (after commit, after feature)
- [ ] Run `/clear` between unrelated topics
- [ ] Check `/context` periodically to know your fill level

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

### 18. Prompt Caching Exploitation (Theoretical)
**Target**: Reduce cost on repeated content

**How it works on the API**: Claude caches prefixes >1024 tokens with 5 min TTL. Subsequent requests with same prefix are 90% cheaper.

**In Claude Code**: Whether prompt caching applies to CLAUDE.md content within Claude Code's internal prompt assembly is **unconfirmed by Anthropic**. The optimization is low-effort (just reorder CLAUDE.md sections), so it is worth doing even speculatively.

**Optimization**: Structure CLAUDE.md so stable sections come FIRST, volatile sections LAST.

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

## MONITORING & MEASUREMENT

### 21. Baseline Your Usage

```bash
# Measure current state
python3 ~/.claude/token-optimizer/scripts/measure.py report

# Save snapshot before optimizing
python3 ~/.claude/token-optimizer/scripts/measure.py snapshot before

# After optimization
python3 ~/.claude/token-optimizer/scripts/measure.py snapshot after

# Compare
python3 ~/.claude/token-optimizer/scripts/measure.py compare
```

Also track with `/cost` at end of each session and `npx ccusage@latest daily` for historical data.

---

### 22. Regular Audits
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

**Baseline (well-optimized)**: ~21K tokens first message
**Typical (unoptimized)**: ~27K tokens first message
**Note**: Pre-Tool-Search (2025), unoptimized setups reached 40-80K+

---

## WORKED EXAMPLE: Power User Optimization

**Before** (typical power user setup, Tool Search active):
- Core system + built-in tools: ~15,000 tokens (fixed)
- MCP (ToolSearch + ~80 deferred tools): ~1,700 tokens
- Skills (~40): ~4,000 tokens
- Commands (~20): ~1,000 tokens
- CLAUDE.md: ~1,500 tokens
- MEMORY.md: ~1,200 tokens
- System reminders: ~2,000 tokens
- **Total overhead: ~26,400 tokens/msg (13% of 200K)**

**Config changes** (what the optimizer implements):
1. CLAUDE.md: 1,500 -> 600 tokens (progressive disclosure)
2. MEMORY.md: 1,200 -> 400 tokens (dedup with CLAUDE.md)
3. Skills: 40 -> 25 (15 archived, ~1,500 tokens saved)
4. Commands: 20 -> 10 (10 archived, ~500 tokens saved)
5. MCP: removed 3 unused servers (~450 tokens saved)
6. .claudeignore created (~1,000 tokens saved from system reminders)
- **Config savings: ~5,350 tokens/msg (20% reduction)**
- **After: ~21,050 tokens/msg (11% of 200K)**

**Behavioral changes** (what the optimizer teaches):
- Agent model selection (haiku for data): 50-60% on automation
- /compact at 70%: 40-82% on long sessions
- Extended thinking awareness: variable, potentially largest single factor
- Batching requests: 2-3x on multi-step tasks
- /clear between topics: prevents stale context accumulation
- **Behavioral savings: estimated 30-50% daily cost reduction on top of config**

The config changes shrink your per-message overhead. The behavioral changes compound across every message, every session, every day. Together they are the full picture.

---

## ANTI-PATTERNS

- Don't add content to CLAUDE.md without asking "Can this be a skill or reference file?"
- Don't duplicate rules between CLAUDE.md and MEMORY.md
- Don't archive skills to subfolder inside skills/ (still loads)
- Don't use Opus agents for file reading (haiku is 5x cheaper)
- Don't wait for auto-compact (do it manually at 70%)
- Don't paste full error logs (paste relevant lines only)
- Don't run tests through Claude (run locally, paste failures only)
- Don't dump everything in global CLAUDE.md (project-specific goes in project CLAUDE.md)
- Don't leave extended thinking on for simple tasks (output tokens cost more)
- Don't assume MCP overhead is huge (Tool Search defers definitions since Jan 2026)
- Don't quote dollar savings to subscription users (talk context budget, not money)
