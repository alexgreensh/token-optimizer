---
description: Quick 10-second context health check with quality score and top issues
---

# Quick Context Check

Fast health check. Show the user where they stand in under 10 lines.

## Steps

1. Resolve measure.py path (shared resolver):
```bash
_r="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/token-optimizer}/hooks/resolve.sh"
[ -f "$_r" ] || _r=$(ls "$HOME/.codex/plugins/cache"/*/token-optimizer/*/hooks/resolve.sh "$HOME/.claude/plugins/cache"/*/token-optimizer/*/hooks/resolve.sh "$PWD/hooks/resolve.sh" 2>/dev/null | head -1)
[ -f "$_r" ] || { echo "[Error] Token Optimizer resolver not found. Is Token Optimizer installed?" >&2; exit 1; }
_o=$(bash "$_r" --export --need measure) || exit 1
eval "$_o"
```

2. Run:
   - Claude Code plugin: `bash "$CLAUDE_PLUGIN_ROOT/hooks/python-launcher.sh" $MEASURE_PY quick --json`
   - Codex or standalone: `TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" quick --json`

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
