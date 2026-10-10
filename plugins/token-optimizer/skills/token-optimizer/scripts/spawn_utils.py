"""Cross-platform detached-subprocess spawn helpers.

On POSIX, ``start_new_session=True`` detaches the child into its own process
group. On Windows, ``start_new_session`` is silently ignored, so the child
inherits the parent's console (causing a ~1s window flash on every prompt) and
dies with the parent's job object. The Windows fix mirrors the daemon-revive
spawn in measure.py (~line 21658, CXP-1): OR together DETACHED_PROCESS,
CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB, and CREATE_NO_WINDOW via
getattr so the flags degrade to 0 on builds where an attribute is missing.

CREATE_NO_WINDOW is belt-and-suspenders here: Windows ignores it when
DETACHED_PROCESS is also set, so it changes nothing today, but it keeps the
invariant "every spawn site carries the no-flash flag" uniform and survives a
future refactor that drops DETACHED_PROCESS. It never makes CreateProcess fail.

Use ``detach_spawn_kwargs()`` for the raw kwargs dict, or ``spawn_detached()``
for fire-and-forget background spawns where the child must survive the parent
(session-end flush, daemon regen, rollup, etc.). ``spawn_detached`` retries
without ``CREATE_BREAKAWAY_FROM_JOB`` if ``CreateProcess`` fails with
``ACCESS_DENIED`` inside a restrictive Windows Job Object, then returns the
``Popen`` or ``None`` (never raises). After each call, the module-level
``last_spawn_used_fallback`` flag records whether the retry path was taken,
so a test (or caller) can assert the non-fallback path succeeded.

hooks/run.py does NOT use this helper: it must keep the child in its own job and
Ctrl+C domain (so no CREATE_NEW_PROCESS_GROUP, no CREATE_BREAKAWAY_FROM_JOB) and
must pass run.py's stdio through to the child. It does use DETACHED_PROCESS alone
(no console at all, issue #215) and hands the child its three std handles
explicitly (``_windows_stdio_kwargs`` there), because a detached child has no
console to inherit them from.
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Set by spawn_detached to True when the CREATE_BREAKAWAY_FROM_JOB retry path
# was taken (i.e. the first attempt failed and the fallback succeeded). Tests
# and callers can read this to assert the non-fallback path was used. Reset to
# False at the start of every spawn_detached call.
last_spawn_used_fallback: bool = False


def detach_spawn_kwargs():
    """Return Popen kwargs that detach the child on the current OS.

    Spread into ``subprocess.Popen(..., **detach_spawn_kwargs())`` alongside
    the caller's own kwargs (stdout, stderr, env, etc.).

    POSIX:  ``{"start_new_session": True}``
    Windows: ``{"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
              | CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW}``

    The Windows flag OR uses ``getattr(subprocess, name, 0)`` so a flag
    absent on an older Python build contributes 0 instead of raising.
    ``CREATE_NO_WINDOW`` is ignored by Windows while
    ``DETACHED_PROCESS`` is present; it is included so every consumer of this
    helper inherits the no-console-flash intent unconditionally.
    """
    if os.name == "nt":
        flags = 0
        for _name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",
                      "CREATE_BREAKAWAY_FROM_JOB", "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, _name, 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def spawn_detached(argv, **popen_kwargs):
    """Fire-and-forget detached spawn. Returns the ``Popen`` or ``None``.

    Combines the caller's kwargs with ``detach_spawn_kwargs()`` and calls
    ``subprocess.Popen``. On Windows, if ``CreateProcess`` raises ``OSError``
    (e.g. ``ACCESS_DENIED`` when ``CREATE_BREAKAWAY_FROM_JOB`` is not allowed
    inside a restrictive parent Job Object), retries ONCE with
    ``creationflags`` minus ``CREATE_BREAKAWAY_FROM_JOB`` (i.e.
    ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW``) and logs the
    fallback at warning level (no logging is configured in the runtime, so
    logger.debug would be dropped by lastResort at WARNING). Every caller
    already swallows ``OSError``, so without this retry the background worker
    would silently never spawn.

    After each call, ``last_spawn_used_fallback`` records whether the retry
    path was taken (True = fallback used, False = primary path succeeded or
    failed without retry). A test can assert this is False to prove the
    non-fallback path worked on a real Windows runner.

    Never raises: returns ``None`` on failure.
    """
    global last_spawn_used_fallback
    last_spawn_used_fallback = False
    kwargs = dict(popen_kwargs)
    kwargs.update(detach_spawn_kwargs())
    try:
        return subprocess.Popen(argv, **kwargs)
    except OSError:
        if os.name != "nt":
            logger.warning("[spawn_utils] Popen failed (non-nt): %r", argv)
            return None
        flags = kwargs.get("creationflags", 0)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if not (flags & breakaway):
            logger.warning("[spawn_utils] Popen failed, no breakaway to drop: %r", argv)
            return None
        kwargs["creationflags"] = flags & ~breakaway
        logger.warning(
            "[spawn_utils] retrying without CREATE_BREAKAWAY_FROM_JOB: %r", argv)
        try:
            result = subprocess.Popen(argv, **kwargs)
            last_spawn_used_fallback = True
            logger.warning(
                "[spawn_utils] retry without CREATE_BREAKAWAY_FROM_JOB "
                "succeeded: %r", argv)
            return result
        except OSError:
            logger.warning(
                "[spawn_utils] Popen retry without CREATE_BREAKAWAY_FROM_JOB "
                "also failed: %r", argv)
            return None
