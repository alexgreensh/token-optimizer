---
name: token-optimizer
description: Find the ghost tokens. Audit Claude Code setup, see where 25-38% of your context goes, fix it. Use when context feels tight.
---

# Token Optimizer: See Where Your Context Window Goes. Get It Back.

Token optimization specialist. Audits a Claude Code setup, identifies context window waste, implements fixes, and measures savings.

**Target**: 5-15% context recovery through config cleanup (more for heavier setups), up to 25%+ with autocompact management. Plus behavioral optimizations that compound across every session.

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

# Verify backup is non-empty
if [ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]; then
  echo "[Warning] Backup directory is empty. No files were backed up."
  echo "This may mean you have a fresh setup (nothing to back up) or a permissions issue."
fi
```

3. **Create coordination folder**:
```bash
COORD_PATH=$(mktemp -d /tmp/token-optimizer-XXXXXXXXXX)
[ -d "$COORD_PATH" ] || { echo "[Error] Failed to create coordination folder. Check /tmp permissions."; return 1; }
mkdir -p "$COORD_PATH"/{audit,analysis,plan,verification}
```

4. **Check SessionEnd hook** (first-time setup, skips silently if already installed):
```bash
python3 ~/.claude/skills/token-optimizer/scripts/measure.py check-hook
```
   - If exit 0: hook is already installed, skip entirely and proceed to Phase 1.
   - If exit 1: explain and offer to install:

   ```
   [Token Optimizer] Usage analytics hook not installed.

   The SessionEnd hook runs `measure.py collect` once when a session closes (~1 second).
   It collects session duration, token usage, skills invoked, and model mix into a local
   SQLite database. No data leaves your machine. No background process, no daemon.

   Why a hook instead of a cron job? Session data only exists after a session ends.
   A cron job polling mid-session would find nothing new. The hook fires exactly once,
   runs for ~1 second, exits. Zero background processes. It's the Claude Code native
   pattern for post-session work.

   Data stored: ~/.claude/_backups/token-optimizer/trends.db
   Remove anytime: delete the SessionEnd entry from ~/.claude/settings.json
   ```

   Ask user:
   1. Install it (run `measure.py setup-hook --dry-run` first to show the diff, then confirm and run `measure.py setup-hook`)
   2. Show me the JSON first (run `measure.py setup-hook --dry-run` and stop)
   3. Skip for now

   If skipped, note it and continue. The audit still works without it, but the Trends tab will only have data from manual `measure.py collect` runs.

Output: `[Token Optimizer Initialized] Backup: $BACKUP_DIR | Coordination: $COORD_PATH`

---

## Phase 1: Quick Audit (Parallel Agents)

Read `references/agent-prompts.md` for all prompt templates.

Dispatch 6 agents in parallel (single message, multiple Task calls):

**Model assignment**: CLAUDE.md, MEMORY.md, Skills, MCP auditors use `model="sonnet"` (judgment calls). Commands use `model="haiku"` (data gathering). Settings & Advanced uses `model="sonnet"` (judgment on rules, settings, @imports).

| Agent | Output File | Task |
|-------|-------------|------|
| CLAUDE.md Auditor | `audit/claudemd.md` | Size, duplication, tiered content, cache structure |
| MEMORY.md Auditor | `audit/memorymd.md` | Size, overlap with CLAUDE.md |
| Skills Auditor | `audit/skills.md` | Count, frontmatter overhead, duplicates |
| MCP Auditor | `audit/mcp.md` | Deferred tools, broken/unused servers |
| Commands Auditor | `audit/commands.md` | Count, menu overhead |
| Settings & Advanced | `audit/advanced.md` | Hooks, rules, settings, @imports, .claudeignore, caching, monitoring |

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

**Validation**: After the synthesis agent completes, verify output exists:
```bash
[ -s "$COORD_PATH/analysis/optimization-plan.md" ] || echo "[Warning] Synthesis output missing or empty. Presenting raw audit files instead."
```
If missing, present the individual `audit/*.md` files directly to the user. Do not proceed to Phase 4 without user review of either the synthesis or the raw findings.

---

## Phase 3: Present Findings

Read the optimization plan and present. For the MODEL ROUTING line, also read `{COORD_PATH}/audit/advanced.md` to extract the "Has routing instructions" and "Usage Pattern" data if not present in the optimization plan.

```
[Token Optimizer Results]

CURRENT STATE
Your per-message overhead: ~X tokens
Context used before first message: ~X%

QUICK WINS (do these today)
- [Action 1]: Save ~X tokens/msg (~Y%)
- [Action 2]: Save ~X tokens/msg (~Y%)

MODEL ROUTING
[Has instructions: Yes/No] | [Token distribution: X% Opus, Y% Sonnet, Z% Haiku or "Not measured yet"]

FULL OPTIMIZATION POTENTIAL
If all implemented: ~X tokens/msg saved (~Y% reduction)

Ready to implement? I can:
1. Auto-fix safe changes (consolidate CLAUDE.md, archive skills)
2. Generate .claudeignore (if missing)
3. Create optimized CLAUDE.md template
4. Show MCP servers to disable

What should we tackle first?
```

**Then generate the interactive dashboard:**

```bash
python3 ~/.claude/skills/token-optimizer/scripts/measure.py dashboard --coord-path $COORD_PATH --serve
```

This serves an interactive HTML dashboard at `http://localhost:8080/dashboard.html` with all findings, a token donut chart, and an optimization checklist. The user can browse categories, toggle optimizations, and click "Copy Prompt" to paste selected items back into Claude Code.

Tell the user: "Dashboard running at http://localhost:8080/dashboard.html. Browse findings by category, check the optimizations you want, click Copy Prompt and paste back here. Or just tell me directly what to tackle."

The terminal summary above remains for headless/terminal-only environments. Dashboard is additive.

**Wait for user decision before proceeding.**

---

## Phase 4: Implementation

Read `references/implementation-playbook.md` for detailed steps.

Available actions: 4A (CLAUDE.md), 4B (MEMORY.md), 4C (Skills), 4D (.claudeignore), 4E (MCP), 4F (Hooks), 4G (Cache Structure), 4H (Rules Cleanup), 4I (Settings Tuning), 4J (Skill Description Tightening), 4K (Compact Instructions Setup), 4L (Model Routing Setup).

Templates in `examples/`. Always backup before changes. Present diffs for approval.

---

## Measurement Tool: Additional Commands

Beyond the core `report`/`snapshot`/`compare`/`dashboard` commands, the measurement tool includes:

- **`measure.py trends [--days N] [--json]`**: Scans all JSONL session logs across projects. Shows which skills you actually use, subagent patterns, model mix, and cross-references against installed skills to surface unused ones. Default: last 30 days.
- **`measure.py health`**: Detects running Claude Code sessions, checks their version against installed, flags stale/zombie processes, and shows automated Claude-related processes.
- Both `trends` and `health` data appear as interactive tabs in the dashboard when generated via `measure.py dashboard`.

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

NEXT STEPS (Behavioral, ordered by ROI)
1. Default subagents to Haiku (60x cheaper than Opus, see Model Routing)
2. Use /compact at 70% context (quality degrades past 70%)
3. Use /clear between unrelated topics
4. Use Plan Mode (Shift+Tab x2) before complex tasks
5. Batch related requests into one message
6. Run /context periodically to check fill level
7. Run `measure.py trends` periodically to review usage patterns
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
| Commands auditor | `haiku` | - | Data gathering: counting, presence checks |
| Settings & Advanced auditor | `sonnet` | `haiku` | Judgment: rules quality, settings tradeoffs, @imports analysis |
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
- **Backup write failure**: If `ls "$BACKUP_DIR"` shows 0 files after Phase 0 backup, warn user and ask whether to proceed without backup. Do not silently continue.
- **mktemp failure**: If `COORD_PATH` directory does not exist after creation, print error and abort. Check /tmp permissions.
- **Synthesis agent failure**: If `analysis/optimization-plan.md` is missing or empty after Phase 2, present raw audit files to user instead. Do not proceed to Phase 4 blindly.
- **Verification agent failure**: If Phase 5 agent fails, fall back to running `measure.py snapshot after` + `measure.py compare` directly in the shell.
- **Snapshot file corrupt**: If `compare` fails with a JSON error, re-run `measure.py snapshot [label]` to regenerate the corrupt file.
- **Stale snapshot warning**: If the "before" snapshot is >24h old when running `compare`, a warning is printed. Consider re-taking it for accurate results.

---

## Restoring Backups

If something goes wrong, restore from the backup created in Phase 0:
```bash
# Find your most recent backup
ls -lt ~/.claude/_backups/token-optimizer-* | head -5

# Restore specific files (replace TIMESTAMP with your backup folder name)
BACKUP="$HOME/.claude/_backups/token-optimizer-TIMESTAMP"
cp "$BACKUP/CLAUDE.md" ~/.claude/CLAUDE.md
cp "$BACKUP/settings.json" ~/.claude/settings.json
cp -r "$BACKUP/commands" ~/.claude/commands
# MEMORY.md files have the project name in the filename
cp "$BACKUP/MEMORY-*.md" ~/.claude/projects/*/memory/MEMORY.md
```

Backups are never automatically deleted. They accumulate in `~/.claude/_backups/`.

---

## Core Rules

- Quantify everything (X tokens, Y%)
- Create backups before any changes (`~/.claude/_backups/`)
- Ask user before implementing
- Never delete files, always archive
- Use appropriate models (with fallbacks) for each task
- Show before/after diffs
- Frame savings as context budget (% of 200K), not dollar amounts
