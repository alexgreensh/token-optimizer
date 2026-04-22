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
            case "$binpath" in
                */WindowsApps/*|*/windowsapps/*)
                    # WindowsApps may contain real Store-installed Python OR
                    # non-functional AppExecutionAlias stubs (non-zero-byte, pass -s).
                    # Probe with --version to distinguish functional interpreters.
                    "$binpath" --version >/dev/null 2>&1 || continue
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

echo "token-optimizer: no usable Python 3 interpreter found" >&2
echo "  tried: python3, python, py -3" >&2
echo "  on Windows: install Python from https://python.org/ and restart Claude Code" >&2
exit 127
