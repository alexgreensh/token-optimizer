#!/usr/bin/env python3
"""Large Bash output policy (opt-in via TOKEN_OPTIMIZER_LARGE_OUTPUT=1, default OFF).

Evaluates Bash stdout > 8 KB that no existing compressor handled — including
chained commands, loops, subshells, and heredocs — while excluding explicit
narrow reads (sed -n, head -n, tail -n, cut ranges, awk NR ranges),
binary-like output, and already-archived output.

Two states only via `TOKEN_OPTIMIZER_LARGE_OUTPUT`:
  - Off (default, unset or != '1'): returns immediately after a cheap env check;
    byte-identical to not having the feature. No logging, no archival, no I/O.
  - On (`TOKEN_OPTIMIZER_LARGE_OUTPUT=1`):
    - Archives raw stdout FIRST and verifies the archive entry exists; if
      archiving fails, returns None so the caller serves original stdout
      untouched.
    - Emits first 25 + last 25 lines, every section/echo header, every critical
      line, an exact declaration of hidden lines by count and line range, and
      the exact runnable expand command.
    - Passes through the shared emit safety verifier (`emit_safety_gate`).
    - Requires >= 15% and >= 200 tokens net saving after the expand pointer.
    - Deterministic and fail-open on any error.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

FEATURE_NAME = "large_output_policy"
ENABLED_ENV = "TOKEN_OPTIMIZER_LARGE_OUTPUT"

MIN_BYTES = 8192
HEAD_LINES = 25
TAIL_LINES = 25
CRITICAL_CONTEXT_BEFORE = 3
CRITICAL_CONTEXT_AFTER = 4
MIN_SAVING_RATIO = 0.15
MIN_SAVING_TOKENS = 200

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Explicit narrow-read exclusions (word-boundary / shell command aware).
_CMD_PREFIX = r"(?:^|[;&|\n]|\|\||&&)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
_SED_N_RE = re.compile(_CMD_PREFIX + r"(?:g?sed)\s+(?:[^;&|\n]*?\s)?-[a-zA-Z]*n\b")
_HEAD_N_RE = re.compile(
    _CMD_PREFIX + r"(?:g?head)\s+(?:[^;&|\n]*?\s)?(?:-n\s*\S+|--lines(?:=|\s+)\S+|-\d+\b)"
)
_TAIL_N_RE = re.compile(
    _CMD_PREFIX + r"(?:g?tail)\s+(?:[^;&|\n]*?\s)?(?:-n\s*\S+|--lines(?:=|\s+)\S+|-\d+\b)"
)
_CUT_RANGE_RE = re.compile(
    _CMD_PREFIX + r"(?:g?cut)\s+(?:[^;&|\n]*?\s)?(?:-[fcb]\s*\S+|--(?:fields|characters|bytes)(?:=|\s+)\S+)"
)
_AWK_NR_RE = re.compile(
    _CMD_PREFIX + r"(?:g?awk|mawk|nawk)\b[^;&|\n]*\b(?:F?NR)\s*(?:==|!=|<=|>=|<|>)"
)
# Deliberate content reads that must never be chopped: bare cat / cat chains,
# jq output, and inline-script / heredoc table dumps.
_JQ_CMD_RE = re.compile(_CMD_PREFIX + r"(?:jq|yq)\b")
_INLINE_SCRIPT_READ_RE = re.compile(
    _CMD_PREFIX + r"(?:python3?(?:\.\d+)?\s+(?:[^;&|\n]*?-c\b|-\s*<<)|(?:node|ruby|perl)\s+[^;&|\n]*?-e\b)"
)
_CAT_SEG_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:cd|cat|nl|bat)\b")
_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")

# Section / command-boundary header patterns (P1 + P2 header preservation).
_SECTION_HEADER_RE = re.compile(
    r"(?:"
    r"={3,}|"
    r"^-{3,}(?:\s|$|-)|"
    r"\s-{3,}\s|"
    r"^diff --git\s+|"
    r"^\+\+\+\s+[ab]/|"
    r"^---\s+[ab]/|"
    r"^##\[(?:group|endgroup)\]|"
    r"^(?:Step|Stage|Phase)\s+\d+(?:/\d+)?\b|"
    r"^\[(?:Step|Stage|Phase)\s+\d+|"
    r"^#{1,3}\s+\S|"
    r"^(?:\+\s+\S|\$\s+\S|>>>\s+\S|\*\*\*\s+\S)"
    r")"
)

# Echo / printf literal boundary extraction from command text.
_ECHO_STMT_RE = re.compile(
    r"(?:^|[;&\n]|&&|\|\|)\s*(?:echo|printf)\s+(?:-e\s+|-n\s+)?(?:'([^'\n]{2,120})'|\"([^\"\n]{2,120})\"|([A-Za-z0-9_=.:/#-]{3,120}))"
)

# Additional critical-line guards supplementing visibility_rule.
_EXTRA_CRITICAL_RE = re.compile(
    r"(?:"
    r"(?i:\bnon-?zero\s+exit\b)|"
    r"(?i:\bexit\s+(?:code|status)\s+[1-9]\d*\b)|"
    r"^FAILED\s+\S+|"
    r"^\s*FAIL\s+\S+|"
    r"\bAssertionError\b|"
    r"(?i:\bcommand\s+failed\b)"
    r")"
)


def is_enabled() -> bool:
    """Return True only when explicitly enabled via TOKEN_OPTIMIZER_LARGE_OUTPUT=1."""
    return os.environ.get(ENABLED_ENV, "").strip() == "1"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _tokens(text: str) -> int:
    if not text:
        return 0
    try:
        from token_estimate import estimate_tokens
        return int(estimate_tokens(text))
    except Exception:
        return max(1, len(text.encode("utf-8", errors="replace")) // 4)


def is_already_archived(text: str) -> bool:
    """True when output already carries a Token Optimizer archive/policy marker."""
    if not text:
        return False
    return (
        "[Full result archived" in text
        or "[bash_compress]" in text
        or "[large_output_policy:" in text
    )


def is_binary_like(text: str) -> bool:
    """True for NUL-bearing or heavily non-printable/replacement-char outputs."""
    if not text:
        return False
    if "\x00" in text:
        return True
    sample = _strip_ansi(text[:4096])
    if not sample:
        return False
    ctrl = sum(1 for ch in sample if ord(ch) < 32 and ch not in ("\t", "\n", "\r"))
    if ctrl / len(sample) > 0.10:
        return True
    repl = sample.count("\ufffd")
    if repl / len(sample) > 0.15:
        return True
    return False


def is_explicit_narrow_read(command: str) -> bool:
    """True for explicit narrow-read commands (sed -n, head -n, tail -n, cut, awk NR)."""
    if not command or not isinstance(command, str):
        return False
    try:
        from pipe_file_compress import has_explicit_output_limit
        if has_explicit_output_limit(command):
            return True
    except Exception:
        pass
    if _SED_N_RE.search(command):
        return True
    if _HEAD_N_RE.search(command):
        return True
    if _TAIL_N_RE.search(command):
        return True
    if _CUT_RANGE_RE.search(command):
        return True
    if _AWK_NR_RE.search(command):
        return True
    return False


def is_deliberate_content_read(command: str) -> bool:
    """True for deliberate content reads (bare cat, cat chains, jq, inline-script dumps)."""
    if not command or not isinstance(command, str):
        return False
    if _JQ_CMD_RE.search(command):
        return True
    if _INLINE_SCRIPT_READ_RE.search(command):
        return True
    # Bare cat or chain of cd/cat/nl/bat segments (e.g. `cat a.py`, `cd /repo && cat a && cat b`).
    segs = [s.strip() for s in _CHAIN_SPLIT_RE.split(command.strip()) if s.strip()]
    if segs and any(re.match(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:cat|nl|bat)\b", s) for s in segs):
        if all(_CAT_SEG_RE.match(s) and "|" not in s for s in segs):
            return True
    return False


def _extract_echo_markers(command: str) -> frozenset[str]:
    """Extract literal echo/printf section labels from the command string."""
    if not command:
        return frozenset()
    markers: set[str] = set()
    for m in _ECHO_STMT_RE.finditer(command):
        val = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        val = val.replace("\\n", "").strip()
        if len(val) >= 2:
            markers.add(val)
    return frozenset(markers)


def is_section_header(line: str, echo_markers: frozenset[str] = frozenset()) -> bool:
    """True when ``line`` is a section/echo/diff/command boundary header."""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    if echo_markers and stripped in echo_markers:
        return True
    return bool(_SECTION_HEADER_RE.search(stripped))


def is_critical_line(line: str) -> bool:
    """True when ``line`` carries an error, failure, exception, traceback, panic,
    failing test, or non-zero exit that must remain visible verbatim."""
    if not line or not line.strip():
        return False
    try:
        from visibility_rule import is_error_or_failure_line
        if is_error_or_failure_line(line):
            return True
    except Exception:
        pass
    return bool(_EXTRA_CRITICAL_RE.search(line))


def _format_hidden_ranges(runs: list[tuple[int, int]]) -> str:
    """Format 0-based inclusive hidden runs as exact 1-based line ranges."""
    parts: list[str] = []
    for start_0, end_0 in runs:
        lo, hi = start_0 + 1, end_0 + 1
        parts.append(f"line {lo}" if lo == hi else f"lines {lo}-{hi}")
    return ", ".join(parts)


def _attach_pointer(preview_body: str, original_chars: int, archive_key: str) -> str:
    """Attach the exact expand pointer using archive_result.build_archive_pointer."""
    try:
        from archive_result import build_archive_pointer
        return build_archive_pointer(preview_body, original_chars, archive_key)
    except Exception:
        script_path = Path(__file__).resolve().parent / "archive_result.py"
        return (
            f"{preview_body}\n\n"
            f"[Full result archived ({original_chars:,} chars) — saved to disk, not lost.\n"
            f'Run: python3 "{script_path}" expand {archive_key}\n]'
        )


def evaluate(
    command: str,
    stdout: str,
    *,
    archive_key: str | None = None,
) -> dict[str, Any] | None:
    """Evaluate eligibility and build the deterministic compressed preview.

    Pure and fail-open: performs zero disk I/O and returns ``None`` if the
    output is ineligible, has no hidden lines, or fails the >=15% and >=200
    token net saving gate after the expand pointer.
    """
    try:
        if not isinstance(stdout, str) or not stdout:
            return None
        raw_bytes = len(stdout.encode("utf-8", errors="replace"))
        if raw_bytes <= MIN_BYTES:
            return None
        if is_already_archived(stdout):
            return None
        if is_binary_like(stdout):
            return None
        if is_explicit_narrow_read(command or ""):
            return None
        if is_deliberate_content_read(command or ""):
            return None

        cleaned = _strip_ansi(stdout)
        lines = cleaned.splitlines()
        total_lines = len(lines)
        if total_lines <= HEAD_LINES + TAIL_LINES:
            return None

        echo_markers = _extract_echo_markers(command or "")
        kept: set[int] = set(range(min(HEAD_LINES, total_lines)))
        kept.update(range(max(0, total_lines - TAIL_LINES), total_lines))

        critical_count = 0
        for idx, line in enumerate(lines):
            crit = is_critical_line(line)
            if crit:
                critical_count += 1
                lo_ctx = max(0, idx - CRITICAL_CONTEXT_BEFORE)
                hi_ctx = min(total_lines, idx + CRITICAL_CONTEXT_AFTER + 1)
                kept.update(range(lo_ctx, hi_ctx))
            elif idx not in kept and is_section_header(line, echo_markers):
                kept.add(idx)

        hidden_indices = [i for i in range(total_lines) if i not in kept]
        if not hidden_indices:
            return None

        # Group hidden indices into contiguous inclusive 0-based runs.
        hidden_runs: list[tuple[int, int]] = []
        run_start = hidden_indices[0]
        run_prev = hidden_indices[0]
        for idx in hidden_indices[1:]:
            if idx == run_prev + 1:
                run_prev = idx
            else:
                hidden_runs.append((run_start, run_prev))
                run_start = run_prev = idx
        hidden_runs.append((run_start, run_prev))

        run_by_start = {s: e for s, e in hidden_runs}
        out_lines: list[str] = []
        i = 0
        while i < total_lines:
            if i in kept:
                out_lines.append(lines[i])
                i += 1
            else:
                end_0 = run_by_start[i]
                count = end_0 - i + 1
                lo, hi = i + 1, end_0 + 1
                if count == 1:
                    out_lines.append(f"[... 1 line hidden: line {lo} ...]")
                else:
                    out_lines.append(f"[... {count} lines hidden: lines {lo}-{hi} ...]")
                i = end_0 + 1

        hidden_lines = len(hidden_indices)
        ranges_str = _format_hidden_ranges(hidden_runs)
        out_lines.append(
            f"[large_output_policy: {hidden_lines} of {total_lines} lines hidden "
            f"({ranges_str}); {critical_count} critical line(s) preserved]"
        )
        preview_body = "\n".join(out_lines)
        key = archive_key or ("0" * 16)
        preview_with_pointer = _attach_pointer(preview_body, len(stdout), key)

        tokens_before = _tokens(cleaned)
        tokens_after = _tokens(preview_with_pointer)
        saved_tokens = tokens_before - tokens_after
        if tokens_before <= 0 or saved_tokens < MIN_SAVING_TOKENS:
            return None
        if (saved_tokens / tokens_before) < MIN_SAVING_RATIO:
            return None

        return {
            "bytes": raw_bytes,
            "lines": total_lines,
            "hidden_lines": hidden_lines,
            "hidden_ranges": ranges_str,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "saved_tokens": saved_tokens,
            "critical_lines": critical_count,
            "preview_body": preview_body,
            "preview_with_pointer": preview_with_pointer,
        }
    except Exception:
        return None


def handle_post_tool_use(
    command: str,
    stdout: str,
    *,
    session_id: str = "",
    tool_use_id: str = "",
    exit_code: int | None = None,
) -> tuple[str | None, str]:
    """PostToolUse entry point wired after existing Bash compressors.

    Returns ``(compressed_output_or_None, cleaned_original)``.
    - Off (default, ``TOKEN_OPTIMIZER_LARGE_OUTPUT`` != ``'1'``): returns
      ``(None, "")`` immediately with zero work.
    - On (``TOKEN_OPTIMIZER_LARGE_OUTPUT=1``): archives raw stdout FIRST,
      verifies the archive entry exists (failing open to None on any error),
      runs the shared emit safety verifier, and returns
      ``(compressed_with_pointer, cleaned)``.
    """
    try:
        if not is_enabled():
            return None, ""
        if not isinstance(stdout, str) or len(stdout.encode("utf-8", errors="replace")) <= MIN_BYTES:
            return None, ""

        archive_key = hashlib.sha256(
            f"{session_id}|{command}|{stdout}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        ev = evaluate(command, stdout, archive_key=archive_key)
        if ev is None:
            return None, ""

        from archive_result import archive_entry_exists, archive_original
        if archive_original(stdout, session_id, archive_key, "Bash") is None:
            return None, ""
        if not archive_entry_exists(session_id, archive_key):
            return None, ""

        compressed = ev["preview_with_pointer"]
        try:
            from bash_compress import _enforce_baseline_invariant
            compressed = _enforce_baseline_invariant(compressed, stdout, archive_key)
        except Exception:
            pass
        from emit_safety_gate import gate_or_none as _emit_gate
        compressed = _emit_gate(
            stdout, compressed, feature=FEATURE_NAME,
            tool_name="Bash", command=command, exit_code=exit_code, require_expand=True,
        )
        if compressed is None:
            return None, ""
        return compressed, _strip_ansi(stdout)
    except Exception:
        return None, ""


def compress_benchmark_fixture(fixture: dict[str, Any]) -> str:
    """Benchmark adapter for ``benchmark.py`` external fixtures (no disk I/O)."""
    cmd = str(fixture.get("command", ""))
    raw = str(fixture.get("raw_output", ""))
    ev = evaluate(cmd, raw, archive_key="bench00000000001")
    if ev is None:
        return raw
    return ev["preview_with_pointer"]
