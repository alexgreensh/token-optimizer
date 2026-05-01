---
name: token-optimizer
description: Find the ghost tokens. Audit Claude Code or Codex setup, see where context goes, fix it. Use when context feels tight.
effort: high
---

# Token Optimizer: See Where Your Context Window Goes. Get It Back.

Token optimization specialist. Audits a Claude Code or Codex setup, identifies context window waste, implements fixes, and measures savings.

**Target**: 5-15% context recovery through config cleanup (more for heavier setups), up to 25%+ with autocompact management. Plus behavioral optimizations that compound across every session.

---

## Codex Runtime: Chat-First Workflow

If this skill is running in Codex, use this section instead of the Claude Code phases below. The user is not asking for a dashboard first. They are asking for a coach who can tell them:

- What is my status?
- What is my setup?
- What is wasteful or risky?
- What should we fix, and what should we leave alone?
- What behavior should I change during long Codex sessions?

### 0. Resolve `measure.py` for Codex

```bash
MEASURE_PY=""
for f in "$HOME/.codex/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.codex/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$PWD/skills/token-optimizer/scripts/measure.py"; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
[ -z "$MEASURE_PY" ] && { echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; }
echo "Using: $MEASURE_PY"
```

Use `TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" ...` for Codex commands.

### 1. Start With Chat Status

Run these before giving advice:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" report
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" coach --json
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" quality current --json
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-doctor --project "$PWD" --json
```

If `quality current` has no parseable session, continue without it. Do not block the audit.

Present the result conversationally:

```
Here is your Codex Token Optimizer status:

STATUS
- Health score: X/100
- Startup overhead: X tokens, Y% of your Codex window
- Usable context after overhead/buffer: ~X tokens
- Current session quality: grade/score, if available

SETUP
- AGENTS.md: X tokens
- Codex memories: X files / X lines
- Skills/plugin skills: X active, Y tokens of discovery surface
- MCP: X servers
- Hooks: balanced / quiet / missing / custom
- Compact prompt: installed / missing / custom
- Status line: installed / missing / custom

GOOD NEWS
- [2-3 things that are already healthy]

TOP FIXES
1. [fix, estimated value, risk]
2. [fix, estimated value, risk]
3. [fix, estimated value, risk]

BEHAVIOR COACHING
- [how to compact/clear/batch/use subagents for this user]
```

Be plainspoken. Avoid selling features. Tell the user what matters for their setup.

### 2. Codex Setup Fixes

Use `codex-doctor` as the setup truth source.

If hooks are missing or stale:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --dry-run
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD"
```

Default to the balanced profile. Balanced means:

- `SessionStart`: recovery context when Codex can use it.
- `UserPromptSubmit`: prompt-quality and loop nudges.
- `Stop`: throttled dashboard refresh and continuity checkpoints.
- Codex compact prompt in `~/.codex/config.toml`.

Only suggest these when the user accepts the tradeoff:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --profile quiet
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --profile telemetry
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --profile aggressive
```

Quiet is Stop-only. Telemetry adds visible PostToolUse rows. Aggressive enables all current hooks, including experimental Bash PreToolUse. Do not make aggressive the default.

### 3. Codex Optimization Actions

Use these as the Codex equivalent of Phase 4:

| Action | What To Inspect | Safe Fix |
|---|---|---|
| AGENTS.md | Global/project `AGENTS.md` and `AGENTS.override.md` size, duplication, volatile content | Keep root guidance lean; move long workflows into skills or referenced docs |
| Codex memories | `~/.codex/memories/**/*.md` when present | Keep high-signal durable preferences only; remove stale operating history |
| Skills/plugin skills | `coach`, `report`, dashboard Manage data | Disable stale user skills with `measure.py codex-skill disable`; do not edit plugin cache directly |
| MCP servers | Codex config MCP inventory | Disable unused servers with `measure.py codex-mcp disable NAME` after checking whether the user actually uses them |
| Hooks | `codex-doctor`, `.codex/hooks.json` | Install/update balanced hooks; keep per-tool hooks opt-in |
| Compact guidance | `codex-compact-prompt --status` | Install `measure.py codex-compact-prompt --install`; use compact around phase boundaries |
| Quality/session rot | `quality current`, `coach` | Recommend `/compact`, `/clear`, rereads, or batching based on the actual score |
| Cost/model behavior | Trends/model mix when available | In Codex, talk about reasoning effort and task boundaries more than Claude-style Haiku/Sonnet/Opus routing |

Always explain side effects before changing config. Prefer dry-runs before writes.

### 4. Codex Runtime Optimizations That Work Now

- Real Codex status in chat via `report` and `coach`.
- Context quality scoring from Codex JSONL where logs expose enough data.
- Balanced hooks for prompt-quality nudges, session continuity, and dashboard refresh.
- Compact prompt installation in `~/.codex/config.toml`.
- Codex status line support.
- Skill/plugin/MCP inventory and enable/disable commands.
- Bounded log parsing so huge Codex transcripts do not burn CPU.
- Explicit file outline tools (`outline.py`, structure helpers) when the user asks to inspect a large file before rereading it.

### 5. Codex Features That Are Not Full Parity Yet

Be honest about these. Do not imply they are working invisibly:

- Delta read substitution is not active in Codex.
- Structure-map substitution is not active in Codex.
- Invisible Bash command rewriting/compression is not reliable in current Codex; keep it experimental and opt-in.
- Claude-style `PreCompact`, `PostCompact`, and `StopFailure` hook parity is approximated with compact prompts and checkpoints.
- Cache-write TTL breakdowns are limited by what Codex logs expose.
- Per-session skill invocation telemetry is incomplete; stale-skill advice is a starting point, not a verdict.

### 6. Codex Chat Close

After presenting status, ask for a concrete next step:

```
My recommendation: fix [one thing] first because it gives the most value with the least risk.
Want me to apply that, or do you want the conservative cleanup list first?
```

Do not end with only a dashboard link. The dashboard is supporting evidence, not the main experience.

---

## Phase 0: Initialize

0. **Resolve measure.py path** (works for both skill and plugin installs):
```bash
MEASURE_PY=""
for f in "$HOME/.claude/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
[ -z "$MEASURE_PY" ] && { echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; }
echo "Using: $MEASURE_PY"
```
Use `$MEASURE_PY` for all subsequent measure.py calls in this session.

1. **Detect context window size**:
   Check if `TOKEN_OPTIMIZER_CONTEXT_SIZE` env var is already set. If not:
   - Check for `ANTHROPIC_API_KEY` env var (indicates API usage, possibly 1M context)
   - If API key found, ask the user: "You appear to be using the API. Do you have 1M token context (e.g. Opus)? If so I'll calibrate for 1M instead of 200K."
   - If they confirm 1M, `export TOKEN_OPTIMIZER_CONTEXT_SIZE=1000000` for this session
   - If no API key or they say no, default is 200K (no action needed)
   Keep this quick, one question max. Don't belabor it.

2. **Quick pre-check** (detect minimal setups):
   Run `python3 $MEASURE_PY report`.
   If estimated controllable tokens < 1,000 and no CLAUDE.md exists, short-circuit:
   ```
   [Token Optimizer] Your setup is already minimal (~X tokens overhead).
   Focus on behavioral changes instead: /compact at 70%, /clear between topics,
   default agents to haiku, batch requests.
   ```

3. **Backup everything first** (before touching anything):
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

4. **Create coordination folder**:
```bash
COORD_PATH=$(mktemp -d /tmp/token-optimizer-XXXXXXXXXX)
[ -d "$COORD_PATH" ] || { echo "[Error] Failed to create coordination folder. Check /tmp permissions."; exit 1; }
mkdir -p "$COORD_PATH"/{audit,analysis,plan,verification}
```

5. **Check SessionEnd hook** (first-time setup, skips silently if already installed):
```bash
python3 $MEASURE_PY check-hook
```
   - If exit 0: hook is already installed (includes plugin auto-install), skip entirely and proceed to Phase 1.
   - If exit 1 (manual/script install users only): explain and offer to install:

   ```
   [Token Optimizer] Want to track your token usage over time?

   Right now, the optimizer can audit your setup. But to track *trends* (which
   skills you actually use, how your context fills up day to day, model costs),
   it needs to save a small log after each Claude Code session.

   What this does:
   - When you close a Claude Code session, it automatically saves usage stats
   - Takes ~2 seconds, runs silently in the background, then stops
   - All data stays on your machine (stored in ~/.claude/_backups/token-optimizer/)
   - Powers the Trends and Health tabs in your dashboard

   Without this, the dashboard only shows a snapshot from right now.
   With it, you get a living history that updates every session.

   Remove anytime by running: python3 measure.py setup-hook --uninstall
   Or manually: delete the SessionEnd entry from ~/.claude/settings.json
   ```

   Ask user:
   1. Install it (run `measure.py setup-hook --dry-run` first to show the diff, then confirm and run `measure.py setup-hook`)
   2. Show me the JSON first (run `measure.py setup-hook --dry-run` and stop)
   3. Skip for now

   If skipped, note it and continue. The audit still works without it, but the Trends tab will only have data from manual `measure.py collect` runs.

6. **Offer the bookmarkable dashboard URL** (macOS and Windows. Linux lands in a future release; skip silently there.):

   Run BOTH probes in one pass, then branch on the combination:
```bash
python3 "$MEASURE_PY" daemon-status
python3 "$MEASURE_PY" daemon-consent --get
```

   `daemon-status` prints one of `DAEMON_RUNNING` (our daemon, identity verified), `DAEMON_FOREIGN` (port 24842 bound by something else), or `DAEMON_NOT_RUNNING`. `daemon-consent --get` prints a JSON object — either `{}` (never prompted) or `{"prompted": true, "consent": true|false, ...}`.

   Decide using this 2×2 truth table:

   | Daemon \ Consent | unrecorded (`{}`) | `consent: true` | `consent: false` |
   |---|---|---|---|
   | `DAEMON_RUNNING` | skip; lead with URL next time output mentions the dashboard | skip; URL works | the user declined but the daemon is still running — offer `setup-daemon --uninstall` once |
   | `DAEMON_FOREIGN` | prompt, but warn port 24842 is already bound by a foreign service (`netstat -ano \| findstr :24842` on Windows, `lsof -i :24842` on Mac) | note conflict, suggest uninstalling our daemon's prior instance or freeing the port | skip silently |
   | `DAEMON_NOT_RUNNING` | first-time install prompt (below) | offer to reinstall (`measure.py setup-daemon`) — launchd / Task Scheduler lost it | skip silently |

   First-time install prompt copy:

   ```
   [Token Optimizer] Want a bookmarkable dashboard URL?

   URL:  http://localhost:24842/token-optimizer
   File: ~/.claude/_backups/token-optimizer/dashboard.html  (always works)

   The URL stays bookmarked and auto-updates after every session.
   The file is the fallback — same content, just harder to reach.

   What installing the URL does:
   - Runs a tiny web server on your machine (~2MB memory)
   - Starts automatically at login, restarts if it ever stops
   - Only reachable from this machine (localhost), not the network
   - Serves just this one dashboard file, nothing else

   Remove anytime: python3 measure.py setup-daemon --uninstall
   ```

   Ask user:
   1. Install it (default — write consent FIRST so we never end up with a running daemon the user said no to, then install: run `measure.py daemon-consent --set yes`, then `measure.py setup-daemon`)
   2. Skip (run `measure.py daemon-consent --set no`)

   On Linux or BSD: skip silently. Mention once that the `file://` URL still works and the systemd `--user` daemon ships in a future release.

7. **Check Smart Compaction hooks** (v2.0, first-time setup, skips silently if already installed; plugin users get these automatically):
```bash
python3 $MEASURE_PY setup-smart-compact --status
```
   - If all 4 hooks installed (includes plugin auto-install): skip entirely.
   - If partially or not installed (manual/script install users only): explain and offer to install:

   ```
   [Token Optimizer] New in v2.0: Smart Compaction

   Auto-compaction destroys working memory. Smart Compaction captures your
   session state (decisions, modified files, errors, agent state) BEFORE
   compaction fires, then restores it afterward.

   What this does:
   - Before compaction: saves a structured checkpoint of your session state
   - After compaction: injects recovered context so you don't lose your place
   - On session end: captures state for potential pickup in next session
   - All checkpoints stored locally (~/.claude/token-optimizer/checkpoints/)

   Remove anytime: python3 measure.py setup-smart-compact --uninstall
   ```

   Ask user:
   1. Install it (run `measure.py setup-smart-compact --dry-run` first, then confirm and run `measure.py setup-smart-compact`)
   2. Show me the JSON first (run `measure.py setup-smart-compact --dry-run` and stop)
   3. Skip for now

   If skipped, note it and continue. The audit still works without it.

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
| Settings & Advanced | `audit/advanced.md` | Hooks, rules, settings, @imports, file exclusion, caching, monitoring |

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
2. Generate permissions.deny rules (if missing)
3. Create optimized CLAUDE.md template
4. Show MCP servers to consider disabling

⚠️ Some optimizations have side effects:
- Deny rules block file access for ALL tools (may break MCP servers that read databases)
- Archiving skills breaks anything that @imports them
- Disabling MCP servers breaks skills that use their tools
I'll check for dependencies and warn you before each change.

What should we tackle first?
```

**Then generate the interactive dashboard:**

```bash
python3 $MEASURE_PY dashboard --coord-path $COORD_PATH
```

This generates an interactive HTML dashboard and opens it in the default browser. The dashboard shows all findings, a token donut chart, and an optimization checklist. The user can browse categories, toggle optimizations, and click "Copy Prompt" to paste selected items back into Claude Code.

Tell the user: "Dashboard opened in your browser. Browse findings by category, check the optimizations you want, click Copy Prompt and paste back here. Or just tell me directly what to tackle."

Also mention the persistent dashboard. **Check if the daemon is actually running first**:
```bash
python3 "$MEASURE_PY" daemon-status 2>/dev/null || echo "DAEMON_NOT_RUNNING"
```

If DAEMON_RUNNING:
```
Your persistent dashboard (auto-updated every session):
  URL:    http://localhost:24842/token-optimizer
  File:   ~/.claude/_backups/token-optimizer/dashboard.html
```

If DAEMON_NOT_RUNNING:
```
Your persistent dashboard (auto-updated every session):
  File:   ~/.claude/_backups/token-optimizer/dashboard.html
```
Then, only on macOS, suggest: "Want a bookmarkable URL instead? Run: `python3 $MEASURE_PY setup-daemon`"
Do NOT mention `localhost:24842` if the daemon is not running. Users will try the URL and get a connection error.

For headless/remote servers, the user can run `python3 $MEASURE_PY dashboard --coord-path $COORD_PATH --serve` separately in a terminal to serve over HTTP. Never use `--serve` from within the SKILL.md orchestrator (it blocks with `serve_forever`).

**Wait for user decision before proceeding.**

---

## Phase 4: Implementation

Read `references/implementation-playbook.md` for detailed steps.

Available actions: 4A (CLAUDE.md), 4B (MEMORY.md), 4C (Skills), 4D (File Exclusion), 4E (MCP), 4F (Hooks), 4G (Cache Structure), 4H (Rules Cleanup), 4I (Settings Tuning), 4J (Skill Description Tightening), 4K (Compact Instructions Setup), 4L (Model Routing Setup), 4M (Smart Compaction Setup), 4N (Context Quality Check), 4O (Version-Aware Optimizations), 4P (Smart Model Routing Instructions).

Templates in `examples/`. Always backup before changes. Present diffs for approval.

### 4M: Smart Compaction Setup

Protects session state across compaction events. Three components:

1. **Install hooks** (PreCompact + SessionStart + Stop + SessionEnd):
```bash
# Preview what will change
python3 $MEASURE_PY setup-smart-compact --dry-run

# Install all hooks
python3 $MEASURE_PY setup-smart-compact

# Check current status
python3 $MEASURE_PY setup-smart-compact --status
```

Show the user the dry-run diff first. Explain:
- **PreCompact**: Captures decisions, modified files, errors, agent state to a checkpoint file before compaction runs
- **SessionStart** (matcher: compact): Injects recovered context after compaction completes
- **Stop**: Captures checkpoint when session ends normally
- **SessionEnd**: Captures checkpoint on /clear or session death

All hooks call `measure.py` directly (pure Python, no shell scripts). Composes safely with any existing hooks the user already has.

2. **Generate Compact Instructions** (project-specific compaction guidance):
```bash
python3 $MEASURE_PY compact-instructions
```

This analyzes the user's CLAUDE.md, recent sessions, and project structure to generate tailored instructions that tell Claude what to prioritize during compaction. The user adds these to their project-level `.claude/settings.json` under `compactInstructions`.

3. **Verify installation**:
```bash
python3 $MEASURE_PY setup-smart-compact --status
```

Should show all 4 hooks as installed. Test by running `/compact` manually and checking that a checkpoint file appears in `~/.claude/token-optimizer/checkpoints/`.

**Configurable via environment variables:**
- `TOKEN_OPTIMIZER_CHECKPOINT_FILES`: Max checkpoint files kept (default: 10)
- `TOKEN_OPTIMIZER_CHECKPOINT_TTL`: Seconds before checkpoint expires for restore (default: 300)
- `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS`: Days to keep old checkpoints (default: 7)
- `TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD`: Keyword overlap for new-session restore (default: 0.3)

### 4N: Context Quality Check

Analyzes current session for content quality (not just quantity):
```bash
python3 $MEASURE_PY quality current
```

Shows a composite score (0-100) based on:
- **Stale reads** (25%): Files read then later edited (re-read would be fresher)
- **Bloated results** (25%): Large tool outputs never referenced again
- **Duplicates** (15%): Repeated system reminders or injected content
- **Compaction depth** (15%): Number of compactions (each = information loss)
- **Decision density** (10%): Ratio of substantive exchanges to overhead
- **Agent efficiency** (10%): Dispatch cost vs useful result size

Score ranges:
- 85-100: Excellent, clean session
- 70-84: Good, some bloat, smart compaction would help
- 50-69: Degraded, significant waste, `/compact` with checkpoint recommended
- <50: Critical, heavy rot, consider `/clear` with checkpoint

Present the score and top issues. Recommend specific actions based on the findings.

### 4P: Smart Model Routing Instructions

Injects a managed model routing block into the project's CLAUDE.md based on actual usage patterns from the last 30 days.

```bash
# Preview what would be injected
python3 $MEASURE_PY inject-routing --dry-run

# Inject (user must approve the diff first)
python3 $MEASURE_PY inject-routing
```

The block is inserted between `<!-- TOKEN_OPTIMIZER:MODEL_ROUTING -->` markers. It includes:
- Current model usage percentages (Opus/Sonnet/Haiku split)
- Task-to-model routing recommendations
- Warnings if usage is heavily skewed (e.g., >70% Opus)

The block has a 48h TTL: if not refreshed within 48 hours, it auto-removes to prevent stale routing advice.

**Always show the user the dry-run diff and get approval before injecting.**

Optional: inject a passive coaching block with session-level insights:
```bash
python3 $MEASURE_PY setup-coach-injection        # Inject COACH block
python3 $MEASURE_PY setup-coach-injection --uninstall  # Remove it
```

---

## Measurement Tool: Additional Commands

Beyond the core `report`/`snapshot`/`compare`/`dashboard` commands, the measurement tool includes:

- **`measure.py dashboard`**: Generates a standalone persistent dashboard at `~/.claude/_backups/token-optimizer/dashboard.html` with Trends and Health tabs. Auto-regenerated by the SessionEnd hook.
- **`measure.py setup-daemon`**: Installs a macOS launchd daemon serving the dashboard at `http://localhost:24842/token-optimizer`. Starts on login, restarts on crash. Remove with `--uninstall`.
- **`measure.py trends [--days N] [--json]`**: Scans all JSONL session logs across projects. Shows which skills you actually use, subagent patterns, model mix, and cross-references against installed skills to surface unused ones. Default: last 30 days.
- **`measure.py health`**: Detects running Claude Code sessions, checks their version against installed, flags stale/zombie processes, and shows automated Claude-related processes.
- Both `trends` and `health` data appear as interactive tabs in the dashboard (standalone or full audit).

### v2.0 Commands

- **`measure.py quality [session-id|current]`**: Analyzes session content quality. Scores stale reads, bloated results, duplicates, compaction depth, decision density, agent efficiency. Returns composite 0-100 score with actionable breakdown.
- **`measure.py setup-smart-compact [--dry-run] [--status] [--uninstall]`**: Installs/manages the Smart Compaction hook system (PreCompact capture + SessionStart restore + Stop/SessionEnd checkpoints). Use `--dry-run` to preview, `--status` to check, `--uninstall` to remove.
- **`measure.py compact-capture`**: Called by PreCompact/Stop/SessionEnd hooks. Parses JSONL transcript, extracts decisions/files/errors/agent state, writes checkpoint to `~/.claude/token-optimizer/checkpoints/`. Not intended for direct user invocation.
- **`measure.py compact-restore`**: Called by SessionStart hook (matcher: compact). Reads most recent checkpoint and injects recovered context. Not intended for direct user invocation.
- **`measure.py compact-instructions [--json]`**: Generates project-specific Compact Instructions based on CLAUDE.md and session patterns. Output is text the user adds to their `.claude/settings.json` compactInstructions field.
- **`measure.py list-checkpoints [--cwd PATH] [--max-age MINUTES]`**: Lists session checkpoints with age, trigger type, and quality scores. Useful for debugging the smart compact system.

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
2. Use /compact at 50-70% context (quality degrades past 70%)
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
ls -ltd ~/.claude/_backups/token-optimizer-* | head -5

# Restore specific files (replace TIMESTAMP with your backup folder name)
BACKUP="$HOME/.claude/_backups/token-optimizer-TIMESTAMP"
cp "$BACKUP/CLAUDE.md" ~/.claude/CLAUDE.md
cp "$BACKUP/settings.json" ~/.claude/settings.json
cp -r "$BACKUP/commands" ~/.claude/commands
# MEMORY.md files have the project name in the filename
for f in "$BACKUP"/MEMORY-*.md; do
  [ -f "$f" ] || continue
  projname="${f##*/MEMORY-}"; projname="${projname%.md}"
  # Guard against path traversal in crafted backup filenames
  case "$projname" in *..* | */* ) echo "[Warning] Skipping suspicious backup: $f"; continue ;; esac
  [ -d "$HOME/.claude/projects/${projname}/memory" ] || continue
  cp "$f" "$HOME/.claude/projects/${projname}/memory/MEMORY.md"
done
```

Backups are never automatically deleted. They accumulate in `~/.claude/_backups/`.

---

## v3.1 Features

### Efficiency Grading (S/A/B/C/D/F)
All quality scores now include a letter grade: S (90-100), A (80-89), B (70-79), C (55-69), D (40-54), F (0-39). Shown in status line (`ContextQ:A(82)`), dashboard badges, coach tab, and CLI output.

### Git-Aware Context Suggestions
New command: `python3 $MEASURE_PY git-context [--json]`
Analyzes git diff/status to suggest which files should be in context: modified files, test companions, co-changed files (from last 50 commits), and import chains.

### PreToolUse Read-Cache
Detects redundant file reads and optionally blocks them with structural digests.

**Default ON** (warn mode). Opt out: `TOKEN_OPTIMIZER_READ_CACHE=0` or config `{"read_cache_enabled": false}`.
**Modes**: `TOKEN_OPTIMIZER_READ_CACHE_MODE=warn` (default, suggests) or `=block` (prevents re-read).
**Decisions log**: Per-session files in `~/.claude/token-optimizer/read-cache/decisions/`
**Stats**: `python3 $MEASURE_PY read-cache-stats --session SESSION_ID`
**Clear**: `python3 $MEASURE_PY read-cache-clear`

### .contextignore
Create `.contextignore` in project root or `~/.claude/.contextignore` (global) to block files from being read. Uses gitignore-style glob patterns (fnmatch). Hard-blocks regardless of read-cache mode.

---

## Core Rules

- Quantify everything (X tokens, Y%)
- Create backups before any changes (`~/.claude/_backups/`)
- Ask user before implementing
- Never delete files, always archive
- Check dependencies before archiving (skills, MCP servers, deny rules can break other tools)
- Warn about side effects: deny rules block ALL tools, MCP removal breaks dependent skills, skill archival breaks @imports
- Prefer project-level deny rules over global (easier to debug, less blast radius)
- Use appropriate models (with fallbacks) for each task
- Show before/after diffs
- Frame savings as context budget (% of context window), not dollar amounts
