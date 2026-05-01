---
name: token-coach
description: Context window coach. Proactive guidance for token-efficient Claude Code or Codex projects, multi-agent systems, and skill architecture.
---

# Token Coach: Plan Token-Efficient Before You Build

Interactive coaching for Claude Code or Codex architecture decisions. Analyzes your setup, identifies patterns (good and bad), and gives personalized advice with real numbers.

**Use when**: Building something new, existing setup feels slow, designing multi-agent systems, or want a quick health check.

---

## Phase 0: Initialize

1. **Resolve runtime and measure.py path** (same as token-optimizer):
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
for f in "$HOME/.codex/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.codex/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$HOME/.claude/skills/token-optimizer/scripts/measure.py" \
         "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$PWD/skills/token-optimizer/scripts/measure.py"; do
  [ -f "$f" ] && MEASURE_PY="$f" && break
done
[ -z "$MEASURE_PY" ] || [ ! -f "$MEASURE_PY" ] && { echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; }
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
```

2. **Collect coaching data**:
```bash
python3 "$MEASURE_PY" coach --json
```
Parse the JSON output. This gives you: snapshot (current measurements), detected patterns, coaching questions, and focus suggestions.

3. **Check context quality** (v2.0):
```bash
python3 "$MEASURE_PY" quality current --json 2>/dev/null
```
If available, parse the quality score and issues. This enriches coaching with session-level insights (not just setup overhead). If the command fails (pre-v2.0 install), skip gracefully.

4. **For Codex, check setup readiness**:
```bash
if [ "$RUNTIME" = "codex" ]; then
  python3 "$MEASURE_PY" codex-doctor --project "$PWD" --json 2>/dev/null
fi
```
Use this to tell the user whether balanced hooks, compact prompt guidance, dashboard refresh, and status-line support are installed.

## Phase 1: Intake

Ask ONE question:

> What's your goal today?
> a) Building something new, want it token-efficient from the start
> b) Existing project feels sluggish / context fills too fast
> c) Designing a multi-agent system, want architecture advice
> d) Quick health check with actionable tips

Wait for the answer. Don't dump info before they choose.

## Phase 2: Load Context (based on intake)

Resolve the token-coach skill directory:
```bash
COACH_DIR=""
if [ -d "$HOME/.codex/skills/token-coach" ]; then
  COACH_DIR="$HOME/.codex/skills/token-coach"
elif [ -d "$HOME/.codex/skills/token-optimizer/../token-coach" ]; then
  COACH_DIR="$HOME/.codex/skills/token-optimizer/../token-coach"
elif [ -d "$HOME/.claude/skills/token-coach" ]; then
  COACH_DIR="$HOME/.claude/skills/token-coach"
elif [ -d "$HOME/.claude/skills/token-optimizer/../token-coach" ]; then
  COACH_DIR="$HOME/.claude/skills/token-optimizer/../token-coach"
else
  COACH_DIR="$(find "$HOME/.codex/plugins/cache" "$HOME/.claude/plugins/cache" -path "*/token-coach" -type d 2>/dev/null | head -1)"
fi
```

Load references based on intake choice:
- **Option a or b**: Read `$COACH_DIR/references/coach-patterns.md` + `$COACH_DIR/references/quick-reference.md`
- **Option c**: Read `$COACH_DIR/references/agentic-systems.md` + `$COACH_DIR/references/quick-reference.md`
- **Option d**: Read `$COACH_DIR/references/quick-reference.md` only (fast path)

Read the matching example from `$COACH_DIR/examples/` as a few-shot template:
- Option a: `coaching-session-new-project.md`
- Option b: `coaching-session-heavy-setup.md`
- Option c: `coaching-session-agentic.md`
- Option d: Skip example (keep it fast)

Read `$COACH_DIR/references/coaching-scripts.md` for conversation structure.

## Phase 3: Coach (conversation, not report)

This is a CONVERSATION. Not a wall of text.

1. Lead with the 1-2 most impactful findings from the coaching data
2. If quality data is available and score < 70, lead with that instead: "Your current session quality is [X]/100. [Top issue] is eating [Y tokens]."
3. Reference their actual numbers ("You have 47 skills costing ~4,700 tokens at startup")
4. Ask a follow-up question. Don't dump everything at once.
5. For agentic systems (option c): walk through their architecture step by step
6. Use the coaching scripts for structure, but keep it natural

For Codex specifically, translate the advice:
- `AGENTS.md` instead of `CLAUDE.md`
- Codex memories instead of `MEMORY.md`
- balanced Codex hooks instead of Claude `SessionEnd`/smart-compaction hooks
- reasoning effort and task boundaries instead of Haiku/Sonnet/Opus routing
- compact prompt guidance instead of Claude `PreCompact`/`PostCompact` parity

**Tone**: Knowledgeable friend, not corporate consultant. Be direct about what matters and why. Use real numbers from their data.

**Anti-patterns to call out**: Reference the anti-patterns from coach-patterns.md. Name them ("You've got the 50-Skill Trap going on").

Continue the conversation for 2-4 exchanges. Let the user ask questions. Adjust advice based on what they tell you about their workflow.

## Phase 4: Action Plan

After the conversation, generate a prioritized action plan:

1. Summarize 3-5 concrete actions, ordered by impact
2. Include estimated token savings for each action (use the numbers from quick-reference.md)
3. If quality score < 70 in Claude Code: include "Set up Smart Compaction" as a recommended action (`python3 $MEASURE_PY setup-smart-compact`)
4. If quality score < 70 in Codex: include "Install balanced Codex hooks and compact prompt guidance" (`TOKEN_OPTIMIZER_RUNTIME=codex python3 $MEASURE_PY codex-install --project .`)
5. If quality score < 50: recommend immediate `/compact` or `/clear` before continuing
6. Flag which actions are quick wins vs deeper changes
7. Offer to run `/token-optimizer` for the full audit + implementation if they want to go beyond coaching

**Format**: Keep it scannable. Numbered list with bold action names, one-line description, estimated savings.

## Phase 5: Dashboard (optional)

If measure.py generated a coach dashboard tab, mention it:
"Your Token Health Score and pattern analysis are in the dashboard. Run `python3 $MEASURE_PY dashboard` to see it."

For Codex, also give the generated file location: `~/.codex/_backups/token-optimizer/dashboard.html`.
