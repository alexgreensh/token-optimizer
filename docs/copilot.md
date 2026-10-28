# Token Optimizer for GitHub Copilot (beta)

Token Optimizer's 6th platform. Two surfaces, one adapter:

- **Copilot CLI** — hooks engine + session analytics from `~/.copilot/session-state/`
- **VS Code Copilot** — per-request AI-credit cost from Copilot Chat debug logs

The value prop leads with **cost**: Copilot already shows context fill natively,
but since the June 2026 per-token AI-Credits billing change nothing answers
"what is this session costing me". Token Optimizer does, using Copilot's own
cost figures — never a re-derived pricing table.

## Install

Run from any folder. The clone creates a `token-optimizer/` folder; `cd` into it before running the installer:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --copilot
# verify (from inside the token-optimizer folder)
TOKEN_OPTIMIZER_RUNTIME=copilot python3 skills/token-optimizer/scripts/measure.py copilot-doctor
```

**Already have Token Optimizer installed** (Claude Code plugin, script install,
or a checkout under `~/.claude/skills/token-optimizer`)? You don't need a fresh
clone, and you won't find `install.sh` inside the skill folder — it only exists
at the repo root. Run the installer module you already have, directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=copilot python3 ~/.claude/skills/token-optimizer/scripts/measure.py copilot-install
TOKEN_OPTIMIZER_RUNTIME=copilot python3 ~/.claude/skills/token-optimizer/scripts/measure.py copilot-doctor
```

Adjust the path if your `measure.py` lives elsewhere — any up-to-date copy
works. Script installs can equivalently run
`bash ~/.claude/token-optimizer/install.sh --copilot`.

This writes user-level hooks to `~/.copilot/hooks/token-optimizer.json` only.
We deliberately never write `.github/hooks/` — repo-level hooks would affect
your whole team without consent.

### Windows (WSL): let it auto-detect — do NOT set `COPILOT_HOME`

Running `bash install.sh --copilot` from a Windows shell launches WSL as
**root**, so `$HOME=/root` and a naive install would land in `/root/.copilot`,
which the native-Windows Copilot CLI never reads. Token Optimizer now
**auto-detects** your real Windows Copilot home under
`/mnt/c/Users/<you>/.copilot` — you don't need to set anything. Just run:

```bash
bash install.sh --copilot
TOKEN_OPTIMIZER_RUNTIME=copilot python3 skills/token-optimizer/scripts/measure.py copilot-doctor
```

> **Do not set `COPILOT_HOME` to a `/mnt/...` path.** `COPILOT_HOME` is GitHub
> Copilot CLI's **own** configuration variable — setting it to a WSL `/mnt`
> path (meaningless on native Windows) makes Copilot relocate its own
> `session-state/`, `session.db`, and `events.jsonl` to a path that doesn't
> exist, so it silently stops logging (and downstream tools like Langfuse lose
> session output). If you previously set it, **unset it** and re-run.

Only if you have **multiple** Windows user profiles (auto-detect can't guess
which is yours) set Token Optimizer's own override — never Copilot's:

```bash
TOKEN_OPTIMIZER_COPILOT_HOME=/mnt/c/Users/<you>/.copilot bash install.sh --copilot
```

`TOKEN_OPTIMIZER_COPILOT_HOME` steers only Token Optimizer; the Copilot CLI
never reads it, so it can't disturb Copilot's own logging.

For VS Code per-request costs, enable both `github.copilot.chat.agentDebugLog`
settings. Note: those debug logs store full prompt text on disk — that is a
VS Code/Copilot behavior, and it's why the setting is opt-in.

## The capability map (why some features gate themselves)

Copilot CLI ships ~weekly and hook output fields break and regress between
releases. Instead of pretending otherwise, the adapter keeps a per-version
capability map (`~/.copilot/token-optimizer/capabilities.json`), reseeds it
whenever your CLI version changes (upgrades AND downgrades), and gates every
engine feature on it. When upstream fixes land, features auto-activate.

Verified status (research date 2026-06-10, CLI v1.0.60):

| Hook power | Status | Upstream reference | TO feature gated on it |
|---|---|---|---|
| `permissionDecision: deny/allow` | 🟢 works (allow ≥1.0.18) | upstream Copilot CLI | rewrite approval suppression |
| `updatedInput` (preToolUse) | 🟢 works ≥1.0.24 | v1.0.24 notes | bash output compression |
| `additionalContext` (sessionStart) | 🟢 works | upstream Copilot CLI | continuity restore |
| `additionalContext` (postToolUse) | 🟢 works ≥1.0.49 | v1.0.49/51 notes | context-growth nudges |
| `additionalContext` (preToolUse) | 🔴 broken upstream | upstream Copilot CLI (open) | read-interception (delta/structure-map) — deferred; needs this field |
| `additionalContext` (userPromptSubmitted) | 🔴 regressed in v1.0.60 | upstream Copilot CLI (open) | per-prompt quality steering |
| `systemMessage` | ⚪ unconfirmed on CLI | docs omit it | not used |

Escape hatch when upstream fixes outpace our matrix:
`TOKEN_OPTIMIZER_COPILOT_CAPS_JSON='{"pretooluse_ctx": true}'`.

## What Copilot does not expose to companions (honest gaps)

| Gap | Why | Tracking |
|---|---|---|
| Per-request CLI token counts | usage events are `ephemeral: true`, never written to disk; only `session.shutdown` aggregates persist | upstream Copilot CLI limitation |
| Compaction steering | compaction is server-side; no PreCompact injection point | — |
| Tool-output substitution | no `updatedOutput` on postToolUse | — |
| Live fill ring (VS Code) | in-memory only, never persisted | — |

Consequences and mitigations:

- **Crash-killed CLI sessions** never write their shutdown totals. The
  postToolUse hook maintains an in-flight tally and the rollup recovers
  partial data (persisted `assistant.message.outputTokens`, checkpoint and
  content estimates), flagged as `~est.` — never silently dropped, never
  silently exact.
- **Live fill on the CLI is an estimate**, calibrated against shutdown totals.
  Cost figures are always Copilot's own (`totalPremiumRequests` on the CLI,
  `copilotUsageNanoAiu` in VS Code).
- **Chat-only sessions** (no tool calls) receive grounding once at session
  start; postToolUse nudges need tool activity to fire.
- **Read-interception savers (Delta Mode / Structure Map) are not yet shipped
  on Copilot.** They require `preToolUse additionalContext`, which is broken
  upstream in the Copilot CLI. They will be added once that field works; until then bash
  output compression is the active CLI saver.
- **The two VS Code debug-log settings are enabled manually**, not by the
  installer. Writing VS Code's `settings.json` from the CLI is fragile across
  install kinds (stable/Insiders/remote/portable), so the installer prints the
  setting names and you flip them — keeping you in control of a switch that
  starts logging full prompt text to disk.

## Data sources, one active per surface

| Surface | Authoritative | Fallback | Never |
|---|---|---|---|
| CLI | `session-state/<id>/events.jsonl` (persisted events) | in-flight tally for crashed sessions | summing both |
| VS Code | debug-logs `main.jsonl` (per-request nanoAIU) | OTel `agent-traces.db` when debug-logs disabled | summing both |

`copilot-doctor` names which source is active for each surface.

## Commands

```bash
measure.py copilot-install     # wire hooks + seed capabilities
measure.py copilot-doctor      # per-source readiness + capability freshness
measure.py copilot-summary     # credits-led session summary
measure.py copilot-rollup      # ingest sessions into trends.db (auto on the sessionEnd hook)
measure.py copilot-uninstall   # remove only what we installed
```

## Uninstall

```bash
TOKEN_OPTIMIZER_RUNTIME=copilot python3 skills/token-optimizer/scripts/measure.py copilot-uninstall
```

Removes only the Token Optimizer hook entry from
`~/.copilot/hooks/token-optimizer.json`. Your own hooks and other tools'
hooks are left intact. The uninstall is idempotent; running it on a clean
config is a no-op.

**Copilot session data is left in place by design.** Token Optimizer reads
Copilot's session logs (`~/.copilot/session-state/`) but never moves or owns
them; your log-based scripts are unaffected. To purge Token Optimizer's own
data (capabilities, in-flight tallies, trends) too:

```bash
rm -rf ~/.copilot/token-optimizer
```

For VS Code Copilot per-request cost tracking, disable the two
`github.copilot.chat.agentDebugLog` settings in VS Code (the installer
enables them only by printing the names; you flip them).

Rates are configurable when GitHub changes billing:
`TOKEN_OPTIMIZER_COPILOT_USD_PER_CREDIT` (default 0.01),
`TOKEN_OPTIMIZER_COPILOT_PREMIUM_RATE` (default 0.04/premium request).

## Live-smoke runbook (first run on a machine with Copilot)

1. Install Copilot CLI and authenticate (`copilot`, then sign in).
2. `bash install.sh --copilot`
3. `measure.py copilot-doctor` — expect all green except VS Code (if unused).
4. Run one short Copilot CLI session that executes a shell command.
5. Confirm: `~/.copilot/token-optimizer/capabilities.json` matches your CLI
   version; `inflight-<session>.json` appeared during the session and was
   cleaned on exit; `copilot-summary` shows the session with premium-request
   cost; the trends DB gained a row after the sessionEnd rollup.
6. If a whitelisted command (e.g. `git status`) ran, check Copilot's transcript:
   the command should have been wrapped through `bash_compress.py`. If it ran
   unwrapped, your CLI version ignores `updatedInput` — file it against the
   capability matrix.
