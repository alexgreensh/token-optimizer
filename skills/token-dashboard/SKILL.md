---
name: token-dashboard
description: Open the Token Optimizer dashboard. Collects latest session data, regenerates the dashboard, and opens it in your browser.
---

# Token Optimizer Dashboard

Opens an up-to-date dashboard showing your context usage trends, quality scores, session history, and skill management.

## Instructions

1. **Resolve runtime and measure.py path**:
```bash
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  if [ -n "$CLAUDE_PLUGIN_ROOT" ] || [ -n "$CLAUDE_PLUGIN_DATA" ]; then
    RUNTIME="claude"
  elif [ -n "$OPENCODE" ] || [ -n "$OPENCODE_BIN" ] || [ -n "$OPENCODE_CONFIG_DIR" ] || [ -n "$OPENCODE_CONFIG" ]; then
    RUNTIME="opencode"
  elif [ -n "$CODEX_HOME" ]; then
    RUNTIME="codex"
  elif [ -d "$HOME/.config/opencode" ] && [ ! -d "$HOME/.codex" ]; then
    RUNTIME="opencode"
  elif [ -d "$HOME/.codex" ]; then
    RUNTIME="codex"
  else
    RUNTIME="claude"
  fi
fi

# Resolve measure.py to the NEWEST installed copy across channels so a stale
# plugin-cache copy never shadows a fresh install (issue #57). find -L follows the
# install.sh symlink under ~/.claude/skills; cd -P resolves it before reading each
# copy's plugin.json for its version. find (not bare globs) never errors under zsh.
MEASURE_PY=""; _best_ver=""
while IFS= read -r _cand; do
  [ -f "$_cand" ] || continue
  _root="$(cd -P -- "$(dirname -- "$_cand")/../../.." 2>/dev/null && pwd)"
  _ver="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_root/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
  [ -n "$_ver" ] || _ver="0.0.0"
  if [ -z "$_best_ver" ] || [ "$(printf '%s\n%s\n' "$_ver" "$_best_ver" | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -n1)" = "$_ver" ]; then
    _best_ver="$_ver"; MEASURE_PY="$_cand"
  fi
done <<EOF
$(find -L "$HOME/.claude/skills" "$HOME/.claude/plugins/cache" "$HOME/.claude/token-optimizer" "$HOME/.codex/skills" "$HOME/.codex/plugins/cache" "$HOME/.config/opencode/plugins/cache" "$HOME/.config/opencode/plugins" -type f -name measure.py -path '*token-optimizer*/scripts/measure.py' 2>/dev/null)
EOF
if [ -z "$MEASURE_PY" ]; then echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; fi
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
```

2. **Collect and open**:
```bash
python3 "$MEASURE_PY" collect --quiet && python3 "$MEASURE_PY" dashboard
```

This collects the latest session data into the trends database, regenerates the dashboard HTML, and opens it in your default browser.

3. **Tell the user** the dashboard is open. Probe the daemon BEFORE mentioning any URL (v5.3.3+):
   - Probe daemon: `python3 "$MEASURE_PY" daemon-status 2>/dev/null`
   - If DAEMON_RUNNING: lead with `URL: http://localhost:24842/token-optimizer` (bookmarkable, auto-updates), then mention the file fallback.
   - If DAEMON_NOT_RUNNING: do NOT print the `localhost:24842` URL. Tell the user the dashboard opened as a file, and suggest `python3 $MEASURE_PY setup-daemon` (macOS and Windows) if they want a bookmarkable URL.
   - File path: never hardcode it. It is install-dependent. Cite the `  Dashboard: ` line from the step-2 output.
   - For Codex when DAEMON_NOT_RUNNING: do not imply the Claude daemon is required; the generated file works, and Stop hooks refresh it when balanced hooks are installed.
