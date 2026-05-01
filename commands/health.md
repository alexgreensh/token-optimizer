---
description: Check running Claude Code or Codex sessions, find zombies, offer to clean up safely
---

# Session Health Check

Run a session health check and help the user manage running sessions safely.

## Steps

1. Resolve measure.py path:
```bash
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  if [ -n "$CLAUDE_PLUGIN_ROOT" ] || [ -n "$CLAUDE_PLUGIN_DATA" ]; then
    RUNTIME="claude"
  elif [ -n "$CODEX_HOME" ] || [ -d "$HOME/.codex" ]; then
    RUNTIME="codex"
  else
    RUNTIME="claude"
  fi
fi
MEASURE_PY=""
for f in "$HOME/.codex/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$HOME/.codex/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$HOME/.claude/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/token-optimizer/skills/token-optimizer/scripts/measure.py"; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
```

2. Run:
   - Claude Code plugin: `bash "$CLAUDE_PLUGIN_ROOT/hooks/python-launcher.sh" $MEASURE_PY health`
   - Codex or standalone: `TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" health`

3. Present results clearly. For each session show: PID, elapsed time, version, and flags (STALE >24h, ZOMBIE >48h, OUTDATED, HEADLESS, TERMINAL).

4. If ANY sessions are flagged STALE or ZOMBIE, ask the user:
   "I found N session(s) that look stale. Want me to show details so you can decide which to terminate?"

5. **CRITICAL SAFETY RULES — follow these exactly:**
   - NEVER auto-kill anything. Always ask first and get explicit confirmation.
   - HEADLESS sessions might be intentional background processes (cron agents, heartbeat monitors, scheduled tasks). Always warn: "This session is headless, it might be a background agent running on purpose. Are you sure you want to terminate it?"
   - Let the user pick specific PIDs to terminate, or offer "terminate all ZOMBIE-flagged sessions" as a batch option.
   - Always run a dry-run first to preview what would be terminated, then ask for confirmation before running without `--dry-run`.
   - Claude Code plugin dry-run: `bash "$CLAUDE_PLUGIN_ROOT/hooks/python-launcher.sh" $MEASURE_PY kill-stale --dry-run`
   - Codex or standalone dry-run: `TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" kill-stale --dry-run`
   - If the user says "kill all" or similar, still show the dry-run preview and confirm. No silent kills.

6. If no stale or zombie sessions found, say: "All sessions look healthy. Your oldest is Xh old."
