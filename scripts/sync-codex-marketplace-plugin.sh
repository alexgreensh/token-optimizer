#!/usr/bin/env bash
# sync-codex-marketplace-plugin.sh
#
# Regenerates plugins/token-optimizer/ (the Codex marketplace plugin directory)
# from the canonical repo-root content: skills/, hooks/, and .codex-plugin/plugin.json.
#
# WHY THIS EXISTS:
#   Codex CLI 0.136.0 only resolves marketplace plugins that live in a SUBDIRECTORY
#   (./plugins/<name>), not at the repo root. So .agents/plugins/marketplace.json
#   points at ./plugins/token-optimizer via a `local` source. That nested directory
#   must contain REAL content — Codex's install copy does NOT follow symlinks, so a
#   symlinked skills/ installs as an empty plugin. The canonical source stays at the
#   repo root (consumed by install.sh / Claude Code); this script mirrors it into the
#   nested Codex plugin dir.
#
# WHY hooks/ IS INCLUDED:
#   The installed skill's own setup scripts (skills/token-optimizer/scripts/
#   codex_install.py and codex_doctor.py) locate hooks via `Path(__file__).parents[3]`
#   = the plugin root, e.g. `<plugin_root>/hooks/python-launcher.sh`. If hooks/ is not
#   shipped at the nested plugin root, Codex hook setup resolves to a missing path and
#   silently breaks. So hooks/ ships alongside skills/.
#
# Run before any release that touches skills/, hooks/, or the Codex plugin version.
# Enforcement: scripts/sign-release.sh regenerates the mirror and `git diff --quiet --
# plugins/token-optimizer`, aborting the release if the committed mirror drifted. Run this
# script locally to refresh the mirror before committing.
# Idempotent: running on an in-sync tree produces no git diff.
set -euo pipefail

# --- Refuse non-bash shells: under `sh script`, BASH_SOURCE is unset and REPO_ROOT
#     would mis-resolve, sending the rm below at the wrong tree. Fail loudly instead.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "ERROR: run with bash (e.g. \`bash scripts/sync-codex-marketplace-plugin.sh\`), not sh." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Sanity-guard REPO_ROOT before any destructive op. Require the markers that only
#     the real repo root has. If any is missing, abort WITHOUT deleting anything.
for marker in ".agents/plugins/marketplace.json" ".codex-plugin/plugin.json" "skills" "hooks"; do
  if [ ! -e "${REPO_ROOT}/${marker}" ]; then
    echo "ERROR: REPO_ROOT looks wrong (missing ${marker}): ${REPO_ROOT}" >&2
    echo "Refusing to run destructive sync." >&2
    exit 3
  fi
done

NESTED="${REPO_ROOT}/plugins/token-optimizer"
STAGE="${REPO_ROOT}/plugins/.token-optimizer.stage.$$"

cleanup_stage() { rm -rf "${STAGE}" 2>/dev/null || true; }
trap cleanup_stage EXIT

# --- Build into a staging dir, then atomically swap. A failure mid-build leaves the
#     existing nested dir untouched (no half-synced/empty plugin gets shipped).
rm -rf "${STAGE}"
mkdir -p "${STAGE}/.codex-plugin"

cp -R "${REPO_ROOT}/skills" "${STAGE}/skills"
cp -R "${REPO_ROOT}/hooks" "${STAGE}/hooks"
cp "${REPO_ROOT}/.codex-plugin/plugin.json" "${STAGE}/.codex-plugin/plugin.json"

# --- Strip build/OS junk so it never ships or causes parity flakiness.
find "${STAGE}" \( -name '__pycache__' -o -name '.DS_Store' -o -name '*.pyc' -o -name '*.pyo' \) \
  -exec rm -rf {} + 2>/dev/null || true

# --- Codex compatibility. Two transforms applied to the mirrored hooks.json;
#     the root hooks/hooks.json keeps its Claude-tuned values (the parity test
#     normalizes both the same way):
#
#     Codex warns and SKIPS any hook with "async": true ("async hooks
#       are not supported yet"), so those hooks never run for Codex marketplace
#       users. Strip the async flag so Codex runs them synchronously. Claude keeps
#       "async": true for its non-blocking path.
#
#     (SessionEnd clamp) Codex hard-caps SessionEnd hook timeouts at 3s and prints
#       "clamping SessionEnd hook timeout to 3s in .../hooks.json" as a load-time
#       ISSUE whenever a SessionEnd hook declares more. Our SessionEnd hook runs
#       `session-end-flush --defer`, which only spawns a detached worker and
#       returns in well under a second, so the 60s Claude value is pure headroom
#       Codex can never honor. Pre-clamp SessionEnd timeouts to 3 in the mirror so
#       Codex loads it clean (no user-facing warning) with identical behavior.
if [ -f "${STAGE}/hooks/hooks.json" ]; then
  python3 - "${STAGE}/hooks/hooks.json" <<'PYSTRIP' || { echo "ERROR: failed to apply Codex hooks.json transforms" >&2; exit 4; }
import json, sys
# Codex's hard cap for SessionEnd hook timeouts (seconds). Anything above this is
# clamped by Codex and surfaced as a load-time issue, so we pre-clamp to match.
CODEX_SESSION_END_TIMEOUT_CAP = 3
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    data = json.load(f)
def strip_async(o):
    if isinstance(o, dict):
        o.pop("async", None)
        for v in o.values():
            strip_async(v)
    elif isinstance(o, list):
        for v in o:
            strip_async(v)
strip_async(data)
# The desktop status bar is a Claude Code hooks module; the mirror ships no
# desktop/ folder and Codex has no hooks modules.
data.pop("modules", None)
# Clamp SessionEnd timeouts only (the event Codex caps at 3s). Other events keep
# their declared timeouts; Codex does not warn on those.
for group in data.get("hooks", {}).get("SessionEnd", []):
    for hook in group.get("hooks", []):
        t = hook.get("timeout")
        if isinstance(t, (int, float)) and t > CODEX_SESSION_END_TIMEOUT_CAP:
            hook["timeout"] = CODEX_SESSION_END_TIMEOUT_CAP
# (Codex mid-session self-heal) Rewrite the fail-open launcher guard into a
# runtime version-resolver: prefer ${CLAUDE_PLUGIN_ROOT}, else scan the parent dir
# for the newest installed version. Codex pins ${CLAUDE_PLUGIN_ROOT} to the
# session's plugin version, so on a mid-session refresh the old dir is already
# gone; scanning for the newest heals without a restart. Since 5.13.51 the root
# hooks.json ships the same resolver (a Claude Code session's root can be deleted
# under it too), so this rewrite is a no-op for current sources and only matters
# for an older guard-style hooks.json. _hook_command_identity strips the resolver.
_GUARD = ('L="${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh"; [ -r "$L" ] || exit 0; '
          'exec "$b" "$L" "${CLAUDE_PLUGIN_ROOT}/hooks/run.py"')
_RESOLVER = ('D="${CLAUDE_PLUGIN_ROOT}"; [ -r "$D/hooks/python-launcher.sh" ] || '
             'D="$(ls -d "${D%[/\\\\]*}"/*/ 2>/dev/null | grep -E "/[0-9]+[.][0-9]+[.][0-9]+/$" | sort -V | tail -n1)"; '
             'D="${D%/}"; [ -r "$D/hooks/python-launcher.sh" ] || exit 0; '
             'exec "$b" "$D/hooks/python-launcher.sh" "$D/hooks/run.py"')
def resolverize(o):
    if isinstance(o, dict):
        c = o.get("command")
        if isinstance(c, str) and _GUARD in c:
            o["command"] = c.replace(_GUARD, _RESOLVER)
        for v in o.values():
            resolverize(v)
    elif isinstance(o, list):
        for v in o:
            resolverize(v)
resolverize(data)
with open(p, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYSTRIP
fi

# --- Exclude dev-only files not needed by the installed Codex skill. benchmark.py is a
#     standalone benchmarking tool whose security test fixtures contain intentionally-fake
#     secret-shaped strings (SLACK_TOKEN=..., GITHUB_TOKEN=ghp_...) that trip GitHub push
#     protection when duplicated. This exclude list is maintained here; the release gate
#     (scripts/sign-release.sh) regenerates from it, so a drifted mirror cannot ship.
find "${STAGE}" -name 'benchmark.py' -exec rm -f {} + 2>/dev/null || true

# --- Verify the staged result BEFORE swapping. Empty/partial plugin must never ship.
skill_dirs=$(find "${STAGE}/skills" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
[ "${skill_dirs}" -ge 4 ] || { echo "ERROR: expected >=4 skill dirs, got ${skill_dirs}" >&2; exit 4; }
[ -f "${STAGE}/.codex-plugin/plugin.json" ] || { echo "ERROR: plugin.json missing after copy" >&2; exit 4; }
[ -f "${STAGE}/hooks/python-launcher.sh" ] || { echo "ERROR: hooks/python-launcher.sh missing after copy" >&2; exit 4; }
[ -f "${STAGE}/hooks/run.py" ] || { echo "ERROR: hooks/run.py missing after copy" >&2; exit 4; }

# --- Atomic swap.
rm -rf "${NESTED}"
mkdir -p "$(dirname "${NESTED}")"
mv "${STAGE}" "${NESTED}"

echo "Synced Codex marketplace plugin -> plugins/token-optimizer"
echo "  skills:      ${skill_dirs} dirs"
echo "  hooks:       present (python-launcher.sh, run.py)"
echo "  plugin.json: $(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "${NESTED}/.codex-plugin/plugin.json" | head -1)"
