#!/usr/bin/env python3
"""Token Optimizer — Grok Build hook bridge.

Thin, fast, fail-safe entry point invoked by Grok Build's hooks system
(``$GROK_HOME/hooks/token-optimizer.json``). One bridge handles every event TO
wires:

    grok_hook_bridge.py SessionStart
    grok_hook_bridge.py UserPromptSubmit
    grok_hook_bridge.py PreToolUse
    grok_hook_bridge.py PostToolUse
    grok_hook_bridge.py Stop

Contract notes (source: the cloned ``github.com/xai-org/grok-build`` repo,
``crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md`` — the only source
of truth; TO is built in NO-INSTALL / contract-only mode, so every assumed
shape below cites that file and is marked "needs live verification" in
G-STATUS.md):

- Payloads arrive on stdin as ONE JSON object with a camelCase envelope:
  ``hookEventName`` (grok's snake_case name, e.g. ``pre_tool_use``),
  ``hook_event_name`` (Claude's PascalCase name, e.g. ``PreToolUse``),
  ``sessionId``, ``cwd``, ``workspaceRoot``, ``timestamp``, ``permissionMode``,
  ``promptId``; tool events add ``toolName``, ``toolInput``, ``toolUseId``,
  ``toolInputTruncated`` (10-hooks.md "Writing Hook Scripts / Input").
- Bash is grok's ``run_terminal_command`` tool (alias table maps ``Bash`` to
  it); the installer scopes PreToolUse with ``matcher: "Bash"`` so only bash
  commands enter the rewrite hot path.
- Output is Grok's top-level contract (NOT the Copilot ``hookSpecificOutput``
  envelope from the ``updatedInput`` standpoint — grok accepts both a top-level
  ``decision`` and ``hookSpecificOutput.permissionDecision``/``updatedInput``/
  ``additionalContext``): PreToolUse rewrite → ``{"hookSpecificOutput":
  {"hookEventName": "PreToolUse", "updatedInput": {...}}}`` (omitting
  ``decision`` = allow + apply rewrite); PostToolUse nudge → ``{"hookSpecificOutput":
  {"hookEventName": "PostToolUse", "additionalContext": "..."}}``.
- Fail-open everywhere: TO never emits ``deny``/``block``; a timed-out or
  crashed hook never blocks a tool call (10-hooks.md "How a Hook Resolves",
  step 4).

Security posture mirrors bash_hook.py: the bash rewrite inherits its whitelist
+ dangerous-char exclusions and fails CLOSED (emit nothing) on any uncertainty.
This bridge never imports Grok internals and never writes outside
``<grok_home>/token-optimizer/``.
"""

from __future__ import annotations

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

# Windows console-flash guard (#107). getattr -> 0 on POSIX.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

# The bridge must run even if siblings are missing (partial install): every
# import is optional and the dependent feature degrades/disabled when absent.
try:
    from runtime_env import grok_home
except ImportError:  # pragma: no cover - broken install
    grok_home = None  # type: ignore[assignment]

try:
    from spawn_utils import spawn_detached
except ImportError:  # pragma: no cover - broken install
    logger.warning("[grok] spawn_utils import failed; using degraded fallback")

    def spawn_detached(argv, **popen_kwargs):  # type: ignore[no-redef]
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
_STALE_LOCK_SECS = 7 * 24 * 3600    # sessionStart lock sweep threshold
_NUDGE_TOOL_CALLS = (30, 80)        # context-growth nudge thresholds (tool proxy)

# Snapshot at import: the installed payload is static for the process lifetime.
_COMPRESS_PATH = _SCRIPT_DIR / "bash_compress.py"
_COMPRESS_AVAILABLE = _COMPRESS_PATH.exists()

_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_session_id(sid):
    """Strip everything but [A-Za-z0-9_-] so a payload id can never traverse."""
    if not sid:
        return "unknown"
    cleaned = _SESSION_ID_RE.sub("", sid)[:64]
    return cleaned if len(cleaned) >= 6 else "unknown"


# ---------------------------------------------------------------------------
# Documented-capability gate (static; contract-only mode)
#
# Unlike the Copilot bridge, Grok hooks are NOT version-gated against a live
# ``grok --version`` (there is no Grok host to probe in NO-INSTALL mode). The
# gate keys EVERY assumed capability to the cloned source's documented contract
# (10-hooks.md) so each assumption is traceable. All entries are marked
# "needs live verification" in G-STATUS.md.
# ---------------------------------------------------------------------------

CAP_UPDATED_INPUT = "updated_input"     # 10-hooks.md "Output (Blocking Hooks)"
CAP_POSTTOOL_CTX = "posttooluse_ctx"    # 10-hooks.md "PostToolUse Output"
CAP_SESSIONSTART_CTX = "sessionstart_ctx"  # passive: stdout ignored (10-hooks.md "Passive Hooks")
CAP_USERPROMPT_CTX = "userprompt_ctx"   # allowing stdout discarded (10-hooks.md "UserPromptSubmit Decision Control")


_CAPABILITIES = None


def _documented_caps() -> dict:
    """The documented Grok hook contract (static — no live host in this mode)."""
    return {
        CAP_UPDATED_INPUT: True,
        CAP_POSTTOOL_CTX: True,
        CAP_SESSIONSTART_CTX: False,   # SessionStart stdout is ignored
        CAP_USERPROMPT_CTX: False,     # allowing UserPromptSubmit stdout is discarded
    }


def load_capabilities():
    """Return the static documented matrix.

    Unlike the Copilot bridge (which re-probes a live CLI version), Grok's
    capability matrix is static in contract-only mode, so there is no refresh.
    Cached at first call to avoid allocating a fresh dict on every tool event.
    """
    global _CAPABILITIES
    if _CAPABILITIES is None:
        _CAPABILITIES = _documented_caps()
    return _CAPABILITIES


# ---------------------------------------------------------------------------
# Payload decoding (fail-closed)
# ---------------------------------------------------------------------------


def _read_stdin_payload():
    """Read and decode the Grok hook payload from stdin. None on any failure."""
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
    """toolInput as a dict, or a JSON-encoded string -> dict; {} otherwise."""
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
    """Normalize a Grok hook payload into a flat, safe dict.

    ``hook_event_name`` (PascalCase) wins over ``hookEventName`` (snake_case)
    for the event label the handlers key on. ``cwd`` is capped at 1024 chars
    because it is rewritten into the tally on every tool call.
    """
    out = {
        "session_id": "",
        "event_name": "",
        "cwd": "",
        "workspace_root": "",
        "tool_name": "",
        "tool_args": {},
        "prompt_id": "",
        "permission_mode": "",
        "timestamp": None,
    }
    if not isinstance(payload, dict):
        return out

    sid = payload.get("sessionId", payload.get("session_id", ""))
    if isinstance(sid, str):
        out["session_id"] = _sanitize_session_id(sid)

    event = payload.get("hook_event_name", payload.get("hookEventName", ""))
    if isinstance(event, str):
        out["event_name"] = event.strip()

    cwd = payload.get("cwd", "")
    if isinstance(cwd, str):
        out["cwd"] = cwd[:1024]

    root = payload.get("workspaceRoot", "")
    if isinstance(root, str):
        out["workspace_root"] = root[:1024]

    tool_name = payload.get("toolName", payload.get("tool_name", ""))
    if isinstance(tool_name, str):
        out["tool_name"] = tool_name.strip()

    out["tool_args"] = _parse_tool_input(
        payload.get("toolInput", payload.get("tool_input", {}))
    )

    prompt_id = payload.get("promptId", "")
    if isinstance(prompt_id, str):
        out["prompt_id"] = prompt_id[:64]

    mode = payload.get("permissionMode", "")
    if isinstance(mode, str):
        out["permission_mode"] = mode[:64]

    out["timestamp"] = payload.get("timestamp")
    return out


# ---------------------------------------------------------------------------
# Data dir + atomic write + observed-events ledger
# ---------------------------------------------------------------------------


_TO_DIR_HOME = None
_TO_DIR_CACHED = None


def _to_dir():
    """Token Optimizer's data dir under the Grok home. None if unavailable.

    Memoized on the resolved grok_home() path so the hot path (3+ calls per
    tool event via _record_observed, _tally_path, _update_tally) does one
    mkdir, not three.
    """
    global _TO_DIR_HOME, _TO_DIR_CACHED
    if grok_home is None:
        return None
    try:
        home = grok_home()
        if _TO_DIR_CACHED is not None and _TO_DIR_HOME == home:
            return _TO_DIR_CACHED
        d = home / "token-optimizer"
        d.mkdir(parents=True, exist_ok=True)
        _TO_DIR_HOME, _TO_DIR_CACHED = home, d
        return d
    except OSError:
        return None


def _atomic_write_json(path, obj):
    if _atomic_write_json_impl is None:
        return False
    try:
        # replace_symlink=True: even if an attacker plants a symlink at the
        # tally/observed-events path between calls, os.replace swaps the
        # symlink itself, never its target. Matches _copy_no_follow in the
        # installer and the hooks-file write.
        _atomic_write_json_impl(path, obj, replace_symlink=True)
        return True
    except OSError:
        return False


def _record_observed(event, **extra):
    """Append one observed-events line (single-line JSONL). Never raises."""
    to_dir = _to_dir()
    if to_dir is None:
        return
    entry = {"event": event, "ts": time.time()}
    entry.update({k: v for k, v in extra.items() if v is not None})
    try:
        with (to_dir / "observed-events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        global _observed_warned
        if not _observed_warned:
            _observed_warned = True
            logger.warning("[grok_hook_bridge] observed-events append failed; "
                           "further append failures are silent", exc_info=True)


# ---------------------------------------------------------------------------
# Tally read-modify-write (nudge proxy + crash-recovery liveness)
# ---------------------------------------------------------------------------


def _tally_path(fields):
    to_dir = _to_dir()
    if to_dir is None:
        return None
    sid = fields.get("session_id") or "unknown"
    return to_dir / f"inflight-{sid}.json"


def _load_tally(path):
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


@contextmanager
def _session_lock(to_dir, sid):
    """Bounded portable lease around a tally read-modify-write. Fail-open."""
    if to_dir is None or sid in (None, "", "unknown"):
        yield False
        return
    lock_dir = to_dir / ".locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield True
        return
    if lease_lock is None:
        yield True
        return
    lock_path = lock_dir / f"inflight-{sid}.lock"
    with lease_lock(lock_path, acquire_timeout=0.075) as acquired:
        yield acquired


def _update_tally(fields, *, count_tool=False, bump_nudge=False, terminal=False):
    """Read-modify-write the session tally; returns ``(tally, nudge_emitted)``."""
    to_dir = _to_dir()
    if to_dir is None:
        return None, False
    sid = fields.get("session_id") or "unknown"
    with _session_lock(to_dir, sid) as acquired:
        if not acquired:
            return None, False
        path = _tally_path(fields)
        tally = _load_tally(path)
        now = time.time()
        if tally is None:
            tally = {"session_id": sid, "first_ts": now}
        if terminal:
            tally["final"] = True
            tally["end_reason"] = "stop"
        else:
            if tally.get("final") or tally.get("end_reason") == "stop":
                tally["final"] = False
                tally["end_reason"] = ""
        tally["updated_at"] = now
        tally.setdefault("tool_calls", 0)
        if fields.get("cwd"):
            tally["cwd"] = fields["cwd"]
        if fields.get("workspace_root"):
            tally["workspace_root"] = fields["workspace_root"]

        if count_tool:
            tally["tool_calls"] = int(tally.get("tool_calls", 0) or 0) + 1

        nudge_emitted = False
        if bump_nudge:
            new_level = _nudge_level(int(tally.get("tool_calls", 0) or 0))
            if new_level > int(tally.get("nudge_level", 0) or 0):
                tally["nudge_level"] = new_level
                nudge_emitted = True

        _atomic_write_json(path, tally)
        return tally, nudge_emitted


def _sweep_stale_locks():
    """Remove stale lock/candidate artifacts (7d) on SessionStart. Never fatal."""
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
# Compression + nudge helpers
# ---------------------------------------------------------------------------


def _compression_enabled():
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
    env["TOKEN_OPTIMIZER_RUNTIME"] = "grok"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if interactive:
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
    return env


# The installer does NOT copy measure.py into the plugin dir (version-drift
# risk); it writes a one-line "measure-path" locator naming the canonical
# measure.py. Resolution: a sibling measure.py (dev/test checkout), then the
# locator. Mirrors hermes/cursor _locate_measure_py.
_MEASURE_LOCATOR = _SCRIPT_DIR / "measure-path"


def _locate_measure_py():
    """Return the path to measure.py, or None (rollups paused) if not found."""
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


def _spawn_measure(command, *, interactive=False):
    """Shared spawn path for rollup and dashboard (probe guard + measure
    lookup + detach). Collapses the two near-duplicate helpers."""
    if os.environ.get("TOKEN_OPTIMIZER_PROBE") == "1":
        return
    measure = _locate_measure_py()
    if measure is None:
        return
    try:
        spawn_detached(
            [sys.executable, str(measure)] + command,
            env=_measure_env(interactive=interactive),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("[grok_hook_bridge] detached %s spawn failed",
                       command[0], exc_info=True)


def _spawn_rollup():
    _spawn_measure(["grok-rollup", "--quiet"])


def _spawn_dashboard():
    _spawn_measure(["dashboard", "--quiet"], interactive=True)


_STOP_ROLLUP_SECS = 120  # at most one stop rollup per machine per 120s


def _stop_rollup_due():
    """True once per 120s per machine. Mirrors the cursor bridge throttle."""
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
    return _atomic_write_json(state, {"last": now})


# ---------------------------------------------------------------------------
# Event handlers (all fail-open: never raise into Grok)
# ---------------------------------------------------------------------------


def _emit(obj):
    print(json.dumps(obj))


def handle_session_start(payload):
    """Sweep stale locks + init the tally. Passive: stdout is IGNORED by grok.

    grok's SessionStart is a passive event whose stdout is ignored
    (10-hooks.md "Passive Hooks"), so no ``additionalContext`` is emitted — the
    documented contract cannot deliver continuity at this seam.
    """
    fields = decode_payload(payload)
    _record_observed("SessionStart", session_id=fields["session_id"] or None)
    _update_tally(fields)
    _sweep_stale_locks()


def handle_user_prompt_submit(payload):
    """Record the prompt submit. Allowing stdout is DISCARDED by grok.

    grok discards an allowing UserPromptSubmit hook's stdout (10-hooks.md
    "UserPromptSubmit Decision Control"), so TO emits nothing here — only the
    observed-events ledger is updated.
    """
    fields = decode_payload(payload)
    _record_observed("UserPromptSubmit", session_id=fields["session_id"] or None)
    _update_tally(fields)


def handle_pre_tool_use(payload):
    """Bash output compression via updatedInput, gated and fail-closed."""
    fields = decode_payload(payload)
    if not load_capabilities().get(CAP_UPDATED_INPUT):
        return
    # grok's bash tool is ``run_terminal_command`` (alias ``Bash``); the
    # installer's matcher already scoped this hook to the Bash tool, but the
    # name check is defense-in-depth in case a config without the matcher fires.
    if fields["tool_name"] not in ("run_terminal_command", "Bash"):
        return
    if not _compression_enabled():
        return
    command = fields["tool_args"].get("command")
    if not isinstance(command, str) or not command:
        return
    if _bash_hook._has_dangerous_chars(command):
        return
    if not _bash_hook._is_whitelisted(command):
        return
    try:
        original_tokens = shlex.split(command)
    except ValueError:
        return
    if not original_tokens:
        return
    rewritten = (
        shlex.quote(sys.executable)
        + " " + shlex.quote(str(_COMPRESS_PATH))
        + " " + " ".join(shlex.quote(t) for t in original_tokens)
    )
    # Echo every original field and replace only ``command`` so any future
    # fields survive the whole-toolInput replacement contract.
    updated = dict(fields["tool_args"])
    updated["command"] = rewritten
    # Omitting ``decision`` = allow + apply the rewrite (10-hooks.md).
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "updatedInput": updated}})
    _record_observed("PreToolUse", tool_name=fields["tool_name"] or None,
                     rewrite="attempted")


def handle_post_tool_use(payload):
    """Record the tool + nudge via additionalContext (fail-open)."""
    fields = decode_payload(payload)
    _record_observed("PostToolUse", tool_name=fields["tool_name"] or None)
    tally, nudge_emitted = _update_tally(fields, count_tool=True, bump_nudge=True)
    if tally is None:
        return
    if nudge_emitted and load_capabilities().get(CAP_POSTTOOL_CTX):
        _emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": _NUDGE_TEXT}})


def handle_stop(payload):
    """Mark the tally final + throttled detached rollup and dashboard refresh.

    Exit 0 + no output = allow the stop (10-hooks.md "Stop Decision Control").
    """
    fields = decode_payload(payload)
    _record_observed("Stop", session_id=fields["session_id"] or None)
    _update_tally(fields, terminal=True)
    if _stop_rollup_due():
        _spawn_rollup()
        _spawn_dashboard()


_HANDLERS = {
    "SessionStart": handle_session_start,
    "UserPromptSubmit": handle_user_prompt_submit,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "Stop": handle_stop,
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
    os.environ.setdefault("TOKEN_OPTIMIZER_RUNTIME", "grok")
    payload = _read_stdin_payload()
    if payload is None:
        payload = {}
    try:
        _HANDLERS[args[0]](payload)
    except Exception:
        try:
            logger.exception("[grok_hook_bridge] handler %s failed; failing open", args[0])
        except Exception:
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
