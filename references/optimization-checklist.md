# Token Optimization Checklist

Comprehensive checklist of ALL optimization techniques.

---

## QUICK WINS (< 1 hour, save 2,000-5,000 tokens/msg)

### 1. CLAUDE.md Consolidation
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

### 2. MEMORY.md Deduplication
**Target**: Remove 100% overlap with CLAUDE.md

**Actions**:
- [ ] Remove Key Paths if already in CLAUDE.md (choose ONE source of truth)
- [ ] Remove personality spec if already in CLAUDE.md
- [ ] Condense verbose operational history to current rule only
- [ ] Keep only: Learnings, corrections, habit tracking

**Expected savings**: 400-800 tokens/msg

---

### 3. .claudeignore Creation
**Target**: Block unnecessary files from context injection

See `examples/claudeignore-template` for a ready-to-use template.

Copy to `~/.claude/.claudeignore` (global) or `.claudeignore` (project-level).

**Why**: Prevents system reminders from injecting modified files you don't need. Security + token savings.

**Expected savings**: Varies (500-2,000 tokens/msg if you frequently edit media/deps)

---

### 4. Archive Unused Skills
**Target**: Reduce skill menu overhead

**Actions**:
- [ ] Identify duplicate skills (similar names/descriptions)
- [ ] Identify unused domain skills
- [ ] Create backup: `mkdir -p ~/.claude/_backups/skills-archived-$(date +%Y%m%d)`
- [ ] Move unused skills: `mv ~/.claude/skills/[skill-name] ~/.claude/_backups/skills-archived-*/`

**CRITICAL**: Subfolder `_archived/` INSIDE skills/ still loads as namespace. Must move OUTSIDE skills/ entirely.

**Expected savings**: ~100 tokens per skill archived

---

### 5. Trim Commands
**Target**: Reduce command menu overhead

**Actions**:
- [ ] Identify rarely-used commands
- [ ] Merge similar commands if possible
- [ ] Archive to `~/.claude/_backups/commands-archived-$(date +%Y%m%d)/`

**Expected savings**: ~50 tokens per command archived

---

## MEDIUM EFFORT (1-3 hours, save 2,000-5,000 tokens)

### 6. MCP Server Audit
**Target**: Disable broken/unused MCP servers

**How to audit**:
1. Check Desktop config: `~/Library/Application Support/Claude/claude_desktop_config.json`
2. Check Claude Code config: `~/.config/claude/user_config.json`
3. Check claude.ai account (web): Settings -> Integrations
4. Count deferred tools in current session (system prompt shows count)

**Identify**:
- [ ] Broken servers (auth failed, deprecated APIs)
- [ ] Duplicate servers (same tools from multiple sources)
- [ ] Rarely-used servers (domain-specific, >10 tools, used <1x/month)

**Expected savings**: ~100 tokens per server disabled (varies by tool count)

---

### 7. Install qmd for Local Search
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

### 8. Migrate CLAUDE.md Content to Skills
**Target**: Move domain-specific content to on-demand loading

**Pattern**: Skills load ~100 tokens at startup (frontmatter only). Full content loads on-demand. This is 98% cheaper than CLAUDE.md for same content.

**How**:
1. Create skill: `mkdir ~/.claude/skills/[name]`
2. Write SKILL.md with content
3. Remove from CLAUDE.md
4. Add 1-line reference in CLAUDE.md: "Full config: see /[name] skill"

**Expected savings**: ~500-1,000 tokens (depends on volume moved)

---

## DEEP OPTIMIZATION (3+ hours, power users)

### 9. Agent Model Selection (Persistent Rule)
**Target**: 50-60% cost reduction on automation/agents

**Add to CLAUDE.md or MEMORY.md**:
```markdown
## Agent Model Selection (MANDATORY)

When dispatching subagents, ALWAYS specify model parameter:

| Task Type | Model | Example |
|-----------|-------|---------|
| File reading, data gathering, counting, scanning | haiku | Email scan, calendar read, file list |
| Analysis, synthesis, writing, moderate reasoning | sonnet | Summarize findings, draft content, prioritize |
| Architecture, novel reasoning, complex debugging | opus | System design, hard bugs, strategic planning |

**Default**: If in doubt, start with haiku. Upgrade to sonnet/opus only if task fails.
```

**Expected savings**: 50-60% on automation costs

---

### 10. Session Folder Pattern (Architecture Change)
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

### 11. Progressive Disclosure Pattern
**Target**: Load context incrementally, not all at once

**Pattern**:
- Phase 1: Load minimal context (identity, current state)
- Phase 2: Ask clarifying questions
- Phase 3: Load relevant context based on answers
- Phase 4: Execute

**Expected savings**: 30-50% on large context tasks

---

## BEHAVIORAL CHANGES (Free, high impact over time)

### 12. /compact and /clear Hygiene
**Target**: 40-82% savings on long sessions

**Rules**:
- [ ] Run `/compact` at 70% context (don't wait for auto-compact at 75-83%)
- [ ] Run `/compact` at natural breakpoints (after commit, after feature)
- [ ] Run `/clear` between unrelated topics
- [ ] Check `/context` periodically to know your fill level

**Community data**: One user saved $50 on a 3-day project by compacting every 15 messages.

---

### 13. Batch Requests
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

### 14. Skip Confirmations
**Target**: Reduce message count

**DON'T**:
```
User: "Thanks!"
Claude: "You're welcome!"
(Claude just re-sent full context for a courtesy)
```

**Community stat**: One analysis showed 40% of tokens were confirmations and "looks good" messages.

---

### 15. Test Locally, Not Through Claude
**Target**: Avoid expensive test output in chat

**DON'T**: Let Claude run tests and dump 5,000 tokens of output.

**DO**: Run `pytest tests/` locally and paste any failures.

**Expected savings**: 5,000-50,000 tokens per test run

---

## ADVANCED (Power Users)

### 16. Prompt Caching Exploitation
**Target**: Reduce cost on repeated content

**How it works**: Claude caches prefixes >1024 tokens with 5 min TTL. Subsequent requests with same prefix are 90% cheaper.

**Optimization**: Structure CLAUDE.md so stable sections come FIRST, volatile sections LAST.

See `examples/claude-md-optimized.md` for the pattern.

---

### 17. Multi-Project CLAUDE.md Strategy
**Target**: Different configs for different projects

**Pattern**:
- Global `~/.claude/CLAUDE.md`: Identity, personality, core rules (~500 tokens)
- Project `[repo]/CLAUDE.md`: Project-specific context, tech stack, conventions (~300 tokens)

**Why**: Global CLAUDE.md loads for ALL projects. Project CLAUDE.md loads only in that directory. Keep global minimal.

---

### 18. Hook-Based Optimizations
**Target**: Pre/post-session token management

See `examples/hooks-starter.json` for a ready-to-use template.

**Key hooks**:
- **PreCompact**: Guide compaction to preserve critical context
- **PostToolUse**: Trigger auto-formatters, save output tokens

---

## MONITORING & MEASUREMENT

### 19. Baseline Your Usage

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

### 20. Regular Audits
**Quarterly** (every 3 months):
- [ ] Re-run `/token-optimizer` (skills accumulate, CLAUDE.md grows back)
- [ ] Re-check MCP servers (you add new ones)

**Why**: Optimization entropy. Without discipline, configs grow back to original size.

---

## TOKEN FLOW REFERENCE

**Every message loads this stack**:
```
├─ Core system prompt:          ~2,800 tokens  (fixed)
├─ Built-in tools (18):         ~9,400 tokens  (fixed)
├─ MCP tool definitions:        ~100 tokens x deferred tools
├─ Skills frontmatter:          ~100 tokens x skill count
├─ Commands frontmatter:        ~50 tokens x command count
├─ CLAUDE.md (global):          Variable (target: <800)
├─ Project CLAUDE.md:           Variable (target: <300)
├─ MEMORY.md:                   Variable (target: <600)
├─ System reminders:            ~2,000 tokens (auto-injected, variable)
└─ Message + history:           Variable
```

**Baseline (well-optimized)**: ~15-20K tokens first message
**Typical (unoptimized)**: ~30-35K tokens first message

---

## WORKED EXAMPLE: Power User Optimization

**Before** (typical power user setup):
- CLAUDE.md: ~80 lines, ~1,500 tokens
- MEMORY.md: ~50 lines, ~1,400 tokens
- Skills: 50+ (plus plugin/command skills)
- MCP: ~180 deferred tools
- Estimated overhead: ~31K tokens/msg

**Actions taken**:
1. CLAUDE.md: ~80 -> ~50 lines (45% reduction, ~550 tokens saved)
2. MEMORY.md: ~50 -> ~35 lines (57% reduction, ~550 tokens saved)
3. Commands: 25 -> 5 (20 archived, ~1,000 tokens saved)
4. .claudeignore: Created (security + token savings)
5. qmd: Installed for local search
6. Agent model selection: Added to automation skills

**After**:
- Estimated overhead: ~24K tokens/msg
- Savings: ~7K tokens/msg (23% reduction)
- Daily automation cost: 50-60% reduction (haiku for data agents)

---

## ANTI-PATTERNS

- Don't add content to CLAUDE.md without asking "Can this be a skill instead?"
- Don't duplicate rules between CLAUDE.md and MEMORY.md
- Don't archive skills to subfolder inside skills/ (still loads)
- Don't use Opus agents for file reading (haiku is 5x cheaper)
- Don't wait for auto-compact (do it manually at 70%)
- Don't paste full error logs (paste relevant lines only)
- Don't run tests through Claude (run locally, paste failures only)
