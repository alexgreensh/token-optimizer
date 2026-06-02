# Coach Patterns: Architecture Patterns and Anti-Patterns

Reference file for Token Coach. Loaded for options a/b/d (config optimization).

---

## Architecture Patterns (The "What To Do" Layer)

### Pattern 1: Skill Design for Minimal Overhead
- SKILL.md body under 500 lines (Anthropic's recommendation)
- Description field under 200 characters, but trigger-rich (helps Claude suggest the skill)
- Heavy content in references/ (zero cost until explicitly read)
- Frontmatter is ~60-100 tokens per skill, loaded every session
- Full SKILL.md body loads on invocation (2K-5K tokens)
- References load only when the skill reads them (zero until then)

### Pattern 2: CLAUDE.md Layering
- Global CLAUDE.md (~/.claude/CLAUDE.md): Identity, critical rules, key paths, model routing. Target <800 tokens.
- Project CLAUDE.md (<project>/.claude/CLAUDE.md): Project-specific conventions, tech stack, file layout. Keep separate.
- CLAUDE.local.md: Personal overrides, not committed to repo.
- Rule: if content only applies to specific tasks, it belongs in a skill, not CLAUDE.md.
- Every line in CLAUDE.md costs tokens on EVERY message, EVERY session, in EVERY subagent.

### Pattern 3: MCP Server Consolidation
- Fewer servers with more tools > many servers with few tools
- Each server adds ~50-100 tokens of instruction text per message (even with deferred tools)
- Disable servers you don't use in CLI (re-enable anytime in settings.json)
- Cloud-synced servers (ENABLE_CLAUDEAI_MCP_SERVERS) may add tools you don't need in CLI
- Check for duplicate tools across servers and plugins

### Pattern 4: Rules Scoping
- Always use `paths:` frontmatter to scope rules to specific directories
- Rules without paths: load EVERY message, same cost as CLAUDE.md
- Audit unscoped rules: are they truly global, or just lazily unscoped?
- Consolidate overlapping rules into fewer files

### Pattern 5: Memory Hygiene
- MEMORY.md auto-loads first 200 lines every session
- Lines beyond 200 are truncated but still counted toward your window
- Move detailed notes to topic-specific files in memory/ directory
- Keep MEMORY.md as an index of high-signal, frequently-referenced items
- Dedup against CLAUDE.md (common source of waste)

### Pattern 6: Import Auditing
- @imports in CLAUDE.md pull entire files into every message
- Each @path/to/file.md adds that file's FULL token count to every message
- Grep CLAUDE.md for @ patterns, resolve paths, add up tokens
- Move large imports to skills or reference files that load on demand

### Pattern 7: Frontmatter Discipline
- Skill descriptions under 200 chars (80 chars is the sweet spot)
- Skill names under 30 chars
- Description should be a trigger phrase, not a paragraph
- Detailed usage instructions belong in SKILL.md body, not frontmatter

### Pattern 8: Progressive Loading via Skills
- Content in CLAUDE.md: costs tokens every message
- Same content as a skill: ~100 tokens in menu, full cost only on invocation
- That's 97% savings on messages that don't invoke the skill
- Rule of thumb: if content is only relevant to specific tasks, make it a skill
- Split heavy skill content into references/ (Tier 3, zero until read)

---

## Anti-Patterns (Common Mistakes with Fixes)

### The 50-Skill Trap
**Problem**: 50+ skills installed. Menu overhead: 5,000+ tokens every session.
**Symptoms**: Slow startup feel. Context fills faster than expected.
**Fix**: Archive unused skills to ~/.claude/_backups/skills-archived/. A subfolder inside skills/ still loads as a namespace, so move OUTSIDE skills/ entirely. Review with `measure.py trends` to see which skills you actually invoke.
**Savings**: ~100 tokens per archived skill per session.

### The Opus Addiction
**Problem**: 70%+ of token usage on Opus when Sonnet/Haiku would suffice.
**Symptoms**: High costs, hitting rate limits, budget burns fast.
**Fix**: Add model routing to CLAUDE.md: "Default subagents to haiku for data gathering, sonnet for analysis. Opus only for complex reasoning."
**Savings**: 50-75% cost reduction on multi-agent workflows. Same context tokens, much less spend.

### The CLAUDE.md Novel
**Problem**: 200+ lines in global CLAUDE.md. 2,000+ tokens loading every message.
**Symptoms**: Config overhead dominates. Less room for actual work.
**Fix**: Progressive disclosure. Move workflows to skills. Move standards to reference files. Move gotchas to MEMORY.md. Target <800 tokens.
**Savings**: 400-1,200+ tokens per message.

### The Import Avalanche
**Problem**: Multiple @imports in CLAUDE.md pulling large files every message.
**Symptoms**: Unexpectedly high baseline token count. CLAUDE.md "feels small" but loads heavy.
**Fix**: Audit @import paths. Move large imports to skills. Keep only tiny, critical imports.
**Savings**: Varies wildly. Some users have 5,000+ tokens in forgotten imports.

### The MCP Sprawl
**Problem**: 15+ MCP servers configured, most rarely used.
**Symptoms**: High tool count in /context. Slow tool search. Context pressure.
**Fix**: Audit settings.json. Disable unused servers (can re-enable anytime). Check for cloud-synced servers from claude.ai.
**Savings**: ~50-100 tokens per disabled server (instruction overhead) plus reduced tool search noise.

### The Stale Memory
**Problem**: MEMORY.md duplicates CLAUDE.md content, or contains outdated entries.
**Symptoms**: Wasted tokens on redundant info. Potentially conflicting instructions.
**Fix**: Dedup MEMORY.md against CLAUDE.md. Remove resolved issues, completed migrations, one-time setup notes. Move verbose entries to topic files.
**Savings**: Depends on duplication level. Commonly 200-600 tokens.

### The Singleton Session
**Problem**: One long session for everything. Never uses /clear or /compact.
**Symptoms**: Quality degrades over time. Compaction happens unexpectedly. Hallucinations increase.
**Fix**: Session hygiene. /compact at 50-70%. /clear between unrelated topics. Fresh session for fresh work.
**Savings**: Not token savings per se, but dramatically better output quality.

### The Unscoped Rules
**Problem**: All rules in .claude/rules/ lack paths: frontmatter.
**Symptoms**: Backend rules load when working on frontend. Testing rules load during docs work.
**Fix**: Add paths: frontmatter to scope rules to relevant directories.
**Savings**: Proportional to rule size. Can be hundreds of tokens per message.
