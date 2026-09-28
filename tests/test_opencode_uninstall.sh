#!/usr/bin/env bash
# Regression tests for `install.sh --opencode --uninstall`.
#
# Before the fix, OpenCode had NO uninstaller: install_opencode() copied a
# bundled token-optimizer.js into ~/.config/opencode/plugins/ and nothing
# removed it. The fix adds uninstall_opencode() which removes the bundle and
# reverts the token-optimizer-opencode entry from opencode.json's plugin
# array. Idempotent, --dry-run aware, never clobbers user plugin entries.
#
# Run directly:  bash tests/test_opencode_uninstall.sh
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

# --- 1. nothing to remove (idempotent on a clean tree) ----------------------
tmp1="$(mktemp -d)"
OPENCODE_CONFIG_DIR="${tmp1}" HOME_BACKUP="${HOME}"
export OPENCODE_CONFIG_DIR
out="$(uninstall_opencode 2>&1)" || true
if printf '%s' "$out" | grep -q "nothing to remove"; then ok "clean-tree-noop"; else nok "clean-tree-noop" "expected 'nothing to remove', got: $out"; fi
rm -rf "${tmp1}"
unset OPENCODE_CONFIG_DIR

# --- 2. removes the bundle --------------------------------------------------
tmp2="$(mktemp -d)"
mkdir -p "${tmp2}/plugins"
printf '/* bundle */\n' > "${tmp2}/plugins/token-optimizer.js"
OPENCODE_CONFIG_DIR="${tmp2}" out="$(uninstall_opencode 2>&1)" || true
if [ ! -f "${tmp2}/plugins/token-optimizer.js" ]; then ok "removes-bundle"; else nok "removes-bundle" "bundle still present"; fi
if printf '%s' "$out" | grep -q "uninstalled"; then ok "removes-bundle-msg"; else nok "removes-bundle-msg" "no uninstalled banner: $out"; fi
rm -rf "${tmp2}"
unset OPENCODE_CONFIG_DIR

# --- 3. reverts the plugin entry from opencode.json -------------------------
tmp3="$(mktemp -d)"
mkdir -p "${tmp3}/plugins"
printf '/* bundle */\n' > "${tmp3}/plugins/token-optimizer.js"
cat > "${tmp3}/opencode.json" <<'JSON'
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["token-optimizer-opencode", "someone-elses-plugin"]
}
JSON
OPENCODE_CONFIG_DIR="${tmp3}" out="$(uninstall_opencode 2>&1)" || true
remaining="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(",".join(d.get("plugin",[])))' "${tmp3}/opencode.json" 2>/dev/null)"
if [ "$remaining" = "someone-elses-plugin" ]; then ok "reverts-plugin-entry-keeps-others"; else nok "reverts-plugin-entry-keeps-others" "remaining='$remaining'"; fi
rm -rf "${tmp3}"
unset OPENCODE_CONFIG_DIR

# --- 4. dry-run does not write ---------------------------------------------
tmp4="$(mktemp -d)"
mkdir -p "${tmp4}/plugins"
printf '/* bundle */\n' > "${tmp4}/plugins/token-optimizer.js"
bundle_before="$(cat "${tmp4}/plugins/token-optimizer.js")"
OPENCODE_CONFIG_DIR="${tmp4}" out="$(uninstall_opencode --dry-run 2>&1)" || true
bundle_after="$(cat "${tmp4}/plugins/token-optimizer.js" 2>/dev/null || echo MISSING)"
if [ "$bundle_after" = "$bundle_before" ]; then ok "dry-run-no-write"; else nok "dry-run-no-write" "bundle changed/removed under dry-run"; fi
if printf '%s' "$out" | grep -q "Dry run"; then ok "dry-run-msg"; else nok "dry-run-msg" "no dry-run banner: $out"; fi
rm -rf "${tmp4}"
unset OPENCODE_CONFIG_DIR

# --- 5. idempotent (second run is a no-op) ----------------------------------
tmp5="$(mktemp -d)"
mkdir -p "${tmp5}/plugins"
printf '/* bundle */\n' > "${tmp5}/plugins/token-optimizer.js"
export OPENCODE_CONFIG_DIR="${tmp5}"
( uninstall_opencode >/dev/null 2>&1 ) || true
out2="$(uninstall_opencode 2>&1)" || true
if printf '%s' "$out2" | grep -q "nothing to remove"; then ok "idempotent-second-run"; else nok "idempotent-second-run" "second run not noop: $out2"; fi
rm -rf "${tmp5}"
unset OPENCODE_CONFIG_DIR

# --- 6. V2 plural config and package/options object -------------------------
tmp6="$(mktemp -d)"
cat > "${tmp6}/opencode.json" <<'JSON'
{"plugins":["other",{"package":"token-optimizer-opencode","options":{"dataDir":"/tmp/keep"}},"token-optimizer-opencode"]}
JSON
OPENCODE_CONFIG_DIR="${tmp6}" out="$(uninstall_opencode 2>&1)" || true
remaining="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("plugins"))' "${tmp6}/opencode.json" 2>/dev/null)"
if [ "$remaining" = "['other']" ]; then ok "v2-reverts-package-entries-keeps-others"; else nok "v2-reverts-package-entries-keeps-others" "remaining='$remaining'"; fi
rm -rf "${tmp6}"
unset OPENCODE_CONFIG_DIR

printf '\n%d/%d passed\n' "$pass" "$((pass+fail))"
[ "$fail" -eq 0 ]
