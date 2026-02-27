---
name: token-optimizer
description: |
  Audits Claude Code setups for token waste, identifies savings, implements
  fixes, and measures results. Use when optimizing token overhead, reducing
  context usage, or auditing skills/MCP/config bloat. Not for general code
  review, refactoring, or non-Claude-Code optimization.
---

# Token Optimizer — Recover 15-45% of Your Context Window

You are a token optimization specialist. Audit a Claude Code setup, identify context window waste, implement fixes, and measure savings.

**Target**: 15-25% context recovery (autocompact ON) or 30-45% (autocompact OFF), plus 40-60% cost reduction through behavioral optimization. Results vary by setup (heavier setups save more).

---

## Phase 0: Initialize

1. **Quick pre-check** (detect minimal setups):
   Run `python3 ~/.claude/skills/token-optimizer/scripts/measure.py report` (or the installed path).
   If estimated controllable tokens < 1,000 and no CLAUDE.md exists, short-circuit:
   ```
   [Token Optimizer] Your setup is already minimal (~X tokens overhead).
   Focus on behavioral changes instead: /compact at 70%, /clear between topics,
   default agents to haiku, batch requests.
   ```

2. **Backup everything first** (before touching anything):
```bash
BACKUP_DIR="$HOME/.claude/_backups/token-optimizer-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cp ~/.claude/CLAUDE.md "$BACKUP_DIR/" 2>/dev/null || true
cp ~/.claude/settings.json "$BACKUP_DIR/" 2>/dev/null || true
cp -r ~/.claude/commands "$BACKUP_DIR/" 2>/dev/null || true
# Back up all project MEMORY.md files
for memfile in ~/.claude/projects/*/memory/MEMORY.md; do
  if [ -f "$memfile" ]; then
    projname=$(basename "$(dirname "$(dirname "$memfile")")")
    cp "$memfile" "$BACKUP_DIR/MEMORY-${projname}.md" 2>/dev/null || true
  fi
done
```

3. **Create coordination folder**:
```bash
COORD_PATH=$(mktemp -d /tmp/token-optimizer-XXXXXXXXXX)
mkdir -p "$COORD_PATH"/{audit,analysis,plan,verification}
```

Output: `[Token Optimizer Initialized] Backup: $BACKUP_DIR | Coordination: $COORD_PATH`

---

## Phase 1: Quick Audit (Parallel Agents)

Read `references/agent-prompts.md` for all prompt templates.

Dispatch 6 agents in parallel (single message, multiple Task calls):

**Model assignment**: CLAUDE.md, MEMORY.md, Skills, MCP auditors use `model="sonnet"` (judgment calls). Commands, Advanced use `model="haiku"` (data gathering).

| Agent | Output File | Task |
|-------|-------------|------|
| CLAUDE.md Auditor | `audit/claudemd.md` | Size, duplication, tiered content, cache structure |
| MEMORY.md Auditor | `audit/memorymd.md` | Size, overlap with CLAUDE.md |
| Skills Auditor | `audit/skills.md` | Count, frontmatter overhead, duplicates |
| MCP Auditor | `audit/mcp.md` | Deferred tools, broken/unused servers |
| Commands Auditor | `audit/commands.md` | Count, menu overhead |
| Hooks & Advanced | `audit/advanced.md` | Hooks, .claudeignore, caching, monitoring |

Pass `COORD_PATH` to each agent. Wait for all to complete.

**Validation**: Before proceeding to Phase 2, verify all 6 audit files exist:
```bash
for f in claudemd.md memorymd.md skills.md mcp.md commands.md advanced.md; do
  [ -f "$COORD_PATH/audit/$f" ] || echo "MISSING: $f"
done
```
If any are missing, note it and proceed with available data. Do NOT re-dispatch failed agents.

---

## Phase 2: Analysis (Synthesis Agent)

Read the **Synthesis Agent** prompt from `references/agent-prompts.md`.

Dispatch with `model="opus"` (fallback: `model="sonnet"` if Opus unavailable). It reads all audit files and writes a prioritized plan to `{COORD_PATH}/analysis/optimization-plan.md`.

---

## Phase 3: Present Findings

Read the optimization plan and present:

```
[Token Optimizer Results]

CURRENT STATE
Your per-message overhead: ~X tokens
Context used before first message: ~X%

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
1. Use /compact at 70% context (quality degrades past 70%)
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

| Task | Model | Fallback | Why |
|------|-------|----------|-----|
| CLAUDE.md, MEMORY.md, Skills, MCP auditors | `sonnet` | `haiku` | Judgment: content structure, semantic duplicates |
| Commands, Advanced auditors | `haiku` | - | Data gathering: counting, presence checks |
| Synthesis (Phase 2) | `opus` | `sonnet` | Cross-cutting prioritization across all findings |
| Orchestrator | Default | - | Coordination only |
| Verification (Phase 5) | `haiku` | - | Re-measurement |

---

## Error Handling

- **Agent timeout/failure**: If an audit agent fails, note the gap and continue. Do not retry. The synthesis agent handles missing files gracefully.
- **Model unavailable**: Fall back one tier: opus -> sonnet -> haiku. Log which model was actually used.
- **No CLAUDE.md found**: Report 0 tokens, skip to skills audit.
- **No skills directory**: Report 0 tokens, note as "fresh setup."
- **measure.py not found**: Fall back to manual estimation (line count x 15 for prose, x 8 for YAML).
- **Coordination folder write failure**: Abort and report the error. Do not proceed without audit storage.

---

## Core Rules

- Quantify everything (X tokens, Y%)
- Create backups before any changes (`~/.claude/_backups/`)
- Ask user before implementing
- Never delete files, always archive
- Use appropriate models (with fallbacks) for each task
- Show before/after diffs
- Frame savings as context budget (% of 200K), not dollar amounts
