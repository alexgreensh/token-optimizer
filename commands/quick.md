---
description: Quick 10-second context health check with quality score and top issues
---

# Quick Context Check

Fast health check. Show the user where they stand in under 10 lines.

## Steps

1. Resolve measure.py path:
```bash
MEASURE_PY=""
for f in "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$HOME/.claude/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/token-optimizer/skills/token-optimizer/scripts/measure.py"; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
```

2. Run: `python3 $MEASURE_PY quick --json`

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
