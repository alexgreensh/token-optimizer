# OpenCode Runtime

You reached this file because the token-optimizer skill was invoked from inside
**OpenCode**. OpenCode loads `~/.claude/skills` by default, so it can pick up
this Claude Code skill even though the user is not in Claude Code.

**Do not run the Claude Code audit/fix phases.** They scan and modify `~/.claude`
(skills, plugins, settings, MEMORY.md). When the user is working in OpenCode,
mutating `~/.claude` is the wrong target.

## What to tell the user

Token Optimizer already supports OpenCode through a dedicated, native plugin —
not this Python audit. On OpenCode it works automatically:

- **Live tracking** of context quality, realized savings, and session continuity
  via OpenCode session hooks (no audit run needed).
- **Dashboard / status**: invoke the `token_dashboard` tool (HTML dashboard) or
  the `token_status` tool (in-chat snapshot).

Install / verify the OpenCode plugin:

```jsonc
// opencode.json
{
  "plugin": ["token-optimizer-opencode"]
}
```

Or drop a local build into `~/.config/opencode/plugins/` (OpenCode auto-loads it).

## Why the Python skill stops here

- The Python skill's audit is Claude Code / Codex specific: it scans
  `~/.claude` structure (skills, plugin cache, `settings.json`, `MEMORY.md`).
- OpenCode stores config under `~/.config/opencode` and data under
  `~/.local/share/opencode` (XDG), with a different layout. A Claude-shaped audit
  would either error or, worse, mutate the wrong home.
- Token Optimizer's own data, when this skill does run any runtime-data command
  under OpenCode, now resolves to OpenCode's data dir (`~/.local/share/opencode`),
  never `~/.claude` — see `runtime_env.opencode_data_home()`.

## Escape hatch

If the user genuinely wants to audit a Claude Code setup *from* OpenCode (rare),
they can force the runtime:

Resolve `$MEASURE_PY` and `$PY` first with the SKILL.md Step 0b snippet (this file is followed instead of the audit phases, so they are not set yet).

```bash
TOKEN_OPTIMIZER_RUNTIME=claude "$PY" "$MEASURE_PY" report
```

This is opt-in and explicit. The default under OpenCode is to never touch
`~/.claude`.

## Refreshing the skill tree (OpenCode-only users)

OpenCode loads skill content from `~/.claude/skills`. If SKILL.md or reference
files look stale, the skill symlink can be refreshed by re-running the
installer. **However**, the default `install.sh` also installs Claude Code
hooks into `~/.claude/settings.json` (via `measure.py setup-all-hooks`).

- If you use **both** Claude Code and OpenCode on this host: `bash install.sh`
  is safe — the hooks are needed for Claude Code anyway.
- If you use **OpenCode only**: the Claude hooks are unnecessary. After
  running `install.sh`, remove them with:
  ```bash
  "$PY" ~/.claude/token-optimizer/skills/token-optimizer/scripts/measure.py cleanup-duplicate-hooks
  ```
  Or simply re-create the symlink manually:
  ```bash
  ln -sfn ~/.claude/token-optimizer/skills/token-optimizer ~/.claude/skills/token-optimizer
  ```
