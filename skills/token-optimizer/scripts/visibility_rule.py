#!/usr/bin/env python3
"""Shared visibility rule for all Token Optimizer compressors.

Rule enforced across every compressor:
  a) Lines showing an error, failure, exception (including CamelCase names like
     ValueError / TimeoutException), traceback, panic, failing test, or
     non-zero exit stay visible verbatim.
  b) Ordinary hidden content is allowed ONLY when the output states exactly
     what was hidden, by count and place (e.g. "21 more matches in src/x.py
     (lines 40-212)"), followed by the exact runnable expand command.
  c) Counts must exactly match what was hidden; output stays deterministic;
     fail open on any error.
  d) If expand storage write fails or the entry was pruned, return the original
     output untouched.

Search-hit distinction:
  Grep/ripgrep/git-grep match lines in structured ``file:line:content`` form
  (where the path is non-empty, not numeric, not a tool diagnostic prefix such
  as ``rg:``/``grep:``/``git:``/``fatal:``/``error:``, and not a Python stack
  frame ``File "...", line N``) are ordinary search hits even when the matched
  source code contains the literal word "error" or an exception name. Search
  *failures* are identified by non-zero exit code, non-empty stderr, or
  non-match diagnostic/error lines on stdout, all of which stay visible verbatim
  (or pass through raw).
"""

from __future__ import annotations

import re

# Structured grep/rg/ag/ack/git-grep match line: file:line:content
_SEARCH_MATCH_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\n]+):(\d+):(.*)$")

# Tool-diagnostic prefixes that can look superficially like `word:digits:`
# or appear on stdout when stderr is merged (2>&1).
_SEARCH_TOOL_ERROR_PREFIXES = (
    "rg:",
    "grep:",
    "egrep:",
    "fgrep:",
    "ag:",
    "ack:",
    "git:",
    "fatal:",
    "error:",
    "warning:",
    "traceback",
)

# Python stack frame line (never a grep match even though it has colons/digits).
_PY_FRAME_RE = re.compile(r'^\s*File\s+"[^"]+",\s+line\s+\d+')

# CamelCase exception/error class names: ValueError, TimeoutException,
# NullPointerException, AssertionError, SegmentationFault, etc.
_EXCEPTION_NAME_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Fault|Panic|Failure)\b"
)

# Non-zero exit status indicators (never match HTTP status=200).
_NONZERO_EXIT_RE = re.compile(
    r"(?i)(?:"
    r"\bexit(?:ed)?(?:\s+with)?(?:\s+(?:code|status))?\s*[:=]?\s*[1-9]\d*\b|"
    r"\breturn(?:ed)?(?:\s+(?:code|status))?\s*[:=]?\s*[1-9]\d*\b|"
    r"\bexit[_-]?(?:code|status)\s*[:=]\s*[1-9]\d*\b|"
    r"\bprocess\s+completed\s+with\s+exit\s+code\s+[1-9]\d*\b"
    r")"
)

# Strict error/exception/traceback/panic/non-zero-exit pattern (safe even on
# lockfiles where package names like "http-errors" appear).
_STRICT_ERROR_RE = re.compile(
    r"(?:"
    r"Traceback \(most recent call last\):|"
    r"^\s*File \"[^\"]+\", line \d+|"
    r"^\s+at\s+\S+\s*\([^)]+:\d+|"
    r"^Caused by:|"
    r"^\s*goroutine \d+|"
    r"##\[error\]|"
    r"\bpanicked at\b|"
    r"(?i:\b(?:panic|fatal(?:\s+error)?)\s*:)|"
    r"^\s*--- FAIL:\s+|"
    r"^FAIL\t\S+|"
    r"^test .+? \.\.\. FAILED\s*$|"
    r"^\s*not ok \d+\s+-|"
    r"^\s*[X✗✖]\s+"
    r")"
)

# General error/failure/exception/traceback/panic/failing-test pattern.
_GENERAL_ERROR_RE = re.compile(
    r"(?:"
    r"##\[(?:error|warning)\]|"
    r"(?i:\b(?:error|errors|err!|fatal|panic|panicked|fail|failed|failing|"
    r"failure|failures|exception|traceback|segfault|aborted|oomkilled|"
    r"crashloopbackoff|imagepullbackoff|evicted|unhealthy|timed\s+out|timeout|"
    r"permission\s+denied|access\s+denied|connection\s+refused)\b)|"
    r"^\s*E\s+(?:assert\b|[A-Z][A-Za-z0-9_]*(?:Error|Exception))|"
    # Localized / foreign error markers (mirrors bash_compress._ERROR_STDERR_PATTERNS
    # and _FOREIGN_ERROR_PATTERNS).
    r"(?i:\b(?:fehler|erreur|errore|ошибка|fout|fel|virhe|hiba|hata|erro|blad|l\u1ed7i)\s*[:：])|"
    r"错误\s*[:：]|錯誤\s*[:：]|エラー\s*[:：]|오류\s*[:：]|"
    r"\u0e02\u0e49\u0e2d\u0e1c\u0e34\u0e14\u0e1e\u0e25\u0e32\u0e14|"
    r"\u05e9\u05d2\u05d9\u05d0\u05d4|"
    r"\u062e\u0637\u0623|"
    r"\u062e\u0637\u0627"
    r")"
)


def is_search_match_line(line: str) -> bool:
    """Return True iff ``line`` is a structured ``file:line:content`` search hit.

    Distinguishes ordinary grep/rg/git-grep matches of literal words like
    ``error`` or ``ValueError`` inside source code from actual tool failures
    (``rg: ...``, ``grep: ...``, tracebacks, exit-code lines).
    """
    if not line:
        return False
    stripped = line.lstrip()
    low = stripped.lower()
    if low.startswith(_SEARCH_TOOL_ERROR_PREFIXES):
        return False
    if _PY_FRAME_RE.match(line):
        return False
    m = _SEARCH_MATCH_RE.match(line)
    if not m:
        return False
    fname = m.group(1).strip()
    if not fname or fname.isdigit():
        return False
    # Reject lines where the "filename" contains whitespace-only or starts with
    # common diagnostic words followed by space (e.g. "Error in file:10: ...").
    if " " in fname and not ("/" in fname or "\\" in fname):
        return False
    return True


def extract_search_line_number(line: str) -> int | None:
    """Extract the 1-based line number from a ``file:line:content`` search hit."""
    m = _SEARCH_MATCH_RE.match(line)
    if not m:
        return None
    try:
        return int(m.group(2))
    except (TypeError, ValueError):
        return None


def is_error_or_failure_line(
    line: str,
    *,
    strict_exception_only: bool = False,
    include_stack_frames: bool = True,
) -> bool:
    """Return True if ``line`` shows an error, failure, exception, traceback,
    panic, failing test, or non-zero exit that must stay visible verbatim.
    """
    if not line or not line.strip():
        return False
    if not include_stack_frames and _PY_FRAME_RE.match(line):
        return False
    if _EXCEPTION_NAME_RE.search(line):
        return True
    if _NONZERO_EXIT_RE.search(line):
        return True
    if _STRICT_ERROR_RE.search(line):
        return True
    if strict_exception_only:
        return False
    return bool(_GENERAL_ERROR_RE.search(line))


def format_line_range(line_numbers: list[int]) -> str:
    """Format a non-empty sequence of 1-based line numbers as ``line N`` or ``lines A-B``."""
    if not line_numbers:
        return ""
    lo = min(line_numbers)
    hi = max(line_numbers)
    if lo == hi:
        return f"line {lo}"
    return f"lines {lo}-{hi}"
