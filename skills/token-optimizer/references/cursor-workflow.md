# Cursor Runtime

You reached this file because the token-optimizer skill was invoked from inside
**Cursor** (IDE or `agent` CLI). Cursor loads `~/.claude/skills` by default, so
it can pick up this Claude Code skill even though the user is not in Claude Code.

**Do not run the Claude Code audit/fix phases.** They scan and modify `~/.claude`
(skills, plugins, settings, MEMORY.md). When the user is working in Cursor,
mutating `~/.claude` is the wrong target. Token Optimizer's own Cursor data lives
under `~/.cursor/token-optimizer/` (override with `TOKEN_OPTIMIZER_CURSOR_HOME`),
never `~/.claude`.

## How Token Optimizer works on Cursor

Cursor support is a hook bridge, not this Python audit. Once installed, the
bridge wires six Cursor hook events and runs automatically:

| Event | What Token Optimizer does |
|-------|---------------------------|
| `sessionStart` | Injects per-workspace continuity restore context (if a rollup produced one) |
| `preToolUse` (Shell) | Bash compression rewrite; fails closed (emits nothing) on any uncertainty |
| `postToolUse` | Updates the durable session tally; emits context-growth nudges |
| `preCompact` | Captures a compaction checkpoint in the tally |
| `stop` | Detached rollup (throttled to once per 120s per machine) |
| `sessionEnd` | Detached rollup + dashboard refresh |

All hooks are fail-open: they never block or deny a Cursor session, and every
event appends one line to `~/.cursor/token-optimizer/observed-events.jsonl` so
readiness is provable without a live Cursor.

## Commands

The audit phases do not run under Cursor. Use the Cursor-native commands
instead. Manual `cursor-summary` / `cursor-rollup` require the runtime prefix
(the bridge sets it automatically):

Resolve `$MEASURE_PY` and `$PY` first with the SKILL.md Step 0b snippet (this file is followed instead of the audit phases, so they are not set yet).

```bash
"$PY" "$MEASURE_PY" cursor-install     # wire Token Optimizer into ~/.cursor/hooks.json
"$PY" "$MEASURE_PY" cursor-doctor      # readiness report (checks + fix hints)
"$PY" "$MEASURE_PY" cursor-doctor --probe   # replay each wired event through the installed command
TOKEN_OPTIMIZER_RUNTIME=cursor "$PY" "$MEASURE_PY" cursor-summary
TOKEN_OPTIMIZER_RUNTIME=cursor "$PY" "$MEASURE_PY" cursor-rollup
"$PY" "$MEASURE_PY" cursor-uninstall   # remove only Token Optimizer's entries + payload
```

Install from a checkout with `bash install.sh --cursor` (merges into Cursor's
shared `~/.cursor/hooks.json`, preserving other tools' entries).

## Honest posture

Cursor stores no local billing data, so Token Optimizer reports **no cost /
savings headline** for Cursor (`cost_source = "cursor_no_cost_data"`). Bash
compression events are still counted. Token counts are best-effort: the hook
tally is authoritative for calls/turns/compactions; tokens come from
`state.vscdb` (IDE) or a chars-over-four transcript estimate (CLI).

## Why the Python skill stops here

- The audit is Claude Code / Codex specific: it scans `~/.claude` structure.
- Cursor stores config under `~/.cursor` with a different layout. A Claude-shaped
  audit would either error or mutate the wrong home.
- The bridge's data dir is `~/.cursor/token-optimizer/`, never `~/.claude`.

## Escape hatch

If the user genuinely wants to audit a Claude Code setup *from* Cursor (rare),
they can force the runtime:

```bash
TOKEN_OPTIMIZER_RUNTIME=claude "$PY" "$MEASURE_PY" report
```

This is opt-in and explicit. The default under Cursor is to never touch
`~/.claude`.
