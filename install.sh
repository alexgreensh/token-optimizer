#!/bin/bash
# Token Optimizer - One-command installer
#
# Usage:
#   git clone https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
#   bash ~/.claude/token-optimizer/install.sh
#
# What it does:
#   1. Checks prerequisites (Python 3.9+, git, ~/.claude/)
#   2. Clones (or updates) the repo into ~/.claude/token-optimizer
#   3. Symlinks the skill into ~/.claude/skills/token-optimizer
#   4. Prints success + usage instructions
#
# Idempotent: safe to run multiple times.
#
# Copyright (C) 2026 Alex Greenshpun
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

set -euo pipefail

GITHUB_REPO="alexgreensh/token-optimizer"
REPO_HTTPS="https://github.com/${GITHUB_REPO}.git"
REPO_SSH="git@github.com:${GITHUB_REPO}.git"
# Honor a relocated Claude home (matches runtime_env.py:380-402 which reads
# CLAUDE_CONFIG_DIR). OpenCode users without Claude Code, or anyone pointing
# Claude at a non-default home, get the skill tree installed in the right
# place instead of hard-failing on a missing ~/.claude.
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
INSTALL_DIR="${CLAUDE_HOME}/token-optimizer"
SKILL_DIR="${CLAUDE_HOME}/skills"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/token-optimizer.XXXXXX")"
CHECKSUM_FILE="${TMP_DIR}/CHECKSUMS.sha256"
RELEASE_TAG=""
CHECKSUM_ASSET_URL=""
INSTALL_OLD_HEAD=""
INSTALL_UPDATED=0
VERIFIED_RELEASE_HEAD=""
trap 'rm -rf "$TMP_DIR"' EXIT

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

# ── OpenCode local-dir install (no npm) ───────────────────────
# `install.sh --opencode` builds the TypeScript plugin and drops a single
# bundled file into ~/.config/opencode/plugins/, which OpenCode auto-loads
# at startup. This is the offline / no-npm fallback to:
#     opencode plugin token-optimizer-opencode
# It needs bun (OpenCode's own runtime) and a checkout of this repo.

install_opencode() {
    command -v bun &>/dev/null || fail "bun not found. OpenCode runs on bun; install it first: https://bun.sh"

    # Locate the opencode/ source relative to this script.
    local script_dir oc_src
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    oc_src="${script_dir}/opencode"

    # If the source isn't present (e.g. a sparse Claude Code checkout), try to
    # add it to the sparse-checkout cone and pull it.
    if [ ! -d "$oc_src" ] && [ -d "${script_dir}/.git" ]; then
        warn "opencode/ not in this checkout. Adding it to sparse-checkout..."
        git -C "$script_dir" sparse-checkout add opencode/ 2>/dev/null || true
        git -C "$script_dir" pull --ff-only 2>/dev/null || true
    fi
    [ -d "$oc_src" ] || fail "opencode/ source not found. Clone the full repo: git clone ${REPO_HTTPS}"

    # Integrity: this path builds from your local clone, so the trust anchor is
    # the checkout itself. Surface the commit and flag a dirty tree so a tampered
    # or modified source tree is visible before it gets auto-loaded by OpenCode.
    # (npm is the cryptographically-verified channel; see the note below.)
    if [ -d "${script_dir}/.git" ]; then
        local oc_sha oc_dirty
        oc_sha="$(git -C "$script_dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
        info "Building from commit ${oc_sha}"
        oc_dirty="$(git -C "$script_dir" status --porcelain -- opencode/ 2>/dev/null)"
        if [ -n "$oc_dirty" ]; then
            warn "opencode/ has uncommitted local changes — building modified source:"
            printf '%s\n' "$oc_dirty" | sed 's/^/    /'
            if [ -e /dev/tty ]; then
                printf "Continue building this modified tree? (y/N) "
                read -r oc_confirm < /dev/tty
                [ "$oc_confirm" = "y" ] || [ "$oc_confirm" = "Y" ] || fail "Aborted by user."
            fi
        fi
    else
        warn "Not a git checkout — cannot verify source provenance. For a verified install use npm: opencode plugin token-optimizer-opencode"
    fi

    info "Installing OpenCode dependencies (bun install)..."
    # --frozen-lockfile: install exactly what bun.lock pins, no silent drift to a
    # newer (untested) transitive version at install time.
    if ! ( cd "$oc_src" && bun install --frozen-lockfile --silent ); then
        fail "bun install failed in ${oc_src} (lockfile out of sync? run 'bun install' in opencode/)."
    fi

    info "Building plugin bundle..."
    if ! ( cd "$oc_src" && bun run build:bundle ); then
        fail "Plugin bundle build failed."
    fi

    local bundle="${oc_src}/dist-bundle/token-optimizer.js"
    [ -f "$bundle" ] || fail "Bundle not produced at ${bundle}"

    # WSL-root wrong-home warning (issue #78, generalized): if running as root
    # under WSL without OPENCODE_CONFIG_DIR, the plugin lands in
    # /root/.config/opencode which the Windows OpenCode CLI never reads.
    _wsl_root_wrong_home_warning "OpenCode" ".config/opencode" "OPENCODE_CONFIG_DIR" "--opencode"

    local plugin_dir="${HOME}/.config/opencode/plugins"
    mkdir -p "$plugin_dir"
    cp "$bundle" "${plugin_dir}/token-optimizer.js"
    info "Installed to ${plugin_dir}/token-optimizer.js"

    echo ""
    printf "${BOLD}${GREEN}Token Optimizer for OpenCode installed!${NC}\n"
    echo ""
    echo "  Plugin:    ${plugin_dir}/token-optimizer.js (auto-loaded by OpenCode)"
    echo "  Tools:     token_status, token_dashboard"
    echo ""
    echo "  Start OpenCode and ask: \"run token_status\""
    echo "  Re-run this command after a git pull to update."
    echo ""
    echo "  Prefer npm? Once published:  opencode plugin token-optimizer-opencode"
    echo ""
    exit 0
}

# ── Hermes plugin install ─────────────────────────────────────
# `install.sh --hermes` installs the Token Optimizer plugin into
# ~/.hermes/plugins/token-optimizer/, which NousResearch Hermes auto-loads.
# Beta. Needs python3 and a checkout of this repo. Extra args (e.g. --dry-run,
# --uninstall) are forwarded to the underlying hermes-install command.
install_hermes() {
    command -v python3 &>/dev/null || fail "python3 not found. Token Optimizer for Hermes needs Python 3."

    local script_dir measure_py
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    measure_py="${script_dir}/skills/token-optimizer/scripts/measure.py"

    if [ ! -f "$measure_py" ] && [ -d "${script_dir}/.git" ]; then
        warn "skills/ not in this checkout. Adding it to sparse-checkout..."
        git -C "$script_dir" sparse-checkout add skills/ hermes/ 2>/dev/null || true
        git -C "$script_dir" pull --ff-only 2>/dev/null || true
    fi
    [ -f "$measure_py" ] || fail "$(printf 'Run install.sh from inside a token-optimizer checkout, not on its own. From any folder:\n    git clone --depth 1 %s\n    cd token-optimizer\n    bash install.sh --hermes' "$REPO_HTTPS")"

    if [ -d "${script_dir}/.git" ]; then
        local h_sha
        h_sha="$(git -C "$script_dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
        info "Installing from commit ${h_sha}"
    else
        warn "Not a git checkout — cannot verify source provenance."
    fi

    # Forward any extra flags (--dry-run, --uninstall, --json) after --hermes.
    local extra=()
    for a in "$@"; do [ "$a" = "--hermes" ] || extra+=("$a"); done

    # WSL-root wrong-home warning (issue #78, generalized): if running as root
    # under WSL without HERMES_HOME, the plugin lands in /root/.hermes which
    # the Windows Hermes CLI never reads.
    _wsl_root_wrong_home_warning "Hermes" ".hermes" "HERMES_HOME" "--hermes"

    info "Installing Token Optimizer into Hermes (~/.hermes/plugins/token-optimizer/)..."
    if ! python3 "$measure_py" hermes-install "${extra[@]+"${extra[@]}"}"; then
        fail "Hermes install failed."
    fi

    echo ""
    printf "${BOLD}${GREEN}Token Optimizer for Hermes installed (beta)!${NC}\n"
    echo ""
    echo "  Plugin:    ~/.hermes/plugins/token-optimizer/ (auto-loaded by Hermes)"
    echo "  Verify:    python3 ${measure_py} hermes-doctor"
    echo "  In Hermes: /token-optimizer  -  hermes token-optimizer (dashboard :24844)"
    echo "  Re-run this command after a git pull to update."
    echo ""
    exit 0
}

# ── WSL-root wrong-home warning (issue #78, generalized cross-platform) ─
# `bash install.sh` (or --opencode / --hermes / --copilot) on native Windows
# runs WSL bash as root, so $HOME=/root and the install lands in /root/<subpath>
# which the Windows-native CLI never reads (it reads %USERPROFILE%\<subpath> =
# /mnt/c/Users/<you>/<subpath>). Detect this and WARN loudly. Does NOT auto-write
# to a guessed Windows home — a wrong autodetect that silently writes to a
# guessed profile is worse than a clear warning. Warn + suggest the per-target
# home env var; the user opts in.
#
# Args (all required):
#   $1 target_label  : human label ("Copilot", "OpenCode", "Hermes", "Claude Code")
#   $2 target_subpath: home-relative subdir (".copilot", ".config/opencode",
#                      ".hermes", ".claude")
#   $3 home_env_var  : the env var that overrides the home ("COPILOT_HOME",
#                      "OPENCODE_CONFIG_DIR", "HERMES_HOME", "CLAUDE_CONFIG_DIR")
#   $4 install_flag  : the install.sh flag to re-run with ("--copilot",
#                      "--opencode", "--hermes", or "" for the Claude default)
_wsl_root_wrong_home_warning() {
    local target_label="$1" target_subpath="$2" home_env_var="$3" install_flag="$4"

    # If the user already set the per-target home env var, they have opted in.
    # bash indirect expansion (${!var}) is bash 3.2+ compatible and avoids eval.
    [ -n "${!home_env_var:-}" ] && return 0

    # Overridable paths so tests can mock /proc/version and /mnt/c/Users
    # without touching the real filesystem. Defaults are the production paths.
    local proc_version_file="${_TO_PROC_VERSION:-/proc/version}"
    local wsl_users_dir="${_TO_WSL_USERS_DIR:-/mnt/c/Users}"

    local is_wsl=0 is_root=0

    # WSL detection: WSL_DISTRO_NAME set, or /proc/version mentions microsoft/WSL.
    if [ -n "${WSL_DISTRO_NAME:-}" ]; then
        is_wsl=1
    elif [ -r "$proc_version_file" ] && grep -qiE 'microsoft|wsl' "$proc_version_file" 2>/dev/null; then
        is_wsl=1
    fi
    [ "$is_wsl" = "1" ] || return 0

    # Root detection: uid 0 or $HOME is /root.
    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
        is_root=1
    elif [ "${HOME:-}" = "/root" ]; then
        is_root=1
    fi
    [ "$is_root" = "1" ] || return 0

    warn "Running as root under WSL — ${target_label} files will land in \${HOME}/${target_subpath} = ${HOME}/${target_subpath}"
    warn "The Windows ${target_label} CLI reads from %USERPROFILE%\\${target_subpath} (e.g. /mnt/c/Users/<you>/${target_subpath}), NOT /root/${target_subpath}."

    # Look for a plausible Windows user profile under /mnt/c/Users/.
    # Skip Windows system/default/public profiles.
    local win_user="" win_count=0 entry name
    if [ -d "$wsl_users_dir" ]; then
        for entry in "${wsl_users_dir}"/*/; do
            [ -d "$entry" ] || continue
            name="$(basename "$entry")"
            case "$name" in
                Public|"All Users"|Default|"Default User"|Windows|WpSystem) continue ;;
            esac
            [ -n "$name" ] || continue
            win_user="$name"
            win_count=$((win_count + 1))
        done
    fi

    # Build the re-run command, appending the install flag only when non-empty
    # (the Claude default install takes no flag).
    local install_cmd="bash install.sh"
    [ -n "$install_flag" ] && install_cmd="$install_cmd $install_flag"

    if [ "$win_count" = "1" ]; then
        warn "Detected one Windows user profile. Re-run with:"
        warn "  ${home_env_var}=/mnt/c/Users/${win_user}/${target_subpath} ${install_cmd}"
    elif [ "$win_count" -gt 1 ]; then
        warn "Multiple Windows user profiles found under /mnt/c/Users/. Set ${home_env_var} to yours, e.g.:"
        warn "  ${home_env_var}=/mnt/c/Users/<your-windows-user>/${target_subpath} ${install_cmd}"
    else
        warn "Set ${home_env_var} to your Windows ${target_label} home, e.g.:"
        warn "  ${home_env_var}=/mnt/c/Users/<your-windows-user>/${target_subpath} ${install_cmd}"
    fi
    warn "Or run from your WSL user (not root), or from a Windows-native shell."
}

# Back-compat wrapper (issue #78 original name). Tests source this for the
# Copilot-specific warning; kept so the existing test_copilot_wsl_root.sh
# still passes unchanged. Delegates to the generalized function with the
# Copilot target parameters.
_copilot_wsl_root_warning() {
    _wsl_root_wrong_home_warning "Copilot" ".copilot" "COPILOT_HOME" "--copilot"
}

# Recover a home override the user set in the WINDOWS environment but which WSL
# bash never inherited (issue #78). WSL does not import Windows env vars unless
# they are listed in WSLENV, so a user who sets COPILOT_HOME in PowerShell / the
# System env and then runs `bash install.sh --copilot` sees it UNSET inside the
# script — the install falls back to /root/.copilot and the Windows Copilot CLI
# never reads the hooks. Here we ask Windows for the value via cmd.exe and
# translate the Windows path (C:\Users\You\.copilot) to its WSL mount form
# (/mnt/c/Users/You/.copilot), then export it so the normal resolver + banner
# pick it up. No-op off WSL, when the var is already set, when cmd.exe is
# unavailable, or when Windows has no such value.
#   $1 home_env_var : the env var to recover (e.g. "COPILOT_HOME")
_recover_home_from_windows_env() {
    local home_env_var="$1"

    # Already set in this shell → the user opted in correctly; nothing to do.
    [ -n "${!home_env_var:-}" ] && return 0

    # Only meaningful under WSL (mock /proc/version via _TO_PROC_VERSION in tests).
    local proc_version_file="${_TO_PROC_VERSION:-/proc/version}"
    if [ -z "${WSL_DISTRO_NAME:-}" ]; then
        { [ -r "$proc_version_file" ] && grep -qiE 'microsoft|wsl' "$proc_version_file" 2>/dev/null; } || return 0
    fi

    # Gate to the ROOT-under-WSL case only — the exact wrong-home scenario (#78):
    # `bash install.sh` launched from a Windows shell runs WSL as root, so
    # $HOME=/root and the install lands where the Windows Copilot CLI can't read
    # it. A NON-root WSL user has a legitimate $HOME, so ~/.copilot is correct and
    # we must NOT redirect them to a Windows profile. This also confines the
    # cmd.exe call (and its hang risk) to the narrow case that needs it.
    local is_root=0
    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
        is_root=1
    elif [ "${HOME:-}" = "/root" ]; then
        is_root=1
    fi
    [ "$is_root" = "1" ] || return 0

    # Locate cmd.exe (override via _TO_CMD_EXE for tests). Absent on locked-down
    # or non-Windows hosts → silently skip and let the warning path guide the user.
    local cmd_exe="${_TO_CMD_EXE:-/mnt/c/Windows/System32/cmd.exe}"
    [ -x "$cmd_exe" ] || return 0

    # Query Windows. cmd.exe strips \r via tr; echoes the literal "%VAR%" when
    # the variable is unset on the Windows side.
    #
    # Guard with set +e (same pattern as the #57 hook-install guard below): under
    # `set -euo pipefail` a failing command substitution in a plain assignment is
    # NOT exempt from set -e, and with pipefail the pipeline inherits cmd.exe's
    # exit status. So if cmd.exe is present but exits non-zero — WSL interop
    # disabled via /etc/wsl.conf [interop], or an Exec format error under a
    # constrained container — the bare `raw=$(...)` would abort the ENTIRE
    # installer instead of degrading to the warning path this function promises.
    #
    # Wrap in `timeout` when available: a cmd.exe that hangs (Defender first-scan,
    # a corporate cmd.exe AutoRun, WSL-interop cold start) would otherwise block
    # the installer indefinitely with no output. A timeout is treated like a
    # non-zero exit → skip recovery and fall through to the warning path.
    local raw win_val cmd_status timeout_bin=""
    command -v timeout >/dev/null 2>&1 && timeout_bin="timeout 5"
    set +e
    raw="$(${timeout_bin} "$cmd_exe" /c "echo %${home_env_var}%" 2>/dev/null | tr -d '\r')"
    cmd_status=$?
    set -e
    [ "$cmd_status" -eq 0 ] || return 0
    win_val="$(printf '%s' "$raw" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$win_val" ] || return 0
    [ "$win_val" = "%${home_env_var}%" ] && return 0   # unset on Windows too

    # Translate a Windows drive path to its WSL mount form; accept an already-WSL
    # path as-is. Anything else (UNC, relative, exotic) is left unrecovered so the
    # warning path can guide the user rather than exporting a bogus home.
    local translated=""
    case "$win_val" in
        [A-Za-z]:\\*|[A-Za-z]:/*)
            local drive rest
            drive="$(printf '%s' "$win_val" | cut -c1 | tr '[:upper:]' '[:lower:]')"
            rest="$(printf '%s' "$win_val" | cut -c3- | tr '\\' '/')"
            translated="/mnt/${drive}${rest}"
            ;;
        /mnt/*)
            translated="$win_val"
            ;;
        *)
            return 0
            ;;
    esac

    export "${home_env_var}=${translated}"
    info "Recovered ${home_env_var} from the Windows environment: ${translated}"
}

install_copilot() {
    command -v python3 &>/dev/null || fail "python3 not found. Token Optimizer for GitHub Copilot needs Python 3."

    local script_dir measure_py
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    measure_py="${script_dir}/skills/token-optimizer/scripts/measure.py"

    if [ ! -f "$measure_py" ] && [ -d "${script_dir}/.git" ]; then
        warn "skills/ not in this checkout. Adding it to sparse-checkout..."
        git -C "$script_dir" sparse-checkout add skills/ copilot/ 2>/dev/null || true
        git -C "$script_dir" pull --ff-only 2>/dev/null || true
    fi
    [ -f "$measure_py" ] || fail "$(printf 'Run install.sh from inside a token-optimizer checkout, not on its own. From any folder:\n    git clone --depth 1 %s\n    cd token-optimizer\n    bash install.sh --copilot' "$REPO_HTTPS")"

    if [ -d "${script_dir}/.git" ]; then
        local c_sha
        c_sha="$(git -C "$script_dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
        info "Installing from commit ${c_sha}"
    else
        warn "Not a git checkout — cannot verify source provenance."
    fi

    # Forward only recognized flags after --copilot (an unknown flag like
    # --hermes would otherwise reach argparse and fail the install with a
    # confusing "invalid choice" error).
    local extra=()
    for a in "$@"; do
        case "$a" in
            --dry-run) extra+=("$a") ;;
        esac
    done

    # WSL-root wrong-home recovery + warning (issue #78). First try to recover a
    # COPILOT_HOME the user set in the Windows environment but that WSL bash never
    # inherited (the most common failure: set in PowerShell, invisible to `bash
    # install.sh`). If recovery succeeds, COPILOT_HOME is now exported and the
    # warning below self-suppresses. If not, warn so the user can Ctrl-C and
    # re-run with COPILOT_HOME set inline.
    _recover_home_from_windows_env COPILOT_HOME
    _copilot_wsl_root_warning

    # Resolve the Copilot home via measure.py so the banner shows the TRUE
    # hook destination. measure.py copilot-home applies a WSL-aware /mnt/
    # exception (issue #78): under WSL root, $HOME=/root, so the strict
    # runtime_env._is_safe_home_dir guard would reject a
    # COPILOT_HOME=/mnt/c/Users/<you>/.copilot path (not under /root). The
    # WSL-aware resolver accepts /mnt/ paths as a deliberate cross-filesystem
    # opt-in. We forward the resolved path to copilot-install via --home so
    # the install and the banner agree (otherwise the install would still
    # write to /root/.copilot while the banner showed the /mnt/c/... path).
    # Falls back to ~/.copilot if the query fails.
    local resolved_copilot_home
    resolved_copilot_home="$(TOKEN_OPTIMIZER_RUNTIME=copilot python3 "$measure_py" copilot-home 2>/dev/null || true)"
    [ -n "$resolved_copilot_home" ] || resolved_copilot_home="${HOME}/.copilot"

    info "Installing Token Optimizer into GitHub Copilot CLI (${resolved_copilot_home})..."
    if ! TOKEN_OPTIMIZER_RUNTIME=copilot python3 "$measure_py" copilot-install --home "$resolved_copilot_home" "${extra[@]+"${extra[@]}"}"; then
        fail "Copilot install failed."
    fi

    echo ""
    printf "${BOLD}${GREEN}Token Optimizer for GitHub Copilot installed (beta)!${NC}\n"
    echo ""
    echo "  Hooks:    ${resolved_copilot_home}/hooks/token-optimizer.json (loaded by the Copilot CLI)"
    echo "  Verify:   TOKEN_OPTIMIZER_RUNTIME=copilot python3 ${measure_py} copilot-doctor"
    echo "  Summary:  TOKEN_OPTIMIZER_RUNTIME=copilot python3 ${measure_py} copilot-summary"
    echo "  VS Code:  enable both github.copilot.chat.agentDebugLog settings for per-request credit costs"
    echo "  Re-run this command after a git pull to update."
    echo ""
    exit 0
}

# Route --opencode / --hermes / --copilot before the Claude Code prerequisite
# checks (OpenCode needs bun; Hermes and Copilot need python3, not the Claude
# Code plugin env).

# Allow tests to source this script for function unit-testing (e.g.
# _copilot_wsl_root_warning) without triggering the install flow or
# prerequisite checks. Set _TO_INSTALL_SH_TEST_MODE=1 before sourcing.
if [ "${_TO_INSTALL_SH_TEST_MODE:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

for arg in "$@"; do
    case "$arg" in
        --opencode) install_opencode ;;
        --hermes) install_hermes "$@" ;;
        --copilot) install_copilot "$@" ;;
    esac
done

# ── Prerequisites ─────────────────────────────────────────────

info "Checking prerequisites..."

# Python 3.9+
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.9+ first."
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] 2>/dev/null || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; } 2>/dev/null; then
    fail "Python ${PY_VERSION} found, but 3.9+ is required."
fi
info "Python ${PY_VERSION} OK"

# Git
if ! command -v git &>/dev/null; then
    fail "git not found. Install git first."
fi
info "git OK"

# curl (needed for out-of-band checksum verification)
if ! command -v curl &>/dev/null; then
    fail "curl not found. Install curl first."
fi

# WSL-root wrong-home warning (issue #78, generalized): if running as root
# under WSL without CLAUDE_CONFIG_DIR, the skill tree + settings land in
# /root/.claude which the Windows Claude Code CLI never reads (it reads
# %USERPROFILE%\.claude = /mnt/c/Users/<you>/.claude). Warn BEFORE creating
# $CLAUDE_HOME so the user can Ctrl-C and re-run with CLAUDE_CONFIG_DIR set.
_wsl_root_wrong_home_warning "Claude Code" ".claude" "CLAUDE_CONFIG_DIR" ""

# Claude home directory (skill-tree home; Claude Code creates this on first
# run, and OpenCode loads $CLAUDE_HOME/skills directly). Don't hard-fail if
# it's absent — create it so the shared skill tree has somewhere to live.
# OpenCode users without Claude Code, or anyone following opencode/README's
# "run the standard installer to refresh the skill tree" instruction, would
# otherwise hit a fatal here (issue #57).
if [ ! -d "$CLAUDE_HOME" ]; then
    mkdir -p "$CLAUDE_HOME" || fail "Could not create ${CLAUDE_HOME}."
    warn "Created ${CLAUDE_HOME} (skill-tree home). Claude Code also creates this on first run."
fi
info "${CLAUDE_HOME} OK"

# ── Plugin Conflict Check ────────────────────────────────────

if [ -d "${CLAUDE_HOME}/plugins/cache" ]; then
    if find "${CLAUDE_HOME}/plugins/cache" -name "plugin.json" -exec grep -l '"name"[[:space:]]*:[[:space:]]*"token-optimizer"' {} \; 2>/dev/null | head -1 | grep -q .; then
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

# ── Integrity Metadata ─────────────────────────────────────────
# Checksums are fetched from the GitHub release (out-of-band), NOT from
# the repo tree. This prevents a single compromised commit from swapping
# both code and checksums simultaneously.
# Set TOKEN_OPTIMIZER_SKIP_VERIFY=1 to bypass (air-gapped installs).

verification_enabled() {
    [ "${TOKEN_OPTIMIZER_SKIP_VERIFY:-}" != "1" ]
}

resolve_latest_release() {
    local release_json parsed
    release_json=$(curl -fsSL \
        "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" \
        2>/dev/null) || return 1

    parsed=$(printf '%s' "$release_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("tag_name", ""))
    asset = ""
    for a in data.get("assets", []):
        if a.get("name") == "CHECKSUMS.sha256":
            asset = a.get("browser_download_url", "")
            break
    print(asset)
except Exception:
    print("")
    print("")
' 2>/dev/null) || return 1

    RELEASE_TAG=$(printf '%s\n' "$parsed" | sed -n '1p')
    CHECKSUM_ASSET_URL=$(printf '%s\n' "$parsed" | sed -n '2p')
    [ -n "$RELEASE_TAG" ] && [ -n "$CHECKSUM_ASSET_URL" ]
}

rollback_install_update() {
    if [ -n "$INSTALL_OLD_HEAD" ] && [ "$INSTALL_UPDATED" = "1" ] && [ -d "${INSTALL_DIR}/.git" ]; then
        local attempted_head
        attempted_head=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
        warn "Rolling back unverified update from ${attempted_head} to ${INSTALL_OLD_HEAD}"
        if git -C "$INSTALL_DIR" reset --hard "$INSTALL_OLD_HEAD" >/dev/null 2>&1; then
            info "Rollback succeeded"
        else
            warn "Rollback failed. Re-clone from: https://github.com/${GITHUB_REPO}"
        fi
    fi
}

fail_verified_install() {
    rollback_install_update
    fail "$1"
}

# Dirty-tree-specific failure for update_repo (issue #57). The verified
# checkout aborts with "local changes would be overwritten" when tracked
# files are locally modified (e.g. a prior chmod flipped measure.py's mode).
# Give the user an actionable message instead of the generic "check network".
fail_dirty_tree_update() {
    rollback_install_update
    fail "Could not update to verified release ${RELEASE_TAG}: the working tree at ${INSTALL_DIR} has local changes that would be overwritten. Inspect with: git -C ${INSTALL_DIR} status — then either commit/stash your edits or re-clone from: https://github.com/${GITHUB_REPO}"
}

# ── Skill payload completeness check (U1, issue #57) ──────────
# A partial sparse-checkout can leave skills/ present but the files inside it
# missing, so SKILL.md emits "[file not found]" refs and the runtime loads
# stale behavior. The existing `[ -d skills ]` guards only check directory
# existence; this helper verifies the *payload* — SKILL.md, scripts/measure.py,
# and every references/*.md SKILL.md actually cites. The reference list is
# derived from SKILL.md itself (grep, not hardcoded) so it never drifts when a
# reference is added or renamed.
#
# Returns 0 if every required file exists, 1 if any is missing. Never suppressed
# with 2>/dev/null or || true — a missing payload must be visible.

verify_skill_payload() {
    local skill_root="${INSTALL_DIR}/skills/token-optimizer"
    local skill_md="${skill_root}/SKILL.md"
    local measure_py="${skill_root}/scripts/measure.py"

    # Fast pre-filter: the skill directory and the two anchor files must exist.
    [ -d "$skill_root" ] || return 1
    [ -f "$skill_md" ] || return 1
    [ -f "$measure_py" ] || return 1

    # Derive the references/*.md files SKILL.md actually references and verify
    # each one is present. sort -u dedups; the while loop reads one path per line.
    local ref
    while IFS= read -r ref; do
        [ -n "$ref" ] || continue
        [ -f "${skill_root}/${ref}" ] || return 1
    done < <(grep -oE 'references/[A-Za-z0-9._-]+\.md' "$skill_md" | sort -u)

    return 0
}

# Repair a partial skill payload via the existing sparse-checkout disable path,
# re-verifying after each attempt. Aborts loudly via fail_verified_install if
# the payload is still incomplete after repair — never wires hooks against a
# partial tree. The completeness check itself is never suppressed.
repair_and_verify_skill_payload() {
    if verify_skill_payload; then
        return 0
    fi
    warn "Incomplete skill payload detected (missing referenced files). Repairing sparse checkout..."
    git -C "$INSTALL_DIR" sparse-checkout set \
        skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/ \
        2>/dev/null || git -C "$INSTALL_DIR" sparse-checkout disable 2>/dev/null || true
    if verify_skill_payload; then
        info "Skill payload restored"
        return 0
    fi
    warn "Sparse checkout set did not restore skill payload. Disabling sparse checkout..."
    git -C "$INSTALL_DIR" sparse-checkout disable 2>/dev/null || true
    if verify_skill_payload; then
        info "Skill payload restored (sparse checkout disabled)"
        return 0
    fi
    fail_verified_install "Incomplete skill payload: required files under ${INSTALL_DIR}/skills/token-optimizer/ are still missing after sparse-checkout repair. Re-clone from: https://github.com/${GITHUB_REPO}"
}

fetch_release_checksums() {
    [ -n "$CHECKSUM_ASSET_URL" ] || return 1
    curl -fsSL -o "$CHECKSUM_FILE" "$CHECKSUM_ASSET_URL" 2>/dev/null && [ -s "$CHECKSUM_FILE" ]
}

verify_checksum_manifest_coverage() {
    local target_dir="${1:-$INSTALL_DIR}"
    local manifest_list tracked_list missing
    manifest_list="${TMP_DIR}/checksum-manifest.paths"
    tracked_list="${TMP_DIR}/tracked-runtime.paths"

    awk 'NF >= 2 {print $2}' "$CHECKSUM_FILE" | sort -u > "$manifest_list"
    git -C "$target_dir" ls-files \
        install.sh \
        hooks/ \
        skills/ \
        .claude-plugin/ \
        .codex-plugin/ \
        .codex/ \
        | sort -u > "$tracked_list"

    missing="$(comm -23 "$tracked_list" "$manifest_list" || true)"
    if [ -n "$missing" ]; then
        warn "Release checksum manifest is missing installed runtime files:"
        printf '%s\n' "$missing" | sed 's/^/    /'
        return 1
    fi
    return 0
}

verify_checksums_in_dir() {
    local target_dir="$1"
    (
        cd "$target_dir" || exit 1
        sha256sum -c "$CHECKSUM_FILE" --quiet 2>/dev/null || \
        shasum -a 256 -c "$CHECKSUM_FILE" --quiet 2>/dev/null
    ) || return 1
    verify_checksum_manifest_coverage "$target_dir"
}

verify_release_candidate_before_live_update() {
    local candidate_dir candidate_url
    candidate_dir="${TMP_DIR}/release-candidate"
    candidate_url=$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || echo "$REPO_HTTPS")

    [ -s "$CHECKSUM_FILE" ] || fetch_release_checksums || return 1
    rm -rf "$candidate_dir"
    git clone --depth 1 --filter=blob:none --sparse --branch "$RELEASE_TAG" \
        "$candidate_url" "$candidate_dir" >/dev/null 2>&1 || return 1
    git -C "$candidate_dir" sparse-checkout set \
        skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/ \
        >/dev/null 2>&1 || return 1
    verify_checksums_in_dir "$candidate_dir" || return 1
    VERIFIED_RELEASE_HEAD=$(git -C "$candidate_dir" rev-parse HEAD 2>/dev/null || echo "")
    [ -n "$VERIFIED_RELEASE_HEAD" ]
}

if verification_enabled; then
    info "Resolving latest verified release..."
    resolve_latest_release || fail_verified_install "Could not resolve the latest GitHub Release and checksum asset. Integrity verification is required. Set TOKEN_OPTIMIZER_SKIP_VERIFY=1 only if you explicitly accept this risk."
    info "Latest verified release: ${RELEASE_TAG}"
else
    warn "Skipping integrity verification (TOKEN_OPTIMIZER_SKIP_VERIFY=1)"
fi

# ── Clone or Update ───────────────────────────────────────────

clone_repo() {
    local clone_log="${TMP_DIR}/clone.log"

    # Sparse checkout: only pull Claude Code files, skip OpenClaw platform files
    try_clone() {
        local url="$1"
        if verification_enabled; then
            git clone --depth 1 --filter=blob:none --sparse --branch "$RELEASE_TAG" "$url" "$INSTALL_DIR" 2>"$clone_log" || return 1
        else
            git clone --depth 1 --filter=blob:none --sparse "$url" "$INSTALL_DIR" 2>"$clone_log" || return 1
        fi
        # Cone mode only accepts directories; root-level files are included automatically
        git -C "$INSTALL_DIR" sparse-checkout set \
            skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/ \
            2>>"$clone_log" || return 1
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

update_repo() {
    local before_head after_head fetched_head
    before_head=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "")
    if verification_enabled; then
        info "Updating to verified release ${RELEASE_TAG}..."
        verify_release_candidate_before_live_update || return 1
        git -C "$INSTALL_DIR" fetch --force --depth 1 origin "refs/tags/${RELEASE_TAG}:refs/tags/${RELEASE_TAG}" || return 1
        fetched_head=$(git -C "$INSTALL_DIR" rev-parse "${RELEASE_TAG}^{commit}" 2>/dev/null || echo "")
        [ -n "$fetched_head" ] && [ "$fetched_head" = "$VERIFIED_RELEASE_HEAD" ] || return 1
        # Dirty-tree preflight (issue #57): a previous install's chmod of
        # measure.py flips the git-tracked 100644 file to 100755; with
        # core.fileMode=true git then sees it as locally modified and the
        # detached checkout below aborts with "local changes would be
        # overwritten". Stash (never discard) only the tracked runtime paths
        # so the working tree is clean for the checkout, while any genuine
        # local edit stays recoverable via `git -C "$INSTALL_DIR" stash list`.
        # Never touches untracked user files or blanket-resets arbitrary edits.
        if ! git -C "$INSTALL_DIR" diff --quiet -- skills hooks .claude-plugin .codex-plugin .codex 2>/dev/null; then
            warn "Dirty working tree on tracked runtime paths. Stashing them before checkout (recover with: git -C ${INSTALL_DIR} stash list)..."
            git -C "$INSTALL_DIR" stash push -q -m "token-optimizer-install-autostash" -- skills hooks .claude-plugin .codex-plugin .codex 2>/dev/null || true
        fi
        if ! git -C "$INSTALL_DIR" checkout --detach -q "$VERIFIED_RELEASE_HEAD" 2>"${TMP_DIR}/checkout.err"; then
            # Checkout failed — distinguish a dirty-tree failure from a
            # network issue so the user gets an actionable message (#57).
            if ! git -C "$INSTALL_DIR" diff --quiet 2>/dev/null \
               || ! git -C "$INSTALL_DIR" diff --cached --quiet 2>/dev/null; then
                fail_dirty_tree_update
            fi
            warn "Checkout failed: $(cat "${TMP_DIR}/checkout.err" 2>/dev/null)"
            return 1
        fi
    else
        git -C "$INSTALL_DIR" pull --ff-only || {
            warn "git pull failed. Try: cd ${INSTALL_DIR} && git pull"
            warn "Continuing with existing version."
        }
    fi
    after_head=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "")
    if [ -n "$before_head" ] && [ "$after_head" != "$before_head" ]; then
        INSTALL_UPDATED=1
    fi
}

if [ -d "${INSTALL_DIR}/.git" ]; then
    info "Existing install found. Updating..."
    INSTALL_OLD_HEAD=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)

    # Enable sparse checkout on existing installs (migrates full clones)
    if ! git -C "$INSTALL_DIR" sparse-checkout list &>/dev/null || \
       git -C "$INSTALL_DIR" sparse-checkout list 2>/dev/null | grep -q "^/$"; then
        info "Migrating to sparse checkout (removing OpenClaw files)..."
        git -C "$INSTALL_DIR" sparse-checkout init --cone 2>/dev/null || true
        # Cone mode only accepts directories; root-level files are included automatically
        git -C "$INSTALL_DIR" sparse-checkout set \
            skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/ \
            2>/dev/null || true
    fi

    # Self-heal: v5.7.5-5.7.9 had a sparse-checkout bug that pruned skills/ and hooks/.
    # If they're missing after update, fix the sparse checkout config.
    if [ ! -d "${INSTALL_DIR}/skills" ] || [ ! -d "${INSTALL_DIR}/hooks" ]; then
        warn "Broken sparse checkout detected (skills/ or hooks/ missing). Repairing..."
        git -C "$INSTALL_DIR" sparse-checkout set \
            skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/ \
            2>/dev/null || git -C "$INSTALL_DIR" sparse-checkout disable 2>/dev/null || true
        if [ ! -d "${INSTALL_DIR}/skills" ]; then
            warn "Sparse checkout repair failed. Disabling sparse checkout..."
            git -C "$INSTALL_DIR" sparse-checkout disable 2>/dev/null || true
        fi
        if [ -d "${INSTALL_DIR}/skills" ]; then
            info "Sparse checkout repaired"
        else
            fail "Could not restore skills/ directory. Try: cd ${INSTALL_DIR} && git sparse-checkout disable"
        fi
    fi

    update_repo || fail_verified_install "Could not update to verified release ${RELEASE_TAG}. The verified release checkout failed (a dirty working tree was already handled with an actionable message above; otherwise check network connectivity or re-clone from: https://github.com/${GITHUB_REPO})."

    # A release checkout can change sparse checkout behavior. Repair again after update.
    if [ ! -d "${INSTALL_DIR}/skills" ] || [ ! -d "${INSTALL_DIR}/hooks" ]; then
        warn "Broken sparse checkout detected after update. Repairing..."
        git -C "$INSTALL_DIR" sparse-checkout set \
            skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/ \
            2>/dev/null || git -C "$INSTALL_DIR" sparse-checkout disable 2>/dev/null || true
        if [ ! -d "${INSTALL_DIR}/skills" ]; then
            warn "Sparse checkout repair failed. Disabling sparse checkout..."
            git -C "$INSTALL_DIR" sparse-checkout disable 2>/dev/null || true
        fi
        [ -d "${INSTALL_DIR}/skills" ] || fail_verified_install "Could not restore skills/ directory. Try re-cloning from: https://github.com/${GITHUB_REPO}"
        # Verify the skill payload (not just the directory) after the dir repair.
        repair_and_verify_skill_payload
    fi

    # Verify skill payload completeness even when skills/ exists — a partial
    # sparse-checkout can leave the directory present but referenced files
    # missing (issue #57). This is the primary guard for the bug.
    repair_and_verify_skill_payload
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

# ── Integrity Verification ────────────────────────────────────
if verification_enabled; then
    info "Fetching checksums from GitHub release..."
    if [ -s "$CHECKSUM_FILE" ] || fetch_release_checksums; then
        info "Verifying file integrity (out-of-band checksums)..."
        verify_checksums_in_dir "$INSTALL_DIR" || fail_verified_install "Integrity check FAILED. Files do not match release checksums or the release manifest is incomplete. Your install may be compromised. Re-clone from: https://github.com/${GITHUB_REPO}"
        info "Integrity check passed"
    else
        fail_verified_install "Could not fetch CHECKSUMS.sha256 from the latest GitHub Release. Integrity verification is required. Set TOKEN_OPTIMIZER_SKIP_VERIFY=1 only if you explicitly accept this risk."
    fi
fi

# Log the current commit SHA so users can audit which version is installed.
SHA_LOG_DIR="$INSTALL_DIR"
mkdir -p "$SHA_LOG_DIR"
CURRENT_SHA=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
CURRENT_SHORT=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
printf "%s\t%s\t%s\n" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$CURRENT_SHA" "install" \
    >> "${SHA_LOG_DIR}/.last-verified-sha"
info "Verified commit ${CURRENT_SHORT} logged to ${SHA_LOG_DIR}/.last-verified-sha"

# ── Symlink Skill ─────────────────────────────────────────────

mkdir -p "$SKILL_DIR"
SKILL_LINK="${SKILL_DIR}/token-optimizer"

if [ -d "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    _skill_backup="${SKILL_LINK}.pre-reconcile.$(date -u +%Y%m%dT%H%M%SZ).$$"
    if mv "$SKILL_LINK" "$_skill_backup" 2>/dev/null; then
        if ln -sfn "${INSTALL_DIR}/skills/token-optimizer" "$SKILL_LINK"; then
            info "Replaced stale skill directory with symlink (old copy backed up to ${_skill_backup})"
        else
            if mv "$_skill_backup" "$SKILL_LINK" 2>/dev/null; then
                warn "Could not create symlink at ${SKILL_LINK}; original directory restored."
            else
                warn "Could not create symlink at ${SKILL_LINK}; original is preserved at ${_skill_backup}."
            fi
            warn "To use the repo version, move it: mv ${SKILL_LINK} ${SKILL_LINK}.local"
        fi
    else
        warn "/token-optimizer skill directory exists (not a symlink) and could not be moved. Skipping."
        warn "To use the repo version, move it: mv ${SKILL_LINK} ${SKILL_LINK}.local"
    fi
elif [ -f "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "Regular file exists at ${SKILL_LINK}. Moving to ${SKILL_LINK}.bak"
    mv "$SKILL_LINK" "${SKILL_LINK}.bak"
    ln -sfn "${INSTALL_DIR}/skills/token-optimizer" "$SKILL_LINK"
    info "Linked /token-optimizer skill"
else
    ln -sfn "${INSTALL_DIR}/skills/token-optimizer" "$SKILL_LINK"
    info "Linked /token-optimizer skill"
fi

# ── Reconcile dev-symlink / plugin-cache skill shadow (issue #57) ─
# A stale plugin-cache skill copy can shadow the fresh symlinked payload. The
# reconcile pass is backup-first, foreign-runtime-guarded, idempotent, and a
# no-op when there is nothing to reconcile. Tolerate any failure: a missing or
# broken reconcile must never abort the install.
if [ -f "${INSTALL_DIR}/skills/token-optimizer/scripts/install_reconcile.py" ]; then
    python3 "${INSTALL_DIR}/skills/token-optimizer/scripts/install_reconcile.py" \
        >"${INSTALL_DIR}/.last-reconcile.log" 2>&1 || true
fi

# ── Make Scripts Executable ───────────────────────────────────
# No tracked script is invoked directly as ./file; every caller runs
# `python3 measure.py`, so chmod is unnecessary and harmful: it flips the
# git-tracked 100644 file to 100755, and with core.fileMode=true the next
# verified-update checkout aborts with "local changes would be
# overwritten" (issue #57). Leave chmod ONLY on files actually run as
# ./file (none currently).

# ── Setup Quality Bar (auto-install cache hook + status line) ─

info "Setting up quality bar..."
if python3 "${INSTALL_DIR}/skills/token-optimizer/scripts/measure.py" setup-quality-bar 2>/dev/null; then
    info "Quality bar installed (status line + cache hook)"
else
    warn "Could not auto-install quality bar. Run manually in Claude Code:"
    warn "  python3 measure.py setup-quality-bar"
fi

# ── Setup All Hooks (v5.0.1: merge plugin hooks.json into settings.json) ────
# Canonical way for script installs to get the full v5 hook set.
# Idempotent: safe to re-run on every install and verified release update.
# Upgrades from v4.x pick up v5 active compression hooks here.

info "Installing all Token Optimizer hooks..."
# Guard with set +e: under `set -euo pipefail` a failing command substitution
# in a plain assignment would hard-exit before HOOK_EXIT is captured, so a
# fresh empty $CLAUDE_HOME (e.g. an OpenCode user without Claude Code, whose
# settings.json doesn't exist yet) would abort the install instead of
# degrading to the warn branch below (issue #57).
set +e
HOOK_OUTPUT=$(python3 "${INSTALL_DIR}/skills/token-optimizer/scripts/measure.py" setup-all-hooks 2>&1)
HOOK_EXIT=$?
set -e
if [ $HOOK_EXIT -eq 0 ]; then
    HOOK_SUMMARY=$(echo "$HOOK_OUTPUT" | grep -E "Added [0-9]+|All hooks already present" | head -1)
    if [ -n "$HOOK_SUMMARY" ]; then
        info "$(echo "$HOOK_SUMMARY" | sed 's/^[[:space:]]*\[setup-all-hooks\][[:space:]]*//')"
    else
        info "Hooks installed"
    fi
    # setup_all_hooks updates last_hook_heal_check automatically on success,
    # suppressing the redundant ensure-health run for the next 24h.
else
    warn "Could not auto-install hooks. Run manually:"
    warn "  python3 ${INSTALL_DIR}/skills/token-optimizer/scripts/measure.py setup-all-hooks"
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
