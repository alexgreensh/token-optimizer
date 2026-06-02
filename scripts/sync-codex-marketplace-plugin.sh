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
# Enforcement: scripts/sign-release.sh runs this and aborts the release if the committed
# mirror drifted; tests/test_codex_marketplace_parity.py is the local dev parity check.
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

# --- Exclude dev-only files not needed by the installed Codex skill. benchmark.py is a
#     standalone benchmarking tool whose security test fixtures contain intentionally-fake
#     secret-shaped strings (SLACK_TOKEN=..., GITHUB_TOKEN=ghp_...) that trip GitHub push
#     protection when duplicated. Keep this list in sync with EXCLUDE_NAMES in
#     tests/test_codex_marketplace_parity.py.
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
