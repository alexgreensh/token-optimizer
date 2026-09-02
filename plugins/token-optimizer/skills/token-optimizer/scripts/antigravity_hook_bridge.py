#!/usr/bin/env python3
"""Token Optimizer — Google Antigravity hook bridge.

One fast, fail-open entry point for the three wired Antigravity lifecycle
events, registered via ``~/.gemini/config/plugins/token-optimizer/hooks.json``:

  pre-invocation  -> continuity restore (invocationNum 1) or context nudge
  pre-tool-use    -> bash output compression (run_command only)
  stop            -> detached rollup + dashboard regeneration

Design contract (R15): every handler exits 0 and emits EITHER one JSON object
or nothing, on any error, malformed stdin, oversize stdin (over 4 MB), or a
missing dependency. Consent-gated (R20): without the install record in
``~/.gemini/token-optimizer/config.json`` every handler no-ops to ``{}``.

Hot-path imports stay stdlib + the copied payload modules (runtime_env,
bash_hook, spawn_utils). ``measure.py`` is never imported here — the Stop hook
locates it through the ``measure-path`` locator written by the installer (KTD5,
the Hermes #58 precedent).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    from utf8_io import enforce_utf8_io  # noqa: PLC0415
except Exception:  # pragma: no cover
    def enforce_utf8_io() -> None:
        pass

_CONVERSATION_ID_RE = None
try:
    import re as _re
    _CONVERSATION_ID_RE = _re.compile(r"^[0-9a-f-]{1,64}$")
except Exception:  # pragma: no cover
    pass

_STDIN_MAX_BYTES = 4 * 1024 * 1024  # 4 MB (R15)
_RESTORE_MAX_BYTES = 16 * 1024      # 16 KB (R11)
_FIELD_MAX_CHARS = 200              # R22 per-field cap
_NUDGE_STATE_TTL_SECONDS = 7 * 86400
_ROLLUP_LEASE_SECONDS = 30

_SURFACES = ("antigravity-cli", "antigravity", "antigravity-ide")

_SCRIPT_DIR = Path(__file__).resolve().parent
_MEASURE_LOCATOR = _SCRIPT_DIR / "measure-path"


def _to_dir(home: Path) -> Path:
    return home / "token-optimizer"


def _consent_ok(home: Path) -> bool:
    """True when the installer recorded data consent."""
    config_path = _to_dir(home) / "config.json"
    try:
        if not config_path.is_file():
            return False
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return bool(isinstance(cfg, dict) and cfg.get("antigravity_consent"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _clean_field(value, max_chars: int = _FIELD_MAX_CHARS) -> str:
    """Printable-only, single-line, length-capped text (R22)."""
    text = str(value or "")
    text = "".join(ch if ch == " " or ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:max_chars]


def _read_payload() -> dict | None:
    """Read the hook JSON payload from stdin with a 4 MB cap."""
    try:
        data = sys.stdin.buffer.read(_STDIN_MAX_BYTES + 1)
    except (OSError, AttributeError):
        return None
    if not data:
        return {}
    if len(data) > _STDIN_MAX_BYTES:
        return None
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _emit(obj: dict) -> None:
    """Write exactly one JSON object to stdout."""
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# PreInvocation
# ---------------------------------------------------------------------------

def _restore_message(home: Path) -> str | None:
    restore_path = _to_dir(home) / "restore-context.md"
    try:
        if not restore_path.is_file() or restore_path.stat().st_size > _RESTORE_MAX_BYTES:
            return None
        content = restore_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not content:
        return None
    # Defense-in-depth: re-clean every line (printable, single-line, capped).
    cleaned = "\n".join(_clean_field(line) for line in content.splitlines())
    return cleaned.strip() or None


def _surface_dir_for_transcript(home: Path, transcript_path) -> Path | None:
    """Resolve the surface directory from transcriptPath's parent basename.

    The Antigravity hook contract names the surface as the immediate parent
    directory of the transcript (e.g. ``.../.gemini/antigravity-cli/transcript.jsonl``).
    Anything else is rejected (no file access), the KTD6 exact-parent match.
    """
    try:
        parent = Path(str(transcript_path)).parent
        if parent.name not in _SURFACES:
            return None
        surf = home / parent.name
        return surf if (surf.is_dir() and not surf.is_symlink()) else None
    except (OSError, ValueError):
        return None


def _live_fill(surface_dir: Path, conversation_id: str) -> float | None:
    try:
        from antigravity_state import read_live_conversation
    except Exception:
        return None
    db = surface_dir / "conversations" / f"{conversation_id}.db"
    session = read_live_conversation(db)
    if session is None:
        return None
    return session.get("last_fill")


def _current_fill_from_payload(home: Path, payload: dict) -> float | None:
    conversation_id = str(payload.get("conversationId") or "")
    if not _CONVERSATION_ID_RE or not _CONVERSATION_ID_RE.match(conversation_id):
        return None
    surface_dir = _surface_dir_for_transcript(home, payload.get("transcriptPath") or "")
    if surface_dir is None:
        return None
    return _live_fill(surface_dir, conversation_id)


def _nudge_state_path(home: Path, conversation_id: str) -> Path:
    return _to_dir(home) / "nudge-state" / f"{conversation_id}.json"


def _nudge_already_sent(home: Path, conversation_id: str, threshold: str) -> bool:
    path = _nudge_state_path(home, conversation_id)
    try:
        if not path.is_file():
            return False
        state = json.loads(path.read_text(encoding="utf-8"))
        return bool(isinstance(state, dict) and state.get(threshold))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _record_nudge(home: Path, conversation_id: str, threshold: str) -> None:
    path = _nudge_state_path(home, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    try:
        if path.is_file():
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
    except (OSError, json.JSONDecodeError, ValueError):
        state = {}
    state[threshold] = True
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".nudge.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _sweep_nudge_state(home: Path) -> None:
    ndir = _to_dir(home) / "nudge-state"
    try:
        if not ndir.is_dir():
            return
        cutoff = time.time() - _NUDGE_STATE_TTL_SECONDS
        for p in ndir.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def handle_pre_invocation(payload: dict) -> dict:
    try:
        from runtime_env import antigravity_home
    except Exception:
        return {}
    home = antigravity_home()

    invocation_num = payload.get("invocationNum")
    try:
        invocation_num = int(invocation_num)
    except (TypeError, ValueError):
        invocation_num = 0

    if invocation_num == 1:
        message = _restore_message(home)
        if message:
            return {"injectSteps": [{"ephemeralMessage": message}]}
        return {}

    if invocation_num <= 1:
        return {}

    _sweep_nudge_state(home)
    fill = _current_fill_from_payload(home, payload)
    try:
        if fill is None or not (0.0 <= fill <= 1.0):
            return {}
    except (TypeError, ValueError):
        return {}

    conversation_id = str(payload.get("conversationId") or "")

    if fill >= 0.85:
        if _nudge_already_sent(home, conversation_id, "85"):
            return {}
        _record_nudge(home, conversation_id, "85")
        return {"injectSteps": [{"ephemeralMessage": (
            "[Token Optimizer] Context is above 85% full. Prefer small, focused "
            "changes; start a fresh conversation soon to avoid truncation."
        )}]}
    if fill >= 0.70:
        if _nudge_already_sent(home, conversation_id, "70"):
            return {}
        _record_nudge(home, conversation_id, "70")
        return {"injectSteps": [{"ephemeralMessage": (
            "[Token Optimizer] Context is above 70% full. Keep prompts tight."
        )}]}
    return {}


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

def handle_pre_tool_use(payload: dict) -> dict:
    if os.name == "nt":
        return {}
    if os.environ.get("TOKEN_OPTIMIZER_BASH_COMPRESS", "").strip() == "0":
        return {}

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return {}
    if tool_call.get("name") != "run_command":
        return {}
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return {}
    command = args.get("CommandLine")
    if not isinstance(command, str) or not command:
        return {}

    try:
        import bash_hook as _bash_hook
    except Exception:
        return {}

    if _bash_hook._has_dangerous_chars(command):
        return {}
    if not _bash_hook._is_whitelisted(command):
        return {}

    compress_path = _SCRIPT_DIR / "bash_compress.py"
    try:
        if not compress_path.is_file():
            return {}
        original_tokens = shlex.split(command)
        if not original_tokens:
            return {}
    except ValueError:
        return {}

    rewritten = (
        shlex.quote(sys.executable)
        + " -E -s "
        + shlex.quote(str(compress_path))
        + " " + " ".join(shlex.quote(t) for t in original_tokens)
    )
    # decision "ask" defers to the user's permission mode + "always allow" cache
    # (R13); we never emit allow/permissionOverrides or forward model fields.
    return {"decision": "ask", "overwrite": {"CommandLine": rewritten}}


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def _locate_measure_py() -> Path | None:
    if _MEASURE_LOCATOR.is_file():
        try:
            located = Path(_MEASURE_LOCATOR.read_text(encoding="utf-8").strip())
            if located.is_file():
                return located
        except (OSError, ValueError):
            pass
    sibling = _SCRIPT_DIR / "measure.py"
    if sibling.is_file():
        return sibling
    return None


def _rollup_lease_held(home: Path) -> bool:
    lease = _to_dir(home) / "rollup.lease"
    try:
        if not lease.exists():
            return False
        if (time.time() - lease.stat().st_mtime) < _ROLLUP_LEASE_SECONDS:
            return True
        return False
    except OSError:
        return False


def _touch_rollup_lease(home: Path) -> None:
    lease = _to_dir(home) / "rollup.lease"
    try:
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.touch()
    except OSError:
        pass


def handle_stop(payload: dict) -> dict:
    try:
        from runtime_env import antigravity_home
    except Exception:
        return {}
    home = antigravity_home()
    if _rollup_lease_held(home):
        return {}

    measure_py = _locate_measure_py()
    if measure_py is None:
        return {}

    _touch_rollup_lease(home)

    try:
        from spawn_utils import spawn_detached
    except Exception:
        return {}

    env = {
        **os.environ,
        "TOKEN_OPTIMIZER_RUNTIME": "antigravity",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        spawn_detached(
            [sys.executable, str(measure_py), "antigravity-rollup", "--quiet"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    try:
        spawn_detached(
            [sys.executable, str(measure_py), "dashboard", "--quiet"],
            env={**env, "TOKEN_OPTIMIZER_INTERACTIVE": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    try:
        enforce_utf8_io()
    except Exception:
        pass

    event = ""
    if argv:
        event = argv[0]
    elif len(sys.argv) > 1:
        event = sys.argv[1]

    payload = _read_payload() or {}

    try:
        from runtime_env import antigravity_home
    except Exception:
        return 0

    try:
        if not _consent_ok(antigravity_home()):
            _emit({})
            return 0

        if event == "pre-invocation":
            out = handle_pre_invocation(payload)
        elif event == "pre-tool-use":
            out = handle_pre_tool_use(payload)
        elif event == "stop":
            out = handle_stop(payload)
        else:
            return 0
        _emit(out)
    except Exception:
        # Fail open: never break the Antigravity loop (R15).
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:2]))
