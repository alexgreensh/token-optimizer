---
name: token-optimizer
description: Find the ghost tokens. Audit Claude Code or Codex setup, see where context goes, fix it. Use when context feels tight.
effort: high
---

# Token Optimizer

Audits a Claude Code or Codex setup, identifies context window waste, implements fixes, and measures savings.

**Target**: 5-15% context recovery through config cleanup, up to 25%+ with autocompact management.

---

## Codex Runtime

If `TOKEN_OPTIMIZER_RUNTIME=codex` or Codex environment is detected, read `references/codex-workflow.md` and follow its chat-first workflow instead of the Claude Code phases below.

---

## Phase 0: Initialize (Claude Code)

Resolve measure.py path:
```bash
MEASURE_PY=""
for f in "$HOME/.claude/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
[ -z "$MEASURE_PY" ] && { echo "[Error] measure.py not found."; exit 1; }
```

Read `references/phase0-setup.md` for the full setup sequence: context window detection, pre-check, backup, coordination folder, hook checks, daemon setup, and smart compaction.

---

## Phase 1: Quick Audit (Parallel Agents)

Read `references/agent-prompts.md` for all prompt templates.

Dispatch 6 agents in parallel:

| Agent | Output File | Model | Task |
|-------|-------------|-------|------|
| CLAUDE.md Auditor | `audit/claudemd.md` | sonnet | Size, duplication, tiered content, cache structure |
| MEMORY.md Auditor | `audit/memorymd.md` | sonnet | Size, overlap with CLAUDE.md |
| Skills Auditor | `audit/skills.md` | sonnet | Count, frontmatter overhead, duplicates |
| MCP Auditor | `audit/mcp.md` | sonnet | Deferred tools, broken/unused servers |
| Commands Auditor | `audit/commands.md` | haiku | Count, menu overhead |
| Settings & Advanced | `audit/advanced.md` | sonnet | Hooks, rules, settings, @imports, caching |

Pass `COORD_PATH` to each. Wait for all to complete. If any output file is missing, note the gap and proceed.

---

## Phase 2: Analysis

Read the **Synthesis Agent** prompt from `references/agent-prompts.md`. Dispatch with `model="opus"` (fallback: sonnet). It reads all audit files and writes `{COORD_PATH}/analysis/optimization-plan.md`. If missing, present raw audit files instead.

---

## Phase 3: Present Findings

Read `references/presentation-workflow.md` for the findings template, dashboard generation, and URL presentation logic. Generate the dashboard:
```bash
python3 $MEASURE_PY dashboard --coord-path $COORD_PATH
```
Wait for user decision before proceeding.

---

## Phase 4: Implementation

Read `references/implementation-playbook.md` for detailed steps. Available actions: 4A-4P covering CLAUDE.md, MEMORY.md, Skills, File Exclusion, MCP, Hooks, Cache, Rules, Settings, Descriptions, Compact Instructions, Model Routing, Smart Compaction, Quality Check, Version-Aware Optimizations, and Smart Routing. Templates in `examples/`. Always backup before changes. Present diffs for approval.

---

## Phase 5: Verification

Read the **Verification Agent** prompt from `references/agent-prompts.md`. Dispatch with `model="haiku"`. Re-measures everything and calculates savings. Present before/after comparison and behavioral next steps.

---

## Reference Files

| Context | Read |
|---------|------|
| Codex runtime | `references/codex-workflow.md` |
| Phase 0 setup details | `references/phase0-setup.md` |
| Phase 1-2 agent prompts | `references/agent-prompts.md`, `references/token-flow-architecture.md` |
| Phase 3 presentation | `references/presentation-workflow.md` |
| Phase 4 implementation | `references/implementation-playbook.md`, `examples/` |
| CLI commands | `references/cli-reference.md` |
| Phase 3 checklist | `references/optimization-checklist.md` |
| Error handling | `references/error-recovery.md` |

---

## Core Rules

- Quantify everything (X tokens, Y%)
- Create backups before any changes
- Ask user before implementing
- Never delete files, always archive outside the skills directory
- Check dependencies before archiving (skills, MCP, deny rules can break other tools)
- Warn about side effects before each change
- Prefer project-level deny rules over global
- Show before/after diffs
- Frame savings as context budget (% of window), not dollar amounts
