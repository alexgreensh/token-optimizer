#!/bin/bash
# Generates CHECKSUMS.sha256 and attaches it to a GitHub release.
#
# Usage:
#   scripts/sign-release.sh v5.7.2
#
# Prerequisites: gh CLI authenticated, tag already pushed.
#
# Copyright (C) 2026 Alex Greenshpun
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

set -euo pipefail

TAG="${1:-}"

if [ -z "$TAG" ]; then
    echo "Usage: scripts/sign-release.sh <tag>"
    echo "Example: scripts/sign-release.sh v5.7.2"
    exit 1
fi

if ! command -v gh &>/dev/null; then
    echo "Error: gh CLI not found. Install: https://cli.github.com"
    exit 1
fi

if ! gh release view "$TAG" &>/dev/null; then
    echo "Error: release $TAG not found. Create it first:"
    echo "  git tag $TAG && git push origin $TAG"
    echo "  gh release create $TAG --title \"$TAG\" --generate-notes"
    exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
CHECKSUM_FILE="${REPO_ROOT}/CHECKSUMS.sha256"

# Preflight: the Codex marketplace plugin (plugins/token-optimizer/) is a mirror of
# root skills/ + hooks/ + .codex-plugin/. Re-sync and fail the release if the committed
# mirror drifted from canonical root — a stale mirror ships outdated skills to Codex
# users. See scripts/sync-codex-marketplace-plugin.sh and issue #51.
echo "Verifying Codex marketplace plugin parity..."
bash "${REPO_ROOT}/scripts/sync-codex-marketplace-plugin.sh" >/dev/null
if ! git -C "$REPO_ROOT" diff --quiet -- plugins/token-optimizer; then
    echo "Error: plugins/token-optimizer/ is out of sync with root skills/ or hooks/."
    echo "Run scripts/sync-codex-marketplace-plugin.sh, commit the result, re-tag, then retry."
    git -C "$REPO_ROOT" diff --stat -- plugins/token-optimizer
    exit 1
fi
echo "Codex marketplace plugin parity OK."

echo "Generating checksums for installed runtime files..."

HASH_CMD="sha256sum"
if [ "$(uname)" = "Darwin" ]; then
    HASH_CMD="shasum -a 256"
fi

git -C "$REPO_ROOT" ls-files \
    install.sh \
    hooks/ \
    skills/ \
    .claude-plugin/ \
    .codex-plugin/ \
    .codex/ \
    | sort | while read -r rel; do
    f="${REPO_ROOT}/${rel}"
    [ -f "$f" ] || continue
    $HASH_CMD "$f" | sed "s|${REPO_ROOT}/||"
done > "$CHECKSUM_FILE"

CHECKSUM_COUNT=$(wc -l < "$CHECKSUM_FILE" | tr -d ' ')
echo "Generated ${CHECKSUM_COUNT} checksums"

if [ "$CHECKSUM_COUNT" -eq 0 ]; then
    echo "Error: no files found to checksum. Aborting upload."
    exit 1
fi

echo "Uploading CHECKSUMS.sha256 to release $TAG..."
gh release upload "$TAG" "$CHECKSUM_FILE" --clobber

echo ""
echo "Done. Verify with:"
echo "  gh release view $TAG"
