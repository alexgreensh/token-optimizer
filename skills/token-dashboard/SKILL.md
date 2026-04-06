---
name: token-dashboard
description: Open the Token Optimizer dashboard. Collects latest session data, regenerates the dashboard, and opens it in your browser.
---

# Token Optimizer Dashboard

Opens an up-to-date dashboard showing your context usage trends, quality scores, session history, and skill management.

## Instructions

1. **Resolve measure.py path**:
```bash
MEASURE_PY=""
for f in "$HOME/.claude/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
[ -z "$MEASURE_PY" ] && { echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; }
```

2. **Collect and open**:
```bash
python3 "$MEASURE_PY" collect --quiet && python3 "$MEASURE_PY" dashboard
```

This collects the latest session data into the trends database, regenerates the dashboard HTML, and opens it in your default browser.

3. **Tell the user** the dashboard is open and mention direct access:
   - File: `~/.claude/_backups/token-optimizer/dashboard.html`
   - Check if the persistent daemon is running: `nc -z 127.0.0.1 24842 2>/dev/null`
   - If running, also mention: "Bookmarkable URL: http://localhost:24842/"
   - If NOT running, do NOT mention the URL (it would give a connection error). Instead suggest: "Want a bookmarkable URL? Run: `python3 $MEASURE_PY setup-daemon`"
