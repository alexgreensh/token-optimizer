#!/usr/bin/env bash
# Locate a usable Python 3 interpreter and exec it with the given arguments.
# Survives:
#   - macOS / Linux (python3 on PATH)
#   - Windows python.org installs at spaced paths like "C:\Program Files\Python313\"
#   - Windows py-launcher-only installs (py -3)
#   - Windows Store Python (real installs proven alive via a flash-free
#     GUI-twin proof-of-life probe, console --version probe as the fallback
#     authority; non-functional AppExecutionAlias stubs skipped automatically)
#   - mise (https://mise.jdx.dev) shims at their default per-platform data-dir
#     location (Windows/macOS/Linux)
# On Windows (Git Bash/MSYS), exec prefers the GUI-subsystem twin over the
# console binary to avoid the per-hook console-window flash and orphaned
# conhost.exe: python.exe/python3.exe swap to pythonw.exe, and py.exe (the
# py-launcher) swaps to pyw.exe beside it, so py-launcher-only installs no
# longer flash (#107). See _maybe_swap_to_pythonw for the constraints.
# Exits 127 with a diagnostic message if none found.

set -eu
# C7: extglob enables +([0-9]) in the version-number case patterns below so
# the glob is anchored to the path-component boundary. Without it, * in a
# case pattern crosses / and Python[23]* matches Python3-evil/python.exe.
shopt -s extglob

# Known-safe prefixes for Python interpreter binaries.
# Binaries outside these directories are rejected even if on PATH.
# This prevents a compromised PATH entry from hijacking the interpreter.
# All prefixes are hardcoded (not derived from PATH-controlled binaries
# like `brew --prefix`, which would be circular trust).
_SAFE_PREFIXES="/usr/bin /usr/local/bin /opt/homebrew/bin /opt/homebrew/opt /home/linuxbrew/.linuxbrew/bin"

_is_safe_prefix() {
    local IFS=$' \t\n'
    local binpath="$1" prefix
    # Reject path traversal FIRST: a '..' component lets a textual prefix match
    # (e.g. /usr/bin/../../tmp/evil/python3) pass the allow-list globs below yet
    # resolve OUTSIDE a trusted dir at exec time. Interpreter paths are absolute
    # and never legitimately contain a '..' path component.
    case "$binpath" in
        *"/../"*|*"/..") return 1 ;;
    esac
    for prefix in $_SAFE_PREFIXES; do
        case "$binpath" in
            "$prefix"/*) return 0 ;;
        esac
    done
    # Windows install locations (git-bash/MSYS path form, e.g. /c/...).
    # Drive-letter-anchored to preserve the anti-PATH-hijack intent.
    # Version-number suffixes block directory-name spoofing (e.g. Python3-evil).
    case "$binpath" in
        # C7: +([0-9]) anchors the version suffix to digits-only so a spoofed
        # dir name like Python3-evil cannot pass (previously * crossed / and
        # matched Python3-evil/python.exe). The trailing /* requires a path
        # separator after the version component.
        /[a-zA-Z]/Program\ Files/Python[23]+([0-9])/*)                 return 0 ;;
        /[a-zA-Z]/Program\ Files\ \(x86\)/Python[23]+([0-9])/*)        return 0 ;;
        /[a-zA-Z]/Python3+([0-9])/*)                                   return 0 ;;
        /[a-zA-Z]/Users/*/AppData/Local/Programs/Python/*)              return 0 ;;
        /[a-zA-Z]/Users/*/AppData/Local/Microsoft/WindowsApps/*)        return 0 ;;
        # All-users `py` launcher lives in the (admin-only-writable) Windows dir.
        # Exact filename keeps the anti-hijack intent (no wildcard in that dir).
        /[a-zA-Z]/Windows/py.exe)                                      return 0 ;;
        /[a-zA-Z]/Windows/pyw.exe)                                     return 0 ;;
        # mise (https://mise.jdx.dev) shim directory, default Windows data dir.
        # Hardcoded default location only -- never derived from MISE_DATA_DIR/
        # MISE_SHIMS_DIR, which would reopen the PATH-hijack vector this
        # allowlist exists to close.
        /[a-zA-Z]/Users/*/AppData/Local/mise/shims/*)                  return 0 ;;
    esac
    # mise (https://mise.jdx.dev) shim directory, default macOS/Linux data dir
    # (XDG: ~/.local/share/mise/shims). Needs its own case block (not
    # _SAFE_PREFIXES) because it requires a per-user wildcard component.
    # Hardcoded default location only, same rationale as the Windows entry above.
    case "$binpath" in
        /Users/*/.local/share/mise/shims/*)                            return 0 ;;
        /home/*/.local/share/mise/shims/*)                             return 0 ;;
    esac
    # C8: case-insensitive WindowsApps allow for Windows-style drive-letter
    # paths. On a case-insensitive FS the dir can be any casing; the drive-
    # letter anchor ([a-zA-Z]/) constrains this to Windows-style paths so
    # Linux is unaffected.
    case "$binpath" in
        /[a-zA-Z]/*)
            _path_contains_windowsapps "$binpath" && return 0
            ;;
    esac
    return 1
}

# The cache is an optional optimization only. Any setup, read, or write failure
# leaves _PY_CACHE_FILE empty (or is ignored) so interpreter discovery proceeds
# exactly as it did before caching was added.
_PY_CACHE_FILE=""

_is_msys_platform() {
    local platform
    platform=$(uname -s 2>/dev/null) || return 1
    case "$platform" in
        MINGW*|MSYS*|CYGWIN*) return 0 ;;
    esac
    return 1
}

# C8: Windows is case-insensitive, so the WindowsApps directory can appear in
# any casing (WindowsApps, windowsapps, WINDOWSAPPS, Windowsapps). The old
# explicit-variant patterns (*/WindowsApps/*|*/windowsapps/*) only covered
# two casings and would miss others, letting a Store AppExecutionAlias stub
# through unprobed or skipping a legit Store install in the safe-prefix list.
# tr is POSIX and present in every supported hook env including Git Bash.
_path_contains_windowsapps() {
    local lower
    lower=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    case "$lower" in
        */windowsapps/*) return 0 ;;
    esac
    return 1
}

_cache_dir_is_per_user() {
    local cache_dir="$1" cache_real root root_real
    cache_real=$(CDPATH='' cd -- "$cache_dir" 2>/dev/null && pwd -P) || return 1
    for root in "${XDG_CACHE_HOME:-}" "${HOME:-}"; do
        [ -n "$root" ] && [ -d "$root" ] || continue
        root_real=$(CDPATH='' cd -- "$root" 2>/dev/null && pwd -P) || continue
        case "$cache_real" in
            "$root_real"/*) return 0 ;;
        esac
    done
    return 1
}

_cache_dir_ready() {
    local cache_dir="$1"
    if [ ! -d "$cache_dir" ]; then
        (umask 077; mkdir -p "$cache_dir") >/dev/null 2>&1 || return 1
    fi
    [ -d "$cache_dir" ] && [ -w "$cache_dir" ] || return 1
    if _is_msys_platform; then
        # Git Bash/MSYS2/Cygwin emulate POSIX ownership, so `test -O` can reject
        # the current user's own directory. Canonical confinement under HOME or
        # XDG_CACHE_HOME supplies the equivalent Windows ACL trust boundary.
        _cache_dir_is_per_user "$cache_dir"
    else
        # On native POSIX, require ownership rather than mere writability so a
        # planted cache record in another user's directory is never trusted.
        [ -O "$cache_dir" ]
    fi
}

_setup_interpreter_cache() {
    local launcher_dir plugin_dir hash_output plugin_hash path_hash_output path_hash cache_dir

    launcher_dir=${0%/*}
    [ "$launcher_dir" != "$0" ] || launcher_dir=.
    if ! plugin_dir=$(CDPATH='' cd -- "$launcher_dir" 2>/dev/null && pwd -P); then
        return 0
    fi

    # cksum is POSIX and is present in the Unix environments supported by this
    # launcher, including Git Bash. If unavailable, caching simply stays off.
    if ! hash_output=$(printf '%s' "$plugin_dir" | cksum 2>/dev/null); then
        return 0
    fi
    plugin_hash=${hash_output%% *}
    case "$plugin_hash" in
        ''|*[!0-9]*) return 0 ;;
    esac
    if ! path_hash_output=$(printf '%s' "${PATH:-}" | cksum 2>/dev/null); then
        return 0
    fi
    path_hash=${path_hash_output%% *}
    case "$path_hash" in
        ''|*[!0-9]*) return 0 ;;
    esac

    if [ "${TOKEN_OPTIMIZER_PY_CACHE+x}" = x ]; then
        cache_dir=$TOKEN_OPTIMIZER_PY_CACHE
        [ -n "$cache_dir" ] || return 0
        _cache_dir_ready "$cache_dir" || return 0
    else
        cache_dir=""
        if [ -n "${XDG_CACHE_HOME:-}" ]; then
            cache_dir="${XDG_CACHE_HOME}/token-optimizer/pylauncher"
        elif [ -n "${HOME:-}" ]; then
            cache_dir="${HOME}/.cache/token-optimizer/pylauncher"
        fi

        # No shared/world-predictable fallback (e.g. /tmp/token-optimizer-$UID):
        # such a path lets another local user pre-create the dir and plant a
        # poisoned interpreter record. If no per-user home cache dir is available
        # and writable, caching stays OFF and discovery runs exactly as before
        # (fail-open). HOME is set in every supported hook env, incl. Git Bash.
        { [ -n "$cache_dir" ] && _cache_dir_ready "$cache_dir"; } || return 0
    fi

    # Cache key includes a PROBE-LOGIC EPOCH (#143). The key is otherwise
    # plugin-dir + PATH checksums, neither of which changes when the launcher's
    # interpreter-liveness logic changes -- so a user already bitten by #143 has a
    # cache record naming the DEAD WindowsApps stub, and the fixed probe never runs
    # on a cache HIT (only on a miss). Bumping this epoch renames the cache file, so
    # every stale record is ignored once on upgrade: discovery re-runs, the new
    # probe rejects the dead stub, and a healthy interpreter is cached under the new
    # key. Bump `e2` on any future change to interpreter-liveness probing.
    _PY_CACHE_FILE="${cache_dir%/}/interpreter-e2-${plugin_hash}-${path_hash}.cache"
}

# On Windows (Git Bash/MSYS), python.exe is a console-subsystem binary: each
# hook invocation allocates a transient console window (the "flash") and can
# orphan a conhost.exe. pythonw.exe is the GUI-subsystem twin shipped beside
# it by python.org; launching it allocates no console. We prefer pythonw.exe
# at EXEC time (not discovery time) so a stale cache record that still names
# python.exe does not keep flashing forever -- the cache key is unchanged, so
# the swap must happen on every exec, in both _exec_cached_interpreter and
# _exec_discovered_interpreter.
#
# Constraints (each is a real failure mode, not paranoia):
#   * Same directory only: pythonw.exe must sit next to the resolved
#     python.exe (same install, same version). We never synthesise a path
#     from PATH.
#   * Skip WindowsApps: a Microsoft Store pythonw AppExecutionAlias exits
#     silently outside an interactive token (see measure.py).
#   * stdout must be a pipe, not a tty: pythonw is GUI-subsystem. When its
#     std handles are not valid pipes, CPython sets sys.stdout/sys.stderr to
#     None and the hook protocol goes dark while the process exits 0 -- the
#     top regression for this change. A tty on fd 1 means a console is
#     already attached (no flash to avoid) AND pythonw could null it, so we
#     keep python.exe there. The hook context always provides a pipe, which
#     pythonw inherits intact.
#   * Non-Windows is a strict no-op: the MSYS guard short-circuits before any
#     filesystem probe, so POSIX behaviour is byte-for-byte unchanged.
#   * py.exe (the `py -3` launcher) swaps to pyw.exe in the same directory
#     (#107): pyw.exe is the GUI-subsystem launcher twin and `pyw -3` execs
#     pythonw.exe. Every other guard (same-dir, safe prefix, WindowsApps
#     skip, tty check, liveness probe) applies to pyw.exe unchanged.
#
# Sets _PYW_INTERP to the chosen interpreter (pythonw.exe when swapped, else
# the input unchanged). Selection never fails -- on any doubt we keep the
# caller's interpreter, so this can only ever opt INTO pythonw, never block
# an exec. Uses a variable (not stdout) so the [ -t 1 ] guard sees the
# launcher's real fd 1, not a command-substitution pipe.
_maybe_swap_to_pythonw() {
    local interp="$1" dir twin pythonw
    _PYW_INTERP="$interp"
    case "$interp" in
        */python.exe|*/python3.exe) twin="pythonw.exe" ;;
        */py.exe) twin="pyw.exe" ;;
        *) return 0 ;;
    esac
    _is_msys_platform || return 0
    if [ -t 1 ]; then
        return 0
    fi
    dir=${interp%/*}
    pythonw="${dir}/${twin}"
    [ -f "$pythonw" ] && [ -x "$pythonw" ] && [ -s "$pythonw" ] || return 0
    _is_safe_prefix "$pythonw" || return 0
    # C8: case-insensitive WindowsApps skip (any casing on case-insensitive FS).
    _path_contains_windowsapps "$pythonw" && return 0
    # Liveness-probe the twin before committing to it. A corrupt/garbage
    # pythonw.exe (e.g. a half-overwritten install twin, or a 0xC000-style
    # Windows stub) passes the -f/-x/-s metadata checks but exits nonzero
    # (often 127) when actually run. exec consumes the process, so a dead
    # twin would brick every hook exec and the launcher's fallback ladder
    # becomes unreachable. Probe exactly like find_interpreter probes
    # WindowsApps python.exe: 2s timeout, -c "" (no program), stdin from
    # /dev/null so the hook's real stdin is never consumed by the probe.
    # On any doubt, keep python.exe (return 0) -- this can only ever opt
    # INTO pythonw, never block an exec.
    # C6: --kill-after=1s escalates to SIGKILL 1s after SIGTERM. pythonw is
    # GUI-subsystem and can ignore SIGTERM (no console handler), leaving a
    # hung twin holding the 2s budget past expiry and stalling the hook.
    if command -v timeout >/dev/null 2>&1; then
        timeout --kill-after=1s 2s "$pythonw" -c "" </dev/null >/dev/null 2>&1 || return 0
    else
        "$pythonw" -c "" </dev/null >/dev/null 2>&1 || return 0
    fi
    _PYW_INTERP="$pythonw"
}

_exec_cached_interpreter() {
    local record payload interp marker

    [ -n "$_PY_CACHE_FILE" ] && [ -f "$_PY_CACHE_FILE" ] || return 1
    record=""
    if ! IFS= read -r record 2>/dev/null < "$_PY_CACHE_FILE"; then
        return 1
    fi
    case "$record" in
        $'INTERP\t'*) payload=${record#*$'\t'} ;;
        *) return 1 ;;
    esac
    case "$payload" in
        *$'\t'*)
            interp=${payload%%$'\t'*}
            marker=${payload#*$'\t'}
            [ "$marker" = "-3" ] || return 1
            ;;
        *)
            interp=$payload
            marker=""
            ;;
    esac
    [ -n "$interp" ] || return 1
    case "$interp" in
        /*) ;;
        *) return 1 ;;
    esac
    # A cache record must name the discovered interpreter (python.exe/py.exe),
    # never its GUI twin (the swap is exec-only, see _maybe_swap_to_pythonw).
    # A record ending in /pythonw.exe or /pyw.exe is either poisoned or a stale
    # artefact of a broken twin that should have been probed out -- reject and
    # let discovery re-run, rather than exec'ing a possibly-dead twin directly.
    case "$interp" in
        */pythonw.exe|*/pyw.exe) return 1 ;;
    esac
    [ -x "$interp" ] && [ -s "$interp" ] || return 1
    # A cache entry never bypasses the anti-PATH-hijack policy.
    _is_safe_prefix "$interp" || return 1
    # Windows: prefer the GUI-subsystem pythonw.exe twin at exec time so a
    # cached python.exe record does not keep flashing. No-op off-Windows.
    _maybe_swap_to_pythonw "$interp"
    interp=$_PYW_INTERP
    [ -n "$interp" ] || return 1
    [ -x "$interp" ] && [ -s "$interp" ] || return 1
    _is_safe_prefix "$interp" || return 1

    if [ "$marker" = "-3" ]; then
        exec "$interp" -3 "$@"
        return 1
    fi
    exec "$interp" "$@"
}

_write_interpreter_cache() {
    local interp="$1" marker="$2" cache_tmp

    [ -n "$_PY_CACHE_FILE" ] || return 0
    cache_tmp="${_PY_CACHE_FILE}.tmp.$$"
    if [ "$marker" = "-3" ]; then
        (umask 077; set -C; printf 'INTERP\t%s\t-3\n' "$interp" > "$cache_tmp" &&
            mv -f "$cache_tmp" "$_PY_CACHE_FILE") 2>/dev/null || :
    else
        (umask 077; set -C; printf 'INTERP\t%s\n' "$interp" > "$cache_tmp" &&
            mv -f "$cache_tmp" "$_PY_CACHE_FILE") 2>/dev/null || :
    fi
    return 0
}

_exec_discovered_interpreter() {
    local interp="$1" marker="$2"
    shift 2

    # Discovery already applied the safe-prefix check. Re-applying it here
    # makes that invariant explicit at the shared cache-write/exec boundary.
    _is_safe_prefix "$interp" || return 1
    _write_interpreter_cache "$interp" "$marker"
    # Windows: prefer the GUI-subsystem pythonw.exe twin at exec time. The
    # cache record keeps naming python.exe (the discovered interpreter), and
    # the swap is reapplied on every exec -- including cache hits -- so a
    # stale record never freezes us on the flashing python.exe. No-op
    # off-Windows.
    _maybe_swap_to_pythonw "$interp"
    interp=$_PYW_INTERP
    [ -n "$interp" ] || return 1
    [ -x "$interp" ] && [ -s "$interp" ] || return 1
    _is_safe_prefix "$interp" || return 1
    if [ "$marker" = "-3" ]; then
        exec "$interp" -3 "$@"
    fi
    exec "$interp" "$@"
}

# Explicit user override. pyenv / asdf / conda / venv interpreters, and Codex
# Desktop's bundled runtime, live outside the PATH-hijack safe-prefix allow-list
# and are otherwise rejected. Naming one in TOKEN_OPTIMIZER_PYTHON is an explicit
# trust decision by the user, so it bypasses the allow-list, but we still verify
# it is genuinely a Python 3 before exec so a stale/broken/wrong override degrades
# instead of wedging. The compound `if` keeps set -e from aborting when the probe
# fails.
#
# The probe MUST be Python-specific. A bare `-c ''` is not: POSIX shells
# (/bin/sh, bash, dash, ksh) also accept `-c ''` and exit 0, so a probe of `''`
# would pass for a shell, then `exec /bin/sh run.py` parses run.py as shell and
# exits non-zero (2) -- a BLOCKING hook, the exact wedge this file exists to
# prevent. `import sys; ...` fails on any shell (`import: not found`) and only a
# real Python 3 (version_info[0] >= 3) exits 0, so only Python is ever exec'd.
if [ -n "${TOKEN_OPTIMIZER_PYTHON:-}" ]; then
    _ov="$TOKEN_OPTIMIZER_PYTHON"
    if [ -x "$_ov" ] && [ -s "$_ov" ] && \
       "$_ov" -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
        exec "$_ov" "$@"
    fi
    # `|| :` so a failed write (stderr closed) cannot abort under `set -e` before
    # we fall through to normal discovery.
    echo "token-optimizer: TOKEN_OPTIMIZER_PYTHON=$_ov is not a working Python 3; ignoring it" >&2 || :
fi

_setup_interpreter_cache
_exec_cached_interpreter "$@" || :

# F2 (#107): decide whether a WindowsApps candidate is a real Store install
# or a dead AppExecutionAlias stub -- WITHOUT flashing a console window on
# the common path. The old probe ran the console-subsystem `python.exe
# --version` on every cache miss, which is exactly the flash this launcher
# exists to prevent.
#
# Design (revised for #143): the CANDIDATE ITSELF must supply POSITIVE PROOF
# OF LIFE by writing a marker to a temp file whose content we then require. A
# dead AppExecutionAlias stub exits without writing it. We do NOT trust a bare
# exit code (a dead alias can exit 0 silently, see measure.py), and we do NOT
# trust a sibling "twin" (pythonw.exe): #143 proved that in WindowsApps each
# alias name is claimed independently, so a LIVE pythonw twin can sit beside a
# DEAD python3.exe from a different package -- the old twin probe then cached
# and exec'd the dead stub on every hook. Only the candidate's own liveness
# decides now.
#
# Flash tradeoff (was #107's concern): the twin was GUI-subsystem, so probing
# it never flashed a console; probing the console candidate directly can flash
# a window ONCE on a cache miss. That regression is accepted deliberately -- a
# silently-dead cached interpreter breaks EVERY hook for affected users, which
# is far worse than a one-time flash that only occurs on the rare cache-miss
# path (the result is cached immediately after). The old code already ran the
# console candidate directly in its `--version` fallback, so invoking the
# candidate here is not a new class of behavior.
#
# Two-tier ladder (both key on the CANDIDATE, never a twin):
#   Tier 1 -- write-marker: candidate writes "ok" via -c -> alive (fast positive).
#   Tier 2 -- --version OUTPUT (not exit code): a live interpreter prints
#     "Python X.Y.Z"; a dead redirector exits 0 but prints a localized "not found /
#     install from Store" message with no version number. Matching the version
#     STRING is what makes this safe -- trusting the bare exit code was the original
#     false-pass. Tier 2 both (a) rescues a live interpreter that could not write the
#     marker (AV-blocked or unwritable temp, cold start, cygpath path mismatch) and
#     (b) rejects a dead stub that reached here because Tier 1 could not run. There
#     is no remaining exit-code-only fallback, so no surviving Store-stub false-pass.
#
# The temp path is handed to the candidate as a native Windows path via cygpath
# when available, so the probe is immune to MSYS argv path-conversion settings
# (MSYS_NO_PATHCONV). Probe stdin comes from /dev/null so the hook's real stdin
# is never consumed. C6 timeout semantics (2s budget, SIGKILL escalation 1s
# after SIGTERM) apply to the probe and the fallback. Strict POSIX no-op is
# preserved: this function is only reached for paths matching
# _path_contains_windowsapps, exactly like the old inline probe.
_probe_windowsapps_candidate() {
    local binpath="$1" probe_tmp probe_arg out ver
    # #143: probe the CANDIDATE ITSELF, never a sibling "twin". In WindowsApps each
    # App Execution Alias name is claimed independently, so pythonw.exe can resolve
    # to a DIFFERENT, live package (e.g. the Python Install Manager) while this
    # candidate (python3.exe) is a dead Microsoft Store redirector. The old code
    # proof-of-life-probed the twin, so a live twin masked a dead candidate and the
    # dead stub got cached and exec'd on every hook. Only the candidate's own
    # liveness may decide.
    #
    # Safe-prefix and a working `timeout` gate the WHOLE function, so both tiers sit
    # under the same guards: never exec an untrusted path, and never run the
    # candidate UNBOUNDED (a blocking/half-broken stub would otherwise hang every
    # hook forever). Without `timeout` we fail closed -- return 1 so discovery
    # advances to the next candidate -- rather than risk an unbounded spawn.
    _is_safe_prefix "$binpath" || return 1
    command -v timeout >/dev/null 2>&1 || return 1
    # Tier 1 (fast positive): the candidate writes a proof-of-life marker via -c. A
    # live interpreter writes "ok"; a dead redirector ignores -c and writes nothing.
    # mktemp already creates a fresh empty 0600 file, so no pre-truncation is needed.
    probe_tmp=$(mktemp "${TMPDIR:-/tmp}/token-optimizer-pyprobe.XXXXXX" 2>/dev/null) || probe_tmp=""
    if [ -n "$probe_tmp" ]; then
        probe_arg=$(cygpath -w "$probe_tmp" 2>/dev/null) || probe_arg=""
        [ -n "$probe_arg" ] || probe_arg="$probe_tmp"
        timeout --kill-after=1s 2s "$binpath" -c \
            'import sys; open(sys.argv[1], "w").write("ok")' \
            "$probe_arg" </dev/null >/dev/null 2>&1 || :
        out=$(cat "$probe_tmp" 2>/dev/null) || out=""
        rm -f "$probe_tmp" 2>/dev/null || :
        if [ "$out" = "ok" ]; then
            return 0   # definitive: the candidate itself is a live interpreter
        fi
    fi
    # Tier 2 (discriminating fallback): trust the candidate's --version OUTPUT, never
    # its exit code. A dead Store redirector exits 0 (the old false pass) but prints a
    # "not found / install from Store" banner; a live interpreter LEADS with
    # "Python X.Y.Z". Matching the version string -- not the exit code -- closes BOTH
    # failure modes the marker probe alone left open:
    #   * a LIVE interpreter that merely could not write the marker (AV/DLP-blocked
    #     temp, cold start over budget, cygpath path mismatch, no writable temp) is
    #     still accepted here instead of being wrongly rejected; and
    #   * a DEAD stub reached because Tier 1 could not run (no writable temp) is
    #     rejected here instead of false-passing on a bare exit-0.
    # Discriminator is hardened against a LOCALIZED banner that names a version to
    # install (e.g. "Python 3 was not found; install from the Microsoft Store"):
    # reject the banner tokens FIRST, then require the version to LEAD the output
    # (head-anchored, no leading '*'), so a version buried in a sentence cannot pass.
    # 2>&1 because CPython has historically printed --version to stderr.
    ver=$(timeout --kill-after=1s 2s "$binpath" --version </dev/null 2>&1) || :
    case "$ver" in
        *[Nn]ot' '[Ff]ound*|*[Ii]nstall*|*[Ss]tore*|*Microsoft*) return 1 ;;
        [Pp]ython' '[0-9]*) return 0 ;;   # version string LEADS the output
        *) return 1 ;;
    esac
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
            # C8: case-insensitive WindowsApps detection (any casing on
            # case-insensitive FS). WindowsApps may contain real Store-installed
            # Python OR non-functional AppExecutionAlias stubs (non-zero-byte,
            # pass -s). F2 (#107): distinguish them flash-free via the GUI
            # twin's proof-of-life probe, with the console --version probe as
            # the fallback authority -- see _probe_windowsapps_candidate.
            if _path_contains_windowsapps "$binpath"; then
                _probe_windowsapps_candidate "$binpath" || continue
            fi
            printf "%s\n" "$binpath"
            return 0
        done
    done
    return 1
}

if py3=$(find_interpreter "python3"); then
    _exec_discovered_interpreter "$py3" "" "$@"
fi

if py=$(find_interpreter "python"); then
    _exec_discovered_interpreter "$py" "" "$@"
fi

if pyl=$(find_interpreter "py"); then
    _exec_discovered_interpreter "$pyl" "-3" "$@"
fi

# Direct probe: hook environments often have a stripped PATH that excludes
# the user's Python. Check known locations directly as a fallback.
for _direct in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 \
               /home/linuxbrew/.linuxbrew/bin/python3; do
    if [ -x "$_direct" ] && [ -s "$_direct" ] && _is_safe_prefix "$_direct"; then
        _exec_discovered_interpreter "$_direct" "" "$@"
    fi
done

# HOOK-SAFETY: we are now on the terminal degradation path and WILL `exit 0`.
# Disable `set -e` first so NOTHING below can abort non-zero before that exit --
# in particular a diagnostic `echo >&2` when the harness invoked the hook with
# stderr closed: under `set -e` the failed write aborts with a non-zero status,
# which is a blocking hook. `set +e` makes the whole exit path unconditional.
set +e
echo "token-optimizer: no usable Python 3 interpreter found" >&2
echo "  tried: python3, python, py -3, direct paths, TOKEN_OPTIMIZER_PYTHON" >&2
echo "  fix: set TOKEN_OPTIMIZER_PYTHON=/path/to/python3 (pyenv/asdf/conda/venv are fine)," >&2
echo "       or install Python 3 from https://python.org/ , then restart your agent" >&2
# HOOK-SAFETY (never remove): this launcher is invoked ONLY from hook wrappers in
# hooks/hooks.json. A hook MUST NOT exit non-zero. Codex and Claude Code treat a
# non-zero PreToolUse hook as a tool-blocking failure, so a missing interpreter
# here would fail on EVERY Read and Bash and freeze the session ("shell runner
# unavailable"). The `; exit 0` in the wrapper is dead code once `exec` replaces
# the shell, so the guarantee has to live here. Degrade cleanly instead: skip the
# optimization and let the tool proceed. Diagnostics above went to stderr.
exit 0
