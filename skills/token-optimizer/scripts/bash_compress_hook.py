#!/usr/bin/env python3
"""Token Optimizer v5.12: PostToolUse Bash Compression Hook.

Compresses Bash tool output AFTER execution via Claude Code's
``updatedToolOutput`` mechanism. Handles eligible simple commands and
pipelines after execution, without rewriting or approving the original call.

Architecture:
  CC runs Bash tool → PostToolUse fires → this hook receives tool_response
  → pipeline_analyzer checks read-only eligibility → bash_compress.compress()
  compresses stdout → archive raw original → attach archive pointer →
  enforce the baseline-size invariant → updatedToolOutput replaces what
  Claude sees.

The legacy PreToolUse bash_hook.py entry point is silent and is not registered.
PostToolUse compression covers read-only simple commands and supported pipelines,
including commands with pipes, &&, ||, ;, redirections, heredocs, and substitutions.

Safety:
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


# Prefix Claude Code puts on a Bash tool_response delivered as a string when
# the command exited non-zero. The remainder of the string is the output.
_EXIT_CODE_RESPONSE_RE = re.compile(r"^Error: Exit code (\d+)\s*\n?")

# Codex/Cowork deliver a failed Bash tool_response as a plain string starting
# with "Exit code N\n..." (no "Error: " prefix). Anchored at the string START
# so a successful command whose output merely contains "Exit code 1" on a later
# line cannot misfire the failure parse.
_EXIT_CODE_RESPONSE_RE_CODEX = re.compile(r"^Exit code (\d+)\s*\n?")

# Claude Code omits agent_id for the main context. Keep that context on the
# historical session-only store id while giving every explicit agent its own
# store. This sentinel is process-local plumbing, never persisted as an id.
_MAIN_AGENT_SENTINEL = "__main_agent__"
_DEDUP_AGENT_ENV = "TOKEN_OPTIMIZER_DEDUP_AGENT_ID"
# Window for recognising a twin hook that predates last_tool_use_id (see
# _crossturn_dedup). Two processes for one call land well inside it.
_LEGACY_TWIN_SECONDS = 2.0


def _pending_redaction_warning() -> str | None:
    """One-time user-facing warning that custom redaction is inactive.

    Populated only when a configured pattern file failed to load; folds into
    the hook's own JSON envelope as ``systemMessage`` so the user sees it
    instead of only a stderr line."""
    try:
        from credential_patterns import pop_redaction_warning
        return pop_redaction_warning()
    except Exception:
        return None


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
    # its reread annuity (current-week-undercount). Scoped to this hook run:
    # the finally below removes it again so in-process callers never observe a
    # mutated parent environment.
    _sid = str(payload.get("session_id", "") or "")
    _injected = bool(_sid) and not os.environ.get("CLAUDE_SESSION_ID")
    if _injected:
        os.environ["CLAUDE_SESSION_ID"] = _sid
    # Payload identity is authoritative for this invocation. A hook process may
    # be reused or nested with a stale value in its environment, so always
    # override it for the call, then restore the exact prior state. A supplied
    # agent_id is an OPAQUE identity: strip is consulted ONLY to decide
    # emptiness, never to normalize. A whitespace-only agent_id is not a real
    # identity, so it falls back to the main-agent sentinel and a host that
    # sends "  " cannot silently fork the main agent off its own dedup/streak
    # history. A non-empty id is carried through UN-stripped, so " agent-A " and
    # "agent-A" stay distinct stores (no accidental merge of two supplied ids).
    _raw_agent_id = str(payload.get("agent_id", "") or "")
    _agent_context = _raw_agent_id if _raw_agent_id.strip() else _MAIN_AGENT_SENTINEL
    _agent_was_present = _DEDUP_AGENT_ENV in os.environ
    _previous_agent_context = os.environ.get(_DEDUP_AGENT_ENV)
    os.environ[_DEDUP_AGENT_ENV] = _agent_context
    try:
        _run(payload)
    finally:
        if _agent_was_present:
            # os.environ values are always strings when the key is present, so
            # the restore is unconditional (the prior value may be "" but never
            # None here).
            os.environ[_DEDUP_AGENT_ENV] = _previous_agent_context
        else:
            os.environ.pop(_DEDUP_AGENT_ENV, None)
        if _injected:
            del os.environ["CLAUDE_SESSION_ID"]


def _run(payload: dict) -> None:
    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        return

    from recovery_output import is_recovery_output
    if is_recovery_output(tool_name, payload.get("tool_input", {})):
        return

    # Extract tool response across ALL host payload shapes so the thrash guard
    # records EVERY Bash run (not just the ones with a shape we recognize):
    #   - Claude Code PostToolUse: dict with stdout/stderr/exit_code
    #   - Claude Code PostToolUseFailure: ``error`` string "Exit code N\n..."
    #     (handled by the separate PostToolUseFailure path, not here)
    #   - Codex/Cowork PostToolUse: dict with exit_code, OR a plain string
    #     "Exit code N\n..." (no "Error: " prefix)
    #   - Any host: a plain string that does not match a failure prefix is a
    #     successful command's output (exit_code=0) -- do NOT return early,
    #     or the thrash guard never records it.
    tool_response = payload.get("tool_response", {})
    exit_code: int | None = None
    if isinstance(tool_response, str):
        m = _EXIT_CODE_RESPONSE_RE.match(tool_response)
        if m is None:
            m = _EXIT_CODE_RESPONSE_RE_CODEX.match(tool_response)
        if m is not None:
            exit_code = int(m.group(1))
            stdout = tool_response[m.end():]
            stderr = ""
        else:
            # No failure prefix: treat as a successful command's output so the
            # thrash guard still records it. exit_code stays None (success).
            stdout = tool_response
            stderr = ""
    else:
        if not tool_response or not isinstance(tool_response, dict):
            return

        # Skip interrupted or image output
        if tool_response.get("interrupted", False):
            return
        if tool_response.get("isImage", False):
            return

        stdout = tool_response.get("stdout", "") or ""
        stderr = tool_response.get("stderr", "") or ""
        structured_exit = tool_response.get("exit_code")
        if isinstance(structured_exit, int):
            exit_code = structured_exit

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
        # Scope the thrash streak store by the SAME agent identity as the
        # cross-turn dedup path (issue #189). Without this, the streak store
        # keys on the bare session id, so a subagent gets a "ran N times this
        # session" nudge for runs the MAIN agent made, and the subagent's runs
        # bump the main agent's streak -- the exact cross-agent contamination
        # the dedup path was scoped to prevent. Pass the already-computed scoped
        # store id (a string, so no circular import back into this module). The
        # main/no-agent case resolves to the bare session id, so behavior there
        # is byte-identical to before.
        _thrash_store_id = _dedup_store_id(
            os.environ.get("CLAUDE_SESSION_ID", ""),
            os.environ.get(_DEDUP_AGENT_ENV, _MAIN_AGENT_SENTINEL),
        )
        _nudge = _thrash_check(command, stdout, stderr=stderr,
                               exit_code=exit_code, store_id=_thrash_store_id)
    except Exception:
        pass  # Fail open: the raw output stands

    # Control group (TOKEN_OPTIMIZER_HOLDOUT, default 0 = off, see holdout.py): a
    # control session replaces nothing and gets no nudge. What this hook would
    # have done is logged at the opportunity tier. With the variable unset this
    # is one environment lookup and no import.
    if _holdout_control():
        _shadow_bash(command, stdout, exit_code)
        return

    # Codex-via-install perf optimization: TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT
    # skips the compression compute entirely. updatedToolOutput is ignored by
    # Codex anyway (collapse there is via archival), so skipping the compute
    # loses nothing. The nudge still reaches the model via additionalContext,
    # which every host honors (Claude Code, Codex, Cowork). One nudge, once.
    if os.environ.get("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", "").strip():
        _redact_warn = _pending_redaction_warning()
        if _nudge or _redact_warn:
            _payload = {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": _nudge,
            }}
            if _redact_warn:
                _payload["systemMessage"] = _redact_warn
            print(json.dumps(_payload))
        return

    # Coverage funnel (counters only, no behaviour change): every result that
    # gets past the checks above ends in exactly one gate row. The closure
    # records the first gate reached and ignores later calls, so no exit path
    # can double count. Fail-open inside coverage_funnel.record().
    _cov_done: list = []

    def _cov(gate: str, reason: str = "", pattern: str = "", text: str = "",
             removed: int = 0) -> None:
        if _cov_done:
            return
        _cov_done.append(gate)
        _record_coverage(gate, reason, pattern, text, removed)

    # Too small to compress
    if not stdout or len(stdout) < 100:
        _cov("error_or_short", "short", text=stdout or "")
        if _nudge:
            _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
        return

    # Detect if the output was ALREADY compressed by PreToolUse bash_hook.
    if "[Full result archived" in stdout or "[bash_compress]" in stdout:
        _cov("already_compressed", "marker", text=stdout)
        if _nudge:
            _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
        return

    # Explicit output-limit guard (head -n, tail -n, sed -n) + Step 3 cat re-read.
    try:
        from pipe_file_compress import (
            FEATURE_BASH_REREAD, has_explicit_output_limit as _has_exp_lim,
            hook_try_cat_reread as _try_cat,
        )
        if _has_exp_lim(command):
            _cov("not_read_only", "explicit_limit", text=stdout)
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return
        if "cat" in command:
            _cat_sid = _dedup_store_id(
                os.environ.get("CLAUDE_SESSION_ID", ""),
                os.environ.get(_DEDUP_AGENT_ENV, _MAIN_AGENT_SENTINEL),
            )
            _is_cat, _cat_out = _try_cat(command, stdout, payload, _cat_sid, exit_code, stderr)
            if _is_cat:
                if _cat_out is not None:
                    _log_event(command, stdout, _cat_out, feature=FEATURE_BASH_REREAD)
                    _cov("compressed", FEATURE_BASH_REREAD, "cat", stdout, _removed_tokens(stdout, _cat_out))
                    _emit_updated_tool_output(_cat_out, stderr, nudge=_nudge)
                else:
                    if _try_large_output_policy(command, stdout, stderr, payload, _nudge, _cov):
                        return
                    _cov("not_read_only", "cat_file_read", text=stdout)
                    if _nudge:
                        _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
                return
    except Exception:
        pass  # Fail open

    # A single renderer cannot safely separate output from multiple producers.
    try:
        from post_run_classify import unsafe_compound_output
        if unsafe_compound_output(command):
            if _try_large_output_policy(command, stdout, stderr, payload, _nudge, _cov):
                return
            _cov("not_read_only", "mixed_output", text=stdout)
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return
    except Exception:
        return  # fail open on ambiguous output

    # Check pipeline read-only eligibility
    compress_command = command  # command used to pick a compressor
    _post_run_shadow = None  # effective command, shadow mode only
    try:
        from pipeline_analyzer import is_read_only_pipeline
        is_ro, reason = is_read_only_pipeline(command)
        if not is_ro:
            # W2: the command already ran, so a harmless prefix (`cd /p &&`,
            # `export X=1;`, `git -C /p`) should not hide it from the same
            # pattern-specific compressor its bare form gets. post_run_classify
            # returns that bare form, or None. Shadow mode only measures.
            try:
                from post_run_classify import mode as _prc_mode
                _prc = _prc_mode()
                if _prc != "off":
                    from post_run_classify import eligible_effective
                    _eff = eligible_effective(command)
                    if _eff is not None and _prc == "on":
                        is_ro, compress_command = True, _eff
                    elif _eff is not None:
                        _post_run_shadow = _eff
            except Exception:
                pass  # Fail open: the baseline path below runs unchanged
        if not is_ro:
            # Step 2: Safe read-only pipelines / harmless-prefix chains (pipe_compress).
            _prc_env = os.environ.get("TOKEN_OPTIMIZER_POST_RUN_CLASSIFY", "").strip().lower()
            if _prc_env not in ("0", "off", "false", "no", "shadow"):
                try:
                    from pipe_file_compress import FEATURE_PIPE_COMPRESS, classify_safe_pipeline, hook_try_pipe_compress
                    _pipe_cls = classify_safe_pipeline(command)
                    _pipe_is_search = bool(_pipe_cls and _pipe_cls[1] == "search_results")
                    if _pipe_cls is not None and not _stdout_has_error_patterns(stdout, is_search=_pipe_is_search):
                        _pr = hook_try_pipe_compress(command, stdout, exit_code, stderr)
                        if _pr is not None:
                            _pipe_out, _clean_orig, _pipe_pat = _pr
                            _log_event(command, _clean_orig, _pipe_out, feature=FEATURE_PIPE_COMPRESS)
                            _cov("compressed", FEATURE_PIPE_COMPRESS, _pipe_pat, stdout, _removed_tokens(_clean_orig, _pipe_out))
                            _emit_updated_tool_output(_pipe_out, stderr, nudge=_nudge)
                            return
                except Exception:
                    pass  # Fail open: fall through to build/raw
            # --- K1: build/test/run output compression (PostToolUse) ---
            # The command is not read-only, so the standard compression path
            # skips it. But build/test/run commands (gcc, pytest, cargo, npm,
            # make, ...) produce the bulk of agent tokens and are safe to
            # compress PostToolUse: the command already ran, we only compress
            # the captured stdout. Fail-open at every step.
            #
            # Performance guard: the line-count gate and the
            # command-string classify() must run BEFORE importing
            # build_output_compress, so small non-read-only commands (the
            # common case) pay no import cost. The gate mirrors
            # build_output_compress._MIN_COMPRESS_LINES without the import.
            if stdout and len(stdout.splitlines()) >= 25:
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
                        _is_build = _build_shape(stdout, command)
                    if _is_build:
                        _compressed_build = _try_build_output_compress(
                            command, stdout)
                        if _compressed_build is not None:
                            _cov("compressed", "build_output", "build_output",
                                 stdout, _removed_tokens(stdout, _compressed_build))
                            _emit_updated_tool_output(
                                _compressed_build, stderr, nudge=_nudge)
                            return
                except Exception:
                    pass  # Fail open: raw output stands
            if _try_large_output_policy(command, stdout, stderr, payload, _nudge, _cov):
                return
            _cov("not_read_only", reason, text=stdout)
            if _post_run_shadow is not None:
                try:
                    from post_run_classify import shadow_measure
                    shadow_measure(command, _post_run_shadow, stdout, stderr,
                                   exit_code, _stdout_has_error_patterns)
                except Exception:
                    pass  # Shadow never changes what the model sees
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return  # Not eligible, pass through raw
    except Exception:
        _cov("not_read_only", "analyzer-error", text=stdout)
        if _nudge:
            _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
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
            _detect_pattern,
        )

        # --- check for failure (exit code or stderr patterns) ---
        if _looks_like_failure(exit_code, stderr):
            if _try_large_output_policy(command, stdout, stderr, payload, _nudge, _cov):
                return
            _cov("error_or_short", "failed_command", text=stdout)
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return  # Don't compress failure output

        # Clean ANSI escape codes before compression
        cleaned_stdout = _strip_ansi(stdout)

        # --- also scan stdout for error patterns ---
        # When stderr is redirected to stdout (2>&1), error lines appear
        # on stdout. For search commands, structured file:line:content hits of
        # literal "error" are ordinary search hits, not tool failures.
        _is_search_cmd = _detect_pattern(compress_command) == "search_results"
        if _stdout_has_error_patterns(cleaned_stdout, is_search=_is_search_cmd):
            if _try_large_output_policy(command, stdout, stderr, payload, _nudge, _cov):
                return
            _cov("error_or_short", "error_words", text=stdout)
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return  # Error on stdout: pass through raw

        # Run the standard compression pipeline
        compressed = compress(compress_command, cleaned_stdout, returncode=exit_code, stderr=stderr)

        # Best single-output representation: the compressed preview when it
        # actually shrank the output by >=10%, else the raw output.
        from token_estimate import estimate_tokens as _est
        orig_tokens = _est(cleaned_stdout)
        comp_helped = bool(compressed) and compressed != cleaned_stdout and (
            orig_tokens == 0 or (1.0 - _est(compressed) / orig_tokens) >= 0.10)
        best = compressed if comp_helped else cleaned_stdout

        # Cross-turn dedup: if this command's output repeats a recent same-session
        # run, emit a compact delta reference instead. Always compare against the
        # full ANSI-stripped stdout (never a lossy preview) so changed-line diffs
        # are exact; prefer dedup only when it beats single-output compression.
        deduped = _crossturn_dedup(command, cleaned_stdout, str(payload.get("tool_use_id", "") or ""))
        _log_feature = "bash_search_noise_fold" if (comp_helped and "[Folded noisy matches:" in best) else "bash_compress_pipeline"
        if deduped is not None and len(deduped) < len(best):
            best, comp_helped = deduped, True
            _log_feature = "crossturn_dedup"
        else:
            try:
                from command_compress_extra import feature_for_command as _extra_feat
                _log_feature = _extra_feat(compress_command, cleaned_stdout) or "bash_compress_pipeline"
            except Exception:
                pass

        if not comp_helped:
            if _try_large_output_policy(command, stdout, stderr, payload, _nudge, _cov):
                return
            _cov("under_10pct", "read_only", _detect_pattern(command), stdout)
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return  # nothing shrank the output -> pass through raw
        compressed = best

        # Enforce visibility rule (a)+(b)+(c): never hide critical lines and
        # declare exact hidden count + line range before attaching the pointer.
        try:
            from visibility_rule import enforce_visibility_and_exact_hidden as _vis_guard
            compressed = _vis_guard(
                cleaned_stdout, compressed, is_search=_is_search_cmd,
                skip_hidden_check=(_log_feature in ("crossturn_dedup", "cmd_git_diff_stat", "cmd_git_ls")),
            )
        except Exception:
            pass

        # --- archive raw stdout + attach a retrieval pointer ---
        # Rule (d): every compressed output must have a verified archive entry
        # and exact runnable expand command; if storage write fails or entry was
        # pruned, return original stdout untouched.
        _archive_key = None
        try:
            from archive_result import (
                archive_entry_exists,
                archive_original,
                build_archive_pointer,
            )
            _session_id = os.environ.get("CLAUDE_SESSION_ID", "")
            _archive_key = hashlib.sha256(
                f"{_session_id}|{command}|{stdout}".encode("utf-8", errors="replace")
            ).hexdigest()[:16]
            if archive_original(stdout, _session_id, _archive_key, "Bash") is not None and archive_entry_exists(_session_id, _archive_key):
                compressed = build_archive_pointer(compressed, len(stdout), _archive_key)
            else:
                _archive_key = None
        except Exception:
            _archive_key = None

        if _archive_key is None:
            compressed = stdout

        # --- enforce the baseline-size invariant ---
        # If our compressed preview + archive pointer would exceed what
        # Claude Code would show as a baseline stub, shrink to fit or pass raw.
        try:
            compressed = _enforce_baseline_invariant(compressed, stdout, _archive_key)
        except Exception:
            pass

        # If archive failed or baseline guard fell back to raw stdout, do not
        # emit updatedToolOutput or log a bogus compression event.
        if compressed == stdout:
            _cov("under_10pct", "read_only", _detect_pattern(command), stdout)
            if _nudge:
                _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
            return

        # Log compression event to trends.db
        _log_event(command, cleaned_stdout, compressed, feature=_log_feature)
        _cov("compressed",
             "crossturn_dedup" if _log_feature == "crossturn_dedup" else "read_only",
             "crossturn_dedup" if _log_feature == "crossturn_dedup" else _detect_pattern(command),
             stdout, _removed_tokens(cleaned_stdout, compressed))

        # Emit updatedToolOutput to replace what Claude sees. The thrash nudge,
        # if present, goes out as additionalContext in the same envelope (every
        # host honors it), NOT appended to the compressed stdout, so Claude Code
        # users see the nudge exactly once and Codex/Cowork users see it too.
        _emit_updated_tool_output(compressed, stderr, nudge=_nudge)

    except Exception:
        _cov("under_10pct", "hook_error", text=stdout)
        if _nudge:
            _emit_updated_tool_output(stdout, stderr, nudge=_nudge)
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
        # case-insensitive patterns in a scoped inline (?i:...) group. A
        # global re.I would silently upgrade the case-sensitive patterns
        # (\bFAILED\b,
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


def _stdout_has_error_patterns(stdout: str, *, is_search: bool = False) -> bool:
    """Check stdout for error patterns (covers 2>&1 redirect case).

    When ``is_search`` is True, structured ``file:line:content`` search matches
    are skipped so grepping for literal ``error`` does not disable compression.
    """
    if not stdout or len(stdout) < 500:
        return False
    try:
        try:
            from visibility_rule import is_search_match_line as _is_hit
        except Exception:
            _is_hit = None
        combined = _get_combined_error_re()
        lines = stdout.splitlines()
        total_lines = len(lines)
        threshold = total_lines * 0.10
        match_count = 0
        if not combined:
            from bash_compress import _ERROR_STDERR_PATTERNS
            for line in lines:
                if is_search and _is_hit is not None and _is_hit(line):
                    continue
                for pat in _ERROR_STDERR_PATTERNS:
                    if pat.search(line):
                        match_count += 1
                        break
            return match_count >= 3 and match_count > threshold
        for line in lines:
            if is_search and _is_hit is not None and _is_hit(line):
                continue
            if combined.search(line):
                match_count += 1
                if match_count >= 3 and match_count > threshold:
                    return True
        return match_count >= 3 and match_count > threshold
    except Exception:
        return False


def _dedup_store_id(session_id: str, agent_id: str) -> str:
    """Return the storage identity for one model-visible conversation context.

    Main-agent calls retain the historical session id exactly. Explicit agents
    use a short digest so arbitrary host identifiers cannot create invalid or
    excessively long SQLite filenames. A supplied agent_id is an OPAQUE identity:
    strip is consulted ONLY to decide emptiness, never to normalize. A
    whitespace-only agent_id is not a real identity, so it falls back to the
    session-only id and a host that sends "  " cannot fork the main agent off its
    own history. A non-empty id is hashed exactly as received (un-stripped), so
    " agent-A ", "agent-A", and "agent-A " are three DISTINCT stores.
    """
    stripped = (agent_id or "").strip()
    if not stripped or agent_id == _MAIN_AGENT_SENTINEL:
        return session_id
    agent_digest = hashlib.sha256(
        agent_id.encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    return f"{session_id}-agent-{agent_digest}"


def _crossturn_dedup(command: str, output: str, tool_use_id: str = ""):
    """Return a compact delta-reference when this command's output repeats a
    recent same-session run, else None.

    Delegates to ``bash_rerun_dedupe.dedupe_rerun`` so normalization, bounded
    storage, exact-diff checks, and redaction live in a dedicated module.
    Never raises (fail-open): any trouble returns None and normal output stands.
    """
    try:
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        if not session_id:
            return None
        agent_id = os.environ.get(_DEDUP_AGENT_ENV, _MAIN_AGENT_SENTINEL)
        store_id = _dedup_store_id(session_id, agent_id)
        from bash_rerun_dedupe import dedupe_rerun
        return dedupe_rerun(
            command,
            output,
            session_id=session_id,
            store_id=store_id,
            tool_use_id=tool_use_id,
            legacy_twin_seconds=_LEGACY_TWIN_SECONDS,
        )
    except Exception as exc:
        try:
            sys.stderr.write(
                f"[Token Optimizer] crossturn dedup failed: "
                f"{type(exc).__name__}\n"
            )
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        return None


def _holdout_control() -> bool:
    """True only when the holdout is on and this session is in the control arm."""
    try:
        if not os.environ.get("TOKEN_OPTIMIZER_HOLDOUT", "").strip():
            return False
        from holdout import is_control
        return is_control(os.environ.get("CLAUDE_SESSION_ID", ""))
    except Exception:
        return False


def _shadow_bash(command: str, stdout: str, exit_code: int | None) -> None:
    """Control arm: record what compression would have removed. Emits nothing,
    archives nothing, writes no coverage-funnel row. Fail-open."""
    try:
        from coverage_funnel import classify_result
        verdict = classify_result(command, stdout, is_error=bool(exit_code))
        if verdict["gate"] != "compressed":
            return
        from compression_log import log_compression_event
        from credential_patterns import redact_credentials as _redact
        log_compression_event(
            feature=("build_output_compress" if verdict["reason"] == "build_output"
                     else "bash_compress_pipeline"),
            session_id=os.environ.get("CLAUDE_SESSION_ID", ""),
            command_pattern=_redact(command)[:100],
            original_tokens=verdict["tokens"],
            compressed_tokens=max(0, verdict["tokens"] - verdict["removed"]),
            quality_preserved=True, verified=False, tier="opportunity",
            detail="holdout_control",
        )
    except Exception:
        pass


def _removed_tokens(original: str, compressed: str) -> int:
    """Tokens the compressed form saves, never negative. Fail-open to 0."""
    try:
        from coverage_funnel import tokens_of
        return max(0, tokens_of(original) - tokens_of(compressed))
    except Exception:
        return 0


def _record_coverage(gate: str, reason: str, pattern: str, text: str,
                     removed: int) -> None:
    """Write one coverage-funnel counter row (see coverage_funnel). Never raises."""
    try:
        from coverage_funnel import record, tokens_of
        record(gate, reason, pattern or "", tokens_of(text), removed,
               os.environ.get("CLAUDE_SESSION_ID", ""))
    except Exception:
        pass


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


def _try_build_output_compress(command: str, stdout: str) -> str | None:
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
        compressed = _build_compress(command, cleaned_stdout)
        if not compressed or compressed == cleaned_stdout:
            return None
        from post_run_classify import echo_boundary_preserved
        if not echo_boundary_preserved(command, cleaned_stdout, compressed):
            return None

        # Check that compression actually helped by >= 10%
        from token_estimate import estimate_tokens as _est
        orig_tokens = _est(cleaned_stdout)
        if orig_tokens > 0:
            comp_tokens = _est(compressed)
            if (1.0 - comp_tokens / orig_tokens) < 0.10:
                return None

        # Archive raw stdout + attach retrieval pointer (same path as read-only)
        # F6: if archiving was attempted but failed (exception, or archive
        # returned None, or the entry was pruned after write), return None so
        # the caller serves raw stdout. Never return a lossy preview with no
        # archive pointer — the model would have no way to retrieve the original.
        _archive_attempted = True
        _archive_key = None
        if _archive_attempted:
            try:
                from archive_result import (
                    archive_entry_exists,
                    archive_original,
                    build_archive_pointer,
                )
                _session_id = os.environ.get("CLAUDE_SESSION_ID", "")
                _key = hashlib.sha256(
                    f"{_session_id}|{command}|{stdout}".encode("utf-8", errors="replace")
                ).hexdigest()[:16]
                # _archive_key stays None until both archive write and
                # existence check succeed, so any failure path (exception,
                # archive_original returning None, entry pruned) leaves it
                # None and the post-block check returns raw.
                if archive_original(stdout, _session_id, _key, "Bash") is not None:
                    if archive_entry_exists(_session_id, _key):
                        compressed = build_archive_pointer(compressed, len(stdout), _key)
                        _archive_key = _key
            except Exception:
                pass

        # F6: archive failure -> raw, never lossy preview.
        if _archive_attempted and _archive_key is None:
            return None

        # Enforce the baseline-size invariant
        try:
            compressed = _enforce_baseline_invariant(compressed, stdout, _archive_key)
        except Exception:
            pass

        # Log to trends.db with the dedicated B2 feature when applicable, else build_output_compress
        _build_feature = "build_output_compress"
        try:
            from command_compress_extra import feature_for_command as _extra_feat
            _build_feature = _extra_feat(command, cleaned_stdout) or "build_output_compress"
        except Exception:
            pass
        _log_event(command, cleaned_stdout, compressed, feature=_build_feature)

        return compressed
    except Exception:
        return None


def _try_large_output_policy(command: str, stdout: str, stderr: str,
                             payload: dict, nudge: str | None,
                             cov_fn) -> bool:
    """Run large_output_policy after existing compressors (shadow by default).

    Returns True only in active mode when compression + archival succeeded and
    updatedToolOutput was emitted. In shadow mode always returns False so the
    caller's output and coverage path stay byte-identical to today.
    """
    if not stdout or len(stdout.encode("utf-8", errors="replace")) <= 8192:
        return False
    try:
        from large_output_policy import FEATURE_NAME, handle_post_tool_use
        compressed, cleaned_orig = handle_post_tool_use(
            command,
            stdout,
            session_id=os.environ.get("CLAUDE_SESSION_ID", ""),
            tool_use_id=str(payload.get("tool_use_id", "") or ""),
        )
        if compressed is None:
            return False
        _log_event(command, cleaned_orig, compressed, feature=FEATURE_NAME)
        cov_fn("compressed", FEATURE_NAME, FEATURE_NAME, stdout,
               _removed_tokens(cleaned_orig, compressed))
        _emit_updated_tool_output(compressed, stderr, nudge=nudge)
        return True
    except Exception:
        return False


def _emit_updated_tool_output(stdout: str, stderr: str,
                              nudge: str | None = None) -> None:
    """Emit the PostToolUse updatedToolOutput envelope, with an optional nudge
    carried as additionalContext.

    The compression/replacement goes in ``updatedToolOutput`` (honored by Claude
    Code; ignored as an unrecognized field by Codex/Cowork, where long-output
    collapse is via ``archive_result`` archival instead). The nudge goes in
    ``additionalContext``, which every host honors, so burn/inline nudges reach
    the model on Claude Code AND Codex AND Cowork. The nudge is NOT duplicated
    inside ``updatedToolOutput``, so Claude Code users see it exactly once.

    ``TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT`` is checked in ``main()`` before
    any compression compute, so this function is only reached on hosts that
    honor ``updatedToolOutput`` (Claude Code) or on Codex-via-marketplace (which
    shares the byte-identical hooks.json and ignores the field harmlessly). The
    codex-install profile sets the env flag to skip the compression compute as a
    perf optimization; the nudge still goes out via ``additionalContext`` from
    the early-return path in ``main()``.
    """
    envelope: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": {
                "stdout": stdout,
                "stderr": stderr,
                "interrupted": False,
                "isImage": False,
            },
        }
    }
    if nudge:
        envelope["hookSpecificOutput"]["additionalContext"] = nudge
    # A broken custom pattern file keeps redacted writes out of every cache —
    # surface that once per file hash as a user-visible systemMessage.
    _redact_warn = _pending_redaction_warning()
    if _redact_warn:
        envelope["systemMessage"] = _redact_warn
    print(json.dumps(envelope))


if __name__ == "__main__":
    main()
