# Token Optimizer for GitHub Copilot

**Beta.** Per-session cost and token tracking, context-quality scoring, capability-gated context savers, and a dashboard for **GitHub Copilot**, both the **CLI** and **VS Code**.

Native Python. Reads Copilot's own data read-only. No telemetry. No dependencies.

Copilot now bills in **AI Credits**, and nothing in the product answers "what is this session costing me." This adapter does — using Copilot's own cost figures, never a re-derived pricing table.

## Two surfaces, one adapter

| Surface | Cost source | Engine |
|---|---|---|
| **Copilot CLI** | `session.shutdown` premium-request totals | hooks: bash compression, continuity restore, context nudges |
| **VS Code Copilot** | per-request `copilotUsageNanoAiu` (authoritative AI-credit cost) | analytics + dashboard (debug-logs or OTel trace DB) |

The two are **separate session populations** — never merged, never summed.

## What it does

- **Session cost + tokens.** AI credits burned (today / month / all-time), per-model token totals, cost pass-through from Copilot's own numbers.
- **Context-quality scoring.** S/A/B/C/D/F grades from the available session signals, with a Copilot-correct context-window table (128K default, never the 1M Claude fallback).
- **Capability-gated hook engine** (CLI). Bash output compression and session-start continuity restore activate only when your installed Copilot CLI version actually supports the hook field they need — and auto-activate when upstream fixes land. See "The capability map" in [`docs/copilot.md`](../docs/copilot.md).
- **Crash recovery.** A per-session in-flight tally recovers partial token counts for sessions that end without a clean shutdown, flagged honestly as estimated.
- **Dashboard + trends.** Copilot sessions flow into the local trends database and dashboard, with a credits-led summary command. Every cost figure is Copilot's own (premium requests / AI credits). The token-priced before/after transformation is intentionally not computed for Copilot: premium-request billing has no token-dollar counterfactual, so no savings headline is shown rather than a wrong one.

## Install

Run these from **any folder**. The first line creates a `token-optimizer/` folder, the second moves into it, the third installs:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --copilot
```

Preview without writing anything (from inside the `token-optimizer` folder): `bash install.sh --copilot --dry-run`.

**Already have Token Optimizer installed** (Claude Code plugin, script install, or a checkout under `~/.claude/skills/token-optimizer`)? Skip the clone — and don't look for `install.sh` inside the skill folder, it only exists at the repo root. The installer module ships with the skill, so run it directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=copilot python3 ~/.claude/skills/token-optimizer/scripts/measure.py copilot-install
```

Adjust the path if your `measure.py` lives elsewhere — any up-to-date copy works. Script installs can equivalently run `bash ~/.claude/token-optimizer/install.sh --copilot`.

The installer writes **user-level** hooks to `~/.copilot/hooks/token-optimizer.json` and copies the adapter into `~/.copilot/token-optimizer/plugin/`. It never writes repo-level (`.github/hooks/`) hooks — those would affect your whole team without consent. The install is idempotent and removes only its own files on uninstall.

For VS Code per-request credit costs, enable both `github.copilot.chat.agentDebugLog` settings (these log full prompt text to disk, so the switch is yours to flip).

## Commands

```bash
measure.py copilot-doctor      # per-source readiness + hook capability check
measure.py copilot-summary     # credits-led session summary
measure.py copilot-rollup      # ingest sessions into trends.db (auto on stop)
measure.py copilot-install     # wire hooks + seed capabilities
measure.py copilot-uninstall   # remove only what was installed
```

Run them with `TOKEN_OPTIMIZER_RUNTIME=copilot` set (the installer and hooks set it for you).

## Honest beta limits

GitHub Copilot CLI ships weekly and its hook fields break and regress between releases, so the adapter gates every engine feature on a per-version capability map. Two savers that need a currently-broken upstream field (Delta Mode / Structure Map, blocked by [github/copilot-cli#2585](https://github.com/github/copilot-cli/issues/2585)) are deferred until it works. The full feature-by-feature status — including what Copilot does not expose to a companion (per-request CLI token data, compaction steering) — lives in [`docs/copilot.md`](../docs/copilot.md).

## License

PolyForm Noncommercial 1.0.0. See [`../LICENSE`](../LICENSE).
