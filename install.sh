#!/bin/bash
# Token Optimizer - One-command installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/alexgreensh/token-optimizer/main/install.sh | bash
#
# What it does:
#   1. Checks prerequisites (Python 3.10+, git, ~/.claude/)
#   2. Clones (or updates) the repo into ~/.claude/token-optimizer
#   3. Symlinks the skill into ~/.claude/skills/token-optimizer
#   4. Prints success + usage instructions
#
# Idempotent: safe to run multiple times.
#
# Copyright (C) 2026 Alex Greenshpun
# SPDX-License-Identifier: AGPL-3.0-only

set -euo pipefail

REPO_HTTPS="https://github.com/alexgreensh/token-optimizer.git"
REPO_SSH="git@github.com:alexgreensh/token-optimizer.git"
INSTALL_DIR="${HOME}/.claude/token-optimizer"
SKILL_DIR="${HOME}/.claude/skills"

# ── Colors ────────────────────────────────────────────────────

if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BOLD='' NC=''
fi

info()  { printf "${GREEN}>${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}!${NC} %s\n" "$1"; }
fail()  { printf "${RED}x${NC} %s\n" "$1"; exit 1; }

# ── Prerequisites ─────────────────────────────────────────────

info "Checking prerequisites..."

# Python 3.10+
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.10+ first."
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] 2>/dev/null || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; } 2>/dev/null; then
    fail "Python ${PY_VERSION} found, but 3.10+ is required."
fi
info "Python ${PY_VERSION} OK"

# Git
if ! command -v git &>/dev/null; then
    fail "git not found. Install git first."
fi
info "git OK"

# Claude Code directory
if [ ! -d "${HOME}/.claude" ]; then
    fail "~/.claude/ not found. Install Claude Code first: https://claude.ai/download"
fi
info "~/.claude/ OK"

# ── Clone or Update ───────────────────────────────────────────

clone_repo() {
    if git clone --depth 1 "$REPO_HTTPS" "$INSTALL_DIR" 2>/dev/null; then
        return 0
    fi
    info "HTTPS clone failed, trying SSH..."
    if git clone --depth 1 "$REPO_SSH" "$INSTALL_DIR" 2>/dev/null; then
        return 0
    fi
    fail "Could not clone repository. Check your GitHub access."
}

if [ -d "${INSTALL_DIR}/.git" ]; then
    info "Existing install found. Updating..."
    git -C "$INSTALL_DIR" pull --ff-only || {
        warn "git pull failed. Try: cd ${INSTALL_DIR} && git pull"
        warn "Continuing with existing version."
    }
elif [ -d "$INSTALL_DIR" ]; then
    BACKUP="${INSTALL_DIR}.backup.$(date +%Y%m%d_%H%M%S)"
    warn "Non-git install found at ${INSTALL_DIR}"
    warn "Backing up to ${BACKUP}"
    mv "$INSTALL_DIR" "$BACKUP"
    info "Cloning Token Optimizer..."
    clone_repo
else
    info "Cloning Token Optimizer..."
    clone_repo
fi

# ── Symlink Skill ─────────────────────────────────────────────

mkdir -p "$SKILL_DIR"
SKILL_LINK="${SKILL_DIR}/token-optimizer"

if [ -L "$SKILL_LINK" ]; then
    rm "$SKILL_LINK"
fi

if [ -d "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "/token-optimizer skill directory exists (not a symlink). Skipping."
    warn "To use the repo version, move it: mv ${SKILL_LINK} ${SKILL_LINK}.local"
else
    ln -s "${INSTALL_DIR}/skills/token-optimizer" "$SKILL_LINK"
    info "Linked /token-optimizer skill"
fi

# ── Make Scripts Executable ───────────────────────────────────

chmod +x "${INSTALL_DIR}/scripts/measure.py" 2>/dev/null || true

# ── Summary ───────────────────────────────────────────────────

COMMIT=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")

echo ""
printf "${BOLD}${GREEN}Token Optimizer installed!${NC}\n"
echo ""
echo "  Location:  ${INSTALL_DIR}"
echo "  Commit:    ${COMMIT}"
echo "  Skill:     /token-optimizer"
echo ""
echo "  Measure current overhead:"
echo "    python3 ${INSTALL_DIR}/scripts/measure.py report"
echo ""
echo "  Start a Claude Code session and run:"
echo "    /token-optimizer"
echo ""
echo "  Full docs: https://github.com/alexgreensh/token-optimizer"
echo ""
