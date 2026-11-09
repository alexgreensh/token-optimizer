"""Thin thrash-guard adapter for the Cursor runtime (U5).

Reuses ``thrash_guard.check`` unchanged - the streak, burn and inline-script
logic, thresholds, cooldowns and redaction stay byte-identical with the
Claude path. This module only bridges two Claude-era assumptions:

1. ``thrash_guard.check`` keys its streak store on ``CLAUDE_SESSION_ID``;
   the Cursor conversation_id (already sanitized by the bridge) is supplied
   through that variable for the duration of the call.
2. ``session_store.SNAPSHOT_DIR`` resolves at import time; pointing
   ``TOKEN_OPTIMIZER_SNAPSHOT_DIR`` at ``<cursor_home>/token-optimizer``
   keeps Cursor streak state under the Cursor home instead of any Claude
   plugin-data dir (the Claude ledger must never see Cursor activity).
   Hook invocations are one-shot processes, so the env shims cannot leak
   across runtimes; they are restored anyway for the in-process tests.

Fail-open by contract: any error returns None and the hook emits nothing.
"""

from __future__ import annotations

import os

_SNAPSHOT_ENV = "TOKEN_OPTIMIZER_SNAPSHOT_DIR"
_SESSION_ENV = "CLAUDE_SESSION_ID"


def _set_env(name, value):
    old = os.environ.get(name)
    os.environ[name] = value
    return old


def _restore_env(name, old):
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


def shell_thrash_nudge(fields, now=None):
    """Return a thrash nudge line for a Cursor Shell postToolUse, else None.

    ``fields`` is the bridge's decoded payload (tool_args.command,
    tool_output, conversation_id). Cursor's postToolUse carries no separate
    exit code or stderr stream, so failure detection falls back to
    thrash_guard's output heuristics - identical to a Claude run whose
    caller does not know the exit status.
    """
    try:
        tool_args = fields.get("tool_args")
        command = tool_args.get("command") if isinstance(tool_args, dict) else None
        output = fields.get("tool_output") or ""
        sid = fields.get("conversation_id") or ""
        if not command or not isinstance(command, str) or not output or not sid:
            return None

        from runtime_env import cursor_home

        old_session = _set_env(_SESSION_ENV, sid)
        old_snapshot = _set_env(
            _SNAPSHOT_ENV, str(cursor_home() / "token-optimizer" / "snapshot")
        )
        try:
            from thrash_guard import check as _thrash_check

            import session_store
            old_dir = session_store.SNAPSHOT_DIR
            session_store.SNAPSHOT_DIR = cursor_home() / "token-optimizer" / "snapshot"
            try:
                return _thrash_check(command, output, combined_output=True, now=now)
            finally:
                session_store.SNAPSHOT_DIR = old_dir
        finally:
            _restore_env(_SESSION_ENV, old_session)
            _restore_env(_SNAPSHOT_ENV, old_snapshot)
    except Exception:
        return None
