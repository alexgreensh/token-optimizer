---
description: Preview safe savings changes and apply them after approval.
---

# Apply safe savings

Run this Bash block to preview the plan. Keep the command prefix from stderr and the exact `token` from the JSON for the approval step.

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
  # Explicit config-dir env of another harness: same tier as Python's
  # detect_runtime() (above CLAUDECODE, which every Claude Code child inherits).
  elif [ -n "$HERMES_HOME" ]; then
    RUNTIME="hermes"
  elif [ -n "$COPILOT_HOME" ] || [ -n "$TOKEN_OPTIMIZER_COPILOT_HOME" ]; then
    RUNTIME="copilot"
  elif [ -n "$TOKEN_OPTIMIZER_CURSOR_HOME" ]; then
    RUNTIME="cursor"
  elif [ -n "$TOKEN_OPTIMIZER_ANTIGRAVITY_HOME" ]; then
    RUNTIME="antigravity"
  elif [ -n "$GROK_HOME" ] || [ -n "$TOKEN_OPTIMIZER_GROK_HOME" ]; then
    RUNTIME="grok"
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
if [ -n "$TO_LAUNCHER" ]; then
  TO_CMD=(bash "$TO_LAUNCHER" "$MEASURE_PY")
else
  TO_CMD=(python3 "$MEASURE_PY")
fi
printf 'Command prefix: ' >&2
printf '%q ' env "TOKEN_OPTIMIZER_RUNTIME=$RUNTIME" "${TO_CMD[@]}" >&2
printf '\n' >&2
"${TO_CMD[@]}" apply-safe --json
```

If the preview fails, show its error and stop. Use the items where `default_selected` is true or absent. If there are no selected changes, say so and stop. Otherwise show the selected changes, their estimated monthly savings (mark unpriced items), and any plan advice. Ask in chat once: **Apply these changes? Reply yes to approve this plan.** End the turn and wait for the user's reply.

Run the saved command prefix followed by `apply-safe --yes --token '<exact token from the displayed plan>'` only after the user replies yes to that plan. Stop on any other answer. If the command reports the plan changed, obtain a new preview and approval before applying it.

Report the receipt's item results, including any failures. For any receipt ID returned, give the undo command using the same prefix followed by `apply-safe --undo '<receipt_id>'`, with the actual receipt ID. Leave undo for the user to request.
