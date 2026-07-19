# Token Optimizer for Codex

Status: supported

**Your AI is getting dumber and you can't see it.**

*Find the ghost tokens. Survive compaction. Track the quality decay.*

Token Optimizer for Codex audits local Codex context usage, tracks real session token/cost data from Codex JSONL logs, and installs a balanced hook profile for quality tracking and session continuity. Pure Python stdlib, zero dependencies, zero telemetry.

## Status

Token Optimizer supports Codex with a Codex-native adapter. Core audit, coaching, dashboard, cost tracking, continuity, and fleet scanning work today. A few Claude Code mechanisms depend on hook APIs Codex does not expose yet, so those are implemented as Codex-safe equivalents or listed as explicit upstream gaps in the [Feature Parity](#feature-parity) table below.

## Install

**Recommended (marketplace, auto-updates on startup):**

```bash
codex plugin marketplace add alexgreensh/token-optimizer
```

Then in the Codex TUI: `/plugins` and install Token Optimizer.

> Auto-update: Codex auto-upgrades Git-backed marketplaces on startup via `git ls-remote`. Manual upgrade: `codex plugin marketplace upgrade`.

**After install, set up hooks globally (one-time):**

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install
```

This installs hooks to `~/.codex/hooks.json`, which Codex loads for all projects regardless of trust level. For per-project overrides, use `--project "$PWD"` instead.

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
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --profile quiet
```

## Usage

Inside Codex, invoke Token Optimizer conversationally:

- **"Run Token Optimizer"** -- status, setup, and safest next fix
- **"Run Token Coach"** -- make this project more token-efficient
- **"Run Fleet Auditor"** -- cross-system audit including Codex sessions
- **"Show the dashboard"** -- analytics dashboard

## Uninstall

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --uninstall
```

This reverses exactly what `codex-install` wrote:

- Strips Token Optimizer hook groups from `~/.codex/hooks.json` (your own
  hooks and other tools' hooks are left intact).
- Removes the `# BEGIN/END token-optimizer compact prompt` managed block and
  the prompt file (`~/.codex/token-optimizer/codex-compact-prompt.md`) from
  `~/.codex/config.toml`.
- Removes the `# BEGIN/END token-optimizer status line` `[tui]` block, and
  uncomments any `status_line`/`terminal_title` settings Token Optimizer
  commented out on a `--force` install. A `[tui]` header Token Optimizer
  added is dropped only if the table is left empty; user-authored `[tui]`
  content is never touched.

Add `--dry-run` to preview without writing. The uninstall is idempotent;
running it on a clean config is a no-op.

Then remove the marketplace plugin via the Codex TUI (`/plugins`) or:

```bash
codex plugin marketplace remove alexgreensh/token-optimizer
```

Codex session/trends data (`~/.codex/token-optimizer/`) is left in place by
design. To purge it too:

```bash
rm -rf ~/.codex/token-optimizer
```

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

Bookmark it. It auto-updates after every session. Runs on macOS (launchd), Linux (systemd --user), and Windows (Task Scheduler). Port 24843 is Codex-specific (Claude Code uses 24842, so both can run side by side). Remove anytime with `setup-daemon --uninstall`. The URL only resolves after `setup-daemon` has been run; until then it returns ERR_CONNECTION_REFUSED.

For LAN access on a headless box, set `TOKEN_OPTIMIZER_DASHBOARD_HOST=0.0.0.0` before running `setup-daemon`. The daemon runs under the service manager with an empty environment, so the chosen host is persisted to a `dashboard-host` file and re-read at startup. The setting is per-runtime (each of Claude/Codex/Hermes/Copilot persists its own). It is sticky across re-runs (and version-bump auto-regen) until you change it or run `setup-daemon --uninstall`. Allowed values: `127.0.0.1`, `localhost`, `0.0.0.0`. In network mode the dashboard is view-only for LAN visitors: the token endpoint is loopback-locked, so toggles work only from the machine running the daemon (LAN visitors can view but cannot fetch the token that gates mutations).

### File fallback

The dashboard file location is install-dependent. Run `TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard` and read the path on the `  Dashboard: ` line; that is the file to open. A common Codex location is `~/.codex/_backups/token-optimizer/dashboard.html` (legacy non-plugin layout), but do not assume it, read it from the command output.

Auto-refreshes via the balanced Stop hook after each session. Works without the daemon, just harder to reach.

## Feature Parity

### What's the same

These features work identically on Claude Code and Codex:

| Feature | Details |
|---------|---------|
| v6 dual-score quality scoring | Resource Health plus Session Efficiency, with Codex-calibrated GPT-5.x long-context curves. |
| Quality grades | S/A/B/C/D/F grades in dashboard, coach, CLI, and status line |
| Session continuity | Checkpoints preserve decisions, files, errors, and next step across compaction and session boundaries |
| Dashboard | Single-file HTML with per-turn token breakdown, cache analysis, cost tracking, quality overlays. Codex-native paths and copy |
| Fleet Auditor | Cross-system scanning across Claude Code, Codex, and custom transcript setups. Use the OpenClaw dashboard for OpenClaw runs |
| Token Coach | Conversational coaching adapted for AGENTS.md, Codex memories, intelligence levels, reasoning effort |
| Waste detectors | 11 detectors: PDF ingestion, web search overhead, retry churn, tool cascade, looping, overpowered model, weak model, bad decomposition, wasteful thinking, output waste, cache instability |
| Cost tracking | Per-turn costs with GPT-5.5/5.4/5.4-Mini/5.3-Codex/5.2 pricing |
| Memory/config audit | AGENTS.md audit (vs CLAUDE.md), Codex memories audit, skills/plugin/MCP inventory |
| Setup repair | `codex-doctor` with 20 readiness checks, guided hook install, compact prompt setup |
| Zero dependencies | Pure Python stdlib. No pip install, no telemetry |

### What's different

Codex and Claude Code have different hook surfaces, so some features work differently:

| Feature | Claude Code | Codex | Why |
|---------|------------|-------|-----|
| Config file | `CLAUDE.md` | `AGENTS.md` | Different platforms |
| Memory system | `MEMORY.md` + project memory dirs | `~/.codex/memories/**/*.md` | Different storage |
| Model routing advice | Opus/Sonnet/Haiku per-agent routing | Intelligence levels (Low/Medium/High/Extra High) + model selection (GPT-5.5, 5.4, 5.4-Mini, 5.3-Codex, 5.2) | Different model families |
| Hook install | Auto via plugin, 8 hook events | `codex-install` command (global by default), 4 profiles, 3-5 hook events | Codex hooks are newer, fewer events |
| Compact lifecycle | PreCompact + PostCompact hooks capture/restore | Compact prompt guidance + Stop checkpoints | Codex lacks PreCompact/PostCompact |
| Tool result archive | PostToolUse archives immediately per tool call | Stop-time backfill from JSONL (balanced), or PostToolUse (telemetry profile) | Different timing |
| Dashboard refresh | SessionEnd hook + daemon at `localhost:24842` | Stop hook + daemon at `localhost:24843` | Both support bookmarkable URL via `setup-daemon` |
| Plugin install | `/plugin marketplace add alexgreensh/token-optimizer` | `codex plugin marketplace add alexgreensh/token-optimizer` | Same concept, different CLI |
| Auto-update | Claude Code marketplace auto-update | Codex marketplace `git ls-remote` on startup | Both work |

### Upstream Codex API gaps

These Claude Code mechanisms need Codex API changes before they can work identically:

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

## Verify Setup

After installing or upgrading Codex support:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py report
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py dashboard --quiet
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-doctor --project "$PWD"
```

Expected health is `codex-doctor` with `0 FAIL`.

## Requirements

- Python 3.9+
- Codex CLI or Codex Desktop
- macOS, Linux, or Windows
- Zero runtime dependencies (pure Python stdlib)

## License

Same as the parent project: [PolyForm Noncommercial 1.0.0](../LICENSE).

---

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
