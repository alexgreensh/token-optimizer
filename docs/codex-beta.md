# Token Optimizer for Codex Beta

Token Optimizer for Codex audits local Codex context usage, tracks real session token/cost data when Codex logs expose it, and installs a balanced hook profile for quality tracking and session continuity.

The beta is intentionally chat-first: when a user invokes Token Optimizer in Codex, it should tell them their status, setup, safest fixes, and behavior coaching before pointing at the dashboard.

## Install

From the Token Optimizer checkout or installed plugin directory:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD"
```

The default `codex-install` profile is `balanced`.

Balanced installs:

- `SessionStart` for session recovery context.
- `UserPromptSubmit` for prompt-quality and loop nudges.
- `Stop` for throttled dashboard refresh and continuity checkpointing.
- Codex compact prompt guidance in `~/.codex/config.toml`.

Optional profiles:

```bash
# Lowest visible hook noise, but weaker live quality tracking
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD" --profile quiet

# Adds PostToolUse telemetry; useful for QA, noisier in Codex Desktop
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD" --profile telemetry

# Enables all currently available Codex hooks, including experimental Bash PreToolUse
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD" --profile aggressive
```

## Dashboard

Generate the local dashboard:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard
```

Open:

```text
~/.codex/_backups/token-optimizer/dashboard.html
```

The source file at `skills/token-optimizer/assets/dashboard.html` is only a template. It intentionally has no local metrics injected.

## What Works In This Beta

- Codex-native `token-optimizer` chat workflow for status, setup, setup repair, and conservative next fixes.
- Codex-aware `token-coach` skill with `AGENTS.md`, Codex memories, balanced hooks, compact prompt, and reasoning-effort guidance.
- Codex-aware `token-dashboard` skill with the correct generated file under `~/.codex/_backups/token-optimizer/dashboard.html`.
- Codex-aware `fleet-auditor` skill and fleet adapter for `~/.codex/sessions` and `~/.codex/archived_sessions`.
- Codex-native dashboard title and copy.
- Real local Codex session parsing from available JSONL logs.
- API-equivalent token/cost calculations from logged usage.
- Context window detection from logged model metadata, Codex config, then model defaults.
- 7-signal context quality scoring where session data is available.
- Balanced hook install by default.
- Session continuity through `SessionStart`, throttled `Stop` checkpointing, and compact prompt guidance.
- Codex skills, MCP, and plugin inventory with enable/disable commands.
- Codex CLI status line support.
- `codex-doctor` readiness checks.

## Claude vs Codex Parity

| Product Surface | Claude Code | Codex Beta | Status |
|---|---|---|---|
| Main `token-optimizer` chat audit | Full deep audit of `CLAUDE.md`, memory, skills, MCP, hooks, commands, settings | Codex-native audit of `AGENTS.md`, Codex memories, skills/plugins, MCP, hooks, compact prompt, status line | Works, beta |
| Status/setup answer in chat | `report`, `quick`, `coach`, quality score | `report`, `quick`, `coach`, `quality current`, `codex-doctor` | Works |
| Guided setup repair | Installs Claude hooks, daemon, smart compaction, quality bar | Installs balanced Codex hooks, compact prompt, and optional status line | Works, different primitives |
| `token-coach` | Conversational coaching with Claude setup and multi-agent patterns | Conversational coaching translated to Codex setup, reasoning effort, compact behavior, and plugin surface | Works |
| Runtime quality nudges | `UserPromptSubmit` quality-cache warnings | `UserPromptSubmit` quality-cache warnings through Codex hook bridge | Works, depends on hook payload |
| Session continuity | `PreCompact`, `PostCompact`, `SessionStart`, `SessionEnd`, `StopFailure` | `SessionStart`, throttled `Stop`, compact prompt guidance | Partial parity |
| Dashboard | Auto-refresh via hooks/daemon, Claude paths | Auto-refresh via balanced Stop hook, Codex paths | Works |
| Fleet Auditor | Claude adapter plus other systems | Adds Codex adapter, still scans Claude/OpenClaw/etc. | Works, beta |
| Quick/health commands | Claude slash commands | Docs are Codex-aware, but Codex command exposure depends on Codex plugin command support | Partial |
| Delta read substitution | `PreToolUse Read` can replace repeated reads | Not active | Missing upstream hook parity |
| Structure-map substitution | Active with Claude tool interception | Not active | Missing upstream hook parity |
| Bash compression/rewrite | Active in Claude hook path | Experimental opt-in only | Partial |
| Cache TTL breakdowns | Claude cache read/write fields available | Codex exposes cached input but not Claude-style TTL write breakdowns | Partial |
| Skill usage telemetry | Claude trends can infer usage better | Codex logs do not expose all skill invocation signals yet | Partial |

## Known Codex API Gaps

These are shown honestly in the dashboard and should not be marketed as complete parity yet:

- Delta Mode read substitution is not active in Codex.
- Structure Map substitution is not active in Codex.
- True invisible Bash compression is experimental because current Codex hooks do not apply rewritten tool input the way Claude Code hooks can.
- Claude-style `PreCompact`, `PostCompact`, and `StopFailure` hook parity is approximated with compact prompts and checkpointing.
- Cache write TTL breakdowns are hidden because Codex logs do not expose Claude-style cache-write TTL fields.
- Tool-level hooks are still less complete than Claude Code; keep `PreToolUse` and `PostToolUse` opt-in until Codex exposes richer, stable payloads across tools.

## Release Gate

Before shipping a beta build:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py report
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard --quiet
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-doctor --project "$PWD"
ruff check skills/token-optimizer/scripts/measure.py skills/token-optimizer/scripts/codex_install.py skills/token-optimizer/scripts/codex_doctor.py
vulture skills/token-optimizer/scripts/measure.py skills/token-optimizer/scripts/codex_install.py skills/token-optimizer/scripts/codex_doctor.py --min-confidence 80
```

Expected beta readiness is `codex-doctor` with `0 FAIL`. A single warning for Codex API limitations is acceptable and should remain visible until upstream Codex exposes the missing hook/cache surfaces.
