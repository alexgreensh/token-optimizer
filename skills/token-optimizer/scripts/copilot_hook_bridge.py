#!/usr/bin/env python3
"""Token Optimizer — GitHub Copilot CLI hook bridge.

Thin, fast, fail-safe entry point invoked by Copilot's hooks system
(~/.copilot/hooks/token-optimizer.json). One bridge handles all events:

    copilot_hook_bridge.py session-start
    copilot_hook_bridge.py pre-tool-use
    copilot_hook_bridge.py post-tool-use
    copilot_hook_bridge.py stop

Contract notes (verified against github/copilot-cli as of v1.0.60, 2026-06-10):

- Hook payloads arrive on stdin as JSON. ``toolArgs`` may be a JSON-encoded
  STRING (camelCase variant, issue #3349) or a parsed object under
  ``tool_input`` (snake_case variant). Both are handled; anything that fails
  to decode results in a silent no-op — never a malformed permission output.
- Every engine action is gated on ``capabilities.json``: a per-installed-
  version capability map, refreshed whenever the Copilot CLI version changes
  (upgrades AND downgrades — weekly releases break hook fields upstream).
- Output uses Copilot's hookSpecificOutput contract. ``updatedInput`` and
  ``modifiedArgs`` are both emitted (release notes name both; emitting the
  pair tolerates either reader). A schema mismatch is silently ignored by
  Copilot, so the doctor's probe is the source of truth for whether rewrites
  actually take effect on the installed version.

Security posture mirrors bash_hook.py: command rewriting reuses its
whitelist + dangerous-char exclusions; decisions fail CLOSED (emit nothing)
on any uncertainty. This bridge never imports Copilot internals and never
writes outside <copilot_home>/token-optimizer/.
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
from pathlib import Path

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

# The bridge must run even if siblings are missing (partial install): every
# import below is optional, with the dependent feature disabled when absent.
try:
    from runtime_env import copilot_home
except ImportError:  # pragma: no cover - broken install
    copilot_home = None  # type: ignore[assignment]

try:
    import bash_hook as _bash_hook
except ImportError:  # pragma: no cover
    _bash_hook = None  # type: ignore[assignment]

try:
    from codex_io import atomic_write_json as _atomic_write_json_impl
except ImportError:  # pragma: no cover - broken install
    _atomic_write_json_impl = None  # type: ignore[assignment]

_MAX_STDIN_BYTES = 4 * 1024 * 1024  # refuse absurd payloads (amplification)
# Snapshot at import: the installed payload is static for the process lifetime,
# so the per-hook-call exists() stat would be pure waste on the hot path.
_COMPRESS_PATH = _SCRIPT_DIR / "bash_compress.py"
_COMPRESS_AVAILABLE = _COMPRESS_PATH.exists()
_NUDGE_EVENTS_BYTES = (400_000, 900_000)  # events.jsonl size thresholds (proxy for context growth)
_INFLIGHT_STALE_SECS = 7 * 24 * 3600

# Capability keys. Seeds encode the verified matrix for the installed version;
# see _seed_capabilities(). Manual override: TOKEN_OPTIMIZER_COPILOT_CAPS_JSON.
CAP_DENY = "deny"
CAP_ALLOW = "allow"
CAP_UPDATED_INPUT = "updated_input"
CAP_PRETOOL_CTX = "pretooluse_ctx"
CAP_POSTTOOL_CTX = "posttooluse_ctx"
CAP_USERPROMPT_CTX = "userprompt_ctx"
CAP_SESSIONSTART_CTX = "sessionstart_ctx"


# ---------------------------------------------------------------------------
# Payload decoding (fail-closed)
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_session_id(sid):
    """Strip everything but [A-Za-z0-9_-] so a hook payload can never traverse.

    Mirrors measure.sanitize_session_id (inlined to keep the hot-path bridge
    free of the 24K-LOC measure import). A payload sessionId like
    "/../../hooks/token-optimizer" would otherwise let _atomic_write_json /
    unlink escape the data dir — lab-confirmed path traversal.
    """
    if not sid:
        return "unknown"
    cleaned = _SESSION_ID_RE.sub("", sid)[:64]
    return cleaned if len(cleaned) >= 6 else "unknown"


def _read_stdin_payload():
    """Read and decode the hook payload from stdin. None on any failure.

    Intentionally NOT hook_io.read_stdin_hook_input: Copilot payloads carry
    JSON-string-encoded toolArgs that can run large (4MB cap vs hook_io's 1MB)
    and this reader rejects non-dict top-level values outright.
    """
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


def decode_payload(payload):
    """Normalize a Copilot hook payload across its camelCase / snake_case shapes.

    Returns a dict with: tool_name (str), tool_args (dict), session_id (str),
    cwd (str), timestamp. Missing fields default to empty values; a
    string-encoded toolArgs that fails to parse yields {} (fail-closed: the
    caller then has no command to rewrite, so the hook no-ops).
    """
    out = {"tool_name": "", "tool_args": {}, "session_id": "", "cwd": "", "timestamp": None}
    if not isinstance(payload, dict):
        return out

    tool_name = payload.get("toolName", payload.get("tool_name", ""))
    if isinstance(tool_name, str):
        out["tool_name"] = tool_name.strip()

    raw_args = payload.get("toolArgs", payload.get("tool_args", payload.get("tool_input", {})))
    if isinstance(raw_args, str):
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    out["tool_args"] = parsed
            except (json.JSONDecodeError, ValueError):
                pass  # fail-closed: leave {}
    elif isinstance(raw_args, dict):
        out["tool_args"] = raw_args

    sid = payload.get("sessionId", payload.get("session_id", ""))
    if isinstance(sid, str):
        out["session_id"] = _sanitize_session_id(sid)
    cwd = payload.get("cwd", "")
    if isinstance(cwd, str):
        # Cap: cwd is rewritten into the tally on every tool call and into
        # restore-context; an unbounded value would bloat both.
        out["cwd"] = cwd[:1024]
    out["timestamp"] = payload.get("timestamp")
    return out


# ---------------------------------------------------------------------------
# Capabilities (version-gated engine powers)
# ---------------------------------------------------------------------------


def _to_dir():
    """Token Optimizer's data dir under the Copilot home. None if unavailable."""
    if copilot_home is None:
        return None
    try:
        d = copilot_home() / "token-optimizer"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _atomic_write_json(path, obj):
    """Atomic JSON write; concurrent writers never corrupt or orphan tmp files.

    Delegates to codex_io.atomic_write_json (mkstemp + unlink-on-failure).
    """
    if _atomic_write_json_impl is None:
        logger.debug(
            "[copilot_hook_bridge] codex_io unavailable — atomic write to %s "
            "skipped (crash recovery / capability cache degraded this session)",
            path,
        )
        return False
    try:
        _atomic_write_json_impl(path, obj)
        return True
    except OSError as exc:
        logger.debug("[copilot_hook_bridge] atomic write to %s failed: %s", path, exc)
        return False


def _parse_version(text):
    """Extract (major, minor, patch) from a version string. None if absent."""
    if not isinstance(text, str):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _copilot_cli_version():
    """Best-effort `copilot --version`. Returns (version_tuple, raw) or (None, "")."""
    exe = os.environ.get("TOKEN_OPTIMIZER_COPILOT_BIN") or "copilot"
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5
        )
        raw = (proc.stdout or proc.stderr or "").strip()
        return _parse_version(raw), raw
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, ""


def _seed_capabilities(version):
    """Verified capability matrix for a Copilot CLI version (2026-06-10 research).

    Sources: github/copilot-cli release notes + issues #2013/#2585/#2643/
    #2142/#3727. Unknown/future versions stay conservative: fields with a
    history of breakage stay OFF until the matrix is updated; long-stable
    fields stay ON.
    """
    v = version or (0, 0, 0)
    caps = {
        CAP_DENY: True,                      # stable since launch
        CAP_ALLOW: v >= (1, 0, 18),          # issue #2643
        CAP_UPDATED_INPUT: v >= (1, 0, 24),  # v1.0.24 release notes, #2013
        CAP_PRETOOL_CTX: False,              # issue #2585 OPEN as of 2026-06-10
        CAP_POSTTOOL_CTX: v >= (1, 0, 49),   # v1.0.49/51 release notes
        CAP_SESSIONSTART_CTX: True,          # issue #2142 closed-fixed
        # Worked through v1.0.59; regressed in v1.0.60 (#3727). Future
        # versions stay OFF until the regression is confirmed fixed.
        CAP_USERPROMPT_CTX: (1, 0, 30) <= v <= (1, 0, 59),
    }
    return caps


def load_capabilities(refresh=True):
    """Load capabilities.json, re-seeding when the CLI version changed.

    The override env TOKEN_OPTIMIZER_COPILOT_CAPS_JSON (a JSON object) wins
    over everything — the escape hatch when upstream fixes land before our
    matrix does.
    """
    override = os.environ.get("TOKEN_OPTIMIZER_COPILOT_CAPS_JSON", "").strip()
    if override:
        try:
            data = json.loads(override)
            if isinstance(data, dict):
                base = _seed_capabilities(None)
                # deny/allow are security decisions, not version-compat toggles:
                # the override exists to flip injection caps when upstream fixes
                # land, and must never weaken the block primitive.
                base.update({
                    k: bool(v) for k, v in data.items()
                    if k in base and k not in (CAP_DENY, CAP_ALLOW)
                })
                return base
        except (json.JSONDecodeError, ValueError):
            pass

    to_dir = _to_dir()
    cap_path = to_dir / "capabilities.json" if to_dir else None
    cached = None
    if cap_path is not None and cap_path.exists():
        try:
            cached = json.loads(cap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            cached = None

    if not refresh and isinstance(cached, dict) and isinstance(cached.get("caps"), dict):
        return cached["caps"]

    # Hot-path guard: the pre/post-tool-use hooks pass refresh=False. If the
    # cache file is absent (fresh install, first tool before sessionStart), do
    # NOT spawn `copilot --version` on every tool call — that subprocess can
    # block up to its 5s timeout per call. Return a conservative seeded default;
    # sessionStart (refresh=True) writes the real cache shortly after.
    if not refresh:
        return _seed_capabilities(None)

    version, raw = _copilot_cli_version()

    # Detection failure must NOT downgrade a previously-resolved matrix back to
    # the conservative "unknown" seed. The sessionStart hook and `bash install.sh`
    # often run in a WSL-root context where the native-Windows `copilot` binary
    # isn't on PATH, so `copilot --version` returns nothing there even though the
    # CLI is real and capable (issue #78). Erasing known-good caps in that context
    # silently gates postToolUse/allow/updated-input off. Keep what we resolved.
    if (
        version is None
        and isinstance(cached, dict)
        and isinstance(cached.get("caps"), dict)
        and cached.get("cli_version") not in (None, "", "unknown")
    ):
        return cached["caps"]

    version_key = ".".join(str(n) for n in version) if version else "unknown"
    if (
        isinstance(cached, dict)
        and cached.get("cli_version") == version_key
        and isinstance(cached.get("caps"), dict)
    ):
        return cached["caps"]

    return _write_capabilities(version, raw, cap_path)


def _write_capabilities(version, raw, cap_path=None):
    """Seed caps for ``version`` and persist to ``cap_path``. Returns the caps."""
    caps = _seed_capabilities(version)
    version_key = ".".join(str(n) for n in version) if version else "unknown"
    if cap_path is None:
        to_dir = _to_dir()
        cap_path = to_dir / "capabilities.json" if to_dir else None
    if cap_path is not None:
        _atomic_write_json(
            cap_path,
            {
                "cli_version": version_key,
                "cli_version_raw": raw,
                "seeded_at": time.time(),
                "matrix_research_date": "2026-06-10",
                "caps": caps,
            },
        )
    return caps


def reseed_capabilities(version, raw=""):
    """Force-write the capability matrix for an externally-resolved version.

    copilot-doctor calls this to self-heal a matrix stuck at "unknown" (or seeded
    for an older CLI) the moment it HAS resolved the real version. The doctor runs
    in the native shell where `copilot` is on PATH, unlike the WSL-root hook that
    seeded "unknown" (issue #78). Returns the freshly-written caps.
    """
    return _write_capabilities(version, raw)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _emit(obj):
    print(json.dumps(obj))


def handle_session_start(payload):
    """Refresh capabilities, clean stale in-flight tallies, inject continuity."""
    caps = load_capabilities(refresh=True)  # version check happens here

    to_dir = _to_dir()
    if to_dir is not None:
        now = time.time()
        try:
            for p in to_dir.glob("inflight-*.json"):
                try:
                    if now - p.stat().st_mtime > _INFLIGHT_STALE_SECS:
                        p.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    if not caps.get(CAP_SESSIONSTART_CTX):
        return

    # Continuity restore: measure.py copilot-rollup maintains this file from
    # the previous session's decisions/checkpoint intel (U7 seam).
    restore = None
    if to_dir is not None:
        restore_path = to_dir / "restore-context.md"
        try:
            if restore_path.exists() and restore_path.stat().st_size <= 16_384:
                restore = restore_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            restore = None
    if restore:
        _emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "sessionStart",
                    "additionalContext": restore,
                }
            }
        )


def handle_pre_tool_use(payload):
    """Bash output compression via updatedInput, gated and fail-closed."""
    caps = load_capabilities(refresh=False)
    if not caps.get(CAP_UPDATED_INPUT):
        return
    if _bash_hook is None:
        return
    if os.environ.get("TOKEN_OPTIMIZER_BASH_COMPRESS", "").strip() == "0":
        return

    fields = decode_payload(payload)
    # Copilot's Unix terminal tool is `bash` (Windows: `powershell` — no
    # compression there; bash_compress wrappers are POSIX).
    if fields["tool_name"] != "bash":
        return
    command = fields["tool_args"].get("command", "")
    if not isinstance(command, str) or not command:
        return
    if _bash_hook._has_dangerous_chars(command):
        return
    if not _bash_hook._is_whitelisted(command):
        return

    if not _COMPRESS_AVAILABLE:
        return
    compress_path = _COMPRESS_PATH
    try:
        original_tokens = shlex.split(command)
    except ValueError:
        return
    rewritten = (
        shlex.quote(sys.executable)
        + " " + shlex.quote(str(compress_path))
        + " " + " ".join(shlex.quote(t) for t in original_tokens)
    )

    # Emit only the fields we validated/produced. Echoing the whole tool_args
    # dict would forward un-checked model-generated fields (e.g. a future
    # `restart` flag) straight back into Copilot's execution input.
    updated = {"command": rewritten}
    if isinstance(fields["tool_args"].get("description"), str):
        updated["description"] = fields["tool_args"]["description"]

    # Release notes name both `updatedInput` and `modifiedArgs`; emit both so
    # either reader applies. permissionDecision allow suppresses the approval
    # dialog for the rewrite (capability-gated separately).
    hook_out = {
        "hookEventName": "preToolUse",
        "updatedInput": updated,
        "modifiedArgs": updated,
    }
    if caps.get(CAP_ALLOW):
        hook_out["permissionDecision"] = "allow"
    _emit({"hookSpecificOutput": hook_out})


class _session_lock:
    """Advisory exclusive lock over a session's tally, so concurrent tool
    calls don't lose updates in the read-modify-write. No-op on Windows or
    when fcntl is unavailable (best-effort; the tally is a soft counter)."""

    def __init__(self, to_dir, sid):
        self._path = to_dir / f"inflight-{sid}.lock"
        self._fh = None

    def __enter__(self):
        try:
            import fcntl  # noqa: PLC0415

            self._fh = open(self._path, "w", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            try:
                self._fh.close()
            except OSError:
                pass
        return False


def handle_post_tool_use(payload):
    """Update the in-flight tally (crash recovery + liveness) and nudge."""
    fields = decode_payload(payload)
    sid = fields["session_id"] or "unknown"
    to_dir = _to_dir()
    nudge_level = 0
    tool_calls = 1

    if to_dir is not None:
      with _session_lock(to_dir, sid):
        tally_path = to_dir / f"inflight-{sid}.json"
        tally = {}
        try:
            if tally_path.exists():
                tally = json.loads(tally_path.read_text(encoding="utf-8"))
                if not isinstance(tally, dict):
                    tally = {}
        except (OSError, json.JSONDecodeError, ValueError):
            tally = {}
        tool_calls = int(tally.get("tool_calls", 0) or 0) + 1
        tally.update(
            {
                "session_id": sid,
                "updated_at": time.time(),
                "tool_calls": tool_calls,
                "cwd": fields["cwd"],
                "models": tally.get("models", {}),
            }
        )
        nudge_level = int(tally.get("nudge_level", 0) or 0)
        # Hot path: skip the events.jsonl stat once the level is maxed out,
        # and only touch capabilities.json when a nudge is actually firing.
        if nudge_level < len(_NUDGE_EVENTS_BYTES):
            new_level = _events_growth_level(sid)
        else:
            new_level = nudge_level
        if new_level > nudge_level:
            tally["nudge_level"] = new_level
        _atomic_write_json(tally_path, tally)

        if new_level > nudge_level and load_capabilities(refresh=False).get(CAP_POSTTOOL_CTX):
            _emit(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "postToolUse",
                        "additionalContext": (
                            "[Token Optimizer] Context is growing large for this "
                            "session. Prefer targeted reads over full files, avoid "
                            "re-reading unchanged files, and summarize before "
                            "continuing long explorations."
                        ),
                    }
                }
            )


def _events_growth_level(session_id):
    """0/1/2 by events.jsonl size — a cheap proxy for context growth.

    Hook payloads carry no token data (issue #3686), so size thresholds on the
    persisted event stream are the honest live signal available per tool call.
    """
    if copilot_home is None or not session_id or session_id == "unknown":
        return 0
    try:
        events = copilot_home() / "session-state" / session_id / "events.jsonl"
        size = events.stat().st_size
    except OSError:
        return 0
    level = 0
    for threshold in _NUDGE_EVENTS_BYTES:
        if size >= threshold:
            level += 1
    return level


def handle_stop(payload):
    """Fire-and-forget rollup so trends stay fresh without blocking the CLI."""
    fields = decode_payload(payload)
    measure = _SCRIPT_DIR / "measure.py"
    if not measure.exists():
        return
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_RUNTIME"] = "copilot"
    # Force UTF-8 in the spawned measure.py so non-ASCII session content can't crash it
    # on a non-UTF-8 host (parity with run.py / the hermes bridge). measure.py also
    # self-re-execs, but inject here so coverage doesn't hinge on os.execv being available.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.Popen(
            [sys.executable, str(measure), "copilot-rollup", "--quiet"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    # Remove this session's in-flight tally: shutdown event is now authoritative.
    to_dir = _to_dir()
    sid = fields["session_id"]
    if to_dir is not None and sid:
        try:
            (to_dir / f"inflight-{sid}.json").unlink()
        except OSError:
            pass


_HANDLERS = {
    "session-start": handle_session_start,
    "pre-tool-use": handle_pre_tool_use,
    "post-tool-use": handle_post_tool_use,
    "stop": handle_stop,
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
    os.environ.setdefault("TOKEN_OPTIMIZER_RUNTIME", "copilot")
    payload = _read_stdin_payload()
    if payload is None:
        payload = {}
    try:
        _HANDLERS[args[0]](payload)
    except Exception:
        # A hook must never break the user's Copilot session. No output, exit 0.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
