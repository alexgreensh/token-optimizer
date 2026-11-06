"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _settings_lock(user_initiated=None):
    """Bounded portable lease for settings.json writes.

    Prevents concurrent writes from silently overwriting each other.
    Contenders skip the mutation after 75ms rather than blocking a hook.

    `user_initiated=True` is an explicit user command: it must not
    lose to the cohort throttle -- it opts out of the herd reservation,
    reclaims a RELEASED lease tombstone at once, and retries briefly (2s, or
    the hook deadline when there is one) while a lease is still held.
    """
    _measure_facade = _compat.facade()
    lease_path = _measure_facade.SETTINGS_PATH.parent / ".settings.lease"
    with _measure_facade.lease_lock(
        lease_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=2.0 if user_initiated else 0.075,
        cohort_throttle=not user_initiated,
        reclaim_released=user_initiated,
    ) as acquired:
        yield acquired


def _acquire_session_end_flush_lock(max_age_seconds=None):
    """Return a lock directory path, or None if another worker is active."""
    _measure_facade = _compat.facade()
    lock_dir = _measure_facade.SNAPSHOT_DIR / ".session-end-flush.lock"
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        lock_dir.mkdir(mode=0o700)
        return lock_dir
    except FileExistsError:
        try:
            age = _measure_facade.time.time() - lock_dir.stat().st_mtime
            if age > max_age_seconds:
                lock_dir.rmdir()
                try:
                    lock_dir.mkdir(mode=0o700)
                except FileExistsError:
                    return None
                return lock_dir
        except OSError:
            pass
        return None
    except OSError:
        return None


def _release_session_end_flush_lock(lock_dir):
    _measure_facade = _compat.facade()
    if not lock_dir:
        return
    try:
        lock_dir.rmdir()
    except OSError:
        pass


def _session_refresh_due(min_interval_seconds=None):
    """Throttle expensive Stop-hook dashboard rebuilds."""
    _measure_facade = _compat.facade()
    marker = _measure_facade.SNAPSHOT_DIR / ".last-session-end-refresh"
    try:
        if marker.exists() and _measure_facade.time.time() - marker.stat().st_mtime < min_interval_seconds:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except OSError:
        # If the marker cannot be read/written, prefer correctness over
        # staleness; the wall-clock budget still caps the worker.
        return True


def _pid_is_running(pid):
    """True when process ``pid`` exists, on every OS. Never raises.

    ``os.kill(pid, 0)`` is a liveness probe only on POSIX. On Windows signal 0
    is CTRL_C_EVENT: Python calls GenerateConsoleCtrlEvent, which fails for
    any pid when the caller has no console (hooks run detached, so a live lock
    owner read as dead) and, with a console, interrupts the whole console
    group. Windows asks the process table instead. When the answer is unknown
    the process counts as running, so a lock is never stolen from a live owner.
    """
    _measure_facade = _compat.facade()
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _measure_facade.os.name == "nt":
        return _measure_facade._win_pid_is_running(pid)
    try:
        _measure_facade.os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _win_pid_is_running(pid):
    _measure_facade = _compat.facade()
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # 5 = access denied: the process exists but is not ours.
            # 87 = invalid parameter: no such process.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def _record_hook_status(key, result):
    """Remember the last outcome of a background hook job, for the doctor.

    The Stop hook's output goes to NUL and its runner always exits 0, so
    without this a user cannot tell "ran", "skipped on purpose" and "broke"
    apart (issue #276). Best effort: never raises.
    """
    _measure_facade = _compat.facade()
    try:
        try:
            data = _measure_facade.json.loads(_measure_facade.HOOK_STATUS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[key] = {"at": _measure_facade.datetime.now(_measure_facade.timezone.utc).isoformat(timespec="seconds"), "result": result}
        _measure_facade.HOOK_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _measure_facade.HOOK_STATUS_FILE.with_name(_measure_facade.HOOK_STATUS_FILE.name + f".{_measure_facade.os.getpid()}.tmp")
        tmp.write_text(_measure_facade.json.dumps(data, indent=2), encoding="utf-8")
        _measure_facade.os.replace(tmp, _measure_facade.HOOK_STATUS_FILE)
    except Exception:
        pass


def _flush_capture_checkpoint(args):
    """The checkpoint step of the deferred Stop/SessionEnd flush worker.

    The worker has no stdin, so the Stop runner hands it the session identity
    the host gave the hook as ``--session-id`` / ``--transcript-path``. On Codex
    with no identity from the host or the environment, no checkpoint is
    written: an anonymous "unknown" file can outrank real ones on restore
    (issue #278). Returns the checkpoint path or None.
    """
    _measure_facade = _compat.facade()
    flush_trigger = "end"
    sid = None
    transcript = None
    for i, a in enumerate(args):
        if a == "--trigger" and i + 1 < len(args):
            flush_trigger = args[i + 1]
        elif a == "--session-id" and i + 1 < len(args):
            sid = args[i + 1]
        elif a == "--transcript-path" and i + 1 < len(args):
            transcript = args[i + 1]
    if (_measure_facade._use_codex_session_adapter() and not sid and not transcript
            and _measure_facade._find_current_session_jsonl() is None):
        _measure_facade._record_hook_status(
            "stop_capture",
            "skipped: the Stop hook payload and environment carried no session id")
        return None
    path = _measure_facade.compact_capture(trigger=flush_trigger, backfill_tools=True,
                           session_id=sid, transcript_path=transcript)
    _measure_facade._record_hook_status(
        "stop_capture",
        "ok: checkpoint written" if path else "no checkpoint written (session transcript not found)")
    return path


def _run_session_end_flush_worker(args, budget_seconds=None):
    """Run heavyweight Stop-hook work outside Codex's visible hook budget.

    ``budget_seconds`` bounds the whole flush; the inline cloud path passes what
    is left of the calling hook's own deadline so the flush can never outlive
    the hook timeout."""
    _measure_facade = _compat.facade()
    lock_dir = _measure_facade._acquire_session_end_flush_lock()
    if lock_dir is None:
        _measure_facade._record_hook_status("stop_flush", "skipped: another refresh was already running")
        return
    _lean = _measure_facade._remote_lean_profile()
    old_budget = _measure_facade._install_hook_budget(budget_seconds)
    _flush_result = "error: stopped early"
    try:
        if not _measure_facade._session_refresh_due():
            _flush_result = "skipped: last refresh and checkpoint were under 2 minutes ago"
        else:
            _flush_result = "ran"
        if _flush_result.startswith("ran"):
            try:
                # First-ever collection: DEEP backfill of all history still on disk
                # (not just the rolling 90-day window), so a long-time user's baseline
                # freezes from their REAL earliest sessions immediately -- we never make
                # someone with existing history wait for a fresh 30-day window. Claude
                # Code purges old transcripts over time, so we grab everything still
                # present the first chance we get; collect is incremental + zero-token
                # (it skips files already collected). Subsequent flushes use the rolling
                # 90-day window. The marker also re-triggers once after an upgrade to this
                # version, so existing users (e.g. installs that only started collecting
                # recently) get the deep backfill applied retroactively.
                _backfill_marker = _measure_facade.SNAPSHOT_DIR / ".initial-backfill-done"
                _attempts_marker = _measure_facade.SNAPSHOT_DIR / ".initial-backfill-attempts"
                _deep = not _backfill_marker.exists()
                _collect_days = _measure_facade._INITIAL_BACKFILL_DAYS if _deep else 90
                if _deep:
                    # Count this attempt BEFORE the heavy call, so a _HookTimeout (which is a
                    # BaseException that bypasses `except Exception` below) still increments the
                    # counter. Once we hit the cap, retire the deep glob unconditionally so it
                    # can never re-fire forever; the rolling 90-day window keeps data current.
                    try:
                        _attempts = int(_attempts_marker.read_text().strip() or "0")
                    except (OSError, ValueError):
                        _attempts = 0
                    try:
                        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                        _attempts_marker.write_text(str(_attempts + 1))
                    except OSError:
                        pass
                    if _attempts + 1 >= _measure_facade._INITIAL_BACKFILL_MAX_ATTEMPTS:
                        try:
                            _backfill_marker.touch()
                        except OSError:
                            pass
                _measure_facade.collect_sessions(days=_collect_days, quiet=True, rebuild=False)
                # Reached only when collect_sessions returns NORMALLY (no timeout): the deep
                # backfill completed within budget, so mark it done and stop re-globbing.
                if _deep:
                    try:
                        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                        _backfill_marker.touch()
                    except OSError:
                        pass
            except Exception:
                pass
            try:
                # No browser in a cloud VM: the 4 MB dashboard rebuild is skipped
                # there (an explicit /token-dashboard still builds it on demand).
                if not _lean and not (_measure_facade._dashboard_hook_backoff_active() or _measure_facade._dashboard_selfheal_inflight()):
                    _measure_facade.generate_standalone_dashboard(days=30, quiet=True)
            except _measure_facade._HookTimeout:
                # Gap 4: the bounded SessionEnd regen was killed by the 20s budget
                # (a large history's 4 MB build does not fit), so it may have left
                # a stale-or-degraded dashboard. Hand the rebuild to the same
                # detached, unbounded self-heal the explicit-open path uses, so the
                # file catches up off the hot path instead of waiting for the user
                # to re-open. Re-raised so the outer handler still runs cleanup.
                try:
                    _measure_facade._spawn_detached_dashboard_selfheal(days=30)
                except Exception:
                    pass
                raise
            except Exception:
                pass
            try:
                _measure_facade._flush_capture_checkpoint(args)
            except Exception:
                pass
            # Data-retention enforcement (enterprise compliance)
            try:
                _measure_facade._cleanup_quality_cache()
            except Exception:
                pass
            try:
                from archive_result import cleanup_old_archives
                cleanup_old_archives()
            except Exception:
                pass
            try:
                _measure_facade.reclaim_orphaned_plugin_data_dirs()
            except Exception:
                pass
            try:
                _measure_facade._rotate_checkpoint_events()
            except Exception:
                pass
            try:
                _measure_facade._prune_trends_db()
            except Exception:
                pass
    except _measure_facade._HookTimeout:
        _flush_result = "timed out: refresh hit its time budget and resumes next turn"
    except Exception as exc:
        _flush_result = f"error: {type(exc).__name__}"
        raise
    else:
        # Flush work completed without a timeout. M-5: release the flush lock
        # BEFORE running post-flush extensions, so a slow/hung extension does
        # not extend lock hold time and block concurrent session-end flushes.
        # The flush work is already complete; the extension runs with the
        # user's privileges, not under the lock. The budget (SIGALRM watchdog)
        # is kept so a hung extension is still hard-killed — only the lock is
        # released early.
        _measure_facade._release_session_end_flush_lock(lock_dir)
        lock_dir = None
        # Optional local post-flush extensions (admin-placed, OFF by default).
        # See _run_post_flush_extensions: nothing is loaded unless the file
        # exists, every failure is swallowed, and extensions get the remaining
        # flush budget.
        try:
            _measure_facade._run_post_flush_extensions(
                time_left_fn=old_budget.remaining,
                version=_measure_facade.TOKEN_OPTIMIZER_VERSION)
        except (Exception, SystemExit):
            pass
    finally:
        # Only release the lock if the else branch didn't already (timeout
        # path or an unexpected exception escaping the try block). The budget
        # is always cleared here — the else branch keeps it for the extension.
        if lock_dir is not None:
            _measure_facade._release_session_end_flush_lock(lock_dir)
        _measure_facade._clear_hook_budget(old_budget)
        _measure_facade._record_hook_status("stop_flush", _flush_result)


def _run_post_flush_extensions(time_left_fn=None, version=None):
    """Load and run an optional local extension after the flush work.

    A user or admin may place a single file at
    ``<CONFIG_DIR>/extensions/post_flush.py`` defining ``run(context)``. It is
    OFF by default: with no file present this function does nothing (no import,
    no I/O beyond one existence check). The file is loaded ONLY from that exact
    path — never from the repo, the snapshot dir, or an env override — and only
    when it is safe to execute: owned by the current user, not
    group/world-writable, and with extensions/config directories that are not
    group/world-writable either (a peer able to write the file or its parent
    directory can inject code). Everything is fail-open: any exception the extension
    raises (or the load itself) is swallowed; the hook always completes. The
    extension receives the remaining flush budget via ``context["time_left_fn"]``
    and should defer work that does not fit; the watchdog may hard-exit the
    worker when the budget expires.

    fd hygiene mirrors module_runner.py's marker writer (C-5): O_NOFOLLOW
    refuses symlinks at the kernel level (a symlinked extension path is not
    followed), O_NONBLOCK refuses FIFOs (a FIFO at the extension path would
    block the worker thread indefinitely, preventing the finally from
    releasing the flush lock), and an st_uid == os.getuid() owner check on
    POSIX refuses an extension file planted by another user. Non-regular
    files (char/block devices) are refused via S_ISREG. The read is bounded
    to 1 MB (M-4) so a huge or sparse file cannot OOM the worker.

    SystemExit is caught alongside Exception (H-6): an extension calling
    sys.exit() must not escape the fail-open boundary and terminate the
    worker. Every decision point emits a [Token Optimizer] stderr line (H-5)
    so an admin who installs a broken extension gets feedback.

    context keys: trends_db, snapshot_dir, config_dir, runtime, version,
    time_left_fn. Token Optimizer ships no extensions and takes no
    responsibility for third-party ones; they run with the user's privileges.
    """
    _measure_facade = _compat.facade()
    ext_dir = _measure_facade.CONFIG_DIR / "extensions"
    ext_path = ext_dir / "post_flush.py"
    try:
        # Open once and judge the fd, not the path: a stat()-then-exec would
        # leave a window where the file is swapped (or symlink-retargeted)
        # between the permission check and the code actually executed.
        # O_NOFOLLOW + O_NONBLOCK mirror module_runner.py's marker defense
        # (C-5): a symlink at ext_path fails ELOOP, a FIFO fails ENXIO
        # instead of blocking the worker forever. Both are absent on Windows;
        # getattr fallback of 0 makes them no-ops there.
        flags = (_measure_facade.os.O_RDONLY
                 | getattr(_measure_facade.os, "O_NOFOLLOW", 0)
                 | getattr(_measure_facade.os, "O_NONBLOCK", 0))
        try:
            fd = _measure_facade.os.open(ext_path, flags)
        except OSError as e:
            # FileNotFoundError (no extension installed, the common case) is
            # silent. ELOOP (symlink) / ENXIO (FIFO) get a stderr note so an
            # admin sees the security rejection (H-5).
            if e.errno != _measure_facade.errno.ENOENT:
                _measure_facade.sys.stderr.write(
                    f"[Token Optimizer] post-flush extension refused at "
                    f"{ext_path}: {_measure_facade.os.strerror(e.errno)} (errno {e.errno})\n")
            return None
        try:
            st = _measure_facade.os.fstat(fd)
            # C-5 / L-3: refuse non-regular files (char/block devices read
            # garbage data and are not executable code).
            if not _measure_facade.stat.S_ISREG(st.st_mode):
                _measure_facade.sys.stderr.write(
                    f"[Token Optimizer] post-flush extension refused at "
                    f"{ext_path}: not a regular file\n")
                return None
            # C-5: owner check on POSIX. An extension file planted by another
            # user (e.g. a world-readable config dir) is local code injection.
            # Windows ACLs are the protection there, not uid bits.
            if _measure_facade.os.name != "nt":
                if st.st_uid != _measure_facade.os.getuid():
                    _measure_facade.sys.stderr.write(
                        f"[Token Optimizer] post-flush extension refused at "
                        f"{ext_path}: owner {st.st_uid} != current user "
                        f"{_measure_facade.os.getuid()}\n")
                    return None
                # POSIX mode bits only: Windows reports 0o666 for every writable
                # file (no group/other split), so this check would reject every
                # extension there. Same hasattr-style POSIX gate as os.fchmod.
                if st.st_mode & (_measure_facade.stat.S_IWGRP | _measure_facade.stat.S_IWOTH):
                    _measure_facade.sys.stderr.write(
                        f"[Token Optimizer] post-flush extension refused at "
                        f"{ext_path}: group/world-writable\n")
                    return None
            # M-4: bounded read. Cap at 1 MB so a very large or sparse file
            # cannot OOM the worker and prevent the finally from releasing
            # the flush lock.
            read_size = min(st.st_size if st.st_size > 0 else 1_048_576,
                            1_048_576)
            source = _measure_facade.os.read(fd, read_size)
        finally:
            # L-2: wrap os.close in try/except so a bad fd never escapes the
            # fail-open boundary.
            try:
                _measure_facade.os.close(fd)
            except OSError:
                pass
        # Directory gates (POSIX): a group/world-writable extensions or config
        # directory lets a peer replace post_flush.py before the open above, so
        # refuse those too. Directories are judged by stat (they cannot be
        # opened like a regular file); Windows mode bits are unreliable so the
        # check is skipped there just like the file check above.
        if _measure_facade.os.name != "nt":
            for directory in (ext_dir, _measure_facade.CONFIG_DIR):
                try:
                    if directory.stat().st_mode & (_measure_facade.stat.S_IWGRP | _measure_facade.stat.S_IWOTH):
                        _measure_facade.sys.stderr.write(
                            f"[Token Optimizer] post-flush extension refused: "
                            f"directory {directory} is group/world-writable\n")
                        return None
                except OSError:
                    pass
        module = _measure_facade.types.ModuleType("_to_post_flush_ext")
        module.__file__ = str(ext_path)
        try:
            exec(compile(source, str(ext_path), "exec"), module.__dict__)
        except Exception as e:
            _measure_facade.sys.stderr.write(
                f"[Token Optimizer] post-flush extension failed to load at "
                f"{ext_path}: {type(e).__name__}\n")
            return None
        run = getattr(module, "run", None)
        if not callable(run):
            _measure_facade.sys.stderr.write(
                f"[Token Optimizer] post-flush extension at {ext_path}: "
                f"no callable run(context) defined\n")
            return None
        context = {
            "trends_db": _measure_facade.TRENDS_DB,
            "snapshot_dir": _measure_facade.SNAPSHOT_DIR,
            "config_dir": _measure_facade.CONFIG_DIR,
            "runtime": _measure_facade.detect_runtime(),
            "version": version,
            "time_left_fn": time_left_fn,
        }
        try:
            result = run(context)
        except SystemExit as e:
            # H-6: SystemExit is a BaseException, not an Exception. Without
            # this catch, an extension calling sys.exit() would propagate
            # uncaught past the loader, past the caller's except Exception,
            # and terminate the worker — violating the docstring's guarantee
            # that "the hook always completes."
            _measure_facade.sys.stderr.write(
                f"[Token Optimizer] post-flush extension at {ext_path} "
                f"called sys.exit({e.code!r}); swallowed (fail-open)\n")
            return None
        except Exception as e:
            _measure_facade.sys.stderr.write(
                f"[Token Optimizer] post-flush extension at {ext_path} "
                f"raised {type(e).__name__}; swallowed (fail-open)\n")
            return None
        _measure_facade.sys.stderr.write(
            f"[Token Optimizer] post-flush extension at {ext_path} ran "
            f"successfully\n")
        return result
    except (Exception, SystemExit):
        # Outer boundary: any unexpected failure (including a SystemExit that
        # somehow bypassed the inner catch) stays fail-open.
        return None


def _defer_session_end_flush(args):
    """Detach the expensive dashboard/session refresh so Stop returns quickly."""
    _measure_facade = _compat.facade()
    try:
        cmd = [
            _measure_facade._detached_python_exe(),
            str(_measure_facade.Path(_measure_facade.__file__).resolve()),
            "session-end-flush-worker",
            *args[1:],
        ]
        env = _measure_facade.os.environ.copy()
        env["TOKEN_OPTIMIZER_RUNTIME"] = _measure_facade.detect_runtime()
        _proc = _measure_facade.spawn_detached(
            cmd,
            stdin=_measure_facade.subprocess.DEVNULL,
            stdout=_measure_facade.subprocess.DEVNULL,
            stderr=_measure_facade.subprocess.DEVNULL,
            cwd=str(_measure_facade.Path.cwd()),
            env=env,
            close_fds=True,
        )
        if _proc is None:
            _measure_facade._log_spawn_failure("session-end flush spawn failed")
    except Exception:
        pass


def _dispatch_session_end_flush(args, budget_seconds=None):
    """Route the `session-end-flush` CLI entry.

    Defer by DEFAULT so a legacy bare `session-end-flush` hook fossilized in
    settings.json (pre-5.11.77 script installs, no --defer) stops running the
    flush synchronously inline (which wedged Windows stop-hooks at 3/4). Only
    `--no-defer` forces the inline worker (manual refresh / tests). Returns the
    chosen mode string so callers/tests can assert the decision without spawning.
    """
    _measure_facade = _compat.facade()
    if "--no-defer" in args:
        _measure_facade._run_session_end_flush_worker(args)
        return "inline"
    if _measure_facade._remote_lean_profile():
        # Claude Code cloud: a detached worker can be cut off when the VM is
        # reclaimed right after the session ends, losing the session's rows.
        # Run the flush in this process instead, bounded by what is left of the
        # hook's own deadline (Stop 13 s, SessionEnd 58 s) so it never runs past
        # the hook timeout. A flush that does not fit resumes on the next Stop.
        # The Stop runner shares ONE deadline across its subcommands and passes
        # this one's fair share (``budget_seconds``); taking the whole remainder
        # instead let the flush overrun into keepwarm-arm's slice, the shared
        # HookDeadline then fired os._exit(0) and the runner's buffered stdout
        # was lost (review #9).
        _measure_facade._run_session_end_flush_worker(args, budget_seconds=_measure_facade._inline_flush_budget_seconds(cap=budget_seconds))
        return "inline-remote"
    _measure_facade._defer_session_end_flush(args)
    return "deferred"


def _inline_flush_budget_seconds(default=None, margin=None, cap=None):
    """Seconds the inline (cloud) session-end flush may use: the remaining time
    on the active hook deadline minus a margin, or ``default`` without one, and
    never more than ``cap`` (the caller's fair share of a shared deadline)
    minus the margin. Floor 0.1 s: a spent share gets a token attempt that
    the flush lock and the budget cut short, never an overrun."""
    _measure_facade = _compat.facade()
    budget = default
    try:
        from hook_runtime import current_deadline
        deadline = current_deadline()
        if deadline is not None:
            budget = min(20.0, deadline.remaining() - margin)
    except Exception:
        pass
    if cap is not None:
        try:
            budget = min(budget, float(cap) - margin)
        except (TypeError, ValueError):
            pass
    return max(0.1, budget)


def _dispatch_collect(args):
    """CLI ``collect`` entry. Bounded ONLY on the hook path.

    A fossilized SessionEnd hook that chains the ``collect`` and ``dashboard``
    subcommands never reaches ``session-end-flush``. The 20s HookDeadline is
    the last line of defense so an escaped fossil still closes the hook pipe
    on Windows.

    The budget is armed only when ``_running_under_hook()`` is
    true (hook runner / fossil context) AND ``--rebuild`` is not present.
    Arming it unconditionally killed interactive ``collect --rebuild`` on a
    large history mid-transaction (os._exit(0) -> SQLite rollback -> the
    "needs rebuild" flag stays true -> every later run wedges, exiting 0).
    ``--rebuild`` is exempt regardless of context: it is an intentionally
    heavy operation and bounding it mid-transaction is the wedge itself.
    """
    _measure_facade = _compat.facade()
    days = 90
    quiet = "--quiet" in args or "-q" in args
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass
    rebuild = "--rebuild" in args
    deadline = (
        _measure_facade._install_hook_budget(20) if (_measure_facade._running_under_hook() and not rebuild) else None
    )
    try:
        _measure_facade.collect_sessions(days=days, quiet=quiet, rebuild=rebuild)
    except _measure_facade._HookTimeout:
        pass
    finally:
        _measure_facade._clear_hook_budget(deadline)

    # Existing-script-install self-heal. An old script
    # install has the collect&&dashboard fossil in settings.json and NO
    # ensure-health SessionStart hook, so run_ensure_health never reaches it.
    # The fossil's OWN hook-path run reconciles settings.json here, removing
    # the fossil going forward. Runs AFTER the flush and its 20s budget are
    # cleared, so it never delays the flush itself; the self-heal carries its
    # OWN short deadline (see the helper) so a stalled settings write can't
    # hold the hook pipe open. Throttled to 24h, fail-open. Gated on the hook
    # path so an interactive terminal `collect` never triggers it.
    if _measure_facade._running_under_hook():
        try:
            _measure_facade._maybe_self_heal_sessionend_fossils_on_hook()
        except Exception:
            pass


def _spawn_detached_dashboard_selfheal(days=None, force=None, *, hook_timeout=None):
    """Rebuild the dashboard in a DETACHED, unbounded background process.

    Bug B self-heal: when a bounded hook-path regen is killed by the 20s budget
    (a large history whose 4 MB rebuild does not fit the window), the on-disk
    dashboard used to stay stale forever -- the SessionEnd/Stop hook kept getting
    killed and never caught up. Instead, on that timeout we hand the rebuild to a
    child that:
      * runs with TOKEN_OPTIMIZER_INTERACTIVE=1 so it is UNBOUNDED (it finishes
        the rebuild however long it takes),
      * is fully detached (start_new_session / DETACHED_PROCESS) so it outlives
        the hook and never holds the hook's stdout pipe open,
      * is quiet and never opens a browser.
    The child is unbounded, so it cannot itself time out and re-spawn -- no loop.
    Fire-and-forget; never raises (a failed self-heal must not break the hook).

    ``force`` appends ``--force`` so the child bypasses the 60s write throttle.
    Used by the version-bump self-heal: a stale file written seconds ago by a
    just-killed regen must not throttle-skip the heal. The PR #154 version guard
    still applies inside generate_standalone_dashboard (force governs the throttle,
    not version precedence), so a forced heal never clobbers a newer dashboard.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._dashboard_heal_spawn_due():
        if hook_timeout:
            _measure_facade._record_dashboard_hook_timeout()
        return
    try:
        env = dict(_measure_facade.os.environ)
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
        env["TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL"] = "1"
        env.pop("TOKEN_OPTIMIZER_HOOK", None)
        # Detach so the child outlives the killed hook: POSIX new session, or
        # (on Windows) DETACHED_PROCESS. start_new_session is POSIX-only.
        popen_kw = {} if _measure_facade.os.name == "nt" else {"start_new_session": True}
        argv = [_measure_facade.sys.executable, _measure_facade.os.path.abspath(_measure_facade.__file__),
                "dashboard", "--quiet", "--days", str(int(days))]
        if force:
            argv.append("--force")
        # creationflags MUST be inlined and mention _NO_WINDOW (CREATE_NO_WINDOW,
        # 0 off-Windows) so a console-less Windows host never flashes a cmd window
        # -- and so the spawn-site invariant test can see it. DETACHED_PROCESS is 0
        # off-Windows, so this evaluates to 0 on POSIX (the only valid value there).
        child = _measure_facade.subprocess.Popen(
            argv,
            stdin=_measure_facade.subprocess.DEVNULL,
            stdout=_measure_facade.subprocess.DEVNULL,
            stderr=_measure_facade.subprocess.DEVNULL,
            close_fds=True,
            env=env,
            creationflags=(getattr(_measure_facade.subprocess, "DETACHED_PROCESS", 0) | _measure_facade._NO_WINDOW),
            **popen_kw,
        )
        try:
            # The marker describes the rebuild child, not the hook that exits.
            _measure_facade._replace_dashboard_heal_pid(str(_measure_facade.os.getpid()), str(child.pid))
        except (AttributeError, OSError):
            pass
        if hook_timeout:
            _measure_facade._record_dashboard_hook_timeout()
    except Exception as _e:
        try:
            marker = _measure_facade.SNAPSHOT_DIR / _measure_facade._DASHBOARD_HEAL_LOCK_NAME
            if marker.read_text().strip() == str(_measure_facade.os.getpid()):
                marker.unlink()
        except OSError:
            pass
        # Fire-and-forget: never raise (a failed self-heal must not break the
        # hook). But DO leave a breadcrumb — every other spawn site calls
        # _log_spawn_failure; this was the only one that swallowed silently,
        # leaving the dashboard stale forever with no diagnostic trail.
        try:
            _measure_facade._log_spawn_failure(
                "dashboard self-heal spawn failed: %s: %s" % (type(_e).__name__, _e)
            )
        except Exception:
            pass


def _dashboard_hook_backoff_active():
    """Back off only after the detached child actually refreshed the dashboard."""
    _measure_facade = _compat.facade()
    marker = _measure_facade.SNAPSHOT_DIR / "dashboard.hook.backoff"
    try:
        marker_mtime = marker.stat().st_mtime
        dashboard_mtime = _measure_facade.DASHBOARD_PATH.stat().st_mtime
        age = _measure_facade.time.time() - dashboard_mtime
        if dashboard_mtime < marker_mtime or not -_measure_facade._DASHBOARD_BACKOFF_CLOCK_SKEW_SECONDS <= age < _measure_facade._DASHBOARD_HOOK_BACKOFF_SECONDS:
            return False
        flags = _measure_facade.os.O_RDONLY | getattr(_measure_facade.os, "O_NOFOLLOW", 0) | getattr(_measure_facade.os, "O_NONBLOCK", 0)
        fd = _measure_facade.os.open(str(marker), flags)
        try:
            return (_measure_facade.stat.S_ISREG(_measure_facade.os.fstat(fd).st_mode)
                    and _measure_facade.os.read(fd, 64).decode("ascii", errors="ignore") == _measure_facade.TOKEN_OPTIMIZER_VERSION)
        finally:
            _measure_facade.os.close(fd)
    except OSError:
        return False


def _record_dashboard_hook_timeout():
    """Record the attempt; an updated dashboard activates the one-hour cooldown."""
    _measure_facade = _compat.facade()
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        marker = _measure_facade.SNAPSHOT_DIR / "dashboard.hook.backoff"
        flags = (_measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT
                 | getattr(_measure_facade.os, "O_NOFOLLOW", 0) | getattr(_measure_facade.os, "O_NONBLOCK", 0))
        fd = _measure_facade.os.open(str(marker), flags, 0o600)
        try:
            if _measure_facade.stat.S_ISREG(_measure_facade.os.fstat(fd).st_mode):
                _measure_facade.os.ftruncate(fd, 0)
                _measure_facade.os.write(fd, _measure_facade.TOKEN_OPTIMIZER_VERSION.encode("ascii"))
        finally:
            _measure_facade.os.close(fd)
    except OSError:
        pass


def _replace_dashboard_heal_pid(expected, child_pid):
    _measure_facade = _compat.facade()
    marker = _measure_facade.SNAPSHOT_DIR / _measure_facade._DASHBOARD_HEAL_LOCK_NAME
    flags = _measure_facade.os.O_RDWR | getattr(_measure_facade.os, "O_NOFOLLOW", 0) | getattr(_measure_facade.os, "O_NONBLOCK", 0)
    fd = _measure_facade.os.open(str(marker), flags)
    try:
        if not _measure_facade.stat.S_ISREG(_measure_facade.os.fstat(fd).st_mode):
            return
        if _measure_facade.os.read(fd, 64).decode("ascii", errors="ignore").strip() != expected:
            return
        _measure_facade.os.lseek(fd, 0, _measure_facade.os.SEEK_SET)
        _measure_facade.os.write(fd, child_pid.encode("ascii"))
        _measure_facade.os.ftruncate(fd, len(child_pid))
    finally:
        _measure_facade.os.close(fd)


def _dashboard_selfheal_inflight():
    _measure_facade = _compat.facade()
    with _measure_facade._dashboard_selfheal_build_lock() as acquired:
        return acquired is False


def _dashboard_selfheal_build_lock():
    """Let one detached rebuild run; the OS releases this lock after a crash."""
    _measure_facade = _compat.facade()
    fd = None
    held = None
    lock_attempted = False
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _measure_facade.SNAPSHOT_DIR / "dashboard.selfheal.build.lock"
        flags = (_measure_facade.os.O_RDWR | _measure_facade.os.O_CREAT
                 | getattr(_measure_facade.os, "O_NOFOLLOW", 0) | getattr(_measure_facade.os, "O_NONBLOCK", 0))
        fd = _measure_facade.os.open(str(path), flags, 0o600)
        if not _measure_facade.stat.S_ISREG(_measure_facade.os.fstat(fd).st_mode):
            raise OSError(_measure_facade.errno.EINVAL, "dashboard self-heal lock is not a regular file")
        if _measure_facade.os.name == "nt":
            import msvcrt
            _measure_facade.os.lseek(fd, 0, _measure_facade.os.SEEK_SET)
            lock_attempted = True
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock_attempted = True
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError as exc:
        if lock_attempted and (exc.errno in (_measure_facade.errno.EACCES, _measure_facade.errno.EAGAIN) or getattr(exc, "winerror", None) == 33):
            held = False
        else:
            _measure_facade._log_spawn_failure("dashboard self-heal lock failed: %s" % exc)
    try:
        yield held
    finally:
        if fd is not None:
            try:
                if held:
                    if _measure_facade.os.name == "nt":
                        _measure_facade.os.lseek(fd, 0, _measure_facade.os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                _measure_facade.os.close(fd)


def _dashboard_heal_spawn_due():
    """Return True iff THIS caller should spawn a dashboard heal.

    Bug (thundering herd): on a version bump every concurrent
    SessionStart's ensure-health tick sees the stale sidecar and each spawns a
    DETACHED, unbounded, FORCED ~9s rebuild -- N sessions => N simultaneous
    builds all racing os.replace on the same dashboard.html (the very lock
    contention the api/regenerate retry was calibrated around). This collapses
    that to ONE: the first caller atomically claims ``dashboard.heal.lock``
    (O_CREAT|O_EXCL, the same pidfile shape as ``_keepwarm_tick_singleton`` and
    ``_daemon_install_lock``) and spawns; concurrent callers within the staleness
    window see the fresh marker and skip. The marker is deliberately NOT unlinked
    on the happy path -- it must OUTLIVE this call to suppress the herd, and it is
    reclaimed by the next caller once it ages past the window.

    The child holds a separate OS lock for the full rebuild. Check it here so
    even an expired marker cannot launch another child while one is working.
    Fail-OPEN on filesystem errors; the child lock still prevents duplicate work.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._dashboard_selfheal_inflight():
        return False
    marker = _measure_facade.SNAPSHOT_DIR / _measure_facade._DASHBOARD_HEAL_LOCK_NAME
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    fd = None
    try:
        fd = _measure_facade.os.open(str(marker), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
    except FileExistsError:
        # A recent spawn already claimed the marker. Reclaim ONLY if it is stale;
        # a fresh marker means a heal is already in flight, so no-op.
        try:
            age = _measure_facade.time.time() - marker.stat().st_mtime
        except OSError:
            return True  # unreadable mtime: don't suppress the heal
        if age <= _measure_facade._DASHBOARD_HEAL_LOCK_STALE_SECONDS:
            return False
        # Known bounded residual (same unlink+O_EXCL shape as
        # _keepwarm_tick_singleton): the fresh-claim path above is race-free, but
        # if TWO ticks both observe the SAME stale marker and both pass the age
        # check before either unlinks, both can reclaim and spawn -- at most 2
        # rebuilds, only at the 60s staleness edge with concurrent sessions. That
        # is consistent with the fail-open contract (an extra rebuild is harmless;
        # the detached child is itself idempotent via os.replace + the version
        # guard), so it is accepted rather than serialised with a heavier lock.
        try:
            _measure_facade.os.unlink(str(marker))
            fd = _measure_facade.os.open(str(marker), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
        except FileExistsError:
            return False  # lost the reclaim race to another session -- it heals
        except OSError:
            return True
    except OSError:
        return True
    try:
        _measure_facade.os.write(fd, str(_measure_facade.os.getpid()).encode("ascii"))
    except OSError:
        pass
    finally:
        try:
            _measure_facade.os.close(fd)
        except OSError:
            pass
    return True


def _is_plugin_installed():
    """Check if token-optimizer is installed as a Claude Code plugin.

    Plugin hooks (hooks.json) auto-install all hooks, so if the plugin is
    installed, we don't need to check settings.json for individual hooks.
    """
    _measure_facade = _compat.facade()
    registry = _measure_facade.CLAUDE_DIR / "plugins" / "installed_plugins.json"
    if registry.exists():
        try:
            with open(registry, "r", encoding="utf-8") as f:
                data = _measure_facade.json.load(f)
            plugins = data.get("plugins", {})
            for key in plugins:
                if "token-optimizer" in key.lower():
                    return True
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
            pass
    # Cowork search-path fallback. When installed_plugins.json is absent or
    # doesn't list us (account-synced installs may not register the same way), but
    # THIS script is running from the synced-plugin tree, we ARE plugin-installed.
    # Additive: desktop hosts have no /plugins/synced/ segment so this never
    # changes desktop behavior.
    if _measure_facade._is_running_from_synced_plugin():
        return True
    return False


def _is_compaction_runner(cmd, event):
    """Recognize runner path arguments for the events each runner dispatches."""
    _measure_facade = _compat.facade()
    if not isinstance(cmd, str):
        return False
    name = "sessionstart_runner" if event == "SessionStart" else "stop_runner"
    if event not in ("SessionStart", "Stop", "SessionEnd"):
        return False
    return bool(_measure_facade.re.search(r"(?:^|[\s\"'/\\])" + name + r"\.py(?:$|[\s\"';])", cmd))


def _is_hook_installed(settings=None, settings_only=None):
    """Check if the SessionEnd measure.py collect hook is installed.

    Returns True if any SessionEnd hook command contains 'measure.py collect'.
    Recognizes both old (collect-only) and new (collect + dashboard) hook commands.
    Also checks plugin cache hooks (auto-installed via marketplace plugin), unless
    ``settings_only`` asks only whether settings.json carries it (doctor's label).
    """
    # Check user settings.json
    _measure_facade = _compat.facade()
    if settings is None:
        if _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
            try:
                with open(_measure_facade.SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                    settings = _measure_facade.json.load(f)
            except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
                settings = {}
        else:
            settings = {}

    hooks = settings.get("hooks", {})
    session_end = hooks.get("SessionEnd", [])
    if isinstance(session_end, list):
        for entry in session_end:
            hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
            for hook in hook_list:
                cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                if isinstance(cmd, str) and (
                    _measure_facade._TO_SESSION_END_CMD_RE.search(cmd) or _measure_facade._is_compaction_runner(cmd, "SessionEnd")
                ):
                    return True

    if settings_only:
        return False

    # Check plugin cache hooks (marketplace plugin auto-install)
    plugin_cache = _measure_facade.CLAUDE_DIR / "plugins" / "cache"
    if plugin_cache.exists():
        import glob as globmod
        for hooks_file in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
            try:
                with open(hooks_file, "r", encoding="utf-8") as f:
                    plugin_hooks = _measure_facade.json.load(f)
                ph = plugin_hooks.get("hooks", {}).get("SessionEnd", [])
                if isinstance(ph, list):
                    for entry in ph:
                        hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
                        for hook in hook_list:
                            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                            if isinstance(cmd, str) and (
                                _measure_facade._TO_SESSION_END_CMD_RE.search(cmd) or _measure_facade._is_compaction_runner(cmd, "SessionEnd")
                            ):
                                return True
            except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
                continue

    return False


def _sessionend_cmd_is_flush(cmd) -> bool:
    """True when ``cmd`` is the current SessionEnd shape (session-end-flush)."""
    _measure_facade = _compat.facade()
    if not isinstance(cmd, str) or not cmd:
        return False
    return bool(_measure_facade.re.search(r"\bmeasure\.py['\"]?\s+session-end-flush\b", cmd))


def _sessionend_cmd_is_collect_fossil(cmd) -> bool:
    """True when ``cmd`` is the collect/dashboard SessionEnd fossil.

    ``_TO_SESSION_END_CMD_RE`` matches both ``collect`` and ``session-end-flush``.
    Anything that matches and is not the flush shape is the inline heavy fossil.
    """
    _measure_facade = _compat.facade()
    if not isinstance(cmd, str) or not cmd:
        return False
    if _measure_facade._sessionend_cmd_is_flush(cmd):
        return False
    return bool(_measure_facade._TO_SESSION_END_CMD_RE.search(cmd))


def _is_hook_current(settings=None):
    """True when the installed SessionEnd hook is the session-end-flush shape.

    The collect-then-dashboard fossil is never current: it runs the
    heavy flush inline and unbounded. The pre-v5.11 win32 cmd-null-redirect
    form is also never current. setup_hook's upgrade branch rewrites anything
    that returns False.
    """
    _measure_facade = _compat.facade()
    if settings is None:
        if not _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
            return False
        try:
            with open(_measure_facade.SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                settings = _measure_facade.json.load(f)
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
            return False

    hooks = settings.get("hooks", {})
    session_end = hooks.get("SessionEnd", [])
    if not isinstance(session_end, list):
        return False
    for entry in session_end:
        hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
        for hook in hook_list:
            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
            if not isinstance(cmd, str):
                continue
            if _measure_facade._sessionend_cmd_is_collect_fossil(cmd):
                return False
            if _measure_facade._sessionend_cmd_is_flush(cmd):
                # A flush-shaped command that still uses the cmd.exe
                # NUL-device redirect is not current on win32.
                if _measure_facade.sys.platform == "win32" and _measure_facade.re.search(r">\s*NUL\b", cmd):
                    return False
                return True
    return False


def check_hook():
    """Exit 0 if SessionEnd measure.py collect hook is installed, 1 if not."""
    _measure_facade = _compat.facade()
    _measure_facade.sys.exit(0 if _measure_facade._is_hook_installed() else 1)


def _settings_write_guard(settings_data, allow_removing_keys=None, dest=None):
    """Refuse any settings.json write that would DROP a top-level key.

    The data-loss class of bug: a caller reads settings.json with the
    lossy ``_read_settings_json()`` (which collapses a missing, malformed, or
    unreadable file to ``{}``), mutates that ``{}``, and writes it back --
    erasing every key the user owned. The fix added
    ``_read_settings_json_checked()`` but converted only 2 of 20 call sites,
    so the class stayed live. This is the structural fix: ONE choke point that
    every write passes through, so no call site can drop keys by accident
    regardless of how it obtained its dict.

    Returns (allowed: bool, reason: str). Never raises.

    Rules:
      * Outgoing value must be a dict.
      * No file on disk -> nothing can be lost -> allow.
      * File on disk unreadable / malformed / not an object -> REFUSE. We
        cannot prove the write is non-destructive, and "unknown" is exactly
        the state that produced the original bug.
      * Any top-level key present on disk but absent from the outgoing dict
        must be declared in ``allow_removing_keys``, else REFUSE.
      * Same one level down inside ``env``: a var present in the on-disk
        ``env`` map but absent from the outgoing ``env`` map must be
        declared as ``env.VAR`` (or the whole subtree licensed by
        declaring ``env``), else REFUSE.

    Deliberate removals stay possible: pass ``allow_removing_keys={"statusLine"}``
    (uninstall paths) or a wider set. The opt-in is per-key and explicit, so a
    caller can never silently widen its blast radius.

    Cost: one extra read of a ~4KB file. Safe for the SessionStart budget.
    """
    _measure_facade = _compat.facade()
    if not isinstance(settings_data, dict):
        return False, f"outgoing settings is {type(settings_data).__name__}, not a dict"
    target = dest if dest is not None else _measure_facade.SETTINGS_PATH
    if _measure_facade.os.path.lexists(target) and not _measure_facade._is_regular_file(target):
        # never open() a FIFO/socket/device (it can block forever).
        return False, "settings.json on disk is not a regular file; cannot prove this write is non-destructive"
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            current = _measure_facade.json.load(f)
    except FileNotFoundError:
        return True, "no settings.json on disk; nothing to preserve"
    except (_measure_facade.json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return False, "settings.json on disk is malformed; cannot prove this write is non-destructive"
    except (PermissionError, OSError) as e:
        return False, f"settings.json on disk is unreadable ({e.__class__.__name__}); cannot prove this write is non-destructive"
    # a file with no write bits was frozen by the user on purpose
    # (chmod 444, or the read-only attribute on Windows). The atomic-write
    # trick (temp file + os.replace) would happily REPLACE it, so the
    # permission check has to happen here, before any write is attempted.
    if _measure_facade.os.stat(target).st_mode & (
            _measure_facade.stat.S_IWUSR | _measure_facade.stat.S_IWGRP | _measure_facade.stat.S_IWOTH) == 0:
        return False, ("settings.json has no write bits set -- it was made "
                       "read-only on purpose; refusing to replace a file "
                       "the user froze")
    if not isinstance(current, dict):
        return False, "settings.json on disk is not a JSON object; refusing to overwrite"
    allowed = set(allow_removing_keys or ())
    dropped = sorted(set(current) - set(settings_data) - allowed)
    if dropped:
        return False, "would DROP top-level key(s): " + ", ".join(dropped)
    # the same data-loss class one level down. `env` is a flat
    # string->string map a caller can carry forward while silently dropping
    # a var inside it (e.g. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE). Diff its keys
    # too. A deliberate nested removal stays possible by declaring the
    # dotted name in allow_removing_keys ({"env.MY_VAR"}); declaring the
    # top-level "env" licenses the whole subtree.
    if "env" not in allowed:
        cur_env = current.get("env")
        out_env = settings_data.get("env")
        if isinstance(cur_env, dict) and isinstance(out_env, dict):
            nested = sorted(
                "env." + key
                for key in set(cur_env) - set(out_env)
                if "env." + key not in allowed
            )
            if nested:
                return False, "would DROP env var(s): " + ", ".join(nested)
    return True, "ok"


def _report_settings_write_refusal(why):
    _measure_facade = _compat.facade()
    print(
        "  [Token Optimizer] REFUSED settings.json write: " + why + ".\n"
        "  Nothing was written; your settings.json is untouched. This guard "
        "exists because a write like this silently erased user settings. "
        "If the removal was intended, the caller must declare it "
        "via allow_removing_keys.",
        file=_measure_facade.sys.stderr,
    )


def _log_settings_lease_denied():
    """Durable breadcrumb when the settings lease is denied (write-return audit).

    The lease denial path in ``_write_settings_atomic`` was completely silent:
    no stderr, no log, no ``last_refusal`` breadcrumb. Callers reported
    "settings.json write was refused" but there was no way to distinguish a
    transient lease denial (contention, retries on next hook fire) from a
    guard refusal (destructive payload, needs a code fix). This writes one
    line to the daemon log dir so the denial is discoverable post-hoc, and
    sets ``last_refusal`` so the existing reporting infrastructure can tell
    the two paths apart. Never raises.
    """
    _measure_facade = _compat.facade()
    _measure_facade._SETTINGS_WRITE_READ_STATE.last_refusal = (
        "lease denied (another writer holds the settings lock)"
    )
    try:
        _measure_facade.DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _measure_facade._cap_append_log(_measure_facade.DAEMON_LOG_DIR / "settings-lease-denials.log")
        with open(_measure_facade.DAEMON_LOG_DIR / "settings-lease-denials.log", "a",
                  encoding="utf-8") as f:
            f.write("%s lease denied\n" % _measure_facade.time.strftime("%Y-%m-%dT%H:%M:%S"))
    except Exception:
        pass


def _settings_file_identity(path=None):
    """(st_mtime_ns, st_size, st_ino) of the settings file, ``("absent",)`` when unreadable.

    A cheap fingerprint taken at the merge read and compared right before
    ``os.replace``. It narrows the window in which an unlocked external editor
    can be overwritten to the gap between that last stat and the replace, a
    few milliseconds. It does not close the window: nothing stops an editor
    that ignores our lease.
    """
    _measure_facade = _compat.facade()
    try:
        st = _measure_facade.os.stat(path if path is not None else _measure_facade.SETTINGS_PATH)
    except (OSError, ValueError):
        return ("absent",)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _settings_write_refusal_reason():
    """Why the last ``_write_settings_atomic`` on this thread returned False."""
    _measure_facade = _compat.facade()
    return (getattr(_measure_facade._SETTINGS_WRITE_READ_STATE, "last_refusal", None)
            or "settings.json locked or guard refused the write")


def _existing_line_ending(path):
    """"\r\n" when the file at ``path`` is mostly CRLF, else "\n".

    A new file, an unreadable one, or one with no line break gets "\n". A mixed
    file follows whichever ending it has more of.
    """
    _measure_facade = _compat.facade()
    try:
        with open(path, "rb") as f:
            raw = f.read(1_048_576)
    except OSError:
        return "\n"
    crlf = raw.count(b"\r\n")
    return "\r\n" if crlf > raw.count(b"\n") - crlf else "\n"


def _note_settings_os_error(exc):
    """Record an OSError from the temp write / replace as the refusal reason."""
    _measure_facade = _compat.facade()
    hint = ("; another program may have it open"
            if isinstance(exc, PermissionError) else "")
    _measure_facade._SETTINGS_WRITE_READ_STATE.last_refusal = (
        f"could not write settings.json ({exc.__class__.__name__}: {exc}){hint}"
    )
    _measure_facade._SETTINGS_WRITE_READ_STATE.os_error = True


def _write_settings_atomic_locked(settings_data, allow_removing_keys=None, _report_refusal=None,
                                  expect_identity=None):
    """Atomic settings.json write assuming the settings lease is ALREADY held.

    This is the lock-free body of ``_write_settings_atomic``, extracted so
    ``_reconcile_sessionend_fossils`` can hold ``_settings_lock()`` across
    fresh-read + re-apply + write as ONE critical section.
    Calling ``_write_settings_atomic`` from inside a held ``_settings_lock()``
    would re-acquire the non-reentrant lease and deadlock, so callers that
    already hold the lease MUST use this primitive instead.

    Never call this without already holding ``_settings_lock()``; it provides
    no serialization of its own. Same tempfile + os.replace + mode/symlink
    semantics as ``_write_settings_atomic`` (see the checked-read fix). Returns True iff the
    write landed.

    ``expect_identity`` is the ``_settings_file_identity`` taken when the
    payload was merged. When given and the file no longer matches right before
    ``os.replace``, nothing is replaced and ``identity_changed`` is set on the
    thread state so the caller can re-merge.
    """
    _measure_facade = _compat.facade()
    _measure_facade._SETTINGS_WRITE_READ_STATE.identity_changed = False
    _measure_facade._SETTINGS_WRITE_READ_STATE.os_error = False
    # Write THROUGH a symlink and preserve the mode.
    # os.replace onto the link path detaches it, turning a dotfiles-managed
    # symlink into a regular file (the user's repo silently stops tracking
    # their settings) and dropping 0644 to mkstemp's 0600. Resolve the link
    # first so the temp file lands in the real target's directory, and copy
    # the destination's existing mode onto it.
    try:
        dest = _measure_facade.SETTINGS_PATH.resolve(strict=False)
    except (OSError, ValueError):
        dest = _measure_facade.SETTINGS_PATH
    # Choke point for the key-loss class. Compared against `dest` (the
    # symlink-resolved real file), which is exactly the path os.replace lands
    # on, so a relocated/symlinked settings.json is checked against the bytes
    # that are actually about to be overwritten.
    _ok, _why = _measure_facade._settings_write_guard(settings_data, allow_removing_keys, dest)
    if not _ok:
        _measure_facade._SETTINGS_WRITE_READ_STATE.last_refusal = _why
        if _report_refusal:
            _measure_facade._report_settings_write_refusal(_why)
        return False
    try:
        dest_mode = _measure_facade.stat.S_IMODE(_measure_facade.os.stat(dest).st_mode)
    except OSError:
        dest_mode = None
    try:
        tmp_fd, tmp_path = _measure_facade.tempfile.mkstemp(
            dir=str(dest.parent),
            prefix=".settings-",
            suffix=".json",
        )
    except OSError as exc:
        _measure_facade._note_settings_os_error(exc)
        return False
    try:
        # newline="" switches off text-mode translation (Windows would turn
        # every "\n" into "\r\n"); the ending is the file's own, set below.
        eol = _measure_facade._existing_line_ending(dest)
        with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
            f.write(_measure_facade.json.dumps(settings_data, indent=2, ensure_ascii=False)
                    .replace("\n", eol) + eol)
        if dest_mode is None:
            # Created from nothing: mkstemp gave 0600, but a fresh settings.json
            # should follow the umask like any file the user's tools create.
            # (os.umask can only be read by setting it; restored at once.)
            if _measure_facade.os.name != "nt":
                try:
                    _umask = _measure_facade.os.umask(0)
                    _measure_facade.os.umask(_umask)
                    dest_mode = 0o666 & ~_umask
                except OSError:
                    dest_mode = None
        if dest_mode is not None:
            try:
                _measure_facade.os.chmod(tmp_path, dest_mode)
            except OSError:
                pass
        if expect_identity is not None and _measure_facade._settings_file_identity(dest) != expect_identity:
            _measure_facade._SETTINGS_WRITE_READ_STATE.identity_changed = True
            return False
        _measure_facade.os.replace(tmp_path, str(dest))
        tmp_path = None  # successfully replaced; do not unlink the destination
    except OSError as exc:
        # Windows refuses the replace while another program holds the file open
        # (a sharing violation); a full disk fails the temp write. Both are a
        # refused write with a real reason, never a traceback. The finally
        # below removes the temp file.
        _measure_facade._note_settings_os_error(exc)
        return False
    finally:
        if tmp_path is not None:
            try:
                _measure_facade.os.unlink(tmp_path)
            except OSError:
                pass
    return True


def _merge_concurrent_settings(snapshot, mine, allow_removing_keys=None):
    """Three-way merge of our payload onto what is on disk right now.

    ``snapshot`` is the ``(path, base)`` pair recorded by
    ``_read_settings_for_write``; ``mine`` is the caller's payload derived from
    that read. Only the keys the caller actually CHANGED relative to ``base``
    are applied on top of the fresh file, so a value edit or key removal made
    by another editor between our read and our write is kept, as long as it
    landed before ``_write_settings_atomic`` fingerprinted the file (an edit
    after that is caught by the identity check before the replace and merged on
    a second try). ``env`` is merged per variable for the same reason. Returns the merged dict, or None
    when there is nothing to merge (no recorded read, the file is unchanged, or
    the payload drops keys it was not licensed to drop, which the write guard
    then refuses as before). Caller must hold the settings lease.
    """
    _measure_facade = _compat.facade()
    if not snapshot or not isinstance(mine, dict):
        return None
    snapshot_path, base = snapshot
    try:
        current_path = str(_measure_facade.SETTINGS_PATH.resolve(strict=False))
    except (OSError, ValueError):
        current_path = str(_measure_facade.SETTINGS_PATH)
    if snapshot_path != current_path or not isinstance(base, dict):
        return None
    allowed = set(allow_removing_keys or ())
    if set(base) - set(mine) - allowed:
        return None
    fresh, _path, fresh_ok = _measure_facade._read_settings_json_checked()
    if not fresh_ok or not isinstance(fresh, dict) or not _measure_facade.SETTINGS_PATH.exists():
        return None
    if fresh == base:
        return None
    merged = dict(fresh)
    for key, value in mine.items():
        if key in base and base[key] == value:
            continue
        base_env, fresh_env = base.get("env"), fresh.get("env")
        if key == "env" and isinstance(value, dict) and isinstance(base_env, dict) \
                and isinstance(fresh_env, dict):
            env = dict(fresh_env)
            for var, val in value.items():
                if var not in base_env or base_env[var] != val:
                    env[var] = val
            for var in base_env:
                if var not in value and ("env" in allowed or "env." + var in allowed):
                    env.pop(var, None)
            merged["env"] = env
        else:
            merged[key] = value
    for key in allowed:
        if key not in mine:
            merged.pop(key, None)
    return merged


def _write_settings_atomic(settings_data, allow_removing_keys=None,
                           user_initiated=None):
    """Write settings.json atomically using tempfile + os.replace().

    Acquires the advisory ``_settings_lock()`` lease to prevent concurrent
    writes from clobbering each other (e.g., during SessionStart when
    multiple hooks may modify settings.json), then delegates the actual
    tempfile + os.replace to ``_write_settings_atomic_locked``.

    ``user_initiated=True`` marks an explicit user command: the
    lease bypasses the cohort throttle and retries briefly so a deliberate
    mutation cannot lose to a herd writer's tombstone. Automated/hook paths
    keep the non-blocking lease.

    Uses try/finally (not try/except Exception) so cleanup also runs when
    _HookTimeout (a BaseException) fires mid-write. Setting tmp_path to
    None after a successful os.replace prevents the finally clause from
    unlinking the already-renamed destination. Any exception encountered
    during the write propagates naturally after cleanup.

    A concurrent edit by a program that ignores our lease is handled on a best
    effort basis: the merge keeps what it saved before our read, and a
    size/mtime/inode check right before ``os.replace`` re-merges once if it
    saved in between. That narrows the window to milliseconds; it cannot
    close it, because an unlocked writer is not stopped.

    Returns True iff the write actually landed, False when the advisory lease
    was denied and nothing was written. Callers that report
    success to the user MUST check this -- a lease miss is logged to
    ``DAEMON_LOG_DIR/settings-lease-denials.log`` and sets ``last_refusal``
    so callers can distinguish lease denial from guard refusal (write-return
    audit 2026-08-29). All 22 call sites now check the return value; there
    are no remaining fire-and-forget callers.

    Callers that already hold ``_settings_lock()`` (e.g.
    ``_reconcile_sessionend_fossils``) MUST call
    ``_write_settings_atomic_locked`` instead -- this primitive would
    re-acquire the non-reentrant lease and deadlock.
    """
    _measure_facade = _compat.facade()
    snapshot = getattr(_measure_facade._SETTINGS_WRITE_READ_STATE, "snapshot", None)
    _measure_facade._SETTINGS_WRITE_READ_STATE.snapshot = None
    # A reason left by an earlier write must not explain this one.
    _measure_facade._SETTINGS_WRITE_READ_STATE.last_refusal = None
    with _measure_facade._settings_lock(user_initiated=user_initiated) as acquired:
        if not acquired:
            # Lease denial was completely silent (write-return audit 2026-08-29).
            # Log a durable breadcrumb and set last_refusal so callers can
            # distinguish "lease denied" from "guard refused".
            _measure_facade._log_settings_lease_denied()
            return False
        # Fingerprint the file before the merge reads it, then re-check right
        # before os.replace. If another program saved in between, re-merge onto
        # what it saved (two tries), so its edit is not written over. This
        # narrows the window to the stat-to-replace gap (milliseconds); an
        # editor that ignores our lease can still win inside that gap.
        for _attempt in range(_measure_facade._SETTINGS_WRITE_TRIES):
            identity = _measure_facade._settings_file_identity() if snapshot else None
            payload = _measure_facade._merge_concurrent_settings(snapshot, settings_data, allow_removing_keys)
            if payload is None:
                payload = settings_data
            if _measure_facade._write_settings_atomic_locked(payload, allow_removing_keys,
                                             _report_refusal=False,
                                             expect_identity=identity):
                return True
            if not getattr(_measure_facade._SETTINGS_WRITE_READ_STATE, "identity_changed", False):
                break
        else:
            _measure_facade._SETTINGS_WRITE_READ_STATE.last_refusal = _measure_facade._SETTINGS_CHANGED_REFUSAL
            _measure_facade._report_settings_write_refusal(_measure_facade._SETTINGS_CHANGED_REFUSAL)
            return False

        if getattr(_measure_facade._SETTINGS_WRITE_READ_STATE, "os_error", False):
            # The OS refused the temp write or the replace; a re-merge cannot
            # help and the guard did not refuse, so say nothing more. The
            # reason is in last_refusal for the caller to report.
            return False

        refusal = getattr(_measure_facade._SETTINGS_WRITE_READ_STATE, "last_refusal", None)

        def refuse():
            _measure_facade = _compat.facade()
            if refusal:
                _measure_facade._report_settings_write_refusal(refusal)
            return False

        # Callers commonly read settings before acquiring the lease. If another
        # writer added a top-level key in that interval, the guard correctly
        # refuses the stale payload. Only a payload derived from a recorded
        # read may be retried; a direct destructive payload must remain refused.
        if not isinstance(settings_data, dict) or not snapshot:
            return refuse()
        snapshot_path, base = snapshot
        try:
            current_path = str(_measure_facade.SETTINGS_PATH.resolve(strict=False))
        except (OSError, ValueError):
            current_path = str(_measure_facade.SETTINGS_PATH)
        if snapshot_path != current_path or not isinstance(base, dict):
            return refuse()

        removed = set(base) - set(settings_data)
        allowed = set(allow_removing_keys or ())
        if removed - allowed:
            return refuse()

        identity = _measure_facade._settings_file_identity()
        fresh, fresh_ok = _measure_facade._read_settings_for_write()
        if not fresh_ok or not isinstance(fresh, dict):
            return refuse()
        changed = {
            key: value
            for key, value in settings_data.items()
            if key not in base or base[key] != value
        }
        merged = dict(fresh)
        merged.update(changed)
        for key in allowed:
            if key not in settings_data:
                merged.pop(key, None)
        if _measure_facade._write_settings_atomic_locked(merged, allow_removing_keys, expect_identity=identity):
            return True
        if getattr(_measure_facade._SETTINGS_WRITE_READ_STATE, "identity_changed", False):
            _measure_facade._SETTINGS_WRITE_READ_STATE.last_refusal = _measure_facade._SETTINGS_CHANGED_REFUSAL
            _measure_facade._report_settings_write_refusal(_measure_facade._SETTINGS_CHANGED_REFUSAL)
        return False


def _autocompact_pct_override_value(settings=None):
    """Read-only lookup of CLAUDE_AUTOCOMPACT_PCT_OVERRIDE.

    Returns ``(value, source)``: the raw string value and where it came from
    (``"process env"``, ``"settings env"``, or ``(None, None)`` when unset).
    Never writes, never mutates the passed dict.
    """
    _measure_facade = _compat.facade()
    env_val = _measure_facade.os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if env_val is not None:
        return env_val, "process env"
    if settings is None:
        settings, _ok = _measure_facade._read_settings_for_write()
    if isinstance(settings, dict):
        env_block = settings.get("env")
        if isinstance(env_block, dict) and "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in env_block:
            return env_block.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"), "settings env"
    return None, None


def _autocompact_pct_override_explanation(settings=None):
    """Explain-only: what CLAUDE_AUTOCOMPACT_PCT_OVERRIDE does, per the docs.

    Returns a list of check tuples ``(status, name, detail)`` for doctor.
    Read-only: this must never touch settings.json.
    """
    _measure_facade = _compat.facade()
    value, source = _measure_facade._autocompact_pct_override_value(settings)
    if value is None:
        return [("OK", "Env vars",
                 "no CLAUDE_AUTOCOMPACT_PCT_OVERRIDE override (not set; "
                 "documented: sets the used %% of the compact window at which "
                 "auto-compaction runs, lower = earlier)")]
    detail = (
        f"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={value} ({source}) is DOCUMENTED: "
        "auto-compaction runs when this percentage of the compact window is "
        "already USED, so a lower value compacts EARLIER; it cannot raise "
        "the threshold. Kept as-is; Token Optimizer never edits it."
    )
    try:
        pct = int(str(value).strip())
    except (TypeError, ValueError):
        return [("!!", "Env override", detail + " (value is not an integer 1-100)")]
    if not 1 <= pct <= 100:
        return [("!!", "Env override", detail + " (outside the documented 1-100 range)")]
    if pct <= _measure_facade._AUTOCOMPACT_PCT_LOW_NOTE:
        detail += (
            f" NOTE: {pct}% is very low -- sessions will compact at {pct}% "
            "of the compact window, very early. Raise it (or remove the "
            "override) if compactions feel too frequent."
        )
    return [("OK", "Env override", detail)]


def _is_token_optimizer_session_end_hook(hook: dict) -> bool:
    """True when a single hook entry is Token Optimizer's SessionEnd hook.

    Matches the same command signature ``_is_hook_installed`` recognizes:
    a command invoking ``measure.py`` with the ``collect`` or
    ``session-end-flush`` subcommand as its first positional argument
    (``_TO_SESSION_END_CMD_RE``). The path may be quoted. Non-dict entries
    are left untouched (returned False) so a malformed entry never gets
    silently dropped.
    """
    _measure_facade = _compat.facade()
    if not isinstance(hook, dict):
        return False
    cmd = hook.get("command", "")
    if not isinstance(cmd, str):
        return False
    return bool(_measure_facade._TO_SESSION_END_CMD_RE.search(cmd))


def _remove_token_optimizer_session_end_hooks(settings: dict) -> int:
    """Strip Token Optimizer's SessionEnd hooks from ``settings`` in place.

    Mirrors the proven filter pattern in ``setup_smart_compact --uninstall``:
    iterate the ``SessionEnd`` event only, drop hook entries whose command is
    Token Optimizer's, keep every other hook (the user's own SessionEnd hooks
    and ALL other hook events are left intact). Empty groups are removed; if
    ``SessionEnd`` ends up empty, the key is dropped. Returns the count of
    Token Optimizer hook entries removed. Never raises.
    """
    _measure_facade = _compat.facade()
    if not isinstance(settings, dict):
        return 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    session_end = hooks.get("SessionEnd")
    if not isinstance(session_end, list) or not session_end:
        return 0
    removed = 0
    new_groups = []
    for group in session_end:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        hook_list = group.get("hooks")
        if not isinstance(hook_list, list):
            new_groups.append(group)
            continue
        kept_hooks = [h for h in hook_list if not _measure_facade._is_token_optimizer_session_end_hook(h)]
        removed += len(hook_list) - len(kept_hooks)
        if kept_hooks:
            group["hooks"] = kept_hooks
            new_groups.append(group)
    if new_groups:
        hooks["SessionEnd"] = new_groups
    else:
        hooks.pop("SessionEnd", None)
    return removed


def _sessionend_has_current_flush(settings: dict) -> bool:
    """True when settings.json already has a session-end-flush SessionEnd hook."""
    _measure_facade = _compat.facade()
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    session_end = hooks.get("SessionEnd")
    if not isinstance(session_end, list):
        return False
    for group in session_end:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []) or []:
            if isinstance(hook, dict) and _measure_facade._sessionend_cmd_is_flush(hook.get("command", "")):
                return True
    return False


def _rewrite_collect_fossil_hook(hook: dict) -> dict:
    """Rewrite a collect/dashboard fossil in place to HOOK_COMMAND.

    Preserves type/async/timeout when present. Fossils written before
    d1b0b53 lack async — add it so the host does not wait synchronously.
    """
    _measure_facade = _compat.facade()
    rewritten = dict(hook)
    rewritten["type"] = hook.get("type") or "command"
    rewritten["command"] = _measure_facade.HOOK_COMMAND
    if "async" not in rewritten:
        rewritten["async"] = True
    return rewritten


def _apply_sessionend_fossil_reconcile(settings, result):
    """Apply the fossil heal mutation to ``settings`` in place.

    Returns True if anything changed. The ``result`` counters (``rewritten`` /
    ``removed`` / ``stop_removed``) are incremented for the changes made in
    THIS call only. Idempotent: a second call on an already-healed settings
    is a no-op and returns False.

    Extracted from ``_reconcile_sessionend_fossils`` so the caller can re-read
    fresh under the lease and re-apply instead of writing a
    snapshot that was read unlocked. If a current-shape session-end-flush
    SessionEnd hook already exists, collect/dashboard fossils are removed.
    Otherwise each fossil is rewritten in place to HOOK_COMMAND (preserving
    async/timeout; adding async:true when absent). Also drops Stop-event
    collect / bare session-end-flush duplicates when the plugin already
    provides Stop.
    """
    _measure_facade = _compat.facade()
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False

    has_current = _measure_facade._sessionend_has_current_flush(settings)
    mutated = False

    session_end = hooks.get("SessionEnd")
    if isinstance(session_end, list) and session_end:
        new_groups = []
        for group in session_end:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            hook_list = group.get("hooks")
            if not isinstance(hook_list, list):
                new_groups.append(group)
                continue
            kept = []
            for hook in hook_list:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                cmd = hook.get("command", "")
                if not _measure_facade._sessionend_cmd_is_collect_fossil(cmd):
                    kept.append(hook)
                    continue
                if has_current:
                    result["removed"] += 1
                    mutated = True
                    continue
                kept.append(_measure_facade._rewrite_collect_fossil_hook(hook))
                result["rewritten"] += 1
                mutated = True
                has_current = True
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_groups.append(group)
        if mutated:
            if new_groups:
                hooks["SessionEnd"] = new_groups
            else:
                hooks.pop("SessionEnd", None)

    # Stop event: drop collect fossils and extra session-end-flush entries
    # when a plugin-provided (or already-present) Stop flush exists. The
    # first flush-shaped Stop hook is kept; extras and collect fossils go.
    stop = hooks.get("Stop")
    if isinstance(stop, list) and stop:
        plugin_provides_stop_flush = False
        try:
            if _measure_facade._is_plugin_installed():
                plugin_hooks_path = _measure_facade._find_plugin_hooks_json()
                if plugin_hooks_path:
                    plugin_hooks = _measure_facade.json.loads(plugin_hooks_path.read_text(encoding="utf-8"))
                    for group in (plugin_hooks.get("hooks") or {}).get("Stop") or []:
                        if not isinstance(group, dict):
                            continue
                        for hook in group.get("hooks") or []:
                            if isinstance(hook, dict) and _measure_facade._sessionend_cmd_is_flush(
                                hook.get("command", "")
                            ):
                                plugin_provides_stop_flush = True
                                break
        except (OSError, _measure_facade.json.JSONDecodeError, TypeError, ValueError):
            plugin_provides_stop_flush = False

        saw_stop_flush = False
        new_stop_groups = []
        stop_mutated = False
        for group in stop:
            if not isinstance(group, dict):
                new_stop_groups.append(group)
                continue
            hook_list = group.get("hooks")
            if not isinstance(hook_list, list):
                new_stop_groups.append(group)
                continue
            kept = []
            for hook in hook_list:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                cmd = hook.get("command", "")
                is_collect = _measure_facade._sessionend_cmd_is_collect_fossil(cmd)
                is_flush = _measure_facade._sessionend_cmd_is_flush(cmd)
                if is_collect:
                    result["stop_removed"] += 1
                    stop_mutated = True
                    continue
                if is_flush:
                    if plugin_provides_stop_flush or saw_stop_flush:
                        result["stop_removed"] += 1
                        stop_mutated = True
                        continue
                    saw_stop_flush = True
                kept.append(hook)
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_stop_groups.append(group)
        if stop_mutated:
            mutated = True
            if new_stop_groups:
                hooks["Stop"] = new_stop_groups
            else:
                hooks.pop("Stop", None)

    return mutated


def _reconcile_sessionend_fossils():
    """Rename-aware heal for the collect && dashboard settings.json fossil.

    Reachability (honest):
      * Plugin installs: the SessionStart ``ensure-health`` hook
        (hooks/hooks.json) calls run_ensure_health, which calls this.
      * NEW script installs: the updated examples/hooks-starter.json
        SessionStart command runs ensure-health, which calls this.
      * EXISTING script installs: their old settings.json has the fossil and
        NO ensure-health hook, so run_ensure_health never runs for them. They
        are reached by the self-heal in _dispatch_collect: when their
        fossil invokes post-fix measure.py ``collect`` on the hook path, that
        run calls this once (throttled, fail-open) and removes/rewrites the
        fossil going forward. A separate pre-fix measure.py copy on disk is
        NOT reached until the user updates -- no code path can reach a copy
        that never runs this module.

    If a current-shape session-end-flush SessionEnd hook already exists,
    fossils are removed. Otherwise each fossil is rewritten in place to
    HOOK_COMMAND (preserving async/timeout; adding async:true when absent).
    Also drops Stop-event collect / bare session-end-flush duplicates when
    the plugin already provides Stop. Atomic, backed up, fail-open. Returns a
    result dict so tests can assert the decision.

    The initial read is unlocked (a cheap probe to
    decide whether any heal work exists, avoiding lease/backup churn when
    there is none). Once work is found, ``_settings_lock()`` is held across
    fresh-read + re-apply + atomic-write as ONE critical section, so a
    concurrent writer (another hook, the user, a settings sync) cannot land
    between the fresh read and the write and be clobbered -- the previous
    code re-read OUTSIDE the lease and only _write_settings_atomic acquired
    it internally, leaving a lost-update window. The write uses
    ``_write_settings_atomic_locked``, which does NOT reacquire the lease
    (the lease is non-reentrant; reacquiring it here would deadlock). A
    denied lease or a bad re-read is a silent no-op (fail-open): the fossil
    persists, which is recoverable; an overwritten settings.json is not.
    """
    _measure_facade = _compat.facade()
    result = {"rewritten": 0, "removed": 0, "stop_removed": 0, "reason": "ok"}
    try:
        settings, _, ok = _measure_facade._read_settings_json_checked()
        if not ok:
            result["reason"] = "settings_unreadable"
            return result
        if not settings:
            result["reason"] = "no_settings"
            return result
        if not isinstance(settings.get("hooks"), dict):
            result["reason"] = "no_hooks"
            return result

        # Unlocked probe: decide whether any heal work exists without taking
        # the lease or writing a backup. `settings` is mutated in place here
        # but discarded after the decision -- the authoritative mutation is
        # re-applied to a fresh read under the lease below.
        if not _measure_facade._apply_sessionend_fossil_reconcile(settings, result):
            result["reason"] = "nothing_to_do"
            return result

        # Hold _settings_lock() across fresh-read +
        # re-apply + atomic-write as ONE critical section. The previous code
        # re-read OUTSIDE the lease and only _write_settings_atomic acquired
        # it internally, so a concurrent writer could land between the fresh
        # read and the lease acquisition and be clobbered. The write uses
        # _write_settings_atomic_locked, which performs the tempfile+replace
        # WITHOUT reacquiring _settings_lock() (the lease is non-reentrant;
        # reacquiring it here would deadlock). A denied lease or bad re-read
        # is fail-open (the fossil stays; recoverable) and retries on the
        # next hook fire.
        with _measure_facade._settings_lock() as acquired:
            if not acquired:
                result["reason"] = "write_skipped"
                return result

            # Backup the on-disk state we are about to overwrite, taken UNDER
            # the lease so it reflects exactly the pre-mutation file (a
            # backup taken outside the lease could capture a state a
            # concurrent writer already superseded).
            backup_dir = _measure_facade.CLAUDE_DIR / "_backups" / "token-optimizer"
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                ts = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_path = backup_dir / f"settings.json.pre-fossil-reconcile-{ts}"
                if _measure_facade.SETTINGS_PATH.exists():
                    import shutil
                    shutil.copy2(str(_measure_facade.SETTINGS_PATH), str(backup_path))
            except OSError:
                pass

            # Fresh read UNDER the lease; reset counters so they reflect only
            # this re-application, not the unlocked probe.
            fresh, _, read_ok = _measure_facade._read_settings_json_checked()
            result["rewritten"] = 0
            result["removed"] = 0
            result["stop_removed"] = 0
            if not read_ok or not fresh:
                result["reason"] = "write_skipped"
                return result
            if not isinstance(fresh.get("hooks"), dict):
                result["reason"] = "no_hooks"
                return result
            if not _measure_facade._apply_sessionend_fossil_reconcile(fresh, result):
                result["reason"] = "nothing_to_do"
                return result
            if not _measure_facade._write_settings_atomic_locked(fresh):
                result["reason"] = "write_skipped"
                return result
        return result
    except Exception:
        result["reason"] = "fail_open"
        return result


def _maybe_self_heal_sessionend_fossils_on_hook():
    """Hook-path self-heal for the collect/dashboard fossil.

    The SessionStart ``ensure-health`` hook reaches plugin installs and NEW
    script installs (via the updated examples/hooks-starter.json). An EXISTING
    script install has an old settings.json with the ``collect && dashboard``
    fossil and NO ``ensure-health`` hook, so ``run_ensure_health`` never runs
    for it and ``_reconcile_sessionend_fossils`` is unreachable from there.
    The fossil's OWN run closes that gap: when post-fix measure.py runs
    ``collect`` on the hook path, this opportunistically calls
    ``_reconcile_sessionend_fossils`` once, so the fossil's own execution
    removes/rewrites it in settings.json going forward -- reaching the
    population the SessionStart hook cannot.

    Throttled to once per 24h via the shared ``last_hook_heal_check`` config
    flag (the same flag run_ensure_health uses), so it does not run on every
    hook fire. Fail-open and time-bounded: any error is swallowed so a
    reconcile failure can never affect the hook flush, and the reconcile runs
    under its own 10s HookDeadline so a stalled settings write cannot hold the
    hook's stdout pipe open (os._exit(0) on expiry EOFs the pipe). A lease miss
    (``reason == "write_skipped"``) leaves the flag untouched so the next
    hook fire retries; any other outcome (healed, nothing-to-do,
    unreadable) advances the flag to suppress redundant retries for 24h.

    Never raises. Called from ``_dispatch_collect`` AFTER the collect flush
    and its 20s budget are cleared, so it never delays or blocks the flush.
    """
    # Bound the ENTIRE self-heal with one short deadline, installed before the
    # first filesystem touch (the throttle read) and cleared only after the
    # last (the throttle write). Every step here -- _read_config_flag, the
    # reconcile's settings read/backup/write, and _write_config_flag's own
    # dir-create/lease/temp-write/os.replace -- is synchronous I/O that, on a
    # stalled filesystem, would otherwise hold the hook's stdout pipe open
    # unbounded and re-wedge the very hang the fossil fix prevents (the collect budget was
    # already cleared by the caller). HookDeadline os._exit(0)s on expiry, so
    # the process exits and the pipe EOFs no matter WHICH step stalls; the flag
    # stays unadvanced so the heal retries on the next hook fire.
    _measure_facade = _compat.facade()
    deadline = _measure_facade._install_hook_budget(10)
    try:
        last_check = _measure_facade._read_config_flag("last_hook_heal_check", 0)
        now = int(_measure_facade.time.time())
        if now - int(last_check or 0) <= 86400:  # 24h
            return
        fossil = _measure_facade._reconcile_sessionend_fossils()
        # write_skipped = lease denied or fresh read failed -> retry next fire.
        if fossil.get("reason", "ok") != "write_skipped":
            _measure_facade._write_config_flag("last_hook_heal_check", now)
    except Exception:
        pass
    finally:
        _measure_facade._clear_hook_budget(deadline)


def setup_hook(dry_run=None, uninstall=None, user_initiated=None):
    """Install or uninstall the SessionEnd hook for automatic usage collection.

    ``uninstall=True`` removes ONLY Token Optimizer's own SessionEnd hook
    entries from ``settings.json`` (commands containing ``measure.py`` plus
    ``collect`` or ``session-end-flush``), leaving the user's other SessionEnd
    hooks and all other hook events intact. Mirrors the proven filter pattern
    in ``setup_smart_compact --uninstall``. Supports ``--dry-run``.

    setup-hook targets ~/.claude/settings.json with a Claude Code hook that
    uses {"async": true}. Async hooks are a Claude Code feature; Codex skips
    them (see codex_doctor) and other runtimes use their own installers. So
    never write this hook under a non-Claude runtime — Codex must go through
    codex-install, which writes a synchronous .codex/hooks.json entry.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() != "claude":
        # Informational on every path (including dry-run / uninstall) so a
        # non-Claude invocation isn't a silent no-op.
        print(
            "[Token Optimizer] setup-hook targets Claude Code (settings.json). "
            "For Codex use codex-install; other runtimes use their own installer."
        )
        return

    # Load existing settings
    settings = {}
    if _measure_facade.SETTINGS_PATH.exists():
        if not _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
            # Never open() a FIFO: it blocks forever.
            print(f"[Error] Could not read {_measure_facade.SETTINGS_PATH}: it is not a regular file.")
            _measure_facade.sys.exit(1)
        try:
            with open(_measure_facade.SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                settings = _measure_facade.json.load(f)
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError) as e:
            print(f"[Error] Could not read {_measure_facade.SETTINGS_PATH}: {e}")
            _measure_facade.sys.exit(1)

    if uninstall:
        removed = _measure_facade._remove_token_optimizer_session_end_hooks(settings)
        if dry_run:
            print(f"\n  [Dry run] Would remove {removed} Token Optimizer SessionEnd hook(s) from {_measure_facade.SETTINGS_PATH}")
            print("  Run without --dry-run to apply.\n")
            return
        if removed == 0:
            print("[Token Optimizer] No Token Optimizer SessionEnd hook found in settings.json. Nothing to remove.")
            return
        # Backup settings.json before mutating (mirrors the install path).
        backup_dir = _measure_facade.CLAUDE_DIR / "_backups" / "token-optimizer"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"settings.json.pre-hook-uninstall-{ts}"
        if _measure_facade.SETTINGS_PATH.exists():
            import shutil
            shutil.copy2(str(_measure_facade.SETTINGS_PATH), str(backup_path))
        try:
            if not _measure_facade._write_settings_atomic(settings, user_initiated=user_initiated):
                print("[Error] settings.json write was refused; no SessionEnd hooks were removed.", file=_measure_facade.sys.stderr)
                return
            print(f"[Token Optimizer] Removed {removed} Token Optimizer SessionEnd hook(s) from settings.json.")
            print(f"  Backup: {backup_path}")
            print("  Other hooks (yours and other tools') were left intact.")
        except PermissionError:
            print(f"[Error] Permission denied writing {_measure_facade.SETTINGS_PATH}.")
            _measure_facade.sys.exit(1)
        return

    # Check if hook is installed and whether it needs upgrading
    installed = _measure_facade._is_hook_installed(settings)
    current = _measure_facade._is_hook_current(settings)

    # Plugin users get this hook from hooks.json — skip writing to settings.json
    is_plugin = _measure_facade._is_running_from_plugin_cache() or _measure_facade._is_plugin_installed()
    if is_plugin:
        if installed:
            print("[Token Optimizer] SessionEnd hook active via plugin hooks.json. Nothing to do.")
        else:
            print("[Token Optimizer] Running as plugin. SessionEnd hook managed by hooks.json.")
        return

    if installed and current:
        print("[Token Optimizer] SessionEnd hook already installed and up to date. Nothing to do.")
        return

    upgrading = installed and not current

    # Build the hook entry
    new_hook = {"type": "command", "command": _measure_facade.HOOK_COMMAND, "async": True}

    if "hooks" not in settings:
        settings["hooks"] = {}

    hooks = settings["hooks"]

    if upgrading:
        # Replace the collect/dashboard fossil with session-end-flush.
        session_end = hooks.get("SessionEnd", [])
        if isinstance(session_end, list):
            for entry in session_end:
                hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
                for i, hook in enumerate(hook_list):
                    cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                    if _measure_facade._sessionend_cmd_is_collect_fossil(cmd) or (
                        "measure.py" in cmd and "collect" in cmd
                    ):
                        hook_list[i] = new_hook
                        break
    elif "SessionEnd" not in hooks:
        hooks["SessionEnd"] = [{"hooks": [new_hook]}]
    else:
        session_end = hooks["SessionEnd"]
        if isinstance(session_end, list) and len(session_end) > 0:
            first_entry = session_end[0]
            if isinstance(first_entry, dict):
                if "hooks" not in first_entry:
                    first_entry["hooks"] = []
                first_entry["hooks"].append(new_hook)
            else:
                session_end.append({"hooks": [new_hook]})
        else:
            hooks["SessionEnd"] = [{"hooks": [new_hook]}]

    if dry_run:
        action = "upgrade" if upgrading else "install"
        print(f"[Token Optimizer] Dry run. Would {action} a SessionEnd hook.\n")
        print("  What it does:")
        print("    When you close a Claude Code session, it automatically:")
        print("    1. Saves your session stats (skills used, tokens, model mix)")
        print("    2. Refreshes your dashboard with the latest data\n")
        print("  Where data is stored:")
        print(f"    {_measure_facade.SNAPSHOT_DIR / 'trends.db'}")
        print(f"    {_measure_facade.DASHBOARD_PATH}\n")
        print("  JSON that would be added to settings.json:")
        print(_measure_facade.json.dumps(hooks.get("SessionEnd", []), indent=2))
        print("\n  No changes written.")
        return

    # Backup settings.json
    backup_dir = _measure_facade.CLAUDE_DIR / "_backups" / "token-optimizer"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"settings.json.pre-hook-{ts}"
    if _measure_facade.SETTINGS_PATH.exists():
        import shutil
        shutil.copy2(str(_measure_facade.SETTINGS_PATH), str(backup_path))

    # Write atomically
    try:
        if not _measure_facade._write_settings_atomic(settings, user_initiated=user_initiated):
            print("[Error] settings.json write was refused; SessionEnd hook was not installed.", file=_measure_facade.sys.stderr)
            return
        action = "upgraded" if upgrading else "installed"
        print(f"[Token Optimizer] SessionEnd hook {action}.")
        print(f"  Backup: {backup_path}")
        print("  Hook collects data + regenerates dashboard after each session.")
        print(f"  Dashboard: {_measure_facade.DASHBOARD_PATH}")
    except PermissionError:
        print(f"[Error] Permission denied writing {_measure_facade.SETTINGS_PATH}.")
        print("Add this manually to your settings.json hooks.SessionEnd:\n")
        print(_measure_facade.json.dumps({"type": "command", "command": _measure_facade.HOOK_COMMAND, "async": True}, indent=2))
        _measure_facade.sys.exit(1)


def _find_plugin_hooks_json():
    """Locate the plugin's hooks.json. Returns Path or None."""
    _measure_facade = _compat.facade()
    for candidate in _measure_facade._HOOKS_JSON_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _resolve_hook_command(template_cmd, plugin_root):
    """Resolve ${CLAUDE_PLUGIN_ROOT} and similar placeholders to absolute paths.

    Script installs don't use Claude Code's plugin loader, so we need to
    substitute ${CLAUDE_PLUGIN_ROOT} ourselves with the install directory.

    Claude Code runs hooks through Git Bash on Windows too, so the bash
    launcher template is already the correct form there. The only
    Windows-specific hardening is substituting a forward-slash root, so
    backslash sequences in user names (\\t, \\n, ...) can't be mangled inside
    bash double quotes. The pre-fix native cmd.exe conversion (list2cmdline +
    cmd null redirect) created a literal NUL file and silently broke hooks.
    """
    _measure_facade = _compat.facade()
    if not template_cmd:
        return template_cmd
    root = str(plugin_root)
    if _measure_facade.platform.system() == "Windows":
        root = root.replace("\\", "/")
    # No shell-metachar escaping needed here: the sole caller (setup_all_hooks)
    # rejects roots containing ' " ` $ ; & | < > (SEC-001) before this runs, so
    # the substituted root is always inert in the bash double-quote context.
    resolved = template_cmd.replace("${CLAUDE_PLUGIN_ROOT}", root)
    resolved = resolved.replace("$CLAUDE_PLUGIN_ROOT", root)
    return resolved


def _windows_hook_command_is_stale(existing_cmd, resolved_cmd):
    """True when SessionStart should replace a legacy Windows hook command.

    Legacy means the pre-fix native cmd.exe form (list2cmdline argv + a cmd
    null redirect), which fails under Git Bash. The current form is the bash
    launcher, identical in shape to POSIX.
    """
    _measure_facade = _compat.facade()
    return (
        _measure_facade.platform.system() == "Windows"
        and existing_cmd != resolved_cmd
        and _measure_facade.re.search(r">\s*NUL\b", existing_cmd) is not None
    )


def _hook_command_identity(cmd):
    """Extract a stable functional identity from a hook command for dedup.

    Tokenizes with shlex, finds the LAST .py file (skipping `test -f ...`
    guards), and returns: script_name + first subcommand + first mode flag,
    ignoring cosmetic flags like --quiet/--warn/--verbose.

    Examples:
      'python3 measure.py quality-cache --warn --quiet' -> 'measure.py:quality-cache'
      'python3 measure.py quality-cache --quiet' -> 'measure.py:quality-cache' (DEDUPS)
      'python3 read_cache.py --quiet' -> 'read_cache.py'
      'python3 read_cache.py --clear --quiet' -> 'read_cache.py:--clear'
      'python3 read_cache.py --invalidate --quiet' -> 'read_cache.py:--invalidate'
      'echo COMPACTION GUIDANCE...' -> 'echo:COMPACTION'
    """
    _measure_facade = _compat.facade()
    if not cmd:
        return ""

    # The plugin's version resolver (D="$(ls -d ... | sort -V | tail -n1)") is
    # plumbing, not identity: drop that one flat $(...) so a resolver command
    # keeps the identity of its handler instead of collapsing to "run.py" for
    # every hook (which would make setup_all_hooks/dedup treat all of an event's
    # hooks as one).
    if 'D="$(ls -d ' in cmd:
        cmd = _measure_facade.re.sub(r'D="\$\(ls -d [^()]*\)"', 'D=""', cmd)

    # Subshell fallback: commands with $(...) or backticks confuse shlex —
    # shell noise (2>/dev/null, |, sort, -V) leaks into identity tokens.
    # Extract .py name via regex and find subcommand/flags after the closing ).
    # Uses re.search (first match) unlike the shlex path (last .py token);
    # known malformed patterns have only one .py token so this is equivalent.
    if "$(" in cmd or "`" in cmd:
        py_match = _measure_facade.re.search(r'([\w.-]+\.py)', cmd)
        if py_match:
            script_name = py_match.group(1)
            # Find tokens after the subshell close (after last ) or `)
            after = ""
            last_paren = cmd.rfind(")")
            last_bt = cmd.rfind("`")
            cut = max(last_paren, last_bt)
            if cut >= 0 and cut + 1 < len(cmd):
                after = cmd[cut + 1:]
            after_tokens = after.split()
            clean = [t for t in after_tokens
                     if t not in _measure_facade._COSMETIC_FLAGS and t not in _measure_facade._SHELL_NOISE
                     and not t.startswith("2>")]
            subcmd = ""
            mode_flag = ""
            for t in clean:
                if not t.startswith("-"):
                    subcmd = t
                    break
            for t in clean:
                if t.startswith("-") and t not in _measure_facade._COSMETIC_FLAGS:
                    mode_flag = t
                    break
            if subcmd and mode_flag:
                return f"{script_name}:{subcmd}:{mode_flag}"
            elif subcmd:
                return f"{script_name}:{subcmd}"
            elif mode_flag:
                return f"{script_name}:{mode_flag}"
            return script_name

    try:
        tokens = _measure_facade.shlex.split(cmd, posix=True)
    except ValueError:
        return cmd[:80]

    # Strip `|| true` / `&&` trailers
    while tokens and tokens[-1] in ("||", "true", "&&"):
        tokens.pop()

    # Find the LAST .py token (skips `test -f '<path>.py'` guard)
    py_idx = -1
    for i, t in enumerate(tokens):
        if t.endswith(".py"):
            py_idx = i

    if py_idx < 0:
        # Non-python hook (echo, curl, etc). Use script name + first meaningful token.
        for i, t in enumerate(tokens):
            if t not in ("test", "-f", "&&", "||", "true", "python3", "python", "bash", "-c"):
                # Found the command name -- use it + next word as identity
                next_word = tokens[i + 1] if i + 1 < len(tokens) else ""
                # For echo, use first word of content as discriminator
                if t == "echo" and next_word:
                    first_word = next_word.split()[0] if next_word.split() else ""
                    return f"echo:{first_word[:20]}"
                return f"{t}:{next_word[:20]}"
        return cmd[:80]

    script_name = _measure_facade.Path(tokens[py_idx]).name
    tail = tokens[py_idx + 1:]
    if not tail:
        return script_name

    # First non-flag token = subcommand (e.g. "quality-cache", "compact-capture")
    subcmd = ""
    for t in tail:
        if not t.startswith("-"):
            subcmd = t
            break

    # First MODE flag (non-cosmetic) = discriminator for scripts without subcommands
    mode_flag = ""
    for t in tail:
        if t.startswith("-") and t not in _measure_facade._COSMETIC_FLAGS:
            mode_flag = t
            break

    # Always include mode_flag in identity when present, even if
    # subcmd exists. This distinguishes 'measure.py compact-restore' from
    # 'measure.py compact-restore --new-session-only' which otherwise collide.
    if subcmd and mode_flag:
        return f"{script_name}:{subcmd}:{mode_flag}"
    elif subcmd:
        return f"{script_name}:{subcmd}"
    elif mode_flag:
        return f"{script_name}:{mode_flag}"
    else:
        return script_name


def _cleanup_duplicate_plugin_hooks_from_settings(dry_run=None):
    """Remove token-optimizer hook commands from settings.json that the plugin
    already provides via its hooks.json.

    Context: users who installed via install.sh before the marketplace plugin
    existed (or alongside it) have settings.json entries AND plugin hooks.json
    entries for the same commands. Claude Code merges both, so every hook fires
    twice per event — wasted CPU, racy SQLite writes, undercounted savings.

    This is the inverse of setup_all_hooks: when the plugin is installed, we
    remove the duplicates from settings.json so the plugin's copy is the single
    source of truth.

    Safety:
    - Only runs when _is_plugin_installed() is True (refuses otherwise).
    - Only removes hooks whose (event, matcher, identity) tuple EXACTLY matches
      an entry in plugin hooks.json. User-custom or third-party hooks are
      preserved untouched.
    - Identity is computed via _hook_command_identity() which normalizes
      cosmetic flags (--quiet/--warn) so 'quality-cache --warn --quiet' and
      'quality-cache --quiet' dedup to the same identity.
    - Atomic: uses _write_settings_atomic with file lock.
    - Idempotent: returns {"removed": 0} when there are no duplicates to remove.
    - Preserves all hook fields and ordering for kept entries.

    Returns: {"removed": N, "reason": str, "dry_run": bool}
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._is_plugin_installed():
        return {"removed": 0, "reason": "plugin_not_installed", "dry_run": dry_run}

    hooks_json_path = _measure_facade._find_plugin_hooks_json()
    if not hooks_json_path:
        return {"removed": 0, "reason": "plugin_hooks_json_not_found", "dry_run": dry_run}

    try:
        plugin_hooks = _measure_facade.json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError) as e:
        return {"removed": 0, "reason": f"plugin_hooks_json_read_error: {e}", "dry_run": dry_run}

    desired = plugin_hooks.get("hooks", {})
    if not desired:
        return {"removed": 0, "reason": "plugin_hooks_empty", "dry_run": dry_run}

    # Build the set of (event, matcher, identity) tuples that the plugin provides.
    # Any settings.json hook matching one of these is a duplicate we can remove.
    plugin_identities = set()
    for event, handler_groups in desired.items():
        if not isinstance(handler_groups, list):
            continue
        for group in handler_groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher", "")
            for h in group.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                ident = _measure_facade._hook_command_identity(h.get("command", ""))
                if ident:
                    plugin_identities.add((event, matcher, ident))

    if not plugin_identities:
        return {"removed": 0, "reason": "no_plugin_identities", "dry_run": dry_run}

    # Read current settings.
    # Writes a dict derived from `current` back.
    current, _rs_ok = _measure_facade._read_settings_for_write()
    if not _rs_ok:
        current = {}
    current_hooks = current.get("hooks") if current else None
    if not current_hooks:
        return {"removed": 0, "reason": "no_settings_hooks", "dry_run": dry_run}

    # Safety: if settings.json exists non-empty but parsed empty,
    # refuse to touch it — concurrent write may have corrupted it.
    if not current:
        try:
            if _measure_facade.SETTINGS_PATH.exists() and _measure_facade.SETTINGS_PATH.stat().st_size > 0:
                return {"removed": 0, "reason": "settings_corrupted", "dry_run": dry_run}
        except OSError:
            pass

    # Walk each event and drop hooks whose identity matches the plugin.
    # Preserve all other hooks (including non-token-optimizer ones) untouched.
    removed = 0
    new_hooks = {}
    for event, handler_groups in current_hooks.items():
        if not isinstance(handler_groups, list):
            # Unknown shape — preserve as-is, don't touch it.
            new_hooks[event] = handler_groups
            continue
        new_groups = []
        for group in handler_groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            matcher = group.get("matcher", "")
            original_hook_list = group.get("hooks", [])
            if not isinstance(original_hook_list, list):
                new_groups.append(group)
                continue
            kept_hooks = []
            for h in original_hook_list:
                if not isinstance(h, dict):
                    kept_hooks.append(h)
                    continue
                ident = _measure_facade._hook_command_identity(h.get("command", ""))
                if ident and (event, matcher, ident) in plugin_identities:
                    removed += 1
                    continue
                kept_hooks.append(h)
            # Only keep the group if it still has hooks; an empty "hooks" array
            # is legal but pointless, so drop the whole group in that case.
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                new_groups.append(new_group)
        if new_groups:
            new_hooks[event] = new_groups
        # If every group in this event was removed, drop the event key too.

    if removed == 0:
        return {"removed": 0, "reason": "no_duplicates_found", "dry_run": dry_run}

    if dry_run:
        return {"removed": removed, "reason": "dry_run", "dry_run": True}

    new_settings = dict(current)
    new_settings["hooks"] = new_hooks
    try:
        if not _measure_facade._write_settings_atomic(new_settings):
            return {"removed": 0, "reason": "write_failed", "dry_run": False}
    except (PermissionError, OSError) as e:
        return {"removed": 0, "reason": f"write_failed: {e}", "dry_run": False}

    return {"removed": removed, "reason": "success", "dry_run": False}


def _cloud_kit_owns_hooks():
    """True when the Claude Code cloud kit wired this machine's hooks.

    In a cloud VM the kit's run.sh launches the real hooks with
    CLAUDE_PLUGIN_ROOT pointing at the npm package, and nothing marks that as a
    plugin install. Without this check the SessionStart self-heal sees "script
    install with missing hooks" and adds its canonical set next to the kit's,
    so every hook fires twice. The kit owns the hook map (generated from
    hooks.json and refreshed on every install), so self-heal stands down.
    Signals: the env var run.sh exports, or a kit runner in settings.json.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_CLOUD_KIT_VERSION"):
        return True
    try:
        current, _ok = _measure_facade._read_settings_for_write()
        hooks = (current or {}).get("hooks") or {}
        if not isinstance(hooks, dict):
            return False
        for groups in hooks.values():
            for group in groups if isinstance(groups, list) else []:
                for h in (group.get("hooks") or []) if isinstance(group, dict) else []:
                    if isinstance(h, dict) and _measure_facade._CLOUD_KIT_RUNNER_MARKER in str(h.get("command", "")):
                        return True
    except Exception:
        return False
    return False


def setup_all_hooks(dry_run=None, verbose=None):
    """Merge the plugin's hooks.json into the user's settings.json.

    This is the canonical way to install all Token Optimizer hooks for
    script-install users (install.sh) and for healing drift on existing
    installs (called from ensure-health at SessionStart).

    Returns a dict with counts: {"added": N, "skipped": N, "plugin_root": str}.

    Safety:
    - Idempotent: dedups by script identity, never adds duplicates
    - Atomic: uses _write_settings_atomic with file lock
    - Preserves all existing hooks (token-optimizer + third-party)
    - Preserves all hook fields (type, command, async, timeout, etc.)
    - Resolves ${CLAUDE_PLUGIN_ROOT} to absolute path for script installs
    - Refuses to write if settings.json appears corrupted (non-empty file but parses to {})
    - Rejects plugin_root containing shell metacharacters
    """
    _measure_facade = _compat.facade()
    if _measure_facade._cloud_kit_owns_hooks():
        if verbose:
            print("  [setup-all-hooks] hooks are managed by the Claude Code cloud kit; nothing to do")
        return {"added": 0, "skipped": 0, "plugin_root": None, "reason": "cloud_kit"}
    hooks_json_path = _measure_facade._find_plugin_hooks_json()
    if not hooks_json_path:
        if verbose:
            print("  [setup-all-hooks] plugin hooks.json not found")
        return {"added": 0, "skipped": 0, "plugin_root": None, "error": "hooks.json not found"}

    plugin_root = hooks_json_path.parent.parent
    plugin_root_str = str(plugin_root)

    # SEC-001 fix: reject plugin_root with shell metacharacters that would break
    # out of single-quoted strings in hook templates or inject commands.
    # v5.4.8: backslash is excluded on Windows since it's the legitimate path
    # separator (C:\Users\...\.claude\plugins\...). The remaining chars still
    # block shell-injection vectors on both platforms. UNC paths (\\server\...)
    # are rejected explicitly to prevent SMB auth leak via hook execution.
    _DANGEROUS_PATH_CHARS = set("'\"`$;&|<>\n\r\x00")
    if _measure_facade.platform.system() != "Windows":
        _DANGEROUS_PATH_CHARS.add("\\")
    elif plugin_root_str.startswith("\\\\") or plugin_root_str.startswith("//"):
        msg = f"Plugin root is a UNC path, refusing to install hooks: {plugin_root_str!r}"
        if verbose:
            print(f"  [setup-all-hooks] {msg}")
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": msg}
    if any(c in plugin_root_str for c in _DANGEROUS_PATH_CHARS):
        msg = f"Plugin root contains unsafe characters: {plugin_root_str!r}"
        if verbose:
            print(f"  [setup-all-hooks] {msg}")
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": msg}

    try:
        plugin_hooks = _measure_facade.json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"  [setup-all-hooks] could not read {hooks_json_path}: {e}")
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": str(e)}

    desired = plugin_hooks.get("hooks", {})
    if not desired:
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str}

    # Read current settings
    # Writes a dict derived from `current` back.
    current, _rs_ok = _measure_facade._read_settings_for_write()
    if not _rs_ok:
        current = {}

    # If settings.json exists with non-zero size but parsed as empty,
    # it was probably corrupted by a concurrent write. Refuse to overwrite.
    if not current:
        try:
            if _measure_facade.SETTINGS_PATH.exists() and _measure_facade.SETTINGS_PATH.stat().st_size > 0:
                msg = "settings.json exists with non-zero size but parsed empty (possible concurrent write). Refusing to overwrite."
                if verbose:
                    print(f"  [setup-all-hooks] {msg}")
                return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": msg}
        except OSError:
            pass

    current_hooks = dict(current.get("hooks", {}))

    added = 0
    skipped = 0

    for event, handler_groups in desired.items():
        if event not in current_hooks:
            current_hooks[event] = []

        # Build an index of existing command identities per matcher,
        # and also track the path used (for stale-path detection).
        existing_ids = {}  # matcher -> set of identity strings
        existing_entries = {}  # matcher -> {identity -> (group, hook_dict)}
        for group in current_hooks[event]:
            matcher = group.get("matcher", "")
            existing_ids.setdefault(matcher, set())
            existing_entries.setdefault(matcher, {})
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                ident = _measure_facade._hook_command_identity(cmd)
                if ident:
                    existing_ids[matcher].add(ident)
                    existing_entries[matcher][ident] = (group, h)

        for group in handler_groups:
            matcher = group.get("matcher", "")
            for h in group.get("hooks", []):
                template_cmd = h.get("command", "")
                resolved_cmd = _measure_facade._resolve_hook_command(template_cmd, plugin_root)
                ident = _measure_facade._hook_command_identity(resolved_cmd)

                if ident and ident in existing_ids.get(matcher, set()):
                    # SEC-003 fix: check if the existing hook points to our current
                    # plugin_root. Only applies to hooks that actually contain a path
                    # (i.e., .py scripts). Echo/bash hooks without paths always skip.
                    existing_group, existing_hook = existing_entries[matcher][ident]
                    existing_cmd = existing_hook.get("command", "")
                    has_path = ".py" in existing_cmd or ".py" in resolved_cmd
                    # On Windows, an existing command can point at the current
                    # root yet still be the legacy native cmd.exe form.
                    # Replace it with the Git-Bash launcher during ensure-health.
                    windows_command_stale = _measure_facade._windows_hook_command_is_stale(
                        existing_cmd, resolved_cmd
                    )
                    # Retrofit the fail-open launcher guard onto a pre-guard baked
                    # command even when the root is unchanged (a stable-dir script
                    # install upgraded in place keeps the same root, so root_in_cmd
                    # would otherwise skip it and it would never gain the guard).
                    # Fires only when the new template carries the guard and the
                    # existing command lacks it.
                    guard_stale = (
                        "[ -r " in resolved_cmd and "[ -r " not in existing_cmd
                    )
                    # _resolve_hook_command now embeds a
                    # forward-slash root on Windows, but plugin_root_str keeps
                    # native backslashes, so a raw substring test never matches
                    # post-fix and every hook would be "replaced" on every run
                    # (perpetual settings.json rewrite). Normalize separators.
                    root_in_cmd = (
                        plugin_root_str.replace("\\", "/") in existing_cmd.replace("\\", "/")
                    )
                    if (not has_path or root_in_cmd) and not windows_command_stale and not guard_stale:
                        skipped += 1
                        if verbose:
                            print(f"  [skip] {event}[{matcher}] {ident} (already present)")
                        continue
                    # Stale path -- replace in place
                    existing_hook["command"] = resolved_cmd
                    # Preserve all fields from new hook (async, timeout, etc.)
                    for k, v in h.items():
                        if k != "command":
                            existing_hook[k] = v
                    added += 1
                    if verbose:
                        print(f"  [replace] {event}[{matcher}] {ident} (stale path updated)")
                    continue

                # Find or create the matcher group
                target_group = None
                for g in current_hooks[event]:
                    if g.get("matcher", "") == matcher:
                        target_group = g
                        break
                if target_group is None:
                    target_group = {"hooks": []}
                    if matcher:
                        target_group["matcher"] = matcher
                    current_hooks[event].append(target_group)

                # Preserve all fields from source hook (async, timeout, type, etc.)
                new_hook = dict(h)
                new_hook["command"] = resolved_cmd
                target_group.setdefault("hooks", []).append(new_hook)
                existing_ids.setdefault(matcher, set()).add(ident)
                existing_entries.setdefault(matcher, {})[ident] = (target_group, new_hook)
                added += 1
                if verbose:
                    print(f"  [add]  {event}[{matcher}] {ident}")

    if added == 0:
        return {"added": 0, "skipped": skipped, "plugin_root": plugin_root_str}

    if dry_run:
        if verbose:
            print(f"  [dry-run] would add {added} hooks, skip {skipped}")
        return {"added": added, "skipped": skipped, "plugin_root": plugin_root_str, "dry_run": True}

    # Apply the merge
    new_settings = dict(current)
    new_settings["hooks"] = current_hooks

    try:
        if not _measure_facade._write_settings_atomic(new_settings):
            if verbose:
                print("  [setup-all-hooks] settings.json write was refused; no hooks added.")
            return {"added": 0, "skipped": skipped, "plugin_root": plugin_root_str, "error": "write refused"}
    except (PermissionError, OSError) as e:
        if verbose:
            print(f"  [setup-all-hooks] could not write settings.json: {e}")
        return {"added": 0, "skipped": skipped, "plugin_root": plugin_root_str, "error": str(e)}

    # Record the heal timestamp so ensure-health's 24h throttle
    # suppresses the redundant run after install.sh.
    try:
        _measure_facade._write_config_flag("last_hook_heal_check", int(_measure_facade.time.time()))
    except Exception:
        pass

    return {"added": added, "skipped": skipped, "plugin_root": plugin_root_str}


def _is_running_from_plugin_cache():
    """Check if this script is running from a Claude Code plugin cache directory."""
    _measure_facade = _compat.facade()
    resolved = str(_measure_facade.Path(_measure_facade.__file__).resolve())
    if _measure_facade.os.name == "nt":
        resolved = resolved.replace("\\", "/")
    return "/plugins/cache/" in resolved


def _is_running_from_synced_plugin():
    """Check if this script is running from a Cowork account-synced plugin dir.

    Cowork installs land under ``~/.claude/plugins/synced/<...>/`` (issue
    audit: "the resolver's search paths didn't include the synced plugins
    location"), NOT under ``/plugins/cache/``. That path segment is the reliable
    self-location marker in Cowork -- ``${CLAUDE_PLUGIN_ROOT}`` is not always
    injected into the hook env (Claude Code does not guarantee env injection), so we recognize the synced
    tree by our own resolved location. Desktop paths never contain it.
    """
    _measure_facade = _compat.facade()
    resolved = str(_measure_facade.Path(_measure_facade.__file__).resolve())
    if _measure_facade.os.name == "nt":
        resolved = resolved.replace("\\", "/")
    return "/plugins/synced/" in resolved


def _get_measure_py_path():
    """Get the path to this measure.py script.

    When running from a plugin cache OR a Cowork synced-plugin dir, returns a
    ${CLAUDE_PLUGIN_ROOT}-based path so that settings.json hooks survive version
    upgrades / account re-syncs. Otherwise returns the resolved absolute path.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._is_running_from_plugin_cache() or _measure_facade._is_running_from_synced_plugin():
        # Use the variable that Claude Code resolves dynamically per version
        return "${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py"
    return str(_measure_facade.Path(_measure_facade.__file__).resolve())


def _read_settings_json():
    """Read ~/.claude/settings.json, return (data, path).

    Lossy by design for read-only callers: a missing file, malformed JSON, and
    an unreadable file all collapse to ``{}``. Any caller that will WRITE the
    result back must use ``_read_settings_json_checked`` instead -- see the
    checked-read fix,
    where a failed re-read returned ``{}`` and that empty dict was
    written straight over the user's whole settings.json.
    """
    _measure_facade = _compat.facade()
    data, path, _ok = _measure_facade._read_settings_json_checked()
    return data, path


def _is_regular_file(path) -> bool:
    """True when ``path`` (symlinks followed) is a regular file. Never opens it."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade.stat.S_ISREG(_measure_facade.os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _read_settings_json_checked():
    """Read settings.json, return (data, path, ok).

    ``ok`` is False when the file exists but could not be parsed or read
    (malformed JSON, permission denied, I/O error) -- i.e. when ``{}`` means
    "unknown", not "empty". A missing file is ``({}, path, True)``: genuinely
    empty and safe to build on.

    Write paths MUST branch on ``ok`` and refuse to write when it is False;
    round-tripping an unknown-state ``{}`` destroys every key the user has.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.SETTINGS_PATH.exists():
        # open() on a FIFO blocks until a writer appears, which would
        # stall a SessionStart hook to its deadline. Anything that is not a
        # regular file (FIFO, socket, device, directory) is "unknown", not data.
        if not _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
            return {}, _measure_facade.SETTINGS_PATH, False
        try:
            # utf-8-sig tolerates a BOM (whether the host tolerates
            # one is not verified, but a BOM-prefixed file must at least be
            # readable here so "unknown" never means "lost"). Non-JSONC
            # comments stay malformed.
            with open(_measure_facade.SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                return _measure_facade.json.load(f), _measure_facade.SETTINGS_PATH, True
        except (_measure_facade.json.JSONDecodeError, UnicodeDecodeError, PermissionError, OSError):
            # UnicodeDecodeError is a ValueError, not an OSError: a cp1252
            # file saved by an ANSI editor is "unknown", not a traceback.
            return {}, _measure_facade.SETTINGS_PATH, False
    return {}, _measure_facade.SETTINGS_PATH, True


def _read_settings_for_write(allow_missing=None):
    """Read settings.json for a caller that intends to WRITE it back.

    Returns ``(data, ok)``. Unlike ``_read_settings_json_checked``, a MISSING
    file is ``ok=False`` by default.

    Rationale: ``_read_settings_json_checked`` reports a
    missing file as ``ok=True`` on the theory that "genuinely empty is safe to
    build on". For a READ that is fine. For a WRITE it is not: settings.json
    existing is the normal state for any established install, so "the file
    isn't there" is far more likely to be a transient window (a rename, a sync
    tool, another process mid-swap, a relocated CLAUDE_CONFIG_DIR) than a
    genuine fresh install. Rebuilding from ``{}`` in that window is precisely
    how a full settings.json becomes a two-key stub.

    Automated/hook callers should use the default (``allow_missing=False``) and
    simply do nothing when ``ok`` is False -- skipping one self-heal tick is
    recoverable, an erased settings.json is not. Explicit, user-invoked
    installers may pass ``allow_missing=True`` so a genuine first-run install
    can still create the file.
    """
    _measure_facade = _compat.facade()
    data, _path, ok = _measure_facade._read_settings_json_checked()
    if not ok:
        return {}, False
    if not isinstance(data, dict):
        # valid JSON that is not an object ([1,2,3], "x", 42) must be
        # the same clean refusal as malformed JSON -- downstream callers
        # mutate the value as a dict and would otherwise traceback.
        return {}, False
    if not _measure_facade.SETTINGS_PATH.exists() and not allow_missing:
        return {}, False
    try:
        resolved = str(_measure_facade.SETTINGS_PATH.resolve(strict=False))
        _measure_facade._SETTINGS_WRITE_READ_STATE.snapshot = (resolved, _measure_facade.copy.deepcopy(data))
    except (OSError, ValueError):
        _measure_facade._SETTINGS_WRITE_READ_STATE.snapshot = None
    return data, True


def _smart_compact_hook_commands():
    """Return the hook commands for smart compaction.

    Each command is fail-open guarded: if measure.py is unreadable (e.g. a
    plugin refresh GC'd the version dir the baked ${CLAUDE_PLUGIN_ROOT} path
    resolved to), the hook exits 0 silently instead of erroring on every event.
    Mirrors the launcher guard in hooks.json.
    """
    _measure_facade = _compat.facade()
    py, mp = _measure_facade._settings_hook_python_and_script(_measure_facade._get_measure_py_path())
    guard = f"[ -r {mp} ] || exit 0; "
    return {
        "PreCompact": f"{guard}{py} {mp} compact-capture --trigger auto",
        "SessionStart": f"{guard}{py} {mp} compact-restore",
        "Stop": f"{guard}{py} {mp} compact-capture --trigger stop",
        "SessionEnd": f"{guard}{py} {mp} compact-capture --trigger end",
    }


def _settings_hook_python_and_script(mp):
    """(interpreter, script) shell tokens for a settings.json hook command.

    Claude Code runs these through Git Bash on Windows, where bare ``python3``
    is often missing or the Microsoft Store stub (exit 9009 on every event),
    so Windows names the running interpreter with forward slashes, quoted for
    bash (an apostrophe in the path is safe). POSIX keeps ``python3 '<mp>'``.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.sys.platform == "win32":
        return (_measure_facade.shlex.quote(str(_measure_facade.sys.executable).replace("\\", "/")),
                _measure_facade.shlex.quote(str(mp).replace("\\", "/")))
    return "python3", f"'{mp}'"


def _is_smart_compact_installed(settings=None):
    """Check which smart compact hooks are installed.

    Returns dict of event -> bool.
    Checks both user settings.json and plugin cache hooks.
    """
    _measure_facade = _compat.facade()
    if settings is None:
        settings, _ = _measure_facade._read_settings_json()

    # Merge user hooks with plugin hooks for detection
    all_hooks = dict(settings.get("hooks", {}))

    # Also check plugin cache hooks (marketplace plugin auto-install)
    plugin_cache = _measure_facade.CLAUDE_DIR / "plugins" / "cache"
    if plugin_cache.exists():
        import glob as globmod
        for hooks_file in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
            try:
                with open(hooks_file, "r", encoding="utf-8") as f:
                    plugin_hooks = _measure_facade.json.load(f).get("hooks", {})
                for event, groups in plugin_hooks.items():
                    if event not in all_hooks:
                        all_hooks[event] = groups
                    else:
                        all_hooks[event] = all_hooks[event] + groups
            except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
                continue

    status = {}
    for event in ("PreCompact", "SessionStart", "Stop", "SessionEnd"):
        installed = False
        event_hooks = all_hooks.get(event, [])
        for hook_group in event_hooks:
            for hook in hook_group.get("hooks", []):
                cmd = hook.get("command", "")
                # SessionEnd uses session-end-flush in plugin (handles both
                # collection + end-of-session checkpoint). Accept it alongside
                # the older compact-capture / compact-restore signatures.
                if not isinstance(cmd, str):
                    continue
                if _measure_facade._is_compaction_runner(cmd, event) or "measure.py" in cmd and (
                    "compact-capture" in cmd
                    or "compact-restore" in cmd
                    or (event == "SessionEnd" and ("session-end-flush" in cmd or "collect" in cmd))
                ):
                    installed = True
                    break
        status[event] = installed

    return status


def setup_smart_compact(dry_run=None, uninstall=None, status_only=None,
                        user_initiated=None):
    """Install, uninstall, or check status of smart compaction hooks.

    Appends to existing hooks (never overwrites). Safe to run multiple times.
    """
    _measure_facade = _compat.facade()
    settings, settings_path = _measure_facade._read_settings_json()
    if not status_only:
        # Every non-status branch below writes `settings` back.
        settings, _ok = _measure_facade._read_settings_for_write(allow_missing=True)
        settings_path = _measure_facade.SETTINGS_PATH
        if not _ok:
            print(
                "[Token Optimizer] settings.json is unreadable or malformed; "
                "refusing to modify smart compaction hooks.",
                file=_measure_facade.sys.stderr,
            )
            return
    current_status = _measure_facade._is_smart_compact_installed(settings)
    commands = _measure_facade._smart_compact_hook_commands()

    if status_only:
        print("\n  Smart Compaction Hook Status")
        print(f"  {'=' * 40}")
        for event, installed in current_status.items():
            icon = "installed" if installed else "not installed"
            print(f"    {event:15s} {icon}")
        all_installed = all(current_status.values())
        if all_installed:
            print("\n  All hooks installed. Smart Compaction is active.")
        else:
            missing = [e for e, v in current_status.items() if not v]
            print(f"\n  Missing: {', '.join(missing)}")
            print(_measure_facade._hint("  Run: python3 measure.py setup-smart-compact"))
        print()
        return

    if uninstall:
        hooks = settings.get("hooks", {})
        removed = 0
        for event in ("PreCompact", "SessionStart", "Stop", "SessionEnd"):
            if event not in hooks:
                continue
            new_groups = []
            for group in hooks[event]:
                new_hooks = [
                    h for h in group.get("hooks", [])
                    if "compact-capture" not in h.get("command", "")
                    and "compact-restore" not in h.get("command", "")
                ]
                if new_hooks:
                    group["hooks"] = new_hooks
                    new_groups.append(group)
                else:
                    removed += 1
            if new_groups:
                hooks[event] = new_groups
            elif event in hooks:
                del hooks[event]

        if dry_run:
            print(f"\n  [Dry run] Would remove {removed} smart compact hook(s) from {settings_path}")
            print("  Run without --dry-run to apply.\n")
            return

        settings["hooks"] = hooks
        # setup-smart-compact also installed compactInstructions; take ours
        # back out (a user-written part stays).
        ci_removed = _measure_facade._remove_our_compact_instructions(settings)
        if not _measure_facade._write_settings_atomic(settings, allow_removing_keys={"compactInstructions"},
                                      user_initiated=user_initiated):
            print("[Error] settings.json write was refused; smart compact hooks were not removed.", file=_measure_facade.sys.stderr)
            return
        print(f"[Token Optimizer] Removed smart compact hooks. {removed} hook(s) removed.")
        if ci_removed:
            print("[Token Optimizer] Removed Token Optimizer compact instructions.")
        return

    # Install
    # Plugin users get all smart compact hooks from hooks.json — skip settings.json.
    # Synced (Cowork) installs get them from the cowork hooks.json too; include the
    # synced check so we never bake a ${CLAUDE_PLUGIN_ROOT} path into settings.json for
    # them (a single-quoted placeholder only resolves on text-substitution hosts).
    is_plugin = (
        _measure_facade._is_running_from_plugin_cache()
        or _measure_facade._is_running_from_synced_plugin()
        or _measure_facade._is_plugin_installed()
    )
    if is_plugin:
        all_active = all(current_status.values())
        if all_active:
            print("[Token Optimizer] Smart Compaction active via plugin hooks.json. Nothing to do.")
        else:
            print("[Token Optimizer] Smart Compaction managed by plugin hooks.json.")
        return

    hooks = settings.setdefault("hooks", {})
    installed = []
    skipped = []

    for event, command in commands.items():
        if event == "SessionStart":
            # SessionStart needs TWO hook groups:
            # 1. Post-compaction recovery (matcher: "compact")
            # 2. New-session checkpoint pointer (no matcher, --new-session-only)
            hooks.setdefault(event, [])
            event_hooks = hooks[event]

            has_compact_matcher = any(
                g.get("matcher") == "compact"
                and any("compact-restore" in h.get("command", "") for h in g.get("hooks", []))
                for g in event_hooks
            )
            new_session_cmd = command + " --new-session-only"
            has_new_session = any(
                "matcher" not in g
                and any("--new-session-only" in h.get("command", "") for h in g.get("hooks", []))
                for g in event_hooks
            )

            added = False
            if not has_compact_matcher:
                event_hooks.append({"matcher": "compact", "hooks": [{"type": "command", "command": command}]})
                added = True
            if not has_new_session:
                event_hooks.append({"hooks": [{"type": "command", "command": new_session_cmd}]})
                added = True

            if added:
                installed.append(event)
            else:
                skipped.append(event)
            continue

        if current_status.get(event):
            skipped.append(event)
            continue

        # Append to existing hook groups for this event
        hook_entry = {"type": "command", "command": command}
        hook_group = {"hooks": [hook_entry]}

        if event not in hooks:
            hooks[event] = []
        hooks[event].append(hook_group)
        installed.append(event)

    if dry_run:
        print("\n  [Dry run] Smart Compaction hook preview")
        print(f"  {'=' * 40}")
        if installed:
            print(f"  Would install hooks for: {', '.join(installed)}")
        if skipped:
            print(f"  Already installed (skip): {', '.join(skipped)}")
        print(f"\n  Settings file: {settings_path}")
        print("  Hook commands:")
        for event in installed:
            print(f"    {event}: {commands[event]}")
        print("\n  Run without --dry-run to apply.\n")
        return

    if not installed:
        print("[Token Optimizer] All smart compact hooks already installed.")
        return

    settings["hooks"] = hooks
    if not _measure_facade._write_settings_atomic(settings, user_initiated=user_initiated):
        print("[Error] settings.json write was refused; smart compact hooks were not installed.", file=_measure_facade.sys.stderr)
        return

    print("[Token Optimizer] Smart Compaction installed.")
    print(f"  Hooks added: {', '.join(installed)}")
    if skipped:
        print(f"  Already had: {', '.join(skipped)}")

    # Also install compact instructions (tells Claude WHAT to preserve)
    print()
    _measure_facade.generate_compact_instructions(install=True, user_initiated=user_initiated)

    print("\n  What happens now:")
    print("    Compact Instructions: Guides Claude on what to preserve during compaction")
    print("    PreCompact hook:      Captures structured state before compaction")
    print("    SessionStart hook:    Restores what was lost after compaction")
    print("    Stop hook:            Saves checkpoint when session ends normally")
    print("    SessionEnd hook:      Saves checkpoint on /clear or termination")
    print(f"\n  Checkpoints stored in: {_measure_facade.CHECKPOINT_DIR}")
    print(_measure_facade._hint("  To remove: python3 measure.py setup-smart-compact --uninstall"))


def _stable_marketplace_script_path(script_name):
    """Version-INDEPENDENT path to a bundled script, or None.

    Plugin-cache installs live at::

        <claude>/plugins/cache/<marketplace>/<plugin>/<VERSION>/skills/...

    so any absolute path written into settings.json dies on the next update.
    The marketplace clone at::

        <claude>/plugins/marketplaces/<marketplace>/skills/...

    carries the same tree at a path with no version segment, so it survives
    updates. Returns that path only when it actually exists and stays confined
    under the runtime marketplaces dir (no symlink escape); otherwise None, so
    the caller falls back to the version-pinned write plus self-heal.
    """
    _measure_facade = _compat.facade()
    try:
        here = _measure_facade.Path(_measure_facade.__file__).resolve()
        parts = here.parts
        idx = len(parts) - 1 - parts[::-1].index("cache")
        marketplace = parts[idx + 1]
    except (ValueError, IndexError, OSError):
        return None
    try:
        base = (_measure_facade.CLAUDE_DIR / "plugins" / "marketplaces").resolve(strict=False)
        candidate = base / marketplace / "skills" / "token-optimizer" / "scripts" / script_name
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not resolved.is_relative_to(base):
            return None
    except (OSError, ValueError):
        return None
    return str(resolved)


def _get_statusline_path():
    """Get the path to the bundled statusline.js script.

    Always returns an absolute path. Unlike hook commands in hooks.json,
    settings.json statusLine may not resolve ${CLAUDE_PLUGIN_ROOT} -- which is
    why this cannot simply mirror _get_measure_py_path().

    Plugin-cache installs previously got the VERSION-PINNED cache path, which
    breaks the moment the plugin updates. _fix_stale_settings_paths() repairs
    it, but only at the NEXT SessionStart, so the first session after an update
    rendered a blank status line before the heal landed. Prefer the
    version-independent marketplace clone path when it resolves; fall back to
    the version-pinned path (still self-healed) when it does not -- a pruned
    marketplace clone, or any non-plugin-cache install.

    Either way the self-disabling guard inside statusline.js still applies:
    both candidate trees ship the sibling files it checks for.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._is_running_from_plugin_cache():
        stable = _measure_facade._stable_marketplace_script_path("statusline.js")
        if stable:
            return stable
    return str(_measure_facade.Path(_measure_facade.__file__).resolve().parent / "statusline.js")


def _fix_stale_settings_paths():
    """Detect and fix stale versioned plugin cache paths in settings.json.

    When a plugin updates (e.g., 3.0.0 -> 3.1.0), any hooks or statusLine
    entries written to settings.json with the old versioned path break silently.

    Since this runs from ensure-health (called at SessionStart via the NEW
    version's hooks.json), Path(__file__).resolve() gives us the current
    version's path. We find any old versioned paths and rewrite them.

    Works by replacing paths in the serialized JSON, which handles all keys
    (hooks, statusLine, and any future settings) without key-specific iteration.

    Note: This rewrites old versioned paths to the current version's absolute
    path, not to ${CLAUDE_PLUGIN_ROOT}. The variable may not be resolved in
    settings.json. This creates a self-healing loop (3.0.0 → 3.1.0, then
    3.1.0 → 3.2.0 on next upgrade). The loop is intentional and cheap
    (runs every SessionStart, takes milliseconds).

    Returns number of stale roots replaced, or 0 on failure/no-op.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._is_running_from_plugin_cache():
        return 0
    try:
        # This WRITES the result back. An unknown-state {} here
        # would be json.dumps'd, string-substituted, and written over the user's
        # whole file. Refuse on a bad or missing read.
        settings, _ok = _measure_facade._read_settings_for_write()
        if not _ok or not settings:
            return 0
    except Exception:
        return 0

    settings_text = _measure_facade.json.dumps(settings)

    if _measure_facade.os.name == "nt":
        # Our current plugin root (e.g., C:\Users\...\plugins\cache\org\token-optimizer\3.1.0)
        current_root = str(_measure_facade.Path(_measure_facade.__file__).resolve().parent.parent.parent.parent)
        normalized_text = settings_text.replace("\\\\", "/")
        if "/plugins/cache/" not in normalized_text or "token-optimizer" not in normalized_text:
            return 0
        pattern = r'((?:[A-Za-z]:)?/[^"\'\\]+/plugins/cache/[^/]+/token-optimizer/[^/]+)'
        stale_roots = {
            m.group(1) for m in _measure_facade.re.finditer(pattern, normalized_text)
            if m.group(1) != current_root.replace("\\", "/")
        }
        new_text = settings_text
        replacement = current_root.replace("\\", "\\\\")
        for stale_root in stale_roots:
            new_text = new_text.replace(stale_root.replace("/", "\\\\"), replacement)
    else:
        if "/plugins/cache/" not in settings_text or "token-optimizer" not in settings_text:
            return 0

        # Our current plugin root (e.g., /home/user/.claude/plugins/cache/org/token-optimizer/3.1.0)
        current_root = str(_measure_facade.Path(_measure_facade.__file__).resolve().parent.parent.parent.parent)
        # Find all versioned token-optimizer plugin cache paths that differ from ours
        stale_roots = set()
        for m in _measure_facade.re.finditer(r'(/[^"\'\\]+/plugins/cache/[^/]+/token-optimizer/[^/]+)', settings_text):
            found_root = m.group(1)
            if found_root != current_root:
                stale_roots.add(found_root)
        new_text = settings_text
        for stale_root in stale_roots:
            new_text = new_text.replace(stale_root, current_root)

    if not stale_roots:
        return 0

    # Replace stale roots directly in the serialized JSON, then parse back.
    # This avoids mutating the original dict (no partial-state on write failure)
    # and covers all keys without key-specific iteration.
    if new_text == settings_text:
        return 0

    try:
        new_settings = _measure_facade.json.loads(new_text)
        if not _measure_facade._write_settings_atomic(new_settings):
            return 0
    except Exception:
        return 0

    return len(stale_roots)


def _migrate_statusline_to_stable_path():
    """Migrate an already-installed statusLine from a version-pinned cache path
    to the update-surviving marketplace clone path.

    G3 C-P2-3: the ca654fe stable-path fix only reaches FRESH installs.
    ``setup_quality_bar`` skips the statusLine whenever one is already installed,
    and ``_is_quality_bar_installed`` counts BOTH the old version-pinned cache
    path and the new clone path as "installed" (it only matches ``statusline.js``
    + ``token-optimizer`` in the command). ``_fix_stale_settings_paths`` rewrites
    cache-root -> *current* cache-root, still version-pinned. So the installed
    base that originally hit "status line blank after every update" keeps the
    version-pinned path and keeps hitting the blank-window-on-first-session.

    This lifts them onto the clone path once, at ensure-health. Acts only on
    plugin-cache installs whose OWN statusLine command still references a
    ``/plugins/cache/`` version-pinned ``statusline.js`` and where the stable
    clone path resolves. It does a targeted substring swap (not a wholesale
    rewrite) so any surrounding command wrapper is preserved. Runs AFTER
    ``_fix_stale_settings_paths`` so the cache path is already the current
    version's before it is lifted to the clone.

    Returns True if it rewrote the command, else False.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._is_running_from_plugin_cache():
        return False
    stable = _measure_facade._stable_marketplace_script_path("statusline.js")
    if not stable:
        return False
    try:
        # Writes `new_settings` (a copy of `settings`) back.
        settings, _ok = _measure_facade._read_settings_for_write()
        if not _ok or not settings:
            return False
    except Exception:
        return False
    sl = settings.get("statusLine") or {}
    cmd = sl.get("command", "")
    if not cmd or "statusline.js" not in cmd or "token-optimizer" not in cmd:
        return False
    # Only normalize separators on Windows; backslash is a legal POSIX
    # filename char, so an unconditional replace would change POSIX behavior.
    normalized_cmd = cmd.replace("\\", "/") if _measure_facade.os.name == "nt" else cmd
    m = _measure_facade.re.search(r'(?:[A-Za-z]:)?/[^"\'\\]+/plugins/cache/[^/]+/token-optimizer/[^"\'\\]*statusline\.js', normalized_cmd)
    if not m:
        return False
    old_path = cmd[m.start():m.end()]
    if old_path == stable:
        return False
    if "\\" in cmd:
        new_cmd = cmd[:m.start()] + stable + cmd[m.end():]
    else:
        new_cmd = cmd.replace(old_path, stable)
    if new_cmd == cmd:
        return False
    new_settings = dict(settings)
    new_sl = dict(sl)
    new_sl["command"] = new_cmd
    new_settings["statusLine"] = new_sl
    try:
        return bool(_measure_facade._write_settings_atomic(new_settings))
    except Exception:
        return False


def _statusline_js_in_command(cmd):
    """(start, end, path) of the statusline.js path inside a statusLine
    command (quoted or bare), or None."""
    _measure_facade = _compat.facade()
    m = _measure_facade._STATUSLINE_JS_IN_CMD_RE.search(cmd or "")
    if not m:
        return None
    if m.group(2) is not None:
        return m.start(2), m.end(2), m.group(2)
    return m.start(3), m.end(3), m.group(3)


def _statusline_cmd_is_ours(cmd):
    """True when a statusLine command runs Token Optimizer's statusline.js.

    Judged by the folder the script sits in (``token-optimizer/`` or
    ``token-optimizer/scripts/``), not by "token-optimizer" appearing anywhere
    in the command: another tool's statusline.js under a home or project folder
    that happens to contain that name is not ours. A command whose script path
    cannot be parsed falls back to the plain substring test.
    """
    _measure_facade = _compat.facade()
    if not isinstance(cmd, str) or "statusline.js" not in cmd or "token-optimizer" not in cmd:
        return False
    found = _measure_facade._statusline_js_in_command(cmd)
    if not found:
        return True
    parts = [p for p in _measure_facade.re.split(r"[\\/]", found[2].lower()) if p]
    if len(parts) < 2 or parts[-1] != "statusline.js":
        return False
    if parts[-2] == "token-optimizer":
        return True
    return len(parts) >= 3 and parts[-2] == "scripts" and parts[-3] == "token-optimizer"


def _path_is_under(path, base):
    _measure_facade = _compat.facade()
    try:
        _measure_facade.Path(path).expanduser().resolve(strict=False).relative_to(_measure_facade.Path(base).resolve(strict=False))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _heal_dangling_statusline():
    """Re-point OUR statusLine when it runs a statusline.js that is gone or
    that sits in Codex's plugin cache.

    Before 5.13.51 a Codex-loaded plugin hook was detected as Claude Code, and
    its settings heal pointed Claude Code's statusLine at the Codex plugin
    cache (deleted on Codex's next plugin update, so the status line went
    blank). The stale-path heal only rewrites ``/plugins/cache/`` roots and
    only on plugin-cache installs, so a script install, a missing file outside
    a cache, or a still-present Codex copy kept the bad command.

    Acts only in Claude Code (never a hosted VM's lean profile), only on a
    statusLine that is ours (``statusline.js`` + ``token-optimizer`` in the
    command), and only when the referenced file is missing or under
    ``<codex home>/plugins/``. An existing statusline.js from another Token
    Optimizer checkout (a dev clone) is the user's choice and is left alone.
    Only the path inside the command changes; every other key is preserved.
    Returns True if it rewrote the command.
    """
    _measure_facade = _compat.facade()
    try:
        if _measure_facade.detect_runtime() != "claude" or _measure_facade._remote_lean_profile():
            return False
        settings, _ok = _measure_facade._read_settings_for_write()
        if not _ok or not settings:
            return False
        sl = settings.get("statusLine")
        if not isinstance(sl, dict):
            return False
        cmd = sl.get("command") or ""
        if not _measure_facade._statusline_cmd_is_ours(cmd):
            return False
        found = _measure_facade._statusline_js_in_command(cmd)
        if not found:
            return False
        start, end, old_path = found
        ours = _measure_facade._get_statusline_path()
        if not ours or not _measure_facade.Path(ours).is_file():
            return False
        if _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(old_path)) == _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(ours)):
            return False
        from runtime_env import codex_home as _codex_home
        in_codex = _measure_facade._path_is_under(old_path, _codex_home() / "plugins")
        if _measure_facade.Path(old_path).expanduser().is_file() and not in_codex:
            return False
        new_cmd = cmd[:start] + ours + cmd[end:]
        if cmd[start - 1:start] not in ("'", '"') and any(c.isspace() for c in ours):
            new_cmd = cmd[:start] + "'" + ours + "'" + cmd[end:]
        new_settings = dict(settings)
        new_sl = dict(sl)
        new_sl["command"] = new_cmd
        new_settings["statusLine"] = new_sl
        return bool(_measure_facade._write_settings_atomic(new_settings))
    except Exception:
        return False


def _heal_keepwarm_plist_path():
    """Rewrite an installed keep-warm launchd plist whose measure.py path went
    stale onto the update-surviving marketplace clone path, then reload the agent.

    Companion to the _keepwarm_measure_script_path stable-path preference (which
    only helps FRESH plist writes). An agent installed by an older build carries a
    version-pinned ``/plugins/cache/.../<VERSION>/measure.py`` path that dies on the
    next plugin update; launchd then fires an old (or missing) script every ~5min,
    so the dashboard's savings freeze at whatever the last good tick produced. This
    lifts an existing plist onto the clone path once, at ensure-health, and
    bootout+bootstraps so the corrected path takes effect immediately.

    Acts only on plugin-cache installs whose OWN keep-warm plist exists and whose
    embedded measure.py path differs from the resolved clone path. Editing the path
    of a plist the user already has is a correctness fix, not a (re)install, so it
    never resurrects a declined/uninstalled agent. The plist only lives under
    ~/Library/LaunchAgents on macOS, so on other platforms it simply won't exist
    and this no-ops. Best effort: any failure leaves the plist untouched, False.
    """
    # Per-account scheduler job: never touched from a redirected HOME
    # (a test sandbox or scratch run would re-point the user's live job).
    # Frozen flag only up here: this runs in ensure-health on EVERY
    # SessionStart, and the full guard's job check is a `schtasks /Query` on
    # Windows, where this macOS heal never has a plist to fix. The full guard
    # runs below, once a stale plist exists and needs a rewrite.
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_snapshot_sandboxed():
        return False
    if not _measure_facade._is_running_from_plugin_cache():
        return False
    stable = _measure_facade._stable_marketplace_script_path("measure.py")
    if not stable:
        return False
    plist_path = _measure_facade._keepwarm_scheduler_plist_path()
    try:
        if not plist_path.is_file():
            return False
    except OSError:
        return False
    import plistlib
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return False
    args = data.get("ProgramArguments")
    if not isinstance(args, list):
        return False
    # The script arg is the measure.py entry (not python3 or the subcommand).
    idx = next((i for i, a in enumerate(args)
                if isinstance(a, str) and a.endswith("measure.py")), None)
    if idx is None or args[idx] == stable:
        return False
    # A rewrite is due. Refuse it when the account's job belongs to another
    # install (rewriting and reloading would re-point it at this one).
    if _measure_facade._scheduler_install_refused():
        return False
    new_args = list(args)
    new_args[idx] = stable
    new_data = dict(data)
    new_data["ProgramArguments"] = new_args
    # Atomic replace so a crash mid-write can't leave a truncated plist.
    try:
        fd, tmp = _measure_facade.tempfile.mkstemp(prefix=".keepwarm-plist.", suffix=".tmp",
                                   dir=str(plist_path.parent))
        try:
            with _measure_facade.os.fdopen(fd, "wb") as f:
                plistlib.dump(new_data, f)
            _measure_facade.os.replace(tmp, str(plist_path))
        finally:
            if _measure_facade.os.path.exists(tmp):
                try:
                    _measure_facade.os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        return False
    # Reload so launchd fires the corrected path (mirrors the installer's
    # idempotent bootout-then-bootstrap). Best effort; the on-disk fix stands even
    # if the reload can't run (non-macOS, no launchctl, unprivileged).
    getuid = getattr(_measure_facade.os, "getuid", None)
    if getuid is not None:
        uid = getuid()
        for argv in (["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
                     ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)]):
            try:
                _measure_facade.subprocess.run(argv, capture_output=True, text=True,
                               errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW)
            except (OSError, _measure_facade.subprocess.TimeoutExpired):
                pass
    return True


def _fix_malformed_hook_commands():
    """Detect and remove malformed hook commands from settings.json.

    Targets two patterns that break hooks silently:
    1. Subshell commands ($(...) or backticks) referencing TO scripts —
       written by Claude during interactive sessions, not by our installers.
    2. Double-$HOME paths ($HOME/Users/X/... or ${HOME}/Users/X/...) —
       expand to /Users/X/Users/X/... which doesn't exist.

    Only removes hooks that reference known TO script names, so other
    plugins' hooks are never touched. After removal, calls setup_all_hooks
    for script-install users (plugin users get hooks from hooks.json
    automatically). Resets last_hook_heal_check so the next session's
    24h gate runs the full heal cycle.

    Returns the number of removed hook entries.
    """
    _measure_facade = _compat.facade()
    try:
        # Writes `new_settings` (built from `current`) back.
        current, _ok = _measure_facade._read_settings_for_write()
        if not _ok:
            return 0
    except Exception:
        return 0

    current_hooks = current.get("hooks") if current else None
    if not current_hooks or not isinstance(current_hooks, dict):
        return 0

    removed = 0
    new_hooks = {}

    for event, handler_groups in current_hooks.items():
        if not isinstance(handler_groups, list):
            new_hooks[event] = handler_groups
            continue
        new_groups = []
        for group in handler_groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            original_hook_list = group.get("hooks", [])
            if not isinstance(original_hook_list, list):
                new_groups.append(group)
                continue
            kept_hooks = []
            for h in original_hook_list:
                if not isinstance(h, dict):
                    kept_hooks.append(h)
                    continue
                cmd = h.get("command", "")
                if _measure_facade._is_malformed_to_hook(cmd):
                    removed += 1
                    continue
                kept_hooks.append(h)
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                new_groups.append(new_group)
        if new_groups:
            new_hooks[event] = new_groups

    if removed == 0:
        return 0

    new_settings = dict(current)
    new_settings["hooks"] = new_hooks

    try:
        if not _measure_facade._write_settings_atomic(new_settings):
            return 0
    except (PermissionError, OSError):
        return 0

    # Reset 24h throttle so next session re-runs the full hook heal cycle.
    try:
        _measure_facade._write_config_flag("last_hook_heal_check", 0)
    except Exception:
        pass

    # Script-install users need immediate reprovisioning (plugin users
    # get hooks from hooks.json automatically, no gap).
    if not _measure_facade._is_plugin_installed():
        try:
            heal_result = _measure_facade.setup_all_hooks(dry_run=False, verbose=False)
            if heal_result.get("error"):
                print(f"  [Token Optimizer] Warning: hook re-provisioning failed after cleanup: {heal_result['error']}", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] Warning: hook re-provisioning failed after cleanup: {_e}", file=_measure_facade.sys.stderr)

    return removed


# The exact version-resolver assignment every installer-written hook command
# carries (hooks/hooks.json, bash-loop wrapper). Treated as plumbing by both
# _hook_command_identity and _is_malformed_to_hook; the regression test
# asserts it matches the resolver substring in every hooks.json entry.
_TO_VERSION_RESOLVER = 'D="$(ls -d "${D%[/\\\\]*}"/*/ 2>/dev/null | grep -E "/[0-9]+[.][0-9]+[.][0-9]+/$" | sort -V | tail -n1)"'


def _is_malformed_to_hook(cmd):
    """Check if a hook command is a malformed token-optimizer entry.

    Returns True only when the command's executed script is provably THIS
    install's payload AND the command carries a subshell pattern or a
    double-$HOME expansion.

    Ownership proof anchors on the actual path token carrying a known TO
    script basename (the script the hook will run) and requires it to
    resolve to skills/token-optimizer/scripts/<name> under one of exactly
    two runtime-proven roots: the running install root (this module's own
    __file__; for a plugin-cache install that IS the current cache version
    directory) or the resolved $CLAUDE_PLUGIN_ROOT environment value. The
    Claude config dir is never a root by itself: another skill, another
    plugin's cache entry, a same-named plugin from a different marketplace,
    or a stale version of this plugin under it is not provably ours and is
    preserved. A root string in an argument, comment, env assignment, or
    redirect proves nothing (comments are stripped before tokenizing).
    Home-relative spellings (~, $HOME, ${HOME}) expand against the real
    home; a literal $CLAUDE_PLUGIN_ROOT resolves through the environment
    when set and is unprovable (preserved) when unset. The macOS
    doubled-$HOME corruption is corrected by stripping the known doubled
    prefix, then the same full proof applies. A relative script argument
    resolves only against a literal D="<root>" assignment in the same
    command (the installer's written form). Every basename-carrying token
    must resolve to our payload: a command mixing our script with a foreign
    one is ambiguous and preserved. Deleting the user's own hook is
    irreversible, so unknown ownership means leave it alone.
    """
    _measure_facade = _compat.facade()
    if not cmd:
        return False
    re_mod = _measure_facade.re
    os_mod = _measure_facade.os
    Path_mod = _measure_facade.Path
    names = _measure_facade._TO_SCRIPT_NAMES
    has_to_script = any(
        re_mod.search(r'(?:^|[/\s])' + re_mod.escape(name) + r'(?:$|[\s])', cmd)
        for name in names
    )
    if not has_to_script:
        return False

    # POSIX, drive-letter (C:/ or C:\) and UNC absolutes; hook commands on
    # Windows installs carry Windows paths.
    abs_re = re_mod.compile(r'^(?:/|[A-Za-z]:[\\/]|\\\\)')
    # Doubled-home prefixes written by Claude on macOS when it expands $HOME
    # and prepends it again: raw ($HOME/Users/<u>/<abs>) and already-expanded
    # (/Users/<u>/Users/<u>/...). Stripping yields the intended absolute
    # path, which still has to pass the full ownership proof.
    doubled_re = re_mod.compile(
        r'^(?:\$HOME|\$\{HOME\}|~)/(?:Users|home)/[^/]+?(?=/|[A-Za-z]:[\\/])'
        r'|^/(?:Users|home)/[^/]+(?=/(?:Users|home)/)'
    )

    def _canon(path):
        # normcase: case-insensitive + separator-normalized on Windows,
        # identity on POSIX.
        return os_mod.path.normcase(str(path)).replace("\\", "/").rstrip("/")

    def _owned_roots():
        roots = []
        try:
            # The facade lives at <root>/skills/token-optimizer/scripts/
            # measure.py, so parents[3] is the install root the installer
            # writes into hook commands.
            roots.append(Path_mod(_measure_facade.__file__).resolve().parents[3])
        except Exception:
            pass
        try:
            plugin_root = os_mod.environ.get("CLAUDE_PLUGIN_ROOT")
            if plugin_root:
                roots.append(Path_mod(plugin_root).expanduser().resolve())
        except Exception:
            pass
        return [_canon(r) for r in roots]

    def _owned_script_path(candidate):
        canon = _canon(candidate)
        name = canon.rsplit("/", 1)[-1]
        if name not in names:
            return False
        suffix = "skills/token-optimizer/scripts/" + name
        return any(len(root) >= 4 and canon == root + "/" + suffix
                   for root in _owned_roots())

    def _proves_ownership():
        try:
            home = str(Path_mod.home())
        except Exception:
            home = ""
        try:
            plugin_root = os_mod.environ.get("CLAUDE_PLUGIN_ROOT") or ""
        except Exception:
            plugin_root = ""

        # Shell comments never execute: a root or script path mentioned only
        # in a comment cannot prove (or veto) ownership.
        probe = re_mod.sub(r'(?m)(^|[\s;|&])#.*$', r'\1', cmd)

        def _expand(path):
            if home and home != "~":
                for prefix in ("${HOME}/", "$HOME/", "~/"):
                    if path.startswith(prefix):
                        return home.rstrip("/") + "/" + path[len(prefix):]
            if plugin_root:
                for prefix in ("${CLAUDE_PLUGIN_ROOT}/", "$CLAUDE_PLUGIN_ROOT/"):
                    if path.startswith(prefix):
                        return plugin_root.rstrip("/") + "/" + path[len(prefix):]
            return path

        # Literal D="<root>" assignments the installer prefixes hook commands
        # with; the base for the relative script argument that follows them.
        d_roots = [
            _expand(m.group(1)).rstrip("/")
            for m in re_mod.finditer(r'(?:^|[;\s])D="([^"]+)"', probe)
        ]

        anchors = []
        for tok in probe.split():
            tok = tok.strip('"').strip("'").rstrip(";").replace("\\", "/")
            for name in names:
                if tok == name or tok.endswith("/" + name):
                    anchors.append(tok)
                    break
        if not anchors:
            return False

        def _resolve_candidates(tok):
            if tok.startswith("$D/"):
                tok = tok[len("$D/"):]
            candidates = []
            expanded = _expand(tok)
            if abs_re.match(expanded):
                candidates.append(expanded)
            for variant in (tok, expanded):
                stripped = doubled_re.sub("", variant)
                if stripped != variant:
                    stripped = _expand(stripped)
                    if abs_re.match(stripped):
                        candidates.append(stripped)
            if not candidates and d_roots:
                candidates.append(d_roots[0] + "/" + expanded)
            return candidates

        for tok in anchors:
            owned = False
            for candidate in _resolve_candidates(tok):
                try:
                    candidate = str(Path_mod(candidate).resolve())
                except Exception:
                    candidate = os_mod.path.normpath(candidate)
                if _owned_script_path(candidate):
                    owned = True
                    break
            if not owned:
                return False
        return True

    if not _proves_ownership():
        return False
    # The installer's version resolver is recognized plumbing, not
    # malformation. Recognize the EXACT template hooks/hooks.json writes
    # (_TO_VERSION_RESOLVER, kept in sync with hooks.json by
    # tests/test_malformed_detector_installer_resolver.py) and remove ONE
    # occurrence before the subshell check. A character-class regex is not
    # acceptable here: this predicate is a rejection gate, and a loose match
    # would let crafted content hide a real subshell inside a resolver-shaped
    # string. Any deviation from the template, any second resolver-shaped
    # subshell, and any other subshell or backtick (Claude-written shapes)
    # leaves "$(" or "`" in the probe and is still rejected.
    probe = cmd.replace(_TO_VERSION_RESOLVER, 'D=""', 1)
    has_subshell = "$(" in probe or "`" in probe
    has_double_home = False
    if _measure_facade.platform.system() == "Darwin":
        has_double_home = (
            "$HOME/Users/" in cmd or "$HOME/home/" in cmd
            or "${HOME}/Users/" in cmd or "${HOME}/home/" in cmd
        )
    return has_subshell or has_double_home


def _command_drives_quality_cache(command):
    """True if a hook *command string* already runs the quality-cache update.

    Recognizes both the legacy standalone hook and the consolidated
    in-process dispatcher (see `_QUALITY_CACHE_HOOK_MARKERS`).
    """
    _measure_facade = _compat.facade()
    blob = str(command or "")
    return any(marker in blob for marker in _measure_facade._QUALITY_CACHE_HOOK_MARKERS)


def _quality_cache_hook_present(groups):
    """True if any UserPromptSubmit hook *group* already drives quality-cache.

    `groups` is a settings.json / hooks.json ``UserPromptSubmit`` list. This is
    the two-marker generalization of the old ``any("quality-cache" in str(h)
    for h in groups)`` scan: it stringifies each group so it stays robust to the
    canonical ``{"hooks": [{"command": ...}]}`` shape and any bare/legacy entry,
    while recognizing the consolidated dispatcher that carries no literal
    "quality-cache" substring.
    """
    _measure_facade = _compat.facade()
    return any(_measure_facade._command_drives_quality_cache(str(group)) for group in (groups or []))


def _is_quality_bar_installed(settings=None):
    """Check which quality bar components are installed.

    Returns dict with 'statusline' and 'hook' bools.
    """
    _measure_facade = _compat.facade()
    if settings is None:
        settings, _ = _measure_facade._read_settings_json()

    result = {"statusline": False, "hook": False}

    # Check statusline
    sl = (settings.get("statusLine") or {})
    cmd = sl.get("command", "")
    if _measure_facade._statusline_cmd_is_ours(cmd):
        result["statusline"] = True

    # Check UserPromptSubmit hook (settings.json). Recognizes both the legacy
    # standalone hook and the consolidated dispatcher.
    hooks = (settings.get("hooks") or {})
    for group in (hooks.get("UserPromptSubmit") or []):
        for hook in (group.get("hooks") or []):
            if _measure_facade._command_drives_quality_cache(hook.get("command") or ""):
                result["hook"] = True
                break

    # Also check plugin cache hooks (matching _is_smart_compact_installed pattern)
    if not result["hook"]:
        plugin_cache = _measure_facade.CLAUDE_DIR / "plugins" / "cache"
        if plugin_cache.exists():
            import glob as globmod
            for hooks_file in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
                try:
                    with open(hooks_file, "r", encoding="utf-8") as f:
                        plugin_hooks = _measure_facade.json.load(f).get("hooks", {})
                    for group in (plugin_hooks.get("UserPromptSubmit") or []):
                        for hook in (group.get("hooks") or []):
                            if _measure_facade._command_drives_quality_cache(hook.get("command") or ""):
                                result["hook"] = True
                                break
                except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
                    continue

    return result


def _set_quality_bar_disabled(disabled):
    """Persist the quality-bar opt-out flag via the atomic+locked config writer.

    Makes `setup-quality-bar --uninstall` sticky across SessionStart auto-
    restore: ensure-health and quality-cache self-heal both already gate on
    this flag. Routed through _write_config_flag so concurrent writers
    (toggle clicks, ensure-health timestamps) never see partial JSON.
    """
    _measure_facade = _compat.facade()
    _measure_facade._write_config_flag("quality_bar_disabled", bool(disabled))


def _set_daemon_disabled(disabled):
    """Persist the dashboard-daemon opt-out flag via the atomic+locked writer.

    Makes `setup-daemon --uninstall` sticky across the SessionStart ensure-
    health self-install: `_ensure_dashboard_daemon` gates on this flag so a
    deliberate uninstall is never resurrected. Symmetric with
    `_set_quality_bar_disabled`; an explicit `setup-daemon` install clears it
    (explicit opt-in). Routed through _write_config_flag so it never races the
    ensure-health timestamp / dashboard-toggle writers.
    """
    _measure_facade = _compat.facade()
    _measure_facade._write_config_flag("daemon_disabled", bool(disabled))


def _set_vscode_ext_disabled(disabled):
    """Persist the VS Code extension auto-install opt-out (sticky across
    SessionStart), symmetric with `_set_quality_bar_disabled` /
    `_set_daemon_disabled`. `setup-vscode-extension --uninstall` sets it; an
    explicit `--install` clears it (opt back in)."""
    _measure_facade = _compat.facade()
    _measure_facade._write_config_flag("vscode_ext_disabled", bool(disabled))


def _detect_vscode_family_cli():
    """Return (editor_label, cli_name) when running inside a VS Code-family editor
    with an install-capable CLI on PATH, else (None, None).

    VS Code, Cursor, Windsurf, and VSCodium all set TERM_PROGRAM=vscode (they are
    VS Code forks) and export VSCODE_* markers in the integrated terminal. We pick
    the fork-specific CLI first (its `--install-extension` resolves from the fork's
    own registry — OpenVSX for Cursor/Windsurf), falling back to `code`.
    """
    _measure_facade = _compat.facade()
    import shutil  # noqa: PLC0415

    term = _measure_facade.os.environ.get("TERM_PROGRAM", "")
    in_vscode = term == "vscode" or any(k.startswith("VSCODE_") for k in _measure_facade.os.environ)
    if not in_vscode:
        return None, None
    bundle = _measure_facade.os.environ.get("__CFBundleIdentifier", "").lower()
    ipc = _measure_facade.os.environ.get("VSCODE_IPC_HOOK", "").lower() + _measure_facade.os.environ.get(
        "VSCODE_GIT_ASKPASS_MAIN", ""
    ).lower()
    label, preferred = "VS Code", []
    if "cursor" in bundle or "cursor" in ipc:
        label, preferred = "Cursor", ["cursor"]
    elif "windsurf" in bundle or "exafunction" in bundle or "windsurf" in ipc:
        label, preferred = "Windsurf", ["windsurf"]
    elif "vscodium" in bundle or "codium" in ipc:
        label, preferred = "VSCodium", ["codium"]
    for name in preferred + ["code", "code-insiders", "cursor", "windsurf", "codium"]:
        if shutil.which(name):
            return label, name
    return None, None


def _ensure_vscode_extension():
    """Auto-install the Token Optimizer VS Code status-bar extension (Claude only,
    default-on, sticky opt-out `vscode_ext_disabled`, throttled to once/24h).
    Idempotent: no-op when already installed or no VS Code-family CLI is present.
    Never raises — a hook must never break the session."""
    _measure_facade = _compat.facade()
    import time as _time  # noqa: PLC0415

    if _measure_facade._read_config_flag("vscode_ext_disabled", False):
        return
    label, cli = _measure_facade._detect_vscode_family_cli()
    if not cli:
        return
    # Throttle: at most one attempt per 24h. Installing once is enough; the window
    # avoids per-session `--list-extensions` spawns while still re-offering to a
    # user who removed it. Stamp BEFORE the work so a hang never re-fires next tick.
    now = _time.time()
    try:
        if now - float(_measure_facade._read_config_flag("vscode_ext_last_check", 0) or 0) < 86400:
            return
    except (TypeError, ValueError):
        pass
    _measure_facade._write_config_flag("vscode_ext_last_check", now)
    try:
        listed = _measure_facade.subprocess.run(
            [cli, "--list-extensions"], capture_output=True, text=True, timeout=15, creationflags=_measure_facade._NO_WINDOW
        )
    except (OSError, _measure_facade.subprocess.SubprocessError):
        return
    if listed.returncode == 0 and _measure_facade._VSCODE_EXTENSION_ID.lower() in (listed.stdout or "").lower():
        return  # already installed
    try:
        r = _measure_facade.subprocess.run(
            [cli, "--install-extension", _measure_facade._VSCODE_EXTENSION_ID],
            capture_output=True, text=True, timeout=120, creationflags=_measure_facade._NO_WINDOW,
        )
    except (OSError, _measure_facade.subprocess.SubprocessError):
        return
    if r.returncode == 0:
        print(
            f"  [Token Optimizer] Installed the VS Code status-bar extension in {label}. "
            "Reload the window to see it (Cmd/Ctrl+Shift+P -> Reload Window). "
            "Opt out anytime: measure.py setup-vscode-extension --uninstall"
        )


def _read_config_flag(key, default=None):
    """Read a single flag from config.json. Returns default on any error.

    A key the user's config.json does not set falls back to the repo-scoped
    project layer ($CLAUDE_PROJECT_DIR/.claude/token-optimizer.json, allowlisted
    non-secret keys only; see runtime_env.project_config), so a Claude Code
    cloud session whose home is fresh every time still sees the team's
    committed opt-outs. The user's own value always wins."""
    _measure_facade = _compat.facade()
    try:
        if _measure_facade.CONFIG_PATH.exists():
            cfg = _measure_facade.json.loads(_measure_facade.CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and key in cfg:
                return cfg[key]
    except (_measure_facade.json.JSONDecodeError, OSError):
        pass
    try:
        value = _measure_facade.project_config_value(key)
        if value is not None:
            return value
    except Exception:
        pass
    return default


def _config_lock():
    """Bounded portable lease for config.json writes.

    The v5 toggle endpoint can trigger 5+ concurrent read-modify-write
    cycles on CONFIG_PATH when a user rage-clicks dashboard checkboxes,
    and SessionStart ensure-health also writes last_hook_heal_check.
    Without serialization, interleaved writers silently clobber each other's
    keys. Contenders skip the mutation after 75ms rather than blocking a hook.
    """
    _measure_facade = _compat.facade()
    lease_path = _measure_facade._CONFIG_LOCK_PATH.with_suffix(".lease")
    with _measure_facade.lease_lock(
        lease_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def _write_config_flag(key, value):
    """Merge a single flag into config.json. Non-fatal on I/O errors.

    v5.4.19: wrapped in _config_lock + tempfile + os.replace
    so concurrent writers (daemon toggles, ensure-health timestamps,
    welcome flags) never race on a non-atomic write. Readers always see
    either the pre-write state or the fully-written state, never partial
    JSON. tmp_path=None sentinel prevents the finally clause from
    unlinking an already-renamed destination.
    """
    _measure_facade = _compat.facade()
    try:
        _measure_facade.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    with _measure_facade._config_lock() as acquired:
        if not acquired:
            return
        cfg = {}
        if _measure_facade.CONFIG_PATH.exists():
            try:
                loaded = _measure_facade.json.loads(_measure_facade.CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg = loaded
            except (_measure_facade.json.JSONDecodeError, OSError):
                pass
        cfg[key] = value
        tmp_path = None
        try:
            tmp_fd, tmp_path = _measure_facade.tempfile.mkstemp(
                dir=str(_measure_facade.CONFIG_DIR),
                prefix=".config-",
                suffix=".json",
            )
            with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                _measure_facade.json.dump(cfg, f, indent=2)
                f.write("\n")
            _measure_facade.os.replace(tmp_path, str(_measure_facade.CONFIG_PATH))
            tmp_path = None
        except OSError:
            pass
        finally:
            if tmp_path is not None:
                try:
                    _measure_facade.os.unlink(tmp_path)
                except OSError:
                    pass


def _settings_env_paths():
    # Path.cwd() raises FileNotFoundError if the working directory was deleted out
    # from under a long-lived process (plausible for the daemon). Degrade to the
    # global settings only rather than crashing every caller.
    _measure_facade = _compat.facade()
    paths = []
    try:
        cwd = _measure_facade.Path.cwd()
    except OSError:
        cwd = None
    if cwd is not None:
        paths.append(cwd / ".claude" / "settings.local.json")
        paths.append(cwd / ".claude" / "settings.json")
    paths.append(_measure_facade.CLAUDE_DIR / "settings.local.json")
    paths.append(_measure_facade.CLAUDE_DIR / "settings.json")
    return tuple(paths)


def _load_settings_file(path):
    """Return the parsed dict for a settings.json-style file, or None if it is
    absent, not a regular file, unreadable, malformed, or not a JSON object.

    is_file() (not exists()) is deliberate: it is False for a directory, broken
    symlink, or FIFO, so a hostile repo cannot plant a named pipe at
    .claude/settings.json and hang the dashboard on open(). UnicodeDecodeError
    (a ValueError, not an OSError) is caught so a non-UTF-8 file degrades to None
    instead of crashing the run.
    """
    _measure_facade = _compat.facade()
    try:
        if not path.is_file():
            return None
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(path)
    cached = _measure_facade._settings_file_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    parsed = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = _measure_facade.json.load(f)
        if isinstance(loaded, dict):
            parsed = loaded
    except (_measure_facade.json.JSONDecodeError, UnicodeDecodeError, PermissionError, OSError):
        parsed = None
    _measure_facade._settings_file_cache[key] = (mtime, parsed)
    return parsed


def _settings_env_value(var_name):
    """Return the value of an env var as configured in any settings.json `env`
    block (project local/shared first, then global), or None if absent.

    Lets feature-enablement detection see flags a user set in settings.json even
    when the generating process did not inherit that env injection.
    """
    _measure_facade = _compat.facade()
    for path in _measure_facade._settings_env_paths():
        loaded = _measure_facade._load_settings_file(path)
        if loaded is None:
            continue
        env = loaded.get("env")
        if isinstance(env, dict) and var_name in env:
            val = env[var_name]
            return str(val) if val is not None else None
    return None


def _resolve_feature_env(env_var):
    """Effective env-var value for a feature: real process env wins (it already
    includes settings.json injection when present), else fall back to reading
    settings.json directly. Returns None when the var is set nowhere."""
    _measure_facade = _compat.facade()
    env_val = _measure_facade.os.environ.get(env_var)
    if env_val is not None:
        return env_val
    return _measure_facade._settings_env_value(env_var)


def _model_routing_handled_elsewhere():
    """True when subagent models are already routed outside settings.json.

    A model-routing plugin sets the model per Agent call, so a pinned main
    model is a choice, not a leak, and "remove the model key" saves nothing
    (issue #275). Matches any enabled plugin with "router" in its name, plus
    an explicit opt-out for routing done some other way.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_SKIP_MODEL_PIN_HINT", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        plugins = _measure_facade._resolve_settings_value("enabledPlugins", {}) or {}
    except Exception:
        return False
    if not isinstance(plugins, dict):
        return False
    return any(
        enabled and "router" in str(name).split("@", 1)[0].lower()
        for name, enabled in plugins.items()
    )


def _resolve_settings_value(key, default=None):
    """Return a top-level settings.json key (e.g. includeGitInstructions, model)
    resolving project local/shared over global local/shared, first match wins.
    Lets recommendations honour a setting the user marked at the project level,
    not just in global ~/.claude/settings.json."""
    _measure_facade = _compat.facade()
    for path in _measure_facade._settings_env_paths():
        loaded = _measure_facade._load_settings_file(path)
        if loaded is None:
            continue
        if key in loaded:
            return loaded[key]
    return default


def _env_val_enables(feature_name, env_val):
    """Back-compat shim: interpret a feature's value via the shared gate.

    Retained for any external/test caller. Resolves tri-state via the registry's
    env_truthy_value and coerces the shared interpreter's "unrecognized" (None)
    to the feature default, preserving the historical bool return. New code
    should call plugin_env.interpret_flag_value directly."""
    _measure_facade = _compat.facade()
    feat = _measure_facade.V5_FEATURES.get(feature_name) or {}
    r = _measure_facade.interpret_flag_value(env_val, env_truthy_value=feat.get("env_truthy_value"))
    return r if r is not None else bool(feat.get("default", False))


def _is_v5_feature_enabled(feature_name):
    """Check if a v5 feature is enabled. Env var wins, then config, then default.

    Value interpretation is delegated to plugin_env.interpret_flag_value so the
    dashboard reader, the read-hook (is_v5_flag_enabled), and the env path all
    share ONE gate and can never disagree. An unrecognized value at
    a source falls through to the next; the registry default is the final
    fallback. The structure_map tri-state still enables only on the exact "beta"
    token (carried as env_truthy_value in the registry)."""
    _measure_facade = _compat.facade()
    feat = _measure_facade.V5_FEATURES.get(feature_name)
    if not feat:
        return False
    tv = feat.get("env_truthy_value")

    # Env var (process env or settings.json) wins.
    env_val = _measure_facade._resolve_feature_env(feat["env_var"])
    if env_val is not None:
        r = _measure_facade.interpret_flag_value(env_val, env_truthy_value=tv)
        if r is not None:
            return r

    # Config.json (user, then plugin-data, via _read_config_flag).
    config_val = _measure_facade._read_config_flag(feat["config_key"], None)
    if config_val is not None:
        r = _measure_facade.interpret_flag_value(config_val, env_truthy_value=tv)
        if r is not None:
            return r

    return feat["default"]


def _set_v5_feature(feature_name, enabled):
    """Enable or disable a v5 feature via config.json (persistent, dashboard-friendly)."""
    _measure_facade = _compat.facade()
    feat = _measure_facade.V5_FEATURES.get(feature_name)
    if not feat:
        return False
    _measure_facade._write_config_flag(feat["config_key"], bool(enabled))
    return True


def _show_v5_welcome():
    """Print the first-run welcome screen for v5 features."""
    _measure_facade = _compat.facade()
    print()
    print("  " + "=" * 68)
    print("  Token Optimizer v5: Active Compression")
    print("  " + "=" * 68)
    print()
    print("  Hi! Token Optimizer just got smarter. It can now actively reduce")
    print("  the tokens your sessions use -- not just measure them.")
    print()
    print("  Here's what's new. You can turn any of these on or off anytime")
    print("  from the dashboard (token-dashboard) or with the command:")
    print("    measure.py v5 enable <feature>")
    print("    measure.py v5 disable <feature>")
    print()

    status = _measure_facade._get_v5_feature_status()
    for name, info in status.items():
        mark = "[ON]" if info["enabled"] else "[OFF]"
        rec = " (recommended)" if info["recommended"] else ""
        print(f"  {mark} {info['label']}{rec}")
        print(f"       What it does: {info['what']}")
        print(f"       Risk: {info['risk']}")
        print()

    print("  " + "-" * 68)
    print("  DEFAULTS (what's on right now):")
    print("    - Quality Nudges      ON  (harmless, just warnings)")
    print("    - Loop Detection      ON  (harmless, just warnings)")
    print("    - Delta Mode          ON  (smart re-reads, big savings)")
    print("    - Bash Compression    ON  (lossy, disable: TOKEN_OPTIMIZER_BASH_COMPRESS=0)")
    print("    - Structure Map        ON  (soft-block, measurement: TOKEN_OPTIMIZER_STRUCTURE_MAP=beta)")
    print()
    print("  Want to change these? Three ways:")
    print("    1. Dashboard:  token-dashboard  (visit the Manage tab)")
    print("    2. CLI:        measure.py v5 enable|disable <feature>")
    print("    3. Env var:    TOKEN_OPTIMIZER_<NAME>=0 in your shell")
    print()
    print("  Full docs: measure.py v5 info <feature>")
    print("  All your data stays 100% local on your machine.")
    print()
    print("DATA NOTICE")
    print("Token Optimizer stores the following data locally on your machine:")
    print("  - Session metrics and trends (trends.db)")
    print("  - Checkpoint snapshots with truncated conversation context")
    print("  - Quality score cache")
    print("  - Tool output archives (auto-deleted after 24h)")
    print("  - Per-session file read cache (auto-deleted after 48h)")
    print()
    print("No data is transmitted externally. Zero network calls. Zero telemetry.")
    print("Run 'measure.py consent --show' to check consent status.")
    print("Run 'measure.py purge --confirm' to delete all Token Optimizer data.")
    print()
    _measure_facade._write_config_flag("enterprise_consent_shown", True)


def _get_v5_feature_status():
    """Return status dict for all v5 features (for dashboard/UI)."""
    _measure_facade = _compat.facade()
    status = {}
    codex_hooks_text = ""
    if _measure_facade.detect_runtime() == "codex":
        try:
            codex_hooks_text = (_measure_facade.Path.cwd() / ".codex" / "hooks.json").read_text(encoding="utf-8")
        except OSError:
            codex_hooks_text = ""
    for name, feat in _measure_facade.V5_FEATURES.items():
        # Resolve env from the process AND settings.json so the dashboard sees
        # flags a user "marked" even when this process (e.g. the daemon) did not
        # inherit Claude Code's env injection.
        env_val = _measure_facade._resolve_feature_env(feat["env_var"])
        config_val = _measure_facade._read_config_flag(feat["config_key"], None)
        enabled = _measure_facade._is_v5_feature_enabled(name)
        source = "default"
        recommended = feat["recommended"]
        if env_val is not None:
            # The user has taken manual control via an env var. Don't nag them to
            # enable it (and the dashboard toggle can't override an env var anyway),
            # regardless of whether the value enables or disables the feature.
            source = "env"
            recommended = False
        elif config_val is not None:
            source = "config"
        status[name] = {
            "label": feat["label"],
            "what": feat["what"],
            "value": feat.get("value", ""),
            "impact_pct": feat.get("impact_pct", 0),
            "how": feat["how"],
            "risk": feat["risk"],
            "risk_level": feat.get("risk_level", "none"),
            "recommended": recommended,
            "enabled": enabled,
            "source": source,
            "env_var": feat["env_var"],
            "config_key": feat["config_key"],
        }
        if _measure_facade.detect_runtime() == "codex":
            if name in {"quality_nudges", "loop_detection"}:
                hook_enabled = "UserPromptSubmit" in codex_hooks_text and "codex_hook_bridge.py" in codex_hooks_text
                status[name]["enabled"] = hook_enabled
                status[name]["source"] = "codex hook" if hook_enabled else "codex opt-in"
                status[name]["managed_by_hooks"] = True
                status[name]["how"] = "Requires the Codex UserPromptSubmit hook. The default balanced Codex install enables it; quiet mode disables live quality nudges."
            elif name == "bash_compress":
                hook_enabled = "PreToolUse" in codex_hooks_text and "bash_hook.py" in codex_hooks_text
                status[name]["enabled"] = False
                status[name]["recommended"] = False
                status[name]["source"] = "codex experimental hook" if hook_enabled else "codex api gap"
                status[name]["value"] = "Codex currently reports Bash usage, but true invisible Bash command rewriting is not supported yet."
                status[name]["how"] = "Codex PreToolUse can block commands, but current Codex support does not apply updatedInput, so invisible Bash compression stays experimental and opt-in."
            elif name in {"delta_mode", "structure_map_beta"}:
                status[name]["enabled"] = False
                status[name]["recommended"] = False
                status[name]["source"] = "codex api gap"
                status[name]["unavailable"] = True
                status[name]["value"] = "Not yet active in Codex. Token Optimizer measures the gap, but safe substitution needs richer Codex hook payloads."
                status[name]["how"] = "Claude can intercept read flows for this feature. Current Codex hooks do not expose Read tool payloads or a safe response-substitution path, so Token Optimizer will not pretend to enable it."
    return status


def _get_v5_savings_recommendation():
    """Calculate total potential savings from disabled v5 features.

    Returns a dict suitable for Overview tab display:
    {
        "total_disabled_impact_pct": 23,
        "disabled_features": [{label, impact_pct, config_key}, ...],
        "enabled_features": [{label}, ...],
        "recommendation": "Turn on 2 more features to save up to 23% more tokens per session",
        "has_savings_available": True,
    }
    """
    _measure_facade = _compat.facade()
    status = _measure_facade._get_v5_feature_status()
    disabled = []
    enabled = []
    total_impact = 0

    for name, info in status.items():
        if info["enabled"]:
            enabled.append({"name": name, "label": info["label"]})
        else:
            disabled.append({
                "name": name,
                "label": info["label"],
                "impact_pct": info["impact_pct"],
                "value": info["value"],
                "risk_level": info["risk_level"],
                "recommended": info["recommended"],
                "source": info.get("source", ""),
            })
            # Only count recommended features for the headline savings estimate
            if info["recommended"]:
                total_impact += info["impact_pct"]

    # Also include non-recommended but safe features for a secondary estimate.
    # An env-managed feature is excluded: if the user set it off via env that is a
    # deliberate opt-out, not headroom we should pitch back to them.
    _excluded_sources = {"codex api gap", "codex experimental hook", "env"}
    additional_impact = sum(
        f["impact_pct"] for f in disabled
        if not f["recommended"]
        and f["risk_level"] in ("none", "low")
        and f.get("source") not in _excluded_sources
    )
    aggressive_impact = total_impact + additional_impact + sum(
        f["impact_pct"] for f in disabled
        if not f["recommended"]
        and f["risk_level"] == "moderate"
        and f.get("source") not in _excluded_sources
    )

    has_savings = total_impact > 0 or aggressive_impact > 0

    if _measure_facade.detect_runtime() == "codex" and not has_savings:
        recommendation = "Codex-safe active features are enabled. Claude-only read substitution features are shown as API gaps, not promised savings."
    elif total_impact > 0:
        recommendation = f"Turn on {sum(1 for f in disabled if f['recommended'])} more recommended feature(s) to save up to {total_impact}% more tokens per session"
    elif aggressive_impact > 0:
        recommendation = f"All recommended features are on. Enable opt-in features for up to {aggressive_impact}% more savings (with trade-offs)"
    else:
        recommendation = "All v5 features enabled. You're running at maximum token efficiency."

    return {
        "total_disabled_impact_pct": total_impact,
        "aggressive_impact_pct": aggressive_impact,
        "disabled_features": disabled,
        "enabled_features": enabled,
        "recommendation": recommendation,
        "has_savings_available": has_savings,
        "runtime_note": "Codex cannot yet use Claude's delta-read or structure-map substitution hooks." if _measure_facade.detect_runtime() == "codex" else "",
    }


def setup_quality_bar(dry_run=None, uninstall=None, status_only=None, force=None, quiet=None,
                      user_initiated=None):
    """Install, uninstall, or check quality bar (status line + cache hook).

    Installs:
      1. UserPromptSubmit hook that updates quality cache every 2 min
      2. StatusLine config pointing to bundled statusline.js

    If user already has a foreign statusLine configured, shows integration
    instructions instead of replacing it — unless force=True, which is used
    by the SessionStart clobber-recovery path (presence of our cache hook
    is strong evidence the user had our full quality bar previously).

    quiet=True suppresses all user-facing output (the foreign-status-line
    advisory and install chatter) while preserving every side-effect. Used by
    the automated self-heal callers (UserPromptSubmit / SessionStart hooks) so
    a silent repair never re-injects the integration advisory into context on
    every turn. The advisory is only shown on an explicit,
    interactive `setup-quality-bar` run.

    user_initiated=True is passed only by the `setup-quality-bar` CLI command
    (the same for setup-hook / setup-smart-compact / compact-instructions):
    an explicit command takes the settings lease past the hook-herd
    reservation, so commands typed back to back are not refused. The
    automatic heal callers keep the default and the non-blocking lease.

    Side-effects on config.json:
      install        -> clears "quality_bar_disabled" (explicit opt-in)
      --uninstall    -> sets   "quality_bar_disabled" (sticky opt-out)
    """
    # Claude Code cloud has no status line, and in a cloud VM TO's hooks are
    # the cloud kit's, which its setup script merges into the VM's
    # ~/.claude/settings.json: a silent self-heal that adds our statusLine +
    # cache hook to that same file would double-register the hook. This holds
    # with the lean profile switched off (TOKEN_OPTIMIZER_REMOTE_LEAN=0) too:
    # turning lean off brings back daemon/keep-warm work, never a second copy
    # of the hooks. Only the automatic (quiet) callers are skipped; an
    # explicit `setup-quality-bar` still runs.
    _measure_facade = _compat.facade()
    if quiet and not (dry_run or uninstall or status_only) and (
            _measure_facade._remote_lean_profile() or _measure_facade.is_claude_code_cloud() or _measure_facade._cloud_kit_owns_hooks()):
        return
    # This function WRITES `settings` back. The old
    # `_read_settings_json()` collapsed a malformed / unreadable / missing file
    # to `{}`, and the write below then rebuilt settings.json from that `{}` --
    # erasing every key the user owned. Under a plugin install the cache hook is
    # skipped, so the rebuilt file contained ONLY `statusLine`: the exact
    # observed wipe signature. Refuse to act on an unknown state instead.
    # status_only is a pure read, so it keeps the lossy reader.
    if status_only:
        settings, settings_path = _measure_facade._read_settings_json()
    else:
        settings, _ok = _measure_facade._read_settings_for_write(allow_missing=not quiet)
        settings_path = _measure_facade.SETTINGS_PATH
        if not _ok:
            if not quiet:
                print(
                    "[Token Optimizer] settings.json is unreadable, malformed, or "
                    "missing right now; skipping quality bar setup rather than "
                    "risk overwriting it.",
                    file=_measure_facade.sys.stderr,
                )
            return
    current = _measure_facade._is_quality_bar_installed(settings)
    mp = _measure_facade._get_measure_py_path()
    sl_path = _measure_facade._get_statusline_path()

    if status_only:
        print("\n  Quality Bar Status")
        print(f"  {'=' * 40}")
        print(f"    Status line:  {'installed' if current['statusline'] else 'not installed'}")
        print(f"    Cache hook:   {'installed' if current['hook'] else 'not installed'}")
        if current["statusline"] and current["hook"]:
            print("\n  Quality Bar is fully active.")
        else:
            missing = []
            if not current["statusline"]:
                missing.append("status line")
            if not current["hook"]:
                missing.append("cache hook")
            print(f"\n  Missing: {', '.join(missing)}")
            print(_measure_facade._hint("  Run: python3 measure.py setup-quality-bar"))
        print()
        return

    if uninstall:
        hooks = settings.get("hooks", {})
        removed = 0

        # Remove UserPromptSubmit quality-cache hooks.
        # intent, do NOT broaden: this filter matches ONLY the legacy
        # `quality-cache` command literal on purpose. Unlike _is_our_hook_entry
        # (whose whitelist recognises the userpromptsubmit_runner.py
        # dispatcher), uninstall must NOT strip the dispatcher: it is multi-purpose
        # and deleting it here would take unrelated hooks down with the quality
        # bar. The narrow literal is the correct, deliberate scope for uninstall.
        if "UserPromptSubmit" in hooks:
            new_groups = []
            for group in hooks["UserPromptSubmit"]:
                new_hooks = [
                    h for h in group.get("hooks", [])
                    if "quality-cache" not in h.get("command", "")
                ]
                if new_hooks:
                    group["hooks"] = new_hooks
                    new_groups.append(group)
                else:
                    removed += 1
            if new_groups:
                hooks["UserPromptSubmit"] = new_groups
            else:
                del hooks["UserPromptSubmit"]

        # Remove statusLine if it's ours
        sl = settings.get("statusLine", {})
        if _measure_facade._statusline_cmd_is_ours(sl.get("command", "")):
            del settings["statusLine"]
            removed += 1

        if dry_run:
            print(f"\n  [Dry run] Would remove {removed} quality bar component(s)")
            print("  Run without --dry-run to apply.\n")
            return

        settings["hooks"] = hooks
        # Deliberate top-level removal: declare it so the choke-point guard
        # allows this one and only this one.
        if not _measure_facade._write_settings_atomic(settings, allow_removing_keys={"statusLine"},
                                      user_initiated=user_initiated):
            print("[Error] settings.json write was refused; Quality Bar was not removed.", file=_measure_facade.sys.stderr)
            return
        _measure_facade._set_quality_bar_disabled(True)
        print(f"[Token Optimizer] Quality bar removed. {removed} component(s) removed.")
        print("  Opt-out is sticky: SessionStart will not auto-restore.")
        print(_measure_facade._hint("  To re-enable later, run: python3 measure.py setup-quality-bar"))
        return

    # Install
    installed = []
    skipped = []
    warnings = []
    is_plugin = _measure_facade._is_running_from_plugin_cache() or _measure_facade._is_plugin_installed()

    # 1. UserPromptSubmit hook for quality cache
    # Skip when running as a plugin — hooks.json already provides this hook,
    # and writing it to settings.json creates a stale-path risk.
    if is_plugin and current["hook"]:
        skipped.append("cache hook (plugin hooks.json; settings.json entry is redundant)")
    elif is_plugin:
        skipped.append("cache hook (plugin hooks.json)")
    elif current["hook"]:
        skipped.append("cache hook")
    else:
        hooks = settings.setdefault("hooks", {})
        _py, _mp = _measure_facade._settings_hook_python_and_script(mp)
        hook_cmd = f"{_py} {_mp} quality-cache --quiet"
        hook_entry = {"type": "command", "command": hook_cmd}
        hook_group = {"hooks": [hook_entry]}
        hooks.setdefault("UserPromptSubmit", []).append(hook_group)
        installed.append("cache hook")

    # 2. StatusLine
    if current["statusline"]:
        skipped.append("status line")
    else:
        existing_sl = settings.get("statusLine", {})
        if not force and (existing_sl.get("command") or existing_sl.get("url")):
            # User has their own status line - don't replace (unless force=True,
            # used by ensure-health clobber recovery when our cache hook is
            # still present, which is strong evidence the foreign statusLine
            # is a clobber rather than an intentional choice).
            try:
                _rt_seg = _measure_facade.RUNTIME_DIR.relative_to(_measure_facade.HOME).as_posix()
                _rt_base = f"os.homedir(), '{_rt_seg}'"
            except ValueError:
                # Runtime dir outside HOME (e.g. CLAUDE_CONFIG_DIR on a
                # relocated volume): print its absolute path, never ~/.claude.
                _rt_base = _measure_facade.json.dumps(_measure_facade.RUNTIME_DIR.as_posix())
            warnings.append(
                "You already have a custom status line configured.\n"
                "  To integrate quality scoring, add this to your status line script:\n\n"
                "    // Read context quality score\n"
                f"    const qFile = path.join({_rt_base}, 'token-optimizer', 'quality-cache.json');\n"
                "    let qScore = '';\n"
                "    if (fs.existsSync(qFile)) {\n"
                "      try {\n"
                "        const q = JSON.parse(fs.readFileSync(qFile, 'utf8'));\n"
                "        const s = q.score;\n"
                "        if (s < 50) qScore = ' | \\x1b[31mContextQ:' + s + '\\x1b[0m';\n"
                "        else if (s < 70) qScore = ' | \\x1b[33mContextQ:' + s + '\\x1b[0m';\n"
                "        else qScore = ' | \\x1b[2mContextQ:' + s + '\\x1b[0m';\n"
                "      } catch (e) {}\n"
                "    }\n"
                "    // Append qScore to your output\n"
            )
            skipped.append("status line (custom detected)")
        else:
            settings["statusLine"] = {
                "type": "command",
                # A plain '...' wrap broke on an apostrophe in the path
                # (C:\Users\O'Brien). Keep the familiar form otherwise.
                "command": (f"node '{sl_path}'" if "'" not in str(sl_path)
                            else f"node {_measure_facade.shlex.quote(str(sl_path))}"),
            }
            installed.append("status line")

    if dry_run:
        print("\n  [Dry run] Quality Bar preview")
        print(f"  {'=' * 40}")
        if installed:
            print(f"  Would install: {', '.join(installed)}")
        if skipped:
            print(f"  Already installed / skipped: {', '.join(skipped)}")
        if warnings:
            print()
            for w in warnings:
                print(f"  Note: {w}")
        print("\n  Run without --dry-run to apply.\n")
        return

    if not installed and not warnings:
        # Already fully installed — still make sure the opt-out flag is clear
        # (handles the rare case where a user manually set the flag but also
        # still has the components in place).
        _measure_facade._set_quality_bar_disabled(False)
        if not quiet:
            print("[Token Optimizer] Quality bar already fully installed.")
        return

    if installed:
        if not _measure_facade._write_settings_atomic(settings, user_initiated=user_initiated):
            if not quiet:
                print("[Error] settings.json write was refused; Quality Bar was not installed.", file=_measure_facade.sys.stderr)
            return
        # Explicit install is an explicit opt-in — clear any prior opt-out.
        _measure_facade._set_quality_bar_disabled(False)

    if quiet:
        # Automated self-heal context (hooks). All side-effects above are
        # already applied; stay silent so the foreign-status-line advisory and
        # install chatter never bloat per-turn / per-session context.
        return

    if installed:
        print("[Token Optimizer] Quality Bar installed.")
        print(f"  Components: {', '.join(installed)}")
        if skipped:
            print(f"  Already had: {', '.join(skipped)}")
        print("\n  What you'll see:")
        print("    Status line:  model | effort | project ████ 43% | ContextQ:74")
        print("    Quality updates every ~2 minutes during active sessions")
        print("    Colors: green (85%+), dim (70-84%), yellow (50-69%), red (<50%)")
        print(_measure_facade._hint("\n  To remove: python3 measure.py setup-quality-bar --uninstall"))

    if warnings:
        print()
        for w in warnings:
            print(f"  {w}")
        if "cache hook" in installed:
            print("  The cache hook is installed. Quality data will be written to:")
            print(f"    {_measure_facade.QUALITY_CACHE_PATH}")


def _settings_heal_dir() -> Path:
    """Resolve the directory where self-heal data lives.

    Defaults to ``CONFIG_DIR / "settings_heal"`` which is
    ``~/.claude/token-optimizer/settings_heal`` on a standard install -- the
    same threat model as settings.json itself, so we harden permissions.
    """
    _measure_facade = _compat.facade()
    return _measure_facade.CONFIG_DIR / "settings_heal"


def _settings_heal_ensure_dir() -> Path | None:
    """Create the heal dir with 0o700 permissions and return it, or None on
    failure. Idempotent: if the dir exists, verifies and tightens permissions."""
    _measure_facade = _compat.facade()
    try:
        d = _measure_facade._settings_heal_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            _measure_facade.os.chmod(str(d), 0o700)
        except OSError:
            pass
        return d
    except OSError:
        return None


def _settings_write_file_secure(path: Path, content: str) -> bool:
    """Write a file with 0o600 permissions (atomic via tempfile + rename).
    Returns True on success."""
    _measure_facade = _compat.facade()
    try:
        d = path.parent
        d.mkdir(parents=True, exist_ok=True)
        try:
            _measure_facade.os.chmod(str(d), 0o700)
        except OSError:
            pass
        fd, tmp = _measure_facade.tempfile.mkstemp(dir=str(d), prefix=".heal-", suffix=".tmp")
        try:
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            try:
                _measure_facade.os.chmod(tmp, 0o600)
            except OSError:
                pass
            _measure_facade.os.replace(tmp, str(path))
            return True
        finally:
            try:
                if _measure_facade.os.path.exists(tmp):
                    _measure_facade.os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass
    return False


def _settings_is_wipe_residue(data: dict) -> bool:
    """SURVIVOR FINGERPRINT: True when every key in the live file is
    machine-written, OR the file is empty {}. If even one user-authored key
    survives, this is NOT a wipe -- the user was editing deliberately.

    An empty {} is treated as wipe residue because the most complete wipe
    leaves exactly {}. A fresh install also starts as {}, but a fresh install
    has no high-water mark to restore from, so the heal is a no-op in that
    case. This is the safe default: if we can't tell, we check for a
    high-water mark and only heal if one exists."""
    _measure_facade = _compat.facade()
    if not isinstance(data, dict):
        return False
    if not data:
        return True  # empty {} is the most complete wipe
    return all(k in _measure_facade._SETTINGS_MACHINE_WRITTEN_KEYS for k in data)


def _settings_has_user_keys(data: dict) -> bool:
    """True when the data has at least one user-authored key (outside the
    machine-written set). Used to decide whether to snapshot vs. heal."""
    _measure_facade = _compat.facade()
    if not isinstance(data, dict):
        return False
    return any(k not in _measure_facade._SETTINGS_MACHINE_WRITTEN_KEYS for k in data)


def _settings_read_highwater() -> dict | None:
    """Read the high-water mark snapshot, or None if it doesn't exist."""
    _measure_facade = _compat.facade()
    try:
        p = _measure_facade._settings_heal_dir() / "highwater.json"
        if p.is_file():
            data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (_measure_facade.json.JSONDecodeError, OSError):
        pass
    return None


def _settings_update_highwater(data: dict) -> bool:
    """Update the high-water mark if this sighting has MORE top-level keys
    AND has at least one user-authored key. A file with only machine-written
    keys is never a valid high-water candidate (it could be wipe residue
    with more keys than the real highwater). Returns True if updated."""
    _measure_facade = _compat.facade()
    if not isinstance(data, dict) or not data:
        return False
    # Poisoning guard: only snapshot files that have user-authored keys.
    # A file with only machine-written keys is wipe residue, not a valid
    # high-water candidate.
    if not _measure_facade._settings_has_user_keys(data):
        return False
    try:
        d = _measure_facade._settings_heal_ensure_dir()
        if d is None:
            return False
        existing = _measure_facade._settings_read_highwater()
        if existing is None or len(data) > len(existing):
            if not _measure_facade._settings_write_file_secure(
                d / "highwater.json",
                _measure_facade.json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            ):
                return False
            # Record metadata.
            meta = {
                "key_count": len(data),
                "key_set": sorted(data.keys()),
                "timestamp": _measure_facade.datetime.now().isoformat(),
            }
            _measure_facade._settings_write_file_secure(
                d / "highwater.meta.json",
                _measure_facade.json.dumps(meta, indent=2) + "\n",
            )
            return True
    except (OSError, TypeError):
        pass
    return False


def _settings_read_tombstones() -> set:
    """Read the set of tombstoned keys (keys the user removed after a heal)."""
    _measure_facade = _compat.facade()
    try:
        p = _measure_facade._settings_heal_dir() / "tombstones.json"
        if p.is_file():
            data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return set(data.get("tombstoned_keys", []))
    except (_measure_facade.json.JSONDecodeError, OSError):
        pass
    return set()


def _settings_write_tombstone(key: str) -> None:
    """Add a tombstone for a key the user deliberately removed post-heal.
    Uses atomic write via _settings_write_file_secure."""
    _measure_facade = _compat.facade()
    try:
        d = _measure_facade._settings_heal_ensure_dir()
        if d is None:
            return
        p = d / "tombstones.json"
        existing = set()
        if p.is_file():
            data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = set(data.get("tombstoned_keys", []))
        existing.add(key)
        _measure_facade._settings_write_file_secure(
            p,
            _measure_facade.json.dumps(
                {"tombstoned_keys": sorted(existing),
                 "updated": _measure_facade.datetime.now().isoformat()},
                indent=2,
            ) + "\n",
        )
    except (OSError, _measure_facade.json.JSONDecodeError):
        pass


def _settings_heal_log(action: str, **fields) -> None:
    """Append a structured record to the heal log (JSONL). Capped at 1000
    entries to prevent unbounded growth."""
    _measure_facade = _compat.facade()
    try:
        d = _measure_facade._settings_heal_ensure_dir()
        if d is None:
            return
        entry = {"timestamp": _measure_facade.datetime.now().isoformat(), "action": action}
        entry.update(fields)
        log_path = d / "heal_log.jsonl"
        # Cap log size: if >1000 lines, keep the last 500.
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8").strip().split("\n")
                if len(lines) > 1000:
                    _measure_facade._settings_write_file_secure(
                        log_path,
                        "\n".join(lines[-500:]) + "\n",
                    )
            except (OSError, _measure_facade.json.JSONDecodeError):
                pass
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_measure_facade.json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _settings_check_tombstones(live: dict, previously_restored: set) -> None:
    """If the live file is healthy (has user keys) and a previously restored
    key is now absent, tombstone it. Called on every healthy sighting."""
    _measure_facade = _compat.facade()
    if not previously_restored:
        return
    if not _measure_facade._settings_has_user_keys(live):
        return
    for key in previously_restored:
        if key not in live:
            _measure_facade._settings_write_tombstone(key)


def _settings_read_restored_keys() -> set:
    """Read the set of keys restored in the last heal (for tombstone tracking).
    Reads from the END of the log for efficiency."""
    _measure_facade = _compat.facade()
    try:
        p = _measure_facade._settings_heal_dir() / "heal_log.jsonl"
        if not p.is_file():
            return set()
        # Read the last few lines to find the most recent restore entry.
        # This avoids loading the entire log on every healthy sighting.
        lines = p.read_text(encoding="utf-8").strip().split("\n")
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = _measure_facade.json.loads(line)
            if entry.get("action") == "restore":
                return set(entry.get("restored_keys", []))
        return set()
    except (OSError, _measure_facade.json.JSONDecodeError):
        pass
    return set()


def _settings_write_atomic_silent(settings_data, allow_removing_keys=None) -> bool:
    """Silent variant of _write_settings_atomic: suppresses the guard's stderr
    refusal message so the heal stays silent to the user. The heal's own log
    captures the outcome."""
    # Save and restore stderr to suppress the guard's refusal message.
    # The guard prints to sys.stderr when it refuses a write; for the heal
    # path, that message would leak to the user's SessionStart output.
    _measure_facade = _compat.facade()
    _old_stderr = _measure_facade.sys.stderr
    try:
        _measure_facade.sys.stderr = open(_measure_facade.os.devnull, "w")
        return _measure_facade._write_settings_atomic(settings_data, allow_removing_keys)
    finally:
        try:
            _measure_facade.sys.stderr.close()
        except Exception:
            pass
        _measure_facade.sys.stderr = _old_stderr


def _settings_reconstruct_extra_known_marketplaces() -> dict | None:
    """Rebuild extraKnownMarketplaces from the authoritative on-disk record:
    ~/.claude/plugins/known_marketplaces.json.

    The settings.json version only stores the ``source`` sub-object (not
    installLocation/lastUpdated/autoUpdate), so we extract just that from
    each marketplace entry. This is the live authoritative record -- it
    reflects corrections the user made (e.g., fixing a stale path) that a
    two-week-old snapshot would not have.
    """
    _measure_facade = _compat.facade()
    try:
        p = _measure_facade.CLAUDE_DIR / "plugins" / "known_marketplaces.json"
        if not p.is_file():
            return None
        data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        result = {}
        for name, entry in data.items():
            if isinstance(entry, dict) and isinstance(entry.get("source"), dict):
                source = entry["source"]
                # Validate: source must have a "source" string key.
                if isinstance(source.get("source"), str):
                    result[name] = {"source": source}
        return result if result else None
    except (_measure_facade.json.JSONDecodeError, OSError):
        return None


def _settings_scan_transcripts_for_plugin_evidence(
    days: int = None, deadline_seconds: float = None,
) -> set:
    """Scan recent session transcripts for evidence of plugin use.

    Returns a set of plugin KEYS (``pluginname@marketplace``) that have
    evidence of actual use. Evidence means:
      * a plugin-qualified skill name like ``pluginname:skillname`` in a
        Skill tool_use block, OR
      * a plugin MCP surface like ``mcp__plugin_<pluginname>_<server>__``
        in a tool_use block.

    Time-bounded: bails out after ``deadline_seconds`` and returns whatever
    evidence was gathered so far. This is the fail-open principle: a partial
    scan is better than no scan, and the heal must not block SessionStart.
    """
    _measure_facade = _compat.facade()
    try:
        # Build plugin name -> set of full keys map.
        p = _measure_facade.CLAUDE_DIR / "plugins" / "installed_plugins.json"
        if not p.is_file():
            return set()
        data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()
        plugins = data.get("plugins", {})
        if not isinstance(plugins, dict):
            return set()
        name_to_keys: dict[str, set[str]] = {}
        for plugin_key in plugins:
            parts = plugin_key.split("@", 1)
            if len(parts) == 2:
                name_to_keys.setdefault(parts[0], set()).add(plugin_key)
        if not name_to_keys:
            return set()

        # Build a single regex matching any known plugin name.
        names_sorted = sorted(name_to_keys.keys(), key=len, reverse=True)
        name_alt = "|".join(_measure_facade.re.escape(n) for n in names_sorted)
        mcp_re = _measure_facade.re.compile(r"mcp__plugin_(" + name_alt + r")_\w")
        skill_re = _measure_facade.re.compile(r'"skill"\s*:\s*"(' + name_alt + r'):\w')

        evidence_keys: set[str] = set()
        start = _measure_facade.time.time()
        jsonl_files = _measure_facade._find_all_jsonl_files(days=days)
        for jf, _mtime, _project in jsonl_files:
            if _measure_facade.time.time() - start > deadline_seconds:
                break  # bail out: partial evidence is better than blocking
            try:
                content = jf.read_text(encoding="utf-8", errors="ignore")
                for m in mcp_re.finditer(content):
                    for key in name_to_keys.get(m.group(1), ()):
                        evidence_keys.add(key)
                for m in skill_re.finditer(content):
                    for key in name_to_keys.get(m.group(1), ()):
                        evidence_keys.add(key)
            except OSError:
                continue
        return evidence_keys
    except Exception:
        return set()


def _settings_reconstruct_enabled_plugins() -> dict | None:
    """Rebuild enabledPlugins from installed_plugins.json (user scope)
    intersected with transcript evidence of actual use.

    A plugin is included iff:
      1. It has a user-scope install record in installed_plugins.json, AND
      2. There is evidence of its use in recent session transcripts (a
         plugin-qualified skill name or MCP surface).

    This correctly EXCLUDES plugins that are installed but deliberately
    disabled (no transcript evidence because the user stopped using them).
    The installed-plugins list alone would wrongly re-enable them.
    """
    _measure_facade = _compat.facade()
    try:
        p = _measure_facade.CLAUDE_DIR / "plugins" / "installed_plugins.json"
        if not p.is_file():
            return None
        data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        plugins = data.get("plugins", {})
        if not isinstance(plugins, dict):
            return None

        # Step 1: collect user-scope plugin keys.
        user_plugins = set()
        for plugin_key, records in plugins.items():
            if not isinstance(records, list):
                continue
            for rec in records:
                if isinstance(rec, dict) and rec.get("scope") == "user":
                    user_plugins.add(plugin_key)
                    break

        # Step 2: scan transcripts for evidence of actual use.
        evidence = _measure_facade._settings_scan_transcripts_for_plugin_evidence()

        # Step 3: intersect -- only plugins that are both installed (user
        # scope) AND have evidence of use.
        enabled = {k: True for k in user_plugins & evidence}
        return enabled if enabled else None
    except (_measure_facade.json.JSONDecodeError, OSError):
        return None


def _settings_validate_value(key: str, value) -> bool:
    """Schema-validate a settings.json value before restoring it.

    A malformed value is worse than a missing key: a stale or corrupt
    extraKnownMarketplaces entry can break plugin discovery, a malformed
    enabledPlugins can re-enable a disabled plugin. Every restored value
    must pass this check before it is written.
    """
    _measure_facade = _compat.facade()
    if key == "extraKnownMarketplaces":
        if not isinstance(value, dict):
            return False
        for name, entry in value.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                return False
            source = entry.get("source")
            if not isinstance(source, dict):
                return False
            if not isinstance(source.get("source"), str):
                return False
        return True
    if key == "enabledPlugins":
        if not isinstance(value, dict):
            return False
        for k, v in value.items():
            if not isinstance(k, str) or not isinstance(v, bool):
                return False
        return True
    if key == "env":
        if not isinstance(value, dict):
            return False
        return all(isinstance(k, str) and isinstance(v, str)
                   for k, v in value.items())
    if key == "cleanupPeriodDays":
        return isinstance(value, int) and value > 0
    if key == "statusLine":
        if not isinstance(value, dict):
            return False
        return isinstance(value.get("command"), str)
    if key == "compactInstructions":
        return isinstance(value, str)
    if key == "hooks":
        if not isinstance(value, dict):
            return False
        return True  # basic shape check; hooks are machine-managed
    if key == "mcpServers":
        if not isinstance(value, dict):
            return False
        return True  # basic shape check
    # For unknown/user keys, accept any JSON-serializable value.
    try:
        _measure_facade.json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _settings_resolve_restore_value(key: str, highwater: dict) -> tuple:
    """Resolve the value to restore for a given absent key.

    Returns (value, source) where source is one of:
      "authoritative" -- rebuilt from the live on-disk record
      "snapshot"      -- taken from the high-water mark snapshot
      (None, None)    -- could not resolve; skip this key

    For keys with an authoritative source (extraKnownMarketplaces,
    enabledPlugins), the snapshot is NEVER used -- we always rebuild from
    disk. For all other keys, we fall back to the snapshot.
    """
    # Try authoritative source first.
    _measure_facade = _compat.facade()
    reconstructor = _measure_facade._SETTINGS_AUTHORITATIVE_RECONSTRUCTORS.get(key)
    if reconstructor is not None:
        try:
            value = reconstructor()
            if value is not None and _measure_facade._settings_validate_value(key, value):
                return value, "authoritative"
        except Exception:
            pass
        # If the authoritative source fails, do NOT fall back to the snapshot
        # for these keys. A stale snapshot value for extraKnownMarketplaces
        # or enabledPlugins is worse than no value (it can break plugin
        # discovery or re-enable a disabled plugin).
        return None, None

    # Fall back to snapshot for keys without an authoritative source.
    if key in highwater:
        value = highwater[key]
        if _measure_facade._settings_validate_value(key, value):
            return value, "snapshot"
    return None, None


def _maybe_self_heal_settings() -> None:
    """Silent self-heal for settings.json wipe damage. Called at the START of
    run_ensure_health, BEFORE any settings writes, so we see the file as the
    user left it.

    Flow:
      1. Read settings.json via the safe checked reader.
      2. If the file is healthy (has user-authored keys):
         a. Check tombstones: if a previously restored key is now absent,
            tombstone it.
         b. Snapshot: update the high-water mark if this sighting is richer.
         c. Record time-proximity signal (last healthy sighting timestamp).
      3. If the file is wipe residue (all machine-written keys, or empty {}):
         a. Read the high-water mark. If none, fail open (nothing to restore).
         b. Compute absent keys = high-water keys - live keys - tombstoned.
         c. If no absent keys, fail open.
         d. Backup the live file via _backup_settings_file. If backup fails,
            ABORT (no restore without a backup).
         e. Re-read settings.json under the settings lock to avoid value race.
         f. Additive merge: add only absent keys from the high-water mark.
         g. Write via _settings_write_atomic_silent (suppresses guard stderr).
         h. Log the restoration.

    SILENT: no stdout, no stderr to the user. All diagnostics go to the heal
    log. FAIL OPEN: any error returns without touching the file.
    """
    _measure_facade = _compat.facade()
    try:
        data, _path, ok = _measure_facade._read_settings_json_checked()
        if not ok or not isinstance(data, dict):
            return  # unreadable or malformed: fail open

        heal_dir = _measure_facade._settings_heal_ensure_dir()
        if heal_dir is None:
            return  # can't create heal dir: fail open

        # --- Healthy sighting: check tombstones + snapshot ---
        if _measure_facade._settings_has_user_keys(data):
            # Check if any previously restored keys were deliberately removed.
            previously_restored = _measure_facade._settings_read_restored_keys()
            _measure_facade._settings_check_tombstones(data, previously_restored)
            # Snapshot: update high-water mark if richer.
            if _measure_facade._settings_update_highwater(data):
                _measure_facade._settings_heal_log(
                    "snapshot",
                    key_count=len(data),
                    key_set=sorted(data.keys()),
                )
            # Time-proximity signal: record last healthy sighting timestamp.
            _measure_facade._settings_write_file_secure(
                heal_dir / "last_healthy.json",
                _measure_facade.json.dumps({"timestamp": _measure_facade.datetime.now().isoformat(),
                            "key_count": len(data)}) + "\n",
            )
            return  # healthy file: no heal needed

        # --- Wipe residue: check for heal conditions ---
        if not _measure_facade._settings_is_wipe_residue(data):
            return  # has unknown keys that aren't machine-written: fail open

        highwater = _measure_facade._settings_read_highwater()
        if not highwater:
            return  # no high-water mark: nothing to restore from

        # Compute absent keys (in highwater but not in live, not tombstoned).
        tombstones = _measure_facade._settings_read_tombstones()
        absent_keys = sorted(
            set(highwater.keys()) - set(data.keys()) - tombstones
        )
        if not absent_keys:
            return  # nothing to restore (all missing keys are tombstoned)

        # Pre-restore backup (reuses the existing _backup_settings_file helper).
        # If the backup fails, ABORT: no restore without a reversible backup.
        backup_dir = heal_dir / "backups"
        backup_path = _measure_facade._backup_settings_file(backup_dir)
        if backup_path is None:
            _measure_facade._settings_heal_log(
                "restore_aborted",
                reason="backup_failed",
                attempted_keys=absent_keys,
            )
            return  # fail open: no restore without a backup

        # Re-read settings.json to get the freshest state before merging.
        # This minimizes the value-race window: the initial read at the top
        # may be stale by the time we write. The _settings_write_guard inside
        # _write_settings_atomic will also re-read and refuse if keys were
        # added concurrently, but re-reading here ensures our merge uses the
        # latest values for present keys.
        fresh_data, _fresh_path, fresh_ok = _measure_facade._read_settings_json_checked()
        if fresh_ok and isinstance(fresh_data, dict):
            data = fresh_data
            # Recompute absent keys against the fresh read (a concurrent writer
            # may have added some of the keys we were about to restore).
            absent_keys = sorted(
                set(highwater.keys()) - set(data.keys()) - tombstones
            )
            if not absent_keys:
                return  # nothing to restore after fresh read

        # Additive merge: add ONLY absent keys.
        # Never overwrite a key that is present in the live file.
        # For each key, resolve the value:
        #   - Authoritative source (extraKnownMarketplaces, enabledPlugins):
        #     rebuild from the live on-disk record, NOT the snapshot.
        #   - All other keys: fall back to the high-water mark snapshot.
        # Every value is schema-validated before it is used. A malformed
        # value is worse than a missing key.
        merged = dict(data)
        restored = []
        sources = {}
        for key in absent_keys:
            value, source = _measure_facade._settings_resolve_restore_value(key, highwater)
            if value is not None:
                merged[key] = value
                restored.append(key)
                sources[key] = source
            # If value is None, skip this key: either the authoritative
            # source failed (and we do NOT fall back to a stale snapshot
            # for authoritative keys), or the snapshot value failed
            # schema validation.

        if not restored:
            return  # no keys could be restored after validation

        # Write via the silent atomic writer (suppresses guard stderr).
        # The guard will verify we're not dropping any keys.
        if _measure_facade._settings_write_atomic_silent(merged):
            _measure_facade._settings_heal_log(
                "restore",
                restored_keys=restored,
                sources=sources,
                from_snapshot=str(heal_dir / "highwater.json"),
                backup_path=str(backup_path),
                live_key_count=len(data),
                healed_key_count=len(merged),
            )
    except Exception:
        # Fail open, always. A heal that breaks a session is a worse bug
        # than the one it heals.
        pass


def run_ensure_health():
    """Body of the ensure-health subcommand. Extracted into its own function
    so the CLI dispatch can wrap the call in a hook wall-clock guard without
    reindenting 200 lines of logic. Called by SessionStart hook.

    Side effects: writes settings.json (cleanupPeriodDays, hook heal, path
    repair, quality bar install), writes config flags (heal timestamp,
    welcome shown, nudge shown), creates / prunes checkpoint and cache
    files, and may spawn a detached verified installer subprocess on
    script-install systems. All side effects are idempotent.

    Task ordering matters: fast, always-safe writes (cleanupPeriodDays)
    run first so they are guaranteed to complete even if a later task
    exhausts the wall-clock budget. (The old "bad env var removal" first
    task is gone: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is documented and is
    never touched here.)
    """
    # Foreign-runtime guardrail, defense-in-depth: every Claude
    # write below is gated on `not _is_codex`, so under OpenCode or Copilot
    # they would all fire against ~/.claude. The CLI dispatch already blocks
    # `ensure-health` for foreign runtimes; this early return also covers any
    # non-CLI caller (e.g. the Copilot sessionStart hook bridge).
    _measure_facade = _compat.facade()
    if _measure_facade._is_foreign_runtime():
        return

    _is_codex = _measure_facade.detect_runtime() == "codex"
    _is_hermes = _measure_facade.detect_runtime() == "hermes"
    # Positive identity, never a catch-all negation: foreign runtimes
    # (opencode, copilot) returned above, but if that guard is ever bypassed
    # they must not be treated as Claude and have ~/.claude written.
    _is_claude = _measure_facade.detect_runtime() == "claude"
    # Claude Code cloud VM: skip everything that only pays off on a machine
    # that outlives the session (see _remote_lean_profile).
    _lean = _measure_facade._remote_lean_profile()

    # Settings self-heal: runs BEFORE any other settings writes so we see the
    # file as the user left it. Claude Code ONLY -- the wipe bug only affects
    # ~/.claude/settings.json, and Codex/Hermes use different config formats.
    # Silent, additive, high-water-mark-based recovery for users already
    # whose settings.json has lost top-level keys. See
    # _maybe_self_heal_settings().
    if _is_claude:
        try:
            _measure_facade._maybe_self_heal_settings()
        except Exception:
            pass  # fail open: a heal error must never break SessionStart

    # Preserve session transcripts: set cleanupPeriodDays if not configured.
    # Claude Code only: writes to ~/.claude/settings.json.
    # Codex and Hermes don't have this setting; their sessions persist by default.
    # Not in a cloud VM: its transcripts go away with the VM regardless.
    if _is_claude and not _lean:
        try:
            # Writes this dict back, so it must not act on an
            # unknown-state or missing-file {}. The `_cp_data` truthiness check
            # below already blocked the empty case; `_read_settings_for_write`
            # makes the intent explicit and covers the malformed case too.
            _cp_data, _cp_ok = _measure_facade._read_settings_for_write()
            if _cp_ok and _cp_data and "cleanupPeriodDays" not in _cp_data:
                _cp_data = dict(_cp_data)
                _cp_data["cleanupPeriodDays"] = 99999
                if _measure_facade._write_settings_atomic(_cp_data):
                    print("  [Token Optimizer] Set cleanupPeriodDays=99999 (preserves transcripts for trends)", file=_measure_facade.sys.stderr)
                else:
                    print("  [Token Optimizer] cleanupPeriodDays was not changed (settings.json locked or refused).", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] cleanupPeriodDays write failed: {_e}", file=_measure_facade.sys.stderr)
    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: documented setting. ensure-health
    # used to AUTO-DELETE it from settings.json; that path is gone and must
    # never return. Read-only here: nothing to do at startup. See
    # _autocompact_pct_override_explanation (doctor) for the explain-only path.

    # Subagent prompt-cache TTL (1h): ADVISE-ONLY by default. The
    # session start never writes the key itself -- it keeps the cached payoff
    # verdict fresh (a detached background scan) and records what the verdict
    # says, so `subagent-cache status`, doctor, quick and coach can carry the
    # per-user recommendation. Only TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 opts
    # in to the automatic write + 14-day tripwire, and only that path can
    # return a user-facing line (one-time set / auto-revert notice).
    # Marker-gated BEFORE any settings read, so the steady-state session
    # start pays one stat/read and moves on (the "must stay inside the hook
    # time budget" rule). Claude Code only -- and never inside Cowork (which
    # must not read ~/.claude). Fail-open: a problem here must never break
    # SessionStart.
    if _is_claude and not _measure_facade.is_cowork():
        try:
            for _sc_line in _measure_facade._subagent_cache_session_start_lines():
                print(_measure_facade.json.dumps({"systemMessage": _sc_line}))
        except Exception:
            pass

    # Usage recommendations: keep the daily-measured record fresh.
    # Session start only stats the record and may spawn the detached refresh;
    # the measurement itself never runs inside a hook. Fail-open.
    if _is_claude and not _measure_facade.is_cowork():
        try:
            _measure_facade._recs_ensure_fresh()
        except Exception:
            pass

    # Capture the pristine structural baseline once on first run. Records the
    # pre-pruning prefix overhead that structural savings are measured against.
    # One-time (no-op once the snapshot exists); never blocks SessionStart.
    try:
        if _measure_facade._auto_capture_pristine_baseline():
            print("  [Token Optimizer] Captured baseline snapshot for structural savings", file=_measure_facade.sys.stderr)
    except Exception:
        pass

    # v5.3.8: auto-regenerate the dashboard HTML when the on-disk file
    # is stale relative to the currently installed plugin version.
    # Users upgrading via /plugin update keep seeing the OLD dashboard
    # because the file is only rewritten by /token-dashboard,
    # /token-optimizer, or the SessionEnd hook -- none of which fires
    # on /plugin update. Non-blocking: wrap in try/except so a bad
    # regen never breaks SessionStart.
    #
    # v5.4.9: dual marker check. Version string alone isn't enough --
    # if a future release shares TOKEN_OPTIMIZER_VERSION with existing
    # HTML but changes the data shape, users would silently see stale
    # numbers. We also check for a SHAPE marker keyed to the newest data
    # contract, so a dashboard generated before that contract is always
    # rebuilt even if its version string somehow matches. The marker tracks
    # the latest headline shape, so any dashboard missing the marker predates
    # the corrected calculation and is force-refreshed.
    try:
        if _measure_facade.DASHBOARD_PATH.exists() and not _lean:
            # Cheap staleness check via the sidecar meta (~40 bytes) instead of
            # scanning the 4-5MB HTML every SessionStart. The dashboard is fresh
            # iff the sidecar's version+shape match the running build. A missing
            # sidecar = a dashboard generated before this scheme: fall back to the
            # one-time full-HTML scan, then the regen writes the sidecar so every
            # subsequent session takes the cheap path.
            _fresh = False
            _meta_path = _measure_facade._dashboard_meta_path(_measure_facade.DASHBOARD_PATH)
            if _meta_path.exists():
                try:
                    _meta = _measure_facade.json.loads(_meta_path.read_text(encoding="utf-8"))
                    # An older session must NOT regenerate (and thus clobber with
                    # pre-fix code) a dashboard a NEWER build wrote; a strictly-newer
                    # on-disk build reads as fresh. See _dashboard_meta_is_fresh.
                    _fresh = _measure_facade._dashboard_meta_is_fresh(_meta)
                except (OSError, _measure_facade.json.JSONDecodeError, ValueError):
                    _fresh = False
            else:
                # Legacy dashboard with no sidecar: scan the HTML once.
                try:
                    with open(str(_measure_facade.DASHBOARD_PATH), "r", encoding="utf-8", errors="replace") as _f:
                        _html = _f.read()
                    _fresh = (f'"version": "{_measure_facade.TOKEN_OPTIMIZER_VERSION}"' in _html
                              and _measure_facade._DASHBOARD_SHAPE_MARKER in _html)
                except OSError:
                    _fresh = False
            if not _fresh:
                # Hand the ~9s rebuild to a DETACHED, unbounded, FORCED child
                # instead of running it in-process. run_ensure_health() runs under
                # an 8s SIGALRM budget (see the ensure-health dispatch), and this
                # rebuild is ~9s, so an in-process call reliably tripped
                # _HookTimeout (a BaseException the local `except Exception` cannot
                # catch). That aborted the WHOLE ensure-health tick BEFORE the
                # daemon-script auto-update block below ever ran -- so on a version
                # bump both the HTML AND the daemon script stayed stale, and unlike
                # the CLI dashboard path there was no detached-child fallback here
                # to ever catch the HTML up. The detached child finishes the forced
                # rebuild off the hot path (re-reading a now-healthy meter, which
                # clears the runway placeholder), and this cheap spawn returns at
                # once so execution always reaches the daemon-script update.
                # thundering herd: guard the spawn with a short-lived
                # inflight marker so N concurrent SessionStarts on a version bump
                # collapse to ONE detached rebuild instead of N racing os.replace
                # on the same file. Mirrors the sibling _daemon_install_lock(
                # soft_fail=True) serialisation the daemon-script update below
                # already uses; _dashboard_heal_spawn_due() fails OPEN so a lock
                # glitch never skips the heal.
                try:
                    _measure_facade._spawn_detached_dashboard_selfheal(days=30, force=True, hook_timeout=False)
                    print(f"  [Token Optimizer] Refreshing dashboard to v{_measure_facade.TOKEN_OPTIMIZER_VERSION} (background)", file=_measure_facade.sys.stderr)
                except Exception as _e:
                    print(f"  [Token Optimizer] dashboard refresh failed: {_e}", file=_measure_facade.sys.stderr)
    except Exception as _e:
        print(f"  [Token Optimizer] dashboard staleness check failed: {_e}", file=_measure_facade.sys.stderr)

    # Auto-regenerate daemon script when it's outdated (e.g. after plugin update).
    # The daemon is a generated file outside the plugin, so it doesn't update
    # automatically. Staleness marker (v5.4.6+): the generated script embeds the
    # version that created it. Any mismatch against the current running version
    # triggers a regen + restart. This is version-bound, not capability-bound,
    # so every future version bump propagates the daemon refresh automatically.
    #
    # Path-aware write (v5.4.7+): users who ran setup-daemon BEFORE the plugin-data
    # migration have a launchd plist / systemd unit / scheduled task pointing at
    # the legacy path (~/.claude/_backups/token-optimizer/dashboard-server.py).
    # Users who set it up AFTER point at the plugin-data SNAPSHOT_DIR. We write
    # to BOTH locations so whichever one the service manager expects gets served.
    try:
        current_marker = f'TOKEN_OPTIMIZER_DAEMON_VERSION = "{_measure_facade.TOKEN_OPTIMIZER_VERSION}"'
        legacy_dir = _measure_facade.RUNTIME_DIR / "_backups" / "token-optimizer"
        candidate_paths = set() if (_measure_facade._daemon_snapshot_sandboxed() or _lean) else {
            _measure_facade.SNAPSHOT_DIR / "dashboard-server.py", legacy_dir / "dashboard-server.py"}
        # The auto-update refresh writes dashboard-server.py (not the LaunchAgent
        # plist) and reloads via `launchctl kickstart`, which restarts the process
        # without re-registering the background item -- so it does NOT fire the
        # macOS "App Background Activity" banner. That banner comes from plist
        # rewrites and bootout+bootstrap, which the install/repair paths now guard
        # with _write_file_if_changed + _launchagent_loaded. So this block keeps its
        # original version-marker staleness check (daemon stays version-current).
        needs_refresh = False
        for p in candidate_paths:
            if p.exists():
                try:
                    if current_marker not in p.read_text(encoding="utf-8", errors="replace"):
                        needs_refresh = True
                        break
                except OSError:
                    continue
        if needs_refresh:
            # v5.4.19 (a follow-up fix): serialise concurrent ensure-health updates so
            # two Claude sessions starting simultaneously don't both write the
            # daemon script and fight over launchctl kickstart. soft_fail=True
            # means the second session just skips the update (the first will
            # land it within milliseconds).
            with _measure_facade._daemon_install_lock(soft_fail=True) as acquired:
                if not acquired:
                    # Another session is already refreshing -- no-op.
                    pass
                else:
                    # Make sure the per-install token exists before writing
                    # the new script (which references DAEMON_TOKEN_PATH).
                    _measure_facade._get_or_create_daemon_token()
                    new_script = _measure_facade._generate_daemon_script()
                    # Track per-path write
                    # outcomes so we can detect silent drift (e.g., legacy
                    # path is unwritable and every SessionStart loops through
                    # this code printing "Auto-updated" without the daemon
                    # ever actually picking up the new script).
                    write_failures = []
                    wrote_any = False
                    for p in candidate_paths:
                        try:
                            p.parent.mkdir(parents=True, exist_ok=True)
                            # atomic daemon-script write: temp sibling +
                            # os.replace instead of a raw write_text. This block
                            # runs under the 8s ensure-health hook budget; a
                            # SIGALRM (_HookTimeout) mid-write would truncate
                            # dashboard-server.py, leaving a syntactically broken
                            # daemon that never starts. os.replace swaps the file
                            # in one step so a reader/loader sees the old or the new
                            # script whole, never a torn one.
                            tmp_fd, tmp_name = _measure_facade.tempfile.mkstemp(
                                dir=str(p.parent), prefix=".dashsrv-", suffix=".tmp")
                            try:
                                if hasattr(_measure_facade.os, "fchmod"):  # POSIX only
                                    _measure_facade.os.fchmod(tmp_fd, 0o600)
                                with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as _f:
                                    _f.write(new_script)
                                _measure_facade.os.replace(tmp_name, str(p))
                            except BaseException:
                                # BaseException, not OSError: a mid-write
                                # _HookTimeout (the 8s budget) must still clean up
                                # the temp and propagate. OSError is caught below as
                                # a per-path write failure.
                                try:
                                    _measure_facade.os.unlink(tmp_name)
                                except OSError:
                                    pass
                                raise
                            wrote_any = True
                        except OSError as e:
                            write_failures.append((p, e))
                    if write_failures and not wrote_any:
                        # All candidate paths failed: surface so user can
                        # debug instead of silently no-oping every session.
                        _measure_facade.sys.stderr.write(
                            "[Token Optimizer] Warning: daemon auto-update "
                            "could not write any dashboard-server.py path:\n"
                        )
                        for p, e in write_failures:
                            _measure_facade.sys.stderr.write(f"  {p}: {e}\n")
                        # Bail out of the refresh flow — don't kickstart a
                        # daemon we haven't actually updated.
                        return
                    elif write_failures:
                        # Partial success: one path landed, another didn't.
                        # Log once so silent drift on the failing path is
                        # visible, then proceed with kickstart on the path
                        # that did update.
                        _measure_facade.sys.stderr.write(
                            "[Token Optimizer] Note: daemon auto-update "
                            "skipped unwritable path(s):\n"
                        )
                        for p, e in write_failures:
                            _measure_facade.sys.stderr.write(f"  {p}: {e}\n")
                    # Restart the daemon so the new script takes effect, then
                    # apply escalation + honest logging. Extracted to
                    # _apply_daemon_restart_outcome for testability (the reap /
                    # kickstart / verify path is the single source of truth).
                    # Gate the restart on the SHARED
                    # gate -- a version bump must not /Run a daemon whose user
                    # tombstoned/disabled it or whose install is condemned.
                    # The script write above is inert and may stand either way.
                    if _measure_facade._daemon_resurrection_blocked() is None:
                        _, _msg = _measure_facade._apply_daemon_restart_outcome(
                            _measure_facade._restart_dashboard_daemon(_measure_facade._normalized_platform()))
                        print(f"  [Token Optimizer] {_msg}", file=_measure_facade.sys.stderr)
    except Exception as _e:
        print(f"  [Token Optimizer] daemon auto-update check failed: {_e}", file=_measure_facade.sys.stderr)

    # Dashboard daemon self-heal: INSTALL it if missing, RESTART it if dead.
    # Default-on for every Claude user (loopback-only + token-authed + ~0 cost),
    # sticky `daemon_disabled` opt-out -- the quality-bar posture, not the
    # marker-gated keep-warm one. Runs AFTER the auto-update block above: that
    # block owns the has-daemon (stale-script) case; this owns the absent/dead
    # case so a lost plist / never-ran-the-audit user still gets the bookmarkable
    # localhost URL. Cheap-gate-first (config read + stat + 1s port probe) and the
    # costly install/restart is internally throttled, so the healthy hot path
    # stays inside the SessionStart budget. Non-blocking (try/except).
    try:
        _daemon_ensure = _measure_facade._ensure_dashboard_daemon()
        if _daemon_ensure == "installed":
            # User-visible onboarding notice (bookmarkable URL). Emitted as a
            # systemMessage so the CC UI shows it to the user without sending
            # it to the model. See the noop-install-failed branch below for the
            # full stdout/stderr/systemMessage rationale.
            print(_measure_facade.json.dumps({"systemMessage":
                "  [Token Optimizer] Dashboard daemon installed. "
                f"URL: http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer "
                "(path required). Opt out: measure.py setup-daemon --uninstall"}))
        elif _daemon_ensure == "restarted":
            print("  [Token Optimizer] Restarted the dashboard daemon.", file=_measure_facade.sys.stderr)
        elif _daemon_ensure == "restart-stale":
            # The dead-daemon restart came back still serving a stale version --
            # surface it instead of swallowing it silently until the throttle
            # window (a crashed-on-bind daemon would otherwise go unreported).
            print("  [Token Optimizer] dashboard daemon restarted but still "
                  "serving a stale version; run: measure.py setup-daemon",
                  file=_measure_facade.sys.stderr)
        elif _daemon_ensure == "noop-install-failed":
            # Persistent, user-recoverable state that the user MUST see.
            # Three channels, three outcomes on a SessionStart hook:
            #   - plain stdout  -> injected into model context every turn (tax)
            #   - stderr        -> invisible in the CC UI on exit 0 (only in
            #                      the Ctrl+O transcript), so the wedge goes
            #                      silent -- the exact failure the
            #                      persistent-disable wedge fix addresses.
            #   - systemMessage -> folded by the runner into the hook envelope,
            #                      rendered to the USER as "<hook> says: ...",
            #                      and NOT sent to the model (zero model tokens).
            # So a systemMessage is the only channel that is both user-visible
            # and model-silent. `measure.py doctor` also reports the marker.
            _mk_reason = _measure_facade._daemon_install_failed_reason()
            print(_measure_facade.json.dumps({"systemMessage":
                "  [Token Optimizer] Dashboard daemon self-heal disabled: "
                f"install failed permanently ({_mk_reason or 'reason unknown'}). "
                f"Retry: {_measure_facade._measure_cli()} setup-daemon"}))
    except Exception as _e:
        print(f"  [Token Optimizer] dashboard daemon self-heal failed: {_e}", file=_measure_facade.sys.stderr)

    # Bounded stale-lease sweeper. Self-heals the unbounded ``.qlease``
    # tombstone / orphan candidate / legacy ``.qlock`` litter that release()
    # intentionally leaves for the next contender (most sessions get none, so
    # they leak forever). Throttled to once per 24h via a one-stat marker, capped
    # at max_files per run, and fail-open -- it NEVER raises into the hook and
    # runs AFTER the daemon revive so it can never starve it. Not on the hot
    # per-tool-call path (this is the SessionStart ensure-health path only).
    try:
        _measure_facade.maybe_sweep_stale_leases()
    except Exception:
        pass

    # VS Code-family status-bar extension auto-install. Only meaningful for the
    # Claude runtime running inside a VS Code/Cursor/Windsurf editor; the detector
    # no-ops everywhere else (plain terminal, foreign runtimes). Default-on with a
    # sticky opt-out and a 24h throttle, mirroring the quality bar / daemon.
    # Non-blocking (try/except) so an editor CLI hiccup never breaks SessionStart.
    if _is_claude:
        try:
            _measure_facade._ensure_vscode_extension()
        except Exception as _e:
            print(f"  [Token Optimizer] VS Code extension self-install failed: {_e}", file=_measure_facade.sys.stderr)

    # Keep-warm scheduler repair. ADDITIVE + consent-gated +
    # cheap. Regenerates a user-deleted/stale keep-warm agent ONLY when the
    # install marker exists AND consent allows (sticky-opt-out:
    # user-removed != regen-target unless WE installed it and consent still
    # holds). The no-op hot path is file-existence checks only (no subprocess)
    # so it stays well inside the ensure-health budget. Completely separate from
    # the dashboard daemon block above: distinct label, own install lock, never
    # touches the dashboard plist/lock/regen loop. Non-blocking (try/except).
    try:
        _kw_repair = _measure_facade.keepwarm_scheduler_repair()
        if _kw_repair == "repaired":
            print("  [Token Optimizer] Repaired keep-warm scheduler agent.", file=_measure_facade.sys.stderr)
    except Exception as _e:
        print(f"  [Token Optimizer] keep-warm scheduler repair failed: {_e}", file=_measure_facade.sys.stderr)

    # v5.2.0 migration notice for Windows users. v5.1.0 and earlier shipped
    # a hooks.json that used POSIX shell syntax, which silently failed on
    # Windows PowerShell -- the plugin installed but every hook was a
    # no-op. After upgrade to v5.2.0 the wrapper-based hooks work. Surface
    # a one-time notice so users know to re-check their dashboard and run
    # the self-check. Shown exactly once per install (idempotent flag).
    try:
        if _measure_facade.platform.system() == "Windows":
            if not _measure_facade._read_config_flag("v52_windows_welcome_shown", False):
                selfcheck_path = str(_measure_facade.Path(_measure_facade.__file__).resolve())
                print("  [Token Optimizer v5.2.0] Windows support is now live.", file=_measure_facade.sys.stderr)
                print("  v5.1.0 hooks ran via POSIX shell and failed silently on Windows;", file=_measure_facade.sys.stderr)
                print("  v5.2.0 uses a cross-platform wrapper so everything now works.", file=_measure_facade.sys.stderr)
                print("  Verify with: python3 \"" + selfcheck_path + "\" health-selfcheck", file=_measure_facade.sys.stderr)
                _measure_facade._write_config_flag("v52_windows_welcome_shown", True)
    except Exception:
        pass
    # v5.0.2: Self-heal hooks with correct semantics for each install mode.
    # Claude Code only: reads/writes ~/.claude/settings.json for hook management.
    # Never in a cloud VM (lean profile): the cloud kit's setup script wires
    # the hooks into the VM's ~/.claude/settings.json, so adding the canonical
    # set there too would fire every hook twice (setup_all_hooks also stands
    # down via _cloud_kit_owns_hooks when the lean profile is off).
    if not _is_codex and not _lean:
        try:
            last_check = _measure_facade._read_config_flag("last_hook_heal_check", 0)
            now = int(_measure_facade.time.time())
            if now - int(last_check or 0) > 86400:  # 24h
                # Layer 2: rewrite/remove the collect&&dashboard fossil in
                # BOTH install modes. Exact-identity dedup cannot match the
                # renamed shape, and setup_all_hooks is additive-only.
                try:
                    fossil = _measure_facade._reconcile_sessionend_fossils()
                    rewritten = fossil.get("rewritten", 0)
                    removed_fossils = fossil.get("removed", 0) + fossil.get("stop_removed", 0)
                    if rewritten or removed_fossils:
                        print(
                            "  [Token Optimizer] Reconciled SessionEnd fossil hook(s) "
                            f"(rewritten={rewritten}, removed={removed_fossils}). Restart to apply.",
                            file=_measure_facade.sys.stderr,
                        )
                except Exception:
                    pass
                if _measure_facade._is_plugin_installed():
                    cleanup_result = _measure_facade._cleanup_duplicate_plugin_hooks_from_settings(dry_run=False)
                    removed = cleanup_result.get("removed", 0)
                    if removed > 0:
                        print(f"  [Token Optimizer] Removed {removed} duplicate hook(s) from settings.json. Restart to apply.", file=_measure_facade.sys.stderr)
                    _measure_facade._write_config_flag("last_hook_heal_check", now)
                else:
                    # Double-registration guard (2026-08-29): if our
                    # SessionStart hook is firing but ~/.claude/settings.json
                    # has ZERO Token Optimizer hooks, our hooks are coming
                    # from another settings layer (--settings flag, project
                    # settings, managed policy). Adding them here would
                    # double-register and every hook would fire twice. The
                    # host does not expose the --settings path or the merged
                    # hook set to hook subprocesses, so the detection is
                    # indirect: hook-firing + zero-our-hooks-in-settings.json
                    # → hooks are from another layer → skip.
                    _skip_result = _measure_facade._should_skip_self_heal_hooks()
                    _skip, _confident = _skip_result
                    if _skip:
                        if _confident:
                            # Confident skip: settings.json is readable and has
                            # zero TO hooks → hooks are from another layer → safe
                            # to advance the 24h throttle.
                            _measure_facade._write_config_flag("last_hook_heal_check", now)
                        # else: uncertain skip (missing/malformed settings.json).
                        # Do NOT advance the throttle so the next SessionStart
                        # re-checks sooner instead of locking out healing for 24h.
                        print("  [Token Optimizer] Hooks from another layer; skipping heal.",
                              file=_measure_facade.sys.stderr)
                    else:
                        heal_result = _measure_facade.setup_all_hooks(dry_run=False, verbose=False)
                        added = heal_result.get("added", 0)
                        if added > 0:
                            print(f"  [Token Optimizer] Self-healed {added} missing hook(s) in settings.json. Restart to activate.", file=_measure_facade.sys.stderr)
                        elif added == 0 and not heal_result.get("error"):
                            _measure_facade._write_config_flag("last_hook_heal_check", now)
        except Exception:
            pass
    # v5.1: First-run welcome. Shows once when v5 is first seen on this machine.
    # Routed to stderr so the welcome banner never enters the model's context.
    # Not in a cloud VM: every cloud session starts on a fresh VM, so the
    # "first run" banner would repeat every session.
    try:
        if not _lean and not _measure_facade._read_config_flag("v5_welcome_shown", False):
            from contextlib import redirect_stdout as _redirect_welcome
            with _redirect_welcome(_measure_facade.sys.stderr):
                _measure_facade._show_v5_welcome()
            _measure_facade._write_config_flag("v5_welcome_shown", True)
    except Exception:
        pass
    # Fix stale versioned plugin cache paths in settings.json.
    # Claude Code only: reads/writes ~/.claude/settings.json.
    if not _is_codex:
        try:
            _stale_fixed = _measure_facade._fix_stale_settings_paths()
            if _stale_fixed:
                print(f"  [Token Optimizer] Fixed {_stale_fixed} stale plugin path(s) in settings.json", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] stale path fix failed: {_e}", file=_measure_facade.sys.stderr)
    # Lift an existing version-pinned statusLine onto the update-surviving
    # marketplace clone path so the installed base stops seeing a blank status
    # line on the first session after every plugin update (G3 C-P2-3). Runs
    # after the stale-path fix so the cache path is already current before it is
    # migrated to the clone. Claude Code only.
    if not _is_codex:
        try:
            if _measure_facade._migrate_statusline_to_stable_path():
                print("  [Token Optimizer] Migrated statusLine to stable path", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] statusLine path migration failed: {_e}", file=_measure_facade.sys.stderr)
        try:
            if _measure_facade._heal_dangling_statusline():
                print("  [Token Optimizer] Re-pointed a statusLine that ran a missing "
                      "or Codex-cache statusline.js", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] statusLine heal failed: {_e}", file=_measure_facade.sys.stderr)
        # Same class for the keep-warm launchd agent: an existing plist pinned to a
        # version-cache measure.py path dies on plugin update and freezes the
        # dashboard's savings. Lift it onto the clone path (macOS-only in practice).
        try:
            if _measure_facade._heal_keepwarm_plist_path():
                print("  [Token Optimizer] Healed keep-warm agent to stable path", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] keep-warm path heal failed: {_e}", file=_measure_facade.sys.stderr)
        # Same class again: a Windows install made by an older build
        # already has a console-flashing Scheduled Task action and .cmd shim on
        # disk. Fixing the generators only helps the next install, so repair the
        # installed artifacts in place. Strict no-op off Windows, and gated so it
        # can never resurrect a declined/uninstalled/failed daemon.
        try:
            if _measure_facade._heal_windows_console_flash():
                print("  [Token Optimizer] Healed Windows daemon launcher, console flashing removed", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] Windows console-flash heal failed: {_e}", file=_measure_facade.sys.stderr)
    # Remove malformed hook commands (subshell patterns, double-$HOME paths).
    # Claude Code only: reads/writes ~/.claude/settings.json.
    if not _is_codex:
        try:
            _malformed_fixed = _measure_facade._fix_malformed_hook_commands()
            if _malformed_fixed:
                print(f"  [Token Optimizer] Removed {_malformed_fixed} malformed hook(s) from settings.json. Restart to apply.", file=_measure_facade.sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] malformed hook fix failed: {_e}", file=_measure_facade.sys.stderr)
    # Plugin cleanup is available as `measure.py plugin-cleanup` but NOT auto-run.
    # Deleting cache dirs on SessionStart can break plugins that load hooks from
    # dogfood/development paths. Users should run it manually after review.
    # Migrate data to CLAUDE_PLUGIN_DATA on first run (v2.1.78+).
    # Idempotent per file: the "not exists" guard means a retry after
    # partial progress copies only the missing files. The marker is
    # written after the copy loop so a hook-budget timeout mid-copy
    # re-enters the migration on the next SessionStart and eventually
    # completes. Leaving the marker last preserves correctness under
    # interrupt; the only downside is slow completion on pathologically
    # slow filesystems, which is acceptable for a one-time path.
    # Migration also fires when plugin-data was discovered via installed_plugins
    # (CLI dashboard path), not just when Claude Code set the env var.
    _migration_target = _measure_facade._PLUGIN_DATA or (str(_measure_facade._RESOLVED_PLUGIN_DATA) if _measure_facade._RESOLVED_PLUGIN_DATA else None)
    if _migration_target:
        # Outer OSError guard closes a latent unhandled-exception path: if
        # SNAPSHOT_DIR / CONFIG_DIR mkdir fails (unwritable, disk full),
        # the OSError would otherwise propagate through _run_ensure_health
        # and miss the outer dispatch's _HookTimeout catch.
        try:
            _legacy_data = _measure_facade.RUNTIME_DIR / "_backups" / "token-optimizer"
            _legacy_config = _measure_facade.RUNTIME_DIR / "token-optimizer"
            _migrated_marker = _measure_facade.Path(_migration_target) / ".migrated"
            if not _migrated_marker.exists():
                import shutil
                _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                _measure_facade.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                for src_dir, dst_dir in [(_legacy_data, _measure_facade.SNAPSHOT_DIR), (_legacy_config, _measure_facade.CONFIG_DIR)]:
                    if src_dir.is_dir():
                        for f in src_dir.iterdir():
                            if f.is_file() and not (dst_dir / f.name).exists():
                                try:
                                    shutil.copy2(f, dst_dir / f.name)
                                except OSError:
                                    pass
                try:
                    _migrated_marker.touch()
                except OSError:
                    pass
        except OSError:
            pass
    # Clean up orphaned temp files from interrupted atomic writes
    # Note: .settings.lock is NOT cleaned up (zero-byte sentinel, not a leak;
    # deleting it while held could break the advisory lock for other processes)
    for f in _measure_facade.SETTINGS_PATH.parent.glob(".settings-*.json"):
        try:
            if _measure_facade.time.time() - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:
            pass
    # Sweep orphaned checkpoint temp files left by an interrupted atomic
    # write in _write_checkpoint_atomic. Defensive: the helper already
    # cleans up on OSError, but a signal interrupt between mkstemp and
    # the tuple unpack can leave an unnamed temp file on disk (rare in
    # CPython, but unhandled by the helper itself).
    try:
        for f in _measure_facade.CHECKPOINT_DIR.glob(".checkpoint-*.md"):
            try:
                if _measure_facade.time.time() - f.stat().st_mtime > 3600:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass
    # Prune old quality-cache and decisions files (older than 7 days)
    _prune_cutoff = _measure_facade.time.time() - 7 * 86400
    try:
        cache_files = sorted(
            _measure_facade.QUALITY_CACHE_DIR.glob("quality-cache-*.json"),
            key=lambda f: ((_measure_facade := _compat.facade()), f.stat().st_mtime)[1], reverse=True
        )
        for f in cache_files[10:]:  # Keep 10 most recent regardless of age
            try:
                if f.stat().st_mtime < _prune_cutoff:
                    f.unlink()
            except OSError:
                pass
    except (OSError, ValueError):
        pass
    try:
        decisions_dir = _measure_facade.SNAPSHOT_DIR / "read-cache" / "decisions"
        if decisions_dir.is_dir():
            for f in decisions_dir.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime < _prune_cutoff:
                        f.unlink()
                except OSError:
                    pass
    except (OSError, ValueError):
        pass
    # Auto-install / auto-restore quality bar on SessionStart.
    # Claude Code only: reads/writes ~/.claude/settings.json for statusLine and hooks.
    # Not in a cloud VM: there is no status line to show it in.
    if not _is_codex and not _lean:
        try:
            _eh_qb_disabled = _measure_facade._read_config_flag("quality_bar_disabled", False)
            _eh_is_plugin = _measure_facade._is_running_from_plugin_cache() or _measure_facade._is_plugin_installed()
            if not _eh_qb_disabled and _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
                try:
                    settings = _measure_facade.json.loads(_measure_facade.SETTINGS_PATH.read_text(encoding="utf-8"))
                except (_measure_facade.json.JSONDecodeError, ValueError):
                    # Malformed settings.json (often a transient partial write by
                    # another tool). Skip the quality-bar self-heal this session
                    # rather than risk overwriting a recoverable file — and stay
                    # silent so a broken file doesn't spam stderr every session.
                    settings = None
                if isinstance(settings, dict):
                    has_statusline = bool(settings.get("statusLine"))
                    if _eh_is_plugin:
                        # Plugin/marketplace installs: the cache hook is supplied by
                        # hooks.json (intentionally absent from settings.json,
                        # so there is no settings.json cache-hook signal to detect
                        # a clobber with. Manage only the statusLine, and act solely
                        # when it is ABSENT. This makes the quality bar default-on for
                        # plugin users without (a) re-running every session once it is
                        # present, (b) re-emitting the foreign-statusline advisory
                        # or (c) overwriting a user's own custom
                        # statusLine. setup_quality_bar skips the redundant settings
                        # hook for plugin installs; here it only ever writes our
                        # statusLine into an otherwise-empty slot.
                        if not has_statusline:
                            _measure_facade.setup_quality_bar(quiet=True)
                            # Only announce success if OUR statusLine actually landed.
                            # A concurrent foreign write in the read→write window would
                            # make setup_quality_bar skip silently; never claim an
                            # install we didn't deliver.
                            _eh_sl = (_measure_facade._read_settings_json()[0].get("statusLine") or {}).get("command", "") or ""
                            if _measure_facade._statusline_cmd_is_ours(_eh_sl):
                                print(
                                    "  [Token Optimizer] Quality statusline enabled "
                                    "(context % + quality score). "
                                    "Opt out: measure.py setup-quality-bar --uninstall",
                                    file=_measure_facade.sys.stderr,
                                )
                    else:
                        statusline_cmd = (settings.get("statusLine") or {}).get("command", "") or ""
                        statusline_is_ours = _measure_facade._statusline_cmd_is_ours(statusline_cmd)
                        hooks = settings.get("hooks", {}).get("UserPromptSubmit", [])
                        # Recognize the consolidated dispatcher as well as
                        # the legacy standalone hook, else this re-adds the
                        # legacy hook every SessionStart.
                        has_cache_hook = _measure_facade._quality_cache_hook_present(hooks)
                        if has_statusline and not statusline_is_ours and has_cache_hook:
                            print(
                                "  [Token Optimizer] Statusline was replaced (e.g. /statusline). "
                                "Auto-restored. Opt out: measure.py setup-quality-bar --uninstall",
                                file=_measure_facade.sys.stderr,
                            )
                            _measure_facade.setup_quality_bar(force=True, quiet=True)
                        elif not has_statusline or (has_statusline and not has_cache_hook):
                            _measure_facade.setup_quality_bar(quiet=True)
        except Exception as _e:
            print(f"  [Token Optimizer] quality bar setup failed: {_e}", file=_measure_facade.sys.stderr)
    # Auto-update check (once per day, script-installed users only).
    # Route through install.sh so script installs update to the latest verified
    # release tag and pass out-of-band checksums instead of pulling moving main.
    try:
        install_dir = _measure_facade.RUNTIME_DIR / "token-optimizer"
        update_marker = install_dir / ".last-update-check"
        installer = install_dir / "install.sh"
        if not _lean and (install_dir / ".git").is_dir() and installer.exists():
            should_check = True
            if update_marker.exists():
                age = _measure_facade.time.time() - update_marker.stat().st_mtime
                should_check = age > 86400  # Once per day
            if should_check:
                update_log = install_dir / ".last-update.log"
                log_fd = _measure_facade.os.open(str(update_log), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
                try:
                    _proc = _measure_facade.spawn_detached(
                        ["bash", str(installer)],
                        cwd=str(install_dir),
                        stdout=log_fd, stderr=_measure_facade.subprocess.STDOUT,
                    )
                finally:
                    # Popen duplicates the fd via start_new_session/creationflags;
                    # the parent must close its own copy so the fd doesn't leak
                    # across daily checks (EMFILE after ~1024 stale opens).
                    _measure_facade.os.close(log_fd)
                if _proc is None:
                    _measure_facade._log_spawn_failure("update installer spawn failed")
                update_marker.touch()
    except Exception:
        pass
    # One-time first-run nudge for marketplace users: Claude Code ships
    # third-party marketplaces with auto-update off by default, and plugin
    # authors cannot change that default. Tell the user how to flip it
    # so they get future bug fixes automatically. Shown exactly once,
    # suppressed if the user has opted out of the quality bar entirely,
    # and skipped for script-install / dev-symlink users (they get their
    # updates via the daily verified installer above or via their local checkout).
    try:
        already_shown = _measure_facade._read_config_flag("autoupdate_nudge_shown", False)
        qb_disabled = _measure_facade._read_config_flag("quality_bar_disabled", False)
        # Claude Code steps only: Codex loads the same plugin build from its
        # own plugin cache, and its users were told to open Claude Code's
        # /plugin menu.
        if (not _lean
                and _measure_facade.detect_runtime() == "claude"
                and _measure_facade._is_running_from_plugin_cache()
                and not already_shown
                and not qb_disabled):
            # User-visible onboarding tip. Emitted as a systemMessage so the
            # CC UI shows it to the user without sending it to the model.
            # See the noop-install-failed branch in the daemon block above for
            # the full stdout/stderr/systemMessage rationale.
            print(_measure_facade.json.dumps({"systemMessage":
                "  [Token Optimizer] First-run tip: enable auto-update for this marketplace\n"
                "  so you get bug fixes automatically. In Claude Code:\n\n"
                "      /plugin  ->  Marketplaces  ->  alexgreensh-token-optimizer\n"
                "               ->  Enable auto-update"}))
            _measure_facade._write_config_flag("autoupdate_nudge_shown", True)
    except Exception:
        pass


def _ensure_health_daemon_revive_first():
    """Dispatch the dashboard-daemon ensure/revive as a DETACHED child.

    The SessionStart path must add no measurable latency, so this NEVER performs
    the install/kickstart/verify work inline. It fires a fire-and-forget
    ``daemon-revive`` child (detached via spawn_detached: start_new_session on
    POSIX / DETACHED_PROCESS+breakaway on Windows, stdio -> DEVNULL) and returns
    immediately. The child reinstalls a missing plist / revives a dead daemon off
    the hot path; launchd then persists a once-installed daemon. The mid-session
    pulse provides a second, session-independent recovery path. Idempotent +
    fail-open: a spawn failure is swallowed, never raised into the hook.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._remote_lean_profile():
        return "noop-remote"
    # Frozen flag only (SessionStart hot path); the detached daemon-revive child
    # runs the writers, which apply the full _scheduler_install_refused().
    if _measure_facade._daemon_snapshot_sandboxed():
        return "noop-sandbox"
    try:
        _proc = _measure_facade.spawn_detached(
            [_measure_facade._detached_python_exe(), str(_measure_facade.MEASURE_PY_PATH), "daemon-revive"],
            stdout=_measure_facade.subprocess.DEVNULL, stderr=_measure_facade.subprocess.DEVNULL,
            stdin=_measure_facade.subprocess.DEVNULL)
        if _proc is None:
            _measure_facade._log_spawn_failure("SessionStart daemon-revive spawn failed")
    except Exception:
        # Fail-open: a daemon-revive spawn failure must never break SessionStart.
        pass
