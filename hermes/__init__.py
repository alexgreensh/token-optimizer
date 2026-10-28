"""token-optimizer — Hermes plugin for Token Optimizer.

Provides the same feature set Token Optimizer gives Claude Code and Codex,
through Hermes's plugin hooks:

  - terminal output compression, loop detection, cross-turn dedup and archive
    with expand pointers (transform_terminal_output)
  - large MCP result archive + preview, activity/decision tracking, read-cache
    upkeep after edits (transform_tool_result)
  - delta / structure-map re-reads and the MCP re-fetch guard (pre_tool_call)
  - checkpoints at fill bands, before compaction, before subagent fan-out,
    after edit batches, at turn end, on fatal API errors and at session end
  - post-compaction restore and cross-session continuity hints (pre_llm_call)
  - proactive context nudges, per-turn token accumulation, session rollup into
    TO's trends.db, credential patterns registered with Hermes's redactor
  - a /token-optimizer slash command (summary, cost, checkpoint, expand) and a
    ``hermes token-optimizer`` CLI subcommand (dashboard)

Every engine runs in a short child process (the same scripts Claude Code's
hooks run), never inside Hermes, and every hook fails open.

Plugin layout: this file lives at ~/.hermes/plugins/token-optimizer/__init__.py
(installed by ``measure.py hermes-install`` or ``hermes_install.py``).

sys.path assumption
-------------------
The plugin needs to import three TO modules from ``scripts/``:
  - hermes_hook_bridge  (thin shim; sits next to this file after install)
  - hermes_state        (read-only state.db reader)
  - hermes_session      (normalizer + quality scorer)

After installation the full plugin directory layout is:

  ~/.hermes/plugins/token-optimizer/
      __init__.py          (this file)
      plugin.yaml
      README.md
      hermes_hook_bridge.py
      hermes_state.py
      hermes_session.py
      measure-path         (one-line locator → checkout's measure.py)

``hermes_install.py`` copies the hermes/ payload (this file, plugin.yaml,
README.md) AND the three runtime modules above into that directory. Earlier
installs copied only the payload, leaving the imports below broken. It does NOT
copy measure.py itself — that would silently drift on update — and instead writes
the ``measure-path`` locator naming the canonical measure.py in the checkout;
hermes_hook_bridge reads it to shell into measure.py.

At import time we add the plugin directory itself (``_PLUGIN_DIR``) to
``sys.path`` so the three sibling modules resolve correctly, whether the plugin
is loaded from the install tree OR from the repo checkout (where scripts/ is the
parent of all four files).  We append it so Hermes core modules keep import
precedence. Host configuration is probed lazily only to avoid competing with
Hermes's native compressor; every host import is wrapped fail-open.

Activation: Hermes (v0.15.x) does NOT auto-discover plugins by directory
presence — the plugin must be allow-listed in the Hermes config under
``plugins.enabled``. ``hermes_install.py --enable`` patches that automatically;
otherwise the installer prints the snippet to add.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolve the plugin directory and make sibling modules importable.
#
# Strategy: We add the directory that contains THIS file to sys.path.
# This works both from the install tree (~/.hermes/plugins/token-optimizer/)
# and from the repo checkout (skills/token-optimizer/scripts/ is already on
# sys.path via the test bootstrap). Keep it at the end so Hermes core modules
# keep import precedence.
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).parent.resolve()
_PLUGIN_DIR_STR = str(_PLUGIN_DIR)
sys.path[:] = [p for p in sys.path if p != _PLUGIN_DIR_STR]
sys.path.append(_PLUGIN_DIR_STR)

# Lazy import so the plugin loads even if hermes_hook_bridge is not available
# in the install tree at module-load time.  We cache ONLY on success so a
# transient ImportError (bridge not yet copied during install) is retried on
# the next hook call rather than permanently frozen as None by lru_cache.
_BRIDGE_SENTINEL = object()  # distinct from None: "not yet resolved"
_bridge_cache: Any = _BRIDGE_SENTINEL


def _import_bridge():
    global _bridge_cache
    if _bridge_cache is not _BRIDGE_SENTINEL:
        return _bridge_cache
    try:
        import hermes_hook_bridge as _bridge  # noqa: PLC0415
        _bridge_cache = _bridge  # cache only on success
        return _bridge
    except Exception as exc:
        logger.debug("[token-optimizer] hermes_hook_bridge not available: %s", exc)
        # Do NOT cache None — allow retry on next call.
        return None


# Same lazy-cache pattern as _import_bridge, but the state reader is loaded by
# file path, not by name: Hermes ships its own ``hermes_state`` module, so a
# bare ``import hermes_state`` inside the host can bind that one instead of
# TO's read-only reader — the compression-health probe would then read foreign
# data (or fail) and the gate would silently stay in observer mode.
_STATE_SENTINEL = object()  # distinct from None: "not yet resolved"
_state_cache: Any = _STATE_SENTINEL

# Candidate locations for TO's hermes_state.py: the installed plugin dir (the
# installer copies it next to this file) and the repo checkout's scripts dir.
_STATE_CANDIDATES = (
    _PLUGIN_DIR / "hermes_state.py",
    _PLUGIN_DIR.parent / "skills" / "token-optimizer" / "scripts" / "hermes_state.py",
)


def _import_state():
    """Return TO's hermes_state module, loaded by explicit file path."""
    global _state_cache
    if _state_cache is not _STATE_SENTINEL:
        return _state_cache
    try:
        path = next((p for p in _STATE_CANDIDATES if p.is_file()), None)
        if path is None:
            return None
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location("to_hermes_state", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _state_cache = module  # cache only on success
        return module
    except Exception as exc:
        logger.debug("[token-optimizer] hermes_state not available: %s", exc)
        # Do NOT cache None — allow retry on next call.
        return None


# ---------------------------------------------------------------------------
# Per-session token accumulation (thread-safe, in-process)
#
# _TALLY:   session_id -> {"input": int, "output": int, "cache_read": int,
#                          "cache_write": int, "reasoning": int}
# _NUDGED:  session_id -> True once the above-threshold nudge has fired.
#           Once set, the gate never re-arms within the session: a later drop
#           below the threshold and a second crossing stay silent. Cleared
#           when the session ends (finalize, or the CLI exit-path
#           on_session_end), never at the end of an ordinary turn.
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_TALLY: dict[str, dict[str, int]] = {}
_NUDGED: set[str] = set()
# H4: tracks sessions already rolled up this process lifetime so
# on_session_finalize + on_session_end don't both spawn a rollup subprocess.
_ROLLED_UP: set[str] = set()

# ---------------------------------------------------------------------------
# Parity state (all guarded by _LOCK, all bounded)
#
# _TASK_SESSION: task_id -> session_id. transform_terminal_output carries only
#                task_id; pre_tool_call carries both, so we learn the mapping.
# _PENDING:      session_id -> notes waiting for the next tool result or turn
#                (post-compaction restore, subagent sprawl).
# _RESTORE_DUE:  session_id -> session id whose checkpoint should be restored
#                at the next delivery point (set by post_compaction).
# _CALL_NOTES:   tool_call_id -> notes to attach to that call's result (a read
#                that was allowed but came with extra context).
# _BANDS:        session_id -> progressive fill bands already checkpointed.
# _EDITS:        session_id -> edit-batch counters.
# _CKPT_AT:      (session_id, trigger) -> last spawn time, for cooldowns.
# _ONCE:         (session_id, key) one-shot guards.
# _SUBAGENTS:    parent session_id -> open child count.
# ---------------------------------------------------------------------------

_TASK_SESSION: dict[str, str] = {}
_PENDING: dict[str, list[str]] = {}
_RESTORE_DUE: dict[str, str] = {}
_CALL_NOTES: dict[str, list[str]] = {}
_BANDS: dict[str, set[int]] = {}
_EDITS: dict[str, dict[str, Any]] = {}
_CKPT_AT: dict[tuple[str, str], float] = {}
_ONCE: dict[tuple[str, str], bool] = {}
_SUBAGENTS: dict[str, int] = {}
_VERBOSITY: dict[str, dict[str, float]] = {}
_SKILLS: dict[str, dict[str, int]] = {}
_LAST_SESSION = {"id": ""}

_MAX_TRACKED = 512            # cap on every per-key map above
_CHECKPOINT_COOLDOWN_S = 90   # mirrors measure._CHECKPOINT_COOLDOWN_SECONDS
_STOP_FAILURE_COOLDOWN_S = 300
_SUBAGENT_SPRAWL_THRESHOLD = 8  # mirrors codex_hook_bridge._SUBAGENT_SPRAWL_THRESHOLD
_PRE_TOOL_TIMEOUT_S = 3.0     # pre_tool_call FAILS CLOSED on timeout: keep well under it
_POST_TOOL_TIMEOUT_S = 8.0
_CONTEXT_TIMEOUT_S = 10.0
_VERBOSITY_MAX_NUDGES = 3       # mirrors measure._VERBOSITY_MAX_NUDGES
_VERBOSITY_COOLDOWN_S = 300     # mirrors measure._VERBOSITY_COOLDOWN_SEC

# Hermes tools whose output already went through transform_terminal_output.
_TERMINAL_TOOLS = frozenset({"terminal", "process_manage"})
_EDIT_TOOLS = frozenset({"write_file", "patch"})


def _enabled(name: str) -> bool:
    """Feature switch: TOKEN_OPTIMIZER_HERMES_<NAME>=0 turns one feature off."""
    import os  # noqa: PLC0415

    return os.environ.get(f"TOKEN_OPTIMIZER_HERMES_{name}", "").strip() != "0"


def _bounded_set(mapping: dict, key: Any, value: Any) -> None:
    """Insert into a dict, dropping the oldest entries past _MAX_TRACKED."""
    mapping[key] = value
    while len(mapping) > _MAX_TRACKED:
        mapping.pop(next(iter(mapping)))


def _note_session(session_id: str) -> None:
    if session_id:
        _LAST_SESSION["id"] = session_id


def _session_for_task(task_id: str) -> str:
    with _LOCK:
        sid = _TASK_SESSION.get(task_id or "")
        return sid or task_id or _LAST_SESSION["id"]


def _add_pending(session_id: str, note: str) -> None:
    if not session_id or not note:
        return
    with _LOCK:
        notes = _PENDING.setdefault(session_id, [])
        if note not in notes:
            notes.append(note)
        while len(_PENDING) > _MAX_TRACKED:
            _PENDING.pop(next(iter(_PENDING)))


def _take_pending(session_id: str) -> list[str]:
    """Pop every note waiting for this session, resolving a due restore first."""
    if not session_id:
        return []
    with _LOCK:
        restore_sid = _RESTORE_DUE.pop(session_id, None)
    if restore_sid and _enabled("RESTORE"):
        bridge = _import_bridge()
        if bridge is not None:
            try:
                text = bridge.run_measure_context(
                    ["compact-restore", "--compact"],
                    {"session_id": restore_sid, "hook_event_name": "SessionStart", "source": "compact"},
                    timeout=_CONTEXT_TIMEOUT_S,
                )
            except Exception:
                text = ""
            if text:
                _add_pending(session_id, text)
    with _LOCK:
        return _PENDING.pop(session_id, [])


def _attach_notes(result: str, notes: list[str]) -> str:
    """Attach notes to a tool result without breaking a JSON result."""
    notes = [n for n in notes if n]
    if not notes:
        return result
    text = "\n\n".join(notes)
    stripped = (result or "").strip()
    if stripped.startswith("{"):
        try:
            import json  # noqa: PLC0415

            obj = json.loads(stripped)
            if isinstance(obj, dict):
                obj["token_optimizer"] = text
                return json.dumps(obj, ensure_ascii=False)
        except ValueError:
            pass
    return f"{result}\n\n{text}" if result else text


def _forget_session(session_id: str) -> None:
    """Drop every per-session entry (finalize, reset, delete)."""
    if not session_id:
        return
    with _LOCK:
        _TALLY.pop(session_id, None)
        _NUDGED.discard(session_id)
        _PENDING.pop(session_id, None)
        _RESTORE_DUE.pop(session_id, None)
        _BANDS.pop(session_id, None)
        _EDITS.pop(session_id, None)
        _SUBAGENTS.pop(session_id, None)
        _VERBOSITY.pop(session_id, None)
        _SKILLS.pop(session_id, None)
        for key in [k for k in _CKPT_AT if k[0] == session_id]:
            _CKPT_AT.pop(key, None)
        for key in [k for k in _ONCE if k[0] == session_id]:
            _ONCE.pop(key, None)
        for task in [t for t, s in _TASK_SESSION.items() if s == session_id]:
            _TASK_SESSION.pop(task, None)


def _maybe_checkpoint(
    session_id: str,
    trigger: str,
    *,
    fill_pct: float | None = None,
    cooldown: float = 0.0,
    once: bool = False,
) -> bool:
    """Spawn a background checkpoint unless a cooldown or one-shot guard says no."""
    if not session_id or not _enabled("CHECKPOINTS"):
        return False
    now = time.time()
    key = (session_id, trigger)
    with _LOCK:
        if once and key in _ONCE:
            return False
        if cooldown and now - _CKPT_AT.get(key, 0.0) < cooldown:
            return False
        _bounded_set(_CKPT_AT, key, now)
        if once:
            _bounded_set(_ONCE, key, True)
    bridge = _import_bridge()
    if bridge is None:
        return False
    try:
        return bool(bridge.spawn_checkpoint(session_id, trigger, fill_pct=fill_pct))
    except Exception as exc:
        logger.debug("[token-optimizer] checkpoint spawn failed: %s", exc)
        return False

# ---------------------------------------------------------------------------
# Nudge configuration
#
# Threshold: fire when estimated context fill exceeds 70% of the model window.
# Default context window: 200 000 tokens (conservative; covers Claude 3/4).
# ---------------------------------------------------------------------------

_NUDGE_THRESHOLD = 0.70
_DEFAULT_CONTEXT_WINDOW = 200_000

# Dashboard port — single source of truth lives in hermes_doctor.DASHBOARD_PORT.
try:
    from hermes_doctor import DASHBOARD_PORT as _DASHBOARD_PORT  # noqa: PLC0415
except Exception:
    _DASHBOARD_PORT = 24844


def _context_window(model: str) -> int:
    """Delegate to hermes_session's single source of truth for model windows."""
    try:
        from hermes_session import context_window_for_model  # noqa: PLC0415
        return context_window_for_model(model or "")
    except Exception:
        return _DEFAULT_CONTEXT_WINDOW


def _estimate_fill_from_history(conversation_history: list[Any]) -> int:
    """Rough token estimate from conversation_history when tally is empty.

    We count total characters across all message content strings and divide by
    the calibrated ~3.3 chars-per-token ratio (matching token_estimate.py, vs the
    old 4 which undercounts ~15-20%).  This is purely a fallback for the first
    turn before post_api_request has accumulated any real usage.
    """
    chars = 0
    for msg in (conversation_history or []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    chars += len(str(part.get("text") or ""))
    return int(chars / 3.3)



def _native_compression_needs_help(session_id: str) -> bool:
    """True only when Token Optimizer should intervene in Hermes compression.

    Hermes owns context compression. We stay silent while its native compressor
    is enabled and healthy, avoiding a competing threshold/policy. A nudge is
    useful when compression is explicitly disabled or its persisted session
    health says the compressor failed or was ineffective. Any probe failure
    defaults to silent observer mode.
    """
    try:
        from hermes_cli.config import load_config_readonly  # noqa: PLC0415
        cfg = load_config_readonly() or {}
        # The config spells the switch two ways: a table
        # (``compression.enabled = false``) or a flat ``compression = false``.
        compression = cfg.get("compression")
        enabled = (
            bool(compression.get("enabled", True))
            if isinstance(compression, dict)
            else (True if compression is None else bool(compression))
        )
        if not enabled:
            return True
    except Exception:
        # Config unreadable: keep going — the persisted health probe below can
        # still see a failing compressor even when the config can't be read.
        pass
    try:
        state = _import_state()
        if state is None:
            return False
        # live=True: the probe runs inside the live host, so it must see
        # committed WAL writes, not just the last checkpoint.
        row = state.get_session(session_id, live=True) or {}
        now = time.time()

        # Hermes keeps the last failure text after its cooldown expires. Treat
        # the failure as current only while that cooldown is live; otherwise a
        # single historical provider error would make TO compete forever.
        failure_deadline = float(row.get("compression_failure_cooldown_until") or 0.0)
        if failure_deadline > now:
            return True

        # Fallback/ineffective counters are strikes, not a permanent health
        # verdict. Hermes trips at two strikes and arms a recovery window
        # lazily. A zero deadline with tripped counters is a current, not-yet-
        # armed failure epoch. Once an armed deadline expires Hermes permits a
        # probation probe, so TO must stand down even if the durable counters
        # have not yet been lowered by that next evaluation.
        fallback_streak = int(row.get("compression_fallback_streak") or 0)
        ineffective_count = int(row.get("compression_ineffective_count") or 0)
        recovery_deadline = float(row.get("compression_recovery_deadline") or 0.0)
        tripped = fallback_streak >= 2 or ineffective_count >= 2
        return tripped and (recovery_deadline <= 0.0 or recovery_deadline > now)
    except Exception:
        return False

def _quality_grade(fill_ratio: float, message_count: int, model: str = "", ctx_win: int = 0) -> str:
    """Grade from fill and message count for the nudge line.

    Q1: Delegates to compute_quality_score so the nudge grade matches the
    stored quality_grade in session_log (same function, same thresholds).
    Falls back to an inline approximation if hermes_session is unavailable.
    """
    try:
        from hermes_session import compute_quality_score as _cqs  # noqa: PLC0415
        # Reconstruct approximate input_tokens from fill_ratio and ctx_win.
        window = ctx_win if ctx_win > 0 else _DEFAULT_CONTEXT_WINDOW
        approx_input = int(fill_ratio * window)
        result = _cqs(
            input_tokens=approx_input,
            output_tokens=0,
            message_count=message_count,
            model=model or "",
            context_window=window,
            # The nudge's fill IS a live occupancy reading — pass it so the
            # grade matches what the rollup stores for the same session.
            context_tokens=approx_input,
        )
        return result["grade"]
    except Exception:
        # Inline fallback: coarser but never crashes the nudge path.
        if fill_ratio < 0.30 and message_count <= 20:
            return "S"
        if fill_ratio < 0.50 and message_count <= 40:
            return "A"
        if fill_ratio < 0.70 and message_count <= 60:
            return "B"
        if fill_ratio < 0.85 and message_count <= 100:
            return "C"
        if fill_ratio < 0.95:
            return "D"
        return "F"


# ---------------------------------------------------------------------------
# Hook: post_api_request — accumulate per-turn token usage
# ---------------------------------------------------------------------------

def on_post_api_request(**kwargs: Any) -> None:
    """Accumulate per-turn token usage into the session-level running tally.

    Observer-only; never returns a value, never raises.
    kwargs.get("usage") is a dict with keys from CanonicalUsage.asdict():
      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
      reasoning_tokens, request_count, prompt_tokens, total_tokens.
    """
    try:
        session_id: str = kwargs.get("session_id") or ""
        if not session_id:
            return
        usage = kwargs.get("usage") or {}
        if not isinstance(usage, dict):
            return

        delta = {
            "input":       int(usage.get("input_tokens", 0) or 0),
            "output":      int(usage.get("output_tokens", 0) or 0),
            "cache_read":  int(usage.get("cache_read_tokens", 0) or 0),
            "cache_write": int(usage.get("cache_write_tokens", 0) or 0),
            "reasoning":   int(usage.get("reasoning_tokens", 0) or 0),
        }
        with _LOCK:
            tally = _TALLY.setdefault(session_id, {
                "input": 0, "output": 0,
                "cache_read": 0, "cache_write": 0, "reasoning": 0,
            })
            for k, v in delta.items():
                tally[k] += v
            # LIVE context size = the prompt THIS call actually sent.
            # `input` above is session-CUMULATIVE: a host re-sends the whole conversation every
            # turn, so that sum climbs past the model window even while real occupancy is low
            # (measured on Hermes: cumulative 1.28M against its own reported 278,545 / 1,000,000
            # for the same session). Never use it to judge fill. Every prompt token occupies the
            # window, so we want the full prompt = input + cache_read + cache_write. Prefer the
            # host's own CanonicalUsage.prompt_tokens when present (drift-proof: it already sums
            # those three); fall back to summing the components. Dropping cache_write would
            # undercount the fill on a cache-creation turn (cached tokens are still in the window).
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            if prompt <= 0:
                prompt = delta["input"] + delta["cache_read"] + delta["cache_write"]
            # Only overwrite when positive: an errored/retried request can report empty/zero
            # usage, and clobbering a good reading with 0 would make on_pre_llm_call treat the
            # next turn as "first turn" (current_input <= 0) and fall back to the history
            # estimate, so a genuinely ~full window would go unwarned.
            if prompt > 0:
                tally["last_prompt"] = prompt
        _note_session(session_id)
        if prompt > 0:
            _maybe_progressive_checkpoint(session_id, prompt, str(kwargs.get("model") or ""))
    except Exception as exc:
        logger.debug("[token-optimizer] post_api_request accumulation error: %s", exc)


def _progressive_bands() -> tuple[int, ...]:
    try:
        from hermes_session import PROGRESSIVE_BANDS  # noqa: PLC0415
        return tuple(PROGRESSIVE_BANDS)
    except Exception:
        return (20, 35, 50, 65, 80)


def _maybe_progressive_checkpoint(session_id: str, prompt_tokens: int, model: str) -> None:
    """Checkpoint once per fill band (20/35/50/65/80%), like Claude Code.

    Fill is the LIVE prompt size of the last request against the model window,
    never the cumulative tally. Only the highest newly crossed band is taken.
    """
    ctx_win = _context_window(model)
    if ctx_win <= 0:
        return
    fill_pct = 100.0 * prompt_tokens / ctx_win
    with _LOCK:
        captured = _BANDS.setdefault(session_id, set())
        while len(_BANDS) > _MAX_TRACKED:
            _BANDS.pop(next(iter(_BANDS)))
        target = None
        for band in sorted(_progressive_bands(), reverse=True):
            if fill_pct >= band and band not in captured:
                target = band
                break
        if target is None:
            return
        # Lower bands are implied by the higher one: never backfill them.
        captured.update(b for b in _progressive_bands() if b <= target)
    _maybe_checkpoint(session_id, f"progressive-{target}", fill_pct=min(fill_pct, 100.0))


# ---------------------------------------------------------------------------
# Hook: pre_llm_call — THE NUDGE
# ---------------------------------------------------------------------------

def on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    """Per-turn context for the model, joined into one injection.

    In order: notes waiting for this session (post-compaction restore,
    subagent sprawl), the new-session checkpoint pointer on the first turn,
    a topic-matched prior-session hint for this prompt (the same continuity
    engine Claude Code's UserPromptSubmit hook runs), and the fill nudge.
    Returns None when there is nothing to say. Never raises.
    """
    parts: list[str] = []
    try:
        session_id: str = kwargs.get("session_id") or ""
        _note_session(session_id)
        parts.extend(_take_pending(session_id))
        history = kwargs.get("conversation_history")
        parts.extend(_continuity_context(
            session_id,
            kwargs.get("user_message"),
            bool(kwargs.get("is_first_turn")),
            model=str(kwargs.get("model") or ""),
            message_count=len(history) if isinstance(history, list) else 0,
        ))
    except Exception as exc:
        logger.debug("[token-optimizer] pre_llm_call continuity error: %s", exc)
    nudge = _fill_nudge(**kwargs)
    if nudge:
        parts.append(nudge["context"])
    parts = [p for p in parts if p]
    if not parts:
        return None
    return {"context": "\n\n".join(parts)}


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "\n".join(
            str(p.get("text") or "") for p in message
            if isinstance(p, dict) and p.get("type") in (None, "text")
        )
    return ""


def _continuity_context(
    session_id: str,
    user_message: Any,
    first_turn: bool,
    *,
    model: str = "",
    message_count: int = 0,
) -> list[str]:
    """New-session pointer, prompt-matched prior-session hint, lean-output nudge.

    One child process per turn (measure.py hermes-turn-context), the same
    engines Claude Code's SessionStart / UserPromptSubmit hooks run.
    """
    if not session_id:
        return []
    continuity = _enabled("CONTINUITY")
    prompt = _message_text(user_message).strip() if continuity else ""
    payload: dict[str, Any] = {
        "session_id": session_id,
        "prompt": prompt[:4000],
        "first_turn": bool(first_turn and continuity),
    }
    lean = _lean_output_reading(session_id, model, message_count)
    if lean is not None:
        payload["fill_pct"], payload["score"], payload["context_window"] = lean
    if not payload["prompt"] and not payload["first_turn"] and lean is None:
        return []
    bridge = _import_bridge()
    if bridge is None:
        return []
    res = bridge.run_turn_context(payload, timeout=_CONTEXT_TIMEOUT_S) or {}
    if res.get("verbosity"):
        with _LOCK:
            state = _VERBOSITY.setdefault(session_id, {"count": 0, "last": 0.0})
            while len(_VERBOSITY) > _MAX_TRACKED:
                _VERBOSITY.pop(next(iter(_VERBOSITY)))
            state["count"] += 1
            state["last"] = time.time()
    parts = res.get("parts") or []
    return [p for p in parts if isinstance(p, str) and p.strip()]


def _lean_output_reading(session_id: str, model: str, message_count: int):
    """(fill %, quality score, model window) for the lean-output nudge, or None.

    None when the feature is off, when this session already had its nudges
    (max 3, 5 minutes apart, like Claude Code), or before the first real
    prompt-size reading.
    """
    if not _enabled("VERBOSITY"):
        return None
    with _LOCK:
        prompt = int((_TALLY.get(session_id) or {}).get("last_prompt", 0) or 0)
        state = dict(_VERBOSITY.get(session_id) or {})
    if prompt <= 0:
        return None
    if int(state.get("count", 0)) >= _VERBOSITY_MAX_NUDGES:
        return None
    if time.time() - float(state.get("last", 0.0)) < _VERBOSITY_COOLDOWN_S:
        return None
    ctx_win = _context_window(model)
    if ctx_win <= 0:
        return None
    fill_ratio = prompt / ctx_win
    try:
        from hermes_session import compute_quality_score  # noqa: PLC0415

        score = float(compute_quality_score(
            input_tokens=prompt, output_tokens=0, message_count=message_count,
            model=model or "", context_window=ctx_win, context_tokens=prompt,
        )["score"])
    except Exception:
        score = 100.0
    return round(min(fill_ratio * 100.0, 100.0), 1), score, int(ctx_win)


def _fill_nudge(**kwargs: Any) -> dict[str, str] | None:
    """Inject a context nudge when fill crosses the threshold.

    Returns {"context": "<nudge text>"} to append to the user message.
    Returns None to stay silent.

    Gating rules:
    - Fires when fill >= _NUDGE_THRESHOLD (0.70; the threshold is inclusive).
    - Once per session, never re-arms: after the first nudge fires for a
      session, subsequent calls within the same session do NOT re-inject,
      even if fill drops back below the threshold and rises again (avoids
      spam). The gate clears on on_session_finalize/on_session_end so the
      next session starts clean.
    """
    try:
        session_id: str = kwargs.get("session_id") or ""
        model: str = kwargs.get("model") or ""
        conversation_history = kwargs.get("conversation_history") or []
        message_count = len(conversation_history) if isinstance(conversation_history, list) else 0

        with _LOCK:
            already_nudged = session_id in _NUDGED
            tally = dict(_TALLY.get(session_id) or {})

        if already_nudged:
            return None

        # Determine LIVE context fill.
        # Use the LAST request's prompt size, never the session-cumulative `input` tally: every
        # turn re-sends the whole conversation, so the cumulative figure climbs past the model
        # window regardless of how small the live context is (measured on Hermes: cumulative
        # 1,285,803 against its own 278,545 / 1,000,000 = 28% for the same session, which made the
        # nudge report "~100% full, Grade: F" while the host showed 28%).
        current_input = int(tally.get("last_prompt", 0) or 0)
        if current_input <= 0:
            # No completed call yet (first turn): estimate from history length.
            current_input = _estimate_fill_from_history(conversation_history)

        ctx_win = _context_window(model)
        fill = current_input / ctx_win if ctx_win > 0 else 0.0

        if fill < _NUDGE_THRESHOLD:
            # Threshold is inclusive: fill >= 0.70 triggers the nudge.
            return None
        if not _native_compression_needs_help(session_id):
            # Hermes already owns compression. Do not inject a second policy
            # while the native compressor is enabled and healthy.
            return None

        # Compute grade via compute_quality_score for consistency with stored grade (Q1).
        grade = _quality_grade(fill, message_count, model=model, ctx_win=ctx_win)
        # Cap the displayed percentage at 100: ctx_win is an ASSUMED window
        # (Hermes does not expose the live context window to the hook), so a
        # larger-window model could otherwise show an absurd >100% figure.
        # Phrase as an estimate against an assumed window so we never overstate
        # an exact fill number.
        fill_pct = min(100, int(fill * 100))
        tip = (
            "Consider /compact to free context."
            if fill >= 0.85
            else "Avoid adding large files; prefer targeted reads."
        )
        nudge = (
            f"[Token Optimizer] Context ~{fill_pct}% full "
            f"(last request prompt ~{current_input:,} tokens vs model window {ctx_win:,}) "
            f"Grade: {grade}. {tip}"
        )

        with _LOCK:
            _NUDGED.add(session_id)

        return {"context": nudge}
    except Exception as exc:
        logger.debug("[token-optimizer] pre_llm_call nudge error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Hooks: on_session_end (per turn) / on_session_finalize (session end)
#
# Hermes fires on_session_end at the end of EVERY turn (agent/turn_finalizer.py,
# canonical payload carries turn_id), plus a reduced exit-path shape when the
# CLI quits mid-turn. on_session_finalize fires once at teardown. So:
#   per-turn on_session_end  -> Claude Code's Stop: a "stop" checkpoint
#   finalize / exit-path end -> Claude Code's SessionEnd: "end" checkpoint,
#                               rollup into trends.db, dashboard refresh,
#                               then drop the session's in-memory state.
# Earlier builds rolled up on the first per-turn on_session_end, which froze the
# session at its first turn and reset the once-per-session nudge every turn.
# ---------------------------------------------------------------------------

def _do_rollup(session_id: str, platform: str, reason: str) -> None:
    """Read the final sessions row and write a rollup via the bridge.

    H4: guards against double-rollup when both on_session_finalize and
    on_session_end fire for the same session.  The first caller claims the
    slot; the second is a no-op (bridge uses INSERT OR IGNORE anyway, but
    avoiding a second subprocess is cleaner and cheaper).

    Async-safe, never raises into the host.
    """
    if not session_id:
        return
    with _LOCK:
        if session_id in _ROLLED_UP:
            logger.debug("[token-optimizer] rollup already fired for %s, skipping", session_id)
            return
        _ROLLED_UP.add(session_id)
    with _LOCK:
        context_tokens = int((_TALLY.get(session_id) or {}).get("last_prompt", 0) or 0)
        skills = dict(_SKILLS.get(session_id) or {})
    _maybe_checkpoint(session_id, "end")
    bridge = _import_bridge()
    if bridge is None:
        logger.debug("[token-optimizer] bridge unavailable, skipping rollup for %s", session_id)
    else:
        try:
            if skills:
                bridge.run_rollup(session_id=session_id, platform=platform, reason=reason,
                                  context_tokens=context_tokens or None, skills=skills)
            else:
                bridge.run_rollup(session_id=session_id, platform=platform, reason=reason,
                                  context_tokens=context_tokens or None)
        except Exception as exc:
            logger.debug("[token-optimizer] rollup error for %s: %s", session_id, exc)
    # Clear per-session state regardless of rollup outcome.
    _forget_session(session_id)


def on_session_finalize(**kwargs: Any) -> None:
    """Handle on_session_finalize: session_id, platform, reason."""
    try:
        session_id: str = kwargs.get("session_id") or ""
        platform: str = kwargs.get("platform") or "hermes"
        reason: str = kwargs.get("reason") or ""
        _do_rollup(session_id, platform, reason)
    except Exception as exc:
        logger.debug("[token-optimizer] on_session_finalize error: %s", exc)


def on_session_end(**kwargs: Any) -> None:
    """Handle on_session_end.

    Canonical per-turn payload (has ``turn_id``): checkpoint the turn, like
    Claude Code's Stop hook, and keep the session's state. Exit-path payload
    (no ``turn_id``; the CLI quit mid-turn): treat as session end. The H4 guard
    in _do_rollup keeps finalize + exit-path end down to one rollup.
    """
    try:
        session_id: str = kwargs.get("session_id") or ""
        if not session_id:
            return
        if kwargs.get("turn_id"):
            _maybe_checkpoint(session_id, "stop", cooldown=_CHECKPOINT_COOLDOWN_S)
            return
        platform: str = kwargs.get("platform") or "hermes"
        reason: str = kwargs.get("reason") or ""
        _do_rollup(session_id, platform, reason)
    except Exception as exc:
        logger.debug("[token-optimizer] on_session_end error: %s", exc)


# ---------------------------------------------------------------------------
# Tool hooks: pre_tool_call / transform_terminal_output / transform_tool_result
# / post_tool_call
# ---------------------------------------------------------------------------

# Claude Code tool names the post-tool engines act on (see hermes_tool_hooks).
_POST_ENGINE_TOOLS = frozenset({"Read", "Edit", "Write", "Grep", "Glob", "Agent"})
_FAILED_STATUSES = frozenset({"error", "blocked"})
_hook_timeout_cache: dict[str, float] = {}


def _host_hook_timeout() -> float:
    """Hermes's plugins.hook_callback_timeout (seconds; 0 = unbounded)."""
    if "value" in _hook_timeout_cache:
        return _hook_timeout_cache["value"]
    value = 30.0
    try:
        from hermes_cli.config import load_config_readonly  # noqa: PLC0415
        cfg = load_config_readonly() or {}
        plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
        raw = plugins.get("hook_callback_timeout") if isinstance(plugins, dict) else None
        if raw is not None:
            value = float(raw)
    except Exception:
        pass
    _hook_timeout_cache["value"] = value
    return value


def _pre_tool_timeout() -> float:
    """Child-process budget for pre_tool_call.

    Hermes BLOCKS the tool when a pre_tool_call callback outlives
    plugins.hook_callback_timeout, so stay well inside it.
    """
    host = _host_hook_timeout()
    if host and host > 0:
        return max(0.2, min(_PRE_TOOL_TIMEOUT_S, host * 0.4))
    return _PRE_TOOL_TIMEOUT_S


def _post_tool_timeout() -> float:
    host = _host_hook_timeout()
    if host and host > 0:
        return max(0.5, min(_POST_TOOL_TIMEOUT_S, host * 0.5))
    return _POST_TOOL_TIMEOUT_S


def _str_notes(res: Any) -> list[str]:
    notes = res.get("notes") if isinstance(res, dict) else None
    return [n for n in (notes or []) if isinstance(n, str) and n.strip()]


def on_pre_tool_call(**kwargs: Any) -> dict[str, str] | None:
    """Re-read savings and the MCP re-fetch guard, before the tool runs.

    - read_file: the Claude Code read cache. An unchanged re-read is answered
      with a structure map, a re-read after an edit with only the diff.
    - mcp__*: an exact repeat of an archived MCP call is redirected to the
      saved copy.
    - delegate_task: checkpoint once before the first subagent fan-out.

    Returns {"action": "block", "message": ...} to answer from cache, else
    None. Fails OPEN: any problem returns None and the tool runs normally.
    """
    try:
        tool_name = str(kwargs.get("tool_name") or "")
        session_id = str(kwargs.get("session_id") or "")
        task_id = str(kwargs.get("task_id") or "")
        call_id = str(kwargs.get("tool_call_id") or "")
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        if not session_id:
            return None
        _note_session(session_id)
        with _LOCK:
            _bounded_set(_TASK_SESSION, task_id, session_id)
        if tool_name == "delegate_task":
            _maybe_checkpoint(session_id, "milestone-pre-fanout", once=True)
            return None
        if tool_name == "read_file":
            if not _enabled("READ_CACHE"):
                return None
            # Hermes resolves a relative path against the session's working
            # directory, which this process cannot see. Resolving it here could
            # answer with a different file's map, so only absolute paths (and
            # ~/...) use the read cache.
            raw_path = args.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                return None
            import os  # noqa: PLC0415

            full_path = os.path.expanduser(raw_path.strip())
            if not os.path.isabs(full_path):
                return None
            args = {**args, "path": full_path}
        elif tool_name.startswith("mcp__"):
            if not _enabled("REFETCH_GUARD"):
                return None
        else:
            return None
        bridge = _import_bridge()
        if bridge is None:
            return None
        for payload in bridge.to_claude_payload(tool_name, args, session_id=session_id, tool_call_id=call_id):
            res = bridge.run_tool_hooks("pre-tool", payload, timeout=_pre_tool_timeout())
            if not res:
                continue
            notes = _str_notes(res)
            block = res.get("block")
            if isinstance(block, str) and block.strip():
                return {"action": "block", "message": "\n\n".join([block.strip(), *notes])}
            if notes and call_id:
                with _LOCK:
                    _bounded_set(_CALL_NOTES, call_id, notes)
        return None
    except Exception as exc:
        logger.debug("[token-optimizer] pre_tool_call error: %s", exc)
        return None


def on_transform_terminal_output(**kwargs: Any) -> str | None:
    """Compress terminal output with the Claude Code bash pipeline.

    Same engine as Claude Code's PostToolUse Bash hook: pattern-aware
    compression (git, find, tests, builds, logs, lint, JSON, ...), cross-turn
    dedup, loop detection nudges, full original archived behind an expand
    pointer, savings logged. Failed commands and error output pass through
    whole. Returns the replacement text or None to keep the original.
    """
    try:
        if not _enabled("TERMINAL"):
            return None
        command = kwargs.get("command")
        output = kwargs.get("output")
        if not isinstance(command, str) or not command.strip() or not isinstance(output, str):
            return None
        session_id = _session_for_task(str(kwargs.get("task_id") or ""))
        if not session_id:
            return None
        bridge = _import_bridge()
        if bridge is None:
            return None
        response: dict[str, Any] = {"stdout": output, "stderr": "", "interrupted": False, "isImage": False}
        rc = kwargs.get("returncode")
        if isinstance(rc, int) and not isinstance(rc, bool):
            response["exit_code"] = rc
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": response,
            "session_id": session_id,
            "tool_use_id": "",
        }
        res = bridge.run_tool_hooks("post-tool", payload, timeout=_post_tool_timeout()) or {}
        replacement = res.get("replacement")
        notes = _str_notes(res) + _take_pending(session_id)
        if not isinstance(replacement, str) and not notes:
            return None
        base = replacement if isinstance(replacement, str) else output
        return base + ("\n\n" + "\n\n".join(notes) if notes else "")
    except Exception as exc:
        logger.debug("[token-optimizer] transform_terminal_output error: %s", exc)
        return None


def on_transform_tool_result(**kwargs: Any) -> str | None:
    """Archive / trim large tool results and attach pending notes.

    Same engines as Claude Code's PostToolUse runner for every non-terminal
    tool: large MCP results become a preview plus an expand pointer (the full
    result is archived), reads/searches/agent results are archived for
    recovery, activity and decisions are tracked, and the read cache is
    refreshed after write_file / patch. Notes waiting for the session (a read
    that came with extra context, a post-compaction restore) ride along.
    """
    try:
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name in _TERMINAL_TOOLS:
            return None
        result = kwargs.get("result")
        if not isinstance(result, str):
            return None
        session_id = str(kwargs.get("session_id") or "") or _session_for_task(str(kwargs.get("task_id") or ""))
        call_id = str(kwargs.get("tool_call_id") or "")
        with _LOCK:
            notes = list(_CALL_NOTES.pop(call_id, [])) if call_id else []
        replacement = None
        status = str(kwargs.get("status") or "").lower()
        if session_id and status not in _FAILED_STATUSES and _enabled("TOOL_RESULTS"):
            bridge = _import_bridge()
            args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
            payloads = bridge.to_claude_payload(
                tool_name, args, session_id=session_id, tool_call_id=call_id, result=result,
            ) if bridge is not None else []
            for payload in payloads:
                cc_name = payload.get("tool_name") or ""
                if cc_name not in _POST_ENGINE_TOOLS and not cc_name.startswith("mcp__"):
                    continue
                res = bridge.run_tool_hooks("post-tool", payload, timeout=_post_tool_timeout()) or {}
                if isinstance(res.get("replacement"), str):
                    replacement = res["replacement"]
                notes.extend(_str_notes(res))
        notes.extend(_take_pending(session_id))
        if replacement is None and not notes:
            return None
        return _attach_notes(replacement if replacement is not None else result, notes)
    except Exception as exc:
        logger.debug("[token-optimizer] transform_tool_result error: %s", exc)
        return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Edit-batch checkpoints: after 4 writes or 3 new files since the last one."""
    try:
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name not in _EDIT_TOOLS:
            return
        if str(kwargs.get("status") or "").lower() in _FAILED_STATUSES:
            return
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return
        try:
            from hermes_session import (  # noqa: PLC0415
                EDIT_BATCH_FILE_THRESHOLD as file_threshold,
                EDIT_BATCH_WRITE_THRESHOLD as write_threshold,
                hermes_tool_to_claude,
            )
            paths = [inp.get("file_path") for _, inp in hermes_tool_to_claude(tool_name, kwargs.get("args"))]
        except Exception:
            write_threshold, file_threshold, paths = 4, 3, []
        with _LOCK:
            edits = _EDITS.setdefault(session_id, {"writes": 0, "files": set(), "mark_writes": 0, "mark_files": 0})
            while len(_EDITS) > _MAX_TRACKED:
                _EDITS.pop(next(iter(_EDITS)))
            edits["writes"] += 1
            edits["files"].update(p for p in paths if p)
            due = (edits["writes"] - edits["mark_writes"] >= write_threshold
                   or len(edits["files"]) - edits["mark_files"] >= file_threshold)
        if due and _maybe_checkpoint(session_id, "milestone-edit-batch", cooldown=_CHECKPOINT_COOLDOWN_S):
            with _LOCK:
                edits["mark_writes"] = edits["writes"]
                edits["mark_files"] = len(edits["files"])
    except Exception as exc:
        logger.debug("[token-optimizer] post_tool_call error: %s", exc)


# ---------------------------------------------------------------------------
# Compaction, errors, subagents, session boundaries
# ---------------------------------------------------------------------------

def on_pre_auxiliary_call(**kwargs: Any) -> None:
    """Checkpoint right before Hermes compacts (Claude Code's PreCompact)."""
    try:
        if str(kwargs.get("aux_task") or "") != "compression":
            return
        session_id = str(kwargs.get("session_id") or "")
        # One per compaction: retries and fallbacks fire this once per attempt.
        _maybe_checkpoint(session_id, "auto", cooldown=60)
    except Exception as exc:
        logger.debug("[token-optimizer] pre_auxiliary_call error: %s", exc)


def on_post_compaction(**kwargs: Any) -> None:
    """After Hermes compacts: forget pre-compaction reads, queue a restore.

    The model no longer holds the files it read before the compaction, so the
    read cache must not answer the next read with "unchanged". The checkpoint
    restore (decisions, files, todos, next step) is delivered with the next
    tool result or user turn. When compaction rotated the session id, the
    restore reads the old session's checkpoints.
    """
    try:
        session_id = str(kwargs.get("session_id") or "")
        old_session_id = str(kwargs.get("old_session_id") or "")
        if not session_id:
            return
        bridge = _import_bridge()
        if bridge is not None and _enabled("READ_CACHE"):
            for sid in {session_id, old_session_id} - {""}:
                bridge.run_tool_hooks("clear-reads", {"session_id": sid}, timeout=_post_tool_timeout())
        with _LOCK:
            _bounded_set(_RESTORE_DUE, session_id, old_session_id or session_id)
    except Exception as exc:
        logger.debug("[token-optimizer] post_compaction error: %s", exc)


def on_api_request_error(**kwargs: Any) -> None:
    """A request failed for good: save a checkpoint (Claude Code's StopFailure)."""
    try:
        if kwargs.get("retryable") is not False:
            return
        session_id = str(kwargs.get("session_id") or "")
        _maybe_checkpoint(session_id, "stop-failure", cooldown=_STOP_FAILURE_COOLDOWN_S)
    except Exception as exc:
        logger.debug("[token-optimizer] api_request_error error: %s", exc)


_SPRAWL_NOTE = (
    "[Token Optimizer] {n} subagents are running concurrently. "
    "Each carries its own context and token cost - consider closing finished "
    "agents or consolidating work to reduce sprawl."
)


def on_subagent_start(**kwargs: Any) -> None:
    """Count open subagents; past 8 at once, queue a sprawl note."""
    try:
        parent = str(kwargs.get("parent_session_id") or "")
        if not parent:
            return
        with _LOCK:
            count = _SUBAGENTS.get(parent, 0) + 1
            _bounded_set(_SUBAGENTS, parent, count)
        if count >= _SUBAGENT_SPRAWL_THRESHOLD:
            _add_pending(parent, _SPRAWL_NOTE.format(n=count))
    except Exception as exc:
        logger.debug("[token-optimizer] subagent_start error: %s", exc)


def on_subagent_stop(**kwargs: Any) -> None:
    try:
        parent = str(kwargs.get("parent_session_id") or "")
        if not parent:
            return
        with _LOCK:
            if parent in _SUBAGENTS:
                _SUBAGENTS[parent] = max(0, _SUBAGENTS[parent] - 1)
    except Exception as exc:
        logger.debug("[token-optimizer] subagent_stop error: %s", exc)


def on_skill_lifecycle(**kwargs: Any) -> None:
    """Count skill loads per session for the dashboard's skill usage view.

    Hermes fires this after each authoritative skill-usage change; a "loaded"
    action is one use. The counts ride along with the session rollup.
    """
    try:
        if str(kwargs.get("action") or "") != "loaded":
            return
        session_id = str(kwargs.get("session_id") or "")
        name = kwargs.get("skill_name")
        if not session_id or not isinstance(name, str) or not name.strip():
            return
        with _LOCK:
            counts = _SKILLS.setdefault(session_id, {})
            while len(_SKILLS) > _MAX_TRACKED:
                _SKILLS.pop(next(iter(_SKILLS)))
            if name in counts or len(counts) < 200:
                counts[name[:128]] = counts.get(name[:128], 0) + 1
    except Exception as exc:
        logger.debug("[token-optimizer] on_skill_lifecycle error: %s", exc)


def on_session_start(**kwargs: Any) -> None:
    """A brand-new session: start from clean per-session state."""
    try:
        session_id = str(kwargs.get("session_id") or "")
        if session_id:
            with _LOCK:
                _ROLLED_UP.discard(session_id)
            _forget_session(session_id)
            _note_session(session_id)
    except Exception as exc:
        logger.debug("[token-optimizer] on_session_start error: %s", exc)


def on_session_reset(**kwargs: Any) -> None:
    """Session boundary (/new, gateway key swap): the new id starts clean."""
    try:
        _forget_session(str(kwargs.get("session_id") or kwargs.get("new_session_id") or ""))
    except Exception as exc:
        logger.debug("[token-optimizer] on_session_reset error: %s", exc)


def on_session_delete(**kwargs: Any) -> None:
    """A session was deleted or archived: drop what we hold in memory for it.

    Files on disk (checkpoints, archives) are left to their normal retention.
    """
    try:
        ids = {str(kwargs.get("session_id") or "")}
        deleted = kwargs.get("deleted_ids")
        if isinstance(deleted, (list, tuple, set)):
            ids.update(str(i) for i in deleted)
        for sid in ids - {""}:
            _forget_session(sid)
    except Exception as exc:
        logger.debug("[token-optimizer] on_session_delete error: %s", exc)


# ---------------------------------------------------------------------------
# Command handler: /token-optimizer (slash command inside Hermes)
# ---------------------------------------------------------------------------

_COMMAND_HELP = (
    "[Token Optimizer] /token-optimizer [summary | cost [words] | checkpoint | expand <id> | help]\n"
    "  summary      token and cost summary for recent Hermes sessions (default)\n"
    "  cost         what a past session cost, e.g. /token-optimizer cost yesterday parser\n"
    "  checkpoint   save a checkpoint of this session now\n"
    "  expand <id>  print an archived tool result in full"
)
_ARCHIVE_KEY_RE = None


def _handle_command(args: str = "", **kwargs: Any) -> str:
    """/token-optimizer [summary | cost ... | checkpoint | expand <id> | help].

    Shells to measure.py via the bridge for the heavy lifting. A bare argument
    that is not a subcommand is treated as a session id (the original form).
    """
    global _ARCHIVE_KEY_RE
    try:
        bridge = _import_bridge()
        if bridge is None:
            return "[Token Optimizer] Bridge not available — is TO installed correctly?"
        words = str(args or "").split()
        verb = words[0].lower() if words else "summary"
        session_id: str = kwargs.get("session_id") or _LAST_SESSION["id"]
        if verb in ("help", "-h", "--help"):
            return _COMMAND_HELP
        if verb == "cost":
            out = bridge.run_command_text(["session-cost", *words[1:]], timeout=30)
            return out or "[Token Optimizer] No matching session found."
        if verb == "checkpoint":
            if not session_id:
                return "[Token Optimizer] No active session to checkpoint."
            ok = _maybe_checkpoint(session_id, "milestone-manual")
            return ("[Token Optimizer] Saving a checkpoint of this session."
                    if ok else "[Token Optimizer] Could not start a checkpoint.")
        if verb == "expand":
            import re  # noqa: PLC0415

            if _ARCHIVE_KEY_RE is None:
                _ARCHIVE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
            key = words[1] if len(words) > 1 else ""
            if not _ARCHIVE_KEY_RE.fullmatch(key):
                return "[Token Optimizer] Usage: /token-optimizer expand <id>"
            out = bridge.run_command_text(["expand", key], timeout=30)
            return out or f"[Token Optimizer] No archived result with id {key}."
        target = "" if verb == "summary" else words[0]
        result = bridge.run_summary(session_id=target or kwargs.get("session_id") or "")
        return result or "[Token Optimizer] No session data available yet."
    except Exception as exc:
        logger.debug("[token-optimizer] command handler error: %s", exc)
        return f"[Token Optimizer] Error: {exc}"


# ---------------------------------------------------------------------------
# CLI setup: `hermes token-optimizer` subcommand (opens dashboard)
# ---------------------------------------------------------------------------

def _setup_cli(subparser: Any) -> None:
    """Add arguments to the `hermes token-optimizer` subparser."""
    try:
        subparser.add_argument(
            "--port",
            type=int,
            default=_DASHBOARD_PORT,
            help=f"Dashboard port (default: {_DASHBOARD_PORT})",
        )
        subparser.add_argument(
            "--session",
            default="",
            help="Session ID to summarise (default: most recent)",
        )
    except Exception:
        pass


def _handle_cli(args: Any) -> None:
    """Handle `hermes token-optimizer [args]` by opening the dashboard."""
    try:
        bridge = _import_bridge()
        if bridge is None:
            print("[Token Optimizer] Bridge not available — is TO installed correctly?")
            return
        port = getattr(args, "port", _DASHBOARD_PORT)
        session_id = getattr(args, "session", "") or ""
        bridge.run_dashboard(session_id=session_id, port=port)
    except Exception as exc:
        logger.debug("[token-optimizer] CLI handler error: %s", exc)
        print(f"[Token Optimizer] Error: {exc}")


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

# Every hook this plugin registers, in one place. plugin.yaml lists the same
# names and hermes-doctor checks both against each other.
HOOKS: tuple[tuple[str, Any], ...] = (
    ("pre_llm_call", on_pre_llm_call),
    ("post_api_request", on_post_api_request),
    ("pre_tool_call", on_pre_tool_call),
    ("post_tool_call", on_post_tool_call),
    ("transform_terminal_output", on_transform_terminal_output),
    ("transform_tool_result", on_transform_tool_result),
    ("pre_auxiliary_call", on_pre_auxiliary_call),
    ("post_compaction", on_post_compaction),
    ("api_request_error", on_api_request_error),
    ("subagent_start", on_subagent_start),
    ("subagent_stop", on_subagent_stop),
    ("on_skill_lifecycle", on_skill_lifecycle),
    ("on_session_start", on_session_start),
    ("on_session_end", on_session_end),
    ("on_session_finalize", on_session_finalize),
    ("on_session_reset", on_session_reset),
    ("on_session_delete", on_session_delete),
)


def _literal_prefix_ok(pattern: str) -> bool:
    """Would Hermes's redactor accept this regex without a warning?

    Mirrors the three structural rules of agent/redact.py's
    register_redaction_patterns (read from upstream, never imported): at least
    2 literal leading characters, no top-level alternation, no unbounded
    quantifier on a group that itself contains one. Pre-filtering keeps Hermes
    from logging a warning for every refused pattern on every start.
    """
    meta = "[(\\.?*+|{^$"
    prefix_len = next((i for i, ch in enumerate(pattern) if ch in meta), len(pattern))
    if prefix_len < 2:
        return False

    def unbounded_at(j: int) -> bool:
        if j >= len(pattern):
            return False
        if pattern[j] in "*+":
            return True
        if pattern[j] == "{":
            k = pattern.find("}", j)
            body = pattern[j + 1:k] if k != -1 else ""
            return body[:-1].isdigit() and body.endswith(",")
        return False

    groups = [False]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            i += 2 if pattern[i + 1:i + 2] == "]" else 1
            while i < len(pattern) and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
        elif ch == "(":
            groups.append(False)
        elif ch == ")":
            inner = groups.pop() if len(groups) > 1 else False
            if inner and unbounded_at(i + 1):
                return False
            groups[-1] = groups[-1] or inner
        elif ch == "|" and len(groups) == 1:
            return False
        elif unbounded_at(i):
            groups[-1] = True
            if ch == "{":
                i = pattern.find("}", i)
                if i == -1:
                    break
        i += 1
    return True


def redaction_patterns() -> list[str]:
    """Token Optimizer's credential patterns (built-in + the user's custom
    file) in the form Hermes's redactor accepts. Empty on any problem.

    Loaded in a child process under the Hermes runtime, so the custom pattern
    file is the same one the engines redact with.
    """
    bridge = _import_bridge()
    if bridge is None:
        return []
    out: list[str] = []
    for text in bridge.redaction_patterns():
        if text not in out and _literal_prefix_ok(text):
            out.append(text)
    return out


def register(ctx: Any) -> None:
    """Register hooks, redaction patterns, slash command, and CLI subcommand."""
    for name, callback in HOOKS:
        try:
            ctx.register_hook(name, callback)
        except Exception as exc:
            # An older Hermes may not know a newer hook name: skip just that one.
            logger.debug("[token-optimizer] register_hook(%s) failed: %s", name, exc)

    if _enabled("REDACTION") and hasattr(ctx, "register_redaction_patterns"):
        try:
            patterns = redaction_patterns()
            if patterns:
                ctx.register_redaction_patterns(patterns)
        except Exception as exc:
            logger.debug("[token-optimizer] register_redaction_patterns failed: %s", exc)

    try:
        ctx.register_command(
            "token-optimizer",
            _handle_command,
            description="Token Optimizer: usage summary, session cost, checkpoint, expand an archived result.",
            args_hint="[summary | cost [words] | checkpoint | expand <id> | help]",
        )
    except Exception as exc:
        logger.debug("[token-optimizer] register_command failed: %s", exc)

    try:
        ctx.register_cli_command(
            "token-optimizer",
            help=f"Open the Token Optimizer dashboard (port {_DASHBOARD_PORT}).",
            setup_fn=_setup_cli,
            handler_fn=_handle_cli,
        )
    except Exception as exc:
        logger.debug("[token-optimizer] register_cli_command failed: %s", exc)
