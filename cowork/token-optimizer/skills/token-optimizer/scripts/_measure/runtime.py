"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _safe_int(value):
    """Coerce to int, mapping non-finite/garbage input to 0.

    Claude-path equivalent of codex_session._safe_int: json.loads accepts the
    non-standard Infinity/NaN literals, so a corrupt or hostile transcript can
    put float("inf")/nan into usage fields where int() then raises
    OverflowError/ValueError — neither caught by the readers' OSError guards.
    """
    _measure_facade = _compat.facade()
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _windows_stdio_kwargs():
    """Usable std streams to hand a console-less child on Windows, else {}.

    With creationflags=CREATE_NO_WINDOW and no std streams, a Windows child gets
    a hidden console (python.exe) or NULL handles (pythonw.exe), so everything
    it prints is lost. Passing the parent's streams makes CPython hand the child
    the parent's own handles. Same guard as hooks/run.py (kept as a copy: this
    script must not import from hooks/). Off Windows the child inherits the
    streams anyway, so nothing is added.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.sys.platform != "win32":
        return {}
    kwargs = {}
    missing = []
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(_measure_facade.sys, name, None)
        if stream is None:
            missing.append(name)
            continue
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            missing.append(name)
            continue
        kwargs[name] = stream
    # Same rule as hooks/run.py: a stream left out falls back to a possibly
    # stale handle and the spawn fails, so an unusable one gets DEVNULL.
    if kwargs:
        for name in missing:
            kwargs[name] = _measure_facade.subprocess.DEVNULL
    return kwargs


def _run_script_child(script, extra_args=None, **run_kwargs):
    """Run a sibling script under this interpreter with its output visible."""
    _measure_facade = _compat.facade()
    return _measure_facade.subprocess.run(
        [_measure_facade.sys.executable, str(script), *extra_args],
        creationflags=_measure_facade._NO_WINDOW,
        **_measure_facade._windows_stdio_kwargs(),
        **run_kwargs,
    )


def _detached_python_exe():
    """Interpreter to use for FIRE-AND-FORGET python children.

    Belt-and-suspenders on top of the detach flags: on Windows,
    prefer the GUI-subsystem ``pythonw.exe`` twin next to ``sys.executable`` so
    the child cannot allocate a console even if a future edit drops
    ``DETACHED_PROCESS``/``CREATE_NO_WINDOW``, or a host launches us in a way we
    do not control. Falls back to ``sys.executable`` when no usable twin exists
    (MS Store alias, no twin) and is a pure no-op off Windows -- POSIX behaviour
    is byte-identical.

    ONLY for children whose stdio is DEVNULL. Do NOT use it for
    ``capture_output=True`` runs: those already suppress the window via
    ``creationflags=_NO_WINDOW``, and swapping the interpreter there would trade
    a solved problem for pythonw's None-stdio semantics.
    """
    _measure_facade = _compat.facade()
    return _measure_facade._windows_gui_python() or _measure_facade.sys.executable or "python3"


def _is_foreign_runtime() -> bool:
    """True when running under a runtime whose audit target is NOT ~/.claude."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() in _measure_facade._FOREIGN_RUNTIMES


def _copilot_audit_notice() -> None:
    """Explain why the Claude audit does not run under Copilot, and where to go."""
    _measure_facade = _compat.facade()
    print("Token Optimizer — GitHub Copilot runtime detected.")
    print()
    print("This audit targets a Claude Code / Codex setup (it scans ~/.claude), so it")
    print("will not run here and will not modify ~/.claude or your Copilot config.")
    print()
    print("On GitHub Copilot, use the Copilot-native commands instead:")
    print("  measure.py copilot-summary   — session cost/quality summary")
    print("  measure.py copilot-rollup    — collect sessions into trends")
    print("  measure.py copilot-doctor    — readiness + hook capability probe")
    print("  measure.py copilot-install   — wire Token Optimizer into ~/.copilot")
    print("  measure.py copilot-home      — print resolved Copilot home (auto-detects WSL; override via TOKEN_OPTIMIZER_COPILOT_HOME)")
    print()
    print("To force this skill onto a specific runtime, set TOKEN_OPTIMIZER_RUNTIME.")


def _cursor_audit_notice() -> None:
    """Explain why the Claude audit does not run under Cursor, and where to go."""
    _measure_facade = _compat.facade()
    print("Token Optimizer — Cursor runtime detected.")
    print()
    print("This audit targets a Claude Code / Codex setup (it scans ~/.claude), so it")
    print("will not run here and will not modify ~/.claude or your Cursor config.")
    print()
    print("On Cursor, use the Cursor-native commands instead:")
    print("  measure.py cursor-summary    — session token/quality summary")
    print("  measure.py cursor-rollup     — collect sessions into trends")
    print("  measure.py cursor-doctor     — readiness + hook firing probe")
    print("  measure.py cursor-install    — wire Token Optimizer into ~/.cursor/hooks.json")
    print("  measure.py cursor-uninstall  — remove Token Optimizer's Cursor entries")


def _grok_audit_notice() -> None:
    """Explain why the Claude audit does not run under Grok Build, and where to go."""
    _measure_facade = _compat.facade()
    print("Token Optimizer — Grok Build runtime detected.")
    print()
    print("This audit targets a Claude Code / Codex setup (it scans ~/.claude), so it")
    print("will not run here and will not modify ~/.claude or your Grok config.")
    print()
    print("On Grok Build, use the Grok-native commands instead:")
    print("  measure.py grok-summary   — session token/cost summary")
    print("  measure.py grok-rollup    — collect sessions into trends")
    print("  measure.py grok-doctor    — readiness + hook probe")
    print("  measure.py grok-install   — wire Token Optimizer into $GROK_HOME/hooks/")
    print("  measure.py grok-home      — print resolved Grok home (override via TOKEN_OPTIMIZER_GROK_HOME)")
    print()
    print("To force this skill onto a specific runtime, set TOKEN_OPTIMIZER_RUNTIME.")


def _foreign_audit_notice() -> None:
    """Route to the right per-runtime notice for blocked Claude-target commands.

    Dispatch is table-driven so adding a foreign runtime means one entry here
    plus _FOREIGN_RUNTIMES — never a fall-through to another runtime's notice.
    """
    _measure_facade = _compat.facade()
    notices = {
        "opencode": _measure_facade._opencode_audit_notice,
        "copilot": _measure_facade._copilot_audit_notice,
        "hermes": _measure_facade._hermes_audit_notice,
        "cursor": _measure_facade._cursor_audit_notice,
        "antigravity": _measure_facade._antigravity_audit_notice,
        "grok": _measure_facade._grok_audit_notice,
        "devin": _measure_facade._devin_audit_notice,
    }
    handler = notices.get(_measure_facade.detect_runtime())
    if handler is not None:
        handler()
    else:  # future foreign runtime without a notice yet: generic, never wrong
        print(f"Token Optimizer — {_measure_facade.runtime_name_for_humans()} runtime detected.")
        print("Claude-targeting commands are disabled here; they scan/modify ~/.claude.")
        print("Set TOKEN_OPTIMIZER_RUNTIME to force a specific runtime.")


def _devin_audit_notice() -> None:
    _measure_facade = _compat.facade()
    print("Token Optimizer - Devin CLI runtime detected.")
    print("Claude-targeting commands will not run here or modify ~/.claude.")
    print("Use devin-install, devin-doctor, devin-summary or devin-filter.")
    print("No native dashboard, quality, savings or provider collector is available.")


def _opencode_audit_notice() -> None:
    """Explain why the Claude audit does not run under OpenCode, and where to go.

    Printed instead of scanning/mutating ~/.claude when OpenCode is detected.
    """
    _measure_facade = _compat.facade()
    print("Token Optimizer — OpenCode runtime detected.")
    print()
    print("This audit targets a Claude Code / Codex setup (it scans ~/.claude), so it")
    print("will not run here and will not modify ~/.claude or your OpenCode config.")
    print()
    print("On OpenCode, Token Optimizer runs automatically as a plugin:")
    print("  - Live context-quality, savings, and continuity tracking via session hooks")
    print("  - Dashboard / status: run the `token_dashboard` or `token_status` tool")
    print()
    print("Install or refresh the OpenCode plugin (builds the native TS plugin")
    print("into ~/.config/opencode/plugins/, NOT the ~/.claude skill tree):")
    print("  install.sh --opencode")
    print("  or via npm:  opencode plugin token-optimizer-opencode")
    print()
    print("To force this skill onto a specific runtime, set TOKEN_OPTIMIZER_RUNTIME.")


def _hermes_audit_notice() -> None:
    """Explain why the Claude audit does not run under Hermes, and where to go.

    Printed instead of scanning/mutating ~/.claude when Hermes is detected.
    Hermes sessions live in ~/.hermes/state.db (read via the hermes_session
    adapter), not ~/.claude/projects JSONL, so the Claude-targeting scan
    commands would scan the wrong tree and produce empty output.
    """
    _measure_facade = _compat.facade()
    print("Token Optimizer — Hermes runtime detected.")
    print()
    print("This audit targets a Claude Code / Codex setup (it scans ~/.claude), so it")
    print("will not run here and will not modify ~/.claude or your Hermes config.")
    print()
    print("On Hermes, use the Hermes-native commands instead:")
    print("  measure.py hermes-summary    — session cost/quality summary")
    print("  measure.py hermes-rollup     — collect sessions into trends")
    print("  measure.py hermes-doctor     — readiness + hook capability probe")
    print("  measure.py hermes-install    — wire Token Optimizer into ~/.hermes/plugins/")
    print("  measure.py dashboard         — open the Hermes dashboard (port 24844)")
    print()
    print("To force this skill onto a specific runtime, set TOKEN_OPTIMIZER_RUNTIME.")


def _antigravity_audit_notice() -> None:
    """Explain why the Claude audit does not run under Antigravity, and where to go.

    Printed instead of scanning/mutating ~/.claude when Antigravity is detected.
    Antigravity sessions live in the read-only conversation store under
    ~/.gemini (read via the antigravity_state adapter), not ~/.claude/projects
    JSONL, so the Claude-targeting scan commands would scan the wrong tree and
    produce empty output.
    """
    _measure_facade = _compat.facade()
    print("Token Optimizer — Google Antigravity runtime detected.")
    print()
    print("This audit targets a Claude Code / Codex setup (it scans ~/.claude), so it")
    print("will not run here and will not modify ~/.claude or your Antigravity config.")
    print()
    print("On Antigravity, use the Antigravity-native commands instead:")
    print("  measure.py antigravity-summary    — session cost/quality summary")
    print("  measure.py antigravity-rollup     — collect sessions into trends")
    print("  measure.py antigravity-doctor     — readiness + hook capability probe")
    print("  measure.py antigravity-install    — wire Token Optimizer into ~/.gemini/config/plugins/")
    print("  measure.py antigravity-home       — print resolved Antigravity home (override via TOKEN_OPTIMIZER_ANTIGRAVITY_HOME)")
    print("  measure.py dashboard              — open the Antigravity dashboard (port 24847)")
    print()
    print("To force this skill onto a specific runtime, set TOKEN_OPTIMIZER_RUNTIME.")


def _windows_profile_dir():
    """The Windows account's profile dir as the OS records it, independent of
    the USERPROFILE / HOME environment variables (Path.home() reads those):
    SHGetKnownFolderPath(FOLDERID_Profile). None off Windows or on any failure,
    never a guess such as %SystemDrive%\\Users\\%USERNAME% (profiles can live
    on another drive, and a wrong guess would refuse a real user's scheduler)."""
    _measure_facade = _compat.facade()
    if _measure_facade.os.name != "nt":
        return None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        raw = uuid.UUID("{5E6C858F-0E22-4760-9AFE-EA3317B67173}").bytes_le  # FOLDERID_Profile
        guid = _GUID.from_buffer_copy(raw)
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        try:
            value = out.value if hr == 0 else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
        return _measure_facade.Path(value) if value else None
    except Exception:
        return None


def _real_account_home():
    """The OS account's own home directory, never read from HOME/USERPROFILE:
    the pwd entry for this uid on POSIX, the profile folder the OS records on
    Windows. None where it cannot be known (no pwd entry, API failure)."""
    _measure_facade = _compat.facade()
    if _measure_facade._ACCOUNT_HOME_IS_WINDOWS:
        return _measure_facade._windows_profile_dir()
    try:
        import pwd  # POSIX only
        raw = pwd.getpwuid(_measure_facade.os.getuid()).pw_dir
        return _measure_facade.Path(raw) if raw else None
    except Exception:
        return None


def _same_path(a, b):
    """True when two paths name the same directory: equal as typed (case-folded
    on Windows), equal once symlinks resolve, or the same inode. HPC/NFS homes
    often reach one directory through an automount symlink and a real mount
    path, so no single comparison is enough on its own."""
    _measure_facade = _compat.facade()
    try:
        if _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(str(a))) == _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(str(b))):
            return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        if _measure_facade.Path(a).resolve() == _measure_facade.Path(b).resolve():
            return True
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        return _measure_facade.os.path.samefile(str(a), str(b))
    except (OSError, ValueError, TypeError):
        return False


def _is_under(child, parent):
    """``child`` is ``parent`` or inside it, compared resolved and as typed."""
    _measure_facade = _compat.facade()
    try:
        _measure_facade.Path(child).resolve().relative_to(_measure_facade.Path(parent).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        pass
    try:
        _measure_facade.Path(_measure_facade.os.path.normcase(_measure_facade.os.path.abspath(str(child)))).relative_to(
            _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(str(parent))))
        return True
    except (ValueError, OSError, RuntimeError, TypeError):
        return False


def _foreign_home_override():
    _measure_facade = _compat.facade()
    return _measure_facade.os.environ.get(_measure_facade._FOREIGN_HOME_OVERRIDE_ENV, "").strip() == "1"


def _compute_home_redirected():
    """True when the home this process derives its paths from (HOME on POSIX,
    USERPROFILE on Windows, via Path.home()) is not the account's own home: a
    test sandbox, a scratch e2e run, a second HOME on one account.

    launchd / systemd --user / Task Scheduler jobs are per ACCOUNT, not per
    HOME, and TO's labels are fixed (com.token-optimizer.dashboard, ...). A
    redirected-HOME run that installs or restarts one of them re-points the
    user's LIVE job at the throwaway home (seen 2026-10-19: an e2e run's
    SessionStart re-bootstrapped the real dashboard agent to a scratch dir).

    The Claude home is NOT compared: Claude Code supports CLAUDE_CONFIG_DIR
    anywhere (claude_home()), and a real user with HOME at home and the config
    on /opt or a volume must keep their dashboard, keep-warm, restart and
    uninstall. The case that guard used to cover (real HOME, scratch
    CLAUDE_CONFIG_DIR) is caught instead by _scheduler_job_points_elsewhere():
    refuse only when an existing job belongs to a different install.

    TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER=1 is the explicit escape hatch
    for a setup that really does run the daemon from a relocated home."""
    _measure_facade = _compat.facade()
    if _measure_facade._foreign_home_override():
        return False
    real = _measure_facade._real_account_home()
    if real is None:
        return False
    try:
        return not _measure_facade._same_path(_measure_facade.HOME, real)
    except Exception:
        return False


def _daemon_snapshot_sandboxed():
    """True when this process must not touch the per-account scheduler jobs
    (dashboard daemon, keep-warm agent) AT ALL, uninstall included:

      * an explicit TOKEN_OPTIMIZER_SNAPSHOT_DIR override (tests, sandboxes);
      * HOME / USERPROFILE is not the account's real home (see
        _compute_home_redirected). Removal goes by fixed label
        (``launchctl bootout gui/<uid>/<label>``, ``schtasks /Delete /TN``,
        ``systemctl --user disable``), so a scratch-HOME "uninstall" would kill
        the user's live job just as surely as an install re-points it.

    Uninstall and status paths check only this. Every path that writes,
    reloads or restarts a job checks _scheduler_install_refused(), which adds
    the existing-job check. Both inputs are frozen at import so later env
    drift cannot separate the guard from the paths it protects.
    """
    _measure_facade = _compat.facade()
    return bool(_measure_facade._SNAPSHOT_DIR_OVERRIDE) or _measure_facade._HOME_REDIRECTED


def _scheduler_install_refused():
    """True when an install / restart / revive / repair must not run: the
    process is sandboxed (above), or, from the account's real home, a job
    already registered for this account points at a different install than
    this process (another CLAUDE_CONFIG_DIR or data dir), so writing ours would
    re-point it. A CLAUDE_CONFIG_DIR outside the home is fine on its own: with
    no job, or with a job that is this install's, nothing is refused.
    TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER=1 lifts the job check."""
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return True
    if _measure_facade._foreign_home_override():
        return False
    return bool(_measure_facade._scheduler_job_points_elsewhere())


def _reset_job_guard_cache():
    _measure_facade = _compat.facade()
    _measure_facade._WINDOWS_TASK_PATHS_CACHE.clear()


def _windows_task_job_paths_cached():
    _measure_facade = _compat.facade()
    key = (_measure_facade._windows_task_job_paths, _measure_facade.WINDOWS_TASK_NAME)
    hit = _measure_facade._WINDOWS_TASK_PATHS_CACHE.get(key)
    now = _measure_facade.time.monotonic()
    if hit and now - hit[0] < _measure_facade._WINDOWS_TASK_PATHS_TTL_S:
        return hit[1]
    paths = _measure_facade._windows_task_job_paths()
    _measure_facade._WINDOWS_TASK_PATHS_CACHE.clear()
    _measure_facade._WINDOWS_TASK_PATHS_CACHE[key] = (now, paths)
    return paths


def _this_install_roots():
    _measure_facade = _compat.facade()
    roots = []
    for value in (_measure_facade.CLAUDE_DIR, _measure_facade.RUNTIME_DIR, _measure_facade.SNAPSHOT_DIR, _measure_facade._RESOLVED_PLUGIN_DATA,
                  _measure_facade.Path(_measure_facade.__file__).resolve().parents[3]):
        if value:
            roots.append(_measure_facade.Path(value))
    return roots


def _plist_job_paths(path):
    _measure_facade = _compat.facade()
    import plistlib
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out = [a for a in (data.get("ProgramArguments") or [])[1:] if isinstance(a, str)]
    out += [data.get(k) for k in ("StandardOutPath", "StandardErrorPath", "WorkingDirectory")
            if isinstance(data.get(k), str)]
    return out


def _systemd_unit_job_paths(path):
    _measure_facade = _compat.facade()
    try:
        text = _measure_facade.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in ("ExecStart", "WorkingDirectory"):
            out.append(value.strip('"').split('" ', 1)[0].strip('"'))
        elif key in ("StandardOutput", "StandardError") and value.startswith(("append:", "file:")):
            out.append(value.split(":", 1)[1])
    return out


def _windows_task_job_paths():
    """<Command> (when it is not just an interpreter), <Arguments> tokens and
    <WorkingDirectory> of this runtime's registered task. Read-only query."""
    _measure_facade = _compat.facade()
    try:
        query = _measure_facade.subprocess.run(
            ["schtasks", "/Query", "/TN", _measure_facade.WINDOWS_TASK_NAME, "/XML", "ONE"],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=_measure_facade._NO_WINDOW,
        )
    except (OSError, _measure_facade.subprocess.SubprocessError, NameError):
        return []
    if query.returncode != 0:
        return []
    raw = (query.stdout or "").replace("\x00", "")
    out = []
    for tag in ("Command", "Arguments", "WorkingDirectory"):
        m = _measure_facade.re.search(r"<%s>(.*?)</%s>" % (tag, tag), raw, _measure_facade.re.S | _measure_facade.re.I)
        if not m:
            continue
        value = m.group(1).strip()
        if tag == "Command" and _measure_facade.re.split(r"[\\/]", value.strip('"'))[-1].lower().startswith("python"):
            continue
        out += [t.strip('"') for t in _measure_facade.re.findall(r'"[^"]*"|\S+', value)] if tag == "Arguments" \
            else [value.strip('"')]
    return [p for p in out if _measure_facade.re.match(r"^([A-Za-z]:[\\/]|[\\/])", p)]


def _scheduler_job_paths():
    """{artifact: [absolute paths it runs or writes]} for every per-account job
    this process could touch, read from where this process would write them."""
    _measure_facade = _compat.facade()
    jobs = {}
    system = _measure_facade._normalized_platform()
    if system == "Darwin":
        for plist in (_measure_facade.PLIST_PATH, _measure_facade._keepwarm_scheduler_plist_path()):
            if plist.is_file():
                jobs[str(plist)] = _measure_facade._plist_job_paths(plist)
    elif system == "Linux":
        unit = _measure_facade._systemd_user_unit_path()
        if unit.is_file():
            jobs[str(unit)] = _measure_facade._systemd_unit_job_paths(unit)
    elif system == "Windows":
        paths = _measure_facade._windows_task_job_paths_cached()
        if paths:
            jobs["task:" + _measure_facade.WINDOWS_TASK_NAME] = paths
    return jobs


def _scheduler_job_points_elsewhere():
    """The first per-account job that belongs to a different install (none of
    its paths sit under this process's Claude home, runtime home, data dirs or
    plugin root) while at least one of them still exists, as
    ``"<artifact> -> <existing path>"``; '' when there is none, when no job
    exists, when the job only points at paths that are gone (a stale job is
    repaired, not protected), or when it cannot be read (fail open)."""
    _measure_facade = _compat.facade()
    answer = ""
    try:
        roots = _measure_facade._this_install_roots()
        for artifact, paths in _measure_facade._scheduler_job_paths().items():
            paths = [p for p in paths if p]
            if not paths or any(_measure_facade._is_under(p, r) for p in paths for r in roots):
                continue
            # A job is another install's only while that install still exists:
            # at least one path it runs or writes is on disk. A dead job (account
            # renamed by Migration Assistant, CLAUDE_CONFIG_DIR moved, old data
            # dir deleted) points at nothing, and refusing to repair it would
            # leave the dashboard broken forever with nobody to own it.
            live = [p for p in paths if _measure_facade.os.path.exists(p)]
            if live:
                answer = "%s -> %s" % (artifact, live[0])
                break
    except Exception:
        answer = ""
    return answer


def _remote_lean_profile():
    """True when this process runs in a Claude Code cloud VM and should skip
    the work that cannot help there.

    A cloud session is an ephemeral Ubuntu VM (root, no status line, no browser,
    no launchd/systemd user bus, thrown away when the session is reclaimed). In
    that host TO skips: the dashboard daemon install/revive/auto-update and the
    background dashboard regen, keep-warm arm + scheduler repair, the status
    line and hook self-heal writes into ~/.claude/settings.json, the
    cleanupPeriodDays write, the script-install auto-updater, the marketplace
    auto-update tip and the first-run welcome (a fresh VM is a "first run" every
    session). The session-end flush runs inline instead of detached so the data
    lands before the VM can go away. In-session features (compression, read
    dedup, archive/expand, checkpoints, trends.db) are untouched.

    ``TOKEN_OPTIMIZER_REMOTE_LEAN=0`` turns the profile off (debugging, or a
    long-lived self-hosted VM that does have a desktop). Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_REMOTE_LEAN", "").strip().lower() in ("0", "false", "no", "off"):
            return False
        return _measure_facade.is_claude_code_cloud()
    except Exception:
        return False


def _dashboard_meta_path(html_path):
    """Tiny sidecar recording the (version, shape) a dashboard HTML was generated
    with, so the SessionStart staleness check reads a ~40-byte JSON file instead
    of scanning the 4-5MB HTML every session (the marker can sit megabytes deep
    in __TOKEN_DATA__)."""
    _measure_facade = _compat.facade()
    return _measure_facade.Path(html_path).with_suffix(".meta.json")


def _parse_semver(v):
    """(major, minor, patch) ints for a version string, or None if unparseable.
    Tolerant: strips a leading 'v' and any -pre / +build suffix, pads missing
    components with 0. Used only for precedence comparison, never display."""
    _measure_facade = _compat.facade()
    try:
        core = str(v).strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
        parts = (core.split(".") + ["0", "0", "0"])[:3]
        return tuple(int(x) for x in parts)
    except (ValueError, AttributeError, TypeError):
        return None


def _write_dashboard_meta(html_path):
    """Write the (version, shape) sidecar next to a dashboard HTML we just wrote,
    so the sidecar always names the build that produced the on-disk HTML. Every
    writer of a shared dashboard MUST call this after writing, or the downgrade
    guard reads a stale version and can be bypassed. Fail-soft; never raises.

    atomic meta write: mkstemp + fchmod + os.replace, matching the HTML
    write. A plain Path.write_text truncates-then-writes in place, so a concurrent
    reader between the truncate and the final byte reads a torn/empty sidecar, and
    an interleave of two writers can leave an (html_new, meta_old) pairing that
    lets an OLDER concurrent build's downgrade guard misfire and clobber a NEWER
    dashboard. os.replace makes each reader see one complete sidecar or the prior
    one, never a partial. Callers MUST write this AFTER the HTML os.replace so the
    sidecar never advertises a version whose HTML is not yet on disk."""
    _measure_facade = _compat.facade()
    _measure_facade._write_dashboard_meta_atomic(_measure_facade._dashboard_meta_path(html_path))


def _write_dashboard_meta_atomic(meta_path):
    """Atomically write the current (version, shape) sidecar to ``meta_path``.
    Shared by ``_write_dashboard_meta`` and the inline post-HTML write in
    ``generate_standalone_dashboard`` so both take the identical torn-write-proof
    path. Fail-soft; never raises (a failed sidecar must not break generation)."""
    _measure_facade = _compat.facade()
    meta_path = _measure_facade.Path(meta_path)
    blob = _measure_facade.json.dumps({"version": _measure_facade.TOKEN_OPTIMIZER_VERSION, "shape": _measure_facade._DASHBOARD_SHAPE_MARKER})
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = _measure_facade.tempfile.mkstemp(dir=str(meta_path.parent), prefix=".dashmeta-", suffix=".tmp")
        fd_owned = False  # True once fdopen's file object owns (and will close) tmp_fd
        try:
            if hasattr(_measure_facade.os, "fchmod"):  # POSIX only (os.fchmod is absent on Windows)
                _measure_facade.os.fchmod(tmp_fd, 0o600)
            with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                fd_owned = True
                f.write(blob)
            _measure_facade.os.replace(tmp_name, str(meta_path))
        except BaseException:
            # BaseException, not OSError: writers under a hook budget can raise
            # _HookTimeout mid-write; clean up the temp then re-raise so the
            # timeout still propagates (same contract as the HTML write).
            # If we failed BEFORE fdopen took ownership (e.g. fchmod raised, or a
            # budget fired between mkstemp and fdopen), the raw fd would leak;
            # close it here. Once fdopen owns it, its `with` already closed it.
            if not fd_owned:
                try:
                    _measure_facade.os.close(tmp_fd)
                except OSError:
                    pass
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        pass


def _dashboard_on_disk_is_newer(html_path):
    """True iff a REAL dashboard already on disk was written by a STRICTLY NEWER
    Token Optimizer version than this process.

    An older build must never overwrite a newer dashboard. Without this guard a
    stale long-lived daemon or a still-running pre-upgrade session regenerates
    the shared dashboard with pre-fix code and clobbers a just-shipped fix -- the
    "fixed but still broken" report, where the fix ships but an old
    in-memory process keeps overwriting the corrected file.

    Fail-OPEN by design: a missing sidecar, an unparseable/dev version on either
    side, or an absent/undersized (corrupt) HTML returns False (allow the write).
    The HTML-integrity floor is the anti-wedge: the guard only protects a REAL
    on-disk dashboard, so a lying/corrupt sidecar next to a broken HTML can never
    permanently block regeneration -- the next writer repairs it and rewrites the
    sidecar. Reads only the ~40-byte sidecar plus a cheap stat. Never raises.
    Equal versions are NOT "newer", so same-version regen and legitimate upgrades
    both proceed; only a strictly-older writer is blocked.

    Residual (documented, not auto-recovered): a *valid* newer sidecar beside a
    *valid* HTML always blocks older writers -- correct when a real newer build
    wrote it; on the rare chance the version was corrupted upward next to intact
    HTML, recovery is deleting the .meta.json sidecar."""
    _measure_facade = _compat.facade()
    try:
        mine = _measure_facade._parse_semver(_measure_facade.TOKEN_OPTIMIZER_VERSION)
        if mine is None:
            return False
        # Only guard a real, non-trivial dashboard. A missing/tiny HTML means
        # there is nothing worth protecting -- allow the write so a broken file
        # (even next to a lying sidecar) always self-heals.
        try:
            if _measure_facade.Path(html_path).stat().st_size < _measure_facade._DASHBOARD_MIN_VALID_BYTES:
                return False
        except OSError:
            return False
        meta_path = _measure_facade._dashboard_meta_path(html_path)
        if not meta_path.exists():
            return False
        meta = _measure_facade.json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return False
        existing = _measure_facade._parse_semver(meta.get("version"))
        if existing is None:
            return False
        return existing > mine
    except Exception:
        return False


def _dashboard_meta_is_fresh(meta):
    """Freshness decision for the SessionStart staleness check, factored out so it
    is directly testable. `meta` is the parsed sidecar dict. Fresh (no regen) iff:
      - the on-disk build is STRICTLY NEWER than ours (an older session must not
        regenerate/clobber a newer dashboard -- leave it to the newer build), OR
      - version AND shape marker match ours exactly.
    Anything else (older on disk, or a shape change) is stale -> regenerate."""
    _measure_facade = _compat.facade()
    if not isinstance(meta, dict):
        return False
    existing = _measure_facade._parse_semver(meta.get("version"))
    mine = _measure_facade._parse_semver(_measure_facade.TOKEN_OPTIMIZER_VERSION)
    if existing is not None and mine is not None and existing > mine:
        return True
    return (meta.get("version") == _measure_facade.TOKEN_OPTIMIZER_VERSION
            and meta.get("shape") == _measure_facade._DASHBOARD_SHAPE_MARKER)


def _use_codex_session_adapter(filepath=None):
    """True when session JSONL should be parsed with the Codex adapter."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() == "codex" or (filepath is not None and _measure_facade.codex_session.is_codex_session_path(filepath))


def _use_hermes_session_adapter():
    """True when sessions should be loaded from the Hermes state.db adapter."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() == "hermes"


def _use_copilot_session_adapter():
    """True when sessions should be loaded from the Copilot adapters."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() == "copilot"


def _use_cursor_session_adapter():
    """True when sessions should be loaded from the Cursor adapters."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() == "cursor"


def _use_antigravity_session_adapter():
    """True when sessions should be loaded from the Antigravity store adapter."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() == "antigravity"


def _use_grok_session_adapter():
    """True when sessions should be loaded from the Grok Build adapter."""
    _measure_facade = _compat.facade()
    return _measure_facade.detect_runtime() == "grok"


def _fmt_context_window(size):
    """Format context window size for display (e.g., '200K', '1M')."""
    _measure_facade = _compat.facade()
    if size >= 1_000_000:
        return f"{size / 1_000_000:.0f}M" if size % 1_000_000 == 0 else f"{size / 1_000_000:.1f}M"
    return f"{size // 1000}K"


def _strip_ansi(text):
    """Remove ANSI/VT escape sequences from text destined for the terminal."""
    _measure_facade = _compat.facade()
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    text = _measure_facade._ANSI_ESCAPE_RE.sub("", text)
    # Strip stray BEL characters left by malformed OSC sequences whose
    # terminator survived the OSC branch (embedded ESC broke the match).
    return _measure_facade._BEL_RE.sub("", text)


def _context_usage(usage):
    """Select live executor usage, leaving aggregate usage for billing.

    Advisor/server-tool responses can sum several executor reads in the
    top-level usage. Only the final message iteration describes occupancy.
    An unusable final executor falls back to the aggregate, never an older
    executor (which may describe a different context). Ignore advisor tails.
    """
    _measure_facade = _compat.facade()
    if isinstance(usage, dict) and isinstance(usage.get("iterations"), list):
        for iteration in reversed(usage["iterations"]):
            if isinstance(iteration, dict) and iteration.get("type") == "message":
                if (_measure_facade._valid_context_usage(iteration)
                        and _measure_facade._context_token_count(iteration) > 0):
                    return iteration
                break
    return usage


def _valid_context_usage(usage):
    """Reject corrupt present executor buckets before lossy int coercion.

    Missing buckets are optional. TTL splits matter only when the flat
    cache-write count is absent or zero, matching _context_token_count.
    """
    _measure_facade = _compat.facade()
    values = [usage[key] for key in ("input_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens") if key in usage]
    if not _measure_facade._safe_int(usage.get("cache_creation_input_tokens")):
        nested = usage.get("cache_creation", {})
        if not isinstance(nested, dict):
            return False
        for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
            if key in nested:
                values.append(nested[key])
            if not nested.get(key) and key in usage:
                values.append(usage[key])
    try:
        return all(_measure_facade.math.isfinite(float(value)) and float(value) >= 0
                   for value in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _resume_usage(usage):
    """The first executor tests whether the cache survived the pause.

    Later executors may read cache written earlier in this same response;
    that is not evidence that a keep-warm ping avoided the initial rewrite.
    Unusable first-executor data keeps the aggregate comparison, not a later
    executor that could hide a paid rewrite.
    """
    _measure_facade = _compat.facade()
    if isinstance(usage, dict) and isinstance(usage.get("iterations"), list):
        for iteration in usage["iterations"]:
            if isinstance(iteration, dict) and iteration.get("type") == "message":
                if (_measure_facade._valid_context_usage(iteration)
                        and _measure_facade._context_token_count(iteration) > 0):
                    return iteration
                break
    return usage


def _usage_buckets(usage):
    """Billing buckets of one usage dict (or iteration), never raising."""
    _measure_facade = _compat.facade()
    if not isinstance(usage, dict):
        return dict.fromkeys(_measure_facade._USAGE_BUCKET_KEYS, 0)
    nested = usage.get("cache_creation", {})
    if not isinstance(nested, dict):
        nested = {}
    cc_1h = _measure_facade._safe_int(nested.get("ephemeral_1h_input_tokens", 0)
                      or usage.get("ephemeral_1h_input_tokens", 0))
    cc_5m = _measure_facade._safe_int(nested.get("ephemeral_5m_input_tokens", 0)
                      or usage.get("ephemeral_5m_input_tokens", 0))
    return {
        "inp": max(0, _measure_facade._safe_int(usage.get("input_tokens", 0))),
        "out": max(0, _measure_facade._safe_int(usage.get("output_tokens", 0))),
        "cr": max(0, _measure_facade._safe_int(usage.get("cache_read_input_tokens", 0))),
        "cc": max(0, _measure_facade._safe_int(usage.get("cache_creation_input_tokens", 0))
                  or (cc_1h + cc_5m)),
        "cc_1h": max(0, cc_1h),
        "cc_5m": max(0, cc_5m),
    }


def _billed_usage_parts(usage, model, tier=None):
    """Split one response's usage into [(price_model, buckets), ...].

    The first part is the executor (priced at the response model). Advisor
    iterations (type "advisor_message") run on the advisor's own model, so
    each becomes its own part priced at that model when the rate card knows
    it, else at the executor model. Tokens are never dropped: when the
    top-level usage already covers the advisor tokens they are moved out of
    the executor part, otherwise (the documented shape, top-level is the
    executor sum only) they are added on top.
    """
    _measure_facade = _compat.facade()
    agg = _measure_facade._usage_buckets(usage)
    iterations = usage.get("iterations") if isinstance(usage, dict) else None
    if not isinstance(iterations, list):
        return [(model, agg)]
    executor_sum = dict.fromkeys(_measure_facade._USAGE_BUCKET_KEYS, 0)
    advisor_sum = dict.fromkeys(_measure_facade._USAGE_BUCKET_KEYS, 0)
    advisors = []
    for iteration in iterations:
        if not isinstance(iteration, dict):
            continue
        kind = iteration.get("type")
        if kind == "message":
            for k, v in _measure_facade._usage_buckets(iteration).items():
                executor_sum[k] += v
        elif kind == "advisor_message":
            buckets = _measure_facade._usage_buckets(iteration)
            if not any(buckets.values()):
                continue
            adv_model = iteration.get("model")
            if not (isinstance(adv_model, str) and adv_model
                    and _measure_facade._is_priced_model(adv_model, tier)):
                adv_model = model
            advisors.append((adv_model, buckets))
            for k, v in buckets.items():
                advisor_sum[k] += v
    if not advisors:
        return [(model, agg)]
    executor = dict(agg)
    if all(agg[k] >= executor_sum[k] + advisor_sum[k] for k in _measure_facade._USAGE_BUCKET_KEYS):
        executor = {k: agg[k] - advisor_sum[k] for k in _measure_facade._USAGE_BUCKET_KEYS}
    return [(model, executor)] + advisors


def _price_usage_parts(parts, tier=None, per_request=None, ttl_split=None):
    """USD for _billed_usage_parts output; cache writes priced by TTL split."""
    _measure_facade = _compat.facade()
    total = 0.0
    for part_model, b in parts:
        if ttl_split and (b["cc_1h"] or b["cc_5m"]):
            total += _measure_facade._get_model_cost(part_model, b["inp"], b["out"], b["cr"], b["cc"],
                                     tier=tier, cache_create_1h=b["cc_1h"],
                                     cache_create_5m=b["cc_5m"], per_request=per_request)
        else:
            total += _measure_facade._get_model_cost(part_model, b["inp"], b["out"], b["cr"], b["cc"],
                                     tier=tier, per_request=per_request)
    return total


def _turn_prefix_cached(turn):
    """Cached prefix a turn leaves behind: final executor when known (#287)."""
    _measure_facade = _compat.facade()
    if "ctx_cache" in turn:
        return max(0, _measure_facade._safe_int(turn.get("ctx_cache")))
    return _measure_facade._safe_int(turn.get("cache_read")) + _measure_facade._safe_int(turn.get("cache_creation"))


def _turn_resume_write(turn):
    """Cache written when a turn starts: first executor when known (#287)."""
    _measure_facade = _compat.facade()
    if "resume_cache_creation" in turn:
        return max(0, _measure_facade._safe_int(turn.get("resume_cache_creation")))
    return _measure_facade._safe_int(turn.get("cache_creation"))


def _context_token_count(usage):
    """Input footprint, including flat or TTL-split cache writes."""
    _measure_facade = _compat.facade()
    if not isinstance(usage, dict):
        return 0
    creation = _measure_facade._safe_int(usage.get("cache_creation_input_tokens"))
    if not creation:
        nested = usage.get("cache_creation")
        if not isinstance(nested, dict):
            nested = {}
        creation = sum(_measure_facade._safe_int(nested.get(key) or usage.get(key)) for key in (
            "ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"))
    return (_measure_facade._safe_int(usage.get("input_tokens"))
            + _measure_facade._safe_int(usage.get("cache_read_input_tokens")) + creation)


def estimate_tokens_from_file(filepath):
    """Estimate tokens by reading file content (character count / 4)."""
    _measure_facade = _compat.facade()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return int(len(content) / _measure_facade.CHARS_PER_TOKEN)
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def _project_claude_dir():
    """`<cwd>/.claude` when it exists and is a real directory, else None.

    Never follows a symlink out of the tree and never raises: a cwd that has
    been deleted underneath us (a stale shell) must degrade to "no project
    scope", not blow up a report.
    """
    _measure_facade = _compat.facade()
    try:
        p = _measure_facade.Path.cwd() / ".claude"
        if p.is_dir() and not p.is_symlink():
            return p
    except OSError:
        pass
    return None


def _skill_scan_dirs():
    """Every directory Claude Code loads skills from for THIS session, in order.

    Project-scoped skills (`<cwd>/.claude/skills`) are injected into the
    skill listing for every session in that cwd, exactly like `~/.claude/skills`,
    but measure.py only ever scanned the global dir. Any project using them was
    under-reported (~5-7% on a measured real project) and the shortfall was
    absorbed by the calibration-gap line as if it were unmeasurable overhead.

    GLOBAL FIRST, deliberately: name maps built from this order let a global
    skill win a name collision, and the dashboard ordering stays stable. The
    project dir is skipped when it IS the global dir (running from `~`), so a
    skill is never counted twice.
    """
    _measure_facade = _compat.facade()
    dirs = [_measure_facade.CLAUDE_DIR / "skills"]
    proj = _measure_facade._project_claude_dir()
    if proj is not None:
        try:
            if proj.resolve() != _measure_facade.CLAUDE_DIR.resolve():
                dirs.append(proj / "skills")
        except OSError:
            pass
    return dirs


def _agent_scan_dirs():
    """Same idea for agents: `~/.claude/agents` plus `<cwd>/.claude/agents`."""
    _measure_facade = _compat.facade()
    dirs = [_measure_facade.CLAUDE_DIR / "agents"]
    proj = _measure_facade._project_claude_dir()
    if proj is not None:
        try:
            if proj.resolve() != _measure_facade.CLAUDE_DIR.resolve():
                dirs.append(proj / "agents")
        except OSError:
            pass
    return dirs


def estimate_tokens_from_frontmatter(filepath):
    """Estimate tokens from YAML frontmatter only (between --- delimiters).

    Parallel implementation exists in fleet-auditor/scripts/fleet.py
    (_estimate_skill_frontmatter_tokens). Both must stay in sync.
    """
    _measure_facade = _compat.facade()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Strip UTF-8 BOM that Windows editors may insert
        content = content.lstrip('\ufeff')
        # Extract frontmatter between first pair of ---
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                return max(int(len(frontmatter) / _measure_facade.CHARS_PER_TOKEN) + _measure_facade.SKILL_WRAPPER_OVERHEAD, 50)
        # No frontmatter found, use rough estimate
        return _measure_facade.TOKENS_PER_SKILL_APPROX
    except (FileNotFoundError, PermissionError, OSError):
        return _measure_facade.TOKENS_PER_SKILL_APPROX


def count_lines(filepath):
    _measure_facade = _compat.facade()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def resolve_real_path(filepath):
    """Resolve symlinks to avoid double-counting."""
    _measure_facade = _compat.facade()
    try:
        return filepath.resolve()
    except OSError:
        return filepath


def _encode_project_dir_name(path_str):
    """Encode an absolute path the way Claude Code names its project dirs.

    Claude Code replaces EVERY non-alphanumeric character with '-' (no collapsing
    of consecutive dashes). This matters on Windows, where a path like
    `D:\\Code\\my app` must become `D--Code-my-app` -- the old `/`-and-`_`-only
    replacement left the drive colon, backslashes, and spaces intact, so the
    encoded name never matched and `find_projects_dir` fell back to the most
    recently modified project across ALL projects (cross-project
    leak). A leading separator still yields the leading '-' on POSIX, and POSIX
    paths with spaces now encode correctly too.
    """
    _measure_facade = _compat.facade()
    return _measure_facade.re.sub(r"[^A-Za-z0-9]", "-", str(path_str))


def _unvirtualize_msix_path(path_str):
    """Map an MSIX physical LocalCache path back to the logical AppData path."""
    _measure_facade = _compat.facade()
    text = str(path_str)
    m = _measure_facade._MSIX_REDIRECT_RE.match(text)
    if not m:
        return text
    sep = "\\" if "\\" in text else "/"
    return f"{m.group('home')}{sep}AppData{sep}{m.group('kind')}{m.group('rest')}"


def cwd_to_project_dir_name():
    """Convert cwd to Claude Code project directory name format.

    e.g. /Users/alex/my project -> -Users-alex-my-project
         D:\\Code\\my app       -> D--Code-my-app  (Windows)
    """
    _measure_facade = _compat.facade()
    return _measure_facade._encode_project_dir_name(_measure_facade._unvirtualize_msix_path(_measure_facade.Path.cwd()))


def _project_dir_from_transcript(projects_base, transcript_path):
    """The project dir holding a transcript, when it lives under projects_base."""
    _measure_facade = _compat.facade()
    if not transcript_path:
        return None
    try:
        rel = _measure_facade.Path(transcript_path).resolve().relative_to(projects_base.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    if len(rel.parts) < 2:
        return None
    candidate = projects_base / rel.parts[0]
    return candidate if candidate.is_dir() else None


def find_projects_dir(cwd=None, transcript_path=None, allow_mtime_guess=None):
    """Find the Claude Code projects directory for this session.

    Most reliable first: the hook payload's transcript_path (its parent is the
    project dir by construction), then the payload cwd, then the process cwd,
    then $CLAUDE_PROJECT_DIR, each with MSIX paths mapped back to the logical
    AppData path (#265). Only then the most-recently-modified guess.

    allow_mtime_guess=False skips that final guess and returns None instead:
    callers resolving a file Claude Code only loads from the CURRENT project
    (MEMORY.md, #301) must not be handed some other project's directory.
    """
    _measure_facade = _compat.facade()
    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None

    from_transcript = _measure_facade._project_dir_from_transcript(projects_base, transcript_path)
    if from_transcript is not None:
        return from_transcript

    bases = []
    if cwd:
        bases.append(cwd)
    try:
        bases.append(_measure_facade.Path.cwd())
    except OSError:
        pass
    if _measure_facade.os.environ.get("CLAUDE_PROJECT_DIR"):
        bases.append(_measure_facade.os.environ["CLAUDE_PROJECT_DIR"])

    for base in bases:
        for text in dict.fromkeys((_measure_facade._unvirtualize_msix_path(base), str(base))):
            expected_dir = projects_base / _measure_facade._encode_project_dir_name(text)
            if expected_dir.exists():
                return expected_dir
            # The user may be in a subdirectory of the project.
            path_cls = _measure_facade.PureWindowsPath if _measure_facade.re.match(r"^[A-Za-z]:[\\/]", text) else _measure_facade.Path
            for parent in list(path_cls(text).parents)[:5]:
                parent_dir = projects_base / _measure_facade._encode_project_dir_name(parent)
                if parent_dir.exists():
                    return parent_dir

    # Last resort: most recently modified (with warning)
    if not allow_mtime_guess:
        return None
    dirs = [d for d in projects_base.iterdir() if d.is_dir()]
    if not dirs:
        return None

    def _safe_mtime(d):
        _measure_facade = _compat.facade()
        try:
            return d.stat().st_mtime
        except OSError:
            return 0

    result = max(dirs, key=_safe_mtime)
    # stderr, never stdout: this runs inside hooks (SessionStart's ensure-health
    # path among them), where stdout becomes the model's additionalContext.
    print(f"  [Warning] Could not match cwd to project dir. Using most recent: {result.name}",
          file=_measure_facade.sys.stderr)
    return result


def get_session_baselines(limit=None):
    """Extract first-message token counts from recent JSONL session logs."""
    # Hermes: sessions live in ~/.hermes/state.db, not
    # ~/.claude/projects/*.jsonl. Return [] so the dashboard's baselines
    # row renders empty without scanning CLAUDE_DIR.
    _measure_facade = _compat.facade()
    if _measure_facade._use_hermes_session_adapter():
        return []
    # Antigravity: sessions live in the read-only conversation
    # store under ~/.gemini, not ~/.claude/projects/*.jsonl.
    if _measure_facade._use_antigravity_session_adapter():
        return []
    projects_dir = _measure_facade.find_projects_dir()
    if not projects_dir:
        return []

    jsonl_files = sorted(
        _measure_facade.glob.glob(str(projects_dir / "*.jsonl")),
        key=_measure_facade.os.path.getmtime,
        reverse=True,
    )

    baselines = []
    for jf in jsonl_files[:limit]:
        try:
            mtime = _measure_facade.os.path.getmtime(jf)
            first_usage = None
            with open(jf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        data = _measure_facade.json.loads(line)
                        if "message" in data and isinstance(data["message"], dict):
                            msg = data["message"]
                            if "usage" in msg:
                                # Context size, not the advisor-summed bill (#287).
                                first_usage = _measure_facade._context_token_count(
                                    _measure_facade._context_usage(msg["usage"]))
                                break
                    except (_measure_facade.json.JSONDecodeError, KeyError, TypeError):
                        continue

            if first_usage:
                baselines.append({
                    "date": _measure_facade.datetime.fromtimestamp(mtime).isoformat(),
                    "baseline_tokens": first_usage,
                })
        except (PermissionError, OSError):
            continue

    return baselines
