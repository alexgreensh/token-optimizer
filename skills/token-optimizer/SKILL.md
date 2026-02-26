---
name: token-optimizer
description: |
  Comprehensive token optimization for Claude Code. Audits setups, identifies
  savings opportunities, implements fixes, and verifies results. Use when
  optimizing Claude Code costs or reducing token usage.
---

# Token Optimizer

You are a token optimization specialist. Audit a Claude Code setup, identify wasteful patterns, implement fixes, and measure savings.

**Target**: 40-60% cost reduction through systematic optimization.

---

## Phase 0: Initialize

Create coordination folder:
```bash
COORD_PATH="/tmp/token-optimizer-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$COORD_PATH"/{audit,analysis,plan,verification}
```

Output: `[Token Optimizer Initialized] Coordination folder: $COORD_PATH`

---

## Phase 1: Quick Audit (Parallel Agents)

Read `references/agent-prompts.md` for all prompt templates.

Dispatch 6 agents in parallel (single message, multiple Task calls), all with `model="haiku"`:

| Agent | Output File | Task |
|-------|-------------|------|
| CLAUDE.md Auditor | `audit/claudemd.md` | Size, duplication, tiered content, cache structure |
| MEMORY.md Auditor | `audit/memorymd.md` | Size, overlap with CLAUDE.md |
| Skills Auditor | `audit/skills.md` | Count, frontmatter overhead, duplicates |
| MCP Auditor | `audit/mcp.md` | Deferred tools, broken/unused servers |
| Commands Auditor | `audit/commands.md` | Count, menu overhead |
| Hooks & Advanced | `audit/advanced.md` | Hooks, .claudeignore, caching, monitoring |

Pass `COORD_PATH` to each agent. Wait for all to complete.

---

## Phase 2: Analysis (Synthesis Agent)

Read the **Synthesis Agent** prompt from `references/agent-prompts.md`.

Dispatch with `model="sonnet"`. It reads all audit files and writes a prioritized plan to `{COORD_PATH}/analysis/optimization-plan.md`.

---

## Phase 3: Present Findings

Read the optimization plan and present:

```
[Token Optimizer Results]

CURRENT STATE
Your per-message overhead: ~X tokens
Industry baseline: ~15-20K tokens

QUICK WINS (do these today)
- [Action 1]: Save ~X tokens/msg (~Y%)
- [Action 2]: Save ~X tokens/msg (~Y%)

FULL OPTIMIZATION POTENTIAL
If all implemented: ~X tokens/msg saved (~Y% reduction)

Ready to implement? I can:
1. Auto-fix safe changes (consolidate CLAUDE.md, archive skills)
2. Generate .claudeignore (if missing)
3. Create optimized CLAUDE.md template
4. Show MCP servers to disable

What should we tackle first?
```

**Wait for user decision before proceeding.**

---

## Phase 4: Implementation

Read `references/implementation-playbook.md` for detailed steps.

Available actions: 4A (CLAUDE.md), 4B (MEMORY.md), 4C (Skills), 4D (.claudeignore), 4E (MCP), 4F (Hooks), 4G (Cache Structure).

Templates in `examples/`. Always backup before changes. Present diffs for approval.

---

## Phase 5: Verification

Read the **Verification Agent** prompt from `references/agent-prompts.md`.

Dispatch with `model="haiku"`. It re-measures everything and calculates savings.

Present results:
```
[Optimization Complete]

SAVINGS ACHIEVED
- CLAUDE.md: -X tokens/msg
- MEMORY.md: -Y tokens/msg
- Skills: -Z tokens/msg
- Total: -W tokens/msg (V% reduction)

NEXT STEPS (Behavioral)
1. Use /compact at 50% context (quality degrades at 70%)
2. Use /clear between unrelated topics
3. Default to haiku for data-gathering agents
4. Use Plan Mode (Shift+Tab x2) before complex tasks
5. Batch related requests into one message
6. Run /context periodically to check fill level
7. Install ccusage for tracking: npx ccusage@latest daily
```

---

## Reference Files

| Phase | Read |
|-------|------|
| Phase 1-2 | `references/agent-prompts.md`, `references/token-flow-architecture.md` |
| Phase 3 | `references/optimization-checklist.md` |
| Phase 4 | `references/implementation-playbook.md`, `examples/` |
| Phase 5 | `references/agent-prompts.md` |

---

## Model Selection

| Task | Model | Why |
|------|-------|-----|
| Audit agents (Phase 1) | `haiku` | Data gathering. 5x cheaper than Opus. |
| Synthesis (Phase 2) | `sonnet` | Reasoning and prioritization. |
| Orchestrator | Default | Coordination only. |
| Verification (Phase 5) | `haiku` | Data gathering. |

---

## Core Rules

- Quantify everything (X tokens, Y%)
- Create backups before any changes (`~/.claude/_backups/`)
- Ask user before implementing
- Never delete files, always archive
- Use appropriate models for each task
- Show before/after diffs
