#!/usr/bin/env python3
"""Token Optimizer v5.12: PostToolUse Bash Compression Hook.

Compresses Bash tool output AFTER execution via Claude Code's
``updatedToolOutput`` mechanism. This is the UNIT B expansion: it handles
pipeline and metachar-containing commands that PreToolUse bash_hook.py
categorically rejects.

Architecture:
  CC runs Bash tool → PostToolUse fires → this hook receives tool_response
  → pipeline_analyzer checks read-only eligibility → bash_compress.compress()
  compresses stdout → archive raw original → attach archive pointer →
  enforce the baseline-size invariant → updatedToolOutput replaces what
  Claude sees.

The existing PreToolUse bash_hook.py continues to handle simple (metachar-free)
commands. This hook handles everything else — pipes, &&, ||, ;, redirections,
heredocs, and command substitutions.

Safety (same stack as bash_hook.py):
  - Fail-open: any exception → exit 0 with no output → Claude sees raw result
  - Read-only only: pipeline_analyzer rejects any side-effecting stage
  - No double-execution: command already ran; we only compress captured output
  - Token preservation: credential scan runs BEFORE compression
  - Raw output archived: the full stdout is stored with a retrievable key;
    the compressed output carries an expand pointer.
  - Baseline-size invariant: _enforce_baseline_invariant runs so the compressed
    preview never exceeds what Claude Code would show as baseline.
  - Error-on-stdout guard: _ERROR_STDERR_PATTERNS checked against stdout
    when stderr was redirected (2>&1), so compressed output never swallows
    error lines that appear on stdout.
  - Exit behavior: no output = pass through; JSON output = compress

Hook config (hooks/hooks.json):
  PostToolUse matcher "Bash" → bash_compress_hook.py --quiet
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path


def main() -> None:
    """Read PostToolUse hook input, compress Bash stdout if eligible."""
    try:
        from hook_io import read_stdin_hook_input
        payload = read_stdin_hook_input(max_bytes=5_242_880)  # 5MB
        if not payload:
            return
    except (json.JSONDecodeError, OSError, ImportError):
        return  # Bad input, exit silently

    # CC delivers the session id in the PostToolUse payload, not the env. Thread
    # it into CLAUDE_SESSION_ID so the archive key, cross-turn dedup, and the
    # savings log (all of which read the env) attribute to the real session
    # instead of "". Empty session_id priced every event oneshot-only, dropping
    # its reread annuity (GLM current-week-undercount finding).
    _sid = str(payload.get("session_id", "") or "")
    if _sid and not os.environ.get("CLAUDE_SESSION_ID"):
        os.environ["CLAUDE_SESSION_ID"] = _sid

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        return

    # Extract tool response
    tool_response = payload.get("tool_response", {})
    if not tool_response or not isinstance(tool_response, dict):
        return

    # Skip interrupted or image output
    if tool_response.get("interrupted", False):
        return
    if tool_response.get("isImage", False):
        return

    stdout = tool_response.get("stdout", "") or ""
    stderr = tool_response.get("stderr", "") or ""

    # Get the command that was run
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")
    if not command:
        return

    # Runtime thrash guard: record EVERY Bash run and, when the same command
    # has produced byte-identical output >= 3 times in a row, append a one-line
    # nudge. Nudge-only: the command already ran, nothing is denied, and any
    # material output change resets the streak inside thrash_guard.check().
    # Runs BEFORE every eligibility gate below because thrash is exactly the
    # small-output / failing-command case those gates skip, and before the
    # already-compressed marker check so PreToolUse-compressed repeats are
    # counted too. The nudge is saved and appended at whichever exit point
    # the compression pipeline reaches, so compression/dedup still runs on
    # nudge-firing runs (a large thrashing output is deduped, not skipped).
    _nudge = None
    try:
        from thrash_guard import check as _thrash_check
        _nudge = _thrash_check(command, stdout)
    except Exception:
        pass  # Fail open: the raw output stands

    # Too small to compress
    if not stdout or len(stdout) < 100:
        if _nudge:
            _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
        return

    # Detect if the output was ALREADY compressed by PreToolUse bash_hook.
    if "[Full result archived" in stdout or "[bash_compress]" in stdout:
        if _nudge:
            _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
        return

    # Check pipeline read-only eligibility
    try:
        from pipeline_analyzer import is_read_only_pipeline
        is_ro, reason = is_read_only_pipeline(command)
        if not is_ro:
            # --- K1: build/test/run output compression (PostToolUse) ---
            # The command is not read-only, so the standard compression path
            # skips it. But build/test/run commands (gcc, pytest, cargo, npm,
            # make, ...) produce the bulk of agent tokens and are safe to
            # compress PostToolUse: the command already ran, we only compress
            # the captured stdout. Fail-open at every step.
            #
            # Binding constraint (orchestrator): the size gate and the
            # command-string classify() must run BEFORE importing
            # build_output_compress, so small non-read-only commands (the
            # common case) pay no import cost.
            if stdout and len(stdout) >= 2048:
                try:
                    # Stage 1: command-string classification (no import needed
                    # for the check itself; classify() is a pure function that
                    # only uses shlex + a static dict).
                    from build_output_compress import classify as _build_classify
                    _is_build = _build_classify(command)
                    # Stage 2: shape fallback only if command didn't match and
                    # the output is large enough to justify the scan.
                    if not _is_build:
                        from build_output_compress import classify_by_shape as _build_shape
                        _is_build = _build_shape(stdout)
                    if _is_build:
                        _compressed_build = _try_build_output_compress(
                            command, stdout, stderr)
                        if _compressed_build is not None:
                            if _nudge:
                                _compressed_build = _compressed_build + "\n" + _nudge
                            _emit_updated_tool_output(_compressed_build, stderr)
                            return
                except Exception:
                    pass  # Fail open: raw output stands
            if _nudge:
                _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
            return  # Not eligible, pass through raw
    except Exception:
        if _nudge:
            _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
        return  # Fail open

    # Compression
    try:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

        from bash_compress import (
            compress,
            _looks_like_failure,
            _strip_ansi,
            _find_preserved_lines,
            _ERROR_STDERR_PATTERNS,
            _enforce_baseline_invariant,
        )

        # --- check stderr for failure patterns ---
        if _looks_like_failure(0, stderr):
            if _nudge:
                _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
            return  # Don't compress failure output

        # Clean ANSI escape codes before compression
        cleaned_stdout = _strip_ansi(stdout)

        # --- also scan stdout for error patterns ---
        # When stderr is redirected to stdout (2>&1), error lines appear
        # on stdout. If the pipeline exits 0 but stdout contains error
        # patterns, pass through raw so the model sees the errors.
        if _stdout_has_error_patterns(cleaned_stdout):
            if _nudge:
                _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
            return  # Error on stdout: pass through raw

        # Run the standard compression pipeline
        compressed = compress(command, cleaned_stdout, returncode=0, stderr=stderr)

        # Best single-output representation: the compressed preview when it
        # actually shrank the output by >=10%, else the raw output.
        from token_estimate import estimate_tokens as _est
        orig_tokens = _est(cleaned_stdout)
        comp_helped = bool(compressed) and compressed != cleaned_stdout and (
            orig_tokens == 0 or (1.0 - _est(compressed) / orig_tokens) >= 0.10)
        best = compressed if comp_helped else cleaned_stdout

        # Cross-turn dedup: if this command's output repeats a recent same-session
        # run, emit a compact delta reference instead. Catches repeats even when
        # single-output compression did not help (a small repeated `git status`),
        # which per-command tools cannot do -- they have no session memory.
        deduped = _crossturn_dedup(command, best)
        _log_feature = "bash_compress_pipeline"
        if deduped is not None:
            best, comp_helped = deduped, True
            _log_feature = "crossturn_dedup"

        if not comp_helped:
            if _nudge:
                _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
            return  # nothing shrank the output -> pass through raw
        compressed = best

        # --- archive raw stdout + attach a retrieval pointer ---
        # Progressive disclosure: the full uncompressed original is stored
        # on disk so the model can retrieve it via `expand <key>`. Mirror
        # the exact archiving path from bash_compress.main().
        _archive_key = None
        if len(stdout) > 500:
            try:
                from archive_result import (
                    archive_entry_exists,
                    archive_original,
                    build_archive_pointer,
                )
                _session_id = os.environ.get("CLAUDE_SESSION_ID", "")
                _archive_key = hashlib.sha256(
                    f"{_session_id}|{command}|{time.time()}|{os.urandom(4).hex()}".encode("utf-8", errors="replace")
                ).hexdigest()[:16]
                if archive_original(stdout, _session_id, _archive_key, "Bash") is not None:
                    if archive_entry_exists(_session_id, _archive_key):
                        compressed = build_archive_pointer(compressed, len(stdout), _archive_key)
                    else:
                        # Entry was pruned after write — serve raw, not lossy preview.
                        # This matches the guarantee in bash_compress.main().
                        compressed = stdout
                        _archive_key = None
                else:
                    _archive_key = None
            except Exception:
                _archive_key = None

        # --- enforce the baseline-size invariant ---
        # If our compressed preview + archive pointer would exceed what
        # Claude Code would show as a baseline stub, shrink to fit.
        try:
            compressed = _enforce_baseline_invariant(compressed, stdout, _archive_key)
        except Exception:
            pass

        # Log compression event to trends.db
        _log_event(command, cleaned_stdout, compressed, feature=_log_feature)

        # Emit updatedToolOutput to replace what Claude sees, with the
        # thrash nudge appended if present (compression + nudge, not either/or).
        if _nudge:
            compressed = compressed + "\n" + _nudge
        _emit_updated_tool_output(compressed, stderr)

    except Exception:
        if _nudge:
            _emit_updated_tool_output(stdout + "\n" + _nudge, stderr)
        return  # Fail open: any error → pass through raw


# C-3: pre-compiled combined error pattern regex. The old code iterated
# every line against all 13 patterns with no early termination: O(lines ×
# 13). Measured: 2.9s for 10K lines, 9.5s for 50K lines (clean output),
# exceeding the ~2s PostToolUse hook timeout. A single combined regex
# does one DFA pass per line instead of 13, and the early-exit below
# returns as soon as the density threshold is met.
_COMBINED_ERROR_RE = None


def _get_combined_error_re():
    """Lazily build the combined error pattern regex on first call."""
    global _COMBINED_ERROR_RE
    if _COMBINED_ERROR_RE is not None:
        return _COMBINED_ERROR_RE
    try:
        from bash_compress import _ERROR_STDERR_PATTERNS
        # Join all patterns into a single alternation. N-2: each pattern's
        # case sensitivity is preserved by wrapping only the originally
        # case-insensitive patterns in a scoped inline (?i:...) group. The
        # first attempt compiled the combined regex with a global re.I, which
        # silently upgraded the case-sensitive patterns (\bFAILED\b,
        # \bTraceback\b) and made benign output containing lowercase
        # "failed"/"traceback" trip the error-density gate.
        parts = []
        for pat in _ERROR_STDERR_PATTERNS:
            if pat.flags & re.I:
                parts.append(f"(?i:{pat.pattern})")
            else:
                parts.append(f"(?:{pat.pattern})")
        _COMBINED_ERROR_RE = re.compile("|".join(parts))
    except Exception:
        _COMBINED_ERROR_RE = False  # sentinel: build failed
    return _COMBINED_ERROR_RE


def _stdout_has_error_patterns(stdout: str) -> bool:
    """Check stdout for error patterns (covers 2>&1 redirect case).

    Uses the same _ERROR_STDERR_PATTERNS list as _looks_like_failure.
    Only triggers when stdout is large enough to make compression
    meaningful (>500 chars), so small outputs with coincidental
    error-keyword lines are not blocked.
    """
    if not stdout or len(stdout) < 500:
        return False
    try:
        combined = _get_combined_error_re()
        if not combined:
            # Fallback: fall back to the old per-pattern loop if the
            # combined regex could not be built.
            from bash_compress import _ERROR_STDERR_PATTERNS
            lines = stdout.splitlines()
            match_count = 0
            for line in lines:
                for pat in _ERROR_STDERR_PATTERNS:
                    if pat.search(line):
                        match_count += 1
                        break
            if match_count >= 3 and match_count > len(lines) * 0.10:
                return True
            return False
        # C-3: single combined regex per line + early exit when the
        # density threshold is met. O(lines × 1) instead of O(lines × 13).
        lines = stdout.splitlines()
        total_lines = len(lines)
        threshold = total_lines * 0.10
        match_count = 0
        for line in lines:
            if combined.search(line):
                match_count += 1
                # Early exit: once we have enough matches AND the density
                # threshold is met, no need to scan the rest.
                if match_count >= 3 and match_count > threshold:
                    return True
        # Final check in case we never hit the early-exit condition but
        # the full scan meets the threshold.
        if match_count >= 3 and match_count > threshold:
            return True
    except Exception:
        return False
    return False


def _crossturn_dedup(command: str, output: str):
    """Return a compact delta-reference when this command's output repeats a
    recent same-session run, else None.

    Per-command wrappers (e.g. Boost) compress each invocation in isolation and
    so re-pay for every re-run of `git status`, `ls`, a test suite, etc. Token
    Optimizer is a session-stateful hook, so it can collapse the repeat: the
    identical case becomes a one-line note, a small change becomes just the diff.
    Reuses the (previously dormant) command_outputs store + delta_diff. The
    caller still attaches the progressive-disclosure pointer, so `expand`
    recovers the full (credential-redacted) output even if the referenced run
    has scrolled out of context -- the reference is self-sufficient, never a
    dangling pointer.
    Never raises (fail-open): any trouble returns None and normal output stands.
    """
    try:
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        if not session_id or len(output) < 200:
            return None
        from session_store import SessionStore
        from delta_diff import content_hash, compute_delta
        # Canonical source: credential_patterns.redact_credentials. Importing the
        # private archive_result._redact_credentials wrapper coupled this hot path
        # to archive_result's import side effects and its _TOKEN_PATTERNS fallback
        # (which emits generic [REDACTED] labels). The shared module is the single
        # owner of the labeled-placeholder redaction contract (L-1).
        from credential_patterns import redact_credentials as _redact_credentials

        store = SessionStore(session_id)
        try:
            cmd_h = content_hash(command.strip())
            out_h = content_hash(output)  # identical-detection stays on raw bytes
            # Redact before anything is persisted or diffed, mirroring the
            # archive path (archive_result._redact_credentials), so a secret in
            # command output never reaches the on-disk dedup store.
            safe_output = _redact_credentials(output)
            # The command itself can carry inline secrets (an auth header, a
            # -pPASSWORD, a connection string). Redact BEFORE truncating so a
            # secret split across the 500-char cutoff can't survive, matching the
            # archive path which redacts the command too. cmd_h stays on the raw
            # command -- the hash is non-reversible, like out_h.
            safe_command = _redact_credentials(command)[:500]
            prior = store.get_command_output(cmd_h)
            # Record THIS run (redacted output) for the next comparison BEFORE we
            # return a delta, so deltas always chain off full (redacted) outputs,
            # not refs.
            store.insert_command_output(cmd_h, safe_command, out_h, len(output), safe_output)
            if not prior or not prior.get("compressed_output"):
                return None
            # Recency guard: only reference a run from the last hour, a rough
            # proxy for "probably still in the agent's context".
            if time.time() - float(prior.get("timestamp") or 0) > 3600:
                return None
            # C-1: the label is embedded in the ref string that becomes
            # updatedToolOutput.stdout (the model sees it) AND is logged to
            # trends.db as compressed_text via _log_event. Use the redacted
            # command so an inline secret (Bearer token, -pPASSWORD, connection
            # string) never reaches the model context or disk. safe_command is
            # already computed above and in scope.
            label = safe_command.strip()[:60]
            if prior.get("output_hash") == out_h:
                ref = (f"[Token Optimizer: identical to your previous `{label}` "
                       f"output this session; unchanged.]")
            else:
                delta, _stats = compute_delta(
                    prior["compressed_output"], safe_output, filename=label)
                if not delta:
                    return None
                ref = (f"[Token Optimizer: same as your previous `{label}` output "
                       f"this session, except:]\n{delta}")
            return ref if len(ref) < len(output) * 0.85 else None
        finally:
            store.close()
    except Exception as exc:
        # M-15: log the exception type (not the message, which may contain
        # unredacted text) so an admin can distinguish "no prior run" from
        # "redaction failed" from logs. Fail-open: still return None.
        try:
            sys.stderr.write(
                f"[Token Optimizer] crossturn dedup failed: "
                f"{type(exc).__name__}\n"
            )
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        return None


def _log_event(command: str, original: str, compressed: str,
               feature: str = "bash_compress_pipeline") -> None:
    """Log a compression event to trends.db. Fail-open, never raises.

    ``feature`` distinguishes the plain pipeline path ("bash_compress_pipeline")
    from the session-stateful cross-turn dedup ("crossturn_dedup") so each shows
    in its own dashboard bucket. Both are headline-eligible categories.
    """
    try:
        from compression_log import log_compression_event
        # C-2: command_pattern is persisted to trends.db's compression_events
        # table. Redact BEFORE truncating so an inline secret (Bearer token,
        # mysql -pPASSWORD, PGPASSWORD=... psql) never reaches disk in
        # cleartext. The dedup store path was redacted by PR #164 but this
        # adjacent path in the same main() flow was missed.
        from credential_patterns import redact_credentials as _redact
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        log_compression_event(
            feature=feature,
            original_text=original,
            compressed_text=compressed,
            session_id=session_id,
            command_pattern=_redact(command)[:100],
            quality_preserved=True,
            verified=True,
            tier="measured",
        )
    except Exception:
        pass


def _try_build_output_compress(command: str, stdout: str, stderr: str) -> str | None:
    """Compress build/test/run output and attach the archive pointer.

    Called from the not-read-only branch when classify() or classify_by_shape()
    says the output is build/test/run. Returns the compressed+archived output,
    or None to signal "pass through raw". Fail-open: any exception -> None.

    Mirrors the read-only compression path: strip ANSI, compress, archive the
    full original, attach the expand pointer, enforce the baseline invariant,
    log to trends.db with feature=build_output_compress.
    """
    try:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

        from bash_compress import _strip_ansi, _enforce_baseline_invariant
        from build_output_compress import compress as _build_compress

        cleaned_stdout = _strip_ansi(stdout)
        compressed = _build_compress(command, cleaned_stdout, returncode=0, stderr=stderr)
        if not compressed or compressed == cleaned_stdout:
            return None

        # Check that compression actually helped by >= 10%
        from token_estimate import estimate_tokens as _est
        orig_tokens = _est(cleaned_stdout)
        if orig_tokens > 0:
            comp_tokens = _est(compressed)
            if (1.0 - comp_tokens / orig_tokens) < 0.10:
                return None

        # Archive raw stdout + attach retrieval pointer (same path as read-only)
        _archive_key = None
        if len(stdout) > 500:
            try:
                from archive_result import (
                    archive_entry_exists,
                    archive_original,
                    build_archive_pointer,
                )
                _session_id = os.environ.get("CLAUDE_SESSION_ID", "")
                _archive_key = hashlib.sha256(
                    f"{_session_id}|{command}|{time.time()}|{os.urandom(4).hex()}".encode("utf-8", errors="replace")
                ).hexdigest()[:16]
                if archive_original(stdout, _session_id, _archive_key, "Bash") is not None:
                    if archive_entry_exists(_session_id, _archive_key):
                        compressed = build_archive_pointer(compressed, len(stdout), _archive_key)
                    else:
                        compressed = stdout
                        _archive_key = None
                else:
                    _archive_key = None
            except Exception:
                _archive_key = None

        # Enforce the baseline-size invariant
        try:
            compressed = _enforce_baseline_invariant(compressed, stdout, _archive_key)
        except Exception:
            pass

        # Log to trends.db with the build_output_compress feature
        _log_event(command, cleaned_stdout, compressed, feature="build_output_compress")

        return compressed
    except Exception:
        return None


def _emit_updated_tool_output(stdout: str, stderr: str) -> None:
    """Emit the PostToolUse updatedToolOutput envelope.

    Both the thrash nudge and the compression path replace what the agent sees
    through the same envelope, so the hook event name and the four output-shape
    fields (stdout, stderr, interrupted, isImage) live in exactly one place.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": {
                "stdout": stdout,
                "stderr": stderr,
                "interrupted": False,
                "isImage": False,
            },
        },
    }))


if __name__ == "__main__":
    main()
