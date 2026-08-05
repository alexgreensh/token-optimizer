#!/usr/bin/env bash
# check-mirror-sync.sh
#
# Verifies that plugins/token-optimizer/ (the Codex marketplace mirror) is in
# sync with the canonical repo-root content (skills/, hooks/,
# .codex-plugin/plugin.json). Single owner of the drift check, called by:
#   - CI: .github/workflows/tests.yml (mirror-sync job) on every push/PR
#   - Release: scripts/sign-release.sh preflight
#
# Works by REGENERATING the mirror with sync-codex-marketplace-plugin.sh and
# diffing the result against the committed tree. Because the generator owns
# the intentional divergences (hooks.json async-strip for Codex #83,
# benchmark.py exclusion), a clean diff means "no drift" with no
# hand-maintained exception list.
#
# Side effect: like the release preflight, a drifted tree is left REGENERATED
# in the working directory, so the fix is `git add plugins/token-optimizer`.
set -euo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
  echo "ERROR: run with bash (e.g. \`bash scripts/check-mirror-sync.sh\`), not sh." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

bash "${REPO_ROOT}/scripts/sync-codex-marketplace-plugin.sh" >/dev/null

# `git status --porcelain` (not `diff --quiet`) so untracked files the regen
# produces also count as drift, not just modifications to tracked files.
if [ -n "$(git -C "${REPO_ROOT}" status --porcelain -- plugins/token-optimizer)" ]; then
  echo "ERROR: plugins/token-optimizer/ is out of sync with root skills/ or hooks/." >&2
  echo "Run scripts/sync-codex-marketplace-plugin.sh, commit the result, then retry." >&2
  git -C "${REPO_ROOT}" status --porcelain -- plugins/token-optimizer >&2
  git -C "${REPO_ROOT}" diff --stat -- plugins/token-optimizer >&2 || true
  exit 1
fi

echo "mirror-sync OK: plugins/token-optimizer matches canonical skills/ + hooks/"
