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

If no hooks exist, offer to create starter hooks config from `examples/hooks-starter.json`.

**Why hooks matter**:
- **PreCompact**: Guides what Claude preserves during context compaction (prevents loss of critical context)
- **PostToolUse**: Triggers auto-formatters, saving output tokens on style explanations

Show the JSON template, explain each hook, and ask user before creating.

---

## 4G: CLAUDE.md Cache Structure

If CLAUDE.md has volatile content mixed with stable content, restructure for prompt caching.

See `examples/claude-md-optimized.md` for the pattern.

**Why**: Prompt caching caches prefixes. If stable content comes first, it stays cached (90% cheaper). Volatile content at the end doesn't break the cache prefix.

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
- [ ] Token monitoring tools recommended (ccusage, /context)

---

## Anti-Patterns

| DON'T | DO |
|-------|-----|
| Make changes without user approval | Ask before implementing |
| Delete files | Always archive to `~/.claude/_backups/` |
| Claim "this might save tokens" | MEASURE IT (use scripts/measure.py) |
| Skip verification step | Run Phase 5 after every change |
| Use Opus agents for file reading (waste) | Use haiku for data gathering |
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
