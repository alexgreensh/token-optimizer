---
name: token-coach
description: Coaches a token-efficient Claude Code or Codex setup from the user's own usage data. Use when planning a build, fixing a slow or costly setup, or deciding which workflow steps need a model.
---

# Token Coach

A coaching conversation about where the user's tokens go and what to change, grounded in numbers measured on their machine. The deliverable is a short, prioritised action plan the user agrees with. For a full audit that edits their setup, hand over to `token-optimizer`.

Token Optimizer's own skills (`token-optimizer`, `token-coach`, `token-dashboard`, `fleet-auditor`, `resume-checkpoint`) are the measurement layer, so leave them out of every unused-skill, archive or consolidation suggestion, however rarely they run.

## 1. Collect the data

Run exactly this first. It finds the newest installed copy, so a stale plugin cache never answers:

```bash
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  if [ -n "$CLAUDE_PLUGIN_ROOT" ] || [ -n "$CLAUDE_PLUGIN_DATA" ]; then
    RUNTIME="claude"
  elif [ -n "$OPENCODE" ] || [ -n "$OPENCODE_BIN" ] || [ -n "$OPENCODE_CONFIG_DIR" ] || [ -n "$OPENCODE_CONFIG" ]; then
    RUNTIME="opencode"
  elif [ -n "$CODEX_HOME" ]; then
    RUNTIME="codex"
  elif [ -n "$CLAUDECODE" ] || [ -n "$CLAUDE_CODE_ENTRYPOINT" ] || [ -n "$CLAUDE_CODE_SESSION_ID" ]; then
    RUNTIME="claude"
  elif [ -d "$HOME/.config/opencode" ] && [ ! -d "$HOME/.codex" ]; then
    RUNTIME="opencode"
  elif [ -d "$HOME/.codex" ]; then
    RUNTIME="codex"
  else
    RUNTIME="claude"
  fi
fi

# Resolve measure.py to the NEWEST installed copy across channels so a stale
# plugin-cache copy never shadows a fresh install. find -L follows the
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
if [ -z "$MEASURE_PY" ] || [ ! -f "$MEASURE_PY" ]; then echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; fi
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
# The coach files ship beside measure.py, so they are always the same version.
COACH_DIR="$(cd -P -- "$(dirname -- "$MEASURE_PY")/../../token-coach" 2>/dev/null && pwd)"
[ -d "$COACH_DIR/references" ] || echo "[Error] token-coach references not found beside $MEASURE_PY. Reinstall Token Optimizer."
```

Then gather, skipping any command that fails (older installs lack some):

```bash
python3 "$MEASURE_PY" coach --json                      # snapshot, patterns, history, questions
python3 "$MEASURE_PY" quality current --json            # this session's quality score and issues
python3 "$MEASURE_PY" subagent-cache status --json      # Claude Code: subagent cache lifetime and its payoff
python3 "$MEASURE_PY" compact-advice --json             # Claude Code: would an earlier compact window pay off
python3 "$MEASURE_PY" recommendations --json            # Claude Code: the daily usage-recommendations record
[ "$RUNTIME" = "codex" ] && python3 "$MEASURE_PY" codex-doctor --project "$PWD" --json
```

What the coach JSON holds: `snapshot` (current overhead), `patterns_good` and `patterns_bad` (named patterns with detail and fix), `history` (7-day against earlier: quality, session length, cache hit rate, grade spread, cost per session, short against long sessions, compression gap, share of sessions that switched model), `deterministic_candidates` when present (work the user repeats that a script could do), and `usage_recommendations` on Claude Code (the daily-measured record: one item each for the compact window and the subagent cache lifetime, plus `wins` once the user acted on an earlier recommendation and the numbers moved). Field-by-field meaning and the numbers to quote: [references/quick-reference.md](references/quick-reference.md).

Every number you quote comes from these outputs or from the quick reference. When the data is thin or a command returned nothing, say so and coach from what the user tells you; an invented figure costs the user's trust in every real one.

## 2. Ask what they want

Ask one question and wait for the answer before showing findings:

> What's your goal today?
> a) Building something new and want it token-efficient from the start
> b) An existing setup feels slow or context fills too fast
> c) Designing a multi-agent system
> d) Quick health check
> e) Review a workflow or automation: which steps need a model at all

Skip the question when the request already answers it.

## 3. Load what that goal needs

| Goal | Read | Example to follow |
|------|------|-------------------|
| a, b | [references/coach-patterns.md](references/coach-patterns.md), quick reference | `examples/coaching-session-new-project.md` (a), `examples/coaching-session-heavy-setup.md` (b) |
| c | [references/agentic-systems.md](references/agentic-systems.md), quick reference | `examples/coaching-session-agentic.md` |
| d | quick reference only | none, keep it fast |
| e | [references/deterministic-stages.md](references/deterministic-stages.md) | none |

For a, b and c also read [references/coaching-scripts.md](references/coaching-scripts.md) for how each conversation tends to run. All paths are under `$COACH_DIR`. The examples show tone and pacing; the user's data decides the content.

## 4. Coach

This is a conversation. One or two findings, then a question; the user steers the rest.

- **Open with what hurts most.** A session quality score under 70 comes first ("quality is 58 of 100, and stale tool results are holding 40K tokens"). Otherwise a worsening trend beats a static number: falling quality, growing session length, a dropping cache hit rate.
- **Use their numbers and name the pattern.** "47 skills cost about 4,700 tokens at every start, the 50-Skill Trap" lands; "skills cost tokens" does not.
- **Explain the mechanism in a sentence** so the advice survives cases this skill never listed. Example: switching model mid-session throws away the prompt cache, so pick the model at the start; a cheaper model inside a subagent is fine because it has its own context.
- **Subagent cache.** Subagents get a 5-minute cache even on a subscription. Token Optimizer measures whether 1 hour pays on this user's transcripts and recommends it (it never sets the key for them); report what `subagent-cache status` measured and its `advice:` line, including when the payoff is negative for this user's pattern. The same verdict also sits in the daily `usage_recommendations` record the dashboard's "From your own usage" section reads.
- **Compaction window.** Present `compact-advice` as an estimate from their own history with cost and quality side by side, and give the command it printed. Earlier compaction saves cache-read tokens on every later turn and risks losing early instructions, so the user decides. The daily `usage_recommendations` record carries the distilled `recommend`/`keep` verdict too.
- **Usage recommendations.** When `usage_recommendations` carries a `recommend` item, lead with it: it is today's measured answer, already gated on enough data. When `wins` is non-empty, name the measured change "since you changed it" and quote both numbers; never claim the change caused it.
- **Goal e, or any time `deterministic_candidates` is non-empty:** walk the workflow step by step and sort each step into "inputs decide the output, so plain code" and "needs judgement, so a model", following the deterministic-stages reference.
- **Codex users** hear Codex terms only: `AGENTS.md`, Codex memories, balanced Codex hooks, reasoning effort and the model picker, compact prompt guidance. Claude model names and `CLAUDE.md` mean nothing to them.

Sound like a knowledgeable friend. Two to four exchanges is typical; follow the user's questions over any script.

## 5. Agree the action plan

Close with three to five actions ordered by impact. Each has a bold name, one line on what to do, the estimated saving and where that estimate comes from, and whether it is a quick win or a deeper change. Include, when they apply:

- Quality under 70 on Claude Code: `python3 "$MEASURE_PY" setup-smart-compact`. On Codex: `TOKEN_OPTIMIZER_RUNTIME=codex python3 "$MEASURE_PY" codex-install --project .`
- Quality under 50: `/compact` or `/clear` before more work.
- Steps to move from a model to a script, each with the mechanism (hook, script, scheduled job).

Then check the plan against the data: every figure traces to a command output or the quick reference, and no action touches Token Optimizer's own skills. Fix what fails and check again before sending.

Offer `/token-optimizer` when they want the changes made for them, and the dashboard (`python3 "$MEASURE_PY" dashboard`; on Codex quote the `Dashboard:` line it prints, never a remembered path) when they want to watch the trend.

## Keep-Warm (Claude Code, asked once)

After the plan, run `python3 "$MEASURE_PY" keepwarm-consent-status`. Only when `should_ask` is true, follow [references/keepwarm-consent.md](references/keepwarm-consent.md) exactly: it spends the user's money, so the wording and the order of commands are fixed.

Written for and checked on Claude Opus 5.5 and Sonnet 5.5, and GPT-5.6 on Codex.
