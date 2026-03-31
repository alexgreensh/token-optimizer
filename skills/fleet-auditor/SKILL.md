---
name: fleet-auditor
description: Audit token waste across agent systems (Claude Code, OpenClaw, Hermes, OpenCode). Detect idle burns, model misrouting, and config bloat with dollar savings.
---

# Fleet Auditor: Cross-Platform Agent Token Waste Auditor

Detects installed agent systems, collects token usage data, identifies waste patterns, and recommends fixes with dollar savings estimates. Everyone tracks. Nobody coaches. Until now.

**Use when**: Running multiple agent systems, spending $2-5/day on agents, suspecting idle heartbeats are burning tokens, or want a cross-system cost audit.

---

## Phase 0: Initialize

1. **Resolve fleet.py path** (works for both skill and plugin installs):
```bash
FLEET_PY=""
for f in "$HOME/.claude/skills/fleet-auditor/scripts/fleet.py" \
         "$HOME/.claude/plugins/cache"/*/token-optimizer/*/skills/fleet-auditor/scripts/fleet.py; do
  [ -f "$f" ] && FLEET_PY="$f" && break
done
[ -z "$FLEET_PY" ] && { echo "[Error] fleet.py not found. Is Fleet Auditor installed?"; exit 1; }
echo "Using: $FLEET_PY"
```
Use `$FLEET_PY` for all subsequent fleet.py calls.

2. **Detect systems**:
```bash
python3 $FLEET_PY detect --json
```
Parse the JSON output. Report what was found.

If nothing detected, explain: "No agent systems found. Fleet Auditor supports: Claude Code, OpenClaw, NanoClaw, Hermes, OpenCode, IronClaw."

---

## Phase 1: Scan

Collect token usage data from detected systems:
```bash
python3 $FLEET_PY scan --days 30
```

Report how many runs were collected per system. If this is the first scan, it may take a moment to parse all session files.

---

## Phase 2: Audit

Run waste pattern detection:
```bash
python3 $FLEET_PY audit --json
```

Parse the JSON output. Present findings ordered by severity and monthly savings.

If no waste found: "Your fleet looks clean. No significant waste patterns detected."

**NEW: OpenClaw Security Check** (March 2026):
- If OpenClaw is detected, flag if version is older than 2026.3.12 (31 CVEs, including ClawJacked full agent takeover)
- Check for skills from ClawHub that match known-malicious patterns (ClawHavoc campaign: 1,184+ malicious skills)
- Flag if gateway password brute-force protection is not configured (CVE-2026-25253)
- Recommend: `openclaw config set security.rateLimit.enabled true`

---

## Phase 3: Present Findings

```
[Fleet Auditor Results]

SYSTEMS DETECTED
- Claude Code: X runs ($Y.YY)
- OpenClaw: X runs ($Y.YY)

WASTE PATTERNS FOUND
1. [SEVERITY] Description
   Est. savings: $X.XX/month
   Fix: recommendation

2. [SEVERITY] Description
   ...

TOTAL POTENTIAL SAVINGS: $X.XX/month

Ready to act? I can:
1. Show detailed fix snippets for each finding
2. Generate the fleet dashboard for visual analysis
3. Run /token-optimizer for deeper Claude Code optimization
```

---

## Phase 4: Dashboard (optional)

If user wants visual analysis:
```bash
python3 $FLEET_PY dashboard
```

This generates `~/.claude/_backups/token-optimizer/fleet-dashboard.html` and opens it in the browser.

---

## Phase 5: Deep Dive (optional)

For Claude Code specifically, offer `/token-optimizer` for full audit (CLAUDE.md, skills, MCP, hooks, etc.).

For other systems, show the fix snippets from the audit and guide the user through implementing them.

---

## Reference Files

| Phase | Read |
|-------|------|
| Adapter development | `references/fleet-systems.md` |
| Detector development | `references/waste-patterns.md` |

---

## Error Handling

- **No systems detected**: Report cleanly, list supported systems
- **Empty scan results**: System detected but no session data in window. Suggest increasing `--days`
- **Permission errors**: Report which files couldn't be read, continue with available data
- **Corrupted data**: Skip bad files, report count of skipped files
- **fleet.py not found**: Check both skill and plugin install paths

---

## Core Rules

- Quantify everything in dollars AND tokens
- Never read or expose message content (privacy-first)
- Report confidence levels alongside findings
- Suppress findings below 0.4 confidence threshold
- Always show fix snippets with recommendations
- Frame savings as monthly recurring, not one-time
