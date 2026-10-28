# Uninstall

Token Optimizer is additive and reversible. Every runtime has a clean
uninstall that removes only what we installed. Your own hooks, config keys,
and plugin entries are never clobbered. Session data and trends are left in
place by design (they're yours); each section below names the exact command
to ALSO purge that data if you want a full wipe.

## Claude Code

**Plugin install** (the recommended path):

```
/plugin uninstall token-optimizer@alexgreensh-token-optimizer
```

That removes the plugin and its hooks. To also drop the marketplace:

```
/plugin marketplace remove alexgreensh-token-optimizer
```

`/plugin uninstall` removes the plugin cache dir but does NOT clean the
plugin's keys in `installed_plugins.json` or `known_marketplaces.json`
(Claude Code ages orphaned entries out on its own schedule), and a manual
`rm -rf` of the plugin tree can leave the `statusLine` command in
`settings.json` pointing at a script whose plugin tree has been removed
(the status line then goes silently blank). The `cleanup` command below
reconciles both.

### One-command cleanup (recommended after `/plugin uninstall`)

```bash
# 1. Preview. Changes nothing, and lists every data path that is retained.
python3 ~/.claude/skills/token-optimizer/scripts/measure.py cleanup --dry-run

# 2. Apply. --confirm is required; a bare `cleanup` refuses and explains why,
#    so a mistyped --dry-run can never perform a real run.
python3 ~/.claude/skills/token-optimizer/scripts/measure.py cleanup --confirm
```

Add `--this-install-only` to either form when you deliberately run
side-by-side installs and want only this one removed (it scopes both the file
sweep and the OS scheduler cleanup to the active identity).

`cleanup` orchestrates the three uninstall surfaces in one call:

1. **Daemon**: stops and removes the dashboard daemon across ALL
   `token-optimizer-*` identities (not just the currently-resolved one),
   and unregisters every runtime's scheduler artifact (LaunchAgent /
   systemd unit / scheduled task) by name. A sibling identity's
   `dashboard-server.py` + `0600 daemon-token` (a live local HTTP daemon
   plus its CSRF secret) would otherwise outlive the uninstall. Launcher
   files another OS's daemon left in the same data dir (a HOME synced or
   migrated between machines) are swept too.
2. **Host config** (`settings.json`): backs up first, then removes ONLY
   Token Optimizer's own entries (`statusLine`, every Token Optimizer hook in
   every event, and the `compactInstructions` text `setup-smart-compact`
   wrote; if you had your own compact instructions, only our appended
   section is removed), leaving your other hooks and every other settings
   key byte-identical. `cleanupPeriodDays` is kept on purpose: removing it
   would let Claude Code start deleting your session transcripts.
3. **Manifests** (`installed_plugins.json` / `known_marketplaces.json`):
   backs up first, then removes our STALE entries (whose `installPath` no
   longer exists), leaving other plugins' entries byte-identical. Active
   (still-installed) entries of ours are reported but kept.

**`--dry-run` is side-effect-free** (no processes stopped, no files
deleted, no config written) and doubles as **retained-paths disclosure**:
it prints exactly what WOULD be removed and what stays, so you can review
before agreeing to a real cleanup.

**`--this-install-only`** scopes the daemon sweep to the currently-resolved
identity only, for users intentionally running side-by-side installs who
only want this one gone. Host config and manifest cleanup still run
(they are host-level, not identity-scoped).

### What is preserved by design

Session data, compaction checkpoints, trend aggregates, and the quality-bar
cache are NOT removed by `cleanup`. They are yours. The `--dry-run` report
lists every retained path with its on-disk status. To also purge them
(optional, full wipe):

```bash
rm -rf ~/.claude/plugins/data/token-optimizer-*   # all identities' session data
rm -rf ~/.cache/token-optimizer                   # interpreter + bytecode cache
```

### Per-component uninstall (script install or granular control)

`bash install.sh`, undo each opt-in component you enabled. Each
`--uninstall` removes ONLY Token Optimizer's own entries and leaves your
other hooks intact:

```bash
python3 ~/.claude/skills/token-optimizer/scripts/measure.py setup-smart-compact --uninstall
python3 ~/.claude/skills/token-optimizer/scripts/measure.py setup-quality-bar --uninstall
python3 ~/.claude/skills/token-optimizer/scripts/measure.py setup-daemon --uninstall
python3 ~/.claude/skills/token-optimizer/scripts/measure.py setup-coach-injection --uninstall
python3 ~/.claude/skills/token-optimizer/scripts/measure.py setup-hook --uninstall
```

`setup-daemon --uninstall` is identity-sweeping by default (all
`token-optimizer-*` identities). Add `--this-install-only` to clean only
the resolved identity. Add `--dry-run` to preview.

Then remove the skill tree and tracking data (optional, full wipe):

```bash
rm -rf ~/.claude/token-optimizer          # install dir: checkpoints, quality caches, config
rm -rf ~/.claude/skills/token-optimizer   # the skill tree
rm -rf ~/.claude/_backups/token-optimizer # session data, trends, dashboard, settings backups
rm -rf ~/.cache/token-optimizer           # interpreter + bytecode cache
rm -f  ~/.claude/.settings.lease          # advisory settings lease file
```

### Self-disabling status line

If the plugin tree is **partially** removed (e.g. a manual `rm -rf` that
leaves `statusline.js` on disk and referenced in `settings.json` but deletes
its sibling `measure.py`), the status line self-disables: it exits quietly
with no output and no stderr spew, so a dangling reference renders as a
blank line instead of a broken-command state. The command string shape is
not changed, so the existing uninstall matcher keeps working.

Scope, stated plainly: the guard covers the partial-removal case **only**.
When `statusline.js` itself is deleted — what `/plugin uninstall` and a full
`rm -rf` actually do — the guard cannot run, because there is no file left to
run it. Claude Code renders a `statusLine` whose command is missing as a
blank status line with no error surfaced (visible only under
`claude --debug`), which is the same end state, but it is the host's
behavior, not ours. The `statusLine` key itself still lingers in your
`settings.json` until it is removed — run `measure.py cleanup` (see below)
before uninstalling, or delete the key by hand afterwards.

## Codex

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --uninstall
```

This strips Token Optimizer hook groups from `~/.codex/hooks.json`, removes
the `# BEGIN/END token-optimizer compact prompt` block and the prompt file
from `~/.codex/config.toml`, and removes the `# BEGIN/END token-optimizer
status line` `[tui]` block (uncommenting any `status_line`/`terminal_title`
settings Token Optimizer commented out on a `--force` install). User-authored
config keys are never touched. Add `--dry-run` to preview.

Then remove the marketplace plugin via the Codex TUI (`/plugins`) or:

```bash
codex plugin marketplace remove alexgreensh/token-optimizer
```

Optional full wipe of Codex session/trends data:

```bash
rm -rf ~/.codex/token-optimizer
```

See [`docs/codex.md`](codex.md).

## GitHub Copilot

```bash
TOKEN_OPTIMIZER_RUNTIME=copilot python3 skills/token-optimizer/scripts/measure.py copilot-uninstall
```

Removes only the Token Optimizer hook entry from
`~/.copilot/hooks/token-optimizer.json`. **Copilot session data
(`~/.copilot/session-state/`, `~/.copilot/token-optimizer/`) is left in
place by design.** Token Optimizer reads Copilot's session logs but never
moves or owns them. To purge Token Optimizer's own data too:

```bash
rm -rf ~/.copilot/token-optimizer
```

For VS Code Copilot per-request cost tracking, disable the two
`github.copilot.chat.agentDebugLog` settings in VS Code. See
[`docs/copilot.md`](copilot.md).

## Cursor

```bash
TOKEN_OPTIMIZER_RUNTIME=cursor python3 skills/token-optimizer/scripts/measure.py cursor-uninstall
```

Removes only the Token Optimizer hook entries from `~/.cursor/hooks.json`
and the payload directory `~/.cursor/token-optimizer/plugin/`. Other tools'
hooks in the shared `hooks.json` are left intact. **Cursor session data
(`~/.cursor/token-optimizer/sessions/`, `restore-context/`,
`observed-events.jsonl`) is left in place by design.** Token Optimizer reads
Cursor's state and transcript files read-only but never moves or owns them.
To purge Token Optimizer's own data too:

```bash
rm -rf ~/.cursor/token-optimizer
```

See [`docs/cursor.md`](cursor.md).

## OpenCode

```bash
bash install.sh --opencode --uninstall
```

Removes `~/.config/opencode/plugins/token-optimizer.js` and reverts the
`token-optimizer-opencode` entry from `opencode.json`'s `plugin` array (if
present). Other plugin entries are left intact. Add `--dry-run` to preview.
The `~/.claude/skills` tree is owned by the standard installer; run
`bash install.sh` (no flag) to manage it. See
[`opencode/README.md`](../opencode/README.md).

## OpenClaw

OpenClaw uses its native plugin manager (no repo-side removal script):

```bash
openclaw plugins uninstall token-optimizer-openclaw --dry-run   # preview
openclaw plugins uninstall token-optimizer-openclaw             # remove
```

Optional full wipe of OpenClaw session/trends data:

```bash
rm -rf "${OPENCLAW_STATE_DIR:-$HOME/.local/state/openclaw}/token-optimizer"
```

See [`openclaw/README.md`](../openclaw/README.md).

## Hermes

```bash
token-optimizer/install.sh --hermes --uninstall
```

Then remove `- token-optimizer` from `plugins.enabled` in your Hermes config
so Hermes does not log a missing-plugin warning on the next start. See
[`hermes/README.md`](../hermes/README.md).

## VS Code

The Token Optimizer status-bar extension is a standard VS Code extension.
Remove it from the Extensions UI (`Cmd/Ctrl-Shift-X` → search "Token
Optimizer" → Uninstall), or:

```bash
code --uninstall-extension alexgreensh.token-optimizer-statusline
```

The companion skill tree (`~/.claude/skills/token-optimizer`) is owned by the
Claude Code installer. Remove it via the Claude Code uninstall steps above
if you want a full wipe.
