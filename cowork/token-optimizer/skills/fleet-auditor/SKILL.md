---
name: fleet-auditor
description: "Audits token waste and cost across Claude Code, Codex, OpenClaw, Hermes and OpenCode, with dollar savings. Use when running several agent systems or suspecting idle heartbeats burn tokens."
---

# Fleet Auditor: Cross-Platform Agent Token Waste Auditor

Token Optimizer's own skills (`token-optimizer`, `token-coach`, `token-dashboard`, `fleet-auditor`) are the measurement layer, so leave them out of every removal, archive, trimming, disabling or consolidation suggestion; deleting them to save a few hundred tokens defeats the audit.

Detects installed agent systems, collects token usage data, identifies waste patterns, and recommends fixes with dollar savings estimates.

Use when running multiple agent systems, spending $2-5/day on agents, suspecting idle heartbeats are burning tokens, or wanting a cross-system cost audit.

---

## Phase 0: Initialize

1. **Resolve runtime and fleet.py path** (works for both skill and plugin installs):
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

# Resolve fleet.py to the NEWEST installed copy across channels so a stale
# plugin-cache copy never shadows a fresh install. find -L follows the
# install.sh symlink under ~/.claude/skills; cd -P resolves it before reading each
# copy's plugin.json for its version. find (not bare globs) never errors under zsh.
FLEET_PY=""; _best_ver=""
while IFS= read -r _cand; do
  [ -f "$_cand" ] || continue
  _root="$(cd -P -- "$(dirname -- "$_cand")/../../.." 2>/dev/null && pwd)"
  _ver="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_root/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
  [ -n "$_ver" ] || _ver="0.0.0"
  if [ -z "$_best_ver" ] || [ "$(printf '%s\n%s\n' "$_ver" "$_best_ver" | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -n1)" = "$_ver" ]; then
    _best_ver="$_ver"; FLEET_PY="$_cand"
  fi
done <<EOF
$(find -L "$HOME/.claude/skills" "$HOME/.claude/plugins/cache" "$HOME/.claude/token-optimizer" "$HOME/.codex/skills" "$HOME/.codex/plugins/cache" "$HOME/.config/opencode/plugins/cache" "$HOME/.config/opencode/plugins" -type f -name fleet.py -path '*fleet-auditor*/scripts/fleet.py' 2>/dev/null)
EOF
if [ -z "$FLEET_PY" ]; then echo "[Error] fleet.py not found. Is Fleet Auditor installed?"; exit 1; fi
echo "Using: $FLEET_PY"
export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"
```
Use `$FLEET_PY` for all subsequent fleet.py calls.

2. **Detect systems**:
```bash
python3 "$FLEET_PY" detect --json
```
Parse the JSON output. Report what was found.

If nothing detected, explain: "No agent systems found. Fleet Auditor supports: Claude Code, Codex, OpenClaw, NanoClaw, Hermes, OpenCode, IronClaw."

---

## Phase 1: Scan

Collect token usage data from detected systems:
```bash
python3 "$FLEET_PY" scan --days 30
```

Report how many runs were collected per system. If this is the first scan, it may take a moment to parse all session files.

---

## Phase 2: Audit

Run waste pattern detection:
```bash
python3 "$FLEET_PY" audit --json
```

Parse the JSON output. Present findings ordered by severity and monthly savings.

If no waste found: "Your fleet looks clean. No significant waste patterns detected."

Dollar figures need a price for the model. For Codex and other non-Claude systems, and for the OpenClaw security checks, read `references/report-template.md` before presenting.

---

## Phase 3: Present Findings

Use the layout in `references/report-template.md`: systems detected, waste patterns with severity, monthly savings and a fix for each, then the total and the next-step options.

---

## Phase 4: Dashboard (optional)

If user wants visual analysis:
```bash
python3 "$FLEET_PY" dashboard
```

This generates `~/.claude/_backups/token-optimizer/fleet-dashboard.html` in Claude Code, or `~/.codex/_backups/token-optimizer/fleet-dashboard.html` when `TOKEN_OPTIMIZER_RUNTIME=codex`.

---

## Phase 5: Deep Dive (optional)

For Claude Code specifically, offer `/token-optimizer` for full audit (`CLAUDE.md`, skills, MCP, hooks, etc.).

For Codex specifically, offer `token-optimizer` for full audit (`AGENTS.md`, Codex memories, plugin skills, MCP, balanced hooks, compact prompt, status line).

For other systems, show the fix snippets from the audit and guide the user through implementing them.

---

## Reference Files

| Read | When |
|------|------|
| [references/report-template.md](references/report-template.md) | Presenting findings, Codex dollar caveats, OpenClaw checks |
| [references/waste-patterns.md](references/waste-patterns.md) | Detector thresholds, severity and confidence levels |
| [references/fleet-systems.md](references/fleet-systems.md) | Per-system data locations and token fields |

---

## Error Handling

- **No systems detected**: report cleanly and list the supported systems
- **Empty scan results**: system detected but no session data in the window; suggest a larger `--days`
- **Permission errors**: name the files that could not be read and continue with the rest
- **Corrupted data**: skip bad files and report how many were skipped
- **fleet.py not found**: check both the skill and plugin install paths

## Core Rules

- Quantify everything in dollars and tokens
- Never read or expose message content (privacy-first)
- Report confidence levels alongside findings and suppress anything below 0.4
- Show a fix snippet with every recommendation
- Frame savings as monthly recurring, not one-time

Written for and checked on Claude Opus 5.5 and Sonnet 5.5, and GPT-5.6 on Codex.
