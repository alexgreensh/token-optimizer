# Hook Architecture

This document describes Token Optimizer's hook system for security reviewers and pen testers. It covers what hooks fire, what data they access, what they write, and the security boundaries that constrain them.

## Execution Model

```
Host platform tool call
  -> Hook event (PreToolUse, PostToolUse, SessionStart, etc.)
    -> python-launcher.sh (shebang resolver)
      -> hooks/run.py (stdlib-only dispatcher)
        -> Consent check (fail-open, inline config read)
          -> Target script (measure.py, read_cache.py, etc.)
            -> Local file write (SQLite / JSON / markdown)
```

**Key properties:**
- `run.py` always exits 0 (never blocks a tool call, even on errors)
- 120-second timeout per hook invocation (child process killed on timeout to prevent SQLite lock starvation)
- Subprocess isolation: each hook script runs as a subprocess, not imported
- `run.py` is stdlib-only Python (~600 lines, no imports from the skills tree)

## Hook Inventory

| Event | Target Script | Purpose | Data Read | Data Written |
|-------|--------------|---------|-----------|-------------|
| **PreToolUse[Read]** | `read_cache.py --quiet` | Detect redundant file reads, serve structure maps | Session store SQLite, target file | Session store (file entry, cached content) |
| **PreToolUse[Bash]** | `bash_hook.py --quiet` | Bash output compression pre-check | None | None |
| **PreToolUse[Agent\|Task]** | `measure.py checkpoint-trigger --milestone pre-fanout` | Checkpoint before sub-agent fan-out | Session transcript | Checkpoint markdown file |
| **PreToolUse[mcp__.*]** | `refetch_guard.py --quiet` | Deny an exact duplicate MCP call after a large result was archived, and hand back the `expand` command | Session archive manifest (session store) | None |
| **PreCompact** (x3) | `measure.py dynamic-compact-instructions` | Generate context-aware compaction instructions | Session transcript, trends.db | Compact instructions (stdout) |
| | `measure.py compact-capture --trigger auto` | Capture checkpoint before compaction | Session transcript | Checkpoint markdown + events JSONL |
| | `read_cache.py --clear` | Clear read cache (context is about to compact) | None | Session store (cleared) |
| **SessionStart** (x1) | `sessionstart_runner.py` | Consolidated dispatcher: ensure-health, forced quality-cache warm, and (on a `compact` start) compact-restore + read-cache clear, then the new-session checkpoint pointer | Session transcript, checkpoint files, settings.json, config.json, session store | settings.json (cleanupPeriodDays; subagentPromptCacheTtl only under the `TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1` opt-in, when the cached verdict says it pays), config.json (consent backfill), quality-cache-*.json, session store (file_reads cleared), stdout injection |
| **StopFailure** | `measure.py compact-capture --trigger stop-failure` | Checkpoint on failure | Session transcript | Checkpoint markdown |
| **UserPromptSubmit** (x1) | `userpromptsubmit_runner.py` | Consolidated dispatcher: prompt-continuity, verbosity steer, quality-cache warn, and (gated to remote/container/Cowork or Codex sessions) ensure-health, forced cache warm, compact-restore | quality-cache-*.json, checkpoint files, config.json, settings.json | None (stdout injection) |
| **PostToolUse** (x1, consolidated) | `posttooluse_runner.py` | Consolidated dispatcher, one process per tool call: bash output compression (Bash), result archiving (Bash, Read, Glob, Grep, Agent, mcp__.*), context-intel scoring (Bash, Read, Grep, Glob, mcp__.*), read-cache invalidation (Edit, Write, MultiEdit, NotebookEdit), throttled quality-cache update | Tool output (stdin), session transcript | Session store (activity log, invalidated entries), tool-archive JSON (credential-redacted), quality-cache-*.json |
| **PostToolUseFailure[Bash]** | `posttooluse_runner.py` | Same consolidated dispatcher, failure-event branch (failed Bash output archived, not compressed) | Tool output (stdin) | As PostToolUse |
| **Stop** | `stop_runner.py` | Consolidated dispatcher: compact-capture (checkpoint on stop), session-end-flush (deferred metrics), keepwarm-arm | Session transcript | Checkpoint markdown + events JSONL, trends.db (session metrics) |
| **SessionEnd** | `stop_runner.py` (async, 60s) | Same consolidated runner, branching on the event name: full session flush (session-end-flush --trigger end --defer) | Session transcript, trends.db | trends.db, dashboard.html, checkpoint |
| **PostCompact** | `measure.py quality-cache --force` | Re-warm quality cache after compaction | Session transcript | quality-cache-*.json |
| **CwdChanged** | `read_cache.py --clear` | Clear read cache on directory change | None | Session store (cleared) |

## Security Boundaries

### No Shell Execution

- `run.py` uses `subprocess.Popen(cmd)` with a list of arguments, never `shell=True`
- `bash_hook.py` and `bash_compress.py` reject commands containing shell metacharacters: `;|&$(){}><\n\r\x00`
- Only a whitelist of safe environment variables (`HOME`, `PATH`, `LANG`, `TERM`, `USER`, `SHELL`, `TMPDIR`) can be passed through command rewrites
- **Worktree-isolated sessions skip Bash compression.** When the session cwd is under `.claude/worktrees/`, `bash_hook.py` passes commands through unrewritten. Claude Code's worktree isolation guard statically rejects the rewrite wrapper as "too complex," so rewriting there would refuse every whitelisted command; skipping compression is the correct trade-off (commands still run, they just aren't compressed).

### Path Traversal Protection

- Session IDs are sanitized to `[a-zA-Z0-9_-]` with UUID fallback
- Plugin data directory resolution rejects symlinks and requires all paths to resolve inside the runtime home
- Checkpoint trigger names are sanitized to prevent path injection in filenames

### Consent Gate

The consent check runs in `run.py` before any script is dispatched:

1. Read `config.json` from the env-derived config path (inline, no imports from skills tree)
2. If `enterprise_consent_shown` is True: proceed
3. If absent but `v5_welcome_shown` is True: backfill consent atomically, proceed
4. If neither: exit 0 (skip data collection, don't block the tool call)
5. On any error: exit 0 (fail-open)

### Data Isolation

- Each hook receives input via stdin (the host platform's hook payload)
- Hooks cannot access other hooks' stdin or intercept each other's output
- File writes are scoped to Token Optimizer's own data directories
- No hook reads or writes to the user's project source code (except reading files for the read cache, which is the host platform's Read tool operation being intercepted)

## What Hooks Modify in Host Platform Config

`ensure-health` (SessionStart) writes to the host platform's `settings.json`:

- `cleanupPeriodDays: 99999` (preserves transcripts for trend analysis)
- `subagentPromptCacheTtl: "1h"` (Claude Code 2.1.243+ only; advise-only by default -- the verdict is a recommendation in `subagent-cache status`/doctor/quick/coach, never a write; only `TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1` opts in to the automatic write + its 14-day payoff tripwire, `=0` never; skipped when the user, an env var, or a managed/project/local settings file already answers; the session-start hook only reads a cached verdict, and a detached background scan produces it; `measure.py subagent-cache enable` applies the recommendation, `disable` undoes what Token Optimizer set, finally)
- Daemon-related config (dashboard server plist registration on macOS)

These are the only modifications to host platform configuration. All other writes go to Token Optimizer's own data directories.

## Attack Surface Analysis

**Can hooks exfiltrate data?**
No. Zero network calls in the shipped hook tree. No HTTP clients, sockets, or DNS lookups imported. The only "network" code in the main plugin is the localhost-bound dashboard server. The separate Cowork diagnostics (`cowork/to-hook-probe`, which optionally POSTs a redacted env dump to a collector URL you configure, and `cowork/collector/`) do touch the network, but they ship only in the Cowork payload, not in this hook tree.

**Can hooks execute arbitrary code?**
No. No `eval()`, `exec()`, `importlib.import_module(variable)`, or dynamic code loading from external sources. All subprocess calls use static command lists.

**Can hooks modify user source code?**
No. Hooks only write to Token Optimizer's own data directories. The read cache reads project files but writes are to session store SQLite, not back to the source.

**Can a malicious actor register rogue hooks?**
The host platform's `settings.json` is user-writable. An attacker with local filesystem access could add malicious hook entries. Token Optimizer does not perform integrity verification on hook registrations. This is a documented limitation (see SECURITY.md).

**Can hook output influence the AI assistant?**
Yes, by design. Several hooks inject content into the conversation via stdout (checkpoint restore, quality warnings, compaction instructions). This content is controlled by Token Optimizer's own code, not by external input. The injected content is derived from locally stored data (checkpoints, quality scores) that was written by previous Token Optimizer hook invocations.

## Antigravity hooks

The Google Antigravity adapter (`agy` CLI, Antigravity 2.0 app, IDE) registers
a user-level plugin at `~/.gemini/config/plugins/token-optimizer/` with three
events:

| Event | Handler | Purpose |
|-------|---------|---------|
| `PreInvocation` | `antigravity_hook_bridge.py pre-invocation` | Continuity restore (invocation 1) or context nudge |
| `PreToolUse[run_command]` | `antigravity_hook_bridge.py pre-tool-use` | Bash output compression via `bash_compress.py` (R13a re-validates argv) |
| `Stop` | `antigravity_hook_bridge.py stop` | Detached, lease-debounced rollup + dashboard regeneration |

Two disclosures specific to this adapter:

- **Consent gate lives in the bridge.** Unlike the Claude `run.py` consent
  check, the Antigravity bridge reads `~/.gemini/token-optimizer/config.json`
  itself; every handler no-ops to `{}` until the installer records
  `antigravity_consent: true` (R20). It fails open and never blocks a turn.
- **Injected continuity text derives from conversation titles.** The
  `PreInvocation` restore summarizes the previous session's model, token totals,
  topic, and workspace; topic/workspace strings originate from Antigravity's
  `conversation_summaries.db` and are treated as untrusted, filtered to
  printable characters, and capped at 200 characters per field (R22).

## Desktop status bar

The plugin's `hooks/hooks.json` also names one hooks module under `modules`: `desktop/token-optimizer-desktop/hooks/register.tsx`. It is not a hook runner: it uses Claude Code's mods feature (2.1.287+) to draw a status bar above the prompt in the Claude desktop app. It adds no entries to the hook inventory above and changes nothing for terminal or VS Code sessions. Older Claude Code versions skip the module and run every hook above as before. The Codex and Cowork builds leave it out.

- **Install**: comes with the main plugin; nothing extra to install.
- **Reads**: Token Optimizer's local data (its quality cache, the installed-plugins list, and `measure.py status-bar`), the session itself, and the current git branch. No network access of its own.
- **Writes**: no files of its own. `status-bar` keeps a small per-session cache under Token Optimizer's data folder, refreshed in the background, and the plugin keeps its own state in Claude Code (its stored values and a pending Start fresh hand-off). Its three buttons act only on a click: Clean up compacts using Token Optimizer's guidance, Start fresh saves a checkpoint and clears after a second click, and Keep warm sends a manual one-click cache refresh that uses a small amount of usage.
- **Switch**: Anthropic can turn mods off remotely. When off, the bar does not appear and nothing else changes.
- **Settings**: `TOKEN_OPTIMIZER_STATUS_BAR=0` hides the bar and `TOKEN_OPTIMIZER_STATUS_BAR_ANIMATE=0` keeps Clawd still, set in the `env` block of `~/.claude/settings.json` like the other Token Optimizer switches.

## Generating a Security Report

```
python3 measure.py security-report        # human-readable report
python3 measure.py security-report --json  # machine-readable for automated assessment
```

The report covers: installed hooks, their target scripts, data store inventory with file permissions, consent status, retention configuration, and credential scanning coverage.
