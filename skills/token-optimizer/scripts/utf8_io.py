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
            child = _sp.Popen([sys.executable, "-X", "utf8", *sys.argv])
            # Wait for the child to finish so output ordering is preserved and
            # the parent's exit code reflects the child's result.
            try:
                child.wait()
                rc = child.returncode
            except (OSError, _sp.SubprocessError):
                rc = 1
            # Flush any parent-buffered output before exiting (os._exit skips
            # normal cleanup / atexit handlers, matching os.execv semantics).
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except (OSError, ValueError):
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
