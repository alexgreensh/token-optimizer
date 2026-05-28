---
name: token-dashboard
description: Open the Token Optimizer dashboard. Collects latest session data, regenerates the dashboard, and opens it in your browser.
---

# Token Optimizer Dashboard

Opens an up-to-date dashboard showing your context usage trends, quality scores, session history, and skill management.

## Instructions

1. **Resolve runtime and measure.py path** (shared resolver):
```bash
_r="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/token-optimizer}/hooks/resolve.sh"
[ -f "$_r" ] || _r=$(ls "$HOME/.codex/plugins/cache"/*/token-optimizer/*/hooks/resolve.sh "$HOME/.claude/plugins/cache"/*/token-optimizer/*/hooks/resolve.sh "$PWD/hooks/resolve.sh" 2>/dev/null | head -1)
[ -f "$_r" ] || { echo "[Error] Token Optimizer resolver not found. Is Token Optimizer installed?" >&2; exit 1; }
_o=$(bash "$_r" --export --need measure) || exit 1
eval "$_o"
```

2. **Collect and open**:
```bash
python3 "$MEASURE_PY" collect --quiet && python3 "$MEASURE_PY" dashboard
```

This collects the latest session data into the trends database, regenerates the dashboard HTML, and opens it in your default browser.

3. **Tell the user** the dashboard is open. URL-first ordering (v5.3.3+):
   - Probe daemon: `python3 "$MEASURE_PY" daemon-status 2>/dev/null`
   - If DAEMON_RUNNING: lead with `URL: http://localhost:24842/token-optimizer` (bookmarkable, auto-updates), then mention the file fallback.
   - For Claude Code file fallback: `File: ~/.claude/_backups/token-optimizer/dashboard.html`.
   - For Codex file fallback: `File: ~/.codex/_backups/token-optimizer/dashboard.html`.
   - If DAEMON_NOT_RUNNING in Claude Code: suggest `python3 $MEASURE_PY setup-daemon` (macOS and Windows).
   - If DAEMON_NOT_RUNNING in Codex: do not imply the Claude daemon is required; the generated file works, and Stop hooks refresh it when balanced hooks are installed.
