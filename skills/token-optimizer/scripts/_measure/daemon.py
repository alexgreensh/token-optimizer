"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _parse_elapsed_time(elapsed_str):
    """Parse ps elapsed time format (dd-HH:MM:SS or HH:MM:SS or MM:SS) to seconds."""
    _measure_facade = _compat.facade()
    elapsed_str = elapsed_str.strip()
    days = 0
    if "-" in elapsed_str:
        parts = elapsed_str.split("-", 1)
        days = int(parts[0])
        elapsed_str = parts[1]

    parts = elapsed_str.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = int(parts[0]), int(parts[1])
    else:
        return 0

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _format_elapsed(seconds):
    """Format seconds into a human-readable elapsed string."""
    _measure_facade = _compat.facade()
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours = seconds // 3600
    if hours < 24:
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m"
    d = hours // 24
    h = hours % 24
    return f"{d}d {h}h"


def _scan_project_jsonl_stats():
    """Return [(path, birth_time, mtime), ...] for every project JSONL, cached.

    The full-tree stat walk of ~/.claude/projects is the dominant cost of version
    detection on a large history (tens of thousands of transcripts): a cold stat
    of every file. _find_session_version_for_pid runs once per live Claude process,
    so a dashboard gen with several running sessions repeated the identical walk N
    times -- an N-file stat storm that blows past the 45s regen-step budget on a
    cold cache. Compute it once and share it across those calls. A 30s TTL keeps
    the long-lived daemon fresh without re-walking on every call.
    """
    _measure_facade = _compat.facade()
    pass
    # monotonic() so NTP skew can't stretch or collapse the TTL window.
    if (_measure_facade._PROJECT_JSONL_STAT_CACHE is not None
            and (_measure_facade.time.monotonic() - _measure_facade._PROJECT_JSONL_STAT_CACHE_TS) < _measure_facade._PROJECT_JSONL_STAT_TTL):
        return _measure_facade._PROJECT_JSONL_STAT_CACHE
    out = []
    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if projects_base.exists():
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            for jf in project_dir.glob("*.jsonl"):
                try:
                    st = jf.stat()
                    birth = getattr(st, "st_birthtime", st.st_ctime)
                    out.append((jf, birth, st.st_mtime))
                except (PermissionError, OSError):
                    continue
    _measure_facade._PROJECT_JSONL_STAT_CACHE = out
    # Stamp AFTER the walk finishes, not before: on a large cold history the walk
    # itself can exceed the TTL. Stamping the pre-walk time would make the cache
    # born already-expired, so the very next per-PID call re-walks and the memoization
    # is a no-op in exactly the cold-cache case this exists to fix.
    _measure_facade._PROJECT_JSONL_STAT_CACHE_TS = _measure_facade.time.monotonic()
    return out


def _find_session_version_for_pid(pid):
    """Try to find the Claude Code version for a running process by matching its session JSONL.

    We look for JSONL files whose first message timestamp is close to the
    process start time. For long-running sessions, we also check file birth
    time (macOS) or creation time as a secondary signal.
    """
    _measure_facade = _compat.facade()
    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None

    # Get process start time for correlation
    try:
        result = _measure_facade.subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
            # Force C locale so ps emits English lstart regardless of host
            # locale (e.g. he_IL.UTF-8 emits Hebrew month names).
            env={**_measure_facade.os.environ, "LC_ALL": "C", "LC_TIME": "C"}, creationflags=_measure_facade._NO_WINDOW,
        )
        if result.returncode != 0:
            return None
        lstart_str = result.stdout.strip()
        # Parse "Fri Feb 27 10:18:43 2026"
        proc_start = _measure_facade.datetime.strptime(lstart_str, "%a %b %d %H:%M:%S %Y")
        proc_start_ts = proc_start.timestamp()
    except (_measure_facade.subprocess.SubprocessError, ValueError, OSError):
        return None

    # Find JSONL files whose creation or first-record timestamp matches process start
    best_match = None
    best_diff = float("inf")

    for jf, birth_time, mtime in _measure_facade._scan_project_jsonl_stats():
        try:
            # Skip files created well before or well after the process
            if birth_time < proc_start_ts - 60 and mtime < proc_start_ts - 60:
                continue

            # Read first 10 lines for version and timestamp
            version_found = None
            with open(jf, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f):
                    if line_num > 10:
                        break
                    try:
                        record = _measure_facade.json.loads(line)
                        v = record.get("version")
                        if v and not version_found:
                            version_found = v
                        ts_str = record.get("timestamp")
                        if not ts_str:
                            continue
                        ts = _measure_facade.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                        diff = abs((ts - proc_start).total_seconds())
                        if diff < best_diff and version_found:
                            best_diff = diff
                            best_match = version_found
                    except (_measure_facade.json.JSONDecodeError, ValueError):
                        continue

            # Also try correlating birth time to process start
            birth_diff = abs(birth_time - proc_start_ts)
            if birth_diff < best_diff and version_found:
                best_diff = birth_diff
                best_match = version_found

        except (PermissionError, OSError):
            continue

    # Return if we found a reasonable match (within 10 minutes of start)
    if best_match and best_diff < 600:
        return best_match
    return None  # No confident match; don't guess (causes false OUTDATED flags)


def _command_matches_process(command, process_name):
    """True if a `ps` COMMAND field denotes a `process_name` CLI session.

    Matches a bare ``argv[0]`` (``claude`` / ``claude --resume ...``) and
    also absolute-path or wrapper launches -- e.g.
    ``/usr/local/.../@anthropic-ai/claude-code/bin/claude`` and the patched
    ``.../bin/claude.exe ...`` form -- by comparing the executable basename
    with a trailing ``.exe`` stripped. The bare-name fast path is preserved
    for backward compatibility. Matching is case-sensitive so the macOS
    desktop app (``/Applications/Claude.app/Contents/MacOS/Claude``) is not
    mistaken for the ``claude`` CLI, and ``claude`` appearing only as an
    argument (``vim claude.py``) does not match.

    Claude Desktop's "Code" tab launches Claude Code through a remote
    launcher whose ``argv[0]`` is a VERSIONED binary under
    ``~/.claude/remote/ccd-cli/<version>`` (e.g.
    ``~/.claude/remote/ccd-cli/2.1.271 --output-format stream-json ...``),
    notably under WSL 2 where ``claude`` is not on ``PATH``. The executable
    basename is then the bare version string, so the basename comparison can
    never match. Recognise that launcher by an anchored, version-shaped
    ``argv[0]`` match (``.../.claude/remote/ccd-cli/<version>``): a bare
    substring test was too loose and also swept in bundled binaries under the
    version dir (``.../ccd-cli/<version>/rg``), helper scripts
    (``.../ccd-cli/helper.sh``), ``.download`` partials, and the bare directory,
    inflating the session count. Restricted to ``process_name == "claude"``
    (Codex is not launched this way) and to ``argv[0]`` (so the path appearing
    only as an argument does not match), which keeps the match from catching
    unrelated processes. See issue #192.
    """
    _measure_facade = _compat.facade()
    command = (command or "").strip()
    if not command:
        return False
    if command == process_name or command.startswith(process_name + " "):
        return True
    argv0 = command.split()[0]
    if process_name == "claude" and _measure_facade._CCD_CLI_LAUNCHER_RE.search(argv0):
        return True
    exe_base = _measure_facade.os.path.basename(argv0)
    if exe_base.endswith(".exe"):
        exe_base = exe_base[:-4]
    return exe_base == process_name


def _posix_comm_name(comm):
    """Lower-case executable name from a ps COMM field ("-zsh" -> "zsh",
    "tmux: server" -> "tmux", "/bin/bash" -> "bash"). Never raises."""
    _measure_facade = _compat.facade()
    text = (comm or "").strip()
    if not text:
        return ""
    text = text.split(": ", 1)[0].rsplit("/", 1)[-1]
    return text.lstrip("-").strip().lower()


def _posix_comm_is_claude(comm):
    """True when a COMM field names a claude CLI executable (basename `claude`,
    `.exe` stripped, or the versioned ccd-cli launcher). Matches desktop-hosted
    sessions installed under a path with spaces (`.../Application Support/...`),
    which the whitespace-split `ps` command column cannot show."""
    _measure_facade = _compat.facade()
    text = (comm or "").strip()
    if not text:
        return False
    base = text.rsplit("/", 1)[-1]
    if base.endswith(".exe"):
        base = base[:-4]
    return base == "claude" or bool(_measure_facade._CCD_CLI_LAUNCHER_RE.search(text))


def _posix_is_claude_host(comm, args):
    """True when a process is the Claude desktop app (main, helper, disclaimer
    wrapper, Electron build) or another claude process: a claude below it is hosted."""
    _measure_facade = _compat.facade()
    if _measure_facade._posix_comm_name(comm).startswith("claude"):
        return True
    low = f"{comm or ''}\n{args or ''}".lower()
    if "claude.app/" in low or "/.claude/remote/ccd-cli/" in low or "@anthropic-ai/claude-code" in low:
        return True
    if "app.asar" in low and "claude" in low:
        return True
    return _measure_facade._command_matches_process(args or "", "claude")


def _posix_option_args(argv):
    """Arguments that can be options: everything after argv[0] up to a bare --."""
    _measure_facade = _compat.facade()
    args = list(argv[1:])
    return args[:args.index("--")] if "--" in args else args


def _posix_is_headless(options):
    """True for SDK/headless/IDE switches and for subcommand processes."""
    _measure_facade = _compat.facade()
    for raw in options:
        a = raw.lower()
        if len(a) > 1 and a[0] == "-" and a[1] != "-" and "p" in a.split("=")[0]:
            return True  # -p, -pc ...
        if any(a == f or a.startswith(f + "=") for f in _measure_facade._WIN_HEADLESS_FLAGS):
            return True
        if a in _measure_facade._WIN_HEADLESS_SUBCOMMANDS:
            return True
    return False


def _posix_read_argv(pid):
    """Exact argv of a process, or None when the OS will not say.

    Linux reads NUL-separated /proc/<pid>/cmdline; macOS asks the kernel
    (sysctl KERN_PROCARGS2). Unlike the `ps` command column, both keep an
    argument that contains spaces as ONE token, so prompt text can neither
    masquerade as a switch nor hide one. Callers fall back to the `ps` column.
    """
    _measure_facade = _compat.facade()
    try:
        pid = int(pid)
        if _measure_facade.sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                data = fh.read()
            if not data:
                return None
            parts = data.split(b"\0")
            while parts and parts[-1] == b"":
                parts.pop()  # trailing NUL, and the padding left by a rewritten title
            return [p.decode("utf-8", "replace") for p in parts] or None
        if _measure_facade.sys.platform == "darwin":
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 8:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
                return None
            data = buf.raw[:size.value]
            argc = int.from_bytes(data[:4], _measure_facade.sys.byteorder)
            rest = data[4:]
            i = rest.index(b"\0")  # exec_path
            while i < len(rest) and rest[i] == 0:
                i += 1
            parts = rest[i:].split(b"\0")[:argc]
            if argc < 1 or len(parts) < argc:
                return None
            return [p.decode("utf-8", "replace") for p in parts]
    except Exception:
        return None
    return None


def _posix_exe_path(pid, comm=None):
    """Real executable path of a process, or None.

    /proc/<pid>/exe on Linux (a replaced binary reads "<path> (deleted)", whose
    directory is still the right one to probe), proc_pidpath on macOS. A native
    install deletes the previous version's binary after an update, so a
    long-running session can legitimately have no resolvable path; callers treat
    None as "no directory evidence", not as a reason to fail.
    """
    _measure_facade = _compat.facade()
    try:
        pid = int(pid)
        if _measure_facade.sys.platform.startswith("linux"):
            target = _measure_facade.os.readlink(f"/proc/{pid}/exe")
            return target[:-len(" (deleted)")] if target.endswith(" (deleted)") else target
        if _measure_facade.sys.platform == "darwin":
            import ctypes
            import ctypes.util

            lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
            buf = ctypes.create_string_buffer(4096)
            if lib.proc_pidpath(pid, buf, 4096) > 0:
                return buf.value.decode("utf-8", "replace")
    except Exception:
        pass
    return comm if comm and comm.startswith("/") else None


def _posix_dir_is_electron_app(exe_path):
    """True/False when the executable's directory is/is not an Electron app
    directory; None when that cannot be determined (fail closed upstream)."""
    _measure_facade = _compat.facade()
    if not exe_path:
        return None
    base = _measure_facade.os.path.dirname(exe_path)
    # os.path.exists swallows every OSError and reports False, which would turn
    # an unreadable directory into "not Electron". Probe with os.stat so that
    # anything other than "definitely absent" is indeterminate.
    try:
        _measure_facade.os.stat(base or ".")
    except (OSError, ValueError):
        return None
    probes = [_measure_facade.os.path.join(base, *marker) for marker in _measure_facade._WIN_ELECTRON_MARKERS]
    if _measure_facade.os.path.basename(base) == "MacOS" and _measure_facade.os.path.basename(_measure_facade.os.path.dirname(base)) == "Contents":
        contents = _measure_facade.os.path.dirname(base)  # <App>.app/Contents
        probes.append(_measure_facade.os.path.join(contents, "Resources", "app.asar"))
        probes.append(_measure_facade.os.path.join(contents, "Frameworks", "Electron Framework.framework"))
    found = False
    for probe in probes:
        try:
            _measure_facade.os.stat(probe)
            found = True
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError):
            return None
    return found


def _posix_is_init_parent(ppid, names, args_by_pid):
    """True when ppid is the init process an orphan is reparented to: pid 1
    (launchd, systemd, init) or a per-user `systemd --user` subreaper."""
    _measure_facade = _compat.facade()
    if ppid == 1:
        return True
    entry = names.get(ppid)
    if not entry or _measure_facade._posix_comm_name(entry[1]) != "systemd":
        return False
    return "--user" in (args_by_pid.get(ppid) or "").split()


def _posix_ancestry_state(pid, names, args_by_pid, electron_parent_pids=None):
    """Walk parents of pid. Returns "hosted", "complete" or "incomplete".

    "complete" means the chain provably reached init/launchd (or a pid-namespace
    root) without meeting the desktop app or another claude process. Missing
    rows, cycles and over-long chains are "incomplete": absence of evidence is
    never a negative host check.
    """
    _measure_facade = _compat.facade()
    seen = {pid}
    cur = pid
    for _ in range(_measure_facade._POSIX_ANCESTRY_MAX_HOPS):
        entry = names.get(cur)
        if not entry:
            return "incomplete"
        ppid = entry[0]
        if ppid <= 0:
            return "complete"
        if ppid in seen:
            return "incomplete"
        parent = names.get(ppid)
        if not parent:
            return "incomplete"
        if ppid in electron_parent_pids or _measure_facade._posix_is_claude_host(parent[1], args_by_pid.get(ppid)):
            return "hosted"
        if ppid == 1:
            return "complete"
        seen.add(ppid)
        cur = ppid
    return "incomplete"


def _classify_posix_claude_process(detail, names, args_by_pid, electron_parent_pids=None):
    """Classify one claude process on macOS/Linux by identity.

    Same classes as `_classify_windows_claude_process`:
    - "helper":           Electron child (--type=...). Never a session.
    - "desktop_app":      The Electron main process of the desktop app (it has
                          --type= children, or an Electron directory layout).
                          Not a session; never terminable.
    - "embedded_session": A Claude Code process hosted by another program
                          (desktop app, IDE, SDK, a parent claude) or running
                          headless (--print, stream-json, mcp ...). A real
                          session owned by its host: never terminated by age.
    - "terminal_cli":     Positively identified interactive terminal process:
                          readable argv, no host/headless markers, a complete
                          parent chain, a controlling TTY and a parent that is a
                          shell, multiplexer, sshd or terminal. The only identity
                          kill_stale_sessions may terminate.
    - "orphan_cli":       An interactive session whose terminal died. ALL of:
                          exact argv was read (KERN_PROCARGS2 or /proc cmdline,
                          never the ps-split fallback), no --type=/headless
                          flag/subcommand, no controlling TTY, the parent is the
                          init process (pid 1 or `systemd --user`), and the
                          executable is positively not an Electron app. Listed
                          as ORPHAN; only `kill-stale --include-orphans` ends it.
                          Anything short of that stays "unknown". Windows has no
                          such class (parent-pid semantics differ).
    - "unknown":          Anything else, including every case where identity
                          evidence could not be read (unreadable ps, undecodable
                          text, an orphan short of the conditions above). Listed,
                          never terminated.

    detail: pid, ppid, tty, argv (list), argv_exact (bool), exe (path or None).
    names: {pid: (ppid, comm)} or None; args_by_pid: {pid: ps args string}.
    """
    _measure_facade = _compat.facade()
    if not detail:
        return "unknown"
    argv = detail.get("argv")
    if not argv:
        return "unknown"
    exe = detail.get("exe") or ""
    if "�" in exe or any("�" in a for a in argv):
        return "unknown"  # undecodable text is never affirmative evidence
    # One token holding spaces is either a rewritten process title or a spaced
    # install path: the two cannot be told apart, so it is not exact argv.
    argv_exact = bool(detail.get("argv_exact")) and (len(argv) > 1 or len(argv[0].split()) == 1)
    if len(argv) == 1:
        # A process that rewrote its title carries its whole command line in
        # argv[0]; split it so a switch inside cannot hide.
        argv = argv[0].split() or argv
    options = _measure_facade._posix_option_args(argv)  # tokens after a bare -- are prompt text
    if any(a.lower().startswith("--type=") for a in options):
        return "helper"
    pid = detail.get("pid")
    if pid in electron_parent_pids:
        return "desktop_app"
    electron_dir = None
    if exe:
        electron_dir = _measure_facade._posix_dir_is_electron_app(exe)
        if electron_dir is None:
            # Positive headless evidence (-p, --print, mcp ...) is never
            # killable either way, so it names the session even when the
            # executable's directory cannot be inspected.
            return "embedded_session" if _measure_facade._posix_is_headless(options) else "unknown"
        if electron_dir:
            return "desktop_app"
    if not names:
        return "unknown"
    ancestry = _measure_facade._posix_ancestry_state(pid, names, args_by_pid, electron_parent_pids)
    if ancestry == "hosted":
        return "embedded_session"
    if ancestry != "complete":
        return "unknown"
    if _measure_facade._posix_is_headless(options):
        return "embedded_session"
    entry = names.get(pid)
    if not entry or detail.get("ppid") != entry[0]:
        return "unknown"  # the two snapshots disagree about the parent
    if (detail.get("tty") or "?") in _measure_facade._POSIX_NO_TTY:
        # No controlling terminal. Under a shell that is just a process that
        # lost its tty (unknown). Under init it is the zombie `health` exists
        # for, but only on exact argv and a binary positively not Electron
        # (an unresolvable executable gives no such evidence).
        if argv_exact and electron_dir is False and _measure_facade._posix_is_init_parent(entry[0], names, args_by_pid):
            return "orphan_cli"
        return "unknown"
    parent = names.get(entry[0])
    if parent and _measure_facade._posix_comm_name(parent[1]) in _measure_facade._POSIX_TERMINAL_PARENTS:
        return "terminal_cli"
    return "unknown"


def _posix_process_names():
    """Return {pid: (ppid, comm)} for every process, or None when unreadable.

    COMM is the last column because it may contain spaces ("Claude Helper
    (Renderer)"); on macOS it is the full executable path. None means the
    parent chain cannot be established: callers treat every process as
    unverified, never as a confirmed terminal session.
    """
    _measure_facade = _compat.facade()
    try:
        result = _measure_facade.subprocess.run(
            ["ps", "-ww", "-eo", "pid,ppid,comm"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            env={**_measure_facade.os.environ, "LC_ALL": "C", "LC_TIME": "C"}, creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    names = {}
    for line in result.stdout.splitlines():
        m = _measure_facade._POSIX_NAME_ROW_RE.match(line)
        if m:
            names[int(m.group(1))] = (int(m.group(2)), m.group(3))
    return names or None


def _posix_ancestor_pids(pid):
    """Pids of every ancestor of `pid`, or None when the chain cannot be established.

    kill-stale runs inside the conversation it was asked from; that claude is an
    ancestor of the command, not its direct parent, so it must be excluded too.
    None (not an empty set) when `ps` is unreadable or its table lacks `pid`:
    "unknown" is a distinct answer from "no ancestors", and kill-stale must not
    act without proof that a candidate is not the conversation it runs inside of.
    """
    _measure_facade = _compat.facade()
    names = _measure_facade._posix_process_names()
    if not names or pid not in names:
        return None
    out = set()
    cur = pid
    for _ in range(64):
        entry = names.get(cur)
        if not entry or entry[0] <= 0 or entry[0] in out:
            break
        out.add(entry[0])
        cur = entry[0]
    return out


def _collect_posix_claude_sessions(process_name=None):
    """Collect running Claude/Codex CLI sessions via `ps` on macOS/Linux.

    Returns a list of session dicts. Returns None if `ps` itself raises a
    subprocess or OS error -- the caller treats None as "health check
    unavailable" to preserve the historical POSIX contract. A non-zero
    `ps` exit with no sessions found returns `[]`.

    For `claude`, every session carries ``identity`` ("terminal_cli",
    "orphan_cli", "embedded_session" or "unknown") and ``identity_source: "ps"``; Electron
    helpers and the desktop app's own process are dropped. See
    `_classify_posix_claude_process`. Codex inventories are untagged: the Codex
    path never terminates by age (see `_collect_health_data`/`kill_stale_sessions`).
    """
    _measure_facade = _compat.facade()
    try:
        result = _measure_facade.subprocess.run(
            ["ps", "-ww", "-eo", "pid,ppid,uid,tty,lstart,etime,command"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            # Force C locale so lstart is always English 5-field format. Under
            # non-English locales (e.g. he_IL.UTF-8) ps emits localized dates
            # with a different field count, breaking the positional parse below
            # and dropping every session.
            env={**_measure_facade.os.environ, "LC_ALL": "C", "LC_TIME": "C"}, creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, ValueError):
        return None
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        m = _measure_facade._POSIX_PS_ROW_RE.match(line)
        if not m:
            continue  # header and anything that is not a process row
        rows.append({
            "pid": int(m.group(1)), "ppid": int(m.group(2)),
            "uid": int(m.group(3)) if m.group(3) is not None else None, "tty": m.group(4),
            "lstart": " ".join(m.group(5).split()), "etime": m.group(6), "args": m.group(7),
        })
    claude = process_name == "claude"
    names = _measure_facade._posix_process_names() if claude else None
    args_by_pid = {r["pid"]: r["args"] for r in rows}

    # Parents of claude-named Electron children (--type=...) are the desktop
    # app's main process, whatever their own name or directory layout.
    electron_parent_pids = set()
    if claude and names:
        for r in rows:
            if r["ppid"] > 0 and any(t.lower().startswith("--type=") for t in _measure_facade._posix_option_args(r["args"].split())):
                comm = (names.get(r["pid"]) or (0, ""))[1]
                if _measure_facade._posix_is_claude_host(comm, r["args"]):
                    electron_parent_pids.add(r["ppid"])

    sessions = []
    for r in rows:
        comm = (names.get(r["pid"]) or (0, ""))[1] if names else ""
        command = r["args"]
        if not (_measure_facade._command_matches_process(command, process_name)
                or (claude and _measure_facade._posix_comm_is_claude(comm))):
            continue
        pid = r["pid"]
        tty = r["tty"]
        elapsed_seconds = _measure_facade._parse_elapsed_time(r["etime"])
        has_terminal = tty not in _measure_facade._POSIX_NO_TTY
        identity = None
        if claude:
            argv = _measure_facade._posix_read_argv(pid)
            argv_exact = bool(argv)
            if not argv:
                # ps joins argv with spaces: still safe (a switch can never be
                # hidden by splitting), merely coarser. On macOS COMM is argv[0]
                # exactly, which keeps a spaced install path in one token.
                if comm.startswith("/") and command.startswith(comm):
                    argv = [comm] + command[len(comm):].split()
                else:
                    argv = command.split()
            exe = _measure_facade._posix_exe_path(pid, comm)
            detail = {"pid": pid, "ppid": r["ppid"], "tty": tty, "argv": argv,
                      "argv_exact": argv_exact, "exe": exe}
            identity = _measure_facade._classify_posix_claude_process(detail, names, args_by_pid, electron_parent_pids)
            if identity in ("helper", "desktop_app"):
                continue
        session = {
            "pid": pid,
            "started": r["lstart"],
            "elapsed_seconds": elapsed_seconds,
            "elapsed_human": _measure_facade._format_elapsed(elapsed_seconds),
            "command": command,
            "has_terminal": has_terminal,
            "tty": tty if has_terminal else None,
            "uid": r["uid"],
        }
        if identity is not None:
            session["identity"] = identity
            session["identity_source"] = "ps"
        sessions.append(session)
    return sessions


def _local_start_label(started):
    """Format a process start time in local time, as the POSIX collector does.

    A timezone-aware value (the Windows collectors report UTC or an offset) is
    converted to the local zone first; a naive value is already local.
    """
    _measure_facade = _compat.facade()
    if started.tzinfo is not None:
        try:
            started = started.astimezone()
        except (ValueError, OverflowError, OSError):
            pass
    return started.strftime("%a %b %d %H:%M:%S %Y")


def _parse_wmi_datetime(wmi_ts):
    """Parse a WMI CIM_DATETIME to elapsed seconds since process start.

    Format: ``YYYYMMDDHHMMSS.ffffff<sign><MMM>`` where sign is + or - and
    MMM is minutes offset from UTC. Parses the timezone when present and
    uses timezone-aware math so DST transitions can't flip elapsed
    negative. Falls back to naive math if the offset is missing/garbage.

    Returns {"started": human, "elapsed_seconds": int} or None.
    """
    _measure_facade = _compat.facade()
    if not wmi_ts or len(wmi_ts) < 14:
        return None
    try:
        year = int(wmi_ts[0:4])
        month = int(wmi_ts[4:6])
        day = int(wmi_ts[6:8])
        hour = int(wmi_ts[8:10])
        minute = int(wmi_ts[10:12])
        second = int(wmi_ts[12:14])
    except (ValueError, TypeError):
        return None

    # Offset: position 21 is sign char, 22-25 is MMM (may be absent on short strings).
    tz_info = None
    if len(wmi_ts) >= 25 and wmi_ts[21] in ("+", "-"):
        try:
            offset_minutes = int(wmi_ts[22:25])
            if wmi_ts[21] == "-":
                offset_minutes = -offset_minutes
            tz_info = _measure_facade.timezone(_measure_facade.timedelta(minutes=offset_minutes))
        except (ValueError, TypeError):
            tz_info = None

    try:
        if tz_info is not None:
            started = _measure_facade.datetime(year, month, day, hour, minute, second, tzinfo=tz_info)
            elapsed = (_measure_facade.datetime.now(_measure_facade.timezone.utc) - started).total_seconds()
        else:
            started = _measure_facade.datetime(year, month, day, hour, minute, second)
            elapsed = (_measure_facade.datetime.now() - started).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None

    return {
        "started": _measure_facade._local_start_label(started),
        "elapsed_seconds": max(0, int(elapsed)),
    }


def _normalize_ps_iso(iso_ts):
    """Return a string datetime.fromisoformat accepts (3.9+), or "" when empty."""
    _measure_facade = _compat.facade()
    s = (iso_ts or "").strip()
    if not s:
        return ""
    s = _measure_facade._PS_DOTTED_TIME_RE.sub(r"\1:\2:\3", s)
    s = _measure_facade._PS_FRACTION_RE.sub(lambda m: ((_measure_facade := _compat.facade()), "." + m.group(1)[:6].ljust(6, "0"))[1], s)
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return s


def _parse_iso_process_datetime(iso_ts):
    """Parse an ISO 8601 timestamp from PowerShell to elapsed seconds."""
    _measure_facade = _compat.facade()
    if not iso_ts:
        return None
    s = _measure_facade._normalize_ps_iso(iso_ts)
    try:
        started = _measure_facade.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if started.tzinfo is None:
        elapsed = (_measure_facade.datetime.now() - started).total_seconds()
    else:
        elapsed = (_measure_facade.datetime.now(_measure_facade.timezone.utc) - started).total_seconds()
    return {
        "started": _measure_facade._local_start_label(started),
        "elapsed_seconds": max(0, int(elapsed)),
    }


def _windows_process_creation(pid):
    """Return {"started", "elapsed_seconds"} for a Windows PID, or {}.

    Tries `wmic` first (built-in back to Win7). Falls back to PowerShell
    `Get-CimInstance` when wmic is absent (deprecated in Windows 11 24H2+).
    All subprocess calls use errors='replace' so non-ASCII output on
    localized Windows (ja-JP, zh-CN, ru-RU) can't raise UnicodeDecodeError.
    """
    _measure_facade = _compat.facade()
    try:
        result = _measure_facade.subprocess.run(
            ["wmic", "process", "where", f"ProcessId={int(pid)}",
             "get", "CreationDate", "/format:list"],
            capture_output=True, text=True, timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("CreationDate="):
                    parsed = _measure_facade._parse_wmi_datetime(line[len("CreationDate="):])
                    if parsed is not None:
                        return parsed
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    try:
        ps_cmd = (
            "(Get-CimInstance Win32_Process -Filter "
            f"'ProcessId={int(pid)}').CreationDate "
            "| ForEach-Object { $_.ToString('o') }"
        )
        result = _measure_facade.subprocess.run(
            # -ExecutionPolicy Bypass for parity with the other PowerShell
            # spawns: an AllSigned host would otherwise block this per-PID
            # fallback and leave the session flagged UNKNOWN_AGE.
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = _measure_facade._parse_iso_process_datetime(result.stdout.strip())
            if parsed is not None:
                return parsed
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    return {}


def _windows_start_times_agree(process_start, cim_creation, tolerance_seconds=None):
    """True only when Get-Process StartTime and CIM CreationDate match.

    Both come from separate queries joined by PID; a PID reused between them
    would otherwise borrow the old process's start time. Missing or
    unparseable values on either side are not agreement.
    """
    _measure_facade = _compat.facade()
    try:
        a = _measure_facade.datetime.fromisoformat(_measure_facade._normalize_ps_iso(process_start))
        b = _measure_facade.datetime.fromisoformat(_measure_facade._normalize_ps_iso(cim_creation))
        return abs((a - b).total_seconds()) <= tolerance_seconds
    except (ValueError, TypeError, AttributeError):
        return False


def _windows_cim_process_details():
    """Return {pid: {"ppid", "path", "cmdline", "creation"}} for claude* processes, or None.

    Get-Process does not expose the command line on Windows PowerShell 5, so
    this asks Win32_Process. None means identity could not be established
    (PowerShell locked down, CIM unavailable): callers must treat every
    process as unverified, never as a confirmed terminal session.
    """
    _measure_facade = _compat.facade()
    import csv as _csv
    import io as _io

    ps_cmd = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process -Filter 'Name LIKE ''claude%''' "
        "-ErrorAction SilentlyContinue | "
        "Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine, "
        "@{N='CreationDate';E={try { $_.CreationDate.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [System.Globalization.CultureInfo]::InvariantCulture) } catch { '' }}} | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = _measure_facade.subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, encoding="utf-8", timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    rows = _measure_facade._read_strict_csv(result.stdout, ("ProcessId", "ParentProcessId", "ExecutablePath", "CommandLine", "CreationDate"))
    if rows is None:
        return None
    details = {}
    for row in rows:
        try:
            pid = int((row.get("ProcessId") or "").strip())
        except ValueError:
            continue
        try:
            ppid = int((row.get("ParentProcessId") or "").strip())
        except ValueError:
            ppid = None
        details[pid] = {
            "ppid": ppid,
            "path": (row.get("ExecutablePath") or "").strip(),
            "cmdline": (row.get("CommandLine") or "").strip(),
            "creation": (row.get("CreationDate") or "").strip(),
        }
    return details


def _windows_process_names():
    """Return {pid: (ppid, image_name_lower)} for all processes, or None.

    Used for ancestry: a claude.exe is only treated as a terminal session when
    its parent is a known shell/terminal host. None = unavailable.
    """
    _measure_facade = _compat.facade()
    import csv as _csv
    import io as _io

    ps_cmd = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Select-Object ProcessId, ParentProcessId, Name | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = _measure_facade.subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, encoding="utf-8", timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    rows = _measure_facade._read_strict_csv(result.stdout, ("ProcessId", "ParentProcessId", "Name"))
    if rows is None:
        return None
    names = {}
    for row in rows:
        try:
            pid = int((row.get("ProcessId") or "").strip())
            ppid = int((row.get("ParentProcessId") or "").strip())
        except ValueError:
            continue
        names[pid] = (ppid, (row.get("Name") or "").strip().lower())
    return names or None


def _windows_ancestor_pids(pid, names=None):
    """Pids of every ancestor of `pid` from the Windows process table, or None.

    Same walk as `_posix_ancestor_pids`, but None (not an empty set) when the table
    is unreadable, does not contain `pid`, or the chain runs into a parent pid the
    table lacks below a system root: kill-stale must not act without proof that a
    candidate is not the conversation it runs inside of (claude.exe -> shell ->
    python), so "unknown" is a distinct answer from "no ancestors".
    """
    _measure_facade = _compat.facade()
    if names is None:
        names = _measure_facade._windows_process_names()
    if not names or pid not in names:
        return None
    out = set()
    cur = pid
    for _ in range(64):
        entry = names.get(cur)
        if not entry:
            # `cur` is a parent pid the table does not list: its process exited
            # and Windows never reparents, so the link above it is gone. That is
            # a normal end only for a system root (explorer.exe's parent,
            # userinit.exe, is always gone). Anywhere else an ancestor such as
            # the claude.exe this runs inside of may sit above the gap.
            top = names.get(last)
            return out if top and top[1] in _measure_facade._WINDOWS_ANCESTRY_ROOTS else None
        if entry[0] <= 0 or entry[0] in out:
            break
        out.add(entry[0])
        last = cur
        cur = entry[0]
    return out


def _windows_dir_is_electron_app(exe_path):
    """True/False when the executable's directory is/is not an Electron app
    directory; None when that cannot be determined (fail closed upstream)."""
    _measure_facade = _compat.facade()
    if not exe_path:
        return False
    import ntpath as _ntpath

    base = _ntpath.dirname(exe_path)
    # os.path.exists swallows every OSError and reports False, which would turn
    # an unreadable directory into "not Electron". Probe with os.stat so that
    # anything other than "definitely absent" is indeterminate.
    try:
        _measure_facade.os.stat(base or ".")
    except (OSError, ValueError):
        return None
    found = False
    for marker in _measure_facade._WIN_ELECTRON_MARKERS:
        try:
            _measure_facade.os.stat(_measure_facade.os.path.join(base, *marker))
            found = True
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError):
            return None
    return found


def _windows_ancestry_state(pid, names, electron_parent_pids):
    """Walk parents of pid. Returns "hosted", "complete" or "incomplete".

    "complete" means the chain provably reached a root without meeting a
    desktop app or another claude process. Missing rows, cycles and over-long
    chains are "incomplete": absence of evidence is never a negative host check.
    """
    _measure_facade = _compat.facade()
    seen = {pid}
    cur = pid
    for _ in range(_measure_facade._WIN_ANCESTRY_MAX_HOPS):
        entry = names.get(cur)
        if not entry:
            return "incomplete"
        ppid = entry[0]
        if ppid == 0:
            return "complete"
        if ppid in seen:
            return "incomplete"
        if ppid in electron_parent_pids:
            return "hosted"
        parent = names.get(ppid)
        if not parent:
            return "incomplete"
        if parent[1].startswith("claude"):
            return "hosted"
        if parent[1] in _measure_facade._WIN_SERVICE_HOSTS:
            return "incomplete"
        if parent[1] in _measure_facade._WIN_STABLE_ROOTS:
            return "complete"
        seen.add(ppid)
        cur = ppid
    return "incomplete"


def _windows_cmdline_tokens(cmdline):
    """Split a Windows command line the way the MSVC runtime does.

    Quotes toggle grouping and are removed (so --"print" is --print),
    backslashes before a quote follow the 2n / 2n+1 rule, and a doubled quote
    inside a quoted run is a literal quote. Quoted prompt text therefore stays
    one token and can never masquerade as a switch.
    """
    _measure_facade = _compat.facade()
    text = cmdline or ""
    n = len(text)
    pos = 0
    tokens = []
    # Program name: quotes toggle grouping and are removed; no escape processing.
    while pos < n and text[pos] in " \t":
        pos += 1
    if pos < n:
        buf = []
        in_quote = False
        while pos < n and (in_quote or text[pos] not in " \t"):
            if text[pos] == '"':
                in_quote = not in_quote
            else:
                buf.append(text[pos])
            pos += 1
        tokens.append("".join(buf))
    while True:
        while pos < n and text[pos] in " \t":
            pos += 1
        if pos >= n:
            break
        buf = []
        in_quote = False
        started = False
        while pos < n:
            ch = text[pos]
            if ch == "\\":
                k = pos
                while k < n and text[k] == "\\":
                    k += 1
                slashes = k - pos
                if k < n and text[k] == '"':
                    buf.append("\\" * (slashes // 2))
                    if slashes % 2:
                        buf.append('"')
                        pos = k + 1
                    else:
                        pos = k  # quote handled by the next iteration
                else:
                    buf.append("\\" * slashes)
                    pos = k
                started = True
                continue
            if ch == '"':
                if in_quote and pos + 1 < n and text[pos + 1] == '"':
                    buf.append('"')
                    pos += 2
                else:
                    in_quote = not in_quote
                    pos += 1
                started = True
                continue
            if ch in " \t" and not in_quote:
                break
            buf.append(ch)
            started = True
            pos += 1
        if started:
            tokens.append("".join(buf))
    return tokens


def _windows_option_args(cmdline):
    """Arguments that can be options: tokens after the program, up to a bare --."""
    _measure_facade = _compat.facade()
    args = _measure_facade._windows_cmdline_tokens(cmdline)[1:]
    return args[:args.index("--")] if "--" in args else args


def _read_strict_csv(text, required):
    """Parse PowerShell CSV output, or None when it is malformed or partial.

    Unterminated quotes, short/long rows and a missing header all mean the
    output may have been truncated, which must never be read as evidence.
    """
    _measure_facade = _compat.facade()
    import csv as _csv
    import io as _io

    try:
        rows = list(_csv.reader(_io.StringIO((text or "").lstrip("\ufeff")), strict=True))
    except (_csv.Error, ValueError):
        return None
    if not rows:
        return []
    header = rows[0]
    if not all(col in header for col in required):
        return None
    out = []
    for row in rows[1:]:
        if not row:
            continue
        if len(row) != len(header):
            return None
        out.append(dict(zip(header, row)))
    return out


def _classify_windows_claude_process(image_name, detail, electron_parent_pids, names=None):
    """Classify one claude* Windows process by identity.

    Returns one of:
    - "helper":           Electron child (--type=...), SSH broker or any other
                          claude-adjacent image. Never a session.
    - "desktop_app":      The Electron main process of the desktop app. Not a
                          session; never terminable.
    - "embedded_session": A Claude Code process hosted by another program
                          (desktop app, IDE, SDK, a parent claude) or running
                          headless (--print, stream-json, mcp). A real session
                          owned by its host: never terminated by age.
    - "terminal_cli":     Positively identified interactive terminal process:
                          readable command line, no host/headless markers and
                          a parent that is a known shell or terminal host. The
                          only identity kill_stale_sessions may terminate.
    - "unknown":          Anything else, including every case where identity
                          evidence could not be read. Listed, never terminated.

    There is deliberately no "orphan_cli" here (macOS/Linux only): a Windows
    child keeps the pid of a parent that has exited and is not reparented to a
    stable init process, so "the parent is gone" is not evidence of an orphan.
    """
    _measure_facade = _compat.facade()
    image = (image_name or "").strip().lower()
    if image not in ("claude", "claude.exe"):
        return "helper"
    if not detail:
        return "unknown"
    cmdline = detail.get("cmdline") or ""
    if not cmdline:
        return "unknown"
    if "\ufffd" in cmdline or "\ufffd" in (detail.get("path") or ""):
        return "unknown"  # undecodable text is never affirmative evidence
    args = _measure_facade._windows_option_args(cmdline)  # tokens after a bare -- are prompt text
    if any(a.lower().startswith("--type=") for a in args):
        return "helper"
    if detail.get("pid") in electron_parent_pids:
        return "desktop_app"
    electron_dir = _measure_facade._windows_dir_is_electron_app(detail.get("path"))
    if electron_dir is None:
        return "unknown"
    if electron_dir:
        return "desktop_app"
    if not detail.get("path") or not names:
        return "unknown"
    # Ancestry: hosted by the desktop app or by another claude process.
    ancestry = _measure_facade._windows_ancestry_state(detail.get("pid"), names, electron_parent_pids)
    if ancestry == "hosted":
        return "embedded_session"
    if ancestry != "complete":
        return "unknown"
    lowered = [a.lower() for a in args]
    if any((len(a) > 1 and a[0] == "-" and a[1] != "-" and "p" in a.split("=")[0]) or any(a == f or a.startswith(f + "=") for f in _measure_facade._WIN_HEADLESS_FLAGS) for a in lowered):
        return "embedded_session"
    if any(a in _measure_facade._WIN_HEADLESS_SUBCOMMANDS for a in lowered):
        return "embedded_session"
    parent_entry = names.get(detail.get("pid"))
    if parent_entry and detail.get("ppid") != parent_entry[0]:
        return "unknown"  # the two snapshots disagree about the parent
    parent_name = names.get(parent_entry[0], (None, ""))[1] if parent_entry else ""
    if parent_name in _measure_facade._WIN_TERMINAL_PARENTS:
        return "terminal_cli"
    return "unknown"


def _collect_windows_claude_sessions(process_name=None, creation_fallback=None):
    """Collect runtime processes on Windows via PowerShell Get-Process.

    Safety invariants:
    - Only matches on the process image name (claude / claude-*).
      Matching on Window Title would catch Chrome tabs viewing claude.ai,
      editors with 'claude' in the filename, etc. kill_stale_sessions
      would then TerminateProcess those apps -> unsaved-work data loss.
      POSIX parity: ps-based match requires command == 'claude'; Windows
      requires the same strictness. The PowerShell-side wildcard pre-filter
      is a performance optimization only; the strict matcher below is the
      security layer.
    - Process identity comes from the command line (Win32_Process), not the
      image name: Electron children (--type=), the desktop app main process
      and claude-ssh-broker are dropped, desktop/SDK-hosted sessions are
      tagged "embedded_session", and anything without positive evidence of an
      interactive terminal parent is tagged "unknown". Only "terminal_cli" is
      ever terminable.
    - Uses SessionId (numeric) to detect service-hosted processes.
      Services run in session 0; unlike the literal 'Services' string,
      SessionId never localizes.
    - subprocess calls use errors='replace' so non-ASCII PowerShell output
      on localized Windows can't raise UnicodeDecodeError.

    The previous `tasklist /v` enumeration was pathologically slow for
    standard (non-elevated) users on Win11 -- /v queries verbose info (incl.
    window titles) for EVERY process, hitting access-denied retries on
    protected and other-user processes. Get-Process -Name 'claude*' filters
    server-side, so only candidate processes are materialized, and StartTime
    is dereferenced inside a try/catch so protected processes can't fail the
    query.

    Returns a list of session dicts. On subprocess failure returns an
    empty list (not None) so the Health tab still renders.
    """
    _measure_facade = _compat.facade()
    import csv as _csv
    import io as _io

    if process_name not in ("claude", "codex"):
        raise ValueError("Unsupported runtime process name")
    sessions = []
    ps_cmd = (
        # -Name 'claude*' filters server-side (only candidate processes are
        # ever materialized), so the "touches only candidates" claim is real,
        # not a post-enumeration Where-Object. -ErrorAction SilentlyContinue
        # keeps a zero-match run from erroring.
        f"Get-Process -Name '{process_name}*' -ErrorAction SilentlyContinue | "
        "Select-Object Id, ProcessName, SessionId, "
        "@{N='StartTime';E={try { $_.StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [System.Globalization.CultureInfo]::InvariantCulture) } catch { '' }}} | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = _measure_facade.subprocess.run(
            # -ExecutionPolicy Bypass so a locked-down host (AllSigned policy)
            # can still run this inline -Command (tasklist needed no policy).
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        return sessions
    if result.returncode != 0:
        return sessions

    try:
        reader = list(_csv.DictReader(_io.StringIO(result.stdout)))
    except (_csv.Error, ValueError):
        return sessions

    cim_details = _measure_facade._windows_cim_process_details() if process_name == "claude" else None
    proc_names = _measure_facade._windows_process_names() if cim_details else None
    electron_parent_pids = set()
    if cim_details:
        for detail in cim_details.values():
            if (any(a.lower().startswith("--type=") for a in _measure_facade._windows_option_args(detail.get("cmdline")))
                    and detail.get("ppid")):
                electron_parent_pids.add(detail["ppid"])

    for row in reader:
        image_name = (row.get("ProcessName") or "").strip()
        pid_str = row.get("Id") or ""
        session_id_str = (row.get("SessionId") or "").strip()
        start_time = (row.get("StartTime") or "").strip()
        image_lower = image_name.lower()
        # Strict image-name match only. See docstring invariants.
        matches = image_lower in ("codex", "codex.exe") if process_name == "codex" else (
                image_lower == "claude.exe"
                or image_lower == "claude"
                or image_lower.startswith("claude.")
                or image_lower.startswith("claude-"))
        if not matches:
            continue
        try:
            pid = int(pid_str.replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
        if pid <= 0:
            continue
        identity = None
        if process_name == "claude":
            detail = dict(cim_details.get(pid) or {}) if cim_details else {}
            detail["pid"] = pid
            identity = _measure_facade._classify_windows_claude_process(image_name, detail, electron_parent_pids, proc_names)
            if identity == "terminal_cli" and not _measure_facade._windows_start_times_agree(start_time, detail.get("creation")):
                identity = "unknown"  # the two queries saw different processes under this PID
            if identity in ("helper", "desktop_app"):
                continue
        creation = _measure_facade._parse_iso_process_datetime(start_time) if start_time else None
        if creation is None:
            # StartTime unreadable (protected process) or unparseable: fall
            # back to the per-PID wmic/CIM lookup.
            creation = _measure_facade._windows_process_creation(pid) if creation_fallback else {}
        elapsed_seconds = int(creation.get("elapsed_seconds") or 0)
        # SessionId 0 is the Services session (language-independent); any
        # other value indicates a user session. A missing/unparseable
        # SessionId assumes a user session (services always report 0).
        try:
            session_id = int(session_id_str)
        except ValueError:
            session_id = -1
        has_terminal = session_id != 0
        session = {
            "pid": pid,
            "started": creation.get("started", "unknown"),
            "elapsed_seconds": elapsed_seconds,
            "elapsed_human": _measure_facade._format_elapsed(elapsed_seconds) if elapsed_seconds else "unknown",
            "command": image_name if image_lower.endswith(".exe") else image_name + ".exe",
            "has_terminal": has_terminal,
            "tty": f"session-{session_id}" if has_terminal and session_id > 0 else None,
        }
        if identity is not None:
            session["identity"] = identity
        sessions.append(session)
    return sessions


def _resolve_runtime_bin(process_name):
    """Resolve a runtime CLI (`claude`/`codex`) to an absolute path, or None.

    LIVE BUG: the dashboard's "installed version" showed `unknown` whenever
    `_collect_health_data` ran from a minimal-PATH context. The auto-refresh
    daemon (launchd / scheduled task) and SessionStart hooks inherit a
    near-empty PATH that omits the modern native-installer dir
    (`~/.local/bin`), so a bare `["claude", "--version"]` raised
    FileNotFoundError and the version silently became None.

    We resolve exactly like `_keepwarm_resolve_claude_bin`: `shutil.which`
    first (honours the real PATH when run from a foreground shell), then the
    common install locations launchd cannot see. Nothing is persisted (a
    resolved path goes stale across npm/Homebrew/native updates); this runs
    once per health collection. Returns the bare name as a last resort so the
    historical `subprocess.run` behaviour is preserved when nothing resolves.
    """
    _measure_facade = _compat.facade()
    import shutil as _shutil
    which = _shutil.which(process_name)
    if which:
        return which
    home = _measure_facade.Path.home()
    candidates = [
        home / ".local" / "bin" / process_name,
        home / ".claude" / "local" / process_name,
        _measure_facade.Path("/opt/homebrew/bin") / process_name,
        _measure_facade.Path("/usr/local/bin") / process_name,
        home / ".bun" / "bin" / process_name,
    ]
    # Claude Code's local installer lives under its config dir, so a relocated
    # CLAUDE_CONFIG_DIR moves it off ~/.claude/local.
    if process_name == "claude" and _measure_facade.CLAUDE_DIR / "local" / process_name not in candidates:
        candidates.insert(2, _measure_facade.CLAUDE_DIR / "local" / process_name)
    for cand in candidates:
        try:
            if cand.is_file() and _measure_facade.os.access(str(cand), _measure_facade.os.X_OK):
                return str(cand)
        except OSError:
            continue
    return process_name


def _semver_key(version):
    """Return a SemVer precedence key, or None when ordering is unknown."""
    _measure_facade = _compat.facade()
    if not isinstance(version, str):
        return None
    match = _measure_facade.re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version)
    if not match:
        return None
    identifiers = []
    prerelease = match.group(4)
    if prerelease is not None:
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    return None
                identifiers.append((0, (len(identifier), identifier)))
            else:
                identifiers.append((1, identifier))
    return (*((len(part), part) for part in match.group(1, 2, 3)),
            prerelease is None, tuple(identifiers))


def _collect_health_data():
    """Collect session health data.

    Returns a dict on macOS, Linux, and Windows. Returns None only if the
    POSIX `ps` probe itself errors (historical contract preserved so
    callers that short-circuit on None keep working).
    """
    _measure_facade = _compat.facade()
    system = _measure_facade.platform.system()
    runtime = _measure_facade.detect_runtime()
    # Hermes: Hermes sessions are not `claude` processes.
    # Probing `claude --version` and `ps | grep claude` under hermes is
    # semantically wrong and would report stale/empty data. Return a
    # hermes-mode minimal dict so the dashboard's health row renders
    # without probing the Claude CLI.
    if runtime == "hermes":
        return {
            "installed_version": None,
            "running_sessions": [],
            "automated": [],
            "runtime": "hermes",
        }
    # Cursor: sessions are not `claude` processes either; probing the Claude
    # CLI and `ps | grep claude` under a Cursor pin reports wrong data.
    if runtime == "cursor":
        return {
            "installed_version": None,
            "running_sessions": [],
            "automated": [],
            "runtime": "cursor",
        }
    process_name = "codex" if runtime == "codex" else "claude"

    installed_version = None
    try:
        result = _measure_facade.subprocess.run(
            [_measure_facade._resolve_runtime_bin(process_name), "--version"],
            capture_output=True, text=True, timeout=10, creationflags=_measure_facade._NO_WINDOW,
        )
        if result.returncode == 0:
            raw_version = result.stdout.strip()
            installed_version = raw_version if runtime == "codex" else (raw_version.split()[0] if raw_version else None)
    except (_measure_facade.subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    if system == "Windows":
        running_sessions = (_measure_facade._collect_windows_claude_sessions(process_name="codex")
                            if runtime == "codex" else _measure_facade._collect_windows_claude_sessions())
    else:
        running_sessions = _measure_facade._collect_posix_claude_sessions(process_name=process_name)
        if running_sessions is None:
            return None

    # Version enrichment per session. _find_session_version_for_pid relies
    # on POSIX /proc-style discovery, so on Windows we set version=None.
    # Future: Windows version probe via Get-Process ProductVersion.
    if system == "Windows" or runtime == "codex":
        for session in running_sessions:
            session["version"] = None
    else:
        for session in running_sessions:
            session["version"] = _measure_facade._find_session_version_for_pid(session["pid"])

    # Zero-subprocess fallback: if `claude --version` could not run (locked-down
    # PATH, sandbox, transient spawn failure), the running session JSONLs still
    # carry the exact version that wrote them -- which IS the installed CLI.
    # Use the highest detected session version so the dashboard never shows
    # "unknown" while a Claude session is demonstrably running.
    if not installed_version and runtime != "codex":
        detected = [(key, session["version"]) for session in running_sessions
                    if (key := _measure_facade._semver_key(session.get("version"))) is not None]
        if detected:
            installed_version = max(detected, key=lambda item: ((_measure_facade := _compat.facade()), item[0])[1])[1]

    installed_key = _measure_facade._semver_key(installed_version)

    # Flag sessions
    for s in running_sessions:
        if runtime == "codex":
            # Desktop app-server lifetime is not task age or evidence of an
            # abandoned session. Never recommend killing shared Codex hosts.
            s["flags"] = ["RUNNING"]
            continue
        flags = []
        session_key = _measure_facade._semver_key(s.get("version"))
        if session_key is not None and installed_key is not None and session_key < installed_key:
            flags.append("OUTDATED")
        if s["elapsed_seconds"] > 172800:
            flags.append("ZOMBIE")
        elif s["elapsed_seconds"] > 86400:
            flags.append("STALE")
        elif system == "Windows" and s["elapsed_seconds"] == 0:
            # Windows without a creation-time source (wmic gone, PowerShell locked
            # down) can't threshold STALE/ZOMBIE. Surface explicitly so the user
            # isn't fooled into thinking all sessions are fresh.
            flags.append("UNKNOWN_AGE")
        identity = s.get("identity")
        if identity == "embedded_session":
            flags.append("DESKTOP")
        elif identity == "unknown":
            flags.append("UNVERIFIED")
        elif identity == "orphan_cli":
            flags.append("ORPHAN")
        elif s.get("has_terminal"):
            flags.append("TERMINAL")
        else:
            flags.append("HEADLESS")
        s["flags"] = flags

    automated = []
    if system == "Darwin":
        try:
            result = _measure_facade.subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    needles = ("codex", "token-optimizer.codex", "tokenoptimizer.codex") if runtime == "codex" else ("claude", "anthropic")
                    if any(needle in line.lower() for needle in needles):
                        automated.append(line.strip())
        except (_measure_facade.subprocess.SubprocessError, OSError):
            pass
    elif system == "Windows":
        # Windows parity: surface scheduled tasks that touch claude/token-optimizer.
        try:
            import csv as _csv
            import io as _io
            result = _measure_facade.subprocess.run(
                ["schtasks", "/Query", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
            )
            if result.returncode == 0:
                for row in _csv.reader(_io.StringIO(result.stdout)):
                    if not row:
                        continue
                    name = row[0]
                    lname = name.lower()
                    needles = ("codex", "token-optimizer.codex", "tokenoptimizer.codex") if runtime == "codex" else ("claude", "token-optimizer", "tokenoptimizer", "anthropic")
                    if any(tok in lname for tok in needles):
                        automated.append(name.strip())
        except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
            pass

    # Build recommendations
    recommendations = []
    # Sessions hosted by another app (DESKTOP) or with unreadable identity
    # (UNVERIFIED) are not terminals the user can close and reopen.
    # ORPHAN sessions have no terminal to close and reopen; they get their own
    # recommendation below.
    _own = [s for s in running_sessions if not any(f in s.get("flags", []) for f in ("DESKTOP", "UNVERIFIED", "ORPHAN"))]
    outdated_count = sum(1 for s in _own if "OUTDATED" in s.get("flags", []))
    stale_count = sum(1 for s in _own if any(f in s.get("flags", []) for f in ("STALE", "ZOMBIE")))

    if outdated_count > 0 and installed_version:
        recommendations.append(
            f"{outdated_count} session{'s' if outdated_count != 1 else ''} running "
            f"older version (installed: {installed_version}). "
            f"Restart to get latest fixes: close and reopen these terminals."
        )
    if stale_count > 0:
        recommendations.append(
            f"{stale_count} session{'s' if stale_count != 1 else ''} running "
            f"24+ hours. Check if still needed, long sessions accumulate context bloat."
        )
    orphan_count = sum(1 for s in running_sessions if "ORPHAN" in s.get("flags", []))
    if orphan_count > 0:
        recommendations.append(
            f"{orphan_count} session{'s' if orphan_count != 1 else ''} with the terminal gone "
            f"(ORPHAN). Never ended automatically. Review, then run "
            f"`{_measure_facade._measure_cli('kill-stale', '--include-orphans', '--dry-run')}` to preview ending them."
        )
    unknown_age_count = sum(1 for s in running_sessions if "UNKNOWN_AGE" in s.get("flags", []))
    if unknown_age_count > 0 and system == "Windows":
        recommendations.append(
            f"{unknown_age_count} session{'s' if unknown_age_count != 1 else ''} missing "
            "start time (wmic and PowerShell Get-CimInstance both unavailable). "
            "Install PowerShell 7+ (winget install Microsoft.PowerShell) or use Task "
            "Manager to identify orphaned processes; STALE/ZOMBIE detection needs a "
            "start-time source to work."
        )

    # Version-specific warnings
    if installed_version:
        try:
            version_parts = tuple(int(x) for x in installed_version.split(".")[:3])
            if version_parts < (2, 1, 70):
                recommendations.append(
                    "Upgrade to Claude Code 2.1.70+ to fix skill listing re-injection on resume (~600 tokens/resume)."
                )
        except (ValueError, TypeError):
            pass

    return {
        "installed_version": installed_version,
        "running_sessions": running_sessions,
        "automated": automated,
        "recommendations": recommendations,
        "cli": _measure_facade._measure_cli(),
        "cli_windows": _measure_facade._windows_hints(),
    }


def health_selfcheck():
    """Probe the current platform's process-listing commands and parse their real output.

    Designed as a lightweight smoke test for the Session Health code path.
    Prints PASS/FAIL per probe; exits non-zero if any critical check fails.
    Intended to be run by users (especially Windows users) after installing
    the plugin: `python3 measure.py health-selfcheck`.
    """
    _measure_facade = _compat.facade()
    system = _measure_facade.platform.system()
    print(f"\nSESSION HEALTH SELF-CHECK  (platform: {system})")
    print("=" * 60)

    passed = 0
    failed = 0

    def check(name, ok, detail=""):
        _measure_facade = _compat.facade()
        nonlocal passed, failed
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}]  {name}")
        if detail:
            print(f"          {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    # Parser self-tests (platform-agnostic)
    wmi_probe = _measure_facade._parse_wmi_datetime("20260101120000.000000+000")
    check("WMI datetime parser", wmi_probe is not None and wmi_probe["elapsed_seconds"] > 0)

    iso_probe = _measure_facade._parse_iso_process_datetime("2026-01-01T12:00:00Z")
    check("ISO-8601 datetime parser", iso_probe is not None and iso_probe["elapsed_seconds"] > 0)

    # Process-name matcher: bare argv0, absolute-path and ".exe" wrapper
    # launches all match; the case-different desktop app, an arg-only
    # mention, and unrelated processes must not.
    _m = _measure_facade._command_matches_process
    matcher_ok = (
        _m("claude", "claude")
        and _m("claude --resume abc", "claude")
        and _m("/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude", "claude")
        and _m("/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe --resume abc", "claude")
        and _m("/opt/homebrew/bin/codex serve", "codex")
        # Claude Desktop / WSL 2 launcher: versioned ccd-cli binary (issue #192)
        and _m("/home/user/.claude/remote/ccd-cli/2.1.271 --output-format stream-json --verbose", "claude")
        and not _m("/Applications/Claude.app/Contents/MacOS/Claude", "claude")
        and not _m("/usr/bin/vim claude.py", "claude")
        and not _m("node /path/to/app.js", "claude")
        # ccd-cli launcher must NOT satisfy a codex probe, and must NOT match
        # when the path appears only as an argument.
        and not _m("/home/user/.claude/remote/ccd-cli/2.1.271", "codex")
        and not _m("/usr/bin/vim /home/user/.claude/remote/ccd-cli/notes.txt", "claude")
        and not _m("", "claude")
    )
    check("process-name matcher (basename + .exe + ccd-cli)", matcher_ok)

    # Live process-listing command
    if system == "Windows":
        # Get-Process probe (same pipeline shape as
        # _collect_windows_claude_sessions; tasklist /v was retired, which is
        # pathologically slow for standard users on Win11)
        try:
            res = _measure_facade.subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "Get-Process -ErrorAction SilentlyContinue | "
                 "Select-Object -First 1 Id, ProcessName, SessionId | "
                 "ConvertTo-Csv -NoTypeInformation"],
                capture_output=True, text=True, timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
            )
            ok = res.returncode == 0 and "ProcessName" in res.stdout
            check("Get-Process csv probe", ok,
                  f"exit={res.returncode}, bytes={len(res.stdout)}")
        except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError) as e:
            check("Get-Process csv probe", False, f"exception: {e!r}")

        # _collect_windows_claude_sessions end-to-end
        try:
            sessions = _measure_facade._collect_windows_claude_sessions()
            check("_collect_windows_claude_sessions end-to-end", True,
                  f"returned {len(sessions)} session(s)")
        except Exception as e:
            check("_collect_windows_claude_sessions end-to-end", False, f"exception: {e!r}")

        # wmic probe against our own pid
        own_pid = _measure_facade.os.getpid()
        try:
            res = _measure_facade.subprocess.run(
                ["wmic", "process", "where", f"ProcessId={own_pid}",
                 "get", "CreationDate", "/format:list"],
                capture_output=True, text=True, timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
            wmic_ok = res.returncode == 0 and "CreationDate=" in res.stdout
            check("wmic CreationDate probe (self-pid)", wmic_ok,
                  f"exit={res.returncode}")
        except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError) as e:
            check("wmic CreationDate probe (self-pid)", False,
                  f"exception: {e!r} (Win11 24H2 removes wmic; PowerShell fallback will be used)")
    else:
        try:
            res = _measure_facade.subprocess.run(
                ["ps", "-ww", "-eo", "pid,ppid,uid,tty,lstart,etime,command"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                # Match the production collectors: force C locale so this
                # diagnostic mirrors what _collect_posix_claude_sessions sees.
                env={**_measure_facade.os.environ, "LC_ALL": "C", "LC_TIME": "C"}, creationflags=_measure_facade._NO_WINDOW,
            )
            ok = res.returncode == 0 and len(res.stdout.strip().split("\n")) > 1
            check("ps -ww -eo pid,ppid,uid,tty,lstart,etime,command", ok,
                  f"exit={res.returncode}, lines={len(res.stdout.strip().split(chr(10)))}")
        except (_measure_facade.subprocess.SubprocessError, OSError) as e:
            check("ps -ww -eo pid,ppid,uid,tty,lstart,etime,command", False, f"exception: {e!r}")

        try:
            sessions = _measure_facade._collect_posix_claude_sessions()
            ok = sessions is not None
            detail = f"returned {len(sessions) if sessions else 0} session(s)" if ok else "returned None"
            check("_collect_posix_claude_sessions end-to-end", ok, detail)
        except Exception as e:
            check("_collect_posix_claude_sessions end-to-end", False, f"exception: {e!r}")

    # Dispatch end-to-end
    try:
        health = _measure_facade._collect_health_data()
        if system == "Windows":
            # Windows must return a dict, not None, after this fix
            ok = isinstance(health, dict) and "running_sessions" in health
            check("_collect_health_data returns dict on Windows",
                  ok, "this is the Bug-1 regression guard")
        else:
            ok = health is None or (isinstance(health, dict) and "running_sessions" in health)
            check("_collect_health_data POSIX contract", ok)
    except Exception as e:
        check("_collect_health_data dispatch", False, f"exception: {e!r}")

    print()
    print(f"  Results: {passed} passed, {failed} failed")
    print()
    if failed > 0:
        print("  Paste this output into the issue to help us diagnose.")
        _measure_facade.sys.exit(1)


def session_health():
    """Check health of running Claude Code sessions."""
    _measure_facade = _compat.facade()
    health = _measure_facade._collect_health_data()
    if health is None:
        print("\nSession health check is not supported on this platform.")
        return

    installed_version, running_sessions, automated, recommendations = (
        health[k] for k in ("installed_version", "running_sessions", "automated", "recommendations"))

    print("\nSESSION HEALTH CHECK")
    print("=" * 55)

    print(f"\n  Installed version: {installed_version or 'unknown (could not run ' + repr('claude --version') + ')'}")

    if not running_sessions:
        print("\n  No running Claude Code CLI sessions found.")
    else:
        print(f"\nRUNNING SESSIONS ({len(running_sessions)})")

        for s in running_sessions:
            flags = s.get("flags", [])
            version_str = s["version"] or "unknown"
            flag_str = f"  {'  '.join(flags)}" if flags else ""
            print(f"  PID {s['pid']:<7d} Started: {s['started']}  ({s['elapsed_human']} ago)")
            print(f"             Version: {version_str}{flag_str}")
            if "ORPHAN" in flags:
                print(f"             terminal gone; started {s['elapsed_human']} ago")

        if recommendations:
            print("\nRECOMMENDATIONS")
            for rec in recommendations:
                print(f"  - {rec}")

    if automated:
        print("\nAUTOMATED PROCESSES")
        for proc in automated:
            print(f"  {proc}")
    try:  # hooks the malformed-hook healer left alone (heal_report.py)
        for _line in __import__("heal_report").health_lines(_measure_facade):
            print(_line)
    except Exception:
        pass  # health must never fail on this optional section

    print()


def _windows_revalidate_terminal_cli(session, fresh_inventory=None):
    """Re-check a Windows session right before termination.

    The inventory is a snapshot; a PID can be reused or reclassified between
    that snapshot and the kill. Re-collect (without per-PID fallback probes for
    unrelated processes) and require the same PID with the same start time to
    still classify as terminal_cli. A race between that snapshot and
    TerminateProcess remains: PID-based termination has no process handle to
    bind to, so a PID reused inside that interval could be terminated.
    """
    _measure_facade = _compat.facade()
    if session.get("started") in (None, "", "unknown"):
        return False
    if fresh_inventory is None:
        try:
            fresh_inventory = {x["pid"]: x for x in _measure_facade._collect_windows_claude_sessions(creation_fallback=False)}
        except Exception:
            return False
    now = fresh_inventory.get(session["pid"])
    return bool(
        now
        and now.get("identity") == "terminal_cli"
        and now.get("started") == session.get("started")
    )


def _posix_revalidate_terminal_cli(session, fresh_inventory=None, identity=None):
    """Re-check a POSIX session right before termination.

    The inventory is a snapshot; a PID can be reused or reclassified between
    that snapshot and the kill. Re-collect and require the same PID with the
    same start time and command line to still classify as `identity`
    (terminal_cli, or orphan_cli for `kill-stale --include-orphans`). A race
    between that re-check and os.kill remains: a PID-based signal has no handle
    to bind to, so a PID reused inside that interval could be signalled.
    """
    _measure_facade = _compat.facade()
    if session.get("started") in (None, "", "unknown"):
        return False
    if fresh_inventory is None:
        try:
            fresh = _measure_facade._collect_posix_claude_sessions("claude")
        except Exception:
            return False
        if fresh is None:
            return False
        fresh_inventory = {x["pid"]: x for x in fresh}
    now = fresh_inventory.get(session["pid"])
    return bool(
        now
        and now.get("identity") == identity
        and now.get("started") == session.get("started")
        and now.get("command") == session.get("command")
        and now.get("uid") == session.get("uid")
    )


def _revalidate_terminal_cli(session, identity=None):
    """Re-verify one identity-tagged session with the collector that tagged it.

    `identity` is the class the session must still have (orphan_cli exists on
    POSIX only; Windows sessions are always terminal_cli)."""
    _measure_facade = _compat.facade()
    if session.get("identity_source") == "ps":
        return _measure_facade._posix_revalidate_terminal_cli(session, identity=identity)
    return identity == "terminal_cli" and _measure_facade._windows_revalidate_terminal_cli(session)


def _parse_kill_stale_args(args):
    """(hours, dry_run, include_orphans) from the `kill-stale` argv tail."""
    _measure_facade = _compat.facade()
    hours = 12
    for i, a in enumerate(args):
        if a == "--hours" and i + 1 < len(args):
            try:
                hours = int(args[i + 1])
            except ValueError:
                pass
    return hours, "--dry-run" in args, "--include-orphans" in args


def _kill_stale_owns(session):
    """True when kill-stale may consider `session`: POSIX sessions must be owned by
    the effective uid of this process (uid unknown means not provably ours)."""
    _measure_facade = _compat.facade()
    if session.get("identity_source") != "ps" or not hasattr(_measure_facade.os, "geteuid"):
        return True
    return session.get("uid") == _measure_facade.os.geteuid()


def kill_stale_sessions(threshold_hours=None, dry_run=None, include_orphans=None):
    """Kill Claude Code sessions that have been running longer than threshold_hours.

    Targets abandoned terminal sessions only: a session is terminable when its
    collector positively identified it as a terminal CLI process. Orphans
    (orphan_cli: terminal gone, reparented to init, macOS/Linux) are listed but
    only terminated with include_orphans. Skips the current process's own PID
    and ancestors to avoid self-termination.
    """
    _measure_facade = _compat.facade()
    import signal

    if _measure_facade.detect_runtime() == "codex":
        print("\n  Codex processes can host multiple active tasks. Process age cannot identify stale tasks; automatic termination is disabled.")
        return
    health = _measure_facade._collect_health_data()
    if health is None:
        print("\n  Session health check is not supported on this platform.")
        return

    # Only ever act on this user's own processes. `ps -e` lists everyone's; as root on
    # a shared box that would otherwise reach another user's conversation. A POSIX
    # session whose owner is unknown is treated as not ours. The Windows collectors
    # carry no owner (see the closing message below).
    running = [s for s in health["running_sessions"] if _measure_facade._kill_stale_owns(s)]
    threshold_seconds = threshold_hours * 3600
    my_pid = _measure_facade.os.getpid()
    my_ppid = _measure_facade.os.getppid()
    # Never terminate the session this command is running inside of: it is an
    # ancestor of this process (claude -> shell -> python), not its direct parent.
    ancestry_unknown = False
    if _measure_facade.os.name == "nt":
        my_ancestors = _measure_facade._windows_ancestor_pids(my_pid)
        if my_ancestors is None:
            ancestry_unknown = True
            my_ancestors = set()
    else:
        my_ancestors = _measure_facade._posix_ancestor_pids(my_pid)
        if my_ancestors is None:
            ancestry_unknown = True
            my_ancestors = set()

    # Fail closed: process age is not evidence that a conversation is
    # abandoned. Sessions whose identity is known to belong to a host app
    # (desktop/SDK/IDE/headless) or could not be established are never
    # terminated, and neither is a session no collector tagged: the default
    # below is "unknown", not "terminal_cli".
    killable = ("terminal_cli", "orphan_cli") if include_orphans else ("terminal_cli",)
    protected = [s for s in running
                 if s.get("identity", "unknown") not in ("terminal_cli", "orphan_cli")
                 and s["elapsed_seconds"] > threshold_seconds]
    stale = [s for s in running
             if s["elapsed_seconds"] > threshold_seconds
             and s["pid"] != my_pid
             and s["pid"] != my_ppid
             and s["pid"] not in my_ancestors
             and s.get("identity", "unknown") in killable]
    held_orphans = [] if include_orphans else [
        s for s in running
        if s.get("identity") == "orphan_cli"
        and s["elapsed_seconds"] > threshold_seconds
        and s["pid"] not in (my_pid, my_ppid)
        and s["pid"] not in my_ancestors]

    if protected:
        print(f"\n  Skipping {len(protected)} long-running session{'s' if len(protected) != 1 else ''} "
              "hosted by the Claude desktop app, an IDE or the SDK, or whose identity could not be verified.")
        print("  Process age alone is not evidence that these are abandoned; they are never auto-terminated.")

    if held_orphans:
        n = len(held_orphans)
        print(f"\n  {n} orphaned session{'s' if n != 1 else ''} (terminal gone, running >{threshold_hours}h) "
              f"{'were' if n != 1 else 'was'} left alone. Orphans are only ended on request:")
        print(f"    {_measure_facade._measure_cli('kill-stale', '--include-orphans', '--hours', str(threshold_hours))}   (add --dry-run to preview)")

    if not stale:
        if protected or held_orphans:
            print(f"\n  No terminable stale sessions found (threshold: {threshold_hours}h).")
            return
        print(f"\n  No stale sessions found (threshold: {threshold_hours}h).")
        print(f"  {len(running)} active session{'s' if len(running) != 1 else ''}, all within threshold.")
        return

    print(f"\n  Found {len(stale)} stale session{'s' if len(stale) != 1 else ''} (running >{threshold_hours}h):\n")
    for s in stale:
        flags = " ".join(s.get("flags", []))
        print(f"    PID {s['pid']:<7d}  {s['elapsed_human']:>10s}  v{s.get('version') or '?':<10s}  {flags}")

    if dry_run:
        print(f"\n  Dry run. Would kill {len(stale)} process{'es' if len(stale) != 1 else ''}.")
        print("  Run without --dry-run to terminate them.\n")
        return

    if ancestry_unknown:
        print("\n  Could not read this process's ancestry, so a candidate cannot be ruled out as the")
        print("  conversation this command is running inside of. Nothing was terminated.\n")
        return

    killed = 0
    for s in stale:
        # Re-collect per candidate, immediately before its termination: the
        # window between verification and the signal is as small as a
        # PID-based kill allows (no process handle is retained).
        if not _measure_facade._revalidate_terminal_cli(s, identity=s.get("identity", "unknown")):
            print(f"    PID {s['pid']} skipped: identity changed or could not be re-verified.")
            continue
        try:
            _measure_facade.os.kill(s["pid"], signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            print(f"    PID {s['pid']} already gone.")
        except PermissionError:
            print(f"    PID {s['pid']} permission denied (owned by another user).")
        except OSError as e:
            # Windows raises a plain OSError (WinError 87), not ProcessLookupError,
            # for a pid that exited between the re-check and the signal. One
            # candidate failing must not skip the rest.
            print(f"    PID {s['pid']} could not be signalled (it may have just exited): {e}")

    print(f"\n  Terminated {killed} stale session{'s' if killed != 1 else ''}.")
    if killed > 0:
        print(f"  These were Claude Code processes running >{threshold_hours}h.")
        if _measure_facade.os.name == "nt":
            # No owner column in the Windows process inventory (a GetOwner call per
            # process is a new slow CIM round trip), so do not claim an account filter.
            print("  Only sessions older than the threshold were targeted. The Windows process list")
            print("  does not record the owning account, so from an elevated shell this can include")
            print("  other accounts' sessions.\n")
        else:
            print("  Your active terminal sessions are unaffected.\n")


def _read_plugin_version(default=None):
    """Read the shipped version from the plugin manifest, once, at import.

    This was a hand-maintained string carrying the comment "keep in sync with
    plugin.json + marketplace.json". It was not kept in sync: it sat four
    releases behind the manifests, and had drifted across at least two earlier
    releases as well, so a fresh install rendered a stale version in the
    dashboard header. A constant that must be updated by hand on every release
    will eventually be missed, so the manifest is now the single source.

    Falls back to the default when the manifest is absent, which is the case for
    a skill-only install with no plugin directory. Never raises: a version
    string is cosmetic and must not prevent the tool from running.
    """
    _measure_facade = _compat.facade()
    here = _measure_facade.Path(_measure_facade.__file__).resolve()
    for base in here.parents:
        candidate = base / ".claude-plugin" / "plugin.json"
        try:
            if candidate.is_file():
                value = _measure_facade.json.loads(candidate.read_text(encoding="utf-8")).get("version")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except (OSError, ValueError):
            return default
    return default


def _scheduler_names_to_sweep(this_install_only: bool, all_names, active_name):
    """Scheduler identifiers to unregister.

    ``this_install_only`` scoped the per-identity FILE sweep but not the
    scheduler loops, so a "just this install" uninstall still booted out every
    runtime's LaunchAgent/unit/task -- leaving siblings half-dead (files
    intact, registration gone) and silently tearing down a co-installed
    Codex/Hermes dashboard. Honor the flag on both halves.
    """
    _measure_facade = _compat.facade()
    return (active_name,) if this_install_only else tuple(all_names)


def _sweep_identity_daemon_files(snap_dir: Path, keys) -> tuple[list[str], list[str]]:
    """Delete the daemon artifacts named by ``keys`` from one identity.

    Returns ``(removed, failed)`` as path strings. Failures are reported
    rather than swallowed so the caller can refuse to claim
    a clean sweep while a 0600 daemon-token is still on disk.
    """
    _measure_facade = _compat.facade()
    files = _measure_facade._daemon_per_identity_files(snap_dir)
    removed: list[str] = []
    failed: list[str] = []
    for key in keys:
        was_removed, did_fail = _measure_facade._unlink_reporting(files[key])
        if was_removed:
            removed.append(str(files[key]))
        elif did_fail:
            failed.append(str(files[key]))
    return removed, failed


def _print_identity_sweep_report(removed, per_identity_removed, failed, this_install_only):
    """Shared uninstall reporting for all three platforms.

    Honesty rules preserved and extended: never print a
    "Deleted" line for a survivor, keep "Nothing to remove" when nothing
    existed, and NEVER print the all-identities banner when any artifact
    resisted deletion -- a surviving token means the sweep was not clean.
    """
    _measure_facade = _compat.facade()
    if removed:
        print("[Token Optimizer] Dashboard daemon removed.")
        for path in removed:
            print(f"  Deleted: {path}")
        if per_identity_removed and not this_install_only and not failed:
            print("  Swept all token-optimizer-* identities:")
            for snap_dir, items in per_identity_removed:
                print(f"    {snap_dir}: {len(items)} file(s)")
    elif not failed:
        print("[Token Optimizer] No daemon artifacts found. Nothing to remove.")
    if failed:
        print(
            f"  WARNING: {len(failed)} daemon artifact(s) could NOT be removed "
            "(permission denied or in use)."
        )
        for path in failed:
            print(f"    Survived: {path}")
        print(
            "    A surviving daemon-token is a live CSRF secret -- delete these "
            "manually, then re-run."
        )


def _daemon_per_identity_files(snapshot_dir: Path) -> dict:
    """Per-identity daemon artifact paths rooted at ``snapshot_dir``.

    These are the files that live INSIDE a single plugin-data identity's data
    dir (one per ``token-optimizer-*`` install): the generated daemon script,
    the 0600 CSRF token, the persisted bind-host, the tombstone, and
    the platform launcher/XML. The OS scheduler REGISTRATION (LaunchAgent
    plist / scheduled task / systemd unit) is per-RUNTIME and shared across
    identities of the same runtime, so it is removed once, not per-identity.
    """
    _measure_facade = _compat.facade()
    return {
        "daemon_script": snapshot_dir / "dashboard-server.py",
        "daemon_token": snapshot_dir / "daemon-token",
        "daemon_host": snapshot_dir / "dashboard-host",
        "thrash_breadcrumb": snapshot_dir / ".daemon-thrash",
        "windows_launcher": snapshot_dir / _measure_facade.WINDOWS_LAUNCHER_NAME,
        "linux_launcher": snapshot_dir / _measure_facade.LINUX_LAUNCHER_NAME,
    }


def _daemon_identity_snapshot_dirs(this_install_only: bool) -> list[Path]:
    """Snapshot dirs to sweep during daemon uninstall.

    Root cause: daemon paths derive from one module-level ``SNAPSHOT_DIR``
    (the resolved identity), but multiple installs create multiple
    ``token-optimizer-*`` identities. An identity-scoped uninstall leaves the
    sibling's ``dashboard-server.py`` + ``0600 daemon-token`` (a live local
    HTTP daemon plus its CSRF secret) on disk.

    By default (sweep-all): every ``token-optimizer-*`` plugin-data identity's
    ``data`` dir, so no sibling outlives the uninstall. With
    ``this_install_only``: just the resolved ``SNAPSHOT_DIR`` (the pre-fix
    behavior), for a user intentionally running side-by-side installs who only
    wants this one gone. Reuses ``plugin_env._all_plugin_data_dirs`` rather
    than a fourth glob, and always includes the resolved ``SNAPSHOT_DIR`` even
    when it is not under the standard base (e.g. a
    ``TOKEN_OPTIMIZER_SNAPSHOT_DIR`` sandbox override).
    """
    _measure_facade = _compat.facade()
    if this_install_only:
        return [_measure_facade.SNAPSHOT_DIR]
    dirs: list[Path] = []
    try:
        for ident in _measure_facade._all_plugin_data_dirs():
            data_dir = ident / "data"
            # _all_plugin_data_dirs vets the IDENTITY dir, but
            # the dir we actually delete from is its `data` child. A real
            # identity dir whose `data` is a SYMLINK pointed at, say, $HOME
            # made the sweep unlink daemon-token/dashboard-server.py/etc from
            # the symlink target -- outside the plugin-data base entirely.
            # Vet the deletion target itself, not just its parent.
            if _measure_facade._daemon_sweep_dir_is_safe(data_dir):
                dirs.append(data_dir)
    except Exception:  # noqa: BLE001
        pass
    if _measure_facade.SNAPSHOT_DIR not in dirs:
        dirs.append(_measure_facade.SNAPSHOT_DIR)
    # Deterministic order, de-duplicated by resolved path.
    seen: set[Path] = set()
    ordered: list[Path] = []
    for d in sorted(dirs, key=lambda p: ((_measure_facade := _compat.facade()), str(p))[1]):
        try:
            r = d.resolve(strict=False)
        except (OSError, ValueError):
            r = d
        if r in seen:
            continue
        seen.add(r)
        ordered.append(d)
    return ordered


def _daemon_sweep_dir_is_safe(snap_dir: Path) -> bool:
    """True when ``snap_dir`` is a real directory we may delete daemon files from.

    The sweep deletes a fixed allow-list of filenames out of
    every identity's ``data`` dir. That dir must therefore be a REAL directory
    whose realpath lands under the plugin-data base -- otherwise a symlinked
    ``data`` child redirects the unlinks outside the base (the escape found in
    review: ``token-optimizer-evil/data -> $HOME`` deleted files from $HOME).

    The resolved ``SNAPSHOT_DIR`` is always allowed even when it sits outside
    the base, because ``TOKEN_OPTIMIZER_SNAPSHOT_DIR`` (tests, sandboxes) and
    the legacy script-install layout legitimately point elsewhere. That is the
    identity we were told to operate on, not one we discovered by globbing.
    """
    _measure_facade = _compat.facade()
    try:
        if snap_dir.is_symlink() or not snap_dir.is_dir():
            return False
        resolved = snap_dir.resolve(strict=True)
    except (OSError, ValueError):
        return False
    try:
        if resolved == _measure_facade.SNAPSHOT_DIR.resolve(strict=False):
            return True
    except (OSError, ValueError):
        pass
    try:
        # Read the base off the module (not a from-import binding) so the
        # check honors a runtime-repointed plugin-data root.
        base = _measure_facade.plugin_env._PLUGIN_DATA_BASE
        return resolved.is_relative_to(base.resolve(strict=False))
    except (OSError, ValueError, AttributeError):
        return False


def _unlink_if_exists(path: Path) -> bool:
    """Delete ``path`` if present. Returns True iff a file was removed.

    Never follows a symlink to delete its target: a planted symlink named
    ``daemon-token`` must be removed as the link itself, not as whatever it
    points at (defense in depth behind
    ``_daemon_sweep_dir_is_safe``).
    """
    _measure_facade = _compat.facade()
    try:
        if path.is_symlink():
            path.unlink()
            return True
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def _unlink_reporting(path: Path) -> tuple[bool, bool]:
    """Delete ``path``; return ``(removed, failed)``.

    ``_unlink_if_exists`` swallows OSError and returns False,
    which is indistinguishable from "was not there". The sweep then printed
    "Swept all token-optimizer-* identities" while a 0600 daemon-token it
    could not delete (read-only dir, EPERM) was still on disk. This variant
    separates the two so the report can be honest about survivors.
    """
    _measure_facade = _compat.facade()
    try:
        if not path.is_symlink() and not path.exists():
            return (False, False)
    except OSError:
        return (False, True)
    try:
        path.unlink()
        return (True, False)
    except OSError:
        return (False, True)


def _get_or_create_daemon_token():
    """Return the per-install daemon auth token, creating it on first use.

    v5.4.19 (security H-2/M-4 fix): all state-mutating POST endpoints
    (/api/v5/toggle, /api/skill/archive, /api/mcp/disable, etc.) require
    a matching X-TO-Token header. The token is generated at daemon
    install time, persisted at DAEMON_TOKEN_PATH with 0600 perms, and
    exposed to the dashboard via a same-origin /api/token endpoint.

    Any local same-user process can still read the token file (standard
    Unix permissions), so this is NOT a defense against a same-user
    attacker — it's defense-in-depth against (a) cross-origin drive-by
    POSTs that bypass the empty-Origin CSRF check, (b) DNS-rebinding
    pages that manage to forge an Origin header, and (c) accidental
    discovery by scripts probing localhost ports.

    Idempotent: returns the existing token if the file is already there
    and non-empty. Only creates a new token on first run or if the file
    is missing/empty.
    """
    # v5.11.1: read via _read_state_file (consistent OSError-safe read path).
    _measure_facade = _compat.facade()
    existing = _measure_facade._read_state_file(_measure_facade.DAEMON_TOKEN_PATH)
    if len(existing) >= 16:
        return existing
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        token = _measure_facade.secrets.token_urlsafe(32)  # 43 chars, cryptographically random
        # O_EXCL: atomic create — concurrent installers race on mkdir, not
        # on truncating each other's token.
        fd = _measure_facade.os.open(
            str(_measure_facade.DAEMON_TOKEN_PATH),
            _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL,
            0o600,
        )
        with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        return token
    except FileExistsError:
        # Lost the race — another installer created it first. Read theirs.
        return _measure_facade._read_state_file(_measure_facade.DAEMON_TOKEN_PATH)
    except OSError:
        return ""


def _read_state_file(path):
    """Read a small single-value state file. Returns '' if absent/unreadable."""
    _measure_facade = _compat.facade()
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _read_daemon_token():
    """Read the daemon token from disk. Returns empty string if missing."""
    _measure_facade = _compat.facade()
    return _measure_facade._read_state_file(_measure_facade.DAEMON_TOKEN_PATH)


def _read_dashboard_host_file():
    """Return the persisted daemon bind-host, or '' if absent/unreadable."""
    _measure_facade = _compat.facade()
    return _measure_facade._read_state_file(_measure_facade.DAEMON_HOST_PATH)


def _persist_dashboard_host(persist=None):
    """v5.11.1: persist the daemon bind-host so the service-managed daemon
    (launchd/systemd/Task Scheduler) -- which starts with an empty environment --
    binds the host the user asked for instead of always 127.0.0.1.

    Reads TOKEN_OPTIMIZER_DASHBOARD_HOST (fallback TOKEN_OPTIMIZER_HOST):
      - set + valid (in the allowlist): write it to DAEMON_HOST_PATH.
      - set + invalid: warn naming the bad value and the allowed values; do NOT
        persist (and do NOT clobber an existing valid file).
      - unset: leave any existing file untouched. The setting is sticky so a
        network setup survives re-running setup-daemon without the env var
        (e.g. during an upgrade that re-invokes the installer).

    persist=False resolves without writing (dry-run path), so the env > file >
    default chain lives in exactly one place.

    The setting is per-runtime: each daemon (Claude/Codex/Hermes/Copilot) has
    its own DAEMON_HOST_PATH and persists independently, so run setup-daemon
    under each runtime you want to expose on the network.

    Returns the effective host for messaging (env var > file > '127.0.0.1').
    """
    _measure_facade = _compat.facade()
    env_raw = _measure_facade.os.environ.get(
        "TOKEN_OPTIMIZER_DASHBOARD_HOST",
        _measure_facade.os.environ.get("TOKEN_OPTIMIZER_HOST", ""),
    ).strip()
    if env_raw:
        if env_raw in _measure_facade._DAEMON_HOST_ALLOWLIST:
            if persist:
                try:
                    _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                    # v5.11.1: O_CREAT|O_TRUNC with 0o600 + fdopen, matching
                    # the daemon-token write -- avoids a umask-widened world-readable
                    # file and the symlink-replacement write that plain write_text
                    # (which follows symlinks) would allow.
                    fd = _measure_facade.os.open(
                        str(_measure_facade.DAEMON_HOST_PATH),
                        _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC,
                        0o600,
                    )
                    with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(env_raw + "\n")
                except OSError as e:
                    print(
                        f"[Token Optimizer] Warning: could not persist dashboard host "
                        f"to {_measure_facade.DAEMON_HOST_PATH}: {e}",
                        file=_measure_facade.sys.stderr,
                    )
            return env_raw
        # Invalid value: warn and fall through to the existing/default host.
        print(
            f"[Token Optimizer] Ignoring invalid dashboard host "
            f"{env_raw!r}. Allowed: {', '.join(_measure_facade._DAEMON_HOST_ALLOWLIST)}.",
            file=_measure_facade.sys.stderr,
        )
    # Env unset or invalid: honor any sticky persisted host, else default.
    return _measure_facade._read_dashboard_host_file() or "127.0.0.1"


def _is_localhost_host_header(host_header):
    """True iff the Host header is a recognised localhost form (DNS rebinding guard).

    Accepts: 'localhost', 'localhost:<port>', '127.0.0.1', '127.0.0.1:<port>',
    '[::1]', '[::1]:<port>'. Rejects anything else, closing the DNS-rebinding
    leak path where an attacker-controlled hostname resolves to 127.0.0.1
    and the browser sends same-origin requests whose Host is the attacker's
    domain (H-1, 2026-04-16).
    """
    _measure_facade = _compat.facade()
    if not host_header:
        return False
    h = host_header.strip().lower()
    # Strip port if present (bracketed IPv6 is handled below)
    if h.startswith("["):
        # [::1] or [::1]:port
        return h.startswith("[::1]") and (h == "[::1]" or h.startswith("[::1]:"))
    # bare or ipv4-with-port
    base = h.split(":", 1)[0]
    return base in ("localhost", "127.0.0.1")


def _generate_daemon_script():
    """Generate a minimal Python HTTP server script for the dashboard daemon.

    v5.4.19 hardening (security):
      - C-2: reject empty Origin and non-localhost Origin on POST.
      - H-1: Host header allowlist (localhost / 127.0.0.1 / [::1]) — DNS rebind defense.
      - H-2/M-4: per-install X-TO-Token header required on mutating endpoints.
      - 3-strikes thrash breadcrumb when DASHBOARD is missing (avoid KeepAlive
                 hot-loop with launchd/systemd).
      - honor breadcrumb tombstone to self-exit cleanly after uninstall.
      - Serves /api/token to localhost Origin+Host only so dashboard JS can fetch it.

    Paths are interpolated via repr() to produce properly escaped Python string
    literals. Without this, Windows paths containing backslash escape sequences
    (\\U, \\N, \\x, \\t) would raise SyntaxError when the generated daemon starts.
    """
    _measure_facade = _compat.facade()
    dashboard_literal = repr(str(_measure_facade.DASHBOARD_PATH))
    measure_path = _measure_facade.Path(_measure_facade.__file__).resolve()
    measure_py_literal = repr(str(measure_path))
    # A daemon can be generated by a runtime cache (for example .codex) while
    # serving another runtime's plugin-data directory (for example .claude).
    # Keep the marketplace identity so the generated resolver can select the
    # unversioned source belonging to the served runtime before using this path.
    measure_marketplace = "alexgreensh-token-optimizer"
    try:
        cache_index = measure_path.parts.index("cache")
        if cache_index + 1 < len(measure_path.parts):
            measure_marketplace = measure_path.parts[cache_index + 1]
    except ValueError:
        pass
    measure_marketplace_literal = repr(measure_marketplace)
    token_path_literal = repr(str(_measure_facade.DAEMON_TOKEN_PATH))
    host_path_literal = repr(str(_measure_facade.DAEMON_HOST_PATH))
    thrash_path_literal = repr(str(_measure_facade.DAEMON_THRASH_BREADCRUMB))
    log_dir_literal = repr(str(_measure_facade.DAEMON_LOG_DIR))
    tombstone_marker_literal = repr(_measure_facade._UNINSTALL_TOMBSTONE_MARKER)
    magic_literal = repr(_measure_facade.DAEMON_IDENTITY_MAGIC)
    return f'''#!/usr/bin/env python3
"""Token Optimizer dashboard server daemon.
Auto-generated by measure.py v{_measure_facade.TOKEN_OPTIMIZER_VERSION}. Serves the dashboard HTML on localhost:{_measure_facade.DAEMON_PORT}.
The SessionEnd hook regenerates the HTML file; this daemon just serves what's on disk.
"""
# Real module constant (the line above used to live INSIDE the
# docstring, so /api/health's reference to TOKEN_OPTIMIZER_DAEMON_VERSION raised
# NameError and the health route crashed on every call). Exposed as a cheap
# constant the non-blocking health route can answer from without doing work.
TOKEN_OPTIMIZER_DAEMON_VERSION = "{_measure_facade.TOKEN_OPTIMIZER_VERSION}"
import hmac
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time

# The daemon now runs on a ThreadingTCPServer so a synchronous
# api/regenerate (10-17s) no longer blocks liveness probes (/api/health,
# /__to_ping) and other requests. Every module global mutated per-request is
# guarded by _STATE_LOCK so concurrent handler threads cannot race on:
#   - _regen_inflight (overlap refusal)
#   - _last_regen (freshness throttle)
#   - _MEASURE_PY_CACHE (measure.py resolution cache)
#   - _REJECT_LOG_LAST_TS (per-path reject-log throttle)
# The lock is held only for the brief read/modify/write of each flag, never
# across the long subprocess.run() of a regen (that would re-introduce the
# single-threaded block). Security checks (Host allowlist, Origin, hmac token)
# run unchanged in each thread; they read only constants / a per-request token
# file, so they need no lock.
_STATE_LOCK = threading.Lock()

# Unbounded-thread regression: ThreadingTCPServer spawns
# one worker thread per accepted connection with NO cap, and daemon_threads=True
# only affects EXIT, not the live count. A slowloris (many connections that never
# send a request line) or a flood of valid-token requests would otherwise spawn
# unbounded threads -- and, via the regen endpoints, unbounded child processes.
# Two conservative bounds close that (the daemon serves a single local user):
#   - _WORKER_SEM caps concurrently-handled connections. The accept loop acquires
#     it NON-BLOCKING before dispatching a worker, so it never stalls (health
#     probes keep being accepted); on saturation it answers a bare 503 and closes
#     instead of spawning another thread.
#   - DAEMON_REQUEST_TIMEOUT is applied to each connection's socket (Handler.timeout
#     -> settimeout), so a peer that connects but never finishes sending a request
#     line is dropped after the timeout, freeing its worker. Without this a slow/
#     idle connection could pin a bounded worker slot forever (slowloris).
DAEMON_MAX_WORKERS = 12
DAEMON_REQUEST_TIMEOUT = 10.0
_WORKER_SEM = threading.BoundedSemaphore(DAEMON_MAX_WORKERS)

# The daemon shells out to `python measure.py ...` for the v5-toggle,
# skill/MCP-manage, and manual-regenerate endpoints. python.exe is a console
# subsystem binary, so without CREATE_NO_WINDOW each of those spawns flashes a
# cmd window on Windows. getattr -> 0 on POSIX makes it a no-op there.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LOG_DIR = {log_dir_literal}

# The Scheduled Task now launches this script with pythonw.exe
# (GUI subsystem) instead of the .cmd launcher, because Task Scheduler paints a
# console window for any console-subsystem action and no creationflags of ours
# can stop it. pythonw gives the process NO std handles at all, so the
# stdout.log / stderr.log trail the .cmd used to provide would vanish -- and a
# silent daemon death is exactly the failure mode that once looked healthy for
# two days. Reopen the handles onto the same log files before anything runs.
LOG_CAP_BYTES = 1048576


def _cap_log(path, max_bytes=LOG_CAP_BYTES):
    """Keep a log under max_bytes by dropping its older half. Truncates in place
    so a handle another process holds (launchd, the shell redirect) stays valid.
    Never raises."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "rb") as fh:
            fh.seek(-(max_bytes // 2), os.SEEK_END)
            tail = fh.read()
        nl = tail.find(b"\\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1:]
        with open(path, "r+b") as fh:
            fh.write(tail)
            fh.truncate()
    except OSError:
        pass


# Daemon logs used to grow forever (every traceback, with local paths).
for _log_name in ("stdout.log", "stderr.log"):
    _cap_log(os.path.join(LOG_DIR, _log_name))

if sys.stdout is None or sys.stderr is None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if sys.stdout is None:
            sys.stdout = open(os.path.join(LOG_DIR, "stdout.log"), "a",
                              encoding="utf-8", errors="replace", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(os.path.join(LOG_DIR, "stderr.log"), "a",
                              encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        pass

DASHBOARD = {dashboard_literal}
TOKEN_PATH = {token_path_literal}
HOST_PATH = {host_path_literal}
THRASH_PATH = {thrash_path_literal}
UNINSTALL_MARKER = {tombstone_marker_literal}
IDENTITY_MAGIC = {magic_literal}
PORT = {_measure_facade.DAEMON_PORT}

# thrash + tombstone guard. If dashboard has been missing for
# 3 consecutive starts (reset when found), write a tombstone and exit with
# code 0 so launchd/systemd consider this a "successful exit" and stop
# respawning us. Uninstall also writes this tombstone directly.
THRASH_LIMIT = 3

# Freshness-on-open: if the cached dashboard HTML is older than this when a GET
# arrives, kick off one background regen (throttled by the same interval) and
# serve the current file immediately, so the next open reflects recent activity.
DASHBOARD_FRESH_SECONDS = 120
_last_regen = 0.0

# The path this daemon was GENERATED against. Used only as a last-resort fallback:
# it points into a versioned plugin-cache directory that any later upgrade may prune,
# which silently broke every regeneration for upgrading users (the daemon kept
# listening, so health checks stayed green while refresh was dead).
MEASURE_PY_FALLBACK = {measure_py_literal}
MEASURE_PY_MARKETPLACE = {measure_marketplace_literal}
_MEASURE_PY_CACHE = ("", 0.0)
_regen_inflight = False
REGEN_STEP_TIMEOUT = 45
# Regen race: the background stale-while-revalidate refresh
# is a fire-and-forget Popen, so nothing reaps its child inline. A daemon reaper
# thread waits for the child and clears _regen_inflight when it exits, bounded by
# this timeout so a hung child can never wedge the guard True forever (the manual
# regen path already caps each step at REGEN_STEP_TIMEOUT; this is one step + headroom).
REGEN_BG_TIMEOUT = REGEN_STEP_TIMEOUT + 45
MEASURE_PY_RESOLVE_TTL = 300
REGEN_LOG = os.path.join(os.path.dirname(DASHBOARD), "daemon-regen.log")


def _log_regen(msg):
    """Append a regeneration event. Never raises; the daemon must survive a dead log."""
    _cap_log(REGEN_LOG)
    try:
        with open(REGEN_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def _reap_bg_regen(proc):
    """Wait for a fire-and-forget background-regen child and clear _regen_inflight
    when it exits, so the manual-regen path (which shares the flag under _STATE_LOCK)
    can never overlap a background refresh. Bounded wait: if the child hangs past
    REGEN_BG_TIMEOUT the guard is cleared anyway, so it can never stick True and
    refuse regeneration forever. Runs on a daemon thread; never raises."""
    global _regen_inflight
    try:
        proc.wait(timeout=REGEN_BG_TIMEOUT)
    except Exception:  # TimeoutExpired or any wait error: do not hold the guard forever
        _log_regen("background regen reaper: child did not exit within %ss; clearing guard"
                   % REGEN_BG_TIMEOUT)
    finally:
        with _STATE_LOCK:
            _regen_inflight = False


# Rate-limited trace for a rejected api/* POST. The M-4 token check runs BEFORE
# the api/regenerate handler, so a rejected regenerate click used to leave NO
# trace in daemon-regen.log -- it stayed empty for weeks while the user reported
# a dead button. This writes one line per _REJECT_LOG_MIN_GAP seconds PER PATH
# so a hammering/looping client cannot spam the log, but a real rejected click
# on a DIFFERENT endpoint is never invisible (was global, which dropped a
# rejected toggle trace after a recent rejected regenerate). The path is
# sanitized so a CR in the request path cannot inject/overwrite a log line
# via terminal carriage-return overprint.
_REJECT_LOG_LAST_TS: dict = {{}}
_REJECT_LOG_MIN_GAP = 30.0
# Cap the per-path throttle dict. It lives for the daemon's lifetime (launchd
# KeepAlive) and _log_reject_regen fires before token auth, so a client -- remote
# in opt-in network mode -- POSTing many distinct bad-token paths would otherwise
# grow it without bound (memory-leak / OOM-restart DoS). Evict oldest half at cap.
_REJECT_LOG_MAX_KEYS = 512


def _sanitize_log_path(path):
    """Keep only printable ASCII in *path* so a request path containing control
    chars cannot inject or overwrite a log line. Drops CR/LF AND ESC and every
    other control byte (a bare ESC sequence like \\x1b[2J would clear the screen
    when the log is cat'd). Returns the cleaned string."""
    if not path:
        return ""
    return "".join(c for c in path if 32 <= ord(c) < 127)


def _log_reject_regen(path):
    """One rate-limited line PER PATH when an api/* POST is rejected for a bad
    token. Per-path throttle so a rejected toggle is still visible after a
    recent rejected regenerate. Path is sanitized before writing."""
    global _REJECT_LOG_LAST_TS
    now = time.time()
    clean = _sanitize_log_path(path)
    # _REJECT_LOG_LAST_TS is a shared module global; under the
    # threading server two POSTs can reject concurrently. Hold _STATE_LOCK only
    # for the dict mutation, not for the file write below.
    with _STATE_LOCK:
        last = _REJECT_LOG_LAST_TS.get(clean, 0.0)
        if now - last < _REJECT_LOG_MIN_GAP:
            return
        _REJECT_LOG_LAST_TS[clean] = now
        if len(_REJECT_LOG_LAST_TS) > _REJECT_LOG_MAX_KEYS:
            _stale = sorted(_REJECT_LOG_LAST_TS, key=_REJECT_LOG_LAST_TS.get)[:len(_REJECT_LOG_LAST_TS) // 2]
            for _k in _stale:
                _REJECT_LOG_LAST_TS.pop(_k, None)
    _cap_log(REGEN_LOG)
    try:
        with open(REGEN_LOG, "a", encoding="utf-8") as f:
            f.write("%s REJECT api/* POST token-mismatch path=%s\\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S"), clean))
    except OSError:
        pass


def _resolve_measure_py():
    """Find the NEWEST installed measure.py at call time, not at generation time.

    Mirrors the discovery the token-dashboard skill already does: walk the known
    install roots, read each copy's .claude-plugin/plugin.json version, keep the
    highest. Resolving at call time is the whole point -- a baked absolute path
    dies the moment an upgrade prunes the version that generated this file.
    Cached for MEASURE_PY_RESOLVE_TTL so a busy daemon does not re-walk per request.
    """
    global _MEASURE_PY_CACHE
    # Cache read under _STATE_LOCK so a concurrent freshness regen
    # and a manual regen don't both walk the container and clobber the cache.
    with _STATE_LOCK:
        cached, when = _MEASURE_PY_CACHE
    now = time.time()
    if cached and now - when < MEASURE_PY_RESOLVE_TTL and os.path.isfile(cached):
        return cached
    # SECURITY: resolution stays inside the SAME plugin identity this daemon was
    # generated from. An earlier version walked seven install roots with
    # followlinks=True and picked whichever copy self-reported the highest version
    # in its .claude-plugin/plugin.json, then executed it. That let any unrelated
    # plugin shipping a nested token-optimizer/scripts/measure.py and a manifest
    # claiming a huge version win the contest, and let a symlink under any walked
    # root escape the roots entirely -- a code-execution path where there had
    # previously been a static self-reference. Narrowed deliberately:
    #   - no directory walk and no symlink following
    #   - candidates must be siblings under the generating identity's version
    #     container, so nothing new is trusted
    #   - the version comes from the DIRECTORY NAME, not from file contents any
    #     other writer controls
    #   - the resolved path is containment-checked before it is ever executed
    fallback = MEASURE_PY_FALLBACK
    # Resolve against the install whose dashboard is actually being served.
    # A .claude data dashboard may be generated by a .codex measure.py process;
    # executing the fallback in that case writes a different snapshot and makes
    # POST /api/regenerate return success without changing the visible file.
    # The marketplace clone is version-independent and remains inside the same
    # runtime's plugins root. Legacy/backups dashboards have no plugin-data
    # shape, so they continue through the identity-scoped fallback below.
    try:
        dashboard_dir = os.path.realpath(os.path.dirname(DASHBOARD))
        dashboard_parts = dashboard_dir.split(os.sep)
        data_idx = next(
            i for i in range(1, len(dashboard_parts))
            if dashboard_parts[i - 1] == "plugins" and dashboard_parts[i] == "data")
        if data_idx >= 1:
            plugins_root = os.sep.join(dashboard_parts[:data_idx])
            marketplaces_root = os.path.realpath(os.path.join(plugins_root, "marketplaces"))
            # 5.13.3: a daemon generated by one runtime (.codex) can serve
            # another runtime's (.claude) data dir. The baked MEASURE_PY_MARKETPLACE
            # is the GENERATING runtime's identity, so under the SERVED runtime it
            # may name a marketplace dir that does not exist -- the lookup missed
            # and fell through to the stale cross-runtime fallback, so regenerate
            # returned ok while the visible file never changed. Try the baked
            # identity first, then discover the served runtime's OWN token-optimizer
            # marketplace by scanning ITS marketplaces root. That root is the served
            # runtime's own plugins tree (same trust boundary as this daemon);
            # symlinks are rejected and every candidate is containment-checked.
            served_identities = []
            if MEASURE_PY_MARKETPLACE:
                served_identities.append(MEASURE_PY_MARKETPLACE)
            try:
                for entry in sorted(os.listdir(marketplaces_root)):
                    if entry not in served_identities and "token-optimizer" in entry:
                        served_identities.append(entry)
            except OSError:
                pass
            for served_ident in served_identities:
                served_candidate = os.path.join(
                    marketplaces_root, served_ident,
                    "skills", "token-optimizer", "scripts", "measure.py")
                served_real = os.path.realpath(served_candidate)
                if (os.path.isfile(served_candidate) and not os.path.islink(served_candidate)
                        and (served_real == marketplaces_root
                             or served_real.startswith(marketplaces_root + os.sep))):
                    with _STATE_LOCK:
                        _MEASURE_PY_CACHE = (served_real, now)
                    return served_real
    except (OSError, ValueError, StopIteration):
        pass
    # .../<identity>/token-optimizer/<version>/skills/token-optimizer/scripts/measure.py
    version_dir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(fallback)))))
    container = os.path.dirname(version_dir)
    suffix = os.path.relpath(os.path.abspath(fallback), version_dir)
    best_path, best_ver = "", None
    try:
        entries = os.listdir(container)
    except OSError:
        entries = []
    container_real = os.path.realpath(container)
    for name in entries:
        cand_dir = os.path.join(container, name)
        if os.path.islink(cand_dir) or not os.path.isdir(cand_dir):
            continue
        # Take the leading numeric run and stop at the first non-numeric
        # component. A plain isdigit() filter silently DROPPED such parts, so
        # "5.11.66-rc1" became (5, 11) and sorted BELOW (5, 11, 60) -- the
        # daemon would pick an older stable over the newer build actually
        # installed. Stopping instead of skipping keeps the tuple positional.
        parts = []
        for chunk in name.split("."):
            digits = ""
            for ch in chunk:
                if not ch.isdigit():
                    break
                digits += ch
            if not digits:
                break
            parts.append(int(digits))
        if not parts:
            continue
        cand = os.path.join(cand_dir, suffix)
        if not os.path.isfile(cand) or os.path.islink(cand):
            continue
        # Containment: the real path must still sit under the real container.
        cand_real = os.path.realpath(cand)
        if not (cand_real == container_real or
                cand_real.startswith(container_real + os.sep)):
            continue
        ver = tuple(parts)
        if best_ver is None or ver > best_ver:
            best_ver, best_path = ver, cand
    if best_path:
        with _STATE_LOCK:
            _MEASURE_PY_CACHE = (best_path, now)
        return best_path
    if os.path.isfile(fallback):
        return fallback
    _log_regen("resolve failed: no installed measure.py under %s" % container)
    return fallback


def _read_token():
    """Read per-install CSRF token from disk. Empty string if missing/unreadable."""
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _is_localhost_host(host_header):
    """True iff Host header is a localhost form. Defeats DNS rebinding."""
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith("["):
        return h.startswith("[::1]") and (h == "[::1]" or h.startswith("[::1]:"))
    base = h.split(":", 1)[0]
    return base in ("localhost", "127.0.0.1")


def _is_localhost_origin(origin):
    """True iff Origin header is http(s)://localhost|127.0.0.1|[::1] or file://.

    v5.4.19 S-2 tightening: require the host to terminate (end-of-string,
    ':', or '/') so a malicious Origin like 'http://127.0.0.1.evil.com'
    cannot masquerade as localhost.
    """
    if not origin:
        return False
    for prefix in ("http://localhost", "http://127.0.0.1", "http://[::1]",
                   "https://localhost", "https://127.0.0.1", "https://[::1]"):
        if origin.startswith(prefix):
            tail = origin[len(prefix):]
            if tail == "" or tail.startswith(":") or tail.startswith("/"):
                return True
    return origin.startswith("file://")


class Handler(http.server.SimpleHTTPRequestHandler):
    # Slowloris defense: StreamRequestHandler.setup() applies this
    # as self.connection.settimeout(...), so a peer that connects but never finishes
    # sending a request line is dropped after DAEMON_REQUEST_TIMEOUT (raising
    # socket.timeout, which BaseHTTPRequestHandler.handle_one_request catches and
    # closes). Without it an idle connection would pin a bounded worker slot forever.
    timeout = DAEMON_REQUEST_TIMEOUT

    def __init__(self, *a, **kw):
        d = os.path.dirname(DASHBOARD)
        super().__init__(*a, directory=d, **kw)

    def log_message(self, fmt, *a):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def _is_dashboard_request(self):
        f = os.path.basename(DASHBOARD)
        clean = self.path.lstrip("/").split("?")[0]
        return clean in ("", "token-optimizer", f)

    def _is_identity_request(self):
        return self.path.lstrip("/").split("?")[0] == "__to_ping"

    def _respond_identity(self):
        # Return the magic string so foreign listeners on PORT can't masquerade.
        body = IDENTITY_MAGIC.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin", "")
        if NETWORK_MODE:
            allowed = origin or "*"
        else:
            allowed = origin if _is_localhost_origin(origin) else "http://localhost:{_measure_facade.DAEMON_PORT}"
        self.send_header("Access-Control-Allow-Origin", allowed)
        if allowed != "*":
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        self.wfile.write(body)

    def _require_localhost(self):
        """Reject non-localhost Host headers unless NETWORK_MODE is active.
        H-1 DNS rebinding defense — fires before we look at Origin/token.
        In network mode, any Host is accepted (remote clients send their own).
        """
        if NETWORK_MODE:
            return True
        if not _is_localhost_host(self.headers.get("Host", "")):
            self.send_error(421, "Misdirected Request")
            return False
        return True

    def _serve_or_redirect(self, method):
        if self._is_identity_request():
            self._respond_identity()
            return
        clean = self.path.lstrip("/").split("?")[0]
        if clean == "api/health":
            self._json_response(200, {{"ok": True, "server": "token-optimizer-daemon", "version": TOKEN_OPTIMIZER_DAEMON_VERSION}})
            return
        if clean == "api/token":
            # v5.11.1: the token endpoint is loopback-locked in BOTH modes.
            # In network mode we serve the dashboard HTML to LAN visitors (view-
            # only) but only hand out the CSRF token to clients connecting FROM
            # the machine itself (client_address loopback), so a LAN visitor can
            # never obtain the token and therefore can never toggle. Mutations
            # are effectively localhost-only. In default mode we additionally
            # require localhost Origin+Host (anti-exfiltration / DNS-rebind).
            if NETWORK_MODE:
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._json_response(403, {{"ok": False, "msg": "token endpoint is loopback-only"}})
                    return
            else:
                origin = self.headers.get("Origin", "")
                origin_ok = (not origin) or _is_localhost_origin(origin)
                if not origin_ok or not _is_localhost_host(self.headers.get("Host", "")):
                    self.send_error(403, "Forbidden")
                    return
            self._json_response(200, {{"token": _read_token()}})
            return
        if not self._require_localhost():
            return
        if self._is_dashboard_request():
            if method == "do_GET":
                self._maybe_refresh_dashboard()
            self.path = "/" + os.path.basename(DASHBOARD)
            getattr(super(), method)()
        else:
            self.send_error(403, "Forbidden")

    def _maybe_refresh_dashboard(self):
        # Stale-while-revalidate: serve the current HTML now, and if it's stale
        # kick off ONE throttled background regen so the next open is fresh. Never
        # blocks the request; failures are silently ignored (best-effort).
        global _last_regen, _regen_inflight
        try:
            mtime = os.path.getmtime(DASHBOARD)
        except OSError:
            return
        now = time.time()
        # _last_regen is a shared throttle stamp; under the threading
        # server two GETs can race the stale check. Hold _STATE_LOCK only for the
        # read+write of the stamp, not for the subprocess.Popen below.
        #
        # Regen race: the manual POST /api/regenerate path
        # guards overlap with _regen_inflight; this background path used to touch
        # only _last_regen, so under the threading server a background refresh and a
        # manual regen (or two backgrounds) could run concurrently. Claim the SAME
        # _regen_inflight flag here under the SAME lock: bail if a regen is already
        # in flight, otherwise set it True and let _reap_bg_regen clear it when the
        # fire-and-forget child exits (bounded, so it can never stick True).
        with _STATE_LOCK:
            if _regen_inflight:
                return
            if now - mtime <= DASHBOARD_FRESH_SECONDS or now - _last_regen <= DASHBOARD_FRESH_SECONDS:
                return
            _last_regen = now
            _regen_inflight = True
        # From here the flag is CLAIMED. It is handed to _reap_bg_regen only once the
        # child is actually spawned; on every earlier exit path we must clear it
        # ourselves or it would wedge True and refuse all future regeneration.
        spawned = False
        errf = None
        try:
            import subprocess
            target = _resolve_measure_py()
            if not os.path.isfile(target):
                _log_regen("SKIP regen: measure.py not found at %s" % target)
                return
            # stderr is captured to a log rather than discarded. Swallowing it is what
            # let a dead regeneration look identical to a healthy one for two days.
            _cap_log(REGEN_LOG)
            errf = open(REGEN_LOG, "a", encoding="utf-8")
            # The regen child is a transient worker (writes the new HTML and
            # exits); it does NOT need to survive the daemon, so DETACHED_PROCESS
            # would be wrong. On Windows it only needs CREATE_NO_WINDOW to avoid
            # a console flash; POSIX detaches via start_new_session. Inlined
            # (not a shared helper) because this template generates a STANDALONE
            # daemon script with no sibling helper module on its sys.path.
            _regen_kwargs = dict(stdout=subprocess.DEVNULL, stderr=errf,
                                 stdin=subprocess.DEVNULL, close_fds=True)
            if os.name == "nt":
                _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if _flags:
                    _regen_kwargs["creationflags"] = _flags
            else:
                _regen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                [sys.executable, target, "dashboard", "--quiet"],
                **_regen_kwargs,
            )
            # Hand the guard's lifetime to a daemon reaper: it clears _regen_inflight
            # when the child exits (or after REGEN_BG_TIMEOUT if it hangs), so the
            # manual regen path cannot overlap this one and the flag never sticks.
            threading.Thread(target=_reap_bg_regen, args=(proc,), daemon=True).start()
            spawned = True
        except Exception as exc:  # best-effort background path: never crash the serving thread
            _log_regen("regen launch failed: %s" % exc)
        finally:
            if errf is not None:
                # Popen duplicates the fd via start_new_session/creationflags;
                # the parent must close its own copy so the file handle doesn't
                # leak per stale-open (EMFILE after ~1024 regens).
                errf.close()
            if not spawned:
                # Never reached the reaper -> release the guard we claimed so a
                # failed/skipped background refresh can't block every future regen.
                with _STATE_LOCK:
                    _regen_inflight = False

    def do_OPTIONS(self):
        self.send_response(200)
        origin = self.headers.get("Origin", "")
        if NETWORK_MODE:
            allowed = origin or "*"
        else:
            allowed = origin if _is_localhost_origin(origin) else "http://localhost:{_measure_facade.DAEMON_PORT}"
        self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-TO-Token")
        if allowed != "*":
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()

    def do_POST(self):
        # Layered defense: Host allowlist (skipped in network mode), then Origin, then token.
        if not NETWORK_MODE and not _is_localhost_host(self.headers.get("Host", "")):
            self.send_error(421, "Misdirected Request")
            return
        origin = self.headers.get("Origin", "")
        if not NETWORK_MODE and (not origin or not _is_localhost_origin(origin)):
            self.send_error(403, "Forbidden: invalid origin")
            return
        expected_tok = _read_token()
        got_tok = self.headers.get("X-TO-Token", "")
        clean = self.path.lstrip("/").split("?")[0]
        if not expected_tok or not hmac.compare_digest(expected_tok, got_tok):
            # Leave a trace. This check fires BEFORE the api/regenerate
            # handler, so without a log line a rejected regenerate click was
            # indistinguishable from a dead button (daemon-regen.log stayed
            # empty for weeks). Rate-limited so a looping client can't spam.
            _log_reject_regen(clean)
            self.send_error(403, "Forbidden: invalid token")
            return

        if clean == "api/v5/toggle":
            length = int(self.headers.get("Content-Length", 0))
            if length > 4096:
                self._json_response(413, {{"ok": False, "msg": "payload too large"}})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {{}}
            except (ValueError, UnicodeDecodeError):
                self._json_response(400, {{"ok": False, "msg": "invalid json"}})
                return
            name = body.get("name", "")
            enabled = bool(body.get("enabled", False))
            # Validate name: alphanumeric + hyphens/underscores only.
            # Prevents argument injection via subprocess argv.
            import re as _re, subprocess
            if not name or not _re.match(r'^[a-zA-Z0-9_-]{{1,64}}$', name):
                self._json_response(400, {{"ok": False, "msg": "invalid feature name"}})
                return
            action = "enable" if enabled else "disable"
            try:
                result = subprocess.run(
                    [sys.executable, _resolve_measure_py(), "v5", action, name, "--json"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=_NO_WINDOW
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                self._json_response(500, {{"ok": False, "msg": "toggle backend unavailable: " + str(e)}})
                return
            if result.returncode == 0:
                v5_features = {{}}
                try:
                    parsed = json.loads(result.stdout)
                    v5_features = parsed.get("v5_features", {{}})
                except (ValueError, KeyError):
                    pass
                state = "ENABLED" if enabled else "DISABLED"
                self._json_response(200, {{"ok": True, "msg": state + ": " + name, "v5_features": v5_features}})
            else:
                self._json_response(500, {{"ok": False, "msg": result.stderr.strip()}})
            return
        if clean in ("api/skill/archive", "api/skill/restore", "api/mcp/disable", "api/mcp/enable"):
            # Skill/MCP management. The dashboard POSTs here; the daemon proxies
            # to `measure.py api-manage <endpoint> <name>` (single runtime-aware
            # source of truth) and forwards its JSON. Previously these paths fell
            # through to the 403 below, which the page surfaced as a misleading
            # "Authentication failed" -- breaking skill/MCP toggles for every
            # daemon user even though auth was fine.
            length = int(self.headers.get("Content-Length", 0))
            if length > 4096:
                self._json_response(413, {{"ok": False, "msg": "payload too large"}})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {{}}
            except (ValueError, UnicodeDecodeError):
                self._json_response(400, {{"ok": False, "msg": "invalid json"}})
                return
            name = body.get("name", "")
            # Permit real skill/MCP names (hyphens, dots, slashes, @, spaces);
            # passed as a single argv element (no shell) so this is belt-and-
            # suspenders against absurd input, not a shell-injection guard.
            import re as _re, subprocess
            if not name or not _re.match(r'^[A-Za-z0-9_.@:/ -]{{1,160}}$', name):
                self._json_response(400, {{"ok": False, "msg": "invalid name"}})
                return
            endpoint = clean[len("api/"):]  # e.g. "skill/archive"
            try:
                result = subprocess.run(
                    [sys.executable, _resolve_measure_py(), "api-manage", endpoint, name],
                    capture_output=True, text=True, timeout=15,
                    creationflags=_NO_WINDOW
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                self._json_response(500, {{"ok": False, "msg": "manage backend unavailable: " + str(e)}})
                return
            try:
                payload = json.loads(result.stdout)
            except (ValueError, KeyError):
                self._json_response(500, {{"ok": False, "msg": (result.stderr or result.stdout or "manage failed").strip()[:200]}})
                return
            self._json_response(200 if payload.get("ok") else 500, payload)
            return
        if clean == "api/regenerate":
            # Synchronous, unlike the background freshness regen: the caller is a
            # human who pressed a button and needs a real success/failure answer,
            # not a fire-and-forget that looks identical whether it worked or not.
            # That ambiguity is exactly what let the daemon serve a two-day-old
            # dashboard while every health check reported green.
            import subprocess
            target = _resolve_measure_py()
            if not os.path.isfile(target):
                _log_regen("MANUAL regen failed: measure.py not found at " + target)
                self._json_response(500, {{"ok": False, "msg": "measure.py not found; reinstall or run setup-daemon"}})
                return
            # The server is now a ThreadingTCPServer, so a regen no
            # longer blocks other requests. The _regen_inflight guard still
            # refuses OVERLAPPING regens (a second click while the first runs).
            # The lock is held ONLY for the check+set and the final clear -- never
            # across subprocess.run(), or it would re-introduce the single-threaded
            # block for every other request that touches any guarded global.
            global _regen_inflight
            with _STATE_LOCK:
                if _regen_inflight:
                    self._json_response(409, {{"ok": False, "msg": "a regeneration is already running"}})
                    return
                _regen_inflight = True
            try:
                for step in ("collect", "dashboard"):
                    # A human pressed Regenerate: the "dashboard" step MUST carry
                    # --force so the 60s write throttle can never silently no-op the
                    # click into serving the same stale file (the very ambiguity that
                    # let a two-day-old dashboard look freshly regenerated). collect
                    # has no such throttle, so it needs no flag.
                    argv = [sys.executable, target, step, "--quiet"]
                    if step == "dashboard":
                        argv.append("--force")
                    # retry cap: retry ONLY the "dashboard" step -- the
                    # one with the observed intermittent hang (lock contention
                    # makes the normally-~9s build blow past a single
                    # REGEN_STEP_TIMEOUT shot) -- and do NOT double the cap on the
                    # retry. The earlier "2 attempts per step, doubled cap" put the
                    # worst case at REGEN_STEP_TIMEOUT*(1+2)*2 = 6x = 270s, WORSE
                    # than the ~240s wedge the original per-step cap fixed. Capping
                    # every attempt at a flat REGEN_STEP_TIMEOUT and retrying only
                    # the flaky step bounds the worst case at
                    # REGEN_STEP_TIMEOUT*(1 + 2) = 3x = 135s, well under 240s, while
                    # still surviving one transient dashboard hang. `collect` has no
                    # observed hang, so it stays single-shot.
                    _max_attempts = 2 if step == "dashboard" else 1
                    r = None
                    _timeout_exc = None
                    for _attempt in range(_max_attempts):
                        _to = REGEN_STEP_TIMEOUT
                        try:
                            r = subprocess.run(
                                argv,
                                capture_output=True, text=True, timeout=_to,
                                creationflags=_NO_WINDOW,
                                # A human pressed Regenerate and is waiting: mark it
                                # INTERACTIVE so the child is not killed by the 20s
                                # hook budget (Bug B). The daemon's own per-attempt
                                # timeout is the real, saner cap here.
                                env={{**os.environ, "TOKEN_OPTIMIZER_INTERACTIVE": "1"}},
                            )
                            _timeout_exc = None
                            break
                        except subprocess.TimeoutExpired as _te:
                            _timeout_exc = _te
                            _log_regen("MANUAL regen %s timed out after %d seconds (attempt %d/%d)" % (step, _to, _attempt + 1, _max_attempts))
                    if _timeout_exc is not None:
                        _log_regen("MANUAL regen error: %s" % _timeout_exc)
                        self._json_response(500, {{"ok": False, "step": step, "msg": "regeneration timed out: " + str(_timeout_exc)}})
                        return
                    if r.returncode != 0:
                        msg = (r.stderr or r.stdout or "").strip()[:300] or ("%s exited %d" % (step, r.returncode))
                        _log_regen("MANUAL regen failed at %s: %s" % (step, msg))
                        self._json_response(500, {{"ok": False, "step": step, "msg": msg}})
                        return
            except OSError as e:
                _log_regen("MANUAL regen error: %s" % e)
                self._json_response(500, {{"ok": False, "msg": "regeneration failed: " + str(e)}})
                return
            finally:
                # Must clear on EVERY exit path, including the mid-loop failure
                # returns above; a stuck flag would refuse regeneration forever.
                with _STATE_LOCK:
                    _regen_inflight = False
            try:
                generated = os.path.getmtime(DASHBOARD)
            except OSError:
                generated = 0
            _log_regen("MANUAL regen OK via %s" % target)
            self._json_response(200, {{"ok": True, "generated_at": generated, "measure_py": target}})
            return
        self.send_error(403, "Forbidden")

    def do_GET(self):
        self._serve_or_redirect("do_GET")

    def do_HEAD(self):
        self._serve_or_redirect("do_HEAD")


def _thrash_check_and_update():
    """Return True to continue, False to tombstone-and-exit cleanly."""
    # two breadcrumb kinds live at THRASH_PATH.
    #   - UNINSTALL tombstone: content == UNINSTALL_MARKER. Written by
    #     `measure.py` uninstall; it must OUTLIVE the uninstall and is cleared
    #     ONLY by a reinstall (setup_daemon unlinks it). Honor it FOREVER -- a
    #     surviving daemon script must never resurrect and mint a fresh CSRF
    #     token after a reported uninstall.
    #   - THRASH tombstone: EMPTY file, written by the 3-strikes path below when
    #     the dashboard has been missing. That is transient (e.g. an update
    #     window), so it self-heals after 60s once the dashboard is back.
    if os.path.exists(THRASH_PATH):
        try:
            with open(THRASH_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            content = ""
        if content == UNINSTALL_MARKER:
            # Real uninstall: stay dead. Never self-clear, never serve.
            return False
        if content == "":
            # Empty = 3-strikes thrash tombstone. Self-heal only when STALE: a
            # stuck tombstone next to a healthy dashboard with this daemon still
            # running is a thrash false-positive, not an uninstall.
            try:
                age = time.time() - os.path.getmtime(THRASH_PATH)
            except OSError:
                age = 0  # unreadable mtime: treat as fresh, preserve safety
            if age < 60:
                return False
            try:
                os.unlink(THRASH_PATH)
            except OSError:
                pass
        # else: non-empty, non-marker -> a numeric thrash counter; fall through
        # to the dashboard check / counter processing below.

    if os.path.exists(DASHBOARD):
        # Healthy start: clear any thrash counter.
        try:
            if os.path.exists(THRASH_PATH):
                os.unlink(THRASH_PATH)
        except OSError:
            pass
        return True

    # dashboard is missing. Increment 3-strikes counter.
    count = 0
    try:
        with open(THRASH_PATH, "r", encoding="utf-8") as f:
            count = int((f.read() or "0").strip() or "0")
    except (OSError, ValueError):
        count = 0
    count += 1
    try:
        os.makedirs(os.path.dirname(THRASH_PATH), exist_ok=True)
        with open(THRASH_PATH, "w", encoding="utf-8") as f:
            f.write(str(count) + "\\n")
    except OSError:
        pass
    if count >= THRASH_LIMIT:
        # Write tombstone (empty file) so future respawns exit immediately.
        try:
            with open(THRASH_PATH, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            pass
        return False
    # Under threshold: still exit 0 (don't fight KeepAlive) but without tombstone,
    # so next SessionEnd regenerating dashboard.html will reset the counter.
    return False


if not _thrash_check_and_update():
    # Exit 0 so launchd's "KeepAlive on unsuccessful exit" stops respawning us.
    sys.exit(0)

# Bind address. Default: localhost only. TOKEN_OPTIMIZER_DASHBOARD_HOST=0.0.0.0 enables
# network access (Tailscale Funnel, LAN). When network-bound, Host header checks are
# relaxed since remote clients send non-localhost Host headers. Token auth still applies.
# Resolution: env var -> persisted host file (HOST_PATH, written by
# setup-daemon) -> 127.0.0.1. The service manager starts us with an empty
# environment, so the env var is usually absent here; the host file carries the
# user's choice across to the daemon and survives version-bump auto-regen.
_LOCALHOST_ADDRS = ("127.0.0.1", "::1", "localhost")
_host_raw = os.environ.get("TOKEN_OPTIMIZER_DASHBOARD_HOST",
                           os.environ.get("TOKEN_OPTIMIZER_HOST", "")).strip()
if not _host_raw:
    try:
        with open(HOST_PATH, "r", encoding="utf-8") as _hf:
            _host_raw = _hf.read().strip()
    except FileNotFoundError:
        # Normal: setup-daemon never persisted a host. Default to loopback.
        _host_raw = ""
    except OSError as _e:
        # v5.11.1: a non-missing read failure (e.g. PermissionError) is
        # worth a one-line note in the service-manager logs before we silently
        # fall back to loopback -- otherwise a misconfigured host file looks
        # like the daemon ignored the setting for no reason.
        print(f"[Token Optimizer] Warning: could not read dashboard host file {{HOST_PATH}}: {{_e}}", file=sys.stderr)
        _host_raw = ""
if not _host_raw:
    _host_raw = "127.0.0.1"
HOST = _host_raw if _host_raw in _LOCALHOST_ADDRS + ("0.0.0.0",) else "127.0.0.1"
NETWORK_MODE = HOST == "0.0.0.0"
if NETWORK_MODE:
    print(f"[Token Optimizer] Network mode: binding {{HOST}}:{{PORT}}", file=sys.stderr)
    print("  Dashboard and toggle API accessible from LAN. Token auth required for mutations.", file=sys.stderr)
try:
    # ThreadingTCPServer so a synchronous api/regenerate (10-17s)
    # does NOT block liveness probes (/api/health, /__to_ping) or other
    # requests. daemon_threads=True so worker threads die with the daemon and
    # never block process exit; allow_reuse_address=True so a fast restart
    # (KeepAlive respawn) does not hit TIME_WAIT on the port. Per-request shared
    # state is guarded by _STATE_LOCK; the _regen_inflight flag still refuses
    # overlapping regens. Security (Host allowlist, Origin, hmac token) runs
    # unchanged in every thread.
    class _DaemonServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

        def process_request(self, request, client_address):
            # Bound concurrent workers.
            # Acquire NON-BLOCKING in the accept loop so it never stalls (health
            # probes keep being accepted even at saturation). On saturation, answer
            # a bare 503 and close instead of spawning another unbounded worker
            # thread + child regen process. The permit is released in
            # process_request_thread's finally, exactly once per successful acquire.
            if not _WORKER_SEM.acquire(blocking=False):
                try:
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\\r\\n"
                        b"Content-Length: 0\\r\\n"
                        b"Connection: close\\r\\n\\r\\n"
                    )
                except OSError:
                    pass
                self.shutdown_request(request)
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            # Release the worker permit on EVERY exit path (handler exception
            # included) so the bounded pool can never leak permits and wedge shut.
            try:
                super().process_request_thread(request, client_address)
            finally:
                _WORKER_SEM.release()

    with _DaemonServer((HOST, PORT), Handler) as httpd:
        httpd.serve_forever()
except OSError:
    # Port in use by another process (or our old instance still dying).
    # Exit 0 cleanly so KeepAlive backs off via ThrottleInterval.
    sys.exit(0)
'''


def _write_file_if_changed(path, content, mode=None):
    """Write *content* to *path* only if it differs from what is already on disk.

    Returns True if the file was created or its content changed, False if it
    already matched (so nothing was written). macOS re-fires the "App Background
    Activity" / background-item banner whenever a LaunchAgent plist is rewritten
    -- even with byte-identical content -- so an unconditional rewrite on every
    ensure/repair pass spams the user. Skipping the no-op write leaves the file
    untouched and the banner silent.
    """
    _measure_facade = _compat.facade()
    try:
        if path.read_text(encoding="utf-8") == content:
            if mode is not None:
                try:
                    path.chmod(mode)
                except OSError:
                    pass
            return False
    except (OSError, ValueError):
        pass
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        try:
            path.chmod(mode)
        except OSError:
            pass
    return True


def _launchagent_loaded(label):
    """True if a LaunchAgent with *label* is currently loaded in this GUI domain.

    Used to decide whether a bootout+bootstrap is actually needed: if the plist
    did not change AND the agent is already loaded, reloading it only re-fires the
    macOS background-item banner for no benefit. Best-effort: any failure (no
    launchctl, non-macOS, timeout) returns False so the caller falls back to the
    reload it would have done anyway.
    """
    _measure_facade = _compat.facade()
    getuid = getattr(_measure_facade.os, "getuid", None)
    if getuid is None:
        return False
    try:
        # 2s, not 10s: a loaded agent answers in milliseconds, and this runs on the
        # SessionStart hot path. On timeout we return False and the caller falls
        # back to the reload it would have done anyway.
        r = _measure_facade.subprocess.run(
            ["launchctl", "print", f"gui/{getuid()}/{label}"],
            capture_output=True, text=True, timeout=2, creationflags=_measure_facade._NO_WINDOW,
        )
        return r.returncode == 0
    except (OSError, _measure_facade.subprocess.TimeoutExpired):
        return False


def _generate_plist():
    """Generate the launchd plist XML for the dashboard daemon."""
    _measure_facade = _compat.facade()
    from xml.sax.saxutils import escape as _xml_escape
    import shutil as _shutil
    _which = _shutil.which("python3") or ""
    python3_path = _which if (_which and "/shims/" not in _which) else "/usr/bin/python3"
    daemon_script = _xml_escape(str(_measure_facade.SNAPSHOT_DIR / "dashboard-server.py"))
    log_out = _xml_escape(str(_measure_facade.DAEMON_LOG_DIR / "stdout.log"))
    log_err = _xml_escape(str(_measure_facade.DAEMON_LOG_DIR / "stderr.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_measure_facade.DAEMON_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_xml_escape(python3_path)}</string>
        <string>{daemon_script}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""


def _ensure_dashboard_file(to_stderr=None):
    """Shared: generate the initial dashboard HTML if missing.

    All platform installers rely on the HTML file already existing on
    disk so the daemon has something to serve. Idempotent.

    ``to_stderr`` routes progress/error lines to stderr so they never
    land in the SessionStart stdout capture.
    """
    _measure_facade = _compat.facade()
    _out = _measure_facade.sys.stderr if to_stderr else _measure_facade.sys.stdout
    if not _measure_facade.DASHBOARD_PATH.exists():
        print("  Generating initial dashboard...", file=_out)
        _measure_facade.generate_standalone_dashboard(quiet=True)
    if not _measure_facade.DASHBOARD_PATH.exists():
        print("[Error] Could not generate dashboard. Run 'measure.py dashboard' first.", file=_out)
        return False
    return True


def _verify_daemon_port(timeout_seconds=None, retries=None, retry_sleep=None):
    """Probe 127.0.0.1:DAEMON_PORT to confirm OUR daemon is up (not a foreign listener).

    retry_sleep (default 1): seconds to wait between retries. The SessionStart
    ensure-health self-heal passes retry_sleep=0 with retries=1 so a single
    quick probe of a dead port returns immediately instead of burning ~1-2s of
    the hook budget on time.sleep -- the cheap-hot-path requirement.

    v5.4.19: a TCP connect() alone can't tell us whether the
    process on DAEMON_PORT is actually ours. A foreign app (or a stale orphan
    under a different dashboard path) that happens to bind 24842 would pass
    the connect check, and install code would assume success. We now follow
    the connect with a GET /__to_ping and verify the body equals our magic
    string. Only then do we claim the daemon is up.

    Retry budget configurable via TOKEN_OPTIMIZER_DASHBOARD_TIMEOUT (total
    seconds, default 30). Large trends DBs (~500+ sessions) can push Python
    cold-start past 20s on first launch.
    """
    _measure_facade = _compat.facade()
    if retries is None:
        try:
            total = max(0, int(_measure_facade.os.environ.get("TOKEN_OPTIMIZER_DASHBOARD_TIMEOUT", "30")))
        except (ValueError, TypeError):
            total = 30
        retries = max(total // 2, 4)
    import socket as _socket
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{_measure_facade.DAEMON_PORT}/__to_ping"
    for _attempt in range(retries):
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(timeout_seconds)
                s.connect(("127.0.0.1", _measure_facade.DAEMON_PORT))
        except (OSError, ConnectionRefusedError):
            if retry_sleep:
                _measure_facade.time.sleep(retry_sleep)
            continue
        # Port is open -- verify identity so we don't mistake a foreign listener for ours.
        try:
            req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{_measure_facade.DAEMON_PORT}"})
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = resp.read(256).decode("utf-8", errors="replace").strip()
            if body == _measure_facade.DAEMON_IDENTITY_MAGIC:
                return True
            # Wrong magic -- foreign listener or older build. Don't retry ident check.
            return False
        except (urllib.error.URLError, OSError, ValueError):
            if retry_sleep:
                _measure_facade.time.sleep(retry_sleep)
            continue
    return False


def _daemon_install_lock(soft_fail=None):
    """Return a context manager that serialises concurrent setup-daemon runs.

    v5.4.19: uses atomic mkdir as the mutex, but also reaps stale
    locks whose owner died (SIGKILL, crash, etc.). A PID file inside the lock
    directory records who took it; on contention, we check whether that PID is
    still alive and the lock was taken recently (mtime < 10 minutes). If the
    owner is gone OR the lock is older than 10 minutes, we reclaim it.

    soft_fail=True: return an inert contextmanager on contention
    rather than sys.exit(1). Used from hook paths where we must never kill
    the calling Claude Code session.
    """
    _measure_facade = _compat.facade()
    from contextlib import contextmanager

    lock_dir = _measure_facade.SNAPSHOT_DIR / ".setup-daemon.lock"
    pid_file = lock_dir / "pid"
    MAX_LOCK_AGE = 10 * 60  # 10 minutes

    def _pid_alive(pid):
        _measure_facade = _compat.facade()
        return _measure_facade._pid_is_running(pid)

    def _try_reclaim_stale():
        """Return True if we successfully reaped a stale lock, False otherwise."""
        _measure_facade = _compat.facade()
        try:
            mtime = lock_dir.stat().st_mtime
        except OSError:
            return False
        owner_pid = 0
        try:
            raw = pid_file.read_text(encoding="utf-8").strip()
            owner_pid = int(raw)
        except (OSError, ValueError):
            owner_pid = 0
        age = _measure_facade.time.time() - mtime
        if not _pid_alive(owner_pid) or age > MAX_LOCK_AGE:
            try:
                if pid_file.exists():
                    pid_file.unlink()
                lock_dir.rmdir()
                return True
            except OSError:
                return False
        return False

    @contextmanager
    def _inert():
        _measure_facade = _compat.facade()
        yield False  # acquired=False: caller can detect "skipped"

    @contextmanager
    def _locked():
        _measure_facade = _compat.facade()
        acquired = False
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            try:
                lock_dir.mkdir(exist_ok=False)
                acquired = True
            except FileExistsError:
                # Try to reap a stale lock, then retry once.
                if _try_reclaim_stale():
                    try:
                        lock_dir.mkdir(exist_ok=False)
                        acquired = True
                    except FileExistsError:
                        acquired = False
                else:
                    acquired = False
            if not acquired:
                if soft_fail:
                    yield False
                    return
                print("[Error] Another setup-daemon is already running.")
                print(f"  Lock file: {lock_dir}")
                print("  If you're sure no other run is active, remove the lock and retry.")
                _measure_facade.sys.exit(1)
            # Record ownership inside the lock for reaping.
            try:
                pid_file.write_text(str(_measure_facade.os.getpid()), encoding="utf-8")
            except OSError:
                pass
            yield True
        finally:
            if acquired:
                try:
                    if pid_file.exists():
                        pid_file.unlink()
                except OSError:
                    pass
                try:
                    lock_dir.rmdir()
                except OSError:
                    pass

    return _locked()


def _reclaim_posix_daemon_port(port=None, script_name=None):
    """SIGTERM any orphaned dashboard-server.py holding `port`.

    Upgrading from a prior version (v5.2 -> v5.3) can leave the old
    daemon alive after launchctl bootout, because bootout drops the
    launchd job without sending SIGTERM to the child. A subsequent
    bootstrap fails to bind. This helper identifies orphaned processes
    that are running our OWN daemon script and terminates them cleanly.
    Never kills a foreign process on the same port.
    """
    _measure_facade = _compat.facade()
    try:
        lsof = _measure_facade.subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        return
    if lsof.returncode != 0 or not lsof.stdout.strip():
        return
    for pid_str in lsof.stdout.strip().splitlines():
        try:
            pid = int(pid_str.strip())
        except ValueError:
            continue
        try:
            ps_out = _measure_facade.subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
            )
        except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
            continue
        if script_name in ps_out.stdout:
            try:
                _measure_facade.os.kill(pid, _measure_facade.signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            # Give the old process a moment to release the port.
            _measure_facade.time.sleep(0.5)


def _daemon_ports_to_reclaim(this_install_only: bool):
    """Which daemon ports a POSIX uninstall must SIGTERM-reclaim.

    A running-process kill was added on uninstall because
    ``launchctl bootout`` / ``systemctl disable`` unregister the job without
    stopping the child, leaving a daemon serving its port with the 0600 CSRF
    token live in memory. But a sweep-all uninstall tears down EVERY runtime's
    scheduler unit and EVERY identity's files, while the reclaim helper defaults
    to ``DAEMON_PORT`` -- the RESOLVED runtime's port only. So a sibling
    runtime's daemon (e.g. Codex on 24843 when cleanup runs from Claude) kept
    running its token-authed mutating API after a reported uninstall.

    Scoped (``this_install_only``): just the resolved runtime's port. Sweep-all:
    every runtime's port (claude 24842 + codex/hermes/copilot/cursor).
    ``script_name``
    in the reclaim helper is runtime-generic, so this only ever kills our own
    ``dashboard-server.py`` bound to one of these ports.
    """
    _measure_facade = _compat.facade()
    if this_install_only:
        return (_measure_facade.DAEMON_PORT,)
    ports = {24842}
    ports.update(_measure_facade._DAEMON_PORT_BY_RUNTIME.values())
    return tuple(sorted(ports))


def _reclaim_daemon_ports(this_install_only: bool):
    """Reclaim every port in scope; never crash the uninstall on cleanup."""
    _measure_facade = _compat.facade()
    for port in _measure_facade._daemon_ports_to_reclaim(this_install_only):
        try:
            _measure_facade._reclaim_posix_daemon_port(port=port)
        except Exception:  # noqa: BLE001 -- uninstall must never crash on cleanup
            pass


def _daemon_access_lines(effective_host):
    """v5.11.1: return the access-scope lines for install/dry-run output.

    Localhost binds print the usual "only your machine" line. A 0.0.0.0 bind is
    network mode -- the dashboard is reachable on the LAN, but token auth still
    gates every mutation, so we say so explicitly instead of claiming localhost
    only.
    """
    _measure_facade = _compat.facade()
    if effective_host == "0.0.0.0":
        return [
            "    - Network mode: reachable on your LAN (bound 0.0.0.0)",
            "    - Token auth still required for any change (toggle/disable)",
        ]
    return ["    - Only accessible from your machine (localhost)"]


def _print_daemon_network_note(effective_host):
    """Print a brief LAN note after a successful network-mode install."""
    _measure_facade = _compat.facade()
    if effective_host == "0.0.0.0":
        print("  Network mode: dashboard reachable on your LAN (bound 0.0.0.0).")
        print("  Token auth still required for any change.\n")


def _install_launchd_daemon(dry_run=None, soft_fail=None, effective_host=None):
    """macOS: install the dashboard daemon via a LaunchAgent.

    v5.4.19: added `soft_fail`. When True (hook paths),
    errors return False instead of sys.exit(1) so we never kill the calling
    Claude Code session. CLI calls keep the default False for backwards
    compatibility -- users still see a hard failure + actionable hint.

    Extracted verbatim from the pre-dispatcher setup_daemon. Re-running
    overwrites the plist idempotently and bootouts any existing instance
    first so we never fight a stale PID.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._scheduler_install_refused():
        return False

    def _fail(msg, hint=None):
        # Under soft_fail we are inside a hook --
        # stdout is session-visible context, so route errors to stderr instead
        # of spamming "[Error] ..." into every SessionStart transcript.
        _measure_facade = _compat.facade()
        out = _measure_facade.sys.stderr if soft_fail else _measure_facade.sys.stdout
        print(msg, file=out)
        if hint:
            print(hint, file=out)
        if soft_fail:
            return False
        _measure_facade.sys.exit(1)

    if dry_run:
        print("[Token Optimizer] Dry run. Would install:\n")
        print("  A tiny web server that makes your dashboard available at:")
        print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer\n")
        print("  What it does:")
        print("    - Serves your dashboard file so you can bookmark the URL")
        print("    - Starts automatically when you log into your Mac")
        print("    - Restarts itself if it ever stops")
        for line in _measure_facade._daemon_access_lines(effective_host):
            print(line)
        print("    - Uses ~2MB of memory\n")
        print("  Files it creates:")
        print(f"    {_measure_facade.SNAPSHOT_DIR / 'dashboard-server.py'}")
        print(f"    {_measure_facade.PLIST_PATH}\n")
        print("  No changes written.")
        return True

    if not _measure_facade._ensure_dashboard_file(to_stderr=soft_fail):
        return _fail("[Token Optimizer] Dashboard file missing; cannot start daemon.")

    # Clear any stale uninstall tombstone so a fresh install proceeds.
    try:
        if _measure_facade.DAEMON_THRASH_BREADCRUMB.exists():
            _measure_facade.DAEMON_THRASH_BREADCRUMB.unlink()
    except OSError:
        pass

    # Ensure per-install auth token exists before writing the daemon script.
    _measure_facade._get_or_create_daemon_token()

    with _measure_facade._daemon_install_lock(soft_fail=soft_fail) as acquired:
        if soft_fail and not acquired:
            # Another installer holds the lock; don't fight it from a hook path.
            return False

        # Write daemon script (catch OSError from permissions/quota issues).
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            _measure_facade.DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)
            daemon_script = _measure_facade.SNAPSHOT_DIR / "dashboard-server.py"
            script_changed = _measure_facade._write_file_if_changed(daemon_script, _measure_facade._generate_daemon_script(), 0o755)
            _measure_facade.LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            plist_changed = _measure_facade._write_file_if_changed(_measure_facade.PLIST_PATH, _measure_facade._generate_plist())
        except OSError as e:
            return _fail(f"[Error] Could not write daemon files: {e}")

        # Nothing changed and the daemon is already loaded and serving -> skip the
        # bootout+bootstrap. Reloading a healthy, current daemon only re-fires the
        # macOS "App Background Activity" banner for no benefit. (A reinstall after
        # uninstall has an unloaded agent, so this guard is skipped and the reload
        # below still runs.)
        if (not script_changed and not plist_changed
                and _measure_facade._launchagent_loaded(_measure_facade.DAEMON_LABEL)
                and _measure_facade._verify_daemon_port(timeout_seconds=1, retries=1, retry_sleep=0)):
            print("[Token Optimizer] Dashboard server already current and running.\n")
            print("  Bookmark this URL:")
            print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer")
            return True

        # Stop existing daemon if running (bootout is idempotent -- non-zero
        # exit when nothing is loaded is expected and ignored).
        try:
            _measure_facade.subprocess.run(["launchctl", "bootout", f"gui/{_measure_facade.os.getuid()}", str(_measure_facade.PLIST_PATH)],
                           capture_output=True, timeout=10, creationflags=_measure_facade._NO_WINDOW)
        except (OSError, _measure_facade.subprocess.TimeoutExpired):
            pass
        # bootout drops the launchd job but doesn't SIGTERM the Python
        # child -- the port stays held until the orphan dies on its own.
        # Reclaim it so bootstrap doesn't hit EADDRINUSE.
        _measure_facade._reclaim_posix_daemon_port()

        # Start daemon
        try:
            result = _measure_facade.subprocess.run(["launchctl", "bootstrap", f"gui/{_measure_facade.os.getuid()}", str(_measure_facade.PLIST_PATH)],
                                    capture_output=True, text=True, timeout=20, creationflags=_measure_facade._NO_WINDOW)
        except (OSError, _measure_facade.subprocess.TimeoutExpired) as e:
            return _fail(f"[Error] launchctl bootstrap failed: {e}")
        if result.returncode != 0:
            return _fail(
                f"[Error] Failed to start daemon: {result.stderr.strip()}",
                hint=f"  Plist written to: {_measure_facade.PLIST_PATH}\n  Try manually: launchctl bootstrap gui/{_measure_facade.os.getuid()} {_measure_facade.PLIST_PATH}",
            )

        # Verify it's actually running. Budget ~16s covers slow cold
        # starts. If we still can't reach the port, surface a clear
        # do-NOT-uninstall instruction so users don't kill a booting
        # daemon mid-init.
        _measure_facade.time.sleep(1)
        if _measure_facade._verify_daemon_port():
            print("[Token Optimizer] Dashboard server installed and running.\n")
            print("  Bookmark this URL:")
            print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer\n")
            _measure_facade._print_daemon_network_note(effective_host)
            print(f"  It updates automatically after every {_measure_facade.runtime_name_for_humans()} session.")
            print("  Starts on login, so the URL always works.\n")
            print(_measure_facade._hint("  To remove: python3 measure.py setup-daemon --uninstall"))
        else:
            print("[Token Optimizer] Server bootstrapped but port not yet reachable.")
            print(f"  Give it 30s, then open: http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer")
            print("  Do NOT run --uninstall while it is still coming up -- that can")
            print(f"  orphan the process and block port {_measure_facade.DAEMON_PORT} for the next install.")
            print(f"  If still unreachable after 30s, check: {_measure_facade.DAEMON_LOG_DIR}/stderr.log")
        return True


def _write_uninstall_tombstone(snapshot_dir=None):
    """v5.4.19: write a marker breadcrumb file so that if the daemon
    process somehow respawns (e.g., an orphaned LaunchAgent the user didn't clean
    up), it exits cleanly on next start instead of resurrecting.

    The breadcrumb content is the ``_UNINSTALL_TOMBSTONE_MARKER``
    (not an empty file). The daemon's 3-strikes thrash path writes an EMPTY
    breadcrumb that self-heals after 60s; an empty uninstall tombstone was
    indistinguishable from it, so a surviving daemon self-cleared it after 60s
    and minted a fresh CSRF token after a reported uninstall. The marker makes
    the uninstall tombstone permanent (cleared only by a reinstall).

    Surface failures to stderr so a silently
    broken tombstone (e.g. SNAPSHOT_DIR unwritable) is visible to the user
    rather than leaving them thinking uninstall succeeded while the daemon
    keeps respawning.

    The tombstone must OUTLIVE the uninstall. Each generated
    dashboard-server.py bakes in its OWN identity's breadcrumb path (see the
    `thrash_path_literal` in the daemon template) and checks it on start
    ("noop-tombstoned"), so a per-identity tombstone is what keeps that
    identity's orphaned daemon dead. `snapshot_dir` targets one identity; the
    uninstall sweep calls this once per swept identity so no sibling is left
    revivable. A legitimate reinstall clears it (setup_daemon unlinks the
    tombstone before starting), which is the ONLY place it may be removed.
    """
    _measure_facade = _compat.facade()
    snap = _measure_facade.SNAPSHOT_DIR if snapshot_dir is None else _measure_facade.Path(snapshot_dir)
    breadcrumb = snap / ".daemon-thrash"
    try:
        snap.mkdir(parents=True, exist_ok=True)
        # Write a distinctive marker (NOT an empty file) so a
        # surviving daemon script can tell an uninstall tombstone (permanent
        # until reinstall) from a transient 3-strikes thrash tombstone (which
        # self-heals after 60s). See _UNINSTALL_TOMBSTONE_MARKER.
        with open(breadcrumb, "w", encoding="utf-8") as f:
            f.write(_measure_facade._UNINSTALL_TOMBSTONE_MARKER)
    except OSError as e:
        _measure_facade.sys.stderr.write(
            f"[Token Optimizer] Warning: could not write uninstall tombstone "
            f"at {breadcrumb}: {e}\n"
            f"  If the daemon is still running after uninstall, remove the "
            f"LaunchAgent/task/unit manually.\n"
        )


def _daemon_install_failed_marker_state():
    """Tri-state stat of the sticky marker: 'present' | 'absent' | 'unknown'.

    The old implementation used ``os.path.exists``, which
    maps EVERY OSError to False -- so the written ``except OSError`` policy was
    dead code, and (worse) an AV/permission denial made a PRESENT marker read
    as ABSENT, silently reopening the retry loop the marker exists to stop --
    intermittently, which is the worst kind of flap to debug. ``os.stat`` with
    explicit errno handling makes present-but-unreadable ('unknown')
    distinguishable from a definitive ENOENT ('absent'). Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        _measure_facade.os.stat(str(_measure_facade.DAEMON_INSTALL_FAILED_BREADCRUMB))
        return "present"
    except (FileNotFoundError, NotADirectoryError):
        # The only DEFINITIVE evidence of absence: the path (or a parent
        # component) does not exist.
        return "absent"
    except OSError:
        # EACCES/EIO/...: the marker may well exist; we just cannot see it.
        return "unknown"


def _daemon_install_failed_marker_present():
    """True when a prior daemon install/revive/self-heal failed and has NOT been
    cleared (by an explicit successful ``setup-daemon``, or by a verified-live
    daemon disproving the record).

    Checked FIRST by every path that would otherwise (re)spawn an installer:
    ``_ensure_dashboard_daemon`` (SessionStart self-heal AND the ``daemon-revive``
    subcommand, including ``force=True``) and ``_daemon_midsession_pulse``
    (per-turn revive). A filesystem breadcrumb, not a config flag, for the same
    reason the uninstall tombstone is: a corrupt/unreadable config.json must not
    silently re-arm the retry loop.

    Policy (now explicit instead of an accident of exists()):
    'unknown' counts as PRESENT. This gate only ever suppresses a costly
    retry, so failing CLOSED on a transient stat error keeps the gate stable
    -- a momentary AV denial must not flap it open and resume the per-prompt
    retry loop. Only a definitive ENOENT reads as absent, and the
    clear-on-verified-healthy path in ``_ensure_dashboard_daemon`` still
    un-wedges a stale marker the moment the daemon is observably alive.
    """
    _measure_facade = _compat.facade()
    return _measure_facade._daemon_install_failed_marker_state() != "absent"


def _daemon_install_failed_reason():
    """First line of the sticky marker (``<iso8601> <reason>``), or None.

    Purely for user-facing surfaces (ensure-health's one-liner, doctor) --
    presence, not content, is what gates behaviour. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        line = _measure_facade.DAEMON_INSTALL_FAILED_BREADCRUMB.read_text(
            encoding="utf-8", errors="replace").strip().splitlines()
        return line[0] if line else None
    except OSError:
        return None


def _write_daemon_install_failed_marker(reason):
    """Persist the sticky install-failed marker. Best effort, never raises.

    Callers must only arm this for DEFINITIVE,
    permanent failure classes (MS-Store alias, schtasks missing, task creation
    denied by policy) -- see the DAEMON_INSTALL_FAILED_BREADCRUMB comment.
    Transient failures rely on the existing throttles instead.

    Content is ``<iso8601> <reason>`` for the user/support (surfaced by
    ensure-health and doctor): only the file's PRESENCE is load-bearing.
    """
    _measure_facade = _compat.facade()
    try:
        if _measure_facade._daemon_install_failed_marker_present():
            return
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            # Match take_snapshot's 0o700 -- when this mkdir is the
            # first creator, the state dir must not land world-readable.
            _measure_facade.os.chmod(str(_measure_facade.SNAPSHOT_DIR), 0o700)
        except OSError:
            pass
        stamp = _measure_facade.datetime.now(_measure_facade.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(_measure_facade.DAEMON_INSTALL_FAILED_BREADCRUMB, "w", encoding="utf-8") as f:
            f.write(f"{stamp} {reason}\n")
    except OSError as _e:
        # An unwritable state dir means we cannot make the refusal sticky. The
        # throttles still bound the retry rate; do not crash a hook over it --
        # but SAY so: a silently-degraded stickiness would leave the
        # per-window retry (and its Windows flashes) with zero explanation.
        try:
            print(
                "[Token Optimizer] Warning: could not persist the daemon "
                f"install-failed marker ({_measure_facade.DAEMON_INSTALL_FAILED_BREADCRUMB}): "
                f"{_e}. Failed installs will keep retrying on the normal "
                "throttle instead of stopping permanently.",
                file=_measure_facade.sys.stderr,
            )
        except Exception:
            pass


def _clear_daemon_install_failed_marker():
    """Remove the sticky install-failed marker.

    Legitimate callers, and NOTHING else (a time- or session-based clear would
    restore the per-prompt retry loop the marker exists to stop):
      * ``setup_daemon``'s explicit install branch, after the platform
        installer reports success -- the user proved the problem is fixed;
      * ``_ensure_dashboard_daemon`` on VERIFIED success -- a live,
        identity-checked daemon (or a completed install/restart) disproves the
        "structurally broken" record, e.g. after the install-lock race where
        the losing session armed the marker while the winner installed fine.

    Returns True only when the marker is VERIFIABLY gone
    (read back after the unlink, never assumed). A clear that did not clear
    warns on stderr -- the pre-fix shape swallowed the OSError, so
    ``setup-daemon`` printed its full "installed and running" success story
    while every self-heal path stayed permanently disabled, undiagnosably.
    Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        _measure_facade.DAEMON_INSTALL_FAILED_BREADCRUMB.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    # Read back: only a definitive ENOENT counts as cleared. 'present' AND
    # 'unknown' both mean the wedge may still stand -- say so.
    if _measure_facade._daemon_install_failed_marker_state() == "absent":
        return True
    try:
        print(
            "[Token Optimizer] Warning: could not remove "
            f"{_measure_facade.DAEMON_INSTALL_FAILED_BREADCRUMB} -- daemon self-heal stays "
            "DISABLED until this file is removed (delete it manually, then "
            f"re-run: {_measure_facade._measure_cli()} setup-daemon).",
            file=_measure_facade.sys.stderr,
        )
    except Exception:
        pass
    return False


def _uninstall_launchd_daemon(this_install_only=None, dry_run=None):
    """macOS: stop and remove the LaunchAgent + daemon script(s).

    Unified output: track what is actually
    deleted so we don't print a contradictory "Nothing to remove" header
    followed by a "Deleted: script.py" line when the plist is gone but
    the script file remains from a half-uninstall.

    v5.11.68: identity-sweeping by default. The LaunchAgent plist
    is per-RUNTIME (shared across ``token-optimizer-*`` identities of the same
    runtime), so it is removed once. The per-identity files
    (``dashboard-server.py``, ``daemon-token``, ``dashboard-host``,
    ``.daemon-thrash``) live in EACH identity's snapshot dir; a sibling
    identity's daemon script + 0600 CSRF token would otherwise outlive the
    uninstall. Sweep every identity unless ``this_install_only`` opts out.
    Also bootout every runtime's LaunchAgent by name so a registration whose
    data dir already vanished still gets unregistered.

    v5.11.68 (cleanup): ``dry_run=True`` makes the uninstaller
    side-effect-free (no bootout, no file deletion, no tombstone write) so the
    cleanup command's ``--dry-run`` is a true preview.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return False

    if dry_run:
        # Dry-run: report what WOULD be removed, touch nothing.
        would_remove = []
        for label in _measure_facade._scheduler_names_to_sweep(
                this_install_only, _measure_facade._ALL_LAUNCH_AGENT_LABELS, _measure_facade.DAEMON_LABEL):
            plist = _measure_facade.LAUNCH_AGENTS_DIR / f"{label}.plist"
            if plist.exists():
                would_remove.append(str(plist))
        per_identity: list[tuple[Path, list[str]]] = []
        for snap_dir in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
            files = _measure_facade._daemon_per_identity_files(snap_dir)
            identity_would = [str(files[k]) for k in ("daemon_script", "daemon_token", "daemon_host") if files[k].exists()]
            if identity_would:
                per_identity.append((snap_dir, identity_would))
                would_remove.extend(identity_would)
        if would_remove:
            print("[Token Optimizer] Dashboard daemon would be removed.")
            for path in would_remove:
                print(f"  Would delete: {path}")
            if per_identity and not this_install_only:
                print("  Would sweep all token-optimizer-* identities:")
                for snap_dir, items in per_identity:
                    print(f"    {snap_dir}: {len(items)} file(s)")
        else:
            print("[Token Optimizer] No daemon artifacts found. Nothing to remove.")
        return
    # Tombstone FIRST so any racing respawn exits cleanly.
    _measure_facade._write_uninstall_tombstone()
    removed = []
    # Sweep the OS scheduler by name across every runtime variant. The plist
    # file lives at a fixed per-runtime path under ~/Library/LaunchAgents.
    for label in _measure_facade._scheduler_names_to_sweep(
            this_install_only, _measure_facade._ALL_LAUNCH_AGENT_LABELS, _measure_facade.DAEMON_LABEL):
        plist = _measure_facade.LAUNCH_AGENTS_DIR / f"{label}.plist"
        # Bootout by LABEL, unconditionally. The old code only
        # booted out when the plist FILE existed, so a job still loaded in
        # launchd whose plist had already been deleted (half-uninstall, manual
        # rm, platform GC) was never unregistered and kept respawning.
        try:
            _measure_facade.subprocess.run(["launchctl", "bootout", f"gui/{_measure_facade.os.getuid()}/{label}"],
                           capture_output=True, timeout=10, creationflags=_measure_facade._NO_WINDOW)
        except (OSError, _measure_facade.subprocess.TimeoutExpired):
            pass
        if plist.exists():
            try:
                _measure_facade.subprocess.run(["launchctl", "bootout", f"gui/{_measure_facade.os.getuid()}", str(plist)],
                               capture_output=True, timeout=10, creationflags=_measure_facade._NO_WINDOW)
            except (OSError, _measure_facade.subprocess.TimeoutExpired):
                pass
            if _measure_facade._unlink_if_exists(plist):
                removed.append(str(plist))
    # Unregistering the job does NOT stop the process --
    # `launchctl bootout` / `systemctl disable` drop the registration without
    # SIGTERMing the running child, so the daemon kept serving its port with
    # the 0600 CSRF token live in memory until logout. Reclaim the port(s) now
    # that the tombstone is down (so nothing races a respawn) and the job is
    # unregistered. On a sweep-all uninstall this covers EVERY runtime's port
    # (a sibling runtime's daemon + live CSRF token otherwise
    # survived), scoped back to the resolved runtime under --this-install-only.
    # Never kills a foreign process on a port -- the helper verifies the command
    # line is our own dashboard-server.py.
    _measure_facade._reclaim_daemon_ports(this_install_only)
    # Sweep per-identity daemon files across every token-optimizer-* identity.
    per_identity_removed: list[tuple[Path, list[str]]] = []
    sweep_failed: list[str] = []
    for snap_dir in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
        identity_removed, identity_failed = _measure_facade._sweep_identity_daemon_files(
            snap_dir, ("daemon_script", "daemon_token", "daemon_host"))
        sweep_failed.extend(identity_failed)
        # The .daemon-thrash tombstone must PERSIST for every
        # swept identity. Each identity's generated dashboard-server.py checks
        # its OWN breadcrumb path on start and exits "noop-tombstoned"; the
        # previous unlink here re-armed self-revive, so an orphaned LaunchAgent
        # /task/unit could resurrect the daemon and mint a fresh 0600 token
        # after a "successful" uninstall. Only a legitimate reinstall clears it
        # (setup_daemon unlinks before start). NOT counted toward `removed` so
        # the "Nothing to remove" honesty invariant holds.
        _measure_facade._write_uninstall_tombstone(snap_dir)
        if identity_removed:
            per_identity_removed.append((snap_dir, identity_removed))
            removed.extend(identity_removed)
    _measure_facade._print_identity_sweep_report(
        removed, per_identity_removed, sweep_failed, this_install_only)


def _is_ms_store_python_alias(exe_path):
    """True if sys.executable looks like a Microsoft Store App Execution Alias.

    App Execution Aliases (path contains WindowsApps) only launch from
    interactive user shell tokens. Task Scheduler's launch context
    cannot resolve them, so pythonw would exit silently and leave the
    user stuck in the 'wait 30s' message forever.
    """
    _measure_facade = _compat.facade()
    if not exe_path:
        return False
    normalized = str(exe_path).replace("/", "\\").lower()
    return "\\windowsapps\\" in normalized


def _resolve_windows_pythonw():
    """Best-effort path to pythonw.exe on Windows. Returns None if only
    an MS Store alias is available -- the caller must refuse install.
    """
    _measure_facade = _compat.facade()
    try:
        exe = _measure_facade.Path(_measure_facade.sys.executable)
    except (TypeError, ValueError):
        return None
    if _measure_facade._is_ms_store_python_alias(exe):
        return None
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists() and not _measure_facade._is_ms_store_python_alias(candidate):
        return str(candidate)
    return None


def _windows_gui_python():
    """pythonw.exe (GUI subsystem) next to ``sys.executable``, or None.

    The ONE interpreter that never allocates a console. Every
    console-subsystem child (python.exe, py.exe, cmd.exe) gets a window from
    Windows itself unless the PARENT passes CREATE_NO_WINDOW -- and when the
    parent is Task Scheduler, we do not get to pass anything. Launching
    pythonw.exe moves the guarantee into the binary, where no parent can undo it.

    Thin alias over ``_resolve_windows_pythonw`` (which already refuses MS Store
    App Execution Aliases -- those do not resolve under Task Scheduler's launch
    context) so the no-flash call sites read by intent rather than by platform trivia.
    Returns None off Windows and whenever no usable twin exists; every caller
    must fall back to its pre-fix behaviour.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.os.name != "nt":
        return None
    try:
        return _measure_facade._resolve_windows_pythonw()
    except Exception:
        # Interpreter SELECTION must never be able to break a spawn. The
        # resolver builds a WindowsPath, which raises on a host whose pathlib
        # flavour disagrees with os.name (real case: a POSIX box with os.name
        # monkeypatched; plausible case: an embedded/frozen interpreter with a
        # non-path sys.executable). Degrade to "no twin" -> caller keeps
        # sys.executable, which is exactly the pre-fix behaviour.
        return None


def _windows_task_exec_action(daemon_script_path, launcher_path):
    """Return ``(command, arguments)`` for the Scheduled Task's ``<Exec>``.

    The idle-pop: the task action used to be the generated
    ``dashboard-launcher.cmd``. Task Scheduler runs a .cmd through cmd.exe, a
    CONSOLE-subsystem process, so EVERY fire of the task painted a console
    window on the user's desktop -- at logon, at boot, and on each
    ``schtasks /Run`` from the daemon revive / restart self-heal (which is what
    made windows pop while the machine looked idle). ``<Hidden>true</Hidden>``
    does NOT suppress this; it only hides the task from the Task Scheduler UI's
    default filter. CREATE_NO_WINDOW cannot help either -- our process is not
    the parent, Task Scheduler is.

    Preferred action: ``pythonw.exe <daemon_script>``. pythonw is GUI-subsystem,
    so Windows never allocates a console for it no matter who launches it.

    Fallback (no usable pythonw -- MS Store alias, or no twin next to
    sys.executable): the old .cmd launcher, which still WORKS and still captures
    stdout/stderr, it just keeps the console window. Better a visible window than
    a daemon that will not start.

    The .cmd is written either way: it stays the fallback, it is what the
    uninstall sweep already knows to delete (``_daemon_per_identity_files``), and
    it is a usable manual repro for a user debugging a dead daemon.
    """
    _measure_facade = _compat.facade()
    pyw = _measure_facade._windows_gui_python()
    if pyw:
        return pyw, f'"{daemon_script_path}"'
    return str(launcher_path), ""


def _compose_windows_user_id():
    """Return DOMAIN\\user when domain-joined, bare username in workgroup.

    Bare %USERNAME% in <LogonTrigger> fails to match
    Windows' logon event on domain-joined machines because the event
    carries the fully-qualified account name.
    """
    _measure_facade = _compat.facade()
    username = _measure_facade.os.environ.get("USERNAME", "").strip()
    if not username:
        # Last-ditch fallback: parse the final path element of USERPROFILE.
        username = _measure_facade.os.environ.get("USERPROFILE", "").split("\\")[-1].strip()
    if not username:
        return None
    domain = _measure_facade.os.environ.get("USERDOMAIN", "").strip()
    computer = _measure_facade.os.environ.get("COMPUTERNAME", "").strip()
    # Workgroup machines report USERDOMAIN == COMPUTERNAME -- strip it
    # so the UserId stays bare and matches the local logon event.
    if domain and domain.upper() != computer.upper():
        return f"{domain}\\{username}"
    return username


def _generate_windows_launcher_cmd(daemon_script_path, log_dir):
    """Generate a .cmd shim that resolves Python at runtime and redirects
    daemon stdout/stderr so future failures leave a trail.

    Rationale: pointing Task
    Scheduler directly at a versioned pythonw.exe path breaks on Python
    upgrades and loses stdout/stderr. A .cmd wrapper dispatches via
    a runtime interpreter ladder and captures daemon output into
    DAEMON_LOG_DIR so port conflicts and import errors no longer
    silently vanish.

    READ THIS BEFORE REORDERING ANYTHING:

    * This shim is now the FALLBACK path only. The Scheduled Task action
      normally points straight at pythonw.exe (see
      ``_windows_task_exec_action``), because Task Scheduler hosts a .cmd
      inside console-subsystem cmd.exe and paints a window on EVERY fire --
      logon, boot, and each ``schtasks /Run`` from the revive/restart
      self-heal. Nothing inside a .cmd can prevent its own cmd.exe host.
      **HONEST LIMIT: when this shim is the task action, the no-flash bug is NOT fixed,
      only REDUCED -- from several console processes per fire (the cmd.exe
      host plus up to three where.exe probes plus a console-subsystem
      interpreter) down to the one unavoidable cmd.exe host window. One flash
      per fire remains for this population, and no rewrite of the shim's
      CONTENTS can ever remove it.** That is why the shim is used only when no
      usable pythonw twin exists.
    * The interpreter ladder was ``py.exe -3`` FIRST, which is exactly
      backwards: py.exe is console-subsystem and wins on any host with the
      Python Launcher installed (the common case), so the LONG-LIVED daemon
      itself ran as a console process. GUI-subsystem interpreters now come
      first: pythonw.exe -> pyw.exe -3 -> py.exe -3 -> python.exe.
    * The three ``where <exe>`` probes are gone. where.exe is itself a console
      binary, so the old shim spawned up to three extra console processes per
      fire. Detection is now cmd's own "command not found" code (9009) from a
      direct invocation, which costs no extra process.

    The ladder is written FLAT (no parenthesised blocks) on purpose:
    ``%ERRORLEVEL%`` inside a ``( ... )`` block expands once at block-parse
    time, so a block form would test a stale value.

    # The bare-PATH ``pythonw.exe`` rung needs a
    WindowsApps guard: a Microsoft Store App Execution Alias exits 0 SILENTLY
    under Task Scheduler's non-interactive token, so an unguarded first rung
    would "succeed", ``exit /b 0``, and no daemon would ever start -- on hosts
    where the pre-fix ``py -3``-first ladder worked. The rung therefore
    resolves the PATH hit with ``%%~$PATH:I`` (pure cmd, no extra process --
    where.exe is banned here) and skips itself when the hit lives under
    WindowsApps. pyw.exe/py.exe are real launchers in C:\\Windows, never Store
    aliases, so they keep plain rungs.

    ``%`` is legal in Windows paths but expands at cmd
    parse time (``%VAR%``), silently mis-targeting DAEMON_SCRIPT and both log
    redirects; ``%%`` is cmd's literal-percent escape inside a batch file, so
    interpolated paths are escaped below.
    """
    _measure_facade = _compat.facade()
    def _rung(invocation):
        _measure_facade = _compat.facade()
        return (
            f'{invocation} "%DAEMON_SCRIPT%" 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"\r\n'
            'if not "%ERRORLEVEL%"=="9009" exit /b %ERRORLEVEL%\r\n'
        )

    daemon_script_path = str(daemon_script_path).replace("%", "%%")
    log_dir = str(log_dir).replace("%", "%%")

    return (
        "@echo off\r\n"
        # cmd.exe reads a batch file in the console (OEM) code page, but this
        # file is written as UTF-8: a profile path like C:\Users\Jos\u00e9 was
        # garbled and the daemon never started. Switch to UTF-8 first; this
        # line and everything above it is plain ASCII.
        "chcp 65001\r\n"
        "REM Token Optimizer dashboard daemon launcher (v5.3.2+).\r\n"
        "REM Auto-generated. Resolves Python at runtime so interpreter upgrades\r\n"
        "REM do not break the scheduled task.\r\n"
        "REM GUI-subsystem interpreters first (pythonw/pyw) so the daemon\r\n"
        "REM itself never owns a console. 9009 = cmd's 'command not found'.\r\n"
        "REM The pythonw.exe rung skips Microsoft Store aliases (WindowsApps):\r\n"
        "REM they exit 0 silently under Task Scheduler and would end the ladder\r\n"
        "REM with no daemon started.\r\n"
        "setlocal\r\n"
        f'set "DAEMON_SCRIPT={daemon_script_path}"\r\n'
        f'set "STDOUT_LOG={log_dir}\\stdout.log"\r\n'
        f'set "STDERR_LOG={log_dir}\\stderr.log"\r\n'
        'if not exist "%DAEMON_SCRIPT%" (\r\n'
        '  echo [%DATE% %TIME%] daemon script missing: "%DAEMON_SCRIPT%" >> "%STDERR_LOG%"\r\n'
        "  exit /b 1\r\n"
        ")\r\n"
        'set "PYW_EXE="\r\n'
        'for %%I in (pythonw.exe) do set "PYW_EXE=%%~$PATH:I"\r\n'
        'if defined PYW_EXE if /i not "%PYW_EXE:windowsapps=%"=="%PYW_EXE%" set "PYW_EXE="\r\n'
        'if defined PYW_EXE "%PYW_EXE%" "%DAEMON_SCRIPT%" 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"\r\n'
        'if defined PYW_EXE if not "%ERRORLEVEL%"=="9009" exit /b %ERRORLEVEL%\r\n'
        + _rung("pyw.exe -3")
        + _rung("py.exe -3")
        + _rung("python.exe")
        + 'echo [%DATE% %TIME%] No Python found on PATH -- install python.org or winget install Python.Python.3 >> "%STDERR_LOG%"\r\n'
        "exit /b 2\r\n"
    )


def _probe_windows_port_owner(port):
    """Return a human-readable string identifying the PID bound to port,
    or None if the port is free or we can't tell.

    A foreign service on 24842 would leave the user in
    an indefinite 'wait 30s' state. Pre-install probe surfaces the
    offending PID so the user can decide whether to reclaim the port.
    """
    _measure_facade = _compat.facade()
    try:
        result = _measure_facade.subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    needle = f":{port}"
    for line in result.stdout.splitlines():
        if needle in line and "LISTENING" in line.upper():
            parts = line.split()
            pid = parts[-1] if parts else "?"
            return f"pid={pid} (netstat: {line.strip()})"
    return None


def _generate_schtasks_xml(task_name, user_id, command, arguments=None):
    """Build a Task Scheduler XML payload for the dashboard daemon.

    Uses the documented Task Scheduler 1.2 schema (MS-LEARN:
    task-scheduler-schema-reference). Key choices:
      - LogonTrigger so the daemon starts whenever the user logs in
        (equivalent to launchd's RunAtLoad + KeepAlive on Mac).
      - Hidden=true keeps it out of the default Task Scheduler UI
        filter; advanced users can still show hidden tasks.
      - StartWhenAvailable=true so a missed logon trigger (e.g., after
        a reboot at a login prompt timeout) still fires.
      - ExecutionTimeLimit=PT0S = no time limit (daemon runs forever).
      - DisallowStartIfOnBatteries=false so laptop users get their
        bookmarkable URL on battery power too.

    ``command``/``arguments`` come from
    ``_windows_task_exec_action`` and are normally pythonw.exe + the daemon
    script, NOT the .cmd launcher. See that helper for why <Hidden> was never
    enough. ``<Arguments>`` is emitted only when non-empty so the .cmd fallback
    still produces the exact pre-fix XML shape.
    """
    _measure_facade = _compat.facade()
    from xml.sax.saxutils import escape as _xml_escape

    # Escape &, <, > plus ' and " so domain\user strings like
    # "O'Brien & Co\\bob" survive the XML round-trip cleanly.
    def xml_escape(s):
        _measure_facade = _compat.facade()
        return _xml_escape(s, {"'": "&apos;", '"': "&quot;"})

    _ = task_name  # kept for API symmetry; no longer embedded via <URI>
    # No <URI> element: it is optional per the Task Scheduler 1.2 schema
    # and creates a mismatch class when enterprise GPO relocates tasks
    # into subfolders. /TN in the schtasks /Create call is sufficient.
    # A BootTrigger requires elevated registration even with LeastPrivilege.
    # This is a per-user service: logon is the correct boundary, including
    # logins following Fast Startup. Keep installation non-elevated.
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>Serves the Token Optimizer dashboard on http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{xml_escape(user_id)}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{xml_escape(user_id)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{xml_escape(str(command))}</Command>\n"
        + (f"      <Arguments>{xml_escape(str(arguments))}</Arguments>\n"
           if arguments else "")
        + "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _install_task_scheduler_daemon(dry_run=None, soft_fail=None, effective_host=None):
    """Windows: register a per-user Scheduled Task that runs the dashboard
    daemon at logon, survives reboot, and stays out of the foreground.

    Idempotent: schtasks /Create /F overwrites an existing task. The
    bootout-then-bootstrap pattern from macOS maps to /End then /Create:
    we stop any running instance before re-registering so we never
    fight a stale process on port 24842.

    soft_fail=True (hook paths, e.g. ensure-health self-heal): every error
    path returns False instead of sys.exit(1) so SystemExit can never escape
    and kill the calling Claude Code session. Returns True on success.

    Not tested on a real Windows box from the development Mac. First
    Windows user to run this is the de facto smoke test. Full rollback
    is one command: `measure.py setup-daemon --uninstall`.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._scheduler_install_refused():
        return False

    def _fail(msg, *extra, permanent_reason=None):
        # Only DEFINITIVE, permanent failure classes
        # arm the sticky no-retry marker (MS-Store alias, schtasks missing,
        # policy-denied task creation). Transient failures stay retryable and
        # are bounded by the existing ensure/revive/heal throttles.
        _measure_facade = _compat.facade()
        if permanent_reason:
            _measure_facade._write_daemon_install_failed_marker(permanent_reason)
        # Under soft_fail we are inside a hook --
        # stdout is session-visible context, so route errors to stderr instead
        # of spamming "[Error] ..." into every SessionStart transcript.
        out = _measure_facade.sys.stderr if soft_fail else _measure_facade.sys.stdout
        print(msg, file=out)
        for line in extra:
            print(line, file=out)
        if soft_fail:
            return False
        _measure_facade.sys.exit(1)

    if dry_run:
        print("[Token Optimizer] Dry run (Windows). Would install:\n")
        print("  A tiny web server that makes your dashboard available at:")
        print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer\n")
        print("  What it does:")
        print("    - Registers a per-user Scheduled Task (no UAC / admin rights needed)")
        print("    - Starts automatically when you log into Windows")
        print("    - Launched hidden by the Scheduled Task (via 'py -3', a stable")
        print("      interpreter path across upgrades; falls back to pythonw.exe)")
        for line in _measure_facade._daemon_access_lines(effective_host):
            print(line)
        print("    - Uses ~2MB of memory\n")
        print("  Files it creates:")
        print(f"    {_measure_facade.SNAPSHOT_DIR / 'dashboard-server.py'}")
        print(f"    Scheduled Task: {_measure_facade.WINDOWS_TASK_NAME}\n")
        print("  No changes written.")
        return

    if not _measure_facade._ensure_dashboard_file(to_stderr=soft_fail):
        return _fail("[Error] Dashboard file missing; cannot start daemon.")

    # Microsoft Store Python's App Execution Alias cannot be
    # launched from Task Scheduler's service context. Refuse install
    # with a clear remediation path rather than registering a task
    # that will silently fail every logon.
    if _measure_facade._is_ms_store_python_alias(_measure_facade.sys.executable):
        return _fail(
            "[Error] Microsoft Store Python detected (App Execution Alias).",
            "  Task Scheduler cannot launch Store aliases as background services,",
            "  so the daemon would never start. Install a real Python build instead:",
            "    winget install Python.Python.3.12",
            "  or download from python.org (make sure 'Add to PATH' is checked).",
            "  Then re-run: python -m measure setup-daemon",
            # Structurally impossible until the user installs a real Python --
            # exactly the class the sticky marker exists for.
            permanent_reason="ms-store-python-alias",
        )

    # Compose DOMAIN\user when domain-joined so LogonTrigger
    # actually fires on corporate machines.
    user_id = _measure_facade._compose_windows_user_id()
    if not user_id:
        return _fail(
            "[Error] Could not determine Windows username from %USERNAME%.",
            "  Set the USERNAME environment variable and retry.",
        )

    # Surface the port owner before we try to install so the
    # user isn't stuck in an indefinite 'wait 30s' loop if 24842 is
    # already bound by something else.
    owner = _measure_facade._probe_windows_port_owner(_measure_facade.DAEMON_PORT)
    if owner:
        return _fail(
            f"[Error] Port {_measure_facade.DAEMON_PORT} is already bound: {owner}",
            "  Release the port (or run --uninstall if it is our own stale",
            "  pythonw), then re-run setup. Reclaim: taskkill /PID <pid> /F",
        )

    _measure_facade._get_or_create_daemon_token()

    with _measure_facade._daemon_install_lock(soft_fail=soft_fail) as acquired:
        if soft_fail and not acquired:
            # Another installer holds the lock; don't fight it from a hook path.
            return False
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _measure_facade.DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        daemon_script = _measure_facade.SNAPSHOT_DIR / "dashboard-server.py"
        daemon_script.write_text(_measure_facade._generate_daemon_script(), encoding="utf-8")

        # .cmd launcher resolves Python at runtime (py -> pythonw ->
        # python) and redirects daemon stdout/stderr into logs so future
        # failures leave a trail instead of vanishing.
        # write_BYTES, not write_text: the shim is explicitly CRLF, and
        # text-mode writing translates "\n" to os.linesep, which on Windows
        # turns every "\r\n" into "\r\r\n". Bytes also makes the file
        # byte-comparable, which is what lets _heal_windows_launcher_shim be
        # idempotent instead of rewriting it on every ensure-health.
        launcher_path = _measure_facade.SNAPSHOT_DIR / _measure_facade.WINDOWS_LAUNCHER_NAME
        launcher_path.write_bytes(
            _measure_facade._generate_windows_launcher_cmd(
                str(daemon_script), str(_measure_facade.DAEMON_LOG_DIR)).encode("utf-8"),
        )

        # Prefer a GUI-subsystem pythonw.exe action over the .cmd
        # launcher so Task Scheduler cannot paint a console window on every
        # fire (logon, boot, and every `schtasks /Run` from the revive path).
        _task_command, _task_arguments = _measure_facade._windows_task_exec_action(
            str(daemon_script), str(launcher_path))
        if not _task_arguments:
            print("[Token Optimizer] Note: pythonw.exe not found next to this "
                  "interpreter; falling back to the .cmd launcher, which shows "
                  "a console window when the task runs.")
        xml_payload = _measure_facade._generate_schtasks_xml(
            task_name=_measure_facade.WINDOWS_TASK_NAME,
            user_id=user_id,
            command=_task_command,
            arguments=_task_arguments,
        )
        # UTF-16 LE with BOM: matches schtasks /Query native output and
        # pins the byte order so future Python encoding defaults can't
        # drift into BE and break schtasks XML parsing.
        xml_path = _measure_facade.SNAPSHOT_DIR / ".schtasks-daemon.xml"
        xml_path.write_bytes(b"\xff\xfe" + xml_payload.encode("utf-16-le"))

        # Stop any prior instance -- safe to fail if task doesn't exist.
        # FileNotFoundError here means schtasks.exe itself is missing from the
        # machine: a STRUCTURAL condition (no scheduler to register with), the
        # one exception class worth the sticky marker.
        try:
            _measure_facade.subprocess.run(
                ["schtasks", "/End", "/TN", _measure_facade.WINDOWS_TASK_NAME],
                capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
            )
            # Register (or overwrite) the task.
            create = _measure_facade.subprocess.run(
                ["schtasks", "/Create", "/XML", str(xml_path),
                 "/TN", _measure_facade.WINDOWS_TASK_NAME, "/F"],
                capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
            )
        except FileNotFoundError:
            return _fail(
                "[Error] schtasks.exe not found on this system.",
                "  The dashboard daemon needs the Windows Task Scheduler CLI.",
                permanent_reason="schtasks-missing",
            )
        if create.returncode != 0:
            _stderr_lower = (create.stderr or "").lower()
            # "Access is denied" / GPO-blocked task creation is a policy
            # decision, not a blip -- it will fail identically next prompt.
            # Everything else (transient service hiccup, odd one-off exit)
            # stays retryable under the throttles.
            _denied = ("access is denied" in _stderr_lower
                       or "denied" in _stderr_lower)
            return _fail(
                f"[Error] schtasks /Create failed: {create.stderr.strip()}",
                "  Common causes: locked-down enterprise policy blocking task",
                "  creation, or schtasks.exe missing. Try manually with:",
                f"    schtasks /Create /XML \"{xml_path}\" /TN {_measure_facade.WINDOWS_TASK_NAME} /F",
                permanent_reason=(
                    "schtasks-create-access-denied" if _denied else None),
            )
        # Fire the task immediately so the user's first URL click works.
        _measure_facade.subprocess.run(
            ["schtasks", "/Run", "/TN", _measure_facade.WINDOWS_TASK_NAME],
            capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
        _measure_facade.time.sleep(1)
        if _measure_facade._verify_daemon_port():
            print("[Token Optimizer] Dashboard server installed and running.\n")
            print("  Bookmark this URL:")
            print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer\n")
            _measure_facade._print_daemon_network_note(effective_host)
            print(f"  It updates automatically after every {_measure_facade.runtime_name_for_humans()} session.")
            print("  Starts at logon, so the URL always works.\n")
            print("  To remove: python -m measure setup-daemon --uninstall")
        else:
            print("[Token Optimizer] Task registered but port not yet reachable.")
            print(f"  Give it 30s, then open: http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer")
            print("  Do NOT run --uninstall while it is still coming up.")
            print(f"  If still unreachable, check daemon logs: {_measure_facade.DAEMON_LOG_DIR}\\stderr.log")
            print(f"  and task status: schtasks /Query /TN {_measure_facade.WINDOWS_TASK_NAME} /V /FO LIST")
        # Install succeeded (task registered); port may still be warming up.
        return True


def _uninstall_task_scheduler_daemon(this_install_only=None, dry_run=None):
    """Windows: stop and remove the dashboard daemon scheduled task.

    Cleans orphan XML files from any prior naming convention via glob so
    version drift doesn't leave artifacts behind.

    v5.11.68: identity-sweeping by default. The scheduled task is
    per-RUNTIME (shared across ``token-optimizer-*`` identities of the same
    runtime), so it is removed once, and we query+delete EVERY runtime's task
    name so a task whose data dir already vanished still gets unregistered.
    The per-identity files (``dashboard-server.py``, the .cmd launcher, the
    schtasks XML, ``daemon-token``, ``dashboard-host``, ``.daemon-thrash``)
    live in EACH identity's snapshot dir; a sibling's daemon script + 0600
    CSRF token would otherwise outlive the uninstall. Sweep every identity
    unless ``this_install_only`` opts out.

    v5.11.68 (cleanup): ``dry_run=True`` makes the uninstaller
    side-effect-free (no schtasks calls, no file deletion, no tombstone write)
    so the cleanup command's ``--dry-run`` is a true preview.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return False

    if dry_run:
        would_remove = []
        per_identity: list[tuple[Path, list[str]]] = []
        for snap_dir in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
            files = _measure_facade._daemon_per_identity_files(snap_dir)
            identity_would = [str(files[k]) for k in ("daemon_script", "windows_launcher", "daemon_token", "daemon_host") if files[k].exists()]
            try:
                for xml_path in snap_dir.glob("*schtasks-daemon*.xml"):
                    identity_would.append(str(xml_path))
            except OSError:
                pass
            if identity_would:
                per_identity.append((snap_dir, identity_would))
                would_remove.extend(identity_would)
        if would_remove:
            print("[Token Optimizer] Dashboard daemon would be removed.")
            for path in would_remove:
                print(f"  Would delete: {path}")
            if per_identity and not this_install_only:
                print("  Would sweep all token-optimizer-* identities:")
                for snap_dir, items in per_identity:
                    print(f"    {snap_dir}: {len(items)} file(s)")
        else:
            print("[Token Optimizer] No daemon artifacts found. Nothing to remove.")
        return
    # Tombstone FIRST so any racing respawn exits cleanly.
    _measure_facade._write_uninstall_tombstone()
    removed = []
    # Sweep the OS scheduler by name across every runtime variant so a task
    # whose data dir already vanished still gets unregistered.
    for task_name in _measure_facade._scheduler_names_to_sweep(
            this_install_only, _measure_facade._ALL_WINDOWS_TASK_NAMES, _measure_facade.WINDOWS_TASK_NAME):
        try:
            _measure_facade.subprocess.run(
                ["schtasks", "/End", "/TN", task_name],
                capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
        except (OSError, _measure_facade.subprocess.TimeoutExpired):
            pass
        try:
            delete = _measure_facade.subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
        except (OSError, _measure_facade.subprocess.TimeoutExpired):
            delete = _measure_facade.subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="timeout")
        if delete.returncode == 0:
            removed.append(f"Scheduled Task: {task_name}")
    # Sweep per-identity daemon files across every token-optimizer-* identity.
    per_identity_removed: list[tuple[Path, list[str]]] = []
    sweep_failed: list[str] = []
    for snap_dir in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
        identity_removed, identity_failed = _measure_facade._sweep_identity_daemon_files(
            snap_dir, ("daemon_script", "windows_launcher", "daemon_token", "daemon_host"))
        sweep_failed.extend(identity_failed)
        # The .daemon-thrash tombstone must PERSIST for every
        # swept identity. Each identity's generated dashboard-server.py checks
        # its OWN breadcrumb path on start and exits "noop-tombstoned"; the
        # previous unlink here re-armed self-revive, so an orphaned LaunchAgent
        # /task/unit could resurrect the daemon and mint a fresh 0600 token
        # after a "successful" uninstall. Only a legitimate reinstall clears it
        # (setup_daemon unlinks before start). NOT counted toward `removed` so
        # the "Nothing to remove" honesty invariant holds.
        _measure_facade._write_uninstall_tombstone(snap_dir)
        # Glob-clean any schtasks XML regardless of version-specific naming
        # (".schtasks-daemon.xml", "schtasks-daemon.xml", etc.).
        try:
            for xml_path in snap_dir.glob("*schtasks-daemon*.xml"):
                try:
                    xml_path.unlink()
                    identity_removed.append(str(xml_path))
                except OSError:
                    pass
        except OSError:
            pass
        if identity_removed:
            per_identity_removed.append((snap_dir, identity_removed))
            removed.extend(identity_removed)
    _measure_facade._print_identity_sweep_report(
        removed, per_identity_removed, sweep_failed, this_install_only)


def _generate_linux_launcher_sh(daemon_script_path, log_dir):
    """Generate a POSIX shell launcher that resolves python at runtime.

    Parallels the Windows .cmd launcher. ExecStart pointing directly at
    sys.executable would break when the user's python install moves
    (pipx venv removed, distro upgrade, brew switch), leaving a
    stranded unit that journald spams forever. The launcher re-resolves
    every invocation (python3 -> python), so Python changes no longer
    invalidate the installed unit.
    """
    _measure_facade = _compat.facade()
    return (
        "#!/bin/sh\n"
        "# Token Optimizer dashboard daemon launcher (v5.3.4+).\n"
        "# Auto-generated by measure.py. Resolves Python at runtime so\n"
        "# interpreter moves/upgrades don't invalidate the systemd unit.\n"
        "set -u\n"
        f'DAEMON_SCRIPT="{daemon_script_path}"\n'
        f'STDOUT_LOG="{log_dir}/stdout.log"\n'
        f'STDERR_LOG="{log_dir}/stderr.log"\n'
        'if [ ! -f "$DAEMON_SCRIPT" ]; then\n'
        '  echo "daemon script missing: $DAEMON_SCRIPT" >>"$STDERR_LOG" 2>&1 || true\n'
        "  exit 1\n"
        "fi\n"
        "for PY in python3 python; do\n"
        '  if command -v "$PY" >/dev/null 2>&1; then\n'
        '    exec "$PY" "$DAEMON_SCRIPT"\n'
        "  fi\n"
        "done\n"
        'echo "no python interpreter found on PATH" >>"$STDERR_LOG" 2>&1 || true\n'
        "exit 2\n"
    )


def _probe_systemd_user_bus():
    """Return True if `systemctl --user` can actually reach the user bus.

    `systemctl --user --version` succeeds even when the user-bus is
    dead (SSH session without linger, cron context, WSL2 w/o systemd).
    A real reachability check runs `list-units` which requires the
    dbus-session. Distinguishes "systemd present but bus dead" from
    "systemd absent" so the installer can emit a targeted error rather
    than planting a unit file that will never start.
    """
    _measure_facade = _compat.facade()
    try:
        result = _measure_facade.subprocess.run(
            ["systemctl", "--user", "list-units", "--no-legend"],
            capture_output=True, text=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
        )
    except (_measure_facade.subprocess.SubprocessError, OSError, FileNotFoundError):
        return False
    return result.returncode == 0


def _systemd_user_unit_path():
    """Resolve the target path for the user systemd unit.

    Respects $XDG_CONFIG_HOME when set (the freedesktop spec), falling
    back to ~/.config. Creating the parent directory is the installer's
    responsibility.
    """
    _measure_facade = _compat.facade()
    xdg = _measure_facade.os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = _measure_facade.Path(xdg) if xdg else _measure_facade.Path.home() / ".config"
    return base / "systemd" / "user" / _measure_facade.SYSTEMD_UNIT_NAME


def _systemd_user_unit_path_for(unit_name: str) -> Path:
    """Resolve the unit path for an arbitrary unit name.

    Same base resolution as ``_systemd_user_unit_path`` but for any runtime
    variant's unit name, so the sweeping uninstall can remove a sibling
    runtime's registration whose data dir already vanished.
    """
    _measure_facade = _compat.facade()
    xdg = _measure_facade.os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = _measure_facade.Path(xdg) if xdg else _measure_facade.Path.home() / ".config"
    return base / "systemd" / "user" / unit_name


def _generate_systemd_user_unit(launcher_path, log_dir):
    """Build a systemd user unit for the dashboard daemon.

    ExecStart points at a shell launcher (_generate_linux_launcher_sh)
    rather than a hardcoded sys.executable. The launcher resolves
    Python at runtime so pipx venv removals, distro upgrades, or brew
    swaps don't strand the unit. The ExecStart path
    is double-quoted per systemd.service(5) so paths with spaces in
    HOME survive tokenization.

    Design choices:
      - Restart=on-failure + RestartSec=10 mirrors macOS launchd
        KeepAlive on-non-zero-exit behavior.
      - WantedBy=default.target so enable links the unit into the
        user's default target (graphical-session.target isn't
        universal across distros).
      - StandardOutput/StandardError piped to append-mode log files
        under DAEMON_LOG_DIR so port conflicts and import errors
        leave a trail, matching the Windows .cmd launcher.
      - Type=simple (default) -- the daemon runs in the foreground of
        its own process group, which is what we want.
    """
    _measure_facade = _compat.facade()
    return (
        "[Unit]\n"
        "Description=Token Optimizer Dashboard\n"
        "Documentation=https://github.com/alexgreensh/token-optimizer\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        f'ExecStart="{launcher_path}"\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"StandardOutput=append:{log_dir}/stdout.log\n"
        f"StandardError=append:{log_dir}/stderr.log\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _install_systemd_user_daemon(dry_run=None, soft_fail=None, effective_host=None):
    """Linux: install the dashboard daemon via systemd --user.

    User-scoped unit, no root required. Survives reboot when the user
    has lingering enabled (loginctl enable-linger $USER -- we surface
    the hint but don't auto-enable because some distros require
    sudo/pkexec for that operation).

    soft_fail=True (hook paths, e.g. ensure-health self-heal): every error
    path returns False instead of sys.exit(1) so SystemExit can never escape
    and kill the calling Claude Code session. Returns True on success. CLI
    callers keep the default False for the hard-failure + actionable-hint UX.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._scheduler_install_refused():
        return False

    def _fail(msg, *extra):
        # Under soft_fail we are inside a hook --
        # stdout is session-visible context, so route errors to stderr instead
        # of spamming "[Error] ..." into every SessionStart transcript.
        _measure_facade = _compat.facade()
        out = _measure_facade.sys.stderr if soft_fail else _measure_facade.sys.stdout
        print(msg, file=out)
        for line in extra:
            print(line, file=out)
        if soft_fail:
            return False
        _measure_facade.sys.exit(1)

    unit_path = _measure_facade._systemd_user_unit_path()
    daemon_script = _measure_facade.SNAPSHOT_DIR / "dashboard-server.py"

    if dry_run:
        print("[Token Optimizer] Dry run (Linux). Would install:\n")
        print("  A tiny web server that makes your dashboard available at:")
        print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer\n")
        print("  What it does:")
        print("    - Registers a systemd --user unit (no root needed)")
        print("    - Starts automatically at login via default.target")
        print("    - Restarts itself on failure (Restart=on-failure)")
        for line in _measure_facade._daemon_access_lines(effective_host):
            print(line)
        print("    - Uses ~2MB of memory\n")
        print("  Files it creates:")
        print(f"    {daemon_script}")
        print(f"    {unit_path}\n")
        print("  Tip: `loginctl enable-linger $USER` makes the daemon survive")
        print("  logout without requiring an active session. (Needs sudo on some distros.)\n")
        print("  No changes written.")
        return

    # Reachability check: `systemctl --user --version`
    # succeeds even when the user-bus is dead (SSH w/o linger, cron,
    # WSL2 without systemd=true). Probing list-units requires the bus,
    # so a positive result confirms we can actually enable a unit.
    if not _measure_facade._probe_systemd_user_bus():
        return _fail(
            "[Error] systemctl --user is not reachable on this system.",
            "  Likely causes:",
            "    - systemd is not installed (minimal container, WSL2 w/o systemd=true)",
            "    - this is a headless SSH session without lingering enabled",
            "    - the user dbus is not running in this shell",
            "  Try: loginctl enable-linger $USER    (may need sudo on some distros)",
            _measure_facade._hint("  Then re-run: python3 measure.py setup-daemon"),
            f"  Meanwhile, the dashboard file still works: {_measure_facade.DASHBOARD_PATH.as_uri()}",
        )

    if not _measure_facade._ensure_dashboard_file(to_stderr=soft_fail):
        return _fail("[Error] Dashboard file missing; cannot start daemon.")

    _measure_facade._get_or_create_daemon_token()

    with _measure_facade._daemon_install_lock(soft_fail=soft_fail) as acquired:
        if soft_fail and not acquired:
            # Another installer holds the lock; don't fight it from a hook path.
            return False
        # Ordering: stop + reclaim FIRST, BEFORE touching
        # unit/daemon files, so a failed stop never races a half-written
        # new unit. On failure after write, rollback removes artifacts
        # so re-running doesn't inherit a broken half-install.
        _measure_facade.subprocess.run(
            ["systemctl", "--user", "disable", "--now", _measure_facade.SYSTEMD_UNIT_NAME],
            capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
        _measure_facade._reclaim_posix_daemon_port()

        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _measure_facade.DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)

        daemon_script.write_text(_measure_facade._generate_daemon_script(), encoding="utf-8")
        try:
            daemon_script.chmod(0o755)
        except OSError:
            pass

        launcher_path = _measure_facade.SNAPSHOT_DIR / _measure_facade.LINUX_LAUNCHER_NAME
        launcher_path.write_text(
            _measure_facade._generate_linux_launcher_sh(str(daemon_script), str(_measure_facade.DAEMON_LOG_DIR)),
            encoding="utf-8",
        )
        try:
            launcher_path.chmod(0o755)
        except OSError:
            pass

        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(
            _measure_facade._generate_systemd_user_unit(
                launcher_path=str(launcher_path),
                log_dir=str(_measure_facade.DAEMON_LOG_DIR),
            ),
            encoding="utf-8",
        )

        def _rollback(reason):
            """Remove everything this install just wrote so a retry
            isn't blocked by half-installed artifacts."""
            _measure_facade = _compat.facade()
            print(f"  Rolling back: {reason}")
            for p in (unit_path, launcher_path):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass
            _measure_facade.subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
            )

        reload_result = _measure_facade.subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
        if reload_result.returncode != 0:
            _rollback("daemon-reload failed")
            return _fail(f"[Error] systemctl daemon-reload failed: {reload_result.stderr.strip()}")

        enable_result = _measure_facade.subprocess.run(
            ["systemctl", "--user", "enable", "--now", _measure_facade.SYSTEMD_UNIT_NAME],
            capture_output=True, text=True, errors="replace", creationflags=_measure_facade._NO_WINDOW,
        )
        if enable_result.returncode != 0:
            _rollback("enable failed")
            return _fail(
                f"[Error] systemctl --user enable failed: {enable_result.stderr.strip()}",
                "  Common causes: XDG_RUNTIME_DIR missing (SSH w/o linger), user bus",
                "  refusing a root-owned unit file (bind-mounted home), or a polkit",
                "  prompt that couldn't reach a TTY.",
            )

        _measure_facade.time.sleep(1)
        if _measure_facade._verify_daemon_port():
            print("[Token Optimizer] Dashboard server installed and running.\n")
            print("  Bookmark this URL:")
            print(f"    http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer\n")
            _measure_facade._print_daemon_network_note(effective_host)
            print(f"  It updates automatically after every {_measure_facade.runtime_name_for_humans()} session.")
            print("  Starts at login via default.target.\n")
            print("  Survive logout: loginctl enable-linger $USER (may need sudo)")
            print(_measure_facade._hint("  To remove: python3 measure.py setup-daemon --uninstall"))
        else:
            print("[Token Optimizer] Unit enabled but port not yet reachable.")
            print(f"  Give it 30s, then open: http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer")
            print("  Do NOT run --uninstall while it is still coming up.")
            print(f"  Diagnose: systemctl --user status {_measure_facade.SYSTEMD_UNIT_NAME}")
            print(f"  Logs:     {_measure_facade.DAEMON_LOG_DIR}/stderr.log")
        # Install succeeded (unit enabled); port may still be warming up.
        return True


def _uninstall_systemd_user_daemon(this_install_only=None, dry_run=None):
    """Linux: stop and remove the systemd --user dashboard unit.

    v5.11.68: identity-sweeping by default. The systemd unit is
    per-RUNTIME (shared across ``token-optimizer-*`` identities of the same
    runtime), so it is removed once, and we disable+remove EVERY runtime's
    unit so a unit whose data dir already vanished still gets unregistered.
    The per-identity files (``dashboard-server.py``, the launcher script,
    ``daemon-token``, ``dashboard-host``, ``.daemon-thrash``) live in EACH
    identity's snapshot dir; a sibling's daemon script + 0600 CSRF token
    would otherwise outlive the uninstall. Sweep every identity unless
    ``this_install_only`` opts out.

    v5.11.68 (cleanup): ``dry_run=True`` makes the uninstaller
    side-effect-free (no systemctl calls, no file deletion, no tombstone write)
    so the cleanup command's ``--dry-run`` is a true preview.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return False

    if dry_run:
        would_remove = []
        per_identity: list[tuple[Path, list[str]]] = []
        for unit_name in _measure_facade._scheduler_names_to_sweep(
            this_install_only, _measure_facade._ALL_SYSTEMD_UNIT_NAMES, _measure_facade.SYSTEMD_UNIT_NAME):
            unit_path = _measure_facade._systemd_user_unit_path_for(unit_name)
            if unit_path.exists():
                would_remove.append(str(unit_path))
        for snap_dir in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
            files = _measure_facade._daemon_per_identity_files(snap_dir)
            identity_would = [str(files[k]) for k in ("daemon_script", "linux_launcher", "daemon_token", "daemon_host") if files[k].exists()]
            if identity_would:
                per_identity.append((snap_dir, identity_would))
                would_remove.extend(identity_would)
        if would_remove:
            print("[Token Optimizer] Dashboard daemon would be removed.")
            for path in would_remove:
                print(f"  Would delete: {path}")
            if per_identity and not this_install_only:
                print("  Would sweep all token-optimizer-* identities:")
                for snap_dir, items in per_identity:
                    print(f"    {snap_dir}: {len(items)} file(s)")
        else:
            print("[Token Optimizer] No daemon artifacts found. Nothing to remove.")
        return
    # Tombstone FIRST so any racing respawn exits cleanly.
    _measure_facade._write_uninstall_tombstone()
    removed = []
    # Sweep the OS scheduler by name across every runtime variant.
    for unit_name in _measure_facade._scheduler_names_to_sweep(
            this_install_only, _measure_facade._ALL_SYSTEMD_UNIT_NAMES, _measure_facade.SYSTEMD_UNIT_NAME):
        try:
            _measure_facade.subprocess.run(
                ["systemctl", "--user", "disable", "--now", unit_name],
                capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
        except (OSError, _measure_facade.subprocess.TimeoutExpired):
            pass
        unit_path = _measure_facade._systemd_user_unit_path_for(unit_name)
        if _measure_facade._unlink_if_exists(unit_path):
            removed.append(str(unit_path))
    # daemon-reload AFTER unit file removal so systemd drops the units
    # from its in-memory catalog.
    try:
        _measure_facade.subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
        )
    except (OSError, _measure_facade.subprocess.TimeoutExpired):
        pass
    # Unregistering the job does NOT stop the process --
    # `launchctl bootout` / `systemctl disable` drop the registration without
    # SIGTERMing the running child, so the daemon kept serving its port with
    # the 0600 CSRF token live in memory until logout. Reclaim the port(s) now
    # that the tombstone is down (so nothing races a respawn) and the job is
    # unregistered. On a sweep-all uninstall this covers EVERY runtime's port
    # (a sibling runtime's daemon + live CSRF token otherwise
    # survived), scoped back to the resolved runtime under --this-install-only.
    # Never kills a foreign process on a port -- the helper verifies the command
    # line is our own dashboard-server.py.
    _measure_facade._reclaim_daemon_ports(this_install_only)
    # Sweep per-identity daemon files across every token-optimizer-* identity.
    per_identity_removed: list[tuple[Path, list[str]]] = []
    sweep_failed: list[str] = []
    for snap_dir in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
        identity_removed, identity_failed = _measure_facade._sweep_identity_daemon_files(
            snap_dir, ("daemon_script", "linux_launcher", "daemon_token", "daemon_host"))
        sweep_failed.extend(identity_failed)
        # The .daemon-thrash tombstone must PERSIST for every
        # swept identity. Each identity's generated dashboard-server.py checks
        # its OWN breadcrumb path on start and exits "noop-tombstoned"; the
        # previous unlink here re-armed self-revive, so an orphaned LaunchAgent
        # /task/unit could resurrect the daemon and mint a fresh 0600 token
        # after a "successful" uninstall. Only a legitimate reinstall clears it
        # (setup_daemon unlinks before start). NOT counted toward `removed` so
        # the "Nothing to remove" honesty invariant holds.
        _measure_facade._write_uninstall_tombstone(snap_dir)
        if identity_removed:
            per_identity_removed.append((snap_dir, identity_removed))
            removed.extend(identity_removed)
    _measure_facade._print_identity_sweep_report(
        removed, per_identity_removed, sweep_failed, this_install_only)


def _normalized_platform():
    """Return a normalized platform label.

    Hedges against frozen builds or test harnesses that
    monkey-patch platform.system() can return non-canonical casing
    ("darwin", "Macintosh", "MacOSX"). We normalize a small allowlist
    of aliases to the canonical CPython labels.
    """
    _measure_facade = _compat.facade()
    raw = (_measure_facade.platform.system() or "").strip()
    lower = raw.lower()
    if lower in ("darwin", "macintosh", "macosx", "mac os x", "mac"):
        return "Darwin"
    if lower == "windows":
        return "Windows"
    if lower == "linux":
        return "Linux"
    return raw


def _setup_daemon_cli(args):
    """`setup-daemon` CLI entry. A refusal used to return "noop-sandbox" to a
    caller that ignored it, so the command printed nothing and the user had
    no idea why the dashboard never came back. Prints the reason, exit 1."""
    _measure_facade = _compat.facade()
    dry = "--dry-run" in args
    uninstall = "--uninstall" in args
    this_install_only = "--this-install-only" in args
    result = _measure_facade.setup_daemon(dry_run=dry, uninstall=uninstall, this_install_only=this_install_only)
    if result == "noop-sandbox":
        detail = "" if (uninstall or _measure_facade._daemon_snapshot_sandboxed()) else _measure_facade._scheduler_job_points_elsewhere()
        print("[Token Optimizer] setup-daemon " + _measure_facade._SCHEDULER_REFUSAL
              + (" (existing job: %s)" % detail if detail else ""))
        return 1
    return 0


def setup_daemon(dry_run=None, uninstall=None, this_install_only=None, latch_optout=None):
    """Install or remove the persistent dashboard HTTP server daemon.

    ``latch_optout`` (default True) controls whether an uninstall persists the
    sticky ``daemon_disabled`` opt-out that stops the SessionStart self-heal from
    ever resurrecting the daemon. The dedicated ``setup-daemon --uninstall``
    command -- whose whole purpose is "turn my dashboard off" -- passes True.
    ``cleanup`` (the full-uninstall tidy) passes False: it removes the hooks too,
    so the self-heal simply stops running after a genuine uninstall, and if the
    user only tidied up while keeping Token Optimizer active, the self-heal must
    be free to bring the dashboard back on its own. A tidy is not a deliberate
    opt-out, and must never silently kill the dashboard until a manual command.

    Dispatches to a platform-specific installer/uninstaller. macOS uses
    launchd (LaunchAgent); Windows and Linux installers ship in future
    releases. All platforms share the daemon script and port
    (DAEMON_PORT = 24842) so the bookmarkable URL is identical
    everywhere.

    v5.11.68: ``--uninstall`` is identity-sweeping by default
    (removes the daemon script + 0600 CSRF token from EVERY
    ``token-optimizer-*`` identity, and unregisters every runtime's scheduler
    artifact). Pass ``this_install_only=True`` (``--this-install-only``) to
    clean only the resolved identity, for a user intentionally running
    side-by-side installs who only wants this one gone.
    """
    _measure_facade = _compat.facade()
    if (_measure_facade._daemon_snapshot_sandboxed() if uninstall else _measure_facade._scheduler_install_refused()):
        return "noop-sandbox"
    system = _measure_facade._normalized_platform()
    if uninstall:
        if system == "Darwin":
            _measure_facade._uninstall_launchd_daemon(this_install_only=this_install_only, dry_run=dry_run)
        elif system == "Windows":
            _measure_facade._uninstall_task_scheduler_daemon(this_install_only=this_install_only, dry_run=dry_run)
        elif system == "Linux":
            _measure_facade._uninstall_systemd_user_daemon(this_install_only=this_install_only, dry_run=dry_run)
        else:
            print(f"[Token Optimizer] Unsupported platform for daemon uninstall: {system}")
        # Sticky opt-out: persist the user's DELIBERATE intent so the SessionStart
        # ensure-health self-install (`_ensure_dashboard_daemon`) never resurrects
        # a daemon the user explicitly turned off. Mirrors the quality-bar opt-out.
        # Gated on `latch_optout`: only the dedicated `setup-daemon --uninstall`
        # command sets it. `cleanup` (full-uninstall tidy) passes latch_optout=
        # False -- it removes the hooks too, so a genuine uninstall stops the
        # self-heal anyway, and a user who merely tidied while keeping Token
        # Optimizer active must get the dashboard self-healed back automatically,
        # never left silently dead pending a manual `setup-daemon`.
        if not dry_run and latch_optout:
            _measure_facade._set_daemon_disabled(True)
        elif dry_run:
            # A dry-run does NOT write the sticky opt-out, so the SessionStart
            # self-heal would reinstall the daemon. Say so, or the user thinks a
            # preview opted them out for good.
            print("  [Token Optimizer] (dry run: sticky opt-out NOT written -- "
                  "the daemon will be reinstalled at next SessionStart. Run "
                  "`setup-daemon --uninstall` without --dry-run to opt out for good.)")
        return
    # v5.11.1: resolve the bind-host once, before branching, so every OS
    # installer (and dry-run messaging) shares the same effective host. The
    # daemon runs under launchd/systemd/Task Scheduler with an empty env, so it
    # reads the persisted value instead of the env var. Dry-run resolves the
    # same env > file > default chain but skips the write, staying
    # side-effect-free.
    effective_host = _measure_facade._persist_dashboard_host(persist=not dry_run)
    # Explicit install = explicit opt-in: clear any sticky opt-out so the
    # ensure-health self-heal resumes maintaining this daemon. Symmetric with
    # the quality-bar install path. Skipped on dry-run (side-effect-free).
    if not dry_run:
        _measure_facade._set_daemon_disabled(False)
        # Every platform's uninstall leaves a permanent tombstone the daemon
        # obeys forever; an explicit install is what clears it. Done here, not
        # per installer: only the launchd installer cleared it, so on Windows
        # and Linux `setup-daemon` after `--uninstall` left the daemon dead.
        try:
            if _measure_facade.DAEMON_THRASH_BREADCRUMB.exists():
                _measure_facade.DAEMON_THRASH_BREADCRUMB.unlink()
        except OSError:
            pass
    if system == "Darwin":
        _installed_ok = _measure_facade._install_launchd_daemon(dry_run=dry_run, effective_host=effective_host)
    elif system == "Windows":
        _installed_ok = _measure_facade._install_task_scheduler_daemon(dry_run=dry_run, effective_host=effective_host)
    elif system == "Linux":
        _installed_ok = _measure_facade._install_systemd_user_daemon(dry_run=dry_run, effective_host=effective_host)
    else:
        print(f"[Error] Dashboard daemon not supported on {system}.")
        print(f"  Open the dashboard file directly: {_measure_facade.DASHBOARD_PATH.as_uri()}")
        _measure_facade.sys.exit(1)
    # THE ONLY place the sticky install-failed marker is cleared.
    # An explicit `setup-daemon` that actually succeeded is the user telling us
    # the structural problem is fixed. Not on dry-run (side-effect-free), and
    # not on a failed install -- the installers return False under soft_fail and
    # sys.exit(1) otherwise, so neither reaches here with a truthy result.
    if not dry_run and _installed_ok:
        _measure_facade._clear_daemon_install_failed_marker()


def _retained_paths() -> list[tuple[str, Path]]:
    """User-data paths preserved by design across an uninstall.

    Session snapshots, compaction checkpoints, trend aggregates, and the
    quality-bar cache are NOT removed by the cleanup command. They are
    disclosed in --dry-run so the user knows what stays.
    """
    _measure_facade = _compat.facade()
    paths: list[tuple[str, Path]] = []
    try:
        for ident in _measure_facade._all_plugin_data_dirs():
            paths.append((f"session data ({ident.name})", ident / "data"))
            paths.append((f"checkpoints ({ident.name})", ident / "data" / "checkpoints"))
            paths.append((f"trends ({ident.name})", ident / "data" / "trends"))
    except Exception:  # noqa: BLE001
        pass
    # Always include the resolved snapshot dir even under a sandbox override.
    paths.append(("session data (this install)", _measure_facade.SNAPSHOT_DIR))
    # Compaction checkpoints (conversation-context captures) live under the
    # per-session state base, which on a script install is NOT inside
    # SNAPSHOT_DIR (#106 named them as undisclosed). Disclose them too.
    try:
        listed = {p for _, p in paths}
        if _measure_facade.CHECKPOINT_DIR not in listed:
            paths.append(("checkpoints (this install)", _measure_facade.CHECKPOINT_DIR))
    except NameError:
        pass
    return paths


def _backup_settings_file(dest_dir: Path) -> Path | None:
    """Copy settings.json into ``dest_dir``. Returns the backup path or None."""
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.SETTINGS_PATH.is_file():
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "settings.json"
        i = 1
        while dest.exists():
            dest = dest_dir / f"settings.json-{i}"
            i += 1
        _measure_facade.shutil.copy2(_measure_facade.SETTINGS_PATH, dest)
        return dest
    except OSError:
        return None


def _is_our_hook_entry(entry) -> bool:
    """True when a settings.json hook entry is one WE installed.

    Ownership markers, all anchored on our own script names rather than a bare
    "token-optimizer" substring (a user hook may legitimately mention us in a
    path or a comment without being ours to delete):
      * the command invokes one of our scripts (measure.py / read_cache.py /
        statusline.js / archive_result.py / context_intel.py) AND references
        token-optimizer, or
      * it carries one of our distinctive subcommands (quality-cache,
        compact-capture, compact-restore, ensure-health, clear-compacted,
        session-end-flush, dynamic-compact-instructions).

    Conservative by construction: unrecognized shapes are NOT ours, so a hook
    we cannot positively identify is left in place. The cost of a false
    negative is a dangling entry the user can delete; the cost of a false
    positive is silently deleting their automation.
    """
    _measure_facade = _compat.facade()
    if not isinstance(entry, dict):
        return False
    cmd = entry.get("command")
    if not isinstance(cmd, str) or not cmd:
        return False
    low = cmd.lower()
    our_subcommands = (
        "quality-cache", "compact-capture", "compact-restore", "ensure-health",
        "clear-compacted", "session-end-flush", "dynamic-compact-instructions",
    )
    if any(sub in low for sub in our_subcommands):
        return True
    our_scripts = (
        "measure.py", "read_cache.py", "statusline.js", "archive_result.py",
        "context_intel.py", "activity_tracker.py",
        # the UserPromptSubmit dispatcher. Without it a hook that
        # runs userpromptsubmit_runner.py (+ token-optimizer) is not recognised
        # as ours, so ownership-gated repair/reconciliation skips it. Whitelisting
        # the script name here is inert until the dispatcher ships (it is not yet
        # in this branch) and never matches a foreign hook (the token-optimizer
        # substring is still required alongside it).
        "userpromptsubmit_runner.py",
    )
    if "token-optimizer" in low and any(s in low for s in our_scripts):
        return True
    return False


def _remove_our_settings_entries(settings: dict) -> list[str]:
    """Strip Token Optimizer's OWN entries from ``settings`` in place.

    Removes only entries we own:
    - ``statusLine`` when its command references our ``statusline.js`` +
      ``token-optimizer`` (the same matcher _is_quality_bar_installed uses).
    - ``UserPromptSubmit`` hook groups whose command references
      ``quality-cache`` (our cache hook).
    - ``SessionEnd`` hook entries that are ours (reuses
      _remove_token_optimizer_session_end_hooks).
    - ``compactInstructions`` when it is the block setup-smart-compact
      generated (removed) or holds our appended section (only that section
      is removed; the user's own text stays).

    Foreign statusLines, foreign hooks, and every other settings key are left
    byte-identical. Returns a list of human-readable descriptions of what was
    removed. Never raises.
    """
    _measure_facade = _compat.facade()
    removed: list[str] = []
    if not isinstance(settings, dict):
        return removed
    # statusLine
    sl = settings.get("statusLine")
    if isinstance(sl, dict):
        cmd = sl.get("command", "")
        if _measure_facade._statusline_cmd_is_ours(cmd):
            del settings["statusLine"]
            removed.append("statusLine (token-optimizer)")
    # Hook entries we own, across every event.
    #
    # The old loop dropped ANY group whose filtered hook list
    # came out empty -- including a user-authored group that never held a hook
    # of ours (an empty `hooks: []`, or a group of foreign hooks in an event we
    # also use) -- and miscounted each one as "UserPromptSubmit quality-cache
    # hook". A group is now removed ONLY when it actually contained one of our
    # hooks and filtering emptied it; user groups pass through untouched.
    #
    # The sweep covered UserPromptSubmit + SessionEnd only,
    # leaving our smart-compact family (PreCompact / SessionStart / Stop /
    # SessionEnd) dangling in settings.json after cleanup -- commands pointing
    # into a plugin tree that is about to be deleted. Every event is now
    # filtered with the same ownership predicate.
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            new_groups = []
            n_removed = 0
            for group in groups:
                if not isinstance(group, dict):
                    new_groups.append(group)
                    continue
                entries = group.get("hooks")
                if not isinstance(entries, list):
                    new_groups.append(group)
                    continue
                kept = [h for h in entries if not _measure_facade._is_our_hook_entry(h)]
                n_ours = len(entries) - len(kept)
                if n_ours == 0:
                    # Nothing of ours here -- leave the group exactly as-is,
                    # even when it is empty (it is the user's to keep).
                    new_groups.append(group)
                    continue
                n_removed += n_ours
                if kept:
                    group["hooks"] = kept
                    new_groups.append(group)
                # else: the group held only our hooks -> drop the whole group.
            if n_removed:
                removed.append(f"{n_removed} {event} hook(s)")
            if new_groups:
                hooks[event] = new_groups
            else:
                del hooks[event]
    # SessionEnd hooks (reuses the proven filter)
    n_session_end = _measure_facade._remove_token_optimizer_session_end_hooks(settings)
    if n_session_end:
        removed.append(f"{n_session_end} SessionEnd hook(s)")
    # compactInstructions written by setup-smart-compact (#106: it outlived
    # cleanup). Only our generated block / appended section is removed.
    if _measure_facade._remove_our_compact_instructions(settings):
        removed.append("compactInstructions (token-optimizer)")
    return removed


def cleanup(dry_run=None, this_install_only=None):
    """One-command uninstall cleanup for Token Optimizer.

    Orchestrates the three uninstall surfaces:
    1. Daemon: stop + remove the dashboard daemon across all
       ``token-optimizer-*`` identities (or just this install under
       ``this_install_only``), via ``setup_daemon(uninstall=True, ...)``.
    2. Host config (``settings.json``): back up first, then remove ONLY our
       own entries (statusLine, quality-cache hook, SessionEnd hooks), leaving
       every other key byte-identical.
    3. Manifests (``installed_plugins.json`` / ``known_marketplaces.json``):
       back up first, then remove our STALE entries via
       ``install_reconcile.reconcile_uninstall(remove=True)``.

    Session data, checkpoints, and trends are PRESERVED by design and
    disclosed in the report.

    ``--dry-run`` is side-effect-free and doubles as retained-paths
    DISCLOSURE: it prints exactly what WOULD be removed and what stays, so
    the user can review before agreeing to a real cleanup.
    """
    _measure_facade = _compat.facade()
    print("[Token Optimizer] Cleanup", "(dry run)" if dry_run else "")
    print(f"  {'=' * 60}")
    if this_install_only:
        print("  Scope: this install only (--this-install-only)")
    else:
        print("  Scope: all token-optimizer-* identities")
    print()

    # 1. Daemon
    print("  [1/3] Dashboard daemon")
    # latch_optout=False: a cleanup is a tidy, not a deliberate "turn my dashboard
    # off forever". This removes the daemon now, but does NOT set the sticky
    # daemon_disabled flag -- so if Token Optimizer stays active, the SessionStart
    # self-heal brings the dashboard back automatically, with no manual command.
    # A genuine full uninstall removes the hooks here too, so the self-heal simply
    # stops running; the sticky flag was never what kept a real uninstall gone.
    _measure_facade.setup_daemon(dry_run=dry_run, uninstall=True, this_install_only=this_install_only, latch_optout=False)
    # The launchd LaunchAgent plist is removed by the macOS uninstaller only.
    # Defensively unlink it here too so a stray plist (a HOME migrated or
    # restored from a Mac, a copied backup) is always cleared, regardless of
    # the platform running cleanup -- it is just a file. Skipped on dry-run so
    # the preview stays side-effect-free.
    if not dry_run:
        _measure_facade._unlink_if_exists(_measure_facade.PLIST_PATH)
        # Same reasoning for the other platforms' per-identity daemon files:
        # each platform's uninstaller sweeps only its own launcher, so a HOME
        # synced or migrated from another OS kept dashboard-launcher.cmd /
        # .schtasks-daemon.xml (Windows) or the Linux launcher after cleanup
        # (#106). They are plain files once the scheduler job is gone.
        if not _measure_facade._daemon_snapshot_sandboxed():
            for _snap in _measure_facade._daemon_identity_snapshot_dirs(this_install_only):
                _gone, _ = _measure_facade._sweep_identity_daemon_files(
                    _snap, ("daemon_script", "daemon_token", "daemon_host",
                            "windows_launcher", "linux_launcher"))
                try:
                    for _xml in _snap.glob("*schtasks-daemon*.xml"):
                        if _measure_facade._unlink_if_exists(_xml):
                            _gone.append(str(_xml))
                except OSError:
                    pass
                for _path in _gone:
                    print(f"  Deleted: {_path}")
    print()

    # 2. settings.json
    print("  [2/3] Host config (settings.json)")
    settings, _ = _measure_facade._read_settings_json()
    our_entries = _measure_facade._remove_our_settings_entries(settings)  # mutate a copy
    if our_entries:
        if dry_run:
            for entry in our_entries:
                print(f"    Would remove: {entry}")
        else:
            # Do NOT claim "Removed" before the write lands. The
            # entries are echoed after a confirmed successful write below; a
            # refused read or a denied lease prints a WARNING instead, so the
            # report can never assert a removal that did not happen.
            pass
        if not dry_run:
            backup_root = _measure_facade.CLAUDE_DIR / "_backups" / "token-optimizer"
            stamp = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_dest = backup_root / stamp
            backed = _measure_facade._backup_settings_file(backup_dest)
            if backed is None:
                print(f"    WARNING: could not back up settings.json; entries left in place.")
            else:
                # Re-read under the backup, re-remove, write atomically.
                #
                # A failed re-read yields {} ("unknown"), and
                # writing that would erase every key the user owns. Refuse to
                # write on a bad read and say so -- their entries stay in
                # place, which is recoverable; an emptied settings.json is not.
                fresh, _, read_ok = _measure_facade._read_settings_json_checked()
                if not read_ok:
                    print(
                        "    WARNING: settings.json became unreadable (malformed "
                        "or permission denied) after the backup; refusing to "
                        "write.\n"
                        f"    Entries left in place. Backup: {backed}"
                    )
                else:
                    _measure_facade._remove_our_settings_entries(fresh)
                    # `_remove_our_settings_entries` deletes exactly one
                    # TOP-LEVEL key when it is ours: `statusLine`. Everything
                    # else it touches is nested inside `hooks`. Declare that one
                    # key so the choke-point guard permits this deliberate
                    # uninstall removal and still blocks accidental key loss.
                    # A denied advisory lease makes the write a
                    # silent no-op. Reporting "Removed" for a write that never
                    # landed leaves the user with the dangling statusLine this
                    # command exists to clear, so surface it instead.
                    # user_initiated: cleanup is an explicit user command. Under
                    # the hook cohort throttle any Token Optimizer settings
                    # write in the previous ~10s (a SessionStart heal, a setup
                    # command) made this write lose the lease, and the whole
                    # removal was skipped (#106 leftovers).
                    if _measure_facade._write_settings_atomic(
                            fresh, allow_removing_keys={"statusLine", "compactInstructions"},
                            user_initiated=True):
                        for entry in our_entries:
                            print(f"    Removed: {entry}")
                        print(f"    Backup: {backed}")
                    else:
                        print(
                            "    WARNING: settings.json is locked by another "
                            "Token Optimizer process; NOTHING was removed.\n"
                            "    Re-run cleanup in a few seconds (no Claude Code "
                            f"session should be starting). Backup: {backed}"
                        )
    else:
        print("    No Token Optimizer entries found in settings.json.")
    print()

    # 2b. Subagent prompt-cache TTL: the uninstall path calls the SAME undo
    # as `subagent-cache disable` -- remove the key only when the marker says
    # Token Optimizer set it and the value is still "1h". Dry-run previews.
    print("  [2b/3] Subagent cache (subagentPromptCacheTtl)")
    try:
        if dry_run:
            _sc_marker = _measure_facade._subagent_cache_read_marker()
            if _sc_marker and _sc_marker.get("state") == "set":
                print("    Would remove: subagentPromptCacheTtl (set by Token Optimizer)")
            else:
                print("    Nothing to remove (no marker says Token Optimizer set it)")
        else:
            _sc_undo = _measure_facade.subagent_cache_disable()
            if _sc_undo.get("changed"):
                print("    Removed: subagentPromptCacheTtl (was set by Token Optimizer)")
            elif _sc_undo.get("state") == "platform-gap":
                print("    Skipped: not Claude Code")
            else:
                print(f"    Nothing to remove ({_sc_undo.get('state')})")
    except Exception as _sc_exc:
        print(f"    WARNING: subagent cache undo failed: {_sc_exc}")
    print()

    # 3. Manifests
    print("  [3/3] Plugin manifests (installed_plugins / known_marketplaces)")
    import install_reconcile
    reconcile = install_reconcile.reconcile_uninstall(
        _measure_facade.CLAUDE_DIR, dry_run=dry_run, remove=True,
        daemon_already_stopped=True,
    )
    for entry in reconcile.get("reported", []):
        stale_tag = " (stale)" if entry.get("stale") else " (active, kept)"
        print(f"    {entry['file']}::{entry['key']}{stale_tag}")
    for warning in reconcile.get("warnings", []):
        print(f"    {warning}")
    if reconcile.get("backup"):
        print(f"    Backup: {reconcile['backup']}")
    for removed in reconcile.get("removed", []):
        print(f"    {'Would remove' if dry_run else 'Removed'}: {removed}")
    if not reconcile.get("reported"):
        print("    No Token Optimizer manifest entries found.")
    print()

    # Retained-paths disclosure (always shown, dry-run or not).
    print("  Retained by design (session data, checkpoints, trends):")
    for label, path in _measure_facade._retained_paths():
        exists = path.exists() if hasattr(path, "exists") else False
        tag = " (present)" if exists else " (absent)"
        print(f"    {label}: {path}{tag}")
    print()
    if dry_run:
        print("  Dry run: nothing was modified. Re-run without --dry-run to apply.")
    else:
        print("  Cleanup complete. Re-enable later with: setup-quality-bar / setup-daemon")
    print()


def _daemon_service_installed(system=None):
    """True when the OS service manager has the dashboard daemon REGISTERED.

    This is the "is it installed" half of the ensure-health health check; the
    "is it alive" half is `_verify_daemon_port`. Kept cheap: macOS/Linux are
    stat-only (no subprocess); only Windows shells out (one locale-independent
    `schtasks /Query`, matching the rest of the Windows daemon code which avoids
    parsing localized text output). Never raises.

    Returns False on unsupported platforms so the ensure path no-ops there.
    """
    _measure_facade = _compat.facade()
    system = system or _measure_facade._normalized_platform()
    try:
        if system == "Darwin":
            return _measure_facade.PLIST_PATH.exists()
        if system == "Linux":
            return _measure_facade._systemd_user_unit_path().exists()
        if system == "Windows":
            try:
                rc = _measure_facade.subprocess.run(
                    ["schtasks", "/Query", "/TN", _measure_facade.WINDOWS_TASK_NAME],
                    capture_output=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
                ).returncode
                return rc == 0
            except (OSError, _measure_facade.subprocess.TimeoutExpired):
                return False
    except OSError:
        return False
    return False


def _daemon_ensure_throttle_seconds():
    """Throttle window (seconds) for the COSTLY half of the ensure path
    (install/restart). The cheap presence + port probe runs every SessionStart;
    an actual install attempt is retried at most once per this interval so a
    persistently-failing install (e.g. unwritable LaunchAgents dir) doesn't shell
    out on every prompt. Mirrors the 24h hook-heal throttle. Read lazily (not a
    module-level constant) because `_int_env` is defined further down the file.
    Overridable via TOKEN_OPTIMIZER_DAEMON_ENSURE_THROTTLE for tests.
    """
    _measure_facade = _compat.facade()
    return _measure_facade._int_env("TOKEN_OPTIMIZER_DAEMON_ENSURE_THROTTLE", 24 * 3600)


def _daemon_pulse_probe_seconds():
    """Cadence (seconds) of the mid-session liveness PROBE. Within this window the
    per-turn pulse is a single config read -- no stat, no socket. Overridable via
    TOKEN_OPTIMIZER_DAEMON_PULSE_PROBE (tests)."""
    _measure_facade = _compat.facade()
    return _measure_facade._int_env("TOKEN_OPTIMIZER_DAEMON_PULSE_PROBE", 60)


def _daemon_pulse_revive_seconds():
    """Cadence (seconds) of the mid-session REVIVE. A persistently-dead daemon
    (e.g. its port stolen by a foreign process) is retried at most this often so
    the pulse never spawns a revive every minute. Independent of the 24h
    SessionStart ensure throttle. Overridable via TOKEN_OPTIMIZER_DAEMON_PULSE_REVIVE."""
    _measure_facade = _compat.facade()
    return _measure_facade._int_env("TOKEN_OPTIMIZER_DAEMON_PULSE_REVIVE", 300)


def _ensure_dashboard_daemon(force=None):
    """SessionStart self-heal: INSTALL the dashboard daemon if missing, RESTART
    it if installed-but-dead, NO-OP when healthy. Default-on for every Claude
    user (loopback-only, token-authed, ~0 cost), with a sticky `daemon_disabled`
    opt-out -- mirroring the quality-bar self-install, NOT the marker-gated
    keep-warm scheduler (that gate exists because keep-warm spends tokens; this
    daemon does not).

    Cheap-gate-first so the healthy hot path costs only a config read + a stat +
    a fast (sleepless) port probe -- no install lock, no install subprocess,
    well inside the ensure-health budget. The one costly branch (install/restart)
    is throttled to once per `_daemon_ensure_throttle_seconds()` via a config
    timestamp; `force=True` bypasses the throttle (tests / explicit repair).

    Concurrency: this does NOT take `_daemon_install_lock` itself -- the platform
    installers self-serialise on that lock (soft_fail=True), so two parallel
    sessions can't double-install; the loser soft-skips and we resolve the final
    status from on-disk reality. Restart is lock-free (kickstart/restart is
    idempotent), matching the auto-update block.

    Security: a silent auto-install always binds LOOPBACK (127.0.0.1) regardless
    of any persisted dashboard host -- a default-on install must never surface a
    network-exposed daemon. An explicit `setup-daemon` still honours a chosen host.

    Distinct from the daemon AUTO-UPDATE block in run_ensure_health: that block
    refreshes a STALE script for an already-installed daemon; this function
    handles the ABSENT / dead-service case. Run this AFTER the update block so
    update owns has-daemon and ensure owns no-daemon.

    Returns one of: 'noop-foreign', 'noop-disabled', 'noop-install-failed',
    'noop-unsupported', 'noop-healthy', 'noop-throttled', 'installed',
    'install-failed', 'restarted', 'restart-stale', 'restart-failed'.
    ('restart-stale' propagates up from _restart_dashboard_daemon's
    landing-verification.) Never raises.
    """
    # An explicit snapshot override is an isolation boundary, including when a
    # detached daemon-revive child calls this with force=True. Frozen flag
    # only up here: this is the SessionStart hot path, and the full guard's
    # job check runs `schtasks /Query` on Windows. It runs below, once the
    # cheap gates and the health probe say a repair is actually due.
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return "noop-sandbox"
    # Claude Code cloud VM: no browser to open the URL in, and no systemd user
    # bus to install a unit on (the install path fails in containers).
    if _measure_facade._remote_lean_profile():
        return "noop-remote"
    # Cheapest gates first -- all pure/stat, no subprocess.
    if _measure_facade._is_foreign_runtime() or _measure_facade.detect_runtime() != "claude":
        return "noop-foreign"
    if _measure_facade._read_config_flag("daemon_disabled", False):
        return "noop-disabled"
    # A prior install/self-heal failed for a DEFINITIVE reason.
    # Refuse. BEFORE the `force` check on purpose -- `daemon-revive`
    # (force=True) is the per-turn retry path this marker exists to stop, and
    # on Windows each retry costs the user a cmd-window flash.
    # A live, identity-verified daemon DISPROVES the
    # "structurally broken" record (the classic case: an install-lock race
    # where the losing session armed the marker while the winner installed
    # fine). One cheap sleepless socket probe -- no subprocess, cannot flash --
    # buys the un-wedge; otherwise the refusal stands until `setup-daemon`.
    if _measure_facade._daemon_install_failed_marker_present():
        if _measure_facade._verify_daemon_port(timeout_seconds=1, retries=1, retry_sleep=0):
            _measure_facade._clear_daemon_install_failed_marker()
            return "noop-healthy"
        return "noop-install-failed"
    system = _measure_facade._normalized_platform()
    if system not in ("Darwin", "Linux", "Windows"):
        return "noop-unsupported"

    installed = _measure_facade._daemon_service_installed(system)
    # Fast, sleepless probe: this runs on the SessionStart budget, so a single
    # quick attempt with NO inter-retry sleep is all we can afford.
    alive = _measure_facade._verify_daemon_port(timeout_seconds=1, retries=1, retry_sleep=0)
    if installed and alive:
        return "noop-healthy"

    # A repair is due. Refuse it (before stamping the throttle) when the
    # account's job belongs to another install: writing ours would re-point it.
    if _measure_facade._scheduler_install_refused():
        return "noop-sandbox"

    # We need the costly path (install or restart). Throttle the ATTEMPT so a
    # persistently-failing install doesn't shell out every prompt. The cheap
    # probe above still runs every time, so a daemon that comes back healthy is
    # detected instantly; only the repair attempt is rate-limited.
    if not force:
        last = _measure_facade._read_config_flag("last_daemon_ensure_attempt", 0)
        try:
            if _measure_facade.time.time() - float(last or 0) < _measure_facade._daemon_ensure_throttle_seconds():
                return "noop-throttled"
        except (TypeError, ValueError):
            pass
        # Stamp BEFORE the attempt so a failure (or a hard exit upstream) can't
        # loop the costly path next session.
        try:
            _measure_facade._write_config_flag("last_daemon_ensure_attempt", int(_measure_facade.time.time()))
        except Exception:
            pass

    if installed:
        # Installed but dead -> restart (lock-free, idempotent).
        # 'restart-failed' is NOT
        # marker-worthy. It is reached through a blanket `except Exception`
        # whose common inhabitants are TRANSIENT -- a 5s launchctl kickstart
        # TimeoutExpired on a busy Mac, a slow systemd, an AV-locked schtasks.
        # An INSTALLED daemon proves the structure works; the 24h ensure
        # throttle already bounds retry cost. A verified restart clears any
        # stale marker a concurrent session may have raced in.
        _status = _measure_facade._restart_dashboard_daemon(system)
        if _status == "restarted":
            _measure_facade._clear_daemon_install_failed_marker()
        return _status

    # Not installed -> run the platform installer. Each installer self-locks with
    # soft_fail so a concurrent session can't deadlock or double-install. Force
    # loopback for this silent install. `except (Exception, SystemExit)` is
    # belt-and-suspenders: installers return False under soft_fail, but a future
    # edit that reintroduces sys.exit must NEVER kill the SessionStart hook.
    try:
        if not _measure_facade._ensure_dashboard_file(to_stderr=True):
            # Transient class (disk full once, a regen hiccup): the 24h
            # throttle bounds the retry. Never marker-worthy.
            return "install-failed"
        _measure_facade._get_or_create_daemon_token()
        if system == "Darwin":
            ok = _measure_facade._install_launchd_daemon(soft_fail=True, effective_host="127.0.0.1")
        elif system == "Linux":
            ok = _measure_facade._install_systemd_user_daemon(soft_fail=True, effective_host="127.0.0.1")
        else:  # Windows
            ok = _measure_facade._install_task_scheduler_daemon(soft_fail=True, effective_host="127.0.0.1")
        if ok is False:
            # Real failure OR a concurrent installer won the lock and soft-skipped
            # us. Resolve from reality so a race doesn't log a false failure.
            # The loser's instantaneous recheck races the
            # winner's multi-second install window -- an ambiguous result must
            # NOT arm the sticky marker (the installer itself arms it at the
            # exact sites that can classify a failure as permanent).
            if _measure_facade._daemon_service_installed(system):
                _measure_facade._clear_daemon_install_failed_marker()
                return "installed"
            return "install-failed"
        # Verified success supersedes any failure record.
        _measure_facade._clear_daemon_install_failed_marker()
        return "installed"
    except (Exception, SystemExit) as _e:
        try:
            print(
                "[Token Optimizer] dashboard daemon self-install failed: "
                f"{str(_e).splitlines()[0] if str(_e) else type(_e).__name__}",
                file=_measure_facade.sys.stderr,
            )
        except Exception:
            pass
        # A raised exception is UNCLASSIFIED -- OSError from a full disk, an
        # AV-locked file, a gui-domain bootstrap over SSH. All recoverable by
        # tomorrow; the throttle bounds them. Definitive classes are armed
        # inside the installer where they can actually be identified.
        return "install-failed"


def _daemon_served_version(port=None, total_timeout=None):
    """Return the version string OUR live daemon reports on /api/health, or None
    if it can't be determined (no daemon, no HTTP, a foreign listener, an old
    daemon without the version field, or timeout). Bounded total wait; never
    raises.

    Used to verify a restart actually LANDED rather than trusting the service
    manager's exit code -- an orphan we couldn't reap, or a job that failed to
    bind, leaves the OLD version serving even though kickstart returned 0. A
    None result means "unknown" and callers MUST treat it as a safe degrade
    (assume restarted), never as proof of staleness: a pre-fix orphan has no
    version field, and the reaper already kills those regardless.

    IDENTITY: only trusts the response when `server == "token-optimizer-daemon"`,
    mirroring _verify_daemon_port's magic-string check, so a foreign listener on
    the port that happens to return JSON with a "version" key is ignored.

    BUDGET: default wait is short (TOKEN_OPTIMIZER_DAEMON_VERIFY_TIMEOUT, 2.0s)
    because this runs inside the SessionStart hook. A slow-cold-start install
    (large trends DB) can exceed it, in which case the probe returns None and
    the caller safe-degrades -- the reap already did the real work; the probe is
    belt-and-suspenders. Operators who want a stronger landing check can raise
    the env knob.
    """
    _measure_facade = _compat.facade()
    if port is None:
        port = _measure_facade.DAEMON_PORT
    if total_timeout is None:
        total_timeout = _measure_facade._float_env("TOKEN_OPTIMIZER_DAEMON_VERIFY_TIMEOUT", 2.0)
    import urllib.request
    deadline = _measure_facade.time.time() + total_timeout
    while _measure_facade.time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1
            ) as resp:
                body = resp.read(2048).decode("utf-8", "replace")
            data = _measure_facade.json.loads(body)
            if data.get("server") != "token-optimizer-daemon":
                # A foreign listener on our port -> not ours; unknown, degrade.
                return None
            ver = data.get("version")
            if ver:
                return str(ver)
            # Reachable OUR daemon but no version field -> old (pre-fix) code.
            return None
        except Exception:
            _measure_facade.time.sleep(0.2)
            continue
    return None


def _daemon_midsession_pulse():
    """Per-turn liveness pulse (UserPromptSubmit) that revives a dashboard daemon
    which died MID-session.

    `_ensure_dashboard_daemon` self-heals only at SessionStart, so a daemon that
    dies during a long session stays dead until the next session begins. This runs
    once per user turn with its OWN short cadence, reuses the same tombstone-safe /
    opt-out-respecting primitive, and NEVER blocks the turn:

      * Probe throttle (60s): within the window the whole pulse is one config read.
        No stat, no socket -- the per-turn hot-path cost is ~a small JSON read.
      * Once per window: a sleepless 1s port probe. Alive -> done.
      * Dead AND revive throttle (300s) open: spawn a DETACHED `daemon-revive`
        subprocess (fire-and-forget) so kickstart + landing-verification run OFF
        the hot path. The next probe (<=60s later) confirms it came back.
        FIX-SPEC-DAEMON req 2: the 300s revive throttle is SKIPPED when the
        scheduler registration (plist/unit/task) is ABSENT -- a missing plist is
        the "something removed it" state, not the transient "port not up yet"
        case the throttle bounds, so a swept daemon self-revives promptly. The
        throttle still applies to the ordinary dead-but-INSTALLED case.

    Concurrency: the throttle timestamps live in the shared config.json (machine-
    wide, not per-session), so cadence is bounded across parallel sessions. A rare
    two-session race just spawns two detached revives, which are idempotent
    (`kickstart -k`) and self-serialise on `_daemon_install_lock` inside the
    installer. Returns a short status string. Never raises, never blocks.
    """
    _measure_facade = _compat.facade()
    try:
        if _measure_facade._remote_lean_profile():
            return "noop-remote"
        # Frozen sandbox flag only: this pulse runs on every UserPromptSubmit.
        # The "job owned by another install" check reads the scheduler (on
        # Windows a `schtasks /Query`, up to 10 s when Task Scheduler is
        # wedged), so it lives in the writers that would actually re-point a
        # job (_install_*, _restart_dashboard_daemon, the heals, setup_daemon),
        # never on a per-prompt path.
        if _measure_facade._daemon_snapshot_sandboxed():
            return "noop-sandbox"
        # SAFETY gates run EVERY turn, BEFORE the probe throttle: a
        # disabled/uninstalled/thrashing daemon must NEVER be revived, not even on
        # the 59/60 throttled turns. All below are cheap (a stat + a small config
        # read + platform.system(); no scheduler query); detect_runtime is paid
        # by quality-cache regardless.
        if _measure_facade._is_foreign_runtime() or _measure_facade.detect_runtime() != "claude":
            return "noop-foreign"
        # Filesystem tombstone is AUTHORITATIVE and independent of config.json: a
        # corrupt config would make `daemon_disabled` read False and defeat the
        # opt-out (CORR-1). The breadcrumb is present after uninstall (size 0) or
        # during a thrash back-off (>0); either way, do NOT revive.
        try:
            if _measure_facade.os.path.exists(str(_measure_facade.DAEMON_THRASH_BREADCRUMB)):
                return "noop-tombstoned"
        except OSError:
            pass
        if _measure_facade._read_config_flag("daemon_disabled", False):
            return "noop-disabled"
        # Sticky install-failed marker. Checked here, alongside the
        # tombstone and BEFORE the probe throttle, so a permanently-broken
        # install stops costing a revive spawn (and on Windows a cmd-window
        # flash) every ~5min for the rest of time. Cleared only by an explicit
        # successful `setup-daemon`.
        if _measure_facade._daemon_install_failed_marker_present():
            return "noop-install-failed"
        if _measure_facade._normalized_platform() not in ("Darwin", "Linux", "Windows"):
            return "noop-unsupported"
        # Probe throttle: within the window the remaining cost is one config read.
        now = _measure_facade.time.time()
        last_probe = _measure_facade._read_config_flag("last_daemon_midsession_probe", 0)
        try:
            if now - float(last_probe or 0) < _measure_facade._daemon_pulse_probe_seconds():
                return "pulse-throttled"
        except (TypeError, ValueError):
            pass
        # Stamp BEFORE the socket work so a slow/hung probe can't let the next turn
        # through the throttle and pile up socket checks.
        _measure_facade._write_config_flag("last_daemon_midsession_probe", int(now))
        # Cheap sleepless liveness probe (identity-checked inside _verify_daemon_port).
        if _measure_facade._verify_daemon_port(timeout_seconds=1, retries=1, retry_sleep=0):
            return "noop-healthy"
        # Dead. A missing scheduler registration (launchd plist / systemd unit /
        # scheduled task absent) is a clear "something removed it" state -- distinct
        # from the transient "port not up yet" case the 300s revive throttle is meant
        # to bound (FIX-SPEC-DAEMON req 2). Bypass the revive throttle for the
        # plist-absent case so a swept daemon self-revives promptly mid-session; keep
        # the throttle for the ordinary dead-but-installed case. Fail-open: a probe
        # error degrades to "installed" so the conservative throttle still applies
        # (never bypass on uncertainty, never raise into the hot path).
        try:
            _service_installed = _measure_facade._daemon_service_installed(_measure_facade._normalized_platform())
        except Exception:  # noqa: BLE001 -- protection must never raise
            _service_installed = True
        if _service_installed:
            last_revive = _measure_facade._read_config_flag("last_daemon_midsession_revive", 0)
            try:
                if now - float(last_revive or 0) < _measure_facade._daemon_pulse_revive_seconds():
                    return "revive-throttled"
            except (TypeError, ValueError):
                pass
        _measure_facade._write_config_flag("last_daemon_midsession_revive", int(now))
        # Detached fire-and-forget revive (or first-time install) -- the user's turn
        # must NEVER block on kickstart + landing verification. POSIX detaches via
        # start_new_session; Windows ignores that, so it needs creationflags
        # (DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP + CREATE_BREAKAWAY_FROM_JOB)
        # or the child dies with the hook's job object (CXP-1). The child runs
        # `daemon-revive`, which calls _ensure_dashboard_daemon(force=True).
        # Routed through spawn_detached (single source of truth) so the
        # CREATE_BREAKAWAY_FROM_JOB retry path is included at the exact CXP-1
        # site it was built for.
        # pythonw.exe on Windows (GUI subsystem) in addition to the
        # detach flags -- the revive child is stdio-DEVNULL fire-and-forget.
        _proc = _measure_facade.spawn_detached(
            [_measure_facade._detached_python_exe(), str(_measure_facade.MEASURE_PY_PATH), "daemon-revive"],
            stdout=_measure_facade.subprocess.DEVNULL, stderr=_measure_facade.subprocess.DEVNULL,
            stdin=_measure_facade.subprocess.DEVNULL)
        if _proc is None:
            _measure_facade._log_spawn_failure("daemon-revive spawn failed")
            # A failed spawn is the
            # LEAST structural failure in the set -- fork EAGAIN under load,
            # AV transiently locking the exe, a corrupt-but-replaceable
            # pythonw twin. The 300s revive throttle already bounds the retry
            # cost, and a failed CreateProcess never painted a window anyway.
            # Never arm the sticky marker from here.
            return "revive-spawn-failed"
        return "revive-spawned"
    except Exception:
        return "pulse-error"


def _windows_action_is_console_flasher(command):
    """True when a Scheduled Task ``<Command>`` is OUR console-subsystem
    action shape (and therefore safe to rewrite).

    Conservative on BOTH sides on purpose. An unrecognised command (a venv
    shim, a user-edited action, a wrapper we do not ship) returns False: we only
    rewrite actions we can positively identify as ours-and-broken, because the
    repair re-registers the whole task. "Positively
    identify" now means it -- a ``.cmd``/``.bat`` matches ONLY when its
    basename is the one launcher we actually generate
    (``WINDOWS_LAUNCHER_NAME``), not any batch file that happens to sit in our
    task slot. And ``pythonw.exe``/``pyw.exe`` return False so the heal is
    idempotent by construction -- once migrated, the same check reports "not
    broken" and nothing runs.

    The value arrives from ``schtasks /Query /XML``, so
    entities are unescaped first (``&quot;C:\\...\\dashboard-launcher.cmd&quot;``
    must classify the same as its unquoted form) -- consistent with
    ``_windows_action_is_dead_path``.
    """
    _measure_facade = _compat.facade()
    if not command:
        return False
    try:
        from xml.sax.saxutils import unescape as _xml_unescape
        # Default unescape covers only &amp;/&lt;/&gt; -- the quote entities
        # schtasks emits for a quoted <Command> need the extra dict.
        cleaned = _xml_unescape(
            str(command), {"&quot;": '"', "&apos;": "'"}
        ).strip().strip('"').strip()
    except Exception:
        cleaned = str(command).strip().strip('"').strip()
    name = cleaned.replace("/", "\\").rsplit("\\", 1)[-1].strip().strip('"').lower()
    if name.endswith(".cmd") or name.endswith(".bat"):
        return name == _measure_facade.WINDOWS_LAUNCHER_NAME.lower()
    return name in _measure_facade._WINDOWS_CONSOLE_ACTION_EXES


def _windows_task_action_info(task_name=None):
    """Return ``(command, enabled)`` for the registered task, or ``(None, None)``
    if the task does not exist / cannot be read.

    ``command`` is the task's ``<Command>``. A None command means "no task" and
    every caller MUST treat it as "do nothing" -- never as "install one".
    Healing an absent task would resurrect a daemon the user declined or
    uninstalled.

    ``enabled`` is the SETTINGS-level ``<Enabled>`` from the same XML (the
    trigger-level ``<Enabled>`` elements live outside ``<Settings>`` and are
    ignored): False when the user disabled the task (Task Scheduler UI
    right-click Disable, or ``schtasks /Change /DISABLE``), True when
    explicitly enabled, None when the XML carries no Settings-level flag.
    A disabled task must be treated exactly like a
    tombstone -- pre-fix that opt-out was durable, and the heal must not
    re-register it ``<Enabled>true</Enabled>``.

    ``schtasks /Query /XML`` emits UTF-16, which text-mode decoding turns into
    ASCII interleaved with NULs; stripping NULs recovers the payload under both
    that and a plain UTF-8 console.
    """
    _measure_facade = _compat.facade()
    try:
        query = _measure_facade.subprocess.run(
            ["schtasks", "/Query", "/TN", task_name or _measure_facade.WINDOWS_TASK_NAME,
             "/XML", "ONE"],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=_measure_facade._NO_WINDOW,
        )
    except (OSError, _measure_facade.subprocess.SubprocessError):
        return None, None
    if query.returncode != 0:
        return None, None
    raw = (query.stdout or "").replace("\x00", "")
    match = _measure_facade.re.search(r"<Command>(.*?)</Command>", raw, _measure_facade.re.S | _measure_facade.re.I)
    if not match:
        return None, None
    command = match.group(1).strip()
    enabled = None
    settings_match = _measure_facade.re.search(r"<Settings>(.*?)</Settings>", raw, _measure_facade.re.S | _measure_facade.re.I)
    if settings_match:
        en = _measure_facade.re.search(r"<Enabled>\s*(true|false)\s*</Enabled>",
                       settings_match.group(1), _measure_facade.re.I)
        if en:
            enabled = en.group(1).lower() == "true"
    return command, enabled


def _windows_task_action_command(task_name=None):
    """Back-compat shim over ``_windows_task_action_info`` (command only)."""
    _measure_facade = _compat.facade()
    return _measure_facade._windows_task_action_info(task_name)[0]


def _windows_action_path_exists(path):
    """``os.path.exists`` behind a seam so tests can stub filesystem reality
    for Windows-shaped paths that cannot exist on the test host. Never raises."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade.os.path.exists(path)
    except (OSError, ValueError):
        return False


def _windows_action_is_dead_path(command):
    """True when the task ``<Command>`` is an absolute Windows path that no
    longer exists on disk.

    The preferred task action bakes the ABSOLUTE path of
    the pythonw twin that ran the install. Versioned python.org installs and
    deletable venvs make that path go dead on upgrade/uninstall -- and a dead
    pythonw path is invisible to ``_windows_action_is_console_flasher`` (it
    classifies the NAME, not liveness), so without this check no heal layer
    ever repairs it and the daemon is permanently dead behind green
    diagnostics.

    Only absolute (drive-letter or UNC) paths are judged; a bare PATH-resolved
    name ("pythonw.exe") cannot be classified dead from here. The XML value is
    unescaped and unquoted first so ``&amp;`` / quoted paths round-trip.
    Never raises.
    """
    _measure_facade = _compat.facade()
    if not command:
        return False
    try:
        from xml.sax.saxutils import unescape as _xml_unescape
        cmd = _xml_unescape(
            str(command), {"&quot;": '"', "&apos;": "'"}
        ).strip().strip('"').strip()
        if not cmd:
            return False
        if not (_measure_facade.re.match(r"^[A-Za-z]:[\\/]", cmd) or cmd.startswith("\\\\")):
            return False
        return not _measure_facade._windows_action_path_exists(cmd)
    except Exception:
        return False


def _daemon_resurrection_blocked():
    """THE shared refusal gate for every daemon heal/revive path.

    The individual heal entry points each grew their own
    subset of these checks, and the ones that forgot (the second
    ``_heal_windows_task_action`` call site inside ``_restart_dashboard_daemon``,
    the ensure-health auto-update block) became resurrection doors for daemons
    the user turned off. One helper, checked by every path that could
    (re)start a daemon, so no future call site can bypass a gate by accident.

    Returns the blocking reason (``"sandbox"`` | ``"tombstoned"`` |
    ``"disabled"`` | ``"install-failed"``) or None when the action may proceed. The tombstone
    stat fails open (matching ``_daemon_install_failed_marker_present``: an
    unreadable state dir is not evidence of intent). Never raises.

    Reads only the frozen sandbox flag, never the scheduler: this gate sits on
    hot hook paths. A job owned by another install is refused by the writers
    themselves (_scheduler_install_refused), and doctor reports it on its own.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return "sandbox"
    try:
        if _measure_facade.os.path.exists(str(_measure_facade.DAEMON_THRASH_BREADCRUMB)):
            return "tombstoned"
    except OSError:
        pass
    if _measure_facade._read_config_flag("daemon_disabled", False):
        return "disabled"
    if _measure_facade._daemon_install_failed_marker_present():
        return "install-failed"
    return None


def _heal_windows_task_action(stop_first=None, throttle=None):
    """One-shot migration: lift an already-registered Scheduled Task
    off a console action (the .cmd launcher, or a bare py.exe/python.exe) onto
    the GUI-subsystem pythonw.exe action.

    Without this, only NEW installs get the fix. Every existing Windows user
    keeps the console action forever, because the auto-update path refreshes
    ``dashboard-server.py`` and then merely ``/End`` + ``/Run``s the task that is
    already registered -- it never re-writes the task definition.

    Deliberately narrow, because re-registering a scheduled task is not free:
      * Windows only, and only when a usable pythonw twin actually exists --
        there is nothing better to migrate TO otherwise.
      * Only when the task EXISTS and its action is positively identified as a
        console flasher. An absent task is left absent.
      * Re-registers through ``_install_task_scheduler_daemon``, the same code
        path a fresh install uses, so heal and install can never drift.
      * Preserves the user's persisted bind host (``persist=False`` reads the
        env > file > default chain WITHOUT writing), so a migration cannot
        silently move a deliberately network-exposed daemon back to loopback.

    ``stop_first``: ``_install_task_scheduler_daemon`` refuses to install while
    something holds DAEMON_PORT, and a running daemon is exactly that. Callers
    that have already stopped the task (``_restart_dashboard_daemon``, between
    its /End and /Run) pass False. The ensure-health entry point passes True.
    On failure the restoring ``/Run`` fires from a ``finally`` (an
    installer that RAISES must not strand the daemon) -- but ONLY when the
    still-registered action is neither a console flasher nor a dead path
    (/Run-ing a .cmd action IS one extra console flash per session,
    the exact console flash the fix set out to remove; the LogonTrigger revives the daemon
    at next logon instead).

    ``throttle``: the ensure-health path passes True so a persistently-failing
    heal attempts at most once per ensure-throttle window (24h) instead of
    /End-ing the live daemon + sleeping 2s on every SessionStart. The
    restart-path caller passes False -- it is already bounded by its own
    throttles and runs with the port freed.

    Returns True only when the re-registration succeeded. Never raises.
    """
    # Per-account scheduler job: never touched from a redirected HOME
    # (a test sandbox or scratch run would re-point the user's live job).
    # Frozen flag here; the job check (a `schtasks /Query`) waits until the
    # cheap gates below say a heal is actually due.
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return False
    try:
        if _measure_facade.os.name != "nt":
            return False
        # UNIFIED GATE: the refusal gates live HERE,
        # inside the primitive, so BOTH call sites (ensure-health heal and
        # _restart_dashboard_daemon's /End->/Run window) and any future one
        # are gated by construction.
        if _measure_facade._daemon_resurrection_blocked() is not None:
            return False
        command, task_enabled = _measure_facade._windows_task_action_info()
        if command is None:
            return False  # no task -> nothing to heal, and nothing to create
        if task_enabled is False:
            # The user DISABLED our task -- the natural Windows
            # "off without uninstalling". Same posture as the tombstone:
            # leave it alone; never re-register it <Enabled>true</Enabled>.
            return False
        flasher = _measure_facade._windows_action_is_console_flasher(command)
        dead = _measure_facade._windows_action_is_dead_path(command)
        if not flasher and not dead:
            return False  # already hidden and alive; idempotent no-op
        if flasher and not dead and not _measure_facade._windows_gui_python():
            return False  # nothing better to migrate TO (a DEAD action is
            # always worth re-registering: the .cmd fallback beats a daemon
            # that can never start again)
        # A heal is due. Refuse it when the registered task belongs to
        # another install (re-registering would re-point it at this one).
        if _measure_facade._scheduler_install_refused():
            return False
        if throttle:
            # A heal that keeps failing (policy-blocked /Create,
            # foreign process on the port) must not bounce the daemon every
            # SessionStart. Stamp BEFORE the attempt, mirroring the ensure
            # throttle; a SUCCESSFUL heal makes the next classify a no-op
            # above, so the stamp never delays healthy behaviour.
            try:
                last = _measure_facade._read_config_flag("last_daemon_flash_heal_attempt", 0)
                if (_measure_facade.time.time() - float(last or 0)
                        < _measure_facade._daemon_ensure_throttle_seconds()):
                    return False
            except (TypeError, ValueError):
                pass
            try:
                _measure_facade._write_config_flag(
                    "last_daemon_flash_heal_attempt", int(_measure_facade.time.time()))
            except Exception:
                pass
        if stop_first:
            try:
                _measure_facade.subprocess.run(
                    ["schtasks", "/End", "/TN", _measure_facade.WINDOWS_TASK_NAME],
                    capture_output=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
                )
                _measure_facade.time.sleep(2)  # let the daemon release DAEMON_PORT
            except (OSError, _measure_facade.subprocess.SubprocessError):
                pass
        ok = False
        try:
            host = _measure_facade._persist_dashboard_host(persist=False)
            ok = bool(_measure_facade._install_task_scheduler_daemon(
                soft_fail=True, effective_host=host))
        finally:
            if not ok and stop_first:
                # We stopped a working daemon and failed to re-register it
                # (returned False OR raised -- hence the finally).
                # Put it back ONLY if firing it cannot flash: re-read the
                # action and refuse to /Run a still-flasher or dead command.
                try:
                    _current, _en = _measure_facade._windows_task_action_info()
                    if (_current is not None
                            and not _measure_facade._windows_action_is_console_flasher(_current)
                            and not _measure_facade._windows_action_is_dead_path(_current)):
                        _measure_facade.subprocess.run(
                            ["schtasks", "/Run", "/TN", _measure_facade.WINDOWS_TASK_NAME],
                            capture_output=True, timeout=5,
                            creationflags=_measure_facade._NO_WINDOW,
                        )
                except (OSError, _measure_facade.subprocess.SubprocessError):
                    pass
        return ok
    except Exception:
        return False


def _legacy_daemon_dir():
    """Pre-plugin-data-migration daemon dir (the auto-update block writes the
    refreshed ``dashboard-server.py`` here too). Behind a helper so tests can
    pin it into a sandbox -- RUNTIME_DIR is NOT covered by the
    TOKEN_OPTIMIZER_SNAPSHOT_DIR override."""
    _measure_facade = _compat.facade()
    return _measure_facade.RUNTIME_DIR / "_backups" / "token-optimizer"


def _heal_one_launcher_shim(launcher, daemon_script):
    """Rewrite ONE existing launcher shim to today's generated content.

    Shared mechanics for both shim locations: byte-compare, link-safety
    refusal, atomic mkstemp+os.replace. Returns True when the file was
    rewritten. Never raises past its caller's guard.
    """
    _measure_facade = _compat.facade()
    if not launcher.is_file():
        return False
    # BYTES on both sides. The shim is explicitly CRLF, and text-mode I/O
    # rewrites line endings in both directions (universal-newlines on read,
    # os.linesep on write), so a text comparison would never match and this
    # would rewrite the file on every single ensure-health.
    desired = _measure_facade._generate_windows_launcher_cmd(
        str(daemon_script), str(_measure_facade.DAEMON_LOG_DIR)).encode("utf-8")
    if launcher.read_bytes() == desired:
        return False
    # Link-safety: refuse to write through a symlink or a
    # hardlinked name -- a planted link would turn this rewrite into an
    # arbitrary-file overwrite with known content (Windows hardlinks need
    # no privilege at all).
    if launcher.is_symlink():
        return False
    if _measure_facade.os.lstat(str(launcher)).st_nlink > 1:
        return False
    # The regenerated ladder redirects into DAEMON_LOG_DIR; a legacy-era shim
    # may predate that dir, and a missing redirect target fails the rung.
    try:
        _measure_facade.DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Atomic replace (the _heal_keepwarm_plist_path pattern): a crash
    # mid-write must not leave a truncated .cmd that Task Scheduler would
    # still execute, and cmd.exe must never resume reading a half-written
    # batch file at its saved byte offset.
    fd, tmp = _measure_facade.tempfile.mkstemp(prefix=".dashboard-launcher.",
                               suffix=".tmp", dir=str(launcher.parent))
    try:
        with _measure_facade.os.fdopen(fd, "wb") as f:
            f.write(desired)
        _measure_facade.os.replace(tmp, str(launcher))
        tmp = None
    finally:
        if tmp is not None:
            try:
                _measure_facade.os.unlink(tmp)
            except OSError:
                pass
    return True


def _heal_windows_launcher_shim():
    """Rewrite an on-disk ``dashboard-launcher.cmd`` left by an older build.

    The shim generated before the no-flash fix tried console ``py.exe -3`` FIRST and probed
    with three ``where.exe`` calls, so on any host with the Python Launcher
    installed the long-lived daemon itself ran as a console process. Even where
    the shim is only the fallback action, an installed copy keeps that shape
    until something rewrites it.

    BOTH shim locations are healed -- the plugin-data
    SNAPSHOT_DIR and the legacy pre-migration dir
    (``~/.claude/_backups/token-optimizer``). The auto-update block already
    knows about both paths for ``dashboard-server.py``; users whose Scheduled
    Task action still points at the LEGACY .cmd would otherwise keep the
    console-first ladder forever. Each shim is regenerated against its own
    directory's daemon script so the legacy action keeps launching the legacy
    (auto-update-refreshed) script.

    Only touches files that ALREADY EXIST (never creates one -- that would be
    half-installing a daemon the user may have declined), and only when the
    content differs from what we would generate today, which makes it a no-op
    on the second run. Returns True when anything was rewritten. Never raises.
    """
    _measure_facade = _compat.facade()
    healed_any = False
    if _measure_facade.os.name != "nt":
        return False
    for base_dir in (_measure_facade.SNAPSHOT_DIR, _measure_facade._legacy_daemon_dir()):
        try:
            launcher = base_dir / _measure_facade.WINDOWS_LAUNCHER_NAME
            daemon_script = base_dir / "dashboard-server.py"
            if _measure_facade._heal_one_launcher_shim(launcher, daemon_script):
                healed_any = True
        except OSError:
            continue
        except Exception:
            continue
    return healed_any


def _heal_windows_console_flash():
    """Runtime self-heal: repair already-installed Windows artifacts
    that flash a console window.

    A generator fix only helps the NEXT install. The reporter already has the
    flashing task and shim on disk, and nothing in the update path rewrites
    either -- the auto-update block refreshes ``dashboard-server.py`` and
    restarts the task it finds. This is the migration that reaches the installed
    base, and it is modelled on ``_heal_keepwarm_plist_path`` /
    ``_migrate_statusline_to_stable_path``: best-effort, fail-open, called once
    per ensure-health.

    Repairs, in order:
      (a) the on-disk ``dashboard-launcher.cmd`` shim (console-first interpreter
          ladder + where.exe probes), and
      (b) the Scheduled Task action, onto hidden pythonw.exe -- via the same
          generation code a fresh install uses, so heal and install cannot drift.

    Refusal gates, checked FIRST, so a heal can never resurrect something the
    user turned off:
      * not Windows -> strict no-op (macOS/Linux never reach schtasks);
      * ``_daemon_resurrection_blocked()`` -- THE shared refusal gate
        (uninstall/thrash tombstone, sticky ``daemon_disabled`` opt-out,
        sticky ``.daemon-install-failed`` marker). The task heal re-checks the
        same gate internally (plus the task's own user-set ``<Enabled>``
        state), so its other call sites are equally safe.

    IDEMPOTENT: both steps compare against the value they would write and return
    False when it already matches, so a second run does nothing and the printed
    line appears exactly once. The task heal is additionally THROTTLED
    (``throttle=True``): a persistently-failing re-registration attempts at
    most once per ensure-throttle window instead of bouncing the live daemon
    on every SessionStart.

    Returns True when anything was actually repaired. Never raises.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.os.name != "nt":
        return False
    if _measure_facade._daemon_resurrection_blocked() is not None:
        return False
    healed_shim = _measure_facade._heal_windows_launcher_shim()
    healed_task = False
    # Reading the task's action costs a `schtasks /Query`. A healthy install
    # needs no heal, so look at most once per ensure-throttle window instead of
    # on every SessionStart. Stamped before the read, like the heal throttle.
    try:
        last = float(_measure_facade._read_config_flag("last_windows_task_action_check", 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    if _measure_facade.time.time() - last >= _measure_facade._daemon_ensure_throttle_seconds():
        try:
            _measure_facade._write_config_flag("last_windows_task_action_check", int(_measure_facade.time.time()))
        except Exception:
            pass
        healed_task = _measure_facade._heal_windows_task_action(stop_first=True, throttle=True)
    return bool(healed_shim or healed_task)


def _restart_dashboard_daemon(system):
    """Restart a dashboard daemon via its service manager, reaping any orphaned
    port-holder first and verifying the restart actually landed.

    Factored out of run_ensure_health's auto-update block so the ensure path can
    reuse the exact same reap/kickstart/restart/Run calls. Returns 'restarted',
    'restart-stale' (service restart ran but the OLD version is still serving),
    or 'restart-failed'. Never raises.

    WHY REAP FIRST: `launchctl kickstart -k` only restarts the launchd job's OWN
    child. An orphaned dashboard-server.py -- from a prior label, a manual
    launch, or ANOTHER runtime (Codex/OpenCode) on the same port -- survives the
    restart and keeps binding DAEMON_PORT, serving the dashboard from its old
    hardcoded measure.py path (the silent version-skew drift). Reaping the port
    holder BEFORE restart frees the port so the service manager's fresh child
    can bind. The reaper only SIGTERMs our own dashboard-server.py and never a
    foreign process, so this is safe.

    IDEMPOTENCY / RACE GUARD: if the port is ALREADY serving the current version
    -- e.g. a sibling SessionStart just restarted it -- do nothing and report
    success. Without this, two near-simultaneous sessions race: session B's reap
    would SIGTERM session A's freshly-bound correct daemon, causing a restart
    flap. Checking the served version first makes the whole restart idempotent.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return "noop-sandbox"
    try:
        # Already current (a sibling session fixed it)? Do not reap/restart,
        # and do not pay for the job check below (`schtasks /Query` on
        # Windows): nothing is about to be written.
        if _measure_facade._daemon_served_version() == _measure_facade.TOKEN_OPTIMIZER_VERSION:
            return "restarted"
        # About to reap and restart: refuse when the job is another install's.
        if _measure_facade._scheduler_install_refused():
            return "noop-sandbox"

        # Reap our own orphaned port-holder first (never a foreign process).
        try:
            _measure_facade._reclaim_posix_daemon_port()
        except Exception:
            pass

        _run_result = None  # Windows-only landing signal (schtasks /Run rc)
        if system == "Darwin":
            uid = _measure_facade.subprocess.run(
                ["id", "-u"], capture_output=True, text=True, creationflags=_measure_facade._NO_WINDOW
            ).stdout.strip()
            _measure_facade.subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{_measure_facade.DAEMON_LABEL}"],
                capture_output=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
            )
        elif system == "Linux":
            _measure_facade.subprocess.run(
                ["systemctl", "--user", "restart", _measure_facade.SYSTEMD_UNIT_NAME],
                capture_output=True, timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
        elif system == "Windows":
            _measure_facade.subprocess.run(
                ["schtasks", "/End", "/TN", _measure_facade.WINDOWS_TASK_NAME],
                capture_output=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
            )
            _measure_facade.time.sleep(2)
            # An install registered by an older build still has the
            # .cmd launcher as its task ACTION, and End/Run just re-runs whatever
            # is registered -- so the console flash would survive every update
            # for the entire installed base. Migrate the action here, between
            # /End and /Run: the task is stopped, so the port is free and the
            # re-registration's port-owner pre-check can pass.
            _measure_facade._heal_windows_task_action()
            _run_result = _measure_facade.subprocess.run(
                ["schtasks", "/Run", "/TN", _measure_facade.WINDOWS_TASK_NAME],
                capture_output=True, timeout=5, creationflags=_measure_facade._NO_WINDOW,
            )
        else:
            return "restart-failed"

        # Verify the restart LANDED: the process now serving DAEMON_PORT must
        # report the CURRENT version. None (unknown -- no HTTP, or a pre-fix
        # orphan without the version field) is a SAFE DEGRADE to 'restarted',
        # never a false 'restart-stale'; the reap above already handles those.
        served = _measure_facade._daemon_served_version()
        if served is not None and served != _measure_facade.TOKEN_OPTIMIZER_VERSION:
            return "restart-stale"
        # A nonzero `schtasks /Run` with nothing
        # serving the port is a DEMONSTRABLY failed restart (e.g. a disabled
        # task, or an action whose baked interpreter path went dead). The
        # None-version safe-degrade above must not convert that into a false
        # "restarted" -- that is how a permanently-dead daemon stayed green.
        if (_run_result is not None
                and getattr(_run_result, "returncode", 0) != 0
                and served is None
                and not _measure_facade._verify_daemon_port(
                    timeout_seconds=1, retries=1, retry_sleep=0)):
            return "restart-failed"
        return "restarted"
    except Exception:
        return "restart-failed"


def _apply_daemon_restart_outcome(restart_status):
    """Apply escalation + resolve the honest log message for a restart result.

    Extracted from run_ensure_health's auto-update block so the escalation
    decision is unit-testable. Returns a (level, message) tuple; the caller logs
    `message` to stdout for the success levels ('ok', 'ok-reinstall') and to
    stderr otherwise ('stale', 'failed'). Never raises.

    On 'restart-stale' the service restart ran but the OLD version is still
    serving the port -- an orphan the service manager couldn't reap. Escalate by
    killing the port holder DIRECTLY, THEN forcing a clean reinstall. Reaping
    first is essential: an alive-but-wrong-version orphan makes the port read as
    healthy, so _ensure_dashboard_daemon(force=True) would no-op ('noop-healthy')
    without the reap.

    DO-NO-HARM GATE: only escalate when the forced reinstall can ACTUALLY run.
    _ensure_dashboard_daemon early-returns a noop under a non-Claude runtime
    (e.g. Codex, which has its own daemon), a `daemon_disabled` opt-out, or an
    unsupported platform -- and `force` bypasses only the throttle, not those
    gates. Reaping and then no-op'ing the reinstall would leave the user with NO
    daemon (worse than a stale-but-alive one). In those cases, leave the running
    daemon in place and log honestly.
    """
    _measure_facade = _compat.facade()
    if restart_status == "restarted":
        return ("ok", f"Auto-updated daemon to v{_measure_facade.TOKEN_OPTIMIZER_VERSION}")
    if restart_status == "restart-stale":
        if (_measure_facade._is_foreign_runtime()
                or _measure_facade.detect_runtime() != "claude"
                or _measure_facade._read_config_flag("daemon_disabled", False)
                or _measure_facade._normalized_platform() not in ("Darwin", "Linux", "Windows")):
            # A forced reinstall would no-op here -- do not reap a daemon we
            # cannot replace. Leave the stale-but-alive daemon running.
            return ("stale",
                    "daemon serving a stale version after auto-update; "
                    "run: measure.py setup-daemon")
        try:
            _measure_facade._reclaim_posix_daemon_port()
        except Exception:
            pass
        escalated = _measure_facade._ensure_dashboard_daemon(force=True)
        if escalated in ("installed", "restarted"):
            return ("ok-reinstall",
                    f"Auto-updated daemon to v{_measure_facade.TOKEN_OPTIMIZER_VERSION} (via reinstall)")
        return ("stale",
                f"daemon still serving a stale version after auto-update "
                f"({escalated}); run: measure.py setup-daemon")
    return ("failed", "daemon restart failed after auto-update")
