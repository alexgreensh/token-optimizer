"""Portable runtime bounds for short-lived hook processes.

This module deliberately uses only the Python standard library.  HookDeadline
provides the same hard wall-clock boundary on every platform; LeaseLock uses
portable exclusive-create semantics instead of platform-specific advisory
locking.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path


_DEADLINE_LOCAL = threading.local()


class HookDeadline:
    """Hard process deadline enforced by a daemon watchdog thread."""

    def __init__(self, seconds: float, message: bytes | None = None, on_timeout=None):
        self.seconds = max(0.0, float(seconds))
        self.end = time.monotonic() + self.seconds
        self.message = (
            b"[Token Optimizer] hook budget exceeded; skipping\n"
            if message is None
            else message
        )
        # Callbacks that run on the timeout path only, before the process is
        # torn down: the place to hand unfinished work to a detached child
        # (the dashboard rebuild does this). Each one is bounded; a callback
        # that hangs is abandoned, never allowed to delay the exit.
        self._on_timeout = [on_timeout] if on_timeout is not None else []
        self._cancelled = threading.Event()
        self._thread = threading.Thread(
            target=self._watch,
            name="token-optimizer-hook-deadline",
            daemon=True,
        )
        self._started = False
        self._previous = None

    def start(self):
        if not self._started:
            self._started = True
            self._previous = getattr(_DEADLINE_LOCAL, "current", None)
            _DEADLINE_LOCAL.current = self
            self._thread.start()
        return self

    def add_on_timeout(self, callback) -> None:
        """Register work to run if (and only if) the deadline fires."""
        if callback is not None:
            self._on_timeout.append(callback)

    def _run_on_timeout(self, budget: float = 1.0) -> None:
        """Run the timeout callbacks, each on its own bounded thread."""
        end = time.monotonic() + max(0.0, budget)
        for callback in list(self._on_timeout):
            runner = threading.Thread(
                target=self._call_quietly,
                args=(callback,),
                name="token-optimizer-hook-deadline-callback",
                daemon=True,
            )
            runner.start()
            runner.join(max(0.0, end - time.monotonic()))

    @staticmethod
    def _call_quietly(callback) -> None:
        try:
            callback()
        except Exception:
            pass

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def expires_wall(self) -> float:
        return time.time() + self.remaining()

    def _emit_diagnostic(self):
        try:
            os.write(2, self.message)
        except OSError:
            pass

    def _watch(self):
        if not self._cancelled.wait(self.remaining()):
            self._run_on_timeout()
            # The diagnostic belongs on the timeout path only - emitting it at
            # arm time made every NORMAL completion print "budget exceeded".
            # It runs in a bounded side thread so a full/undrained stderr pipe
            # can never delay termination: if the write blocks, the daemon
            # thread is abandoned and os._exit still fires. Losing the message
            # is acceptable; failing to exit is not.
            emitter = threading.Thread(
                target=self._emit_diagnostic,
                name="token-optimizer-hook-deadline-msg",
                daemon=True,
            )
            emitter.start()
            emitter.join(0.05)
            os._exit(0)

    def cancel(self):
        if not self._started:
            return
        self._cancelled.set()
        self._thread.join(timeout=0.1)
        if getattr(_DEADLINE_LOCAL, "current", None) is self:
            _DEADLINE_LOCAL.current = self._previous

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.cancel()


def current_deadline() -> HookDeadline | None:
    """Return the active deadline for the calling thread, if any."""

    deadline = getattr(_DEADLINE_LOCAL, "current", None)
    if deadline is not None and deadline.remaining() > 0:
        return deadline
    return None


# --- Per-event hook entry budgets --------------------------------------------
# The host timeout is a kill, not a usable budget. These self-imposed limits
# let run.py fail open before a host kills a process mid-write. The Codex
# values were measured in an isolated HOME: 0.10-0.23s over five runs per
# entry point, then given a generous margin under each host ceiling.

BUDGET_PRETOOL = 0.75
BUDGET_POSTTOOL = 0.75
BUDGET_PRETOOL_MEASURE = 2.0
BUDGET_POSTTOOL_MEASURE = 2.0
BUDGET_STOP = 2.5
BUDGET_CODEX_SESSION_START = 4.5
BUDGET_CODEX_USER_PROMPT = 4.5
BUDGET_CODEX_SUBAGENT = 2.5
BUDGET_POSTTOOL_RUNNER = 4.5


def _entry_budget_rules():
    """Return precise (module, argv predicate, seconds, label) rules."""

    def _flags_exactly(*expected):
        want = frozenset(expected)
        return lambda argv: frozenset(argv) == want

    def _has(*needed):
        return lambda argv: all(n in argv for n in needed)

    def _subcommand(name, *needed):
        return lambda argv: bool(argv) and argv[0] == name and all(
            n in argv for n in needed
        )

    def _trigger(name, *triggers):
        allowed = frozenset(triggers)

        def _match(argv):
            if not argv or argv[0] != name:
                return False
            for i, arg in enumerate(argv):
                if arg == "--trigger" and i + 1 < len(argv):
                    return argv[i + 1] in allowed
            return False

        return _match

    return (
        ("read_cache", _flags_exactly("--quiet"), BUDGET_PRETOOL, "PreToolUse:Read"),
        ("bash_hook", _flags_exactly("--quiet"), BUDGET_PRETOOL, "PreToolUse:Bash"),
        ("refetch_guard", _flags_exactly("--quiet"), BUDGET_PRETOOL, "PreToolUse:mcp"),
        ("measure", _subcommand("checkpoint-trigger"), BUDGET_PRETOOL_MEASURE, "PreToolUse:Agent"),
        ("bash_compress_hook", _has("--quiet"), BUDGET_POSTTOOL, "PostToolUse:Bash"),
        ("archive_result", _has("--quiet"), BUDGET_POSTTOOL, "PostToolUse:archive"),
        ("context_intel", _has("--quiet"), BUDGET_POSTTOOL, "PostToolUse:intel"),
        ("read_cache", _has("--invalidate"), BUDGET_POSTTOOL, "PostToolUse:invalidate"),
        ("measure", _subcommand("quality-cache", "--throttle-only"), BUDGET_POSTTOOL_MEASURE, "PostToolUse:quality-cache"),
        ("posttooluse_runner", _flags_exactly(), BUDGET_POSTTOOL_RUNNER, "PostToolUse:runner"),
        ("measure", _trigger("compact-capture", "stop", "stop-failure"), BUDGET_STOP, "Stop:compact-capture"),
        ("measure", _trigger("session-end-flush", "stop"), BUDGET_STOP, "Stop:session-end-flush"),
        ("measure", _subcommand("keepwarm-arm"), BUDGET_STOP, "Stop:keepwarm-arm"),
        ("codex_hook_bridge", _flags_exactly("session-start"), BUDGET_CODEX_SESSION_START, "Codex:SessionStart"),
        ("codex_hook_bridge", _flags_exactly("user-prompt-submit"), BUDGET_CODEX_USER_PROMPT, "Codex:UserPromptSubmit"),
        ("codex_hook_bridge", _flags_exactly("subagent-start"), BUDGET_CODEX_SUBAGENT, "Codex:SubagentStart"),
        ("codex_hook_bridge", _flags_exactly("subagent-stop"), BUDGET_CODEX_SUBAGENT, "Codex:SubagentStop"),
        ("measure", _subcommand("dynamic-compact-instructions", "--quiet"), BUDGET_POSTTOOL_MEASURE, "PreCompact:dynamic-compact-instructions"),
        ("measure", _trigger("compact-capture", "auto"), BUDGET_STOP, "PreCompact:compact-capture"),
        ("measure", _flags_exactly("quality-cache", "--force", "--quiet"), BUDGET_POSTTOOL_MEASURE, "PostCompact:quality-cache"),
        # PreCompact and CwdChanged intentionally share this exact argv shape;
        # the resolver has no event parameter, so the label stays event-neutral.
        ("read_cache", _flags_exactly("--clear", "--quiet"), BUDGET_POSTTOOL_MEASURE, "cache-clear"),
    )


def resolve_entry_budget(module_name, script_args):
    """Return (seconds, label), or (None, None) for an unbudgeted entry."""
    try:
        argv = list(script_args or ())
        for module, predicate, seconds, label in _entry_budget_rules():
            if module != module_name or not predicate(argv):
                continue
            override = os.environ.get("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", "").strip()
            if override:
                try:
                    milliseconds = int(override)
                except ValueError:
                    return seconds, label
                if milliseconds <= 0:
                    return None, None
                return milliseconds / 1000.0, label
            return seconds, label
    except Exception:
        pass
    return None, None


def arm_entry_budget(module_name, script_args):
    """Start a silent deadline for a mapped entry, if one exists."""
    seconds, _label = resolve_entry_budget(module_name, script_args)
    if not seconds:
        return None
    try:
        return HookDeadline(seconds, message=b"").start()
    except Exception:
        return None


class LeaseLock:
    """Portable, bounded, fail-open lock backed by an exclusive-create file."""

    def __init__(
        self,
        path,
        *,
        deadline: HookDeadline | None = None,
        acquire_timeout: float = 0.075,
        lease_seconds: float = 10.0,
        reclaim_grace: float = 0.25,
        cohort_throttle: bool = True,
    ):
        # Preserve an already-materialized concrete path. This also lets tests
        # simulate Windows by changing os.name after pathlib created PosixPath
        # objects, without asking pathlib to instantiate an unsupported flavor.
        self.path = path if hasattr(path, "read_text") else Path(path)
        self.deadline = deadline
        self.acquire_timeout = max(0.0, float(acquire_timeout))
        self.lease_seconds = max(0.1, float(lease_seconds))
        self.reclaim_grace = max(0.0, float(reclaim_grace))
        self.cohort_throttle = bool(cohort_throttle)
        self.nonce = secrets.token_hex(16)
        self._owner_path = self.path.with_name(
            f".{self.path.name}.candidate-{self.nonce}"
        )
        self.acquired = False

    def _metadata(self):
        now = time.time()
        expires = (
            self.deadline.expires_wall()
            if self.deadline is not None
            else now + self.lease_seconds
        )
        return {
            "pid": os.getpid(),
            "nonce": self.nonce,
            "released": 0,
            # Waiting acquisitions represent a thundering-herd cohort. Keep
            # that generation reserved through its lease so a delayed cohort
            # member cannot run the same mutation after an early release.
            # Nonblocking locks retain immediate sequential reuse. Writers that
            # persist DISTINCT mutations (e.g. archive writers keyed by
            # tool_use_id) opt out via cohort_throttle=False so a trailing
            # distinct-PID writer can immediately reclaim a released lease
            # instead of being suppressed for the full lease window.
            "reuse_wall": expires if (self.acquire_timeout > 0 and self.cohort_throttle) else now,
            "created_wall": now,
            "expires_wall": expires,
        }

    def _try_create(self):
        metadata = json.dumps(
            self._metadata(), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        candidate = self._owner_path
        fd = None
        published = False
        try:
            fd = os.open(
                str(candidate),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            offset = 0
            while offset < len(metadata):
                written = os.write(fd, metadata[offset:])
                if written <= 0:
                    raise OSError("short lease metadata write")
                offset += written
            try:
                os.fsync(fd)
            except OSError:
                pass
            os.close(fd)
            fd = None
            # Publishing a hard link is an atomic no-replace operation on the
            # supported POSIX and Windows filesystems. The final pathname can
            # therefore never expose partially-written JSON.
            os.link(str(candidate), str(self.path))
            published = True
        except FileExistsError:
            return False
        except OSError:
            return None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not published:
                try:
                    candidate.unlink()
                except OSError:
                    pass
        self.acquired = True
        return True

    def _read_owner(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
            owner = json.loads(raw)
            nonce = owner.get("nonce")
            created = float(owner.get("created_wall"))
            expires = float(owner.get("expires_wall"))
            reuse = float(owner.get("reuse_wall", created))
            released = owner.get("released", 0)
            if (
                not isinstance(nonce, str)
                or not nonce
                or released not in (0, 1, False, True)
                or created <= 0
                or expires < created
                or reuse < created
                or reuse > expires
            ):
                return None
            return owner, created, expires
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _reclaim_path(self, generation, validate_claim):
        """Unlink one exact lease generation after exclusively claiming it.

        The hard link pins the observed inode while it is validated, turning
        unlink into a conditional operation without platform-specific APIs.
        A paused stale reclaimer may link a successor, but validation prevents
        it from unlinking that successor. A process killed mid-reclaim leaves
        its claim behind, making future contenders fail open.
        """

        digest = hashlib.sha256(generation.encode("utf-8")).hexdigest()[:32]
        claim = self.path.with_name(f".{self.path.name}.reclaim-{digest}")
        try:
            os.link(str(self.path), str(claim))
        except (FileExistsError, OSError):
            return False
        try:
            current = os.stat(self.path)
            claimed = os.stat(claim)
            if not os.path.samestat(current, claimed):
                return False
            if not validate_claim(claim):
                return False
            os.unlink(self.path)
            return True
        except OSError:
            return False
        finally:
            try:
                claim.unlink()
            except OSError:
                pass

    def _reclaim_if_expired(self):
        parsed = self._read_owner()
        if parsed is None:
            # A creator killed before older releases finished their direct
            # write can leave empty/truncated JSON. Reclaim only a stable,
            # grace-aged pathname; a fresh malformed file may still be in the
            # middle of publication by an old process.
            try:
                before = os.lstat(self.path)
                malformed_grace = max(self.reclaim_grace, 0.25)
                if time.time() <= before.st_mtime + malformed_grace:
                    return False
                raw = self.path.read_bytes()
                after = os.lstat(self.path)
                if (
                    not os.path.samestat(before, after)
                    or len(raw) != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                ):
                    return False
            except OSError:
                return False
            generation = "malformed:{0}:{1}:{2}:{3}".format(
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )

            def stable_malformed(claim):
                try:
                    claimed = os.lstat(claim)
                    return (
                        claimed.st_size == before.st_size
                        and claimed.st_mtime_ns == before.st_mtime_ns
                        and claim.read_bytes() == raw
                    )
                except OSError:
                    return False

            return self._reclaim_path(generation, stable_malformed)
        owner, created, expires = parsed
        now = time.time()
        # Future creation times indicate clock rollback or malformed metadata.
        # Fail open without stealing a lock whose lease cannot be assessed.
        if created > now + self.reclaim_grace:
            return False
        if owner.get("released"):
            reuse = float(owner.get("reuse_wall", created))
            if owner.get("pid") != os.getpid() and now <= reuse:
                return False
        elif now <= expires + self.reclaim_grace:
            return False

        def same_owner(claim):
            try:
                claimed = json.loads(claim.read_text(encoding="utf-8"))
                return claimed.get("nonce") == owner["nonce"]
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return False

        return self._reclaim_path(f"owner:{owner['nonce']}", same_owner)

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        wait_for = self.acquire_timeout
        if self.deadline is not None:
            wait_for = min(wait_for, self.deadline.remaining())
        stop = time.monotonic() + wait_for
        # Attempt FIRST, check the deadline after a failed attempt. A waiter
        # whose sleep overslept (loaded box, descheduling) must still take a
        # lease that was released while it slept; checking the deadline before
        # the attempt made acquire() give up on an observably free lock, which
        # flaked test_cleanup_period_and_statusline_writers_merge_stale_reads
        # ~1-in-5 (instrumented 2026-09-02: iters=2, acquire False at 0.0777s,
        # lease free since 0.001s). The bottom-of-loop check still bounds the
        # wait: the loop exits at `stop` after a failed attempt and never
        # sleeps past it.
        while True:
            created = self._try_create()
            if created is True:
                return True
            if created is None:
                return False
            if self._reclaim_if_expired():
                # Reclamation freed the pathname. Retry exclusive creation once
                # even when acquire_timeout is zero.
                created = self._try_create()
                if created is not False:
                    return created is True
            if time.monotonic() >= stop:
                return False
            remaining = stop - time.monotonic()
            time.sleep(min(remaining, random.uniform(0.004, 0.012)))

    def release(self):
        if not self.acquired:
            return
        try:
            raw = self._owner_path.read_bytes()
            owner = json.loads(raw)
            marker = b'"released":0'
            offset = raw.find(marker)
            if owner.get("nonce") == self.nonce and offset >= 0:
                fd = os.open(str(self._owner_path), os.O_WRONLY)
                try:
                    os.lseek(fd, offset + len(marker) - 1, os.SEEK_SET)
                    if os.write(fd, b"1") != 1:
                        raise OSError("short lease release write")
                    try:
                        os.fsync(fd)
                    except OSError:
                        pass
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        finally:
            try:
                self._owner_path.unlink()
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


@contextmanager
def lease_lock(path, **kwargs):
    """Yield whether the lease was acquired; callers skip mutation on False."""

    lock = LeaseLock(path, **kwargs)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


# --- Stale-lease sweeper -----------------------------------------------------
# LeaseLock's release() leaves the canonical ``.qlease`` tombstone on disk for
# the next contender to reclaim. Sessions are one-shot, so most tombstones get
# no future contender and leak forever (one+ per session per user, unbounded).
# This sweeper is the self-healing backstop: it removes ONLY lease artifacts
# whose expiry is demonstrably far in the past, so no live locker can ever be
# touched. It is fail-open by construction (whole body is try/except, never
# raises) and must NEVER run on a hot per-tool-call path unthrottled.

# A ``.qlease`` is long-expired once its parsed ``expires_wall`` is at least an
# hour behind ``now``. The hour of slack absorbs clock skew and the
# deadline-vs-lease rounding without ever reaching back into a live lease
# (leases themselves are ~10s).
_SWEEP_LEASE_EXPIRED_GRACE = 3600
# Candidate/reclaim hard links and legacy ``.qlock`` files carry no parseable
# expiry, so they are swept purely by mtime. 24h is far beyond any live lease
# or reclaim-grace window, so a file this old is unambiguously orphaned.
_SWEEP_MTIME_AGE = 86400


def _sweep_stale_leases(directory, now=None, max_files=2000):
    """Remove long-expired lease artifacts from ``directory``.

    Scans ``directory`` (the quality-cache dir) and unlinks ONLY files whose
    lease is demonstrably long-expired, so no live locker can be touched:

      * ``*.qlease``: parse the JSON metadata and remove if ``expires_wall`` <
        ``now - _SWEEP_LEASE_EXPIRED_GRACE`` (3600s). If the file is
        unparseable/corrupt/partial, remove only when its mtime is older than
        ``now - _SWEEP_MTIME_AGE`` (86400s) so a file mid-publication by an old
        process is never torn down.
      * ``.*.candidate-*`` and ``.*.reclaim-*``: remove when mtime is older
        than ``now - _SWEEP_MTIME_AGE``.
      * legacy ``*.qlock`` (pre-5.11 artifacts; the current code never writes
        them): remove when mtime is older than ``now - _SWEEP_MTIME_AGE``.

    The scan is capped at ``max_files`` entries per run so a pathological cache
    dir cannot blow the hook budget. The whole function is wrapped in
    try/except: it NEVER raises into a hook and NEVER blocks a session. Returns
    the number of files removed (best-effort, 0 on any error).
    """
    removed = 0
    try:
        if directory is None:
            return 0
        dir_path = Path(directory) if not hasattr(directory, "iterdir") else directory
        if not dir_path.is_dir():
            return 0
        now = time.time() if now is None else float(now)
        lease_cutoff = now - _SWEEP_LEASE_EXPIRED_GRACE
        mtime_cutoff = now - _SWEEP_MTIME_AGE
        seen = 0
        # ``scandir`` is one stat-free readdir; per-entry mtime comes from the
        # DirEntry where available, falling back to os.stat only when needed.
        try:
            entries = list(os.scandir(dir_path))
        except OSError:
            return 0
        for entry in entries:
            if seen >= max_files:
                break
            seen += 1
            name = entry.name
            if not name:
                continue
            try:
                # Skip non-files (subdirectories) cheaply via the scandir cache.
                if entry.is_dir(follow_symlinks=False):
                    continue
                should_remove = False
                if name.endswith(".qlease"):
                    should_remove = _sweep_qlease_is_stale(
                        dir_path / name, entry, lease_cutoff, mtime_cutoff
                    )
                elif name.startswith(".") and (
                    ".candidate-" in name or ".reclaim-" in name
                ):
                    should_remove = _sweep_mtime_is_stale(entry, mtime_cutoff)
                elif name.endswith(".qlock"):
                    should_remove = _sweep_mtime_is_stale(entry, mtime_cutoff)
                if should_remove:
                    try:
                        os.unlink(entry.path)
                        removed += 1
                    except OSError:
                        pass
            except OSError:
                continue
    except Exception:
        # Fail-open: never propagate. A sweeper error must not break a hook.
        return removed
    return removed


def _sweep_qlease_is_stale(path, entry, lease_cutoff, mtime_cutoff):
    """True if a ``.qlease`` is long-expired (parseable) or long-dead (corrupt)."""
    try:
        raw = path.read_bytes()
        owner = json.loads(raw)
        expires = owner.get("expires_wall")
        if isinstance(expires, (int, float)) and expires > 0:
            return float(expires) < lease_cutoff
        # Valid JSON but no usable expiry -> treat as corrupt (mtime gate).
        return _sweep_mtime_is_stale(entry, mtime_cutoff)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Unparseable / partial / truncated JSON. A file mid-publication by an
        # old process could be momentarily malformed, so gate on a 24h mtime
        # age instead of removing a fresh malformed file immediately.
        return _sweep_mtime_is_stale(entry, mtime_cutoff)


def _sweep_mtime_is_stale(entry, mtime_cutoff):
    """True if the entry's mtime is older than ``mtime_cutoff``."""
    try:
        return entry.stat(follow_symlinks=False).st_mtime < mtime_cutoff
    except OSError:
        return False
