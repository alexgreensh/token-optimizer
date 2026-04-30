# Token Optimizer for Codex Beta

Token Optimizer for Codex audits local Codex context usage, tracks real session token/cost data when Codex logs expose it, and installs a balanced hook profile for quality tracking and session continuity.

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
