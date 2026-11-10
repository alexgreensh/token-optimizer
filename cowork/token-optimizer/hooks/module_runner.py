#!/usr/bin/env python3
"""Runs a hook script as a module instead of as `__main__`, so CPython reuses
its __pycache__ bytecode across invocations.

Script-mode execution (`python foo.py`) never checks or writes __pycache__ for
the file being run as __main__ -- only for things it imports. Since every hook
call is a fresh process, running measure.py as a script would recompile it from
source every single time. Module-mode goes through the import system, which
checks and writes __pycache__, so only the first call pays the compile.

That first compile is not small. measure.py is ~2.7 MB / 58k lines (5.13.51).
Compiling it takes ~1.7 s on an Apple-silicon Mac and ~4 s under WSL2 Linux on
a 2015 i7, but 20-28 s with Windows CPython 3.12/3.13 on that same i7 (#264;
compile time grows faster than linearly with file size there). That is longer
than the host's hook timeouts (Stop 15 s, UserPromptSubmit/SessionStart 20 s),
and Python writes the .pyc only once the compile finishes, so a hook killed
mid-compile never warms the cache and every later call starts cold again.
_measure_bytecode_ready() therefore moves the cold compile into ONE detached
background process and lets the hook wait for it only as long as the host
deadline allows; a warm cache costs one stat and a 16-byte read. The
structural fix is splitting measure.py so each hook compiles only what it uses.

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
        if getattr(sys, "pycache_prefix", None):
            return  # bytecode goes to a cache prefix, scripts_dir needn't be writable
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
        # This marker lives at a predictable, world-shared temp path, so another
        # local user can pre-plant something hostile there (CWE-377). Defend
        # against all of it: unlink any existing entry first (drops a planted
        # symlink, FIFO, or hardlink by name -- a hardlink's target is left
        # untouched), then create our own EXCLUSIVELY. O_EXCL means we only ever
        # open a file we just created; O_NOFOLLOW + O_NONBLOCK close the tiny
        # unlink->open race (a re-planted symlink fails ELOOP, a re-planted FIFO
        # fails ENXIO instead of blocking the hook forever). Every failure here
        # is swallowed by the outer `except Exception` -> the hook stays
        # fail-open and just skips the once-a-day warning. O_NOFOLLOW/O_NONBLOCK
        # are absent on Windows; the getattr fallback of 0 makes them no-ops.
        try:
            os.unlink(marker)
        except OSError:
            pass
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
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


# --- Cold-compile gate (#264) ------------------------------------------------
# Entry points that import measure.py before doing anything, and how long each
# may wait for a background compile of it. Each wait leaves 8 s of the host's
# hook timeout (4.5 s for the Codex subagent bridge, whose own budget is 2.5 s)
# for interpreter start-up, loading the fresh .pyc and the hook's own work
# (1.0-1.5 s warm on the #264 machine):
#   UserPromptSubmit / SessionStart runners: host 20 s (Claude Code and Codex)
#   Stop runner: host 15 s (SessionEnd runs it async with 60 s)
#   measure.py subcommands: StopFailure is the tightest at 12 s
#   codex_hook_bridge: Codex SubagentStart/Stop, host 6 s
# A compile that finishes inside the wait gives exactly the old behaviour (the
# hook runs, warm). One that does not makes the hook exit 0 with no output, the
# host's "nothing to add", instead of being killed with the cache still cold.
_COLD_COMPILE_WAIT_S = {
    "userpromptsubmit_runner": 12.0,
    "sessionstart_runner": 12.0,
    "stop_runner": 7.0,
    "measure": 4.0,
    "codex_hook_bridge": 1.5,
}
# The PreCompact save (measure.py compact-capture --trigger auto) is the one
# measure.py hook whose skip loses data: no save means the restore after the
# compaction falls back to an older checkpoint. Its host timeout is 30 s and it
# runs async, so it can wait far longer than the 4 s the tightest measure.py
# hook allows. 12 s + its own 15 s budget (hook_runtime.BUDGET_PRECOMPACT_CAPTURE)
# + start-up stays under the 30 s.
_COLD_COMPILE_WAIT_PRECOMPACT_SAVE_S = 12.0


def _is_precompact_save(module_name: str, script_args) -> bool:
    args = list(script_args or ())
    if module_name != "measure" or not args or args[0] != "compact-capture":
        return False
    try:
        return args[args.index("--trigger") + 1] == "auto"
    except (ValueError, IndexError):
        return False
# Seconds; overrides every wait above. A negative value turns the gate off
# (compile inline, the pre-5.13.51 behaviour).
_COLD_COMPILE_WAIT_ENV = "TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S"
# A lock older than this belongs to a compile that died (killed, machine
# slept): the next hook takes over. ~4x the slowest compile measured (28 s).
_COLD_COMPILE_LOCK_STALE_S = 120.0
_COLD_COMPILE_LOCK = ".token-optimizer-compile.lock"
_COLD_COMPILE_FAILED = ".token-optimizer-compile.failed"
_COLD_COMPILE_TAG = "token-optimizer-bytecode-warmup"

# This closed set matches _measure.RUNTIME_SOURCES. No recursive repo scan.
_MEASURE_PACKAGE_SOURCES = (
    '__init__.py', '_compat.py', 'runtime.py', 'pricing.py', 'audit.py',
    'dashboard.py', 'lifecycle.py', 'sessions.py', 'trends.py', 'cache_policy.py',
    'daemon.py', 'quality_archive.py', 'continuity.py', 'savings.py', 'cli.py', 'dashboard_history.py', 'weekly_activity.py', 'headline.py', 'counted_position.py', 'counted_pricing_v5.py', 'transcript_backfill.py', 'counted_subagents.py', 'lean_resume_threshold.py', 'output_reread.py', 'session_effect.py', 'structural_ledger.py', 'structural_listing.py', 'structural_report.py', 'structural_seed.py', 'structural_value.py', 'rework_metrics.py', 'session_best.py', 'savings_story.py', 'counted_orphans.py', 'counted_cap.py', 'archive_void_evidence.py', 'compaction_survival.py', 'archive_void_codex.py', 'compression_fixture_void.py', 'structural_dedupe.py', 'archive_health.py', 'cost_per_message_trend.py', 'lean_resume_receivers.py', 'routing_period.py', 'fixture_context.py', 'transcript_product.py',
)


def _runtime_sources(measure_src):
    package = os.path.join(os.path.dirname(measure_src), '_measure')
    sources = (measure_src,)
    required = os.path.isdir(package)
    if not required:
        # Old unsplit fixture trees have no package. A damaged split install
        # must still include its missing providers in readiness/failure keys.
        try:
            with open(measure_src, 'rb') as stream:
                required = b'from _measure import _compat' in stream.read()
        except OSError:
            pass
    if required:
        sources += tuple(os.path.join(package, name) for name in _MEASURE_PACKAGE_SOURCES)
    return sources


def _source_set_key(measure_src):
    import hashlib
    records = []
    for source in _runtime_sources(measure_src):
        try:
            st = os.stat(source)
            records.append(f'{source}:{st.st_mtime_ns}:{st.st_size}')
        except OSError:
            records.append(f'{source}:missing')
    if len(records) == 1:
        st = os.stat(measure_src)
        return f'{st.st_mtime_ns}:{st.st_size}'
    return hashlib.sha256('\n'.join(records).encode('utf-8')).hexdigest()

# The detached compiler warms the entire eager runtime before dropping its
# lock, then warms the consolidated runners and the other flat scripts. A
# failure leaves a marker keyed to every required source's mtime and size,
# so hooks stop deferring to a compile that cannot succeed and compile inline
# as before.
_COMPILER_SOURCE = r'''
import sys
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
sys.dont_write_bytecode = False
import os, importlib.util, py_compile, json
_tag, lock, failed, prefix, opt, measure_src, hooks_dir, scripts_dir = sys.argv[1:9]
sources, source_key = json.loads(sys.argv[9]), sys.argv[10]
sys.pycache_prefix = prefix or None
opt = int(opt)
mode = py_compile.PycInvalidationMode.TIMESTAMP

def _fresh(src, cfile):
    try:
        st = os.stat(src)
        with open(cfile, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if len(head) < 16 or head[:4] != importlib.util.MAGIC_NUMBER:
        return False
    if int.from_bytes(head[4:8], "little") & 1:
        return True
    return (int.from_bytes(head[8:12], "little") == int(st.st_mtime) & 0xFFFFFFFF
            and int.from_bytes(head[12:16], "little") == st.st_size & 0xFFFFFFFF)

def _compile(src):
    cfile = importlib.util.cache_from_source(src, optimization="" if opt == 0 else opt)
    if not _fresh(src, cfile):
        py_compile.compile(src, cfile=cfile, doraise=True, optimize=opt, invalidation_mode=mode)

try:
    for source in sources:
        _compile(source)
except BaseException:
    try:
        with open(failed, "w", encoding="utf-8") as fh:
            fh.write(source_key)
    except OSError:
        pass
finally:
    try:
        os.unlink(lock)
    except OSError:
        pass
# Then the rest a hook imports: the consolidated runners and the scripts.
for folder, suffix in ((hooks_dir, "_runner.py"), (scripts_dir, ".py")):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        continue
    for name in names:
        src = os.path.join(folder, name)
        if name.endswith(suffix) and name[:-3].isidentifier() and src != measure_src:
            try:
                _compile(src)
            except BaseException:
                pass
'''


def _bytecode_path(source: str) -> str:
    """Where the import system reads and writes ``source``'s bytecode for this
    interpreter, honouring sys.pycache_prefix (PYTHONPYCACHEPREFIX, or the
    per-user prefix run.py chooses inside a git work tree or a hosted VM)."""
    import importlib.util

    return importlib.util.cache_from_source(source)


def _bytecode_fresh(source: str, cache: str) -> bool:
    """True when ``cache`` is bytecode the import system will accept for
    ``source`` without recompiling: one stat and a 16-byte read. The checks
    mirror importlib's: magic number, then the source mtime and size a
    timestamp .pyc records. A hash-based .pyc counts as fresh (import itself
    validates its hash)."""
    import importlib.util

    try:
        st = os.stat(source)
        with open(cache, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if len(head) < 16 or head[:4] != importlib.util.MAGIC_NUMBER:
        return False
    if int.from_bytes(head[4:8], "little") & 1:
        return True
    return (int.from_bytes(head[8:12], "little") == int(st.st_mtime) & 0xFFFFFFFF
            and int.from_bytes(head[12:16], "little") == st.st_size & 0xFFFFFFFF)


def _compile_failed_for(marker: str, source: str) -> bool:
    """The background compile already failed on this exact source."""
    try:
        with open(marker, encoding="utf-8") as fh:
            return fh.read(128).strip() == _source_set_key(source)
    except (OSError, ValueError):
        return False


def _take_compile_lock(lock: str):
    """True: this hook owns the compile. False: another hook's compile is
    running. None: no lock can be made here (read-only cache dir), so the
    caller compiles inline as before. A lock past its stale age is taken over
    once."""
    import time

    for _attempt in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                continue  # released between the two calls: try again
            if age < _COLD_COMPILE_LOCK_STALE_S:
                return False
            try:
                os.unlink(lock)
            except OSError:
                return False
            continue
        except OSError:
            return None
        try:
            os.write(fd, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
        except OSError:
            pass
        finally:
            os.close(fd)
        return True
    return False


def _start_background_compile(lock, failed, measure_src, plugin_scripts):
    """Start the one detached compiler. Returns its Popen, or None (the lock is
    then released again and the caller compiles inline)."""
    import subprocess
    import json

    hooks_dir = os.path.dirname(os.path.abspath(__file__))
    argv = [sys.executable, "-c", _COMPILER_SOURCE, _COLD_COMPILE_TAG, lock, failed,
            getattr(sys, "pycache_prefix", None) or "", str(sys.flags.optimize),
            measure_src, hooks_dir, plugin_scripts,
            json.dumps(_runtime_sources(measure_src)), _source_set_key(measure_src)]
    proc = None
    try:
        from spawn_utils import spawn_detached

        # stdio to DEVNULL: an inherited stdout would hold the hook's pipe
        # open, and the host waits for EOF on it.
        proc = spawn_detached(argv, cwd=plugin_scripts, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        proc = None
    if proc is None:
        try:
            os.unlink(lock)
        except OSError:
            pass
    return proc


def _measure_bytecode_ready(plugin_scripts: str, wait_s: float) -> bool:
    """False only when measure.py's bytecode is still being compiled in the
    background after ``wait_s``: the hook should then exit 0 without output.

    True means import as usual: the cache is warm, a compile finished inside
    the wait, or the background route is unavailable (no cache_tag, read-only
    cache dir, spawn refused, the compile failed or died), in which case the
    import compiles inline exactly as it did before this gate existed."""
    import time

    measure_src = os.path.join(plugin_scripts, "measure.py")
    try:
        cache = _bytecode_path(measure_src)
    except Exception:
        return True
    def ready():
        return all(_bytecode_fresh(source, _bytecode_path(source))
                   for source in _runtime_sources(measure_src))

    if ready():
        return True
    if not os.path.isfile(measure_src):
        return True
    cache_dir = os.path.dirname(cache)
    lock = os.path.join(cache_dir, _COLD_COMPILE_LOCK)
    failed = os.path.join(cache_dir, _COLD_COMPILE_FAILED)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return True
    end = time.monotonic() + max(0.0, wait_s)
    child = None
    while True:
        if ready():
            return True
        if _compile_failed_for(failed, measure_src):
            return True
        if child is not None and child.poll() is not None:
            # Ours exited and the cache is still cold: compile inline.
            return True
        if child is None:
            owned = _take_compile_lock(lock)
            if owned is None:
                return True
            if owned:
                child = _start_background_compile(lock, failed, measure_src, plugin_scripts)
                if child is None:
                    return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def _cold_compile_wait(module_name: str, script_args=()):
    """Seconds this entry may wait for a background compile, or None when the
    entry is not gated (it does not import measure.py up front)."""
    wait = _COLD_COMPILE_WAIT_S.get(module_name)
    if _is_precompact_save(module_name, script_args):
        wait = _COLD_COMPILE_WAIT_PRECOMPACT_SAVE_S
    if wait is None:
        return None
    raw = os.environ.get(_COLD_COMPILE_WAIT_ENV, "").strip()
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            pass
    return None if wait < 0 else wait


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

    # A host-set PYTHONDONTWRITEBYTECODE=1 would stop __pycache__ being written.
    sys.dont_write_bytecode = False

    sys.path = [p for p in sys.path if p not in ("", ".")]
    sys.path.insert(0, scripts_dir)
    sys.argv = [module_name, *script_args]

    # The plugin's own scripts dir (hook_runtime, spawn_utils, measure.py).
    # The consolidated runners live in hooks/, so scripts_dir is hooks/ for
    # them; APPENDED so it can never shadow a module of the hook being run.
    _plugin_scripts = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills", "token-optimizer", "scripts")
    if os.path.isdir(_plugin_scripts) and _plugin_scripts not in sys.path:
        sys.path.append(_plugin_scripts)

    # Before anything imports measure.py: never compile it on the hook's
    # deadline (#264). See _measure_bytecode_ready.
    _wait = _cold_compile_wait(module_name, script_args)
    if _wait is not None:
        try:
            if not _measure_bytecode_ready(_plugin_scripts, _wait):
                return 0
        except Exception:
            pass

    # Per-event entry budget first (PreToolUse / PostToolUse / Stop). These are
    # self-imposed deadlines that land far under the host's hooks.json timeout,
    # so an over-budget hook exits 0 with NO output instead of being killed
    # mid-write by the host. hook_runtime.resolve_entry_budget owns the table
    # and the evidence behind each number. When the entry point isn't budgeted
    # (consolidated Claude runners, or entries with no precise argv rule) we
    # fall back to the 110s orphan backstop described below.
    #
    # Generic Windows orphan-grandchild backstop, NOT a collect/dashboard fossil bound.
    #
    # When the host TerminateProcess-es run.py on Windows it bypasses run.py's
    # SIGTERM handler, orphaning this in-process grandchild while it still
    # holds the hook stdout pipe. HookDeadline's daemon thread calls
    # os._exit(0) so the pipe EOFs even when no parent is left to reap us.
    # The 110s sits a few seconds under run.py's 120s wait so a normally
    # reaped hook never trips it.
    #
    # This does NOT bound the collect/dashboard fossil: that fossil is a
    # raw `measure.py collect && dashboard` in settings.json that invokes
    # measure.py directly via the launcher, never through run.py/module_runner,
    # so this layer never sees it. The fossil is bounded by the 20s dispatch
    # budget in measure.py._dispatch_collect/_dispatch_dashboard,
    # armed only on the hook path. The session-end-flush path this DOES wrap
    # already self-bounds at 60s, so 110s is a last-resort backstop for any
    # OTHER hook that runs past run.py's wait while orphaned on Windows.
    # Fail-open if hook_runtime is missing (unit-test dummy plugin trees).
    # The consolidated runners live in hooks/, so scripts_dir is hooks/ and
    # hook_runtime (in skills/token-optimizer/scripts) was never importable for
    # them: the import failed, the except swallowed it, and every runner ran
    # with no backstop at all. It is found through the plugin's own scripts
    # dir, appended to sys.path above.
    deadline = None
    try:
        from hook_runtime import HookDeadline, arm_entry_budget
        deadline = arm_entry_budget(module_name, script_args)
        if deadline is None:
            deadline = HookDeadline(110).start()
    except Exception:
        deadline = None
    previous_measure = sys.modules.get('measure')
    try:
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
    finally:
        if previous_measure is None and module_name == 'measure':
            sys.modules.pop('measure', None)
        elif previous_measure is not None:
            sys.modules['measure'] = previous_measure
        if deadline is not None:
            try:
                deadline.cancel()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
