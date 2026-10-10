"""Process-wide UTF-8 I/O enforcement for Token Optimizer.

Token Optimizer runs as short-lived hook / statusline subprocesses spawned by the
host CLI. On hosts where the active encoding is not UTF-8 -- Windows ANSI codepages
(cp125x), an explicit non-UTF-8 LANG/LC_ALL, or PYTHONUTF8=0 -- Python's standard
streams (and the default open() encoding) fall back to that locale charset. Session
paths and transcript content frequently contain non-ASCII text (Hebrew, CJK, accented
names); decoding or printing it then raises UnicodeDecodeError / UnicodeEncodeError,
which aborts the hook before it can identify the session.

enforce_utf8_io() makes stdout / stderr / stdin UTF-8 with errors="replace" so a
stray undecodable byte degrades gracefully instead of crashing the process. It is
idempotent and safe to call at the top of every entry point. File reads still pass
encoding="utf-8" explicitly at their call sites; this module only covers the standard
streams, which have no per-call encoding hook.

Note: scripts dispatched through hooks/run.py also inherit PYTHONUTF8=1 /
PYTHONIOENCODING=utf-8 from the parent, which additionally makes the default open()
encoding UTF-8. This shim is the belt-and-suspenders for entry points invoked
directly by a host (statusline, hook bridges, the CLI).
"""

from __future__ import annotations

import os
import sys

_DONE = False

# Sentinel so a re-exec can never loop, even if the child still reports a
# non-UTF-8 preferred encoding for some pathological reason.
_REEXEC_FLAG = "TOKEN_OPTIMIZER_UTF8_REEXEC"


def _inheritable_stream(stream):
    """Return ``stream`` when it has a real OS handle Popen can pass through,
    else None (fall back to default inheritance).

    ``sys.std*`` can be None (GUI-subsystem ``pythonw`` -- the launcher
    swap) or a wrapper with no fileno (pytest capture, embedded hosts). Passing
    either to Popen raises, which would turn a UTF-8 convenience re-exec into a
    hard crash of the CLI. None keeps today's behavior for those callers.
    """
    if stream is None:
        return None
    try:
        fileno = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return stream if isinstance(fileno, int) and fileno >= 0 else None


def reexec_in_utf8_mode() -> None:
    """Re-exec this process under Python UTF-8 mode iff the locale isn't UTF-8.

    Stream reconfiguration (enforce_utf8_io) fixes stdout/stderr/stdin, but it
    cannot change locale.getpreferredencoding(), which is fixed at interpreter
    startup and drives the default open() encoding AND how subprocess(text=True)
    decodes child output. On a non-UTF-8 host the only way to flip all of those at
    once is to restart the interpreter with -X utf8 / PYTHONUTF8=1.

    This re-execs at most once (guarded by an env sentinel) and only when the
    active encoding is genuinely non-UTF-8, so UTF-8 users -- and scripts already
    launched with PYTHONUTF8=1 by hooks/run.py -- pay nothing. Call it as the very
    first statement of an entry point, before reading stdin or doing any work.
    Falls back silently (caller should still call enforce_utf8_io) if re-exec is
    unavailable (frozen build, no os.execv).
    """
    if os.environ.get(_REEXEC_FLAG) == "1":
        return
    if getattr(sys.flags, "utf8_mode", 0):
        return
    try:
        import locale
        enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "").replace("_", "")
    except (ImportError, LookupError):
        enc = ""
    # "cp65001"/"65001" = the Windows UTF-8 code page; treat as UTF-8 so Windows
    # users on it don't re-exec on every single hook invocation. "" = undetectable
    # (don't gamble on a re-exec we can't verify).
    if enc in ("utf8", "cp65001", "65001", ""):
        return
    os.environ[_REEXEC_FLAG] = "1"
    os.environ["PYTHONUTF8"] = "1"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        import subprocess as _sp

        if sys.platform.startswith("win"):
            # On Windows, os.execv does not quote the executable path, so a
            # path like "C:\Program Files\Python312\python.exe" gets split at
            # the space and the relaunched interpreter dies with
            # "can't open file '...\Files\Python312\python.exe'". Use
            # subprocess.Popen (which quotes correctly) and exit immediately
            # so the child takes over the role of this process.
            #
            # DETACHED_PROCESS gives the child NO console at all when the
            # parent is console-less (e.g. a hook spawned by the host).
            # CREATE_NO_WINDOW would still allocate a hidden console -- a
            # conhost.exe spawn that flashes nothing but leaks a kernel token
            # reference on the affected Windows build (issue #215). The flag
            # is kept UNCONDITIONALLY -- do NOT drop it when stdout is a
            # pipe. The flash/leak-sensitive case (a host-spawned
            # hook/statusline) is exactly the case where stdout is not a
            # tty; dropping the flag there would let Windows allocate a
            # console while fixing nothing, because the stdio-binding problem
            # below is solved by passing the handles explicitly, not by
            # removing the flag. The detached child inherits the parent's
            # stdio through those explicit handles (STARTF_USESTDHANDLES),
            # never through a console it does not have.
            #
            # Invariant: this function must be the FIRST statement of the entry
            # point (it already is at measure.py's __main__). If anything reads
            # stdin before the re-exec, those buffered bytes are lost to the
            # child, which continues reading the same stdin handle from the
            # byte after the parent left it.
            #
            # Flush the parent's buffered streams BEFORE spawning. Once the
            # child shares the parent's stdout/stderr fd, any parent-buffered
            # text flushed AFTER the spawn interleaves out of order behind the
            # child's own output. The post-wait flush below is kept as the
            # final safety net (os._exit skips normal cleanup / atexit
            # handlers, matching os.execv semantics).
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except (OSError, ValueError, AttributeError):
                    pass
            _popen_kwargs = {}
            # DETACHED_PROCESS only when no console is in play. If stdout is a
            # tty the CLI was typed into a console: a detached child would sit
            # off that console (no Ctrl+C, console handles used from outside
            # their console), and no conhost can flash, so the flag buys
            # nothing. Without it the child shares the console. Streams that
            # are not a tty (every host-spawned hook) keep the flag.
            try:
                _on_console = bool(sys.stdout is not None and sys.stdout.isatty())
            except (AttributeError, OSError, ValueError):
                _on_console = False
            _flags = getattr(_sp, "DETACHED_PROCESS", 0)
            if _flags and not _on_console:
                _popen_kwargs["creationflags"] = _flags
            # Pass the three std handles explicitly so CPython sets
            # STARTF_USESTDHANDLES with the parent's real handles and marks
            # them inheritable. This is the ONLY stdio channel a detached
            # child has: with all three left None its std handles bind to
            # NULL, and every byte it writes is silently discarded (the
            # "silent no-op" signature on a cp1252 host).
            # _inheritable_stream degrades to None for streams without a real
            # OS handle (pytest capture, sys.std* None under pythonw per the
            # launcher swap), so a UTF-8 convenience re-exec never hard-crashes
            # the CLI.
            # When at least one stream is usable, each unusable one gets DEVNULL
            # rather than being left out: CPython falls back to GetStdHandle for
            # an omitted stream, and a stale non-NULL value there makes
            # DuplicateHandle fail (WinError 6) so Popen raises and the re-exec
            # is silently skipped. With none usable nothing is passed.
            _missing = []
            for _name in ("stdin", "stdout", "stderr"):
                _s = _inheritable_stream(getattr(sys, _name, None))
                if _s is not None:
                    _popen_kwargs[_name] = _s
                else:
                    _missing.append(_name)
            if any(_n in _popen_kwargs for _n in ("stdin", "stdout", "stderr")):
                for _name in _missing:
                    _popen_kwargs[_name] = _sp.DEVNULL
            child = _sp.Popen([sys.executable, "-X", "utf8", *sys.argv],
                              **_popen_kwargs)
            # Wait for the child to finish so output ordering is preserved and
            # the parent's exit code reflects the child's result.
            try:
                child.wait()
                rc = child.returncode
            except (OSError, _sp.SubprocessError):
                rc = 1
            # Final flush of any parent-buffered output before exiting.
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except (OSError, ValueError, AttributeError):
                    pass
            os._exit(rc)
        else:
            os.execv(sys.executable, [sys.executable, "-X", "utf8", *sys.argv])
    except ImportError:
        # subprocess module unavailable (frozen / restricted): leave the sentinel
        # set and let the caller's enforce_utf8_io() + explicit per-call encodings
        # carry it.
        pass
    except (OSError, _sp.SubprocessError):
        # execv/Popen failed: same fallback as above.
        pass
    except Exception:
        # Any other exotic/restricted-environment failure: never crash the host
        # process over a UTF-8 re-exec. Leave the sentinel set and degrade to
        # enforce_utf8_io() + per-call encodings, exactly like the no-execv case.
        pass


def enforce_utf8_io() -> None:
    """Reconfigure std streams to UTF-8 (errors=replace). Idempotent and fail-safe."""
    global _DONE
    if _DONE:
        return
    _DONE = True
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Stream replaced (e.g. captured in tests) or not a TextIOWrapper.
            continue
        if (getattr(stream, "encoding", "") or "").lower() == "utf-8":
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            # Detached/closed stream, or reconfigure unsupported -- leave as-is.
            pass
