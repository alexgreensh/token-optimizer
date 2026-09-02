# Token Optimizer for Google Antigravity

**Beta.** Per-session token and cost tracking, context-quality scoring, bash output compression, continuity restore, and a dashboard for **Google Antigravity** — the `agy` CLI, the Antigravity 2.0 desktop app, and the Antigravity IDE.

Pure stdlib. Reads Antigravity's own data read-only. No telemetry. No dependencies.

Antigravity is subscription/credit-metered; nothing in the product answers "what is this session costing me" at token granularity. This adapter reports token totals per session and, where the model has a known Gemini rate card, a **list-price estimate** clearly labelled as such. When Antigravity reports its own credit figures, those are shown instead of a derived dollar number.

## Three surfaces, one adapter

| Surface | Storage | Hooks |
|---|---|---|
| **`agy` CLI** | `~/.gemini/antigravity-cli/conversations/*.db` | PreInvocation, PreToolUse, Stop |
| **Antigravity 2.0 app** | `~/.gemini/antigravity/conversations/*.db` | same hooks.json |
| **Antigravity IDE** | `~/.gemini/antigravity-ide/conversations/*.db` | same hooks.json |

The three are **separate session populations** — never merged, never summed. Every
session is tagged with its surface.

## What it does

- **Session token + cost totals.** Input / output / cache-read / thinking tokens per session, decoded from Antigravity's own `gen_metadata` SQLite blobs with a zero-dependency protobuf reader (decoder version `ag-v1`).
- **Context fill + model.** Estimated tokens used vs. max context and the model for each session.
- **Context-quality scoring.** S/A/B/C/D/F grades from the same three-signal subset the Hermes and Copilot adapters use.
- **Bash output compression.** The `run_command` hook rewrites whitelisted commands through `bash_compress.py`, and that wrapper re-validates its argv itself (R13a), so a cached "always allow" can never turn it into an unguarded runner.
- **Continuity restore.** The `PreInvocation #1` hook injects a one-line summary of your previous session; titles and workspace paths are filtered through the same printable-only, length-capped `R22` filter as every other adapter.
- **Stop-time rollup + dashboard.** Sessions flow into the local trends DB on Stop; the dashboard daemon runs on port **24846**.

## Install

Run these from **any folder**:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --antigravity
```

Preview without writing anything (from inside the `token-optimizer` folder): `bash install.sh --antigravity --dry-run`.

**Already have Token Optimizer installed** (Claude Code plugin, script install, or a checkout under `~/.claude/skills/token-optimizer`)? Skip the clone — `install.sh` only lives at the repo root. Run the installer module you already have, directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 ~/.claude/skills/token-optimizer/scripts/measure.py antigravity-install
```

Adjust the path if your `measure.py` lives elsewhere.

The installer writes a **user-level plugin directory** at `~/.gemini/config/plugins/token-optimizer/` (a `plugin.json` + `hooks.json` + the adapter payload). It never touches your own `~/.gemini/config/hooks.json`, `config.json`, or `settings.json`. The install is idempotent and uninstalls only that directory.

### Consent (R20)

Data collection is **off by default**. The installer prints the data notice and records consent in `~/.gemini/token-optimizer/config.json`; until that record exists the hooks and rollup are no-ops. Uninstalling removes the plugin directory but deliberately leaves `~/.gemini/token-optimizer/` (your collected data) and `~/.gemini/` conversation data in place. To purge Token Optimizer's own data too:

```bash
rm -rf ~/.gemini/token-optimizer
```

## Commands

```bash
measure.py antigravity-install     # wire the plugin + record consent
measure.py antigravity-doctor      # readiness check (binary, home, plugin, store)
measure.py antigravity-summary     # per-surface session summary
measure.py antigravity-rollup      # ingest sessions into trends.db (auto on Stop)
measure.py antigravity-uninstall   # remove only what was installed
measure.py antigravity-home        # print the resolved Antigravity home
```

Run them with `TOKEN_OPTIMIZER_RUNTIME=antigravity` set (the installer and hooks set it for you).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN_OPTIMIZER_ANTIGRAVITY_HOME` | `~/.gemini` | Relocated Antigravity home (accepted only under `$HOME`). |
| `TOKEN_OPTIMIZER_ANTIGRAVITY_BIN` | first `agy` on `PATH` | Override the `agy` binary the doctor probes. |
| `TOKEN_OPTIMIZER_BASH_COMPRESS` | `1` (on) | Set `0` to disable command-output compression. |

## Honest beta limits

Antigravity's hook contract is synchronous and command-only, with no
`PreCompact`, no `SessionStart`/`SessionEnd`, and no payload token counts. The
full feature-by-feature status lives in [`docs/antigravity.md`](../docs/antigravity.md):

- **green** — token/cache/thinking totals, model, duration, tool-call counts, context fill, quality grade, trends + dashboard, continuity restore, bash compression, stop-time rollup, doctor, install/uninstall.
- **partial** — context-growth nudges (derived from the live conversation's last decoded generation, not hook payload), crash recovery (a killed conversation flags its partial totals as incomplete).
- **red** — compaction steering (no PreCompact), read-interception / Delta Mode / Structure Map (no PostToolUse output rewrite), status line (Antigravity owns its status-line data model), keep-warm.

Pricing is the same list-price estimate shown to users on the Gemini rate card;
it is never presented as a measured bill.

## License

PolyForm Noncommercial 1.0.0. See [`../LICENSE`](../LICENSE).
