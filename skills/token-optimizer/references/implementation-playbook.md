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
   - **Tier 1 (always loaded, ~300 lines / ~4,500 tokens)**: Identity, critical rules, key paths
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
# Backup first (handles multiple projects)
for memfile in ~/.claude/projects/*/memory/MEMORY.md; do
  [ -f "$memfile" ] || continue
  projname=$(basename "$(dirname "$(dirname "$memfile")")")
  cp "$memfile" "$HOME/.claude/_backups/MEMORY-${projname}.pre-optimization-$(date +%Y%m%d).md"
done
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

**DEPENDENCY CHECK (mandatory before archival)**:
Before archiving any skill, search for references to it:
1. `grep -r "[skill-name]" ~/.claude/CLAUDE.md ~/.claude/rules/ ~/.claude/skills/` to find @imports or instructions that reference it
2. Check if any MCP server tools depend on the skill (e.g., a skill that configures API keys used by MCP tools)
3. Warn the user: "Archiving [skill] may break [dependent] which references it. Archive anyway?"
If dependencies are found, list them explicitly and get confirmation.

---

## 4D: File Exclusion Rules

If missing, add `permissions.deny` rules to `.claude/settings.json` (project-level) or `~/.claude/settings.json` (global). See `examples/permissions-deny-template.json` for a starter template.

```json
{
  "permissions": {
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)",
      "Read(./node_modules/**)",
      "Read(./dist/**)",
      "Read(./build/**)",
      "Read(./**/*.log)"
    ]
  }
}
```

**Why**: Files matching deny patterns are excluded from file discovery, search, and read operations. This is the officially supported approach (replaces deprecated `ignorePatterns`). Security + token savings.

**SIDE EFFECT WARNING (mandatory before applying)**:
Deny rules affect ALL tools in ALL sessions. Before applying:
1. **Database deny rules** (`*.db`, `*.sqlite`): Will break any skill or MCP server that reads SQLite databases (e.g., session memory tools, local search indexes, WhatsApp MCP). Only add these at project level, never globally, unless you are certain no tools need database access.
2. **Credential deny rules** (`.env`, `*.key`, `*.pem`): Will prevent Claude from reading these files. If any skill reads API keys from `.env` at runtime, it will fail silently. This is usually DESIRED for security, but warn the user.
3. **Global vs project-level**: Recommend project-level (`./claude/settings.json`) over global (`~/.claude/settings.json`). Global rules affect every project and are harder to debug when something breaks.

Always tell the user: "These deny rules will prevent Claude from reading matching files in ALL sessions. If any of your skills or MCP servers need access to these file types, they will stop working. Apply at project level first to test."

---

## 4E: MCP Server Guidance

Don't auto-disable MCP servers (requires manual config edits). Instead, provide:

```
To disable these MCP servers:

1. Edit config file:
   - Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json
   - Claude Code: ~/.claude/settings.json

2. Remove or comment out these entries:
   - [server1]: [reason]
   - [server2]: [reason]

3. Restart Claude

Estimated savings: ~X tokens
```

**CONSEQUENCE CHECK (mandatory before suggesting MCP disabling)**:
1. Search CLAUDE.md, skills, and rules for references to the server's tool names (e.g., `mcp__[server]__*`)
2. If ANY skill or instruction references the server's tools, warn: "Disabling [server] will break [skill/instruction] which uses [tool_name]. Disable anyway?"
3. For servers with deferred tool loading: even if no hardcoded references exist, the user may invoke them conversationally. Ask: "Do you ever use [server] tools directly in conversation?" before recommending removal.

---

## 4F: Hooks Configuration

### SessionEnd (measure.py collect)

Handled in Phase 0 setup. If the user skipped it there, offer again here:

```bash
python3 $MEASURE_PY check-hook
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
   | CLAUDE_AUTOCOMPACT_PCT_OVERRIDE | not set | ~98%    | Auto-removed if found (undocumented, semantics inverted) |
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
   - Run `python3 $MEASURE_PY trends --json --days 30` if trends DB exists
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

## 4O: Version-Aware Optimizations (v2.1.83-86)

Check the user's Claude Code version (`claude --version`) and apply version-specific guidance:

**v2.1.86+: Read tool native deduplication**
- Claude Code deduplicates unchanged re-reads natively (compact line-number format)
- If Token Optimizer's `read_cache.py` PreToolUse hook is installed, it is redundant on v2.1.86+
- Check: run `python3 $MEASURE_PY read-cache-stats --session current`. If "blocks: 0" for recent sessions, native dedup is handling it.
- Action: Disable the read-cache hook (`TOKEN_OPTIMIZER_READ_CACHE=0` in settings.json env block) to eliminate hook process overhead

**v2.1.86+: @ file mentions no longer JSON-escaped**
- Raw string content in file mentions saves tokens automatically
- No action needed. Note in findings: ~5-15% savings on file-heavy sessions (free upgrade)

**v2.1.84+: Idle-return /clear prompt conflicts with Smart Compaction**
- Claude Code prompts to /clear after 75+ min idle
- /clear destroys Smart Compaction checkpoints. /compact preserves them.
- Action: If Smart Compaction is active, tell the user to choose /compact instead of /clear when prompted. Add to CLAUDE.md: "After idle, use /compact not /clear (preserves checkpoints)."

**v2.1.83+: Auto-compact circuit breaker**
- Auto-compaction stops after 3 consecutive failures and does not retry until a new session
- Smart Compaction's PreCompact hook will not fire after the circuit breaker trips
- Action: If user reports "compaction stopped working mid-session", explain the circuit breaker. Workaround: run `/compact` manually (resets the counter)

**v2.1.85+: Conditional hooks with `if` field**
- Token Optimizer's hooks can use `if` filters to skip execution when not needed
- Action: Add `"if": "tool_uses"` to the quality-cache PostToolUse hook to skip on messages with no tool calls

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
- [ ] Version-aware optimizations checked (4O)
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
