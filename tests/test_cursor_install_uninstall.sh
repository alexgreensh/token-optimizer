#!/usr/bin/env bash
# Regression tests for `install.sh --cursor` and `install.sh --cursor --uninstall`.
#
# Cursor has ONE shared ~/.cursor/hooks.json, so the shell wrappers must merge
# not clobber, and the uninstaller must remove only Token Optimizer's entries.
# These tests pin the shell-level behavior against a temp HOME + cursor home.
#
# Run directly:  bash tests/test_cursor_install_uninstall.sh
# Exits non-zero on first failure.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SH="${REPO}/install.sh"

# Source install.sh in test mode so the functions are defined without
# triggering the install flow / prerequisite checks.
_TO_INSTALL_SH_TEST_MODE=1 source "${INSTALL_SH}"

pass=0
fail=0

ok() { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
nok() { printf '  FAIL  %s: %s\n' "$1" "$2"; fail=$((fail+1)); }

# --- 1. install writes the six-event merged hooks.json ----------------------
tmp1="$(mktemp -d)"
mkdir -p "${tmp1}/home/.cursor"
cat > "${tmp1}/home/.cursor/hooks.json" <<'JSON'
{"version": 1, "hooks": {"sessionStart": [{"command": "echo other-tool", "type": "command", "timeout": 5}]}}
JSON
HOME="${tmp1}/home" TOKEN_OPTIMIZER_CURSOR_HOME="${tmp1}/home/.cursor" \
  out="$(install_cursor 2>&1)" || { nok "install-exits-zero" "exit=$?: $out"; rm -rf "$tmp1"; exit 1; }
events_ok="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); s=sorted((d.get("hooks") or {}).keys()); print("ok" if s == ["afterAgentResponse","afterShellExecution","postToolUse","postToolUseFailure","preCompact","preToolUse","sessionEnd","sessionStart","stop","subagentStart","subagentStop"] else ",".join(s))' "${tmp1}/home/.cursor/hooks.json")"
if [ "$events_ok" = "ok" ]; then ok "install-wires-events"; else nok "install-wires-events" "events=$events_ok"; fi
starts="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d["hooks"]["sessionStart"]))' "${tmp1}/home/.cursor/hooks.json")"
if printf '%s' "$starts" | grep -q 'other-tool'; then ok "install-preserves-foreign-entry"; else nok "install-preserves-foreign-entry" "starts=$starts"; fi
if [ -f "${tmp1}/home/.cursor/token-optimizer/plugin/cursor_hook_bridge.py" ]; then ok "install-copies-payload"; else nok "install-copies-payload" "payload missing"; fi
rm -rf "$tmp1"

# --- 2. re-install is idempotent (single "ours" entry per event) ------------
tmp2="$(mktemp -d)"
mkdir -p "${tmp2}/home/.cursor"
( HOME="${tmp2}/home" TOKEN_OPTIMIZER_CURSOR_HOME="${tmp2}/home/.cursor" install_cursor >/dev/null 2>&1 )
( HOME="${tmp2}/home" TOKEN_OPTIMIZER_CURSOR_HOME="${tmp2}/home/.cursor" install_cursor >/dev/null 2>&1 )
ours="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); h=d["hooks"]["stop"]; print(len([e for e in h if "cursor_hook_bridge.py" in e["command"]]))' "${tmp2}/home/.cursor/hooks.json")"
if [ "$ours" = "1" ]; then ok "install-idempotent"; else nok "install-idempotent" "ours=$ours"; fi
rm -rf "$tmp2"

# --- 3. uninstall removes only ours, keeps foreign, drops payload -----------
tmp3="$(mktemp -d)"
mkdir -p "${tmp3}/home/.cursor"
cat > "${tmp3}/home/.cursor/hooks.json" <<'JSON'
{"version": 1, "hooks": {"sessionStart": [{"command": "echo other-tool", "type": "command", "timeout": 5}]}}
JSON
( HOME="${tmp3}/home" TOKEN_OPTIMIZER_CURSOR_HOME="${tmp3}/home/.cursor" install_cursor >/dev/null 2>&1 )
( HOME="${tmp3}/home" TOKEN_OPTIMIZER_CURSOR_HOME="${tmp3}/home/.cursor" uninstall_cursor >/dev/null 2>&1 )
starts="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d["hooks"]["sessionStart"]))' "${tmp3}/home/.cursor/hooks.json")"
ours="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); h=d["hooks"]["sessionStart"]; print(len([e for e in h if "cursor_hook_bridge.py" in e["command"]]))' "${tmp3}/home/.cursor/hooks.json")"
if printf '%s' "$starts" | grep -q 'other-tool' && [ "$ours" = "0" ]; then ok "uninstall-keeps-foreign-removes-ours"; else nok "uninstall-keeps-foreign-removes-ours" "starts=$starts ours=$ours"; fi
if [ ! -d "${tmp3}/home/.cursor/token-optimizer/plugin" ]; then ok "uninstall-removes-payload"; else nok "uninstall-removes-payload" "plugin dir still present"; fi
rm -rf "$tmp3"

# --- 4. --dry-run writes nothing --------------------------------------------
tmp4="$(mktemp -d)"
mkdir -p "${tmp4}/home/.cursor"
( HOME="${tmp4}/home" TOKEN_OPTIMIZER_CURSOR_HOME="${tmp4}/home/.cursor" install_cursor --dry-run >/dev/null 2>&1 )
if [ ! -f "${tmp4}/home/.cursor/hooks.json" ] && [ ! -d "${tmp4}/home/.cursor/token-optimizer" ]; then ok "dry-run-writes-nothing"; else nok "dry-run-writes-nothing" "hooks or payload written"; fi
rm -rf "$tmp4"

printf '\n%d/%d passed\n' "$pass" "$((pass+fail))"
[ "$fail" -eq 0 ]
