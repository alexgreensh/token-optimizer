# Token Optimizer for Cursor

**Beta.** Per-session token accounting, context-quality scoring, Bash-output compression, and per-workspace continuity for **Cursor**, both the **IDE (Composer)** and the **CLI (`agent`)**.

Native Python. Reads Cursor's own data read-only. No telemetry. No dependencies.

Cursor shows context usage natively but never persists per-request token totals in one place, and it hands hooks no cost figure. Token Optimizer closes the accounting gap and is honest about the cost gap: where Cursor exposes no number, it says so instead of inventing one.

## Two surfaces, one adapter

| Surface | Token source | Engine |
|---|---|---|
| **Cursor IDE** | `state.vscdb` per-request token fields (primary) | six-event hooks + dashboard |
| **Cursor CLI** | transcript estimate (fallback) | same adapter, same hooks |

The two share one adapter and one session population keyed by `conversation_id`.

## What it does

- **Session token accounting.** Reads Cursor's own stored token fields, with a strict trust ordering (`cursor_state_vscdb` → `cursor_transcript_estimate` → `cursor_tally_only`) and never sums sources.
- **Context-quality scoring.** S/A/B/C/D/F grades from the signals Cursor persists, with a single-signal fallback when the richer schema is absent.
- **Bash output compression.** The `preToolUse` (Shell) hook rewrites whitelisted commands through the existing compressor, fail-closed on any uncertainty. Honoured/ignored evidence lands in `observed-events.jsonl`.
- **Per-workspace continuity.** `sessionStart` restores the right repo's context (keyed by workspace hash), never on stale input and never while a sibling chat is live.
- **Crash recovery.** A per-session in-flight tally recovers partial session data for composers that never fired a clean `sessionEnd`, flagged honestly as `~est.`.
- **Doctor with a live probe.** `cursor-doctor --probe` replays the documented payloads through the installed hook `command`, so the six wired events are proven on your build rather than assumed.
- **Honest cost posture.** Cursor exposes no billing number to hooks or state, so `cost_source` is `cursor_no_cost_data`; no savings headline is shown rather than a wrong one.

## Install

Run these from **any folder**. The first line creates a `token-optimizer/` folder, the second moves into it, the third installs:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --cursor
```

Preview without writing anything (from inside the `token-optimizer` folder): `bash install.sh --cursor --dry-run`.

**Already have Token Optimizer installed** (Claude Code plugin, script install, or a checkout under `~/.claude/skills/token-optimizer`)? Skip the clone. The installer module ships with the skill, so run it directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=cursor python3 ~/.claude/skills/token-optimizer/scripts/measure.py cursor-install
```

The installer read-merges user-level hooks into `~/.cursor/hooks.json` (Cursor has one shared hooks file, so foreign entries are preserved) and copies the adapter to `~/.cursor/token-optimizer/plugin/`. It is idempotent and removes only its own files on uninstall.

## Commands

```bash
measure.py cursor-doctor      # readiness + hook-firing probe
measure.py cursor-summary     # token/quality session summary
measure.py cursor-rollup      # ingest sessions into trends.db (auto on stop/sessionEnd)
measure.py cursor-install     # read-merge hooks.json + copy the payload
measure.py cursor-uninstall   # remove only what was installed
```

Run them with `TOKEN_OPTIMIZER_RUNTIME=cursor` set (the installer and hooks set it for you).

## Honest beta limits

The adapter covers the Cursor IDE and the local agent CLI only. Cursor cloud agents and self-hosted background workers load project-level `.cursor/hooks.json` and Enterprise team or managed hooks, never the user-level `~/.cursor/hooks.json` this installer writes, so Token Optimizer does not observe those runs.

Cursor exposes no billing figure to a companion, so per-session cost is a genuine red cell rather than an approximation: `cursor_no_cost_data` is surfaced, never a fake dollar number. On native Windows the installer writes hook commands for PowerShell and cmd.exe (a bare interpreter path and double-quoted arguments, the same form the Codex hooks use); Windows execution has not been validated on a live host. Compaction is observational — `preCompact` records Cursor's real context numbers, it cannot steer them. The full feature-by-feature status lives in [`docs/cursor.md`](../docs/cursor.md) and the [capability matrix](https://alexgreensh.github.io/token-optimizer/reference/capability-matrix/).

## License

PolyForm Noncommercial 1.0.0. See [`../LICENSE`](../LICENSE).
