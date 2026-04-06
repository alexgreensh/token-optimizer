---
description: Check running Claude Code sessions, find zombies, offer to clean up safely
---

# Session Health Check

Run a session health check and help the user manage running sessions safely.

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

2. Run: `python3 $MEASURE_PY health`

3. Present results clearly. For each session show: PID, elapsed time, version, and flags (STALE >24h, ZOMBIE >48h, OUTDATED, HEADLESS, TERMINAL).

4. If ANY sessions are flagged STALE or ZOMBIE, ask the user:
   "I found N session(s) that look stale. Want me to show details so you can decide which to terminate?"

5. **CRITICAL SAFETY RULES — follow these exactly:**
   - NEVER auto-kill anything. Always ask first and get explicit confirmation.
   - HEADLESS sessions might be intentional background processes (cron agents, heartbeat monitors, scheduled tasks). Always warn: "This session is headless, it might be a background agent running on purpose. Are you sure you want to terminate it?"
   - Let the user pick specific PIDs to terminate, or offer "terminate all ZOMBIE-flagged sessions" as a batch option.
   - Always run `python3 $MEASURE_PY kill-stale --dry-run` first to preview what would be terminated, then ask for confirmation before running without `--dry-run`.
   - If the user says "kill all" or similar, still show the dry-run preview and confirm. No silent kills.

6. If no stale or zombie sessions found, say: "All sessions look healthy. Your oldest is Xh old."
