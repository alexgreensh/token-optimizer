# Codex Runtime: Chat-First Workflow

If this skill is running in Codex, use this section instead of the Claude Code phases. The user is not asking for a dashboard first. They are asking for a coach who can tell them:

- What is my status?
- What is my setup?
- What is wasteful or risky?
- What should we fix, and what should we leave alone?
- What behavior should I change during long Codex sessions?

## 0. Resolve `measure.py` for Codex

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

## 1. Start With Chat Status

Run these before giving advice:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" report
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" coach --json
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" quality current --json
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-state --json
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-doctor --project "$PWD" --json
```

`codex-state` reads Codex's versioned SQLite state read-only (`state_*.sqlite`, `goals_*.sqlite`): subagent count and token cost (with leaked/never-closed subagents), memory overhead from `stage1_outputs`, and goal budget utilization. It is empty-safe when those tables have no rows yet.

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
- Codex memory: X tokens of densified memory (state_*.sqlite stage1_outputs)
- Subagents: X total, Y leaked (open + stale), Z tokens of subagent cost
- Goals: X active, any budget-limited/usage-limited
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

## 2. Codex Setup Fixes

Use `codex-doctor` as the setup truth source.

If hooks are missing or stale:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --dry-run
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD"
```

Default to the balanced profile. Balanced means:

- `SessionStart`: recovery context when Codex can use it.
- `UserPromptSubmit`: prompt-quality and loop nudges.
- `SubagentStart`/`SubagentStop`: silent subagent counting with a sprawl nudge when too many run concurrently (opt out with `--no-subagent-hooks`).
- `Stop`: throttled dashboard refresh and continuity checkpoints.
- Codex compact prompt in `~/.codex/config.toml`.

Only suggest these when the user accepts the tradeoff:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --profile quiet
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --profile telemetry
TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project "$PWD" --profile aggressive
```

Quiet is Stop-only. Telemetry adds visible PostToolUse rows. Aggressive enables all current hooks, including experimental Bash PreToolUse. Do not make aggressive the default.

## 3. Codex Optimization Actions

Use these as the Codex equivalent of Phase 4:

| Action | What To Inspect | Safe Fix |
|---|---|---|
| AGENTS.md | Global/project `AGENTS.md` and `AGENTS.override.md` size, duplication, volatile content | Keep root guidance lean; move long workflows into skills or referenced docs |
| Codex memory | `codex-state` memory overhead (densified `stage1_outputs` in `state_*.sqlite`; the legacy `~/.codex/memories/` dir is deprecated) | Memory is auto-densified by Codex; flag only when overhead is large relative to the window |
| Subagent sprawl | `codex-state` subagent count, token cost, and leaked (open + stale) subagents | Close finished subagents; consolidate work; high leaked counts mean agents never closed cleanly |
| Goal budgets | `codex-state` goal utilization and budget-limited/usage-limited status | When a goal is budget/usage limited, raise its budget deliberately or split the work |
| Skills/plugin skills | `coach`, `report`, dashboard Manage data | Disable stale user skills with `measure.py codex-skill disable`; do not edit plugin cache directly |
| MCP servers | Codex config MCP inventory | Disable unused servers with `measure.py codex-mcp disable NAME` after checking whether the user actually uses them |
| Hooks | `codex-doctor`, `.codex/hooks.json` | Install/update balanced hooks; keep per-tool hooks opt-in |
| Compact guidance | `codex-compact-prompt --status` | Install `measure.py codex-compact-prompt --install`; use compact around phase boundaries |
| Quality/session rot | `quality current`, `coach` | Recommend `/compact`, `/clear`, rereads, or batching based on the actual score |
| Cost/model behavior | Trends/model mix when available | Codex uses intelligence levels (Low/Medium/High/Extra High) and model selection (GPT-5.5, GPT-5.4, GPT-5.4-Mini, GPT-5.3-Codex, GPT-5.2). Advise on reasoning effort settings, switching to GPT-5.4-Mini for routine tasks, and using lower intelligence for simple operations |

Always explain side effects before changing config. Prefer dry-runs before writes.

## 4. Codex Runtime Optimizations That Work Now

- Real Codex status in chat via `report` and `coach`.
- Context quality scoring from Codex JSONL where logs expose enough data, including OpenAI/GPT-5.5 long-context calibration.
- Balanced hooks for prompt-quality nudges, topic-relevant continuity hints, session continuity, and dashboard refresh.
- Quality-aware checkpoints that preserve score, weakest signals, model/window metadata, decisions, files, and next step.
- Stop-time backfill of large/high-signal Codex tool outputs into the local archive and SQLite session store, without enabling noisy per-tool hooks by default.
- Read-only Codex state metrics via `codex-state`: subagent token cost and leak detection (`thread_spawn_edges` + `threads`), memory overhead (`stage1_outputs`), and goal budget utilization (`thread_goals`). Historical and complete, no hooks required.
- Modern subagent parsing from `collab_agent_spawn_end`/`collab_close_end` events plus legacy `spawn_agent`, so multi-agent Codex sessions are no longer invisible.
- Compaction compression measured per session (post-compaction context vs. pre-compaction occupancy from `compacted.replacement_history` + per-turn token counts).
- Rate-limit, per-turn effort, and tool/turn duration signals parsed from the Codex transcript.
- Optional `SubagentStart`/`SubagentStop` hooks for real-time sprawl nudges.
- Compact prompt installation in `~/.codex/config.toml`.
- Codex native `[tui]` status line configuration (coexists with the v0.131+ blended-token display; never clobbers a custom config without `--force`).
- Skill/plugin/MCP inventory and enable/disable commands.
- Bounded log parsing so huge Codex transcripts do not burn CPU.
- Explicit file outline tools (`outline.py`, structure helpers) when the user asks to inspect a large file before rereading it.

## 5. Codex Features That Are Not Full Parity Yet

Be honest about these. Do not imply they are working invisibly:

- Delta read substitution is not active in Codex.
- Structure-map substitution is not active in Codex.
- Invisible Bash command rewriting/compression is not reliable in current Codex; keep it experimental and opt-in.
- Claude-style `PreCompact`/`PostCompact` hook parity is approximated with compact prompts and checkpoints; compaction quality is now measured after the fact from the transcript.
- Cache-write TTL breakdowns are limited by what Codex logs expose.
- Tool-result archiving in balanced mode happens at Stop, not immediately after each tool call.
- For generic Codex runtime/auth/network health, defer to the native `codex doctor`; Token Optimizer's `codex-doctor` focuses on token and optimization setup.

## 6. Codex Chat Close

After presenting status, ask for a concrete next step:

```
My recommendation: fix [one thing] first because it gives the most value with the least risk.
Want me to apply that, or do you want the conservative cleanup list first?
```

Do not end with only a dashboard link. The dashboard is supporting evidence, not the main experience.
