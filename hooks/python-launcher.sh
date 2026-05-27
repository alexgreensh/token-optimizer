#!/usr/bin/env bash
# Locate a usable Python 3 interpreter and exec it with the given arguments.
# Survives:
#   - macOS / Linux (python3 on PATH)
#   - Windows python.org installs at spaced paths like "C:\Program Files\Python313\"
#   - Windows py-launcher-only installs (py -3)
#   - Windows Store Python (real installs probed with --version; non-functional
#     AppExecutionAlias stubs skipped automatically)
# Exits 127 with a diagnostic message if none found.

set -eu

# Known-safe prefixes for Python interpreter binaries.
# Binaries outside these directories are rejected even if on PATH.
# This prevents a compromised PATH entry from hijacking the interpreter.
# All prefixes are hardcoded (not derived from PATH-controlled binaries
# like `brew --prefix`, which would be circular trust).
_SAFE_PREFIXES="/usr/bin /usr/local/bin /opt/homebrew/bin /opt/homebrew/opt /home/linuxbrew/.linuxbrew/bin"

_is_safe_prefix() {
    local binpath="$1" prefix
    for prefix in $_SAFE_PREFIXES; do
        case "$binpath" in
            "$prefix"/*) return 0 ;;
        esac
    done
    return 1
}

find_interpreter() {
    local name="$1"
    local IFS=:
    local dir binpath ext
    for dir in $PATH; do
        [ -n "$dir" ] || dir="."
        for ext in "" ".exe"; do
            binpath="${dir}/${name}${ext}"
            [ -x "$binpath" ] || continue
            [ -s "$binpath" ] || continue
            # Reject interpreters outside known-safe prefix directories.
            # Prevents PATH-order attacks where a malicious dir appears first.
            _is_safe_prefix "$binpath" || continue
            case "$binpath" in
                */WindowsApps/*|*/windowsapps/*)
                    # WindowsApps may contain real Store-installed Python OR
                    # non-functional AppExecutionAlias stubs (non-zero-byte, pass -s).
                    # Probe with --version (2s timeout) to distinguish them.
                    if command -v timeout >/dev/null 2>&1; then
                        timeout 2s "$binpath" --version >/dev/null 2>&1 || continue
                    else
                        "$binpath" --version >/dev/null 2>&1 || continue
                    fi
                    ;;
            esac
            printf "%s\n" "$binpath"
            return 0
        done
    done
    return 1
}

if py3=$(find_interpreter "python3"); then
    exec "$py3" "$@"
fi

if py=$(find_interpreter "python"); then
    exec "$py" "$@"
fi

if pyl=$(find_interpreter "py"); then
    exec "$pyl" -3 "$@"
fi

# Direct probe: hook environments often have a stripped PATH that excludes
# the user's Python. Check known locations directly as a fallback.
for _direct in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 \
               /home/linuxbrew/.linuxbrew/bin/python3; do
    if [ -x "$_direct" ] && [ -s "$_direct" ]; then
        exec "$_direct" "$@"
    fi
done

echo "token-optimizer: no usable Python 3 interpreter found" >&2
echo "  tried: python3, python, py -3, direct paths" >&2
echo "  on Windows: install Python from https://python.org/ and restart Claude Code" >&2
exit 127
