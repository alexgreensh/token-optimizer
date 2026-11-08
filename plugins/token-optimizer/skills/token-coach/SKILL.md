---
name: token-coach
description: Coaches a token-efficient Claude Code or Codex setup from the user's own usage data. Use when planning a build, fixing a slow or costly setup, or deciding which workflow steps need a model.
---

# Token Coach

A coaching conversation about where the user's tokens go and what to change, grounded in numbers measured on their machine. The deliverable is a short, prioritised action plan the user agrees with. For a full audit that edits their setup, hand over to `token-optimizer`.

Token Optimizer's own skills (`token-optimizer`, `token-coach`, `token-dashboard`, `fleet-auditor`, `resume-checkpoint`) are the measurement layer, so leave them out of every unused-skill, archive or consolidation suggestion, however rarely they run.

## 1. Collect the data

Run exactly this first. It finds the newest installed copy, so a stale plugin cache never answers. Each Bash call is a fresh shell, so env vars do not persist: the block echoes the resolved values, and every later command must use them verbatim.

```bash
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  # Harness config-dir env sits above CLAUDECODE, which every Claude Code child inherits.
  if [ -n "$CLAUDE_PLUGIN_ROOT" ] || [ -n "$CLAUDE_PLUGIN_DATA" ]; then RUNTIME="claude"
  elif [ -n "$OPENCODE" ] || [ -n "$OPENCODE_BIN" ] || [ -n "$OPENCODE_CONFIG_DIR" ] || [ -n "$OPENCODE_CONFIG" ]; then RUNTIME="opencode"
  elif [ -n "$CODEX_HOME" ]; then RUNTIME="codex"
  elif [ -n "$HERMES_HOME" ]; then RUNTIME="hermes"
  elif [ -n "$COPILOT_HOME" ] || [ -n "$TOKEN_OPTIMIZER_COPILOT_HOME" ]; then RUNTIME="copilot"
  elif [ -n "$TOKEN_OPTIMIZER_CURSOR_HOME" ]; then RUNTIME="cursor"
  elif [ -n "$TOKEN_OPTIMIZER_ANTIGRAVITY_HOME" ]; then RUNTIME="antigravity"
  elif [ -n "$GROK_HOME" ] || [ -n "$TOKEN_OPTIMIZER_GROK_HOME" ]; then RUNTIME="grok"
  elif [ -n "$CLAUDECODE" ] || [ -n "$CLAUDE_CODE_ENTRYPOINT" ] || [ -n "$CLAUDE_CODE_SESSION_ID" ]; then RUNTIME="claude"
  elif [ -d "$HOME/.config/opencode" ] && [ ! -d "$HOME/.codex" ]; then RUNTIME="opencode"
  elif [ -d "$HOME/.codex" ]; then RUNTIME="codex"
  else RUNTIME="claude"
  fi
fi

# Resolve measure.py to the NEWEST installed copy across channels so a stale
# plugin-cache copy never shadows a fresh install (find -L follows the
# install.sh symlink; cd -P resolves it before reading each copy's plugin.json).
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
# Cursor-only installs keep nothing under ~/.claude: the Cursor installer writes
# a locator naming the canonical measure.py (it is never copied into the plugin dir).
if [ -z "$MEASURE_PY" ]; then
  _cursor_loc="${TOKEN_OPTIMIZER_CURSOR_HOME:-$HOME/.cursor}/token-optimizer/plugin/measure-path"
  [ -f "$_cursor_loc" ] && MEASURE_PY="$(head -n1 "$_cursor_loc" 2>/dev/null)"
fi
if [ -z "$MEASURE_PY" ] || [ ! -f "$MEASURE_PY" ]; then echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; fi

# Resolve an interpreter that really runs Python. On native-Windows Git Bash
# `python3` is often the Microsoft Store alias stub, which fails silently under
# a pipe (#301). Probe `python` first there, `python3` first elsewhere; require >=3.9.
PY=""; _py_candidates="python3 python"
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) _py_candidates="python python3" ;; esac
for _py in $_py_candidates; do
  command -v "$_py" >/dev/null 2>&1 || continue
  _py_ver="$("$_py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || _py_ver=""
  _py_major="$(echo "$_py_ver" | cut -d. -f1)"; _py_minor="$(echo "$_py_ver" | cut -d. -f2)"
  [ -n "$_py_major" ] && [ -n "$_py_minor" ] || continue
  case "$_py_major$_py_minor" in *[!0-9]*) continue ;; esac
  if [ "$_py_major" -lt 3 ] 2>/dev/null || { [ "$_py_major" -eq 3 ] && [ "$_py_minor" -lt 9 ]; } 2>/dev/null; then continue; fi
  PY="$_py"; break
done
if [ -z "$PY" ]; then echo "[Error] Python 3.9+ not found (the Windows Store python3 stub does not count). Install Python, then retry."; exit 1; fi
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
# The coach files ship beside measure.py, so they are always the same version.
COACH_DIR="$(cd -P -- "$(dirname -- "$MEASURE_PY")/../../token-coach" 2>/dev/null && pwd)"
[ -d "$COACH_DIR/references" ] || echo "[Error] token-coach references not found beside $MEASURE_PY. Reinstall Token Optimizer." >&2

# Separate Bash calls do not share shell state: copy these into every later command.
# They print to stderr so a --json command chained in the same call keeps stdout parseable.
printf 'RUNTIME=%q\n' "$RUNTIME" >&2
printf 'MEASURE_PY=%q\n' "$MEASURE_PY" >&2
printf 'PY=%q\n' "$PY" >&2
printf 'COACH_DIR=%q\n' "$COACH_DIR" >&2
```

Then gather, skipping any command that fails (older installs lack some):

```bash
# These scan the Claude Code / Codex setup; under any other runtime measure.py
# prints a wrong-runtime notice instead of JSON, so they are gated.
if [ "$RUNTIME" = "claude" ] || [ "$RUNTIME" = "codex" ]; then
  "$PY" "$MEASURE_PY" coach --json                    # snapshot, patterns, history, questions
  "$PY" "$MEASURE_PY" quality current --json          # this session's quality score and issues
  "$PY" "$MEASURE_PY" subagent-cache status --json    # Claude Code: subagent cache lifetime and its payoff
  "$PY" "$MEASURE_PY" compact-advice --json           # Claude Code: would an earlier compact window pay off
  "$PY" "$MEASURE_PY" recommendations --json          # Claude Code: the daily usage-recommendations record
fi
[ "$RUNTIME" = "codex" ] && "$PY" "$MEASURE_PY" codex-doctor --project "$PWD" --json
# Cursor: native coach data (sessions, quality, patterns), then hook evidence
# and the session token/quality summary.
[ "$RUNTIME" = "cursor" ] && "$PY" "$MEASURE_PY" cursor-coach --json
[ "$RUNTIME" = "cursor" ] && "$PY" "$MEASURE_PY" cursor-doctor --json
[ "$RUNTIME" = "cursor" ] && "$PY" "$MEASURE_PY" cursor-summary
```

Under Cursor, `coach --json` does not run (it scans the Claude Code setup); `cursor-coach --json` is the data source instead. It carries `snapshot` (sessions analyzed, turns/tool calls/compactions, token provenance counts, the cost block that always reads `cursor_no_cost_data`), `patterns_good`/`patterns_bad` built from real session evidence (hot pre-compaction fill, repeated compactions, recorded tool failures, unknown token footprints), `history` from the rollup's Cursor rows, `questions`, and a `health_score` built from per-session quality scores whose `signals_active`/`signals_omitted` say exactly which evidence backed them - plus what the user tells you.

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
- **Cursor users** hear Cursor terms only: `.cursor/rules` and `AGENTS.md` (there is no `CLAUDE.md`/`MEMORY.md` surface), and the hooks in `~/.cursor/hooks.json`. Cursor exposes no local billing data, so coach in tokens and never in dollars — a $0 is "no data", never "free". Quote token provenance with the number: rollup-resolved (`state.vscdb`, best-effort), transcript estimate, or tally-only unknown. Context-fill claims need a real occupancy snapshot (the numbers preCompact records); a lifetime token sum is never a fill. When `cursor-doctor` reports a channel `unknown` or `shadow`, say so — never imply a protection ran that has no delivery evidence on this install.

Sound like a knowledgeable friend. Two to four exchanges is typical; follow the user's questions over any script.

## 5. Agree the action plan

Close with three to five actions ordered by impact. Each has a bold name, one line on what to do, the estimated saving and where that estimate comes from, and whether it is a quick win or a deeper change. Include, when they apply:

- Quality under 70 on Claude Code: `"$PY" "$MEASURE_PY" setup-smart-compact`. On Codex: `TOKEN_OPTIMIZER_RUNTIME=codex "$PY" "$MEASURE_PY" codex-install --project .`
- Quality under 50: `/compact` or `/clear` before more work.
- Steps to move from a model to a script, each with the mechanism (hook, script, scheduled job).

Then check the plan against the data: every figure traces to a command output or the quick reference, and no action touches Token Optimizer's own skills. Fix what fails and check again before sending.

Offer `/token-optimizer` when they want the changes made for them, and the dashboard (`"$PY" "$MEASURE_PY" dashboard`; on Codex quote the `Dashboard:` line it prints, never a remembered path) when they want to watch the trend.

## Keep-Warm (Claude Code, asked once)

After the plan, run `"$PY" "$MEASURE_PY" keepwarm-consent-status`. Only when `should_ask` is true, follow [references/keepwarm-consent.md](references/keepwarm-consent.md) exactly: it spends the user's money, so the wording and the order of commands are fixed.

Written for and checked on Claude Opus 5.5 and Sonnet 5.5, and GPT-5.6 on Codex.
