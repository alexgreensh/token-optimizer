#!/usr/bin/env python3
"""Token Optimizer — Cursor hook bridge.

Thin, fast, fail-safe entry point invoked by Cursor's hooks.json (merged into
``~/.cursor/hooks.json``). One bridge handles every Cursor event TO wires:

    cursor_hook_bridge.py sessionStart
    cursor_hook_bridge.py preToolUse
    cursor_hook_bridge.py postToolUse
    cursor_hook_bridge.py preCompact
    cursor_hook_bridge.py stop
    cursor_hook_bridge.py sessionEnd

Contract notes (verified against cursor.com/docs/hooks and the installed
Cursor CLI bundle as of 2026-08-31):

- Payloads arrive on stdin as ONE snake_case JSON object. Common fields:
  ``conversation_id``, ``generation_id``, ``model``, ``model_id``,
  ``hook_event_name``, ``cursor_version``, ``workspace_roots``,
  ``transcript_path``. ``tool_input`` may be an object or a JSON-encoded
  string (either tolerated); anything undecodable is a silent no-op.
- stdin → stdout JSON, exit 0 to accept. Exit 2 would deny (never emitted:
  TO never blocks). Non-0 non-2 fails open in Cursor, but we always exit 0.
- No capability matrix. Cursor hooks are observed, not version-gated: every
  handler appends one line to ``observed-events.jsonl`` so ``cursor-doctor``
  can say exactly which events have fired on this install (KTD7/R13).
- Output is Cursor's top-level contract (NOT the Copilot
  ``hookSpecificOutput`` envelope): ``preToolUse`` → ``{"permission":
  "allow", "updated_input": {...}}``; ``sessionStart`` / ``postToolUse`` →
  ``{"additional_context": "..."}``; the rest emit nothing.

Security posture mirrors bash_hook.py: the ``Shell`` rewrite inherits its
whitelist + dangerous-char exclusions and fails CLOSED (emit nothing) on any
uncertainty. This bridge never imports Cursor internals and never writes
outside ``<cursor_home>/token-optimizer/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
_observed_warned = False

# Windows console-flash guard. Cursor's hook runners launch hooks
# directly, so any child we spawn would otherwise allocate a console window on
# Windows. getattr -> 0 on POSIX (creationflags=0 is an accepted no-op).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

# The bridge must run even if siblings are missing (partial install): each
# import below is optional, with the dependent feature degraded/disabled.
try:
    from runtime_env import cursor_home
except ImportError:  # pragma: no cover - broken install
    cursor_home = None  # type: ignore[assignment]

try:
    from spawn_utils import spawn_detached
except ImportError:  # pragma: no cover - broken install
    logger.warning("[cursor] spawn_utils import failed; using degraded fallback")

    def spawn_detached(argv, **popen_kwargs):  # type: ignore[no-redef]
        # Degraded broken-install path: does NOT detach (see
        # spawn_utils.spawn_detached for the real OS-flag logic + breakaway
        # retry). It DOES OR-in CREATE_NO_WINDOW so a broken install
        # degrades to "child dies with the parent" rather than "console window
        # flashes on every stop/sessionEnd hook".
        import subprocess as _sp

        kwargs = dict(popen_kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
        try:
            return _sp.Popen(argv, **kwargs)
        except OSError:
            return None

try:
    import bash_hook as _bash_hook
except ImportError:  # pragma: no cover
    _bash_hook = None  # type: ignore[assignment]

try:
    from codex_io import atomic_write_json as _atomic_write_json_impl
except ImportError:  # pragma: no cover - broken install
    _atomic_write_json_impl = None  # type: ignore[assignment]

try:
    from hook_runtime import lease_lock
except ImportError:  # pragma: no cover - broken install
    lease_lock = None  # type: ignore[assignment]

_MAX_STDIN_BYTES = 4 * 1024 * 1024  # refuse absurd payloads (amplification)
_RESTORE_MAX_BYTES = 16_384        # restore-context files above this are ignored
_ACTIVE_WORKSPACE_SECS = 600        # skip restore when a sibling tally is live
_STOP_ROLLUP_SECS = 120             # R12: at most one stop rollup per machine per 120s
_STALE_LOCK_SECS = 7 * 24 * 3600    # sessionStart lock sweep threshold
_NUDGE_TOOL_CALLS = (30, 80)        # context-growth nudge thresholds (turn proxy)

# Snapshot at import: the installed payload is static for the process lifetime.
_COMPRESS_PATH = _SCRIPT_DIR / "bash_compress.py"
_COMPRESS_AVAILABLE = _COMPRESS_PATH.exists()

_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_session_id(sid):
    """Strip everything but [A-Za-z0-9_-] so a payload id can never traverse.

    Mirrors measure.sanitize_session_id / copilot_hook_bridge: a conversation_id
    like "../../../etc/passwd" would otherwise let _atomic_write_json / the lock
    path escape the data dir.
    """
    if not sid:
        return "unknown"
    cleaned = _SESSION_ID_RE.sub("", sid)[:64]
    return cleaned if len(cleaned) >= 6 else "unknown"


def _read_stdin_payload():
    """Read and decode the Cursor hook payload from stdin. None on any failure."""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        return None
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _parse_tool_input(raw):
    """tool_input as a dict, or as a JSON-encoded string -> dict; {} otherwise."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def decode_payload(payload):
    """Normalize a Cursor hook payload into a flat, safe dict.

    ``conversation_id`` wins over ``session_id`` (sessionStart carries only
    session_id; the tool events carry conversation_id). ``workspace_roots`` is
    the list Cursor exports; ``cwd`` is capped at 1024 chars because it is
    rewritten into the tally on every tool call and into restore-context.
    Missing fields default to empty values so callers never KeyError.
    """
    out = {
        "tool_name": "",
        "tool_args": {},
        "conversation_id": "",
        "hook_event_name": "",
        "cwd": "",
        "cursor_version": "",
        "workspace_roots": [],
        "transcript_path": None,
        "model": None,
        "timestamp": None,
        # preCompact numbers (0/0.0 when absent or the payload is not a dict)
        "context_tokens": 0,
        "context_window_size": 0,
        "context_usage_percent": 0.0,
        "trigger": "",
        # U4 correlation/output fields (CLI bundle 2026.11.05 evidence)
        "generation_id": "",
        "tool_use_id": "",
        "parent_tool_call_id": "",
        "tool_output": "",
        "duration": 0.0,
    }
    if not isinstance(payload, dict):
        return out

    tool_name = payload.get("tool_name", payload.get("toolName", ""))
    if isinstance(tool_name, str):
        out["tool_name"] = tool_name.strip()
    out["tool_args"] = _parse_tool_input(
        payload.get("tool_input", payload.get("toolInput", {}))
    )

    sid = payload.get("conversation_id", payload.get("session_id", ""))
    if isinstance(sid, str):
        out["conversation_id"] = _sanitize_session_id(sid)

    event = payload.get("hook_event_name", "")
    if isinstance(event, str):
        out["hook_event_name"] = event.strip()

    cwd = payload.get("cwd", "")
    if isinstance(cwd, str):
        out["cwd"] = cwd[:1024]

    version = payload.get("cursor_version", "")
    if isinstance(version, str):
        out["cursor_version"] = version[:64]

    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        out["workspace_roots"] = [
            str(r)[:1024] for r in roots if isinstance(r, str)
        ][:8]

    tp = payload.get("transcript_path")
    if isinstance(tp, str):
        out["transcript_path"] = tp[:1024]

    for key in ("generation_id", "tool_use_id", "parent_tool_call_id"):
        v = payload.get(key)
        if isinstance(v, str):
            out[key] = v[:128]
    to = payload.get("tool_output")
    if isinstance(to, str):
        out["tool_output"] = to[:262144]
    try:
        out["duration"] = float(payload.get("duration") or 0.0)
    except (TypeError, ValueError):
        out["duration"] = 0.0

    model = payload.get("model")
    if isinstance(model, str):
        out["model"] = model[:128]

    out["timestamp"] = payload.get("timestamp")

    for key in ("context_tokens", "context_window_size"):
        try:
            out[key] = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    try:
        out["context_usage_percent"] = float(payload.get("context_usage_percent") or 0.0)
    except (TypeError, ValueError):
        out["context_usage_percent"] = 0.0
    trigger = payload.get("trigger")
    if isinstance(trigger, str):
        out["trigger"] = trigger
    return out


# ---------------------------------------------------------------------------
# Data dir + atomic write
# ---------------------------------------------------------------------------


def _to_dir():
    """Token Optimizer's data dir under the Cursor home. None if unavailable."""
    if cursor_home is None:
        return None
    try:
        d = cursor_home() / "token-optimizer"
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)  # session tallies/ledger hold file contents (G1)
        except OSError:
            pass
        return d
    except OSError:
        return None


def _sessions_dir():
    """<cursor_home>/token-optimizer/sessions/ — the durable tally store."""
    to_dir = _to_dir()
    if to_dir is None:
        return None
    try:
        d = to_dir / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _atomic_write_json(path, obj):
    """Atomic JSON write via codex_io; concurrent writers never corrupt."""
    if _atomic_write_json_impl is None:
        return False
    try:
        _atomic_write_json_impl(path, obj)
        return True
    except OSError:
        return False


def _record_observed(event, **extra):
    """Append one observed-events line (KTD7/R13). Single-line JSONL append.

    Each event writes one line; the doctor tolerates a torn final line and sums
    the rest. cursor_version comes from the hook env first (Cursor exports it),
    then the payload. Never raises — the ledger is observational, not gating.
    """
    to_dir = _to_dir()
    if to_dir is None:
        return
    entry = {
        "event": event,
        "cursor_version": str(os.environ.get("CURSOR_VERSION") or "").strip()[:64],
        "ts": time.time(),
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    try:
        ledger = to_dir / "observed-events.jsonl"
        # O_NOFOLLOW where the platform has it: a symlinked ledger must never
        # redirect the append into a victim file (G1). 0o600 on create.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(ledger), flags, 0o600)
        try:
            os.fchmod(fd, 0o600)  # tighten a pre-existing 0o644 ledger
        except OSError:
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        # Log once per process: on a persistently unwritable data dir this fires
        # per hook event, and a full traceback per tool call is log-spam.
        global _observed_warned
        if not _observed_warned:
            _observed_warned = True
            logger.warning("[cursor_hook_bridge] observed-events append failed; "
                           "further append failures are silent", exc_info=True)


# ---------------------------------------------------------------------------
# Tally read-modify-write (durable session record)
# ---------------------------------------------------------------------------


def _tally_path(fields):
    sdir = _sessions_dir()
    if sdir is None:
        return None
    sid = fields.get("conversation_id") or "unknown"
    return sdir / f"{sid}.json"


def _load_tally(path):
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


@contextmanager
def _session_lock(to_dir, sid):
    """Bounded portable lease around a tally read-modify-write.

    Fail-open on contention: a contender skips after 75ms rather than blocking
    a Cursor hot path. The tally is a soft counter, so a skipped update is
    tolerable. A missing ``hook_runtime`` (broken install) proceeds unlocked.

    Every Cursor hook event is its own process, so the default cohort
    throttle (reuse_wall = lease expiry) would suppress every distinct-PID
    acquirer for ~10s after a clean release and silently drop tally updates
    (G2 P1, executed repro: 2 of 3 sequential events lost). Distinct
    mutation writers opt out via ``cohort_throttle=False``;
    ``reclaim_released=True`` lets a cleanly FINISHED event's successor in
    without ever stealing a LIVE lease (release marker is written only on
    clean finish).
    """
    if to_dir is None or sid in (None, "", "unknown"):
        yield False
        return
    lock_dir = to_dir / ".locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield True  # fail open: tally update proceeds, just unlocked
        return
    if lease_lock is None:
        yield True
        return
    lock_path = lock_dir / f"{sid}.lock"
    with lease_lock(lock_path, acquire_timeout=0.075,
                    cohort_throttle=False, reclaim_released=True) as acquired:
        yield acquired


def _update_tally(fields, *, terminal=False, end_reason=None,
                  increment_turns=False, count_tool=False, compaction=None,
                  bump_nudge=False, hook_tokens=None, failure=False,
                  subagent=False, compact_checkpoint=None, restore_pending=None):
    """Read-modify-write the session tally; returns ``(tally, nudge_emitted)``.

    Non-terminal activity reopens an idle/sessionEnd-finalised tally (final set
    back to False) so a long-lived IDE composer resumed after lunch is never
    frozen at the old ``idle`` verdict. ``terminal=True`` is sessionEnd only.
    """
    to_dir = _to_dir()
    path = _tally_path(fields) if to_dir is not None else None
    if path is None:
        return None, False
    sid = fields.get("conversation_id") or "unknown"
    with _session_lock(to_dir, sid) as acquired:
        if not acquired:
            return None, False
        tally = _load_tally(path)
        now = time.time()
        if tally is None:
            tally = {"conversation_id": sid, "first_ts": now}

        if terminal:
            tally["final"] = True
            tally["end_reason"] = end_reason or "sessionEnd"
        else:
            if tally.get("final") or tally.get("end_reason") in ("idle", "sessionEnd"):
                tally["final"] = False
                tally["end_reason"] = ""

        tally["updated_at"] = now
        tally.setdefault("turns", 0)
        tally.setdefault("tool_calls", 0)
        tally.setdefault("models", {})
        tally.setdefault("tool_names", {})
        tally.setdefault("compactions", [])

        if fields.get("cwd"):
            tally["cwd"] = fields["cwd"]
        if fields.get("workspace_roots"):
            tally["workspace_roots"] = fields["workspace_roots"]
        if fields.get("transcript_path") is not None:
            tally["transcript_path"] = fields["transcript_path"]
        if fields.get("cursor_version"):
            tally["cursor_version"] = fields["cursor_version"]

        model = fields.get("model")
        if isinstance(model, str) and model:
            models = tally.setdefault("models", {})
            models[model] = int(models.get(model, 0) or 0) + 1

        if count_tool:
            tally["tool_calls"] = int(tally.get("tool_calls", 0) or 0) + 1
            name = fields.get("tool_name") or "tool"
            names = tally.setdefault("tool_names", {})
            names[name] = int(names.get(name, 0) or 0) + 1

        if increment_turns:
            tally["turns"] = int(tally.get("turns", 0) or 0) + 1

        if compaction is not None:
            comps = tally.setdefault("compactions", [])
            comps.append(compaction)
            tally["compactions"] = comps[-200:]

        if compact_checkpoint is not None:
            tally["compact_checkpoint"] = compact_checkpoint
        if restore_pending is not None:
            tally["restore_pending"] = bool(restore_pending)

        if hook_tokens is not None:
            hm = tally.setdefault("hook_measured_tokens", {
                "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                "responses": 0, "source": "cursor_hook_measured",
            })
            for k in ("input", "output", "cache_read", "cache_write"):
                hm[k] = int(hm.get(k, 0) or 0) + int(hook_tokens.get(k) or 0)
            hm["responses"] = int(hm.get("responses", 0) or 0) + 1
            hm["source"] = "cursor_hook_measured"

        if failure:
            fails = tally.setdefault("failures", {})
            fname = fields.get("tool_name") or "tool"
            fails[fname] = int(fails.get(fname, 0) or 0) + 1

        if subagent:
            tally["subagents"] = int(tally.get("subagents", 0) or 0) + 1

        # Context-growth nudge (R10), folded into THIS locked RMW: computing it
        # in the caller needed a second lock (and a gap a concurrent stop/
        # preCompact could exploit to clobber either the tool_calls increment
        # or the nudge_level). preCompact gives real numbers; when it never
        # fired the tool-call count is the honest available proxy.
        nudge_emitted = False
        if bump_nudge:
            new_level = _nudge_level(int(tally.get("tool_calls", 0) or 0))
            if new_level > int(tally.get("nudge_level", 0) or 0):
                tally["nudge_level"] = new_level
                nudge_emitted = True

        # The emit decision travels as the tuple's second element, never as a
        # key in the tally: no caller can accidentally persist it.
        _atomic_write_json(path, tally)
        return tally, nudge_emitted


def _claim_restore(fields):
    """Atomically claim a pending compact restore; checkpoint path or None.

    The restore_pending pop happens inside ONE locked RMW, so concurrent
    postToolUse processes (each its own PID) can never both observe True:
    only the claimer emits the restore (G2 P2). Fail-open: a skipped claim
    just defers the restore to the next event.
    """
    to_dir = _to_dir()
    path = _tally_path(fields) if to_dir is not None else None
    if path is None:
        return None
    sid = fields.get("conversation_id") or "unknown"
    with _session_lock(to_dir, sid) as acquired:
        if not acquired:
            return None
        tally = _load_tally(path)
        if not tally or not tally.get("restore_pending"):
            return None
        tally["restore_pending"] = False
        tally["updated_at"] = time.time()
        _atomic_write_json(path, tally)
        return tally.get("compact_checkpoint")


# ---------------------------------------------------------------------------
# Restore context (per workspace)
# ---------------------------------------------------------------------------


def _workspace_root(fields):
    roots = fields.get("workspace_roots") or []
    if roots and isinstance(roots[0], str) and roots[0]:
        return roots[0]
    return fields.get("cwd") or None


def _restore_for_workspace(fields):
    """Return the restore-context for THIS workspace, or None.

    Cursor IDE runs many concurrent chats across repos, so a single global
    restore file would seed every new chat with whichever conversation rolled
    up last. Files are keyed by ``sha1(workspace_root)`` and injected only for
    the matching root. Injection is skipped while a non-final sibling tally in
    that workspace has activity in the last 10 minutes.
    """
    root = _workspace_root(fields)
    to_dir = _to_dir()
    if to_dir is None or not root:
        return None

    sdir = _sessions_dir()
    now = time.time()
    own_id = fields.get("conversation_id") or ""
    if sdir is not None:
        try:
            for p in sdir.glob("*.json"):
                t = _load_tally(p)
                if not t or t.get("final"):
                    continue
                # Exclude the session being started: its own freshly-written
                # tally is active by definition and must not suppress restore.
                if own_id and str(t.get("conversation_id") or "") == own_id:
                    continue
                troot = None
                wroots = t.get("workspace_roots")
                if isinstance(wroots, list) and wroots and isinstance(wroots[0], str):
                    troot = wroots[0]
                else:
                    troot = t.get("cwd")
                if str(troot) == str(root):
                    try:
                        if now - float(t.get("updated_at") or 0) < _ACTIVE_WORKSPACE_SECS:
                            return None
                    except (TypeError, ValueError):
                        pass
        except OSError:
            pass

    digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
    path = to_dir / "restore-context" / f"{digest}.md"
    try:
        if path.exists() and path.stat().st_size <= _RESTORE_MAX_BYTES:
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Compression + nudge helpers
# ---------------------------------------------------------------------------


def _compression_enabled():
    # Native Windows stays record-only: the rewrite quotes with shlex
    # (POSIX single quotes), which cmd.exe does not honor, and which host
    # shell Cursor's Windows Shell tool actually uses is unverified - an
    # unsafe rewrite is worse than no rewrite (G4). POSIX shells keep it.
    if os.name == "nt":
        return False
    return (
        _bash_hook is not None
        and _COMPRESS_AVAILABLE
        and os.environ.get("TOKEN_OPTIMIZER_BASH_COMPRESS", "").strip() != "0"
    )


def _nudge_level(tool_calls):
    level = 0
    for threshold in _NUDGE_TOOL_CALLS:
        if tool_calls >= threshold:
            level += 1
    return level


_NUDGE_TEXT = (
    "[Token Optimizer] Context is growing large for this session. Prefer "
    "targeted reads over full files, avoid re-reading unchanged files, and "
    "summarize before continuing long explorations."
)


# ---------------------------------------------------------------------------
# Spawns (fire-and-forget; detach + CREATE_NO_WINDOW)
# ---------------------------------------------------------------------------


def _measure_env(interactive=False):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_RUNTIME"] = "cursor"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if interactive:
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
    return env


# The installer does NOT copy measure.py into the plugin dir (version-drift
# risk); it writes a one-line "measure-path" locator next to this bridge naming
# the canonical measure.py in the checkout. Resolution order: a sibling
# measure.py (dev/test checkout), then the locator. Mirrors
# hermes_hook_bridge._locate_measure_py.
_MEASURE_LOCATOR = _SCRIPT_DIR / "measure-path"


def _locate_measure_py():
    """Return the path to measure.py, or None if not found (rollups paused).

    The locator is installer-written, but the plugin dir is user-writable: a
    crafted locator pointing anywhere would have this bridge spawn its target
    as code. Only a regular, non-symlinked file named measure.py is accepted.
    """
    sibling = _SCRIPT_DIR / "measure.py"
    if sibling.is_file() and not sibling.is_symlink():
        return sibling
    try:
        if _MEASURE_LOCATOR.is_file() and not _MEASURE_LOCATOR.is_symlink():
            located = Path(_MEASURE_LOCATOR.read_text(encoding="utf-8").strip())
            if (located.is_file() and not located.is_symlink()
                    and located.name == "measure.py"):
                return located
    except (OSError, ValueError):
        pass
    return None


def _spawn_rollup():
    # cursor-doctor --probe replays the installed command to prove it fires;
    # it must not trigger a real detached rollup/dashboard as a side effect.
    if os.environ.get("TOKEN_OPTIMIZER_PROBE") == "1":
        return
    measure = _locate_measure_py()
    if measure is None:
        return
    try:
        spawn_detached(
            [sys.executable, str(measure), "cursor-rollup", "--quiet"],
            env=_measure_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("[cursor_hook_bridge] detached rollup spawn failed", exc_info=True)


def _spawn_dashboard():
    # Same probe guard as _spawn_rollup(): replay must not spawn detached work.
    if os.environ.get("TOKEN_OPTIMIZER_PROBE") == "1":
        return
    measure = _locate_measure_py()
    if measure is None:
        return
    try:
        spawn_detached(
            [sys.executable, str(measure), "dashboard", "--quiet"],
            env=_measure_env(interactive=True),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("[cursor_hook_bridge] detached dashboard spawn failed", exc_info=True)


def _stop_rollup_due():
    """True once per 120s per machine (R12). Best-effort: reads then writes.

    The read-check-write runs under a lease lock: two concurrent stop hooks
    (multi-window Cursor) would otherwise both read a stale ``last`` and both
    spawn rollup+dashboard. On lock contention we skip the spawn (the winner is
    rolling up); without hook_runtime we degrade to the unlocked path.
    """
    to_dir = _to_dir()
    if to_dir is None:
        return False
    state = to_dir / ".stop-rollup-last.json"
    now = time.time()
    if lease_lock is not None:
        lock_dir = to_dir / ".locks"
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            lock_dir = None
        if lock_dir is not None:
            with lease_lock(lock_dir / ".stop-rollup.lock",
                            acquire_timeout=0.5, lease_seconds=30.0) as acquired:
                if not acquired:
                    return False
                return _stop_rollup_due_locked(state, now)
    return _stop_rollup_due_locked(state, now)


def _stop_rollup_due_locked(state, now):
    last = None
    try:
        if state.exists():
            data = json.loads(state.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last = float(data.get("last", 0) or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        last = None
    if last is not None and now - last < _STOP_ROLLUP_SECS:
        return False
    # Fail closed: if the throttle-state write fails (disk full, read-only data
    # dir), do NOT spawn — otherwise every stop would spawn a rollup+dashboard
    # storm precisely under the degraded conditions the throttle is meant to
    # contain. _atomic_write_json returns True only on a successful write.
    return _atomic_write_json(state, {"last": now})


def _sweep_stale_locks():
    """Remove stale lock/candidate artifacts (7d) on sessionStart. Never fatal."""
    to_dir = _to_dir()
    if to_dir is None:
        return
    lock_dir = to_dir / ".locks"
    if not lock_dir.is_dir():
        return
    now = time.time()
    try:
        for pattern in ("*.lock", ".*.lock.candidate-*"):
            for p in lock_dir.glob(pattern):
                try:
                    if now - p.stat().st_mtime > _STALE_LOCK_SECS:
                        p.unlink()
                except OSError:
                    continue
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Event handlers (all fail-open: never raise into Cursor)
# ---------------------------------------------------------------------------


def _emit(obj):
    print(json.dumps(obj))


def handle_session_start(payload):
    """Sweep stale locks, re/init the tally, inject per-workspace continuity."""
    fields = decode_payload(payload)
    _record_observed("sessionStart",
                     conversation_id=fields["conversation_id"] or None)
    _update_tally(fields)
    _sweep_stale_locks()

    restore = _restore_for_workspace(fields)
    if restore:
        _emit({"additional_context": restore})


def handle_pre_tool_use(payload):
    """Shell bash-compression via updated_input, gated and fail-closed.

    Read interception (U2) runs through cursor_read_cache: shadow by
    default, deny + user_message only under an explicit per-install mode,
    fail-open on every uncertainty.
    """
    fields = decode_payload(payload)
    if fields["tool_name"] == "Read":
        try:
            import cursor_read_cache

            decision = cursor_read_cache.evaluate(fields)
        except Exception:
            decision = None
        if decision is not None:
            emit = decision.get("emit")
            if emit:
                _emit(emit)
            observed = decision.get("observed")
            if isinstance(observed, dict):
                _record_observed("readCache",
                                 conversation_id=fields["conversation_id"] or None,
                                 **observed)
        else:
            # Not an interceptable read (ineligible, first read elsewhere,
            # fail-open): keep the plain observed record.
            _record_observed("preToolUse",
                             conversation_id=fields["conversation_id"] or None,
                             tool_name=fields["tool_name"] or None,
                             rewrite=None)
        return
    rewrite = None
    if fields["tool_name"] == "Shell" and _compression_enabled():
        command = fields["tool_args"].get("command")
        if isinstance(command, str) and command:
            # The rewrite is emitted with permission "allow", so it is limited
            # to commands that cannot write, delete or run other programs.
            if _bash_hook._is_auto_approvable(command):
                try:
                    original_tokens = shlex.split(command)
                except ValueError:
                    original_tokens = None
                if original_tokens:
                    rewritten = (
                        shlex.quote(sys.executable)
                        + " " + shlex.quote(str(_COMPRESS_PATH))
                        + " " + " ".join(shlex.quote(t) for t in original_tokens)
                    )
                    # Echo every original field and replace only `command` so
                    # working_directory and any future fields survive the
                    # whole-tool_input replacement contract.
                    updated = dict(fields["tool_args"])
                    updated["command"] = rewritten
                    _emit({"permission": "allow", "updated_input": updated})
                    rewrite = "attempted"
    _record_observed("preToolUse",
                     conversation_id=fields["conversation_id"] or None,
                     tool_name=fields["tool_name"] or None,
                     rewrite=rewrite)


def handle_post_tool_use(payload):
    """Update the tally and, for Shell, record whether the rewrite was honoured.

    Cursor's postToolUse tool_input carries the command as actually executed:
    ``command`` containing ``bash_compress.py`` proves the rewrite was honoured;
    a whitelisted command that arrived bare proves it was ignored.
    """
    fields = decode_payload(payload)
    rewrite = None
    if fields["tool_name"] == "Shell" and _compression_enabled():
        command = fields["tool_args"].get("command")
        if isinstance(command, str) and command:
            if not _bash_hook._has_dangerous_chars(command) and _bash_hook._is_whitelisted(command):
                rewrite = "honoured" if "bash_compress.py" in command else "ignored"
    read_path = None
    if fields["tool_name"] == "Read":
        rp = fields["tool_args"].get("file_path")
        if isinstance(rp, str) and rp:
            read_path = Path(rp).name
    _record_observed("postToolUse",
                     conversation_id=fields["conversation_id"] or None,
                     tool_name=fields["tool_name"] or None,
                     path=read_path,
                     rewrite=rewrite)

    tally, nudge_emitted = _update_tally(fields, count_tool=True, bump_nudge=True)
    if tally is None:
        return
    nudges = [_NUDGE_TEXT] if nudge_emitted else []
    # U3: post-compaction restore - exactly one bounded inject, then clear.
    # Consecutive compactions without a tool call coalesce to this one.
    # The claim pops restore_pending inside ONE locked RMW: with distinct
    # hook processes no longer suppressed, a check-then-clear across two
    # locks would let concurrent postToolUse events both emit (G2 P2).
    cp = _claim_restore(fields)
    if isinstance(cp, str) and cp:
        restore_text = _read_checkpoint_bounded(cp)
        if restore_text:
            nudges.append(restore_text)
            _record_observed("compactRestore",
                             conversation_id=fields["conversation_id"] or None,
                             emitted=True)
    # U5 thrash guard: same streak/burn/inline-script logic as Claude,
    # nudge-only, fail-open (cursor_thrash owns the env bridging).
    if fields["tool_name"] == "Shell":
        import cursor_thrash

        thrash = cursor_thrash.shell_thrash_nudge(fields)
        if thrash:
            nudges.append(thrash)
            _record_observed("thrashNudge", nudge="thrash")
    if nudges:
        _emit({"additional_context": "\n".join(nudges)})


_COMPACT_RESTORE_MAX_BYTES = 8_000  # restore payload cap (bounded inject)


def _capture_compact_checkpoint(fields):
    """U3: translate the Cursor disk transcript and capture a checkpoint.

    Returns the checkpoint path (str) or None. Uses the U1 adapter's
    bundle-proven schema translation and compact_capture's trusted internal
    path; the translated temp file is always removed. Imported ``measure``
    resolves CHECKPOINT_DIR under the Cursor runtime home (CURSOR_VERSION is
    exported to every Cursor hook process), so checkpoint state never lands
    in Claude's ledger. Fail-open: any error returns None and preCompact
    degrades to today's numbers-only recording.
    """
    transcript_path = fields.get("transcript_path")
    sid = fields.get("conversation_id") or ""
    if not transcript_path or not sid:
        return None
    translated = None
    try:
        import cursor_transcript
        from cursor_thrash import _restore_env, _set_env

        home = cursor_home() if cursor_home is not None else None
        if home is None:
            return None
        translated = cursor_transcript.translate_transcript(
            transcript_path, home, sid
        )
        if translated is None:
            return None
        old_snapshot = _set_env(
            "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
            str(home / "token-optimizer" / "snapshot"),
        )
        try:
            import measure

            fill_pct = fields.get("context_usage_percent")
            try:
                fill_pct = float(fill_pct) if fill_pct is not None else None
            except (TypeError, ValueError):
                fill_pct = None
            checkpoint = measure.compact_capture(
                session_id=sid,
                trigger=fields.get("trigger") or "precompact",
                cwd=fields.get("cwd") or None,
                fill_pct=fill_pct,
                _trusted_transcript=translated,
            )
        finally:
            _restore_env("TOKEN_OPTIMIZER_SNAPSHOT_DIR", old_snapshot)
        return str(checkpoint) if checkpoint else None
    except Exception:
        return None
    finally:
        if translated is not None:
            try:
                translated.unlink()
            except OSError:
                pass


def _read_checkpoint_bounded(path_str):
    """Read a checkpoint for restore injection, byte-bounded. Never raises.

    The tally is forgeable local state: only paths that resolve under our own
    data dir (<cursor_home>/token-optimizer) may be read into model context.
    resolve() collapses parent-directory symlink escapes; the leaf symlink
    check above covers the file itself (G1).
    """
    try:
        path = Path(path_str)
        if not path.is_file() or path.is_symlink():
            return None
        home = cursor_home() if cursor_home is not None else None
        if home is None:
            return None
        root = (home / "token-optimizer").resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            return None
        if resolved.stat().st_size > _RESTORE_MAX_BYTES:
            return None
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    body = text.strip()
    if not body:
        return None
    prefix = (
        "Token Optimizer checkpoint captured before compaction "
        "(restore after context rebuild):\n\n"
    )
    marker = "\n\n[truncated: checkpoint exceeds restore budget]"
    # The 8KB contract bounds the WHOLE emitted string (prefix + body +
    # marker) in UTF-8 bytes; cut encoded bytes so multibyte checkpoints
    # cannot inflate the inject (a character cut misses by up to ~4x).
    body_budget = _COMPACT_RESTORE_MAX_BYTES - len(prefix.encode("utf-8"))
    if len(body.encode("utf-8", "replace")) > body_budget:
        cut = body.encode("utf-8", "replace")[
            : body_budget - len(marker.encode("utf-8"))
        ].decode("utf-8", errors="ignore")
        body = cut + marker
    return prefix + body


def handle_pre_compact(payload):
    """Record Cursor's real context numbers and compaction trigger.

    U3: when the payload names a transcript, capture a redacted checkpoint
    through the U1 adapter + compact_capture and mark restore-pending; the
    next postToolUse emits exactly one bounded restore. The preCompact
    ``user_message`` note is emitted and ledgered as ``emitted`` - delivery
    into Cursor's UI is claimed only with positive native evidence.
    """
    fields = decode_payload(payload)
    _record_observed("preCompact", conversation_id=fields["conversation_id"] or None)
    _update_tally(
        fields,
        compaction={
            "trigger": fields["trigger"][:64],
            "context_tokens": fields["context_tokens"],
            "context_window_size": fields["context_window_size"],
            "context_usage_percent": fields["context_usage_percent"],
            "ts": time.time(),
        },
    )
    try:
        checkpoint = _capture_compact_checkpoint(fields)
    except Exception:
        checkpoint = None  # fail-open: never raise into Cursor
    if checkpoint:
        _update_tally(fields, compact_checkpoint=checkpoint, restore_pending=True)
        _record_observed("compactCapture",
                         conversation_id=fields["conversation_id"] or None,
                         checkpoint=os.path.basename(checkpoint))
        _emit({"user_message":
               "Token Optimizer: checkpoint captured before compaction; "
               "it will be offered back after the context rebuild."})


def handle_stop(payload):
    """Per-turn tally + throttled detached rollup and dashboard refresh."""
    fields = decode_payload(payload)
    _record_observed("stop", conversation_id=fields["conversation_id"] or None)
    _update_tally(fields, increment_turns=True)
    if _stop_rollup_due():
        _spawn_rollup()
        _spawn_dashboard()


def handle_session_end(payload):
    """Mark the tally final and spawn an unthrottled rollup + dashboard."""
    fields = decode_payload(payload)
    reason = payload.get("reason") if isinstance(payload, dict) else None
    _record_observed("sessionEnd", conversation_id=fields["conversation_id"] or None)
    _update_tally(fields, terminal=True,
                  end_reason=str(reason)[:64] if reason else "sessionEnd")
    _spawn_rollup()
    _spawn_dashboard()


_RESPONSE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _opt_nonneg_int(value):
    """Plausible token count or None: rejects bools, negatives, non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue >= 0 else None


def handle_after_agent_response(payload):
    """Record the event; ingest real per-response tokens when plausible (KTD3).

    The token fields are third-party-observed and undocumented by Cursor, so
    they are a candidate source only: present + plausible values accumulate
    into the tally flagged ``cursor_hook_measured``; anything else leaves the
    existing source ordering untouched. Record-only: never emits.
    """
    fields = decode_payload(payload)
    raw = payload if isinstance(payload, dict) else {}
    present = [k for k in _RESPONSE_TOKEN_KEYS if k in raw]
    state = "absent"
    tokens = None
    if present:
        vals = {k: _opt_nonneg_int(raw.get(k)) for k in present}
        if all(v is not None for v in vals.values()):
            state = "measured"
            tokens = {
                "input": vals.get("input_tokens") or 0,
                "output": vals.get("output_tokens") or 0,
                "cache_read": vals.get("cache_read_tokens") or 0,
                "cache_write": vals.get("cache_write_tokens") or 0,
            }
        else:
            state = "rejected"
    _record_observed("afterAgentResponse",
                     conversation_id=fields["conversation_id"] or None,
                     generation_id=fields["generation_id"] or None,
                     tokens=state)
    if tokens is not None:
        _update_tally(fields, hook_tokens=tokens)


def _redact_for_ledger(text):
    """Redact before the observed-events ledger; fail CLOSED (drop the field).

    Redaction runs on the full string BEFORE any truncation - a mid-shape
    cut could slice a credential past its pattern's anchors (G1).
    """
    try:
        from credential_patterns import redact_credentials

        redacted = redact_credentials(text)
        return redacted if isinstance(redacted, str) else None
    except Exception:
        return None


def handle_post_tool_use_failure(payload):
    """Count tool failures by tool name; feeds the R9 burn signal. Record-only."""
    fields = decode_payload(payload)
    error = payload.get("error") if isinstance(payload, dict) else None
    redacted_error = _redact_for_ledger(str(error)) if error is not None else None
    _record_observed("postToolUseFailure",
                     conversation_id=fields["conversation_id"] or None,
                     tool_name=fields["tool_name"] or None,
                     tool_use_id=fields["tool_use_id"] or None,
                     error=redacted_error[:256] if redacted_error is not None else None)
    if fields["conversation_id"]:
        _update_tally(fields, failure=True)


def handle_after_shell_execution(payload):
    """Corroborate rewrite-honoured evidence. NEVER counts a tool call: the
    matching postToolUse already did (a Shell call fires both events)."""
    fields = decode_payload(payload)
    command = payload.get("command") if isinstance(payload, dict) else None
    output = payload.get("output") if isinstance(payload, dict) else None
    _record_observed("afterShellExecution",
                     conversation_id=fields["conversation_id"] or None,
                     tool_use_id=fields["tool_use_id"] or None,
                     rewrite=("honoured" if isinstance(command, str)
                              and "bash_compress.py" in command else None),
                     output_len=len(output) if isinstance(output, str) else None)


def handle_subagent_start(payload):
    """Record subagent fan-out; counted once per start for fleet rows."""
    fields = decode_payload(payload)
    _record_observed("subagentStart",
                     conversation_id=fields["conversation_id"] or None,
                     tool_use_id=fields["tool_use_id"] or None)
    if fields["conversation_id"]:
        _update_tally(fields, subagent=True)


def handle_subagent_stop(payload):
    """Record subagent completion. Record-only; the start already counted."""
    fields = decode_payload(payload)
    _record_observed("subagentStop",
                     conversation_id=fields["conversation_id"] or None,
                     tool_use_id=fields["tool_use_id"] or None)


_HANDLERS = {
    "sessionStart": handle_session_start,
    "preToolUse": handle_pre_tool_use,
    "postToolUse": handle_post_tool_use,
    "preCompact": handle_pre_compact,
    "stop": handle_stop,
    "sessionEnd": handle_session_end,
    "afterAgentResponse": handle_after_agent_response,
    "postToolUseFailure": handle_post_tool_use_failure,
    "afterShellExecution": handle_after_shell_execution,
    "subagentStart": handle_subagent_start,
    "subagentStop": handle_subagent_stop,
}


def main(argv=None):
    try:
        from utf8_io import enforce_utf8_io
        enforce_utf8_io()
    except Exception:
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in _HANDLERS:
        return 0
    os.environ.setdefault("TOKEN_OPTIMIZER_RUNTIME", "cursor")
    payload = _read_stdin_payload()
    if payload is None:
        payload = {}
    try:
        _HANDLERS[args[0]](payload)
    except Exception:
        # A hook must never break the user's Cursor session: still exit 0, but
        # never silently. The log itself is guarded — a closed/full stderr
        # (Cursor may not drain hook pipes) must not turn fail-open into
        # fail-hung inside the handler that guarantees exit 0.
        try:
            logger.exception("[cursor_hook_bridge] handler %s failed; failing open", args[0])
        except Exception:
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
