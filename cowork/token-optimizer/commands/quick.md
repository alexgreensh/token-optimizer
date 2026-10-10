---
description: Runs a 10-second context health check with a quality score and the top issues. Use for a fast pulse on the current session.
---

# Quick Context Check

Fast health check. Show the user where they stand in under 10 lines.

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
   keeps measurement scoped to the OpenCode session and never writes to `~/.claude`):
   - Claude Code plugin: `bash "$CLAUDE_PLUGIN_ROOT/hooks/python-launcher.sh" $MEASURE_PY quick --json`
   - Codex / OpenCode / standalone: `TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" bash "$TO_LAUNCHER" "$MEASURE_PY" quick --json`
     (only if `$TO_LAUNCHER` is empty, fall back to `TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" python3 "$MEASURE_PY" quick --json` — the bare-python3 form flashes a console window on Windows)

3. Parse the JSON output and present concisely:
   - Context overhead: X tokens (Y% of context window)
   - Quality score: N/100 (letter grade)
   - Top 3 offenders with estimated savings (if any)
   - Degradation risk (from the MRCR curve data)

4. Keep the response under 10 lines. This is a quick pulse check, not a full audit.

5. Based on quality score, suggest next action:
   - Score 85+: "Context is clean. No action needed."
   - Score 70-84: "Context is good but has some bloat. Consider `/compact` if you've been going a while."
   - Score 50-69: "Context quality is degraded. Run `/compact` to reclaim quality, or `/token-optimizer` for a full audit."
   - Score below 50: "Context quality is critical. Consider `/clear` with checkpoint, or run `/token-optimizer` for a full audit."
