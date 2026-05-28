#!/usr/bin/env bash
# Token Optimizer: shared path resolver. Centralizes the runtime
# detection and measure.py / fleet.py / token-coach lookup that used to
# be duplicated as ~20-line bash blocks at the top of every SKILL.md
# and command body. Each skill body line costs auto-compaction budget
# (https://code.claude.com/docs/en/skills), so the bodies just `eval`
# the export form below.
#
# Usage:
#   eval "$(bash resolve.sh --export [--need measure[,fleet][,coach-dir]])"
#   bash resolve.sh --what runtime|measure|fleet|coach-dir
#
# Supports Claude plugin (${CLAUDE_PLUGIN_ROOT}), legacy install.sh
# (~/.claude/token-optimizer/), Codex plugin
# (~/.codex/plugins/cache/.../), and dev checkout ($PWD). Bash-only,
# Git-Bash / MSYS friendly on Windows.

set -u
shopt -s nullglob 2>/dev/null || true

what="" do_export=0 need_measure=0 need_fleet=0 need_coach=0

while [ $# -gt 0 ]; do
    case "$1" in
        --what)   what="${2:-}";   shift 2 ;;
        --export) do_export=1;     shift   ;;
        --need)
            _old_ifs="${IFS-}"; IFS=','
            for _i in ${2:-}; do
                case "$_i" in
                    measure)         need_measure=1 ;;
                    fleet)           need_fleet=1   ;;
                    coach-dir|coach) need_coach=1   ;;
                    *) echo "[Error] resolve.sh: unknown --need value: $_i" >&2; exit 2 ;;
                esac
            done
            IFS="$_old_ifs"; shift 2 ;;
        *) echo "[Error] resolve.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Runtime: explicit env wins, else Claude plugin signals, else Codex, else claude.
RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then RUNTIME=claude
    elif [ -n "${CODEX_HOME:-}" ] || [ -d "${HOME}/.codex" ];                 then RUNTIME=codex
    else                                                                           RUNTIME=claude
    fi
fi

# Resolve a relative tail across all known install layouts.
# Order: CLAUDE_PLUGIN_ROOT (if set) → codex-first/claude-first per RUNTIME
# → legacy install.sh → dev checkout ($PWD).
_resolve() {
    local op="$1" tail="$2" c
    local -a cs=()
    [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && cs+=("${CLAUDE_PLUGIN_ROOT}/${tail}")
    local -a codex=(
        "${HOME}/.codex/${tail}"
        "${HOME}/.codex/plugins/cache"/*/token-optimizer/*/"${tail}"
    )
    local -a claude=(
        "${HOME}/.claude/${tail}"
        "${HOME}/.claude/plugins/cache"/*/token-optimizer/*/"${tail}"
        "${HOME}/.claude/token-optimizer/${tail}"
    )
    if [ "$RUNTIME" = codex ]; then cs+=("${codex[@]}" "${claude[@]}")
    else                            cs+=("${claude[@]}" "${codex[@]}")
    fi
    cs+=("${PWD}/${tail}")
    for c in "${cs[@]}"; do
        [ -n "$c" ] || continue
        { [ "$op" = f ] && [ -f "$c" ]; } || { [ "$op" = d ] && [ -d "$c" ]; } || continue
        printf '%s\n' "$c"; return 0
    done
    return 1
}

MEASURE_PY="$(_resolve f 'skills/token-optimizer/scripts/measure.py' || true)"
FLEET_PY="$(_resolve f 'skills/fleet-auditor/scripts/fleet.py' || true)"
COACH_DIR="$(_resolve d 'skills/token-coach' || true)"

_die_measure() { echo "[Error] measure.py not found. Is Token Optimizer installed?" >&2; exit 1; }
_die_fleet()   { echo "[Error] fleet.py not found. Is Fleet Auditor installed?"    >&2; exit 1; }
_die_coach()   { echo "[Error] token-coach directory not found. Is Token Coach installed?" >&2; exit 1; }

if [ -n "$what" ]; then
    case "$what" in
        runtime)         printf '%s\n' "$RUNTIME" ;;
        measure)         [ -n "$MEASURE_PY" ] || _die_measure; printf '%s\n' "$MEASURE_PY" ;;
        fleet)           [ -n "$FLEET_PY"   ] || _die_fleet;   printf '%s\n' "$FLEET_PY"   ;;
        coach-dir|coach) [ -n "$COACH_DIR"  ] || _die_coach;   printf '%s\n' "$COACH_DIR"  ;;
        *) echo "[Error] resolve.sh: unknown --what value: $what" >&2; exit 2 ;;
    esac
    exit 0
fi

if [ "$do_export" -eq 1 ]; then
    [ "$need_measure" -eq 1 ] && [ -z "$MEASURE_PY" ] && _die_measure
    [ "$need_fleet"   -eq 1 ] && [ -z "$FLEET_PY"   ] && _die_fleet
    [ "$need_coach"   -eq 1 ] && [ -z "$COACH_DIR"  ] && _die_coach
    # %q quotes paths so spaces in $HOME (Windows /c/Users/Some Name/) survive eval.
    printf 'export TOKEN_OPTIMIZER_RUNTIME=%q\n' "$RUNTIME"
    [ -n "$MEASURE_PY" ] && printf 'export MEASURE_PY=%q\n' "$MEASURE_PY"
    [ -n "$FLEET_PY"   ] && printf 'export FLEET_PY=%q\n'   "$FLEET_PY"
    [ -n "$COACH_DIR"  ] && printf 'export COACH_DIR=%q\n'  "$COACH_DIR"
    exit 0
fi

echo "[Error] resolve.sh: must specify --what or --export" >&2
exit 2
