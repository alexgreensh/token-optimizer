---
name: token-dashboard
description: Answers what a past session cost ("the Garden session yesterday", a session id) with a priced number, and opens the Token Optimizer dashboard when asked to see it.
---

# Token Optimizer Dashboard

Answers what a session cost from the user's own words, and opens an up-to-date dashboard of context usage, quality, session history and skills. Open the dashboard only when the user asks to see it; a cost question never opens a browser.

## Instructions

1. **Resolve runtime and measure.py path** (each Bash call is a fresh shell, so env vars do not persist: the block echoes the resolved values, and every later command must use them verbatim):
```bash
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  if [ -n "$CLAUDE_PLUGIN_ROOT" ] || [ -n "$CLAUDE_PLUGIN_DATA" ]; then
    RUNTIME="claude"
  elif [ -n "$OPENCODE" ] || [ -n "$OPENCODE_BIN" ] || [ -n "$OPENCODE_CONFIG_DIR" ] || [ -n "$OPENCODE_CONFIG" ]; then
    RUNTIME="opencode"
  elif [ -n "$CODEX_HOME" ]; then
    RUNTIME="codex"
  # Explicit config-dir env of another harness: same tier as Python's
  # detect_runtime() (above CLAUDECODE, which every Claude Code child inherits).
  elif [ -n "$HERMES_HOME" ]; then
    RUNTIME="hermes"
  elif [ -n "$COPILOT_HOME" ] || [ -n "$TOKEN_OPTIMIZER_COPILOT_HOME" ]; then
    RUNTIME="copilot"
  elif [ -n "$TOKEN_OPTIMIZER_CURSOR_HOME" ]; then
    RUNTIME="cursor"
  elif [ -n "$TOKEN_OPTIMIZER_ANTIGRAVITY_HOME" ]; then
    RUNTIME="antigravity"
  elif [ -n "$GROK_HOME" ] || [ -n "$TOKEN_OPTIMIZER_GROK_HOME" ]; then
    RUNTIME="grok"
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
if [ -z "$MEASURE_PY" ]; then echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; fi

# Resolve an interpreter that really runs Python. On native-Windows Git Bash
# `python3` is often the Microsoft Store alias stub, which prints "Python was
# not found" instead of running -- and a gate piped to `head` then looks like
# a pass (#301). Probe `python` first there, `python3` first elsewhere, and
# accept only a clean >=3.9 version reply (same ladder install.sh uses).
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

# Separate Bash calls do not share shell state: copy these into every later command.
# They print to stderr so a --json command chained in the same call keeps stdout parseable.
printf 'RUNTIME=%q\n' "$RUNTIME" >&2
printf 'MEASURE_PY=%q\n' "$MEASURE_PY" >&2
printf 'PY=%q\n' "$PY" >&2
```

2. **A cost question** ("what did the Garden session yesterday cost?", "how much were my TR sync sessions?", "cost of session <id>"): run this instead of steps 3-4.
```bash
"$PY" "$MEASURE_PY" session-cost <the user's words> --json
```
   - Pass the user's own words (`garden yesterday`, `tr sync`, an id or its first 8 characters). Names match topic, slug and project, case-insensitively, and all of them must match; `today`, `yesterday` and `YYYY-MM-DD` set the day. Add `--days N` for "this week" (default 30), `--limit N` to list more.
   - Answer from `sessions[]`: date, topic, `cost_usd.total`, the main-thread and subagent split when subagents ran, `cost_usd.on_date` when a day was asked and the session ran over several days, and `partial_reason` when set. Several matches: give `total_cost_usd` and list them, asking which one only when it changes the answer.
   - State `basis` in one line: API list prices, so on a subscription it is what the work would have cost, not a bill.
   - No match: say so, show `closest[]`, and pass on `note` (a session is collected when it ends).
   - Never query trends.db or price tokens yourself: this is the dashboard's own number, read without writing.

3. **Collect and open**:
```bash
# TOKEN_OPTIMIZER_INTERACTIVE=1 marks this as a user-initiated open, so the 20s
# hook budget does not cut off a heavy rebuild on a large history. The user is
# waiting for this open, so it runs unbounded. --user is the argv twin of the
# env var for shells where `VAR=1 cmd` does not parse (Windows).
TOKEN_OPTIMIZER_INTERACTIVE=1 "$PY" "$MEASURE_PY" collect --quiet --user && \
  TOKEN_OPTIMIZER_INTERACTIVE=1 "$PY" "$MEASURE_PY" dashboard --user
```

This collects the latest session data into the trends database, regenerates the dashboard HTML using the last selected history window (30 days until changed), and opens it in your default browser. If the user asks for a different window, add `--days N` or `--days all` to the dashboard command; later refreshes retain that choice.

4. **Tell the user** the dashboard is open. Probe the daemon before mentioning any URL:
   - Probe daemon: `"$PY" "$MEASURE_PY" daemon-status 2>/dev/null`
   - If DAEMON_RUNNING: lead with `URL: http://localhost:24842/token-optimizer` (bookmarkable, auto-updates), then mention the file fallback.
   - If DAEMON_NOT_RUNNING: do not print the `localhost:24842` URL. Tell the user the dashboard opened as a file, and suggest `"$PY" "$MEASURE_PY" setup-daemon` (macOS and Windows) if they want a bookmarkable URL.
   - File path: never hardcode it. It is install-dependent. Cite the `  Dashboard: ` line from the step-3 output.
   - For Codex when DAEMON_NOT_RUNNING: do not imply the Claude daemon is required; the generated file works, and Stop hooks refresh it when balanced hooks are installed.

Written for and checked on Claude Opus 5.5 and Sonnet 5.5, and GPT-5.6 on Codex.
