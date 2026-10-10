---
description: Checks running Claude Code or Codex sessions, finds zombies, and offers a safe cleanup. Use when sessions feel stuck or many are open.
---

# Session Health Check

Run a session health check and help the user manage running sessions safely.

## Steps

1. Resolve measure.py path:
```bash
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  # Env signals are authoritative and checked before directory heuristics: a host
  # with BOTH ~/.codex and ~/.config/opencode (running OpenCode) must resolve to
  # opencode, not codex, so the tool never reaches into ~/.claude.
  if [ -n "$CLAUDE_PLUGIN_ROOT" ] || [ -n "$CLAUDE_PLUGIN_DATA" ]; then
    RUNTIME="claude"
  elif [ -n "$OPENCODE" ] || [ -n "$OPENCODE_BIN" ] || [ -n "$OPENCODE_CONFIG_DIR" ] || [ -n "$OPENCODE_CONFIG" ]; then
    RUNTIME="opencode"
  elif [ -n "$CODEX_HOME" ]; then
    RUNTIME="codex"
  elif [ -n "$CLAUDECODE" ] || [ -n "$CLAUDE_CODE_ENTRYPOINT" ] || [ -n "$CLAUDE_CODE_SESSION_ID" ]; then
    RUNTIME="claude"
  elif [ -d "$HOME/.config/opencode" ] && [ ! -d "$HOME/.codex" ]; then
    RUNTIME="opencode"
  elif [ -d "$HOME/.codex" ]; then
    RUNTIME="codex"
  else
    RUNTIME="claude"
  fi
fi
# Resolve measure.py to the NEWEST installed copy across channels so a stale
# plugin-cache copy never shadows a fresh install. find -L follows the
# install.sh symlink under ~/.claude/skills; cd -P resolves it before reading each
# copy's plugin.json for its version. find (not bare globs) never errors under zsh.
MEASURE_PY=""; TO_LAUNCHER=""; _best_ver=""
while IFS= read -r _cand; do
  [ -f "$_cand" ] || continue
  _root="$(cd -P -- "$(dirname -- "$_cand")/../../.." 2>/dev/null && pwd)"
  _ver="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_root/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
  [ -n "$_ver" ] || _ver="0.0.0"
  if [ -z "$_best_ver" ] || [ "$(printf '%s\n%s\n' "$_ver" "$_best_ver" | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -n1)" = "$_ver" ]; then
    _best_ver="$_ver"; MEASURE_PY="$_cand"; TO_LAUNCHER="$_root/hooks/python-launcher.sh"
  fi
done <<EOF
$(find -L "$HOME/.claude/skills" "$HOME/.claude/plugins/cache" "$HOME/.claude/token-optimizer" "$HOME/.codex/skills" "$HOME/.codex/plugins/cache" "$HOME/.config/opencode/plugins/cache" "$HOME/.config/opencode/plugins" -type f -name measure.py -path '*token-optimizer*/scripts/measure.py' 2>/dev/null)
EOF
if [ -z "$MEASURE_PY" ]; then echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; fi
# python-launcher.sh sits at the plugin root beside skills/. Routing EVERY
# runtime through it keeps Windows invocations flash-free: bare
# `python3` is a console-subsystem spawn on a Codex/OpenCode Windows host,
# while the launcher swaps to the GUI-subsystem pythonw.exe. On POSIX the
# launcher resolves the same python3 it always did.
[ -f "$TO_LAUNCHER" ] || TO_LAUNCHER=""
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
```

2. Run (use the resolved `$RUNTIME` — never hardcode a runtime; under OpenCode this
   keeps the session scan scoped to OpenCode and never reaches into `~/.claude`):
   - Claude Code plugin: `bash "$CLAUDE_PLUGIN_ROOT/hooks/python-launcher.sh" $MEASURE_PY health`
   - Codex / OpenCode / standalone: `TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" bash "$TO_LAUNCHER" "$MEASURE_PY" health`
     (only if `$TO_LAUNCHER` is empty, fall back to `TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" python3 "$MEASURE_PY" health` — the bare-python3 form flashes a console window on Windows)

3. Present results clearly. For each session show: PID, elapsed time, version, and flags (STALE >24h, ZOMBIE >48h, OUTDATED, HEADLESS, TERMINAL, DESKTOP, UNVERIFIED, ORPHAN). For an ORPHAN session also show "terminal gone; started <age> ago".

4. If ANY sessions are flagged STALE or ZOMBIE (and are not DESKTOP, UNVERIFIED or ORPHAN), ask the user:
   "I found N session(s) that look stale. Want me to show details so you can decide which to terminate?"

5. **Terminate only with the user's explicit confirmation**, because a session that looks stale may be doing real work:
   - Never auto-kill anything. Ask first and wait for confirmation.
   - HEADLESS sessions might be intentional background processes (cron agents, heartbeat monitors, scheduled tasks). Always warn: "This session is headless, it might be a background agent running on purpose. Are you sure you want to terminate it?"
   - DESKTOP sessions are hosted by another program (the Claude desktop app, an IDE, the SDK, a headless `-p` run or a subcommand such as `mcp`) and UNVERIFIED sessions are processes whose identity could not be read. `kill-stale` never terminates either, because process age alone does not show that a conversation is abandoned. On every platform, a process whose parent chain cannot be fully read, or that has lost its terminal or shell without meeting every ORPHAN condition below, is also UNVERIFIED and is never killed automatically. Only a session attached to a terminal under a shell, tmux/screen or sshd is offered for termination.
   - ORPHAN (macOS and Linux only) is a fourth, explicit class: an interactive session whose terminal died. It needs exact argv, no headless flag or subcommand, no controlling TTY, init (pid 1 or `systemd --user`) as parent, and a binary that is not an Electron app; anything short of that stays UNVERIFIED. A launchd/systemd automation that carries `-p`/`--print` is DESKTOP, not ORPHAN. `kill-stale` never terminates orphans by default: it prints how many there are and the exact command. Only if the user asks to clean them up, preview with `kill-stale --include-orphans --dry-run` (same `--hours` threshold), show the list, get explicit confirmation, then run `kill-stale --include-orphans`. Each orphan is re-verified right before its kill (same pid, same start time, still orphan). Windows has no ORPHAN class. Do not suggest killing them by PID; point the user to the app itself or the operating system's process manager.
   - Let the user pick specific PIDs to terminate, or offer "terminate all ZOMBIE-flagged sessions" as a batch option.
   - Always run a dry-run first to preview what would be terminated, then ask for confirmation before running without `--dry-run`.
   - Claude Code plugin dry-run: `bash "$CLAUDE_PLUGIN_ROOT/hooks/python-launcher.sh" $MEASURE_PY kill-stale --dry-run`
   - Codex / OpenCode / standalone dry-run: `TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" bash "$TO_LAUNCHER" "$MEASURE_PY" kill-stale --dry-run`
     (only if `$TO_LAUNCHER` is empty, fall back to `TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" python3 "$MEASURE_PY" kill-stale --dry-run`)
   - If the user says "kill all" or similar, still show the dry-run preview and confirm. No silent kills.

6. If no stale or zombie sessions found, say: "All sessions look healthy. Your oldest is Xh old."
