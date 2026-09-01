#!/usr/bin/env python3
"""Runs a hook script as a module instead of as `__main__`, so CPython reuses
its __pycache__ bytecode across invocations.

Script-mode execution (`python foo.py`) never checks or writes __pycache__ for
the file being run as __main__ -- only for things it imports. Since every hook
call is a fresh process, running measure.py (35k+ lines) as a script recompiles
it from source every single time: ~0.3s of pure CPython parse/compile on top of
whatever the hook actually does. Module-mode goes through the import system,
which does check/write __pycache__, cutting that to ~0.1s after the first call.

sys.path is stripped of '' and '.' (the cwd-equivalent entries the interpreter
would otherwise add) before inserting scripts_dir explicitly, so a same-named
file in the invoking project's own working directory (e.g. a project that
happens to have its own measure.py at its root) can never shadow the plugin's
module.
"""
from __future__ import annotations

import os
import runpy
import sys


def _warn_readonly_scripts_dir_once(scripts_dir: str) -> None:
    """If scripts_dir isn't writable, __pycache__ can't be written and CPython
    recompiles the target from source on every call — the perf win silently
    evaporates. Surface it, but at most once per day per user so we don't spam
    stderr on the per-tool-call hot path. The marker lives in the OS temp dir
    (writable even when the plugin install dir is read-only, which is the whole
    failure mode). Best-effort: any error here must never break the hook."""
    try:
        if os.access(scripts_dir, os.W_OK):
            return  # normal case — bytecode cache works, stay silent
        import hashlib
        import tempfile
        import time

        tag = hashlib.sha1(scripts_dir.encode("utf-8", "replace")).hexdigest()[:12]
        marker = os.path.join(tempfile.gettempdir(), f".token-optimizer-ro-pyc-{tag}")
        try:
            fresh = (time.time() - os.path.getmtime(marker)) < 86400
        except OSError:
            fresh = False
        if fresh:
            return
        # Open with O_NOFOLLOW so a symlink pre-created at this predictable,
        # world-shared temp path by another local user is refused (raises
        # ELOOP, swallowed by the outer `except Exception` below -> we just skip
        # the warning) rather than followed to a victim file (CWE-377). O_TRUNC
        # matches the "w" truncation. O_NOFOLLOW is absent on Windows; the
        # getattr fallback of 0 makes it a no-op flag there.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(marker, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
        sys.stderr.write(
            f"[Token Optimizer] note: {scripts_dir} is not writable, so Python "
            "bytecode (__pycache__) can't be cached — hooks will be slower than "
            "expected. Make the plugin scripts dir writable to restore the speedup.\n"
        )
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 3:
        return 0

    scripts_dir = sys.argv[1]
    module_name = sys.argv[2]
    script_args = sys.argv[3:]

    # Defense-in-depth: don't rely solely on run.py sanitizing these. Refuse a
    # module_name that isn't a bare Python identifier (blocks path separators,
    # dots, and traversal), and require the resolved target to actually exist in
    # scripts_dir. Fail OPEN (return 0) rather than raise — a hook must never
    # crash the tool call. Without this, any future caller that skipped run.py's
    # own validation would hand runpy an arbitrary-module-execution primitive.
    if not module_name.isidentifier():
        return 0
    if not os.path.isfile(os.path.join(scripts_dir, module_name + ".py")):
        return 0

    _warn_readonly_scripts_dir_once(scripts_dir)

    sys.path = [p for p in sys.path if p not in ("", ".")]
    sys.path.insert(0, scripts_dir)
    sys.argv = [module_name, *script_args]

    # Per-event entry budget first (PreToolUse / PostToolUse / Stop). These are
    # self-imposed deadlines that land far under the host's hooks.json timeout,
    # so an over-budget hook exits 0 with NO output instead of being killed
    # mid-write by the host. hook_runtime.resolve_entry_budget owns the table
    # and the evidence behind each number. When the entry point isn't budgeted
    # (consolidated Claude runners, or entries with no precise argv rule) we
    # fall back to the 110s orphan backstop described below.
    #
    # Generic Windows orphan-grandchild backstop, NOT a #114 fossil bound.
    #
    # When the host TerminateProcess-es run.py on Windows it bypasses run.py's
    # SIGTERM handler, orphaning this in-process grandchild while it still
    # holds the hook stdout pipe. HookDeadline's daemon thread calls
    # os._exit(0) so the pipe EOFs even when no parent is left to reap us.
    # The 110s sits a few seconds under run.py's 120s wait so a normally
    # reaped hook never trips it.
    #
    # This does NOT bound the #114 collect/dashboard fossil: that fossil is a
    # raw `measure.py collect && dashboard` in settings.json that invokes
    # measure.py directly via the launcher, never through run.py/module_runner,
    # so this layer never sees it. The fossil is bounded by the 20s dispatch
    # budget in measure.py._dispatch_collect/_dispatch_dashboard (#114 Fix 4),
    # armed only on the hook path. The session-end-flush path this DOES wrap
    # already self-bounds at 60s, so 110s is a last-resort backstop for any
    # OTHER hook that runs past run.py's wait while orphaned on Windows.
    # Fail-open if hook_runtime is missing (unit-test dummy plugin trees).
    deadline = None
    try:
        from hook_runtime import HookDeadline, arm_entry_budget
        deadline = arm_entry_budget(module_name, script_args)
        if deadline is None:
            deadline = HookDeadline(110).start()
    except Exception:
        deadline = None
    try:
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
    finally:
        if deadline is not None:
            try:
                deadline.cancel()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
