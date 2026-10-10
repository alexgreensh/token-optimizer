---
name: token-optimizer
description: Audits a Claude Code or Codex setup for context-window waste, fixes it with the user's approval, and measures the saving. Use when context feels tight, sessions degrade, or costs climb.
effort: high
---

# Token Optimizer

Measures where a setup spends its context window, proposes fixes, applies the ones the user approves, and re-measures. The deliverable is a before-and-after the user can verify, with every change backed up. Typical recovery is 5-15% of the window from config cleanup, more when compaction and caching settings are wrong for the way the user works.

Token Optimizer's own skills (`token-optimizer`, `token-coach`, `token-dashboard`, `fleet-auditor`, `resume-checkpoint`) are the measurement layer, so leave them out of every unused-skill, archive or consolidation suggestion, however rarely they run.

## Step 0: pick the right workflow for this runtime

The audit below reads and edits `~/.claude`, which is the wrong target anywhere but Claude Code, so settle the runtime before touching any path. Run exactly this, in this order.

**a. Environment check** (touches no file; an explicit `TOKEN_OPTIMIZER_RUNTIME` wins, and Claude plugin variables are checked before stray `OPENCODE_*` exports, the same order `detect_runtime()` uses):

```bash
if [ "${TOKEN_OPTIMIZER_RUNTIME:-}" = "claude" ] || [ "${TOKEN_OPTIMIZER_RUNTIME:-}" = "codex" ]; then
  :
elif [ "${TOKEN_OPTIMIZER_RUNTIME:-}" = "opencode" ]; then
  echo "Token Optimizer — OpenCode runtime detected."
elif [ "${TOKEN_OPTIMIZER_RUNTIME:-}" = "copilot" ]; then
  echo "Token Optimizer — GitHub Copilot runtime detected."
elif [ "${TOKEN_OPTIMIZER_RUNTIME:-}" = "cursor" ]; then
  echo "Token Optimizer — Cursor runtime detected."
elif [ "${TOKEN_OPTIMIZER_RUNTIME:-}" = "antigravity" ]; then
  echo "Token Optimizer — Google Antigravity runtime detected."
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}${CLAUDE_PLUGIN_DATA:-}" ]; then
  :
elif [ -n "${OPENCODE_BIN:-}${OPENCODE_CONFIG_DIR:-}${OPENCODE_DATA_DIR:-}${OPENCODE_CONFIG:-}${OPENCODE_CLIENT:-}" ]; then
  echo "Token Optimizer — OpenCode runtime detected."
elif [ -n "${COPILOT_HOME:-}${TOKEN_OPTIMIZER_COPILOT_HOME:-}" ]; then
  echo "Token Optimizer — GitHub Copilot runtime detected."
elif [ -n "${TOKEN_OPTIMIZER_CURSOR_HOME:-}" ]; then
  echo "Token Optimizer — Cursor runtime detected."
elif [ -n "${CURSOR_PROJECT_DIR:-}" ] && [ -n "${CURSOR_VERSION:-}" ]; then
  echo "Token Optimizer — Cursor runtime detected."
elif [ -n "${TOKEN_OPTIMIZER_ANTIGRAVITY_HOME:-}" ]; then
  echo "Token Optimizer — Google Antigravity runtime detected."
fi
```

| It prints | Do this |
|-----------|---------|
| "OpenCode runtime detected." | Stop. Follow `references/opencode-workflow.md` (Token Optimizer runs as a native plugin there). |
| "Cursor runtime detected." | Stop. Follow `references/cursor-workflow.md` (it runs through the Cursor hook bridge). |
| "GitHub Copilot runtime detected." | Stop. Follow the Copilot guidance in `docs/copilot.md`. |
| "Google Antigravity runtime detected." | Stop. Follow `docs/antigravity.md`. |
| nothing | Continue to b. |

**b. Resolve `$MEASURE_PY` once.** Every later command uses it:

```bash
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
$(find -L "$HOME/.claude/skills" "$HOME/.claude/plugins/cache" "$HOME/.claude/token-optimizer" "$HOME/.codex/skills" "$HOME/.codex/plugins/cache" "$HOME/.config/opencode/plugins" -type f -name measure.py -path '*token-optimizer*/scripts/measure.py' 2>/dev/null)
EOF
if [ -z "$MEASURE_PY" ]; then echo "[Error] measure.py not found. Is Token Optimizer installed?"; exit 1; fi
```

**c. The authoritative gate.** The environment check cannot see an OpenCode started without its variables; this one scans the process tree:

```bash
python3 "$MEASURE_PY" report 2>&1 | head -1
```

Any "... runtime detected." line means stop and follow that runtime's file from the table above. On Codex (`TOKEN_OPTIMIZER_RUNTIME=codex` or a Codex environment) follow `references/codex-workflow.md`. Claude Code continues below.

## The audit (Claude Code)

Work through these in order and tick each off; a skipped phase stays in the list as `skip: <reason>`.

```
- [ ] 0. Setup            references/phase0-setup.md
- [ ] 1. Parallel audit   references/agent-prompts.md
- [ ] 2. Synthesis        references/agent-prompts.md (Synthesis Agent)
- [ ] 3. Present, then wait for the user's decision   references/presentation-workflow.md
- [ ] 4. Implement the approved changes               references/implementation-playbook.md
- [ ] 5. Verify and report before/after               references/agent-prompts.md (Verification Agent)
```

**0. Setup.** Context window detection, pre-check, backup, the coordination folder (`COORD_PATH`), hook checks, daemon, smart compaction. Then, only when `python3 "$MEASURE_PY" keepwarm-consent-status` returns `should_ask: true`, follow `references/keepwarm-consent.md` exactly; it spends the user's money, so its wording and command order are fixed.

**1. Parallel audit.** Dispatch six agents in one message, each with `COORD_PATH` and its prompt from `references/agent-prompts.md`. They gather and count, so they run on the cheapest capable model; the synthesis is where judgement is spent.

| Agent | Writes | Looks at |
|-------|--------|----------|
| CLAUDE.md | `audit/claudemd.md` | Size, duplication, tiering, cache structure |
| MEMORY.md | `audit/memorymd.md` | Size, overlap with CLAUDE.md |
| Skills | `audit/skills.md` | Count, frontmatter overhead, duplicates |
| MCP | `audit/mcp.md` | Deferred tools, broken or unused servers |
| Commands | `audit/commands.md` | Count, menu overhead |
| Settings and advanced | `audit/advanced.md` | Hooks, rules, settings, @imports, cache lifetime, compaction window |

A missing output file is a gap to note, not a reason to stop.
**2. Synthesis.** One agent on the strongest available model reads every audit file and writes `{COORD_PATH}/analysis/optimization-plan.md`. If it fails, present the raw audit files.

**3. Present.** Generate the dashboard and show the findings as the presentation workflow describes, then wait:

```bash
python3 "$MEASURE_PY" dashboard --coord-path "$COORD_PATH"
```

**4. Implement.** Only what the user approved, one change at a time, following the playbook (actions 4A-4P: CLAUDE.md, MEMORY.md, skills, file exclusion, MCP, hooks, cache, rules, settings, descriptions, compact instructions, model routing, smart compaction, quality check, version-aware optimizations, smart routing). Templates are in `examples/`. Also offer, with the measured numbers:

- `python3 "$MEASURE_PY" subagent-cache status`: the subagent cache lifetime and what it is worth to this user. Relay its `advice:` line verbatim (their own numbers + `subagent-cache enable|disable`); Token Optimizer recommends, it never sets the key for them.
- `python3 "$MEASURE_PY" compact-advice`: whether an earlier compaction window would pay off on their own history. It is an estimate; the user decides.
- Repeated work a script could do instead of a model: hand over to `token-coach`, goal e.

**5. Verify.** The verification agent re-measures and computes the saving. Show before and after, then the habits that keep it.

If a verification number is worse than the baseline, find which change caused it, restore that file from the backup, and return to step 4.

## Rules for changing a user's setup

These protect work the user cannot easily get back:

- Back up before every change, and show the diff before applying it.
- Archive, never delete, and archive outside the skills directory so the archive does not load.
- Check what depends on a skill, MCP server or deny rule before archiving it, and name the side effect first.
- Prefer a project-level deny rule to a global one.
- Leave the user's own settings values alone unless they approve the specific change.
- Quote measured numbers (tokens and percent of the window). Frame savings as context budget; give dollars only where `measure.py` computed them.

## References

| Need | Read |
|------|------|
| Codex, Cursor, OpenCode workflows | `references/codex-workflow.md`, `references/cursor-workflow.md`, `references/opencode-workflow.md` |
| Setup details, Keep-Warm consent | `references/phase0-setup.md`, `references/keepwarm-consent.md` |
| Agent prompts, how tokens flow | `references/agent-prompts.md`, `references/token-flow-architecture.md` |
| Presenting findings | `references/presentation-workflow.md` |
| Implementation steps, checklist | `references/implementation-playbook.md`, `references/optimization-checklist.md`, `examples/` |
| CLI commands, session continuity, errors | `references/cli-reference.md`, `references/session-continuity.md`, `references/error-recovery.md` |

Written for and checked on Claude Opus 5.5 and Sonnet 5.5, and GPT-5.6 on Codex.
