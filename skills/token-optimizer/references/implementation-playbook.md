# Implementation Playbook

Detailed implementation steps for Phase 4 of the Token Optimizer. The orchestrator dispatches to these based on user choice.

---

## 4A: CLAUDE.md Consolidation

```bash
# Backup first
cp ~/.claude/CLAUDE.md ~/.claude/_backups/CLAUDE.md.pre-optimization-$(date +%Y%m%d)
```

**Steps**:
1. Read current CLAUDE.md
2. Apply tiered architecture pattern:
   - **Tier 1 (always loaded, <800 tokens)**: Identity, critical rules, key paths
   - **Tier 2 (skill/command, loaded on-demand)**: Workflows, domain docs, tool configs
   - **Tier 3 (file reference, explicit only)**: Full guides, templates, detailed standards
3. Move Tier 2/3 content to skills or reference files
4. Output optimized version to `{COORD_PATH}/plan/CLAUDE.md.optimized`
5. Present diff to user for approval before overwriting

**Targets**:
- Remove content that belongs in skills/commands (workflows, detailed configs)
- Remove content that duplicates MEMORY.md
- Move reference content to on-demand files
- Condense personality/voice specs to 1-2 lines (full spec can live in MEMORY.md)

---

## 4B: MEMORY.md Deduplication

```bash
# Backup first
cp ~/.claude/projects/-Users-*/memory/MEMORY.md ~/.claude/_backups/MEMORY.md.pre-optimization-$(date +%Y%m%d)
```

**Steps**:
1. Read current MEMORY.md
2. Remove content that duplicates CLAUDE.md (choose ONE source of truth)
3. Condense verbose operational history to current rule only
4. Keep only: learnings, corrections, habit tracking
5. Output to `{COORD_PATH}/plan/MEMORY.md.optimized`
6. Present diff for approval

---

## 4C: Skill Archival

```bash
# Create backup location
mkdir -p ~/.claude/_backups/skills-archived-$(date +%Y%m%d)

# Move identified skills
mv ~/.claude/skills/[skill-name] ~/.claude/_backups/skills-archived-$(date +%Y%m%d)/
```

**CRITICAL**: A subfolder `_archived/` INSIDE `skills/` still loads as a namespace. Must move OUTSIDE `skills/` entirely.

List what will be archived, ask for confirmation before moving.

---

## 4D: .claudeignore Creation

If missing, create from the template in `examples/claudeignore-template`.

Copy to `~/.claude/.claudeignore` (global) or `.claudeignore` (project-level).

**Why**: Prevents system reminders from injecting modified files you don't need. Security + token savings.

---

## 4E: MCP Server Guidance

Don't auto-disable MCP servers (requires manual config edits). Instead, provide:

```
To disable these MCP servers:

1. Edit config file:
   - Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json
   - Claude Code: ~/.config/claude/user_config.json

2. Remove or comment out these entries:
   - [server1]: [reason]
   - [server2]: [reason]

3. Restart Claude

Estimated savings: ~X tokens
```

---

## 4F: Hooks Configuration

### SessionEnd (measure.py collect)

Handled in Phase 0 setup. If the user skipped it there, offer again here:

```bash
python3 ~/.claude/skills/token-optimizer/scripts/measure.py check-hook
```

If not installed, run `setup-hook --dry-run` to show the proposed change, then `setup-hook` after confirmation. The hook runs `measure.py collect --quiet` once per session close (~1 second, zero background processes).

### PreCompact

Guides what Claude preserves during context compaction (prevents loss of critical context). Offer `examples/hooks-starter.json` template for the PreCompact hook.

### PostToolUse

Triggers auto-formatters on file writes, saving output tokens on style explanations. Show the JSON template, explain the hook, and ask user before creating.

---

## 4G: CLAUDE.md Cache Structure

If CLAUDE.md has volatile content mixed with stable content, restructure for prompt caching.

See `examples/claude-md-optimized.md` for the pattern.

**Why**: Prompt caching caches prefixes. If stable content comes first, it stays cached (90% cheaper). Volatile content at the end doesn't break the cache prefix.

---

## 4H: Rules Cleanup

Scan `.claude/rules/` directory and optimize rule files.

**Steps**:
1. List all files in `~/.claude/rules/` (if directory exists)
2. For each rule file:
   - Measure token cost (lines x ~15 for prose, ~8 for YAML)
   - Check for `paths:` frontmatter (scoped vs always-loaded)
   - Compare content against other rules for duplication
3. Present findings:
   ```
   Rules Directory: X files, ~Y tokens total
   Always-loaded (no path scope): X files, ~Y tokens
   Path-scoped: X files

   Optimization opportunities:
   - [rule1.md] and [rule2.md]: 60% content overlap, merge candidate
   - [rule3.md]: No path scope but only applies to tests/ (add paths: ["tests/**"])
   - [rule4.md]: Stale (references deprecated tool)
   ```
4. Generate merge plan for duplicates
5. Execute after user approval (backup originals to `~/.claude/_backups/rules-$(date +%Y%m%d)/`)

---

## 4I: Settings Tuning

Audit settings.json env block and help user tune token-relevant variables.

**Steps**:
1. Read `~/.claude/settings.json` (and `settings.local.json` if exists)
2. Check env block for token-relevant variables (items 23-30 from checklist)
3. Present current vs default values with tradeoff explanations:
   ```
   Settings Audit:
   | Variable                        | Current | Default | Recommendation |
   |---------------------------------|---------|---------|----------------|
   | CLAUDE_AUTOCOMPACT_PCT_OVERRIDE | not set | ~83%    | Set to 70 for better quality |
   | MAX_THINKING_TOKENS             | not set | 10,000  | Default is fine |
   | ENABLE_TOOL_SEARCH              | auto    | auto    | Good (active)  |
   ```
4. Apply user-chosen changes to settings.json env block
5. Verify changes don't conflict with settings.local.json overrides

---

## 4J: Skill Description Tightening

Flag verbose skill frontmatter and generate tighter descriptions.

**Steps**:
1. Scan all skill SKILL.md files in `~/.claude/skills/`
2. Extract frontmatter `description:` field from each
3. Flag descriptions >200 characters (~50 tokens, twice the typical ~100 token budget)
4. Generate tighter alternatives:
   ```
   Verbose Descriptions:
   - morning (312 chars): "Your comprehensive daily briefing that covers email, calendar..."
     Suggested: "Daily briefing: email, calendar, tasks, partner updates"
   - code-review (285 chars): "Performs an in-depth code review analyzing..."
     Suggested: "Code review with style, security, and performance checks"
   ```
5. Apply approved changes to SKILL.md frontmatter (backup first)

**Note**: Only modify the `description:` field in frontmatter. Never touch skill body content.

---

## 4K: Compact Instructions Setup

Generate and add a compact instructions section to CLAUDE.md.

**Steps**:
1. Read current CLAUDE.md content
2. Identify what should survive compaction:
   - Current task context
   - Key file paths being modified
   - Active decisions and constraints
   - Error states and test results
3. Generate a compact instructions section:
   ```markdown
   ## Compact Instructions
   When compacting this conversation, always preserve:
   - Current task context and progress
   - File paths being modified and their state
   - Test results and error messages
   - Active constraints and decisions made
   - User preferences expressed in this session
   ```
4. Present to user for customization
5. Add to CLAUDE.md after approval (place near end, volatile section)

**Why**: Without compact instructions, compaction is generic and may lose critical session context. This is especially valuable for long sessions with complex multi-step work.

---

## 4L: Model Routing Setup

Add or improve model routing instructions in CLAUDE.md.

**Steps**:
1. Check if CLAUDE.md already has model routing instructions:
   - Grep for "haiku", "sonnet", "opus", "model" in CLAUDE.md
   - If found, assess specificity (detailed table vs vague "use appropriate models")
2. If no routing instructions exist, present the recommended snippet:
   ```markdown
   ## Agent Model Selection
   Default subagents to haiku. Upgrade only when task requires judgment:
   - haiku: file reading, data gathering, counting, scanning, formatting
   - sonnet: analysis, code review, writing, moderate reasoning
   - opus: architecture decisions, novel debugging, cross-cutting synthesis
   ```
3. If routing instructions exist, compare against actual model_mix usage:
   - Run `measure.py trends --json --days 30` if trends DB exists
   - Are subagents actually following the routing? (check model_mix percentages)
   - Is the instruction specific enough? ("default to haiku" vs detailed task-to-model table)
   - If >70% of tokens go to Opus despite routing instructions, the instructions may be too vague
4. Present routing snippet for user approval
5. Add to CLAUDE.md under the heading `## Agent Model Selection`. Place in the stable
   (static) section of CLAUDE.md, not the volatile tail (routing instructions rarely change,
   so they benefit from prompt caching). If no clear section exists, add after identity/rules.

**Targets**:
- 50-75% cost reduction on multi-agent workflows (API users)
- Rate limit conservation: Haiku has higher throughput limits than Opus
- Faster responses: Haiku is 3-5x faster than Opus for equivalent tasks
- No context window impact (routing doesn't change system prompt size)

---

## Quality Checklist

- [ ] Coordination folder created with manifest
- [ ] All 6 audit agents dispatched in parallel
- [ ] Synthesis completed with tiered plan
- [ ] Findings presented clearly (no jargon)
- [ ] User consent before any file changes
- [ ] Backups created before modifications
- [ ] Verification run after changes
- [ ] Results quantified (tokens + cost)
- [ ] Hooks configuration offered (PreCompact, PostToolUse)
- [ ] CLAUDE.md cache structure checked (static first, volatile last)
- [ ] Model routing instructions checked/added (4L)
- [ ] Token monitoring tools recommended (measure.py trends + SessionEnd hook, /context, /cost)

---

## Anti-Patterns

| DON'T | DO |
|-------|-----|
| Make changes without user approval | Ask before implementing |
| Delete files | Always archive to `~/.claude/_backups/` |
| Claim "this might save tokens" | MEASURE IT (use scripts/measure.py) |
| Skip verification step | Run Phase 5 after every change |
| Use opus for simple file reading | Match model to task: haiku for counting, sonnet for judgment, opus for synthesis |
| Present findings without next steps | Quantify everything (X tokens, Y%) |

---

## Error Handling

| Issue | Response |
|-------|----------|
| CLAUDE.md not found | "No global CLAUDE.md found. This is unusual but means zero overhead from it. Skip to skills audit." |
| MEMORY.md not found | "No MEMORY.md found. Skip this optimization." |
| No skills directory | "No skills found. Setup is minimal (good for tokens). Focus on CLAUDE.md + MCP." |
| Can't measure MCP tools | "Deferred tools not visible in this session. Skip MCP audit or check Desktop config manually." |
| User says 'skip verification' | "Noted. Skipping verification. Recommend running /cost before and after to measure actual savings." |
| Backup is empty/missing | "Backup directory appears empty. This could mean a fresh setup or a write failure. Proceeding without backup, but will not modify any files without explicit user confirmation for each change." |
| File write fails mid-implementation | "Write failed for [file]. Restoring from backup. Check disk space and permissions." Present the restore command from the Restoring Backups section. |
| Synthesis output missing | "Synthesis agent did not produce output. Showing raw audit findings instead." Present the individual audit files from `$COORD_PATH/audit/` for user review. |
