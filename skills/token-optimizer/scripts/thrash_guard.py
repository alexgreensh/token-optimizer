#!/usr/bin/env python3
"""Runtime thrash guard: nudge-only loop prevention across turns.

Why this exists
---------------
A per-command wrapper (e.g. JFrog Boost) is exec'd once per command and keeps
no cross-turn state, so it structurally cannot see an agent quietly re-running
the same command — the failure mode Boost's own blog describes ("an agent
quietly running `ls` six times to re-establish where it is ... So we needed the
agent itself to tell us. In band.") and their issue #35 (an agent looping
"until the user interrupts", caused by Boost and undetected). Token Optimizer
is a session-stateful hook, so it can see the streak and say something.

Design (nudge-only):
- Fire only on >= 3 consecutive runs of the SAME command with BYTE-IDENTICAL
  output. Any material output change resets the streak to 1, so a command
  whose output is evolving (progress bars, growing logs) never fires.
- Burn nudge: a second signal that fires when the SAME command
  (normalised: heredoc bodies stripped, so ``cat <<EOF > file`` edits with
  changing bodies count as one command) FAILS >= 3 times in a session with
  DIFFERENT output each time. This catches the edit-compile-fail cycle where
  the agent keeps re-running a command that fails differently each time —
  the exact pattern the identical-output guard cannot see.
- Inline-script repeat nudge: a third signal that fires when a command whose
  heredoc body is >= INLINE_SCRIPT_MIN_CHARS has been run >=
  INLINE_SCRIPT_THRESHOLD times this session under the same normalised hash
  (any outcome). The agent re-sends the whole inline script as input tokens
  every turn; saving it to a file and running the file avoids that cost.
- Never deny a tool call: the caller appends the nudge line to the output the
  agent already has. The command has already run; nothing is blocked.
- Cooldown: after a nudge at streak S, the next nudge waits until streak
  S + REPEAT_AFTER, so a long stuck loop is reminded periodically, not
  every turn.
- Staleness: a streak older than STALE_SECONDS is reset — repeats spaced
  hours apart are deliberate re-checks, not thrash.
- Fail-open everywhere: any error returns None and the output stands.
- Priority: identical-output nudge > burn nudge > inline-script nudge.
  Exactly one nudge per output.

The state lives in the per-session SessionStore (command_run_streaks), so
streaks never leak across sessions.
"""

from __future__ import annotations

import os
import re
import time

# Fire once a command has produced byte-identical output this many times in
# a row (inclusive). 3 = the documented "ran `ls` six times" pattern minus
# one grace run for legitimate re-checks.
STREAK_THRESHOLD = 3
# After a nudge at streak S, the next nudge fires at streak S + REPEAT_AFTER.
REPEAT_AFTER = 3
# A streak older than this is deliberate re-checking, not thrash: reset.
STALE_SECONDS = 1800
# Outputs shorter than this are not worth a nudge line (a bare "" or "0").
MIN_OUTPUT_CHARS = 2
# Fire the burn nudge once a command has failed this many times in a session
# with different output each time. Env-tunable via the same naming convention
# as the other knobs.
FAIL_STREAK_THRESHOLD = int(os.environ.get("TOKEN_OPTIMIZER_FAIL_STREAK_THRESHOLD", "3"))
# Fire the inline-script repeat nudge once a command with a heredoc body >=
# INLINE_SCRIPT_MIN_CHARS has been run this many times under the same
# normalised hash (any outcome). 8 = enough iterations that the re-sent
# body has cost real tokens, but low enough to catch it within a session.
INLINE_SCRIPT_THRESHOLD = int(os.environ.get("TOKEN_OPTIMIZER_INLINE_SCRIPT_THRESHOLD", "8"))
# Heredoc bodies shorter than this are not worth a nudge (a one-liner
# `python3 <<EOF\nprint(1)\nEOF` is fine to repeat).
INLINE_SCRIPT_MIN_CHARS = 300
# Session IDs must match this pattern (alphanumeric + - and _). An invalid
# session ID would cause SessionStore to generate a fresh fallback UUID per
# call, preventing streak accumulation. Rejecting it here makes the silent
# disablement explicit rather than silently broken.
_VALID_SESSION_ID = re.compile(r"^[a-zA-Z0-9_-]+$")

_NUDGE_TEMPLATE = (
    "[Token Optimizer: `{label}` has now run {streak} times this session with "
    "byte-identical output. Re-running it will not change the result; either "
    "change the approach, or state plainly what is blocking you.]"
)

_BURN_NUDGE_TEMPLATE = (
    "[Token Optimizer: `{label}` has failed {fail_streak} times this session "
    "with different output. Re-running it will likely fail again; consider "
    "changing the approach, or state plainly what is blocking you.]"
)

_INLINE_SCRIPT_NUDGE_TEMPLATE = (
    "[Token Optimizer: `{label}` has been run {inline_count} times this "
    "session with an inline script; save the script to a file and run that "
    "file instead, so it is not re-sent every turn.]"
)

# Matches a heredoc: <<[-] 'DELIM' or <<[-] "DELIM" or <<[-] DELIM, followed
# by the rest of the opener line, the body, and the closing delimiter on its
# own line. We strip the body (everything between the opener line and the
# closing delimiter) so `cat <<EOF > file.c` edits with changing bodies all
# hash as one command. The rest of the opener line (e.g. `> file.c`) is kept.
# Group 1 = delimiter, group 2 = body (for _heredoc_body).
_HEREDOC_RE = re.compile(
    # The closing delimiter must be the whole line (followed by a newline or
    # end of string), so a longer token like `EOFEXTRA` does not close an
    # `EOF` heredoc early and leak the tail into the normalized command.
    r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\1(?:\n|$)",
    re.DOTALL,
)


def _normalize_command(command: str) -> str:
    """Normalise a command for hashing by stripping heredoc bodies.

    ``cat <<EOF > file.c\\n<internals>\\nEOF`` and ``cat <<EOF > file.c\\n
    <different internals>\\nEOF`` produce the same hash, so an agent
    re-editing the same file via heredocs counts as one command for streak
    purposes. Commands without heredocs are unaffected (the regex does not
    match). Trailing whitespace is stripped.
    """
    stripped = _HEREDOC_RE.sub(
        lambda m: m.group(0).split("\n")[0], command
    )
    return stripped.strip()


def _heredoc_body(command: str) -> str | None:
    """Return the body of the first heredoc in the command, or None.

    Used by the inline-script repeat nudge to check whether the body is
    large enough to be worth nudging about (>= INLINE_SCRIPT_MIN_CHARS).
    """
    m = _HEREDOC_RE.search(command)
    return m.group(2) if m else None


def _int_or_none(val) -> int | None:
    """Coerce a store value to int, or None if missing/empty."""
    return int(val) if val is not None else None


def _sanitize_label(command: str) -> str:
    """Sanitize a command for use in the nudge label.

    Strips backticks and instruction-like phrases so the nudge cannot be used
    as a prompt-injection vector via the echoed command text. The agent
    already sees the full command in the tool input; the nudge label is for
    identification, not verbatim replay.
    """
    label = command[:60]
    # Remove backticks so the label cannot break out of the template's code span.
    label = label.replace("`", "'")
    # Collapse newlines so a multi-line command cannot break the one-line nudge
    # (or inject a second line into the template).
    label = label.replace("\n", " ").replace("\r", " ")
    return label


def _looks_like_failure(stdout: str, stderr: str) -> bool:
    """Detect failure from stdout/stderr when no exit code is available.

    Reuses the ``_ERROR_STDERR_PATTERNS`` list from ``bash_compress`` (the
    same patterns the compression pipeline uses to skip failure output),
    applied to both stderr and stdout. When stderr is redirected to stdout
    (2>&1), error lines appear on stdout, so both must be checked.

    Fast path: when there is no stderr and the stdout is short and contains
    no error-like keyword, return False without importing ``bash_compress``
    (which is a heavy module). This keeps the hot path — short, clean output
    — at the same cost as the existing identical-output streak check.
    """
    if not stderr and not stdout:
        return False
    # Cheap pre-screen: if neither text contains a colon (all the patterns
    # require a colon or a specific keyword), skip the heavy import. This
    # avoids importing bash_compress for the common case of clean short
    # output, which is the hot path the brief asks us not to regress.
    has_potential = False
    for text in (stderr, stdout):
        if text and (":" in text or "Traceback" in text or "FAILED" in text
                      or "Error" in text or "error" in text
                      or "fatal" in text or "panic" in text
                      or "Fehler" in text or "erreur" in text
                      or "errore" in text):
            has_potential = True
            break
    if not has_potential:
        return False
    try:
        from bash_compress import _ERROR_STDERR_PATTERNS
        for text in (stderr, stdout):
            if not text:
                continue
            for pat in _ERROR_STDERR_PATTERNS:
                if pat.search(text):
                    return True
    except Exception:
        pass
    return False


def check(
    command: str,
    output: str,
    now: float | None = None,
    stderr: str = "",
):
    """Record this Bash run and return a nudge line when the streak warrants it.

    Returns None when there is nothing to say (the overwhelmingly common case).
    Never raises; never denies — the caller decides how to surface the nudge.
    ``now`` is injectable for tests and defaults to ``time.time()``.
    ``stderr`` is the tool response's stderr, used for failure detection when
    no exit code is available.

    Three signals share one store record:
    1. Identical-output streak (existing): fires when the same command
       produces byte-identical output >= STREAK_THRESHOLD times in a row.
    2. Burn streak: fires when the same command (normalised hash) fails
       >= FAIL_STREAK_THRESHOLD times with different output.
    3. Inline-script repeat: fires when a command whose heredoc body is
       >= INLINE_SCRIPT_MIN_CHARS has been run >= INLINE_SCRIPT_THRESHOLD
       times this session under the same normalised hash (any outcome).
    Priority: identical-output > burn > inline-script. Exactly one nudge
    per output.
    """
    try:
        if not command or not output or len(output) < MIN_OUTPUT_CHARS:
            return None
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        if not session_id or not _VALID_SESSION_ID.match(session_id):
            return None

        from session_store import SessionStore
        from delta_diff import content_hash
        from archive_result import _redact_credentials

        normalized = _normalize_command(command)
        cmd_h = content_hash(normalized)
        out_h = content_hash(output)
        # Redact before persisting, mirroring the cross-turn dedup path
        # (archive_result._redact_credentials): the command line can carry
        # inline secrets (-pPASSWORD, an auth header, a connection string) and
        # must never reach the on-disk streak store. The label shown to the
        # agent stays on the live (unredacted) command the agent already sees.
        safe_command = _redact_credentials(normalized)[:500]
        now = time.time() if now is None else now
        is_fail = _looks_like_failure(output, stderr)
        body = _heredoc_body(command)
        has_inline_script = body is not None and len(body) >= INLINE_SCRIPT_MIN_CHARS
        store = SessionStore(session_id)
        try:
            prior = store.get_command_streak(cmd_h)
            fresh = not prior or now - float(prior.get("last_ts") or 0) > STALE_SECONDS

            # --- Identical-output streak (existing signal) ---
            if (
                prior
                and prior.get("output_hash") == out_h
                and not fresh
            ):
                streak = int(prior.get("streak") or 0) + 1
                nudged_streak = _int_or_none(prior.get("nudged_streak"))
            else:
                # Material change (different output), a new command, or a stale
                # streak: start over. This is the "never fire when the output
                # changed materially" guarantee.
                streak = 1
                nudged_streak = None

            fire_identical = streak >= STREAK_THRESHOLD and (
                nudged_streak is None or streak >= nudged_streak + REPEAT_AFTER
            )

            # --- Burn streak (new signal) ---
            # Consecutive failures with DIFFERENT output. A success (a run
            # whose output does NOT match failure patterns) resets the streak
            # to 0. Byte-identical output is the stuck-nudge's domain, not a
            # success: it neither increments the burn streak (output is not
            # different) nor resets it (it is still a failure).
            if fresh:
                fail_streak = 1 if is_fail else 0
                fail_nudged = None
            elif not is_fail:
                # Success: the consecutive failure streak ends.
                fail_streak = 0
                fail_nudged = None
            elif prior and prior.get("output_hash") == out_h:
                # Failure with byte-identical output: the "stuck" case, which
                # belongs to the identical-output nudge. It breaks the run of
                # DIFFERENT-output failures the burn nudge tracks, so reset the
                # burn streak; a later varied failure starts a fresh count.
                fail_streak = 0
                fail_nudged = None
            else:
                # Failure with different output: the consecutive failure
                # streak advances.
                fail_streak = int(prior.get("fail_streak") or 0) + 1
                fail_nudged = _int_or_none(prior.get("fail_nudged_streak"))

            fire_burn = (
                fail_streak >= FAIL_STREAK_THRESHOLD
                and not fire_identical
                and (
                    fail_nudged is None
                    or fail_streak >= fail_nudged + FAIL_STREAK_THRESHOLD
                )
            )

            # --- Inline-script repeat (third signal) ---
            # Counts total runs of this normalised hash when the command
            # carries a heredoc body >= INLINE_SCRIPT_MIN_CHARS. Any outcome
            # (success or failure) increments. Stale resets to 0.
            if fresh or not has_inline_script:
                inline_count = 1 if has_inline_script else 0
                inline_nudged = None
            else:
                inline_count = int(prior.get("inline_run_count") or 0) + 1
                inline_nudged = _int_or_none(prior.get("inline_nudged_count"))

            fire_inline = (
                has_inline_script
                and inline_count >= INLINE_SCRIPT_THRESHOLD
                and not fire_identical
                and not fire_burn
                and (
                    inline_nudged is None
                    or inline_count >= inline_nudged + INLINE_SCRIPT_THRESHOLD
                )
            )

            store.upsert_command_streak(
                cmd_h, safe_command, out_h, streak,
                streak if fire_identical else nudged_streak, now,
                fail_streak,
                fail_streak if fire_burn else fail_nudged,
                inline_count,
                inline_count if fire_inline else inline_nudged,
            )

            if fire_identical:
                return _NUDGE_TEMPLATE.format(
                    label=_sanitize_label(safe_command), streak=streak
                )
            if fire_burn:
                return _BURN_NUDGE_TEMPLATE.format(
                    label=_sanitize_label(safe_command), fail_streak=fail_streak
                )
            if fire_inline:
                return _INLINE_SCRIPT_NUDGE_TEMPLATE.format(
                    label=_sanitize_label(safe_command), inline_count=inline_count
                )
            return None
        finally:
            store.close()
    except Exception:
        return None
