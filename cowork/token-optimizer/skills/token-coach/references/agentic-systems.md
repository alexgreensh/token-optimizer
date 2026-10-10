# Agentic Systems: Multi-Agent Design Patterns for Token Efficiency

Reference file for Token Coach. Loaded ONLY for option c (multi-agent architecture).

---

## Contents

- [The Cost Model](#the-cost-model)
- [Design Patterns](#design-patterns)
- [Anti-Patterns in Multi-Agent Design](#anti-patterns-in-multi-agent-design)
- [Quick Decision Framework](#quick-decision-framework)

---

## The Cost Model

Every subagent gets its own fresh context window sized to its model (1M tokens on current Claude models). This is both the power and the cost of multi-agent architectures.

### What Each Agent Inherits
- System prompt + built-in tools: ~15K tokens (FIXED, same as your main session)
- MCP tool definitions: same as your main session (deferred or eager)
- Skills frontmatter: same menu as your main session (~100 tokens/skill)
- Global CLAUDE.md: full content, every agent
- MEMORY.md: full content, every agent

### What Each Agent Does NOT Inherit
- Your conversation history (good, this is why subagents are useful)
- Files you've already read (they start fresh)
- Results from other subagents

### The Math
- 5 agents x 15K config overhead = 75K tokens just for setup
- Slimming CLAUDE.md by 1,000 tokens saves 5,000 tokens across 5 agents
- Measured native agent overhead: ~13K tokens per agent

---

## Design Patterns

### Pattern 1: Subagent as Context Isolation
Anthropic's official recommendation: use subagents to preserve main session context.
- Every file Claude reads stays in your context until compaction
- Subagents run in their own window and return only summaries
- Prompt: "use a subagent to investigate X" keeps your main window clean
- Think of subagents as disposable research assistants, not just parallel workers

### Pattern 2: The Coordination Folder
Prevents orchestrator context overflow from agent outputs. Use a project-local folder (add it to `.gitignore` so agent output never lands in a commit or a shared temp directory):
```
.coordination/
  COORDINATION.md       # Status tracker
  findings/             # Agents write here
    agent-1-findings.md
    agent-2-findings.md
  status/               # Agent completion signals
```
- Agents write FULL findings to files
- Orchestrator gets "Agent X completed, output at {path}" not the full output
- Synthesis agent reads files directly
- Do not pull raw agent output into the orchestrator's context; the whole point of the folder is that the orchestrator reads one line per agent, not every finding

### Pattern 3: Parallel Dispatch for Independent Tasks
- Independent tasks in one message with multiple Agent tool calls
- Don't dispatch sequentially when tasks have no dependencies
- Each parallel agent gets its own fresh context
- Total token usage is the same, but wall-clock time is much less

### Pattern 4: Model Routing for Agents
Default routing table:
| Task Type | Model tier | Why |
|-----------|-------|-----|
| File reading, counting, directory scans | haiku | The cheapest tier is accurate enough for data gathering |
| Code analysis, judgment calls, writing | sonnet | Good balance of quality and cost |
| Complex multi-step reasoning, architecture | opus | Only when you need deep reasoning |

Add to CLAUDE.md: "Default subagents to model='haiku' for data gathering, model='sonnet' for analysis. Reserve model='opus' for complex reasoning."

### Pattern 5: Surgical Skill Assignments
- Skills in a subagent's `skills:` field load FULLY at agent startup (not progressively)
- 5 skills x 3K tokens each = 15K tokens before the agent does anything
- Only assign skills the agent actually needs
- Built-in agents (Explore, Plan) don't get skills at all
- Reference files within assigned skills still load progressively

### Pattern 6: Built-in Agent Type Selection
| Type | Access | Use For |
|------|--------|---------|
| Explore | Read-only (Glob, Grep, Read) | Codebase navigation, file search |
| Plan | Read-only | Planning, architecture analysis |
| General-purpose | Full tools | Tasks requiring write access |
| Custom | You configure | Specialized workflows |

Use Explore when you just need to find things. Use Plan when you need reasoning without edits. Use General-purpose only when the agent needs to write files.

Built-in Explore and Plan run on the main conversation's model, and General-purpose does too unless `CLAUDE_CODE_SUBAGENT_MODEL` or the `model` field sets another. To get a lighter tier for reads, define a read-only custom subagent with `model: haiku` (or set `CLAUDE_CODE_SUBAGENT_MODEL` together with `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`, which also forces Explore and Plan).

### Pattern 7: Agent Team Cost-Benefit Analysis
Anthropic's docs report that agent teams use roughly 7x the tokens of a single session in plan mode.
- Agent teams buy wall-clock speed with token cost
- Break-even: only use teams when the time savings justify several times the token cost
- For budget-conscious users: single agent with selective /dispatch for parallelizable subtasks

---

## Anti-Patterns in Multi-Agent Design

### The Context Flood
**Problem**: Orchestrator reads back all agent output files into its own context.
**Symptoms**: Orchestrator hits compaction after 2-3 agents report back.
**Fix**: Orchestrator receives only "Agent X completed at {path}". Synthesis agent reads files.

### The Clone Army
**Problem**: Every agent is general-purpose with full tools and default model.
**Symptoms**: High cost. Agents doing simple reads with Opus.
**Fix**: Use read-only agents for reads and give them a lighter `model`; Explore and Plan inherit the main model unless you force one. Route model by task complexity.

### The Skill Dump
**Problem**: Custom agents assigned all available skills "just in case."
**Symptoms**: Each agent loads 10+ skills fully at startup. 30K+ tokens before work begins.
**Fix**: Assign only the 1-2 skills each agent actually needs.

### The Sequential Chain
**Problem**: Independent tasks dispatched one at a time instead of in parallel.
**Symptoms**: 5 minute task takes 25 minutes. Same total tokens, much longer wall-clock.
**Fix**: Launch independent agents in a single message with multiple Agent tool calls.

### The Missing Handoff
**Problem**: No coordination folder. Agents can't see each other's work.
**Symptoms**: Duplicate work. Agents solving the same problem independently. No synthesis.
**Fix**: Create a coordination folder. Agents write findings to it. Orchestrator tracks status.

---

## Quick Decision Framework

**Should I use subagents for this task?**
1. Does the task require reading many files? -> YES, use subagents for context isolation
2. Are there 3+ independent subtasks? -> YES, parallel dispatch
3. Is it a simple, focused task? -> NO, single session is simpler and cheaper
4. Am I hitting compaction frequently? -> YES, subagents help by isolating file reads

**How many agents?**
- 1 agent: Simple tasks, focused edits, single-module changes
- 2-3 agents: Multi-module tasks with clear boundaries
- 4-6 agents: Large refactors, cross-cutting changes, parallel research
- 7+ agents: Rarely justified. Coordination overhead exceeds benefits.
