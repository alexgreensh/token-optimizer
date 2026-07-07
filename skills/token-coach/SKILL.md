---
name: token-coach
description: |
  Context window coach analyzing setup overhead, historical usage trends, and session habits.
  Use when building something new and wanting token efficiency from the start, existing sessions feel sluggish or context fills too fast, designing multi-agent systems, or wanting a quick health check with real numbers.
  Do NOT use for running the full audit and applying fixes (use /token-optimizer instead).
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

MEASURE_PY=""
for f in "$HOME/.config/opencode/plugins/cache"/*/token-optimizer/*/skills/token-optimizer/scripts/measure.py \
         "$HOME/.codex/skills/token-optimizer/scripts/measure.py" \
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
Parse the JSON output. This gives you: snapshot (current measurements), detected patterns, coaching questions, focus suggestions, and **history** (trend data from past sessions).

The `history` key contains (when trends.db has enough data):
- `quality_recent_avg` / `quality_prior_avg` - 7-day vs older quality scores
- `duration_recent_avg` / `duration_prior_avg` - session length trends (minutes)
- `cache_hit_recent_avg` / `cache_hit_prior_avg` - prompt cache hit rate trends
- `grade_d_pct_recent` / `grade_distribution` - recent grade breakdown
- `total_cost_usd` / `cost_per_session_usd` / `sessions_in_period` - spend summary
- `quality_short_sessions` / `quality_long_sessions` / `optimal_session_hint` - duration-quality correlation
- `compression_measured_saved` / `compression_opportunity_tokens` - compression gap
- `multi_model_session_pct` - percentage of recent sessions that switched models mid-session

Historical patterns also appear in the `patterns_bad` array (e.g. "Quality Declining", "Session Duration Creep", "Cache Hit Rate Dropping", "Cache Hit Rate Dropping (Model Switches)", "Frequent Model Switching", "High Cost Per Session", "Compression Opportunity Gap").

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

5. **Keep-Warm consent (first run only, Claude Code)**:
```bash
python3 "$MEASURE_PY" keepwarm-consent-status   # JSON: {billing_mode, consent, should_ask}
```
If `should_ask` is `false`, skip silently. If `true` (API-billed, not yet asked), offer Keep-Warm once after the coaching conversation. First compute the projection from the user's own history:
```bash
python3 "$MEASURE_PY" keepwarm-backfill --json --no-fence   # read modes."probe-only".net_usd
```
Then pitch: when a session pauses past its 1h cache window and resumes, the prefix is re-written at up to 2x; Keep-Warm pings before expiry (~0.1x, max 2 pings/pause) so resumes stay warm, with a tripwire that auto-disables if it stops paying off. If `modes."probe-only".net_usd` is positive, say "a history-replay projection from your own last 30 days nets ~$<net_usd>/30d at probe-only"; if backfill yields nothing or `net_usd <= 0`, drop the dollar sentence (do not invent one) and say savings depend on their own pattern and the dashboard shows it once pings fire.

Record the answer — **yes/no FIRST** so an interrupted run never strands an "asked" marker with no answer: `keepwarm-enable` (yes) or `keepwarm-disable` (no), both terminal. Only if the user defers/ignores (records neither) run `keepwarm-consent-asked` as the shown-marker. `keepwarm-enable` records consent and installs the scheduler (macOS); other OSes are scheduler-pending, watchdog-only. Confirm it is armed with `keepwarm-scheduler status` and `keepwarm-tick --dry-run`. It is off by default and refuses on subscription auth.

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
3. If `history` data is available, weave in trend insights naturally:
   - Quality trending down? Lead with that, it's more urgent than a static snapshot
   - Cost data? Ground advice in dollars ("At $X.XX/session across Y sessions, routing alone could save $Z/month")
   - Duration-quality correlation? "Your short sessions score X vs Y for long ones" is a concrete, actionable insight
   - Grade distribution? "N% of your sessions scored D" hits harder than an abstract score
   - Model switching? If multi_model_session_pct is high, explain: switching models mid-session invalidates the prompt cache. Set model at session start, not mid-conversation. Subagent routing to cheaper models is fine (separate context)
   - Don't dump all history data at once. Pick the 1-2 most relevant trends for their intake choice
4. Reference their actual numbers ("You have 47 skills costing ~4,700 tokens at startup")
5. Ask a follow-up question. Don't dump everything at once.
6. For agentic systems (option c): walk through their architecture step by step
7. Use the coaching scripts for structure, but keep it natural

For Codex specifically, translate all advice to native Codex concepts:
- `AGENTS.md` instead of `CLAUDE.md`
- Codex memories instead of `MEMORY.md`
- balanced Codex hooks instead of Claude hooks
- Intelligence levels (Low/Medium/High/Extra High) and model selection (GPT-5.5, GPT-5.4, GPT-5.4-Mini, GPT-5.3-Codex, GPT-5.2) instead of Opus/Sonnet/Haiku routing
- Reasoning effort settings instead of model-per-agent routing
- compact prompt guidance instead of PreCompact/PostCompact lifecycle hooks
- Never reference Claude-specific concepts (Opus, Sonnet, Haiku, CLAUDE.md) when coaching a Codex user

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
