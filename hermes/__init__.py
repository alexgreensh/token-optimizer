"""token-optimizer — Hermes plugin for Token Optimizer.

Provides proactive context nudges, per-turn token accumulation, session rollup
into TO's trends.db, and a /token-optimizer slash command + CLI subcommand.

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
#           below the threshold and a second crossing stay silent. Cleared on
#           session_finalize/end so the NEXT session starts clean.
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_TALLY: dict[str, dict[str, int]] = {}
_NUDGED: set[str] = set()
# H4: tracks sessions already rolled up this process lifetime so
# on_session_finalize + on_session_end don't both spawn a rollup subprocess.
_ROLLED_UP: set[str] = set()

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
    except Exception as exc:
        logger.debug("[token-optimizer] post_api_request accumulation error: %s", exc)


# ---------------------------------------------------------------------------
# Hook: pre_llm_call — THE NUDGE
# ---------------------------------------------------------------------------

def on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
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
# Hook: on_session_finalize / on_session_end — rollup to trends.db
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
    bridge = _import_bridge()
    if bridge is None:
        logger.debug("[token-optimizer] bridge unavailable, skipping rollup for %s", session_id)
    else:
        try:
            bridge.run_rollup(session_id=session_id, platform=platform, reason=reason, context_tokens=context_tokens or None)
        except Exception as exc:
            logger.debug("[token-optimizer] rollup error for %s: %s", session_id, exc)
    # Clear per-session state regardless of rollup outcome.
    with _LOCK:
        _TALLY.pop(session_id, None)
        _NUDGED.discard(session_id)


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
    """Handle on_session_end: broader payload, same rollup path.

    _do_rollup contains the H4 double-rollup guard: if on_session_finalize
    already rolled up this session, _do_rollup returns early so only one
    subprocess is spawned.  The call is still made so that sessions where
    only on_session_end fires (finalize was not called) are still captured.
    """
    try:
        session_id: str = kwargs.get("session_id") or ""
        platform: str = kwargs.get("platform") or "hermes"
        reason: str = kwargs.get("reason") or ""
        _do_rollup(session_id, platform, reason)
    except Exception as exc:
        logger.debug("[token-optimizer] on_session_end error: %s", exc)


# ---------------------------------------------------------------------------
# Command handler: /token-optimizer (slash command inside Hermes)
# ---------------------------------------------------------------------------

def _handle_command(args: str = "", **kwargs: Any) -> str:
    """Print a token/cost summary for the current or most-recent session.

    Shells to measure.py via the bridge for the heavy lifting.
    """
    try:
        bridge = _import_bridge()
        if bridge is None:
            return "[Token Optimizer] Bridge not available — is TO installed correctly?"
        session_id: str = kwargs.get("session_id") or ""
        result = bridge.run_summary(session_id=session_id)
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

def register(ctx: Any) -> None:
    """Register hooks, slash command, and CLI subcommand with the Hermes context."""
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_end", on_session_end)

    try:
        ctx.register_command(
            "token-optimizer",
            _handle_command,
            description="Show context usage and cost summary for this session.",
            args_hint="[session_id]",
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
