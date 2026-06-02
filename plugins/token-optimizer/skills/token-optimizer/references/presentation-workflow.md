# Phase 3: Presentation Workflow

## Findings Template

Read the optimization plan and present. For MODEL ROUTING, also read `{COORD_PATH}/audit/advanced.md` for "Has routing instructions" and "Usage Pattern" data.

```
[Token Optimizer Results]

CURRENT STATE
Your per-message overhead: ~X tokens
Context used before first message: ~X%

QUICK WINS (do these today)
- [Action 1]: Save ~X tokens/msg (~Y%)
- [Action 2]: Save ~X tokens/msg (~Y%)

MODEL ROUTING
[Has instructions: Yes/No] | [Token distribution: X% Opus, Y% Sonnet, Z% Haiku or "Not measured yet"]

FULL OPTIMIZATION POTENTIAL
If all implemented: ~X tokens/msg saved (~Y% reduction)

Ready to implement? I can:
1. Auto-fix safe changes (consolidate CLAUDE.md, archive skills)
2. Generate permissions.deny rules (if missing)
3. Create optimized CLAUDE.md template
4. Show MCP servers to consider disabling

Some optimizations have side effects:
- Deny rules block file access for ALL tools (may break MCP servers that read databases)
- Archiving skills breaks anything that @imports them
- Disabling MCP servers breaks skills that use their tools
I'll check for dependencies and warn you before each change.

What should we tackle first?
```

## Dashboard Generation

```bash
python3 $MEASURE_PY dashboard --coord-path $COORD_PATH
```

Tell the user: "Dashboard opened in your browser. Browse findings by category, check the optimizations you want, click Copy Prompt and paste back here. Or just tell me directly what to tackle."

## Dashboard URL Presentation

Check daemon status first:
```bash
python3 "$MEASURE_PY" daemon-status 2>/dev/null || echo "DAEMON_NOT_RUNNING"
```

If DAEMON_RUNNING:
```
Your persistent dashboard (auto-updated every session):
  URL:    http://localhost:24842/token-optimizer
  File:   ~/.claude/_backups/token-optimizer/dashboard.html
```

If DAEMON_NOT_RUNNING:
```
Your persistent dashboard (auto-updated every session):
  File:   ~/.claude/_backups/token-optimizer/dashboard.html
```
On macOS only, suggest: "Want a bookmarkable URL? Run: `python3 $MEASURE_PY setup-daemon`"
Do NOT mention `localhost:24842` if daemon is not running.

For headless/remote: user can run `dashboard --serve` separately. Never use `--serve` from within the orchestrator (it blocks with `serve_forever`).

**Wait for user decision before proceeding.**
