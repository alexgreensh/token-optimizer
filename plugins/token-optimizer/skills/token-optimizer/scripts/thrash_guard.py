#!/usr/bin/env python3
"""Runtime thrash guard: nudge-only loop prevention across turns.

Why this exists
---------------
A per-command wrapper (e.g. JFrog Boost) is exec'd once per command and keeps
no cross-turn state, so it structurally cannot see an agent quietly re-running
the same command — the failure mode Boost's own blog describes ("an agent
quietly running `ls` six times to re-establish where it is ... So we needed the
agent itself to tell us. In band.") and a Boost blog post describing an agent
looping "until the user interrupts", caused by Boost and undetected). Token Optimizer
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
streaks never leak across sessions. When the caller passes ``store_id`` (the
same agent-scoped identity the cross-turn dedup path uses), the streak store is
keyed on that instead, so streaks never leak across agents within a session
either -- a subagent never inherits or bumps the main agent's streak.
"""

from __future__ import annotations

import bisect
import os
import re
import shlex
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
    "[Token Optimizer: `{label}` has run {streak} times this session with "
    "byte-identical output. Before running again, state what you expect to "
    "differ; prefer a targeted edit over rewriting the whole file.]"
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

# Matches a heredoc opener: <<[-] 'DELIM' or <<[-] "DELIM" or <<[-] DELIM.
# Only the opener is matched here; the closing delimiter is found by a
# forward scan (see _find_all_heredocs). This avoids the catastrophic
# backtracking of the old single regex `<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n
# (.*?)\n\1(?:\n|$)` whose lazy `.*?` + backreference `\1` went quadratic
# on inputs with many unclosed openers (the hot path: _normalize_command
# runs via check() on every Bash PostToolUse).
_HEREDOC_OPENER_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")

# A closing delimiter line: a newline followed by a bare word (the
# delimiter) that is itself the whole line (followed by another newline or
# end of string). The lookahead ensures a longer token like ``EOFEXTRA``
# does not close an ``EOF`` heredoc early. Used to pre-index every
# candidate closing line in a single forward pass so the per-opener lookup
# is O(log n) instead of O(n).
_CLOSING_LINE_RE = re.compile(r"\n(\w+)(?=\n|$)")


def _find_all_heredocs(command: str):
    """Find all non-overlapping heredocs using a non-backtracking two-pass scan.

    Replaces the old backtracking regex that went quadratic on many unclosed
    ``<<EOF`` openers. Pass 1 finds every opener and every candidate closing
    line in two single forward scans. Pass 2 walks the openers in order and,
    for each, binary-searches the pre-indexed closing lines for the first one
    whose delimiter matches and whose position is past the opener's body
    start. Non-overlapping: once a heredoc is closed, openers inside its body
    are skipped, matching the old ``re.sub`` semantics.

    Returns a list of ``(match_start, match_end, opener_line, body)`` tuples
    where ``match_start``/``match_end`` delimit the span from the ``<<`` of
    the opener through the trailing newline of the closing delimiter line
    (or end of string), ``opener_line`` is the first line of that span, and
    ``body`` is the text between the opener line and the closing line.
    """
    openers = list(_HEREDOC_OPENER_RE.finditer(command))
    if not openers:
        return []

    # Index every candidate closing line by delimiter word. Each entry is
    # (close_pos, match_end) where close_pos is the position of the ``\n``
    # that precedes the delimiter line and match_end is the position after
    # the trailing ``\n`` (or end of string). finditer returns matches in
    # ascending position order, so each list is sorted by close_pos.
    closing_by_delim: dict[str, list[tuple[int, int]]] = {}
    for m in _CLOSING_LINE_RE.finditer(command):
        delim = m.group(1)
        close_pos = m.start()  # the \n before the delimiter word
        after = m.end()        # position just past the delimiter word
        if after < len(command) and command[after] == "\n":
            match_end = after + 1  # include the trailing \n
        else:
            match_end = after      # end of string, no trailing \n
        closing_by_delim.setdefault(delim, []).append((close_pos, match_end))

    results = []
    consumed = 0  # position past the end of the last accepted heredoc
    for m in openers:
        if m.start() < consumed:
            continue  # opener is inside a previously closed heredoc body
        delim = m.group(1)
        line_end = command.find("\n", m.end())
        if line_end == -1:
            continue  # opener is on the last line; no body possible
        body_start = line_end + 1
        candidates = closing_by_delim.get(delim)
        if not candidates:
            continue  # delimiter never appears as a closing line: unclosed
        # Binary-search for the first closing line at or past the position
        # of the opener line's newline (body_start - 1 == line_end). A
        # closing line immediately after the opener line has its preceding
        # ``\n`` at exactly line_end, so the lower bound is line_end.
        idx = bisect.bisect_left(candidates, (line_end,))
        if idx >= len(candidates):
            continue  # no closing line after this opener
        close_pos, match_end = candidates[idx]
        match_start = m.start()
        opener_line = command[match_start:line_end]
        body = command[body_start:close_pos]
        results.append((match_start, match_end, opener_line, body))
        consumed = match_end
    return results


def _normalize_command(command: str) -> str:
    """Normalise a command for hashing by stripping heredoc bodies.

    ``cat <<EOF > file.c\\n<internals>\\nEOF`` and ``cat <<EOF > file.c\\n
    <different internals>\\nEOF`` produce the same hash, so an agent
    re-editing the same file via heredocs counts as one command for streak
    purposes. Commands without heredocs are unaffected. Trailing whitespace
    is stripped.
    """
    heredocs = _find_all_heredocs(command)
    if not heredocs:
        return command.strip()
    parts: list[str] = []
    last = 0
    for start, end, opener_line, _body in heredocs:
        parts.append(command[last:start])
        parts.append(opener_line)
        last = end
    parts.append(command[last:])
    return "".join(parts).strip()


def _heredoc_body(command: str) -> str | None:
    """Return the body of the first heredoc in the command, or None.

    Used by the inline-script repeat nudge to check whether the body is
    large enough to be worth nudging about (>= INLINE_SCRIPT_MIN_CHARS).
    """
    heredocs = _find_all_heredocs(command)
    return heredocs[0][3] if heredocs else None


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


# A line that is an error keyword followed only by a number is a metric
# ("Error: 0.000835" from a numeric report, "fatal: 8.35e-04" from a loss
# printout), not a failure. Scientific notation included.
_NUMERIC_ERROR_LINE_RE = re.compile(
    r"^\s*(?:error|fatal)\s*:\s*[-+]?[\d.]+(?:e[-+]?\d+)?\s*$", re.IGNORECASE
)


def _text_has_failure(text: str) -> bool:
    """True when any line of ``text`` matches a failure pattern.

    Per line, so a pattern can match anywhere in the line (gcc/clang
    ``file.c:5:3: error: ...``, pytest ``test_b FAILED``), while a line that
    is only an error keyword plus a number is skipped as a metric.
    """
    from bash_compress import _ERROR_STDERR_PATTERNS
    for line in text.splitlines():
        if _NUMERIC_ERROR_LINE_RE.match(line):
            continue
        for pat in _ERROR_STDERR_PATTERNS:
            if pat.search(line):
                return True
    return False

# Final pipeline stages whose visible output can stay identical while the
# underlying data changes (a truncating filter shows the same slice; a count
# can repeat). Byte-identical output from these is weaker evidence of a stuck
# loop, so they need a longer streak before the nudge fires -- they are
# delayed, never silenced.
_TRUNCATING_FILTER_CMDS = frozenset({"tail", "head", "wc"})


def _ends_with_truncating_filter(command: str) -> bool:
    """True when the command's last pipeline stage truncates its output.

    ``progress.log | tail -10`` can be byte-identical across runs even while
    the log grows, and ``wc -l`` / ``grep -c`` can repeat on unchanged data,
    so these commands must survive a longer identical-output streak before
    the nudge fires.
    """
    try:
        last_stage = command.rstrip().split("|")[-1].strip()
        tokens = shlex.split(last_stage)
        if not tokens:
            return False
        cmd = tokens[0].rsplit("/", 1)[-1]
        if cmd in _TRUNCATING_FILTER_CMDS:
            return True
        if cmd in ("grep", "egrep", "fgrep"):
            # -c counts matches; -m N caps how many are shown. Context flags
            # (-A/-B/-C) do not truncate.
            for t in tokens[1:]:
                if not t.startswith("-") or t.startswith("--"):
                    continue
                cluster = t[1:]
                if cluster and all(ch in "cm0123456789" for ch in cluster):
                    if any(ch in cluster for ch in "cm") or any(
                            ch.isdigit() for ch in cluster):
                        return True
            return False
        return False
    except Exception:
        return False


def _workspace_changed_since(store, ts: float) -> bool:
    """True when a file-writing tool ran after ``ts`` (best effort).

    Reads the session's activity log, which records every tool use with a
    timestamp. If the agent edited files between two runs of the same
    command, byte-identical output is not evidence of a stuck loop.

    ``store`` must be the BARE-SESSION store (``SessionStore(session_id)``),
    because that is where ``context_intel.py`` writes ``activity_log``. Passing
    the agent-scoped streak store here would read an empty log for subagents and
    make edit-detection inert (issue #190 follow-up).
    """
    try:
        row = store._connect().execute(
            "SELECT 1 FROM activity_log "
            "WHERE tool_bucket = 'edit' AND timestamp > ? LIMIT 1",
            (ts,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _looks_like_failure(
    stdout: str,
    stderr: str,
    exit_code: int | None = None,
    redirected: bool = False,
) -> bool:
    """Decide whether a Bash run failed.

    The exit code is authoritative when known: non-zero means failure, zero
    means success, and stdout is never scanned in that case. A program that
    prints an accuracy metric like ``Error: 0.000835`` and exits 0 is a
    success, not a failure.

    When no exit code is available, fall back to text: stderr is always
    scanned; stdout is scanned only when the command redirected stderr into
    stdout (``2>&1``), because otherwise an error-looking line on stdout is
    just data. Redirected stdout is scanned line by line, anchored to line
    start, with purely numeric payloads after the colon excluded.
    """
    if exit_code is not None:
        return exit_code != 0
    if not stderr and not stdout:
        return False
    # Cheap pre-screen: if neither scanned text contains a colon (all the
    # patterns require a colon or a specific keyword), skip the heavy import.
    # This avoids importing bash_compress for the common case of clean short
    # output, which is the hot path. Stdout is only scanned when redirected,
    # so it only participates in the pre-screen in that case.
    has_potential = False
    for text in (stderr, stdout if redirected else ""):
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
        if stderr and _text_has_failure(stderr):
            return True
        if stdout and redirected and _text_has_failure(stdout):
            return True
    except Exception:
        pass
    return False


def check(
    command: str,
    output: str,
    now: float | None = None,
    stderr: str = "",
    exit_code: int | None = None,
    store_id: str | None = None,
):
    """Record this Bash run and return a nudge line when the streak warrants it.

    Returns None when there is nothing to say (the overwhelmingly common case).
    Never raises; never denies — the caller decides how to surface the nudge.
    ``now`` is injectable for tests and defaults to ``time.time()``.
    ``stderr`` is the tool response's stderr, used for failure detection when
    no exit code is available. ``exit_code`` is the command's exit status when
    the caller knows it; when present it is the sole failure signal and the
    output text is never scanned. ``store_id`` is the agent-scoped storage
    identity to key the streak store on (the same one the cross-turn dedup path
    computes via ``_dedup_store_id``); ``None`` (the default) preserves the
    historical session-only identity byte-for-byte, so the main/no-agent case is
    unchanged.

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
        # Agent-scoped streak store: when the caller passes an already-computed
        # scoped identity (the SAME _dedup_store_id the cross-turn dedup path
        # uses), key the streak store on it so a subagent's streaks never mix
        # with the main agent's, and vice versa (issue #189). store_id=None (the
        # default) preserves the historical session-only identity exactly. A
        # malformed scoped id is rejected the same way an invalid session id is
        # (SessionStore would otherwise spawn a fresh fallback store per call and
        # silently stop accumulating streaks).
        store_identity = store_id or session_id
        if not _VALID_SESSION_ID.match(store_identity):
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
        safe_command = _redact_credentials(normalized, command=True)[:500]
        now = time.time() if now is None else now
        is_fail = _looks_like_failure(
            output, stderr, exit_code=exit_code,
            redirected="2>&1" in command,
        )
        body = _heredoc_body(command)
        has_inline_script = body is not None and len(body) >= INLINE_SCRIPT_MIN_CHARS
        store = SessionStore(store_identity)
        # Edit-detection reads activity_log, which context_intel.py writes to the
        # BARE-session store (SessionStore(session_id)), NOT the agent-scoped
        # streak store. For a subagent, store_identity != session_id, so the
        # agent-scoped store's activity_log is empty; reading it there would make
        # the "workspace changed since last run" suppression inert and produce a
        # false thrash nudge (issue #190 follow-up). Streak state stays on the
        # agent-scoped ``store``; only the activity_log lookup uses this one. In
        # the main/no-agent path store_identity == session_id, so this is the
        # same store object and behavior is byte-identical.
        if store_identity == session_id:
            activity_store = store
            _own_activity_store = False
        else:
            activity_store = SessionStore(session_id)
            _own_activity_store = True
        try:
            # Acquire a write lock BEFORE the read so the get-compute-upsert
            # sequence is atomic: two concurrent hook processes cannot both
            # read the same streak and both write streak+1 (losing one
            # increment). BEGIN IMMEDIATE blocks other writers for the
            # duration; a lock failure is caught by the outer fail-open and
            # the streak proceeds without atomicity (a dropped increment,
            # not a crash).
            try:
                store._connect().execute("BEGIN IMMEDIATE")
            except Exception:
                pass  # fail-open: proceed without the lock
            prior = store.get_command_streak(cmd_h)
            fresh = not prior or now - float(prior.get("last_ts") or 0) > STALE_SECONDS
            # An edit between two runs makes byte-identical output
            # meaningless as a stuck-loop signal: the workspace changed, so
            # the streak restarts. Only the identical-output branch cares,
            # so the activity-log lookup runs only there.
            workspace_changed = (
                bool(prior) and not fresh
                and prior.get("output_hash") == out_h
                and _workspace_changed_since(
                    activity_store, float(prior.get("last_ts") or 0)
                )
            )

            # --- Identical-output streak (existing signal) ---
            if (
                prior
                and prior.get("output_hash") == out_h
                and not fresh
                and not workspace_changed
            ):
                streak = int(prior.get("streak") or 0) + 1
                nudged_streak = _int_or_none(prior.get("nudged_streak"))
            else:
                # Material change (different output), a new command, a stale
                # streak, or an intervening edit: start over. This is the
                # "never fire when the output changed materially" guarantee.
                streak = 1
                nudged_streak = None

            # A truncating filter's identical output is weaker evidence, so
            # it takes twice the streak to fire.
            identical_threshold = (
                2 * STREAK_THRESHOLD
                if _ends_with_truncating_filter(command)
                else STREAK_THRESHOLD
            )
            fire_identical = (
                streak >= identical_threshold
                and (
                    nudged_streak is None or streak >= nudged_streak + REPEAT_AFTER
                )
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
                    label=_sanitize_label(safe_command), streak=streak,
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
            if _own_activity_store:
                activity_store.close()
    except Exception:
        return None
