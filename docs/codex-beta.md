# Token Optimizer for Codex

Version: `0.1.0-beta`

**Your AI is getting dumber and you can't see it.**

*Find the ghost tokens. Survive compaction. Track the quality decay.*

Token Optimizer for Codex audits local Codex context usage, tracks real session token/cost data from Codex JSONL logs, and installs a balanced hook profile for quality tracking and session continuity. Pure Python stdlib, zero dependencies, zero telemetry.

## Status

This is a **beta release**. The core audit, coaching, dashboard, and fleet scanning work. Some advanced features are waiting on upstream Codex API surfaces. See the [Feature Parity](#feature-parity) table below.

## Install

**Recommended (marketplace, auto-updates on startup):**

```bash
codex plugin marketplace add alexgreensh/token-optimizer
```

Then in the Codex TUI: `/plugins` and install Token Optimizer.

> Auto-update: Codex auto-upgrades Git-backed marketplaces on startup via `git ls-remote`. Manual upgrade: `codex plugin marketplace upgrade`.

**After install, set up hooks for your project:**

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD"
```

The default profile is `balanced`. It installs:

- `SessionStart` for session recovery context.
- `UserPromptSubmit` for prompt-quality and loop nudges.
- `Stop` for throttled dashboard refresh and continuity checkpointing.
- Codex compact prompt guidance in `~/.codex/config.toml`.

### Hook profiles

| Profile | What it installs | Noise level |
|---------|-----------------|-------------|
| `balanced` (default) | SessionStart + UserPromptSubmit + Stop + compact prompt | Low, 3 hook events |
| `quiet` | Stop only | Minimal, 1 hook event |
| `telemetry` | Balanced + PostToolUse | Medium, visible rows in Desktop |
| `aggressive` | All hooks including experimental Bash PreToolUse | High, full coverage |

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD" --profile quiet
```

## Usage

Inside Codex, invoke Token Optimizer conversationally:

- **"Run Token Optimizer"** -- status, setup, and safest next fix
- **"Run Token Coach"** -- make this project more token-efficient
- **"Run Fleet Auditor"** -- cross-system audit including Codex sessions
- **"Show the dashboard"** -- analytics dashboard

### CLI commands

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py report
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py coach
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py quality current
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-doctor --project "$PWD"
```

## Dashboard

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard
```

### Bookmarkable URL (recommended)

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py setup-daemon
```

This installs a tiny local web server that starts at login and serves the dashboard at:

```
http://localhost:24843/token-optimizer
```

Bookmark it. It auto-updates after every session. Runs on macOS (launchd), Linux (systemd --user), and Windows (Task Scheduler). Port 24843 is Codex-specific (Claude Code uses 24842, so both can run side by side). Remove anytime with `setup-daemon --uninstall`.

### File fallback

Dashboard file: `~/.codex/_backups/token-optimizer/dashboard.html`

Auto-refreshes via the balanced Stop hook after each session. Works without the daemon, just harder to reach.

## Feature Parity

### What's the same

These features work identically on Claude Code and Codex:

| Feature | Details |
|---------|---------|
| 7-signal quality scoring | Context fill, stale reads, bloated results, compaction depth, duplicates, decision density, agent efficiency. GPT-5.5 long-context calibration for Codex. |
| Quality grades | S/A/B/C/D/F grades in dashboard, coach, CLI, and status line |
| Session continuity | Checkpoints preserve decisions, files, errors, and next step across compaction and session boundaries |
| Dashboard | Single-file HTML with per-turn token breakdown, cache analysis, cost tracking, quality overlays. Codex-native paths and copy |
| Fleet Auditor | Cross-system scanning across Claude Code, Codex, OpenClaw, and others. Codex adapter parses `~/.codex/sessions/` |
| Token Coach | Conversational coaching adapted for AGENTS.md, Codex memories, intelligence levels, reasoning effort |
| Waste detectors | 11 detectors: PDF ingestion, web search overhead, retry churn, tool cascade, looping, overpowered model, weak model, bad decomposition, wasteful thinking, output waste, cache instability |
| Cost tracking | Per-turn costs with GPT-5.5/5.4/5.4-Mini/5.3-Codex/5.2 pricing |
| Memory/config audit | AGENTS.md audit (vs CLAUDE.md), Codex memories audit, skills/plugin/MCP inventory |
| Setup repair | `codex-doctor` with 20 readiness checks, guided hook install, compact prompt setup |
| Zero dependencies | Pure Python stdlib. No pip install, no network calls, no telemetry |

### What's different

Codex and Claude Code have different hook surfaces, so some features work differently:

| Feature | Claude Code | Codex | Why |
|---------|------------|-------|-----|
| Config file | `CLAUDE.md` | `AGENTS.md` | Different platforms |
| Memory system | `MEMORY.md` + project memory dirs | `~/.codex/memories/**/*.md` | Different storage |
| Model routing advice | Opus/Sonnet/Haiku per-agent routing | Intelligence levels (Low/Medium/High/Extra High) + model selection (GPT-5.5, 5.4, 5.4-Mini, 5.3-Codex, 5.2) | Different model families |
| Hook install | Auto via plugin, 8 hook events | `codex-install` command, 4 profiles, 3-5 hook events | Codex hooks are newer, fewer events |
| Compact lifecycle | PreCompact + PostCompact hooks capture/restore | Compact prompt guidance + Stop checkpoints | Codex lacks PreCompact/PostCompact |
| Tool result archive | PostToolUse archives immediately per tool call | Stop-time backfill from JSONL (balanced), or PostToolUse (telemetry profile) | Different timing |
| Dashboard refresh | SessionEnd hook + daemon at `localhost:24842` | Stop hook + daemon at `localhost:24843` | Both support bookmarkable URL via `setup-daemon` |
| Plugin install | `/plugin marketplace add alexgreensh/token-optimizer` | `codex plugin marketplace add alexgreensh/token-optimizer` | Same concept, different CLI |
| Auto-update | Claude Code marketplace auto-update | Codex marketplace `git ls-remote` on startup | Both work |

### What's waiting on upstream Codex

These features need Codex API changes before they can work:

| Feature | Claude Code | Codex | Blocker |
|---------|------------|-------|---------|
| Delta read substitution | PreToolUse Read returns diff instead of full file | Not active | Codex PreToolUse Read hook doesn't support `updatedInput` |
| Structure-map substitution | PreToolUse Read returns AST skeleton for re-reads | Not active | Same blocker |
| Invisible Bash compression | PreToolUse Bash rewrites commands transparently | Experimental opt-in only | Codex hooks can't rewrite tool input silently |
| Cache-write TTL breakdowns | Full 1h/5m cache-write split visible | Cached input shown, no TTL split | Codex logs don't expose cache-write TTL fields |
| StopFailure recovery | Dedicated hook fires on crash/timeout | Approximated with Stop + compact prompt | No StopFailure hook in Codex |
| Skill usage telemetry | Per-skill invocation tracking from trends | Partial, limited log signals | Codex logs don't expose all skill invocation events |

## Codex Models and Pricing

Token Optimizer tracks costs for all Codex models:

| Model | Input ($/1M) | Cached ($/1M) | Output ($/1M) |
|-------|-------------|---------------|---------------|
| GPT-5.5 | $5.00 | $0.50 | $30.00 |
| GPT-5.4 | $2.50 | $0.25 | $15.00 |
| GPT-5.4-Mini | $0.75 | $0.075 | $4.50 |
| GPT-5.3-Codex | $1.75 | $0.175 | $14.00 |
| GPT-5.2 | $1.75 | $0.175 | $14.00 |

Prices sourced from OpenAI API pricing. Dashboard shows per-turn costs using the model detected from session logs.

## Release Gate

Before shipping a beta build:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py report
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard --quiet
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-doctor --project "$PWD"
ruff check skills/token-optimizer/scripts/measure.py skills/token-optimizer/scripts/codex_install.py skills/token-optimizer/scripts/codex_doctor.py
vulture skills/token-optimizer/scripts/measure.py skills/token-optimizer/scripts/codex_install.py skills/token-optimizer/scripts/codex_doctor.py --min-confidence 80
```

Expected beta readiness is `codex-doctor` with `0 FAIL`.

## Requirements

- Python 3.8+
- Codex CLI or Codex Desktop
- macOS, Linux, or Windows
- Zero runtime dependencies (pure Python stdlib)

## License

Same as the parent project: [PolyForm Noncommercial 1.0.0](../LICENSE).

---

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
