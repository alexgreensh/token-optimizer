# Coaching Scripts: Conversation Flows for Each Intake Option

Reference file for Token Coach. Provides conversation structure for each coaching scenario.

---

## General Coaching Principles

1. **Lead with their data, not your knowledge.** "You have 47 skills" lands harder than "Skills cost tokens."
2. **One insight at a time.** Present 1-2 findings, then ask a question. Don't dump everything.
3. **Name the anti-pattern.** "You've got the 50-Skill Trap" is memorable. "You have many skills" is forgettable.
4. **Quantify everything.** "~4,700 tokens" beats "a lot of tokens."
5. **Respect their workflow.** Some skills matter even if rarely invoked. Ask before recommending removal.
6. **End with action, not information.** Every coaching exchange should close with "Here's what to do next."

---

## Option A: Building Something New

### Opening
"Nice, building from scratch is the best time to get this right. Let me look at your current setup to see what your new project will inherit."

### Flow
1. **Show inherited overhead**: "Every project you create starts with [X tokens] of overhead from your global config. That's [Y%] of your 200K window spoken for before you write a single line of project code."
2. **Identify the big items**: Call out the top 3 overhead contributors from their current setup.
3. **Ask about the project**: "What are you building? (Skill, MCP server, multi-agent system, app with Claude integration?)"
4. **Give architecture advice based on answer**:
   - Skill: Point to Pattern 1 (Skill Design) and Pattern 7 (Frontmatter Discipline)
   - MCP server: Point to Pattern 3 (MCP Consolidation) and deferred loading
   - Multi-agent: Switch to agentic-systems.md patterns
   - App: Focus on CLAUDE.md layering and session management
5. **Recommend a token budget**: "For a skill, budget ~100 tokens frontmatter + 3K body + references as needed. For a CLAUDE.md section, budget under 800 tokens total."

### Closing
"Want me to run the full audit with /token-optimizer? Or are there specific architecture questions I can help with?"

---

## Option B: Existing Project Feels Sluggish

### Opening
"Let's figure out where the weight is. I've got your current measurements."

### Flow
1. **Show the headline number**: "Your setup uses [X tokens] at startup. That's [Y%] of your 200K window before you type anything."
2. **Identify the top 3 waste sources**: Use the coaching data patterns. Name the anti-patterns.
3. **Ask what they notice**: "When does it feel slow? Early in sessions? After a few messages? During multi-agent work?"
4. **Based on their answer**:
   - Early: Focus on startup overhead (skills, CLAUDE.md, MCP)
   - After a few messages: Focus on compaction and context management (/compact, /clear habits)
   - During multi-agent: Switch to agentic-systems.md patterns
5. **Prioritize fixes**: "The biggest win here is [X]. That alone would recover [Y tokens]. Want to tackle that first?"

### Closing
"I'd recommend running /token-optimizer for the full audit and automated fixes. It'll back up everything first and measure the before/after difference."

---

## Option C: Designing a Multi-Agent System

### Opening
"Multi-agent is where token optimization really matters. Every agent multiplies your config overhead. Let's design this right."

### Flow
1. **Ask about the architecture**: "Walk me through what you're building. How many agents? What does each one do?"
2. **Calculate the cost**: "With [N] agents, your config overhead alone is [N x overhead] tokens. That's before any of them read a single file."
3. **Review agent types**: "Which of these need write access? Which are just gathering data? The data-gathering ones should be Explore agents (Haiku, read-only)."
4. **Check for the common anti-patterns**:
   - Clone Army: All general-purpose agents
   - Skill Dump: Too many skills assigned to agents
   - Sequential Chain: Independent tasks not parallelized
   - Missing Handoff: No coordination folder
5. **Recommend architecture changes**: Specific agent types, model routing, coordination pattern.

### Closing
"Shall I also look at your overall setup? Slimming CLAUDE.md saves [X x N] tokens across all [N] agents."

---

## Option D: Quick Health Check

### Opening
"Quick scan coming up."

### Flow
1. **Token Health Score**: Show the composite score (0-100) and what drives it.
2. **Top 3 actions**: The three highest-impact things they can do right now, with estimated savings.
3. **One habit tip**: The single behavioral change that would help most.

### Closing
Keep it under 2 minutes of reading. "That's the quick view. For the deep dive, run /token-optimizer."

---

## Handling Follow-Up Questions

### "How do I know which skills to archive?"
"Run `python3 measure.py trends` to see which skills you've actually invoked in the last 30 days. Anything you haven't used is a candidate. Move to ~/.claude/skills/_archived/ and you can always move it back."

### "What should my CLAUDE.md look like?"
"Identity (1-2 lines), critical behavioral rules, key file paths, and model routing instructions. Everything else should be in skills, reference files, or MEMORY.md. Target: under 50 lines, under 800 tokens."

### "Is this costing me money or just context?"
"Both, but differently. Context overhead affects output quality (degrades past 50% fill). Token costs affect your bill. Skills cost tokens but only on invocation. CLAUDE.md costs tokens every single message. Multi-agent workflows multiply everything."

### "Should I use /compact or /clear?"
"Different tools for different situations. /compact preserves conversation context but may lose nuance. /clear gives you a completely fresh window. Rule of thumb: /compact within a topic, /clear between topics."

### "My setup is already minimal. What else can I do?"
"Focus on behavioral habits: batch related requests into one message, use /compact at 50-70% (don't wait for auto), use subagents for file-heavy research, and match your model to the task complexity."

### "My session feels degraded but context isn't full."
"That's a quality problem, not a quantity problem. Run `python3 measure.py quality current` to see what's rotting. Common culprits: stale file reads (you read a file, then edited it, but never re-read), bloated tool results nobody referenced again, and duplicate system reminders piling up. Smart Compaction can checkpoint your important state before /compact clears the noise."

### "What's Smart Compaction?"
"Auto-compaction fires when context gets tight, but it's lossy. It summarizes your session with a generic checklist and drops nuance: the 'why' behind decisions, error sequences, agent state. Smart Compaction adds two things: (1) a PreCompact hook that snapshots structured state to disk before compaction, and (2) a SessionStart hook that injects what was lost back into the fresh context. Set it up with `python3 measure.py setup-smart-compact`."

---

## Quality-Driven Coaching (v2.0)

When quality data is available from `measure.py quality`, use these coaching patterns:

### Quality Score 85-100 (Excellent)
"Your session is clean. [Score]/100 quality. Not much to optimize here. Focus on behavioral habits and model routing."

### Quality Score 70-84 (Good)
"Session quality is [Score]/100. You've got some bloat building up: [top issue]. A manual `/compact` now would clear [X tokens] of low-value content. If you haven't set up Smart Compaction yet, now's a good time."

### Quality Score 50-69 (Degraded)
"Your session quality dropped to [Score]/100. That's the danger zone. [Top 2 issues] are eating [X tokens] combined. I'd recommend `/compact` now. If Smart Compaction is installed, your decisions and error context will survive. If not, consider `/clear` and reloading what you need."

### Quality Score <50 (Critical)
"Session quality is [Score]/100. Heavy rot. You've got [N stale reads], [N bloated results], and [N compactions] worth of information loss stacked up. The context window is working against you at this point. Recommended: `/clear` and start fresh. If you need continuity, install Smart Compaction first (`python3 measure.py setup-smart-compact`), then `/clear`. Your state will be checkpointed."
