#!/bin/bash
# Token Optimizer - One-command installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/alexgreensh/token-optimizer/main/install.sh | bash
#
# What it does:
#   1. Checks prerequisites (Python 3.8+, git, ~/.claude/)
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

# Python 3.8+
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.8+ first."
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] 2>/dev/null || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; } 2>/dev/null; then
    fail "Python ${PY_VERSION} found, but 3.8+ is required."
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

# ── Plugin Conflict Check ────────────────────────────────────

if [ -d "${HOME}/.claude/plugins/cache" ]; then
    if find "${HOME}/.claude/plugins/cache" -name "plugin.json" -exec grep -l '"name"[[:space:]]*:[[:space:]]*"token-optimizer"' {} \; 2>/dev/null | head -1 | grep -q .; then
        warn "Token Optimizer is already installed as a Claude Code plugin."
        warn "The script installer creates a skill symlink, which would duplicate the plugin."
        warn "If you want the script version instead, first uninstall the plugin:"
        warn "  /plugin uninstall token-optimizer@alexgreensh-token-optimizer"
        echo ""
        if [ -t 0 ] || [ -e /dev/tty ]; then
            printf "Continue anyway? (y/N) "
            read -r confirm < /dev/tty
            [ "$confirm" = "y" ] || [ "$confirm" = "Y" ] || exit 0
        else
            warn "Non-interactive mode detected. Skipping (use plugin install instead)."
            exit 0
        fi
    fi
fi

# ── Clone or Update ───────────────────────────────────────────

clone_repo() {
    local clone_log="/tmp/token-optimizer-clone-$$.log"

    # Sparse checkout: only pull Claude Code files, skip OpenClaw platform files
    try_clone() {
        local url="$1"
        git clone --depth 1 --filter=blob:none --sparse "$url" "$INSTALL_DIR" 2>"$clone_log" || return 1
        git -C "$INSTALL_DIR" sparse-checkout set \
            skills/ hooks/ .claude-plugin/ \
            install.sh README.md LICENSE NOTICE PRIVACY.md \
            2>>"$clone_log" || true
    }

    if try_clone "$REPO_HTTPS"; then
        rm -f "$clone_log"
        return 0
    fi
    warn "HTTPS clone failed. Details: $(cat "$clone_log" 2>/dev/null)"
    rm -rf "$INSTALL_DIR"
    info "Trying SSH..."
    if try_clone "$REPO_SSH"; then
        rm -f "$clone_log"
        return 0
    fi
    warn "SSH clone also failed. Details: $(cat "$clone_log" 2>/dev/null)"
    rm -f "$clone_log"
    rm -rf "$INSTALL_DIR"
    fail "Could not clone repository. Check network connectivity and GitHub access."
}

if [ -d "${INSTALL_DIR}/.git" ]; then
    info "Existing install found. Updating..."

    # Enable sparse checkout on existing installs (migrates full clones)
    if ! git -C "$INSTALL_DIR" sparse-checkout list &>/dev/null || \
       git -C "$INSTALL_DIR" sparse-checkout list 2>/dev/null | grep -q "^/$"; then
        info "Migrating to sparse checkout (removing OpenClaw files)..."
        git -C "$INSTALL_DIR" sparse-checkout init --cone 2>/dev/null || true
        git -C "$INSTALL_DIR" sparse-checkout set \
            skills/ hooks/ .claude-plugin/ \
            install.sh README.md LICENSE NOTICE PRIVACY.md \
            2>/dev/null || true
    fi

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

if [ -d "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "/token-optimizer skill directory exists (not a symlink). Skipping."
    warn "To use the repo version, move it: mv ${SKILL_LINK} ${SKILL_LINK}.local"
elif [ -f "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "Regular file exists at ${SKILL_LINK}. Moving to ${SKILL_LINK}.bak"
    mv "$SKILL_LINK" "${SKILL_LINK}.bak"
    ln -sf "${INSTALL_DIR}/skills/token-optimizer" "$SKILL_LINK"
    info "Linked /token-optimizer skill"
else
    ln -sf "${INSTALL_DIR}/skills/token-optimizer" "$SKILL_LINK"
    info "Linked /token-optimizer skill"
fi

# ── Make Scripts Executable ───────────────────────────────────

chmod +x "${INSTALL_DIR}/skills/token-optimizer/scripts/measure.py" 2>/dev/null || true

# ── Setup Quality Bar (auto-install cache hook + status line) ─

info "Setting up quality bar..."
if python3 "${INSTALL_DIR}/skills/token-optimizer/scripts/measure.py" setup-quality-bar 2>/dev/null; then
    info "Quality bar installed (status line + cache hook)"
else
    warn "Could not auto-install quality bar. Run manually in Claude Code:"
    warn "  python3 measure.py setup-quality-bar"
fi

# ── Summary ───────────────────────────────────────────────────

COMMIT=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")

echo ""
printf "${BOLD}${GREEN}Token Optimizer installed!${NC}\n"
echo ""
echo "  Location:  ${INSTALL_DIR}"
echo "  Commit:    ${COMMIT}"
echo "  Skill:     /token-optimizer"
echo "  Quality:   ContextQ score in status line (updates every ~2 min)"
echo ""
echo "  Measure current overhead:"
echo "    python3 ${INSTALL_DIR}/skills/token-optimizer/scripts/measure.py report"
echo ""
echo "  Start a Claude Code session and run:"
echo "    /token-optimizer"
echo ""
echo "  Full docs: https://github.com/alexgreensh/token-optimizer"
echo ""
