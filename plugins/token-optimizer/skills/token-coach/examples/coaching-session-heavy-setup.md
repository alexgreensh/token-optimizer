# Example: Coaching Session for a Heavy Setup

User chose option (b): Existing project feels sluggish / context fills too fast.

---

## Coach Output

**Coach**: Found the weight. Your setup uses ~32,400 tokens at startup. That's 16.2% of your 200K window before you type anything. Combined with the ~45K autocompact buffer, you're starting each session with only ~122,600 usable tokens.

The top 3 overhead sources:
1. **54 skills**: ~5,400 tokens in the startup menu. You've got the 50-Skill Trap going on.
2. **CLAUDE.md**: 2,180 tokens. That's over the 800-token target. The CLAUDE.md Novel pattern.
3. **8 MCP servers**: ~1,200 tokens between tool names and server instructions.

When does it feel slow? Early in sessions, after a few messages, or during multi-agent work?

**User**: After about 10-15 messages. Compaction keeps hitting and I lose context.

**Coach**: That tracks. You're starting at 16.2% used. Each message adds conversation history plus file reads. By message 10-15, you're likely at 70%+ where quality degrades and compaction triggers.

Two things would help most:

First, your skills: 54 installed, but in the last 30 days you've only invoked 19 of them. Those 35 unused skills cost ~3,500 tokens per session for zero benefit. Move them to ~/.claude/_backups/skills-archived/. You can always move them back.

Second, your CLAUDE.md has a 400-line section about coding standards that should be a skill. As a skill, it'd cost ~100 tokens in the menu instead of ~2,000 tokens every message. That's a 95% reduction on that content alone.

Together: ~5,500 tokens recovered per session. That buys you roughly 3-4 more messages before compaction.

**User**: Which skills should I archive?

**Coach**: Looking at your trends data, these 35 skills haven't been invoked in 30 days: [list]. Before archiving, scan for any that are seasonal (deploy skills you use monthly, tax-season skills, etc.). Keep those. Archive the rest.

The quick process:
```bash
mkdir -p ~/.claude/_backups/skills-archived
mv ~/.claude/skills/[skill-name] ~/.claude/_backups/skills-archived/
```

## Action Plan

1. **Archive 35 unused skills** (Quick Win): Move to ~/.claude/_backups/skills-archived/. Recovers ~3,500 tokens/session.
2. **Extract coding standards from CLAUDE.md to a skill** (Medium): Create a coding-standards skill. Recovers ~1,900 tokens/message.
3. **Review 8 MCP servers** (Medium): Disable servers you don't use in CLI. Ask yourself which you actually use in conversation before disabling.
4. **Use /compact at 50-70%** (Habit): Don't wait for auto-compact. Run /compact proactively around message 8-10 given your current setup.
5. **Run /token-optimizer for full audit**: Get the before/after measurement and automated fixes.
