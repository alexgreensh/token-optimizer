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


_BENIGN_ZERO_ERR_RE = re.compile(
    r"\b(?:0|no|zero|without)\s+(?:errors?|failures?|failed)\b|"
    r"\b(?:error_count|errors|failures|failed)\s*[:=]\s*0\b",
    re.I,
)

# Failure lines that carry no literal "error" word (shared with emit_safety_gate).
_NON_ERROR_FAILURE_RE = re.compile(
    r"(?:"
    r"(?i:\bcommand\s+not\s+found\b)|"
    r"(?i:\bno\s+such\s+file\s+or\s+directory\b)|"
    r"^CONFLICT\b|(?i:\bmerge\s+conflict\b)|"
    r"\[rejected\]|"
    r"^\s*Killed(?:\s|$|:)|\bKilled:\s*\d+|"
    r"(?i:\bexceeded\s+its\s+progress\s+deadline\b)|"
    r"(?i:\bno\s+space\s+left\s+on\s+device\b)|"
    r"(?i:\baddress\s+already\s+in\s+use\b)|"
    r"(?i:\bread-only\s+file\s+system\b)|"
    r"(?i:\bcannot\s+(?:access|open|create|remove)\b)|"
    r"(?i:\b(?:could|unable)\s+to\s+resolve\b)"
    r")"
)

_STRICT_PANIC_RE = re.compile(
    r"\b(?:fatal|panic|panicked|segfault|segmentation fault|core dumped|out of memory)\b",
    re.I,
)


def is_strict_critical_line(line: str) -> bool:
    """True for unambiguous hard failures (traceback, fatal/panic, non-zero exit)
    that must stay visible even when matched inside a ``file:line:content`` search hit."""
    if not line or not line.strip():
        return False
    return bool(
        _STRICT_ERROR_RE.search(line)
        or _NONZERO_EXIT_RE.search(line)
        or _STRICT_PANIC_RE.search(line)
    )


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
    if _NON_ERROR_FAILURE_RE.search(line):
        return True
    if strict_exception_only:
        return False
    if _BENIGN_ZERO_ERR_RE.search(line):
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


def format_line_ranges(line_numbers: list[int]) -> str:
    """Format sorted 1-based line numbers into exact contiguous ranges."""
    if not line_numbers:
        return ""
    nums = sorted(set(line_numbers))
    runs: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            runs.append((start, prev))
            start = prev = n
    runs.append((start, prev))
    return ", ".join(f"line {a}" if a == b else f"lines {a}-{b}" for a, b in runs)


_FAST_STRICT_RE = re.compile(
    r"traceback|file \"|at \S+\s*\(|caused by:|goroutine \d+|##\[error\]|"
    r"panicked|panic|fatal|segfault|segmentation fault|core dumped|out of memory|"
    r"--- fail:|fail\t|\.\.\. failed|not ok \d+|exit|return",
    re.I,
)


def find_missing_critical_lines(
    raw_text: str,
    compressed_text: str,
    *,
    is_search: bool = False,
    strict_exception_only: bool = False,
) -> list[tuple[int, str]]:
    """Return ``(1-based line_no, line)`` for every critical line in ``raw_text``
    that is not visible verbatim in ``compressed_text`` (O(N+M))."""
    if not raw_text:
        return []
    # Fast whole-text pre-check for search output: if no strict-critical keyword
    # appears anywhere in raw_text and no non-match diagnostic line is possible,
    # skip per-line regex scanning.
    has_strict_anywhere = bool(_FAST_STRICT_RE.search(raw_text))
    comp_lines = compressed_text.splitlines()
    comp_set = set(comp_lines) | {ln.strip() for ln in comp_lines}
    missing: list[tuple[int, str]] = []
    for idx, line in enumerate(raw_text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if is_search and is_search_match_line(line):
            if not has_strict_anywhere or not is_strict_critical_line(line):
                continue
        elif not is_error_or_failure_line(line, strict_exception_only=strict_exception_only):
            continue
        if line in comp_set or stripped in comp_set:
            continue
        # Fallback substring check only for the few critical lines not matching a whole line.
        if line not in compressed_text and stripped not in compressed_text:
            missing.append((idx, line))
    return missing


_GENERIC_OMIT_LINE_RE = re.compile(
    r"(?m)^\.\.\.\s+(\d+)\s+lines?\s+omitted(?:\s*\([^)]*\))?\s*\.\.\.$"
)
_DIFF_MORE_LINE_RE = re.compile(
    r"(?m)^\.\.\.\s+\((\d+)\s+more\s+lines,\s+([^)]+)\)$"
)
_ANY_DECLARED_HIDE_RE = re.compile(
    r"(?i)(?:"
    r"\.\.\.\s*\(?\d+\s+(?:more\s+)?(?:lines?|matches?|files?|items?|entries|stack frames|non-file lines|preamble lines|extra lines)\b|"
    r"\b\d+\s+(?:more\s+)?(?:lines?|matches?|files?|items?|entries)\s+(?:omitted|hidden|archived|truncated)\b|"
    r"\[Folded noisy matches:\s*\d+|"
    r"\[large_output_policy:\s*\d+\s+of\s+\d+\s+lines\s+hidden|"
    r"plus\s+\d+\s+more\s+files\s+with\s+matches"
    r")"
)
_ARCHIVE_POINTER_START = "\n\n[Full result archived ("


def compress_generic_exact(text: str, *, head_n: int = 20, tail_n: int = 20) -> str:
    """Generic structural/log compression with exact raw-line omission counts
    and zero dropped critical lines."""
    lines = text.splitlines()
    # Collapse consecutive identical lines while tracking exact raw line spans.
    collapsed: list[tuple[str, int, int, int]] = []  # (rendered, start_lno, end_lno, raw_count)
    run_val: str | None = None
    run_start = 1
    run_count = 0

    def _flush(end_lno: int) -> None:
        if run_val is None:
            return
        rendered = f"{run_val}  (x{run_count})" if run_count > 1 else run_val
        collapsed.append((rendered, run_start, end_lno, run_count))

    for idx, ln in enumerate(lines, 1):
        if ln == run_val:
            run_count += 1
        else:
            _flush(idx - 1)
            run_val = ln
            run_start = idx
            run_count = 1
    _flush(len(lines))

    if len(collapsed) <= head_n + tail_n:
        return "\n".join(item[0] for item in collapsed)

    head = collapsed[:head_n]
    tail = collapsed[len(collapsed) - tail_n:]
    middle = collapsed[head_n:len(collapsed) - tail_n]

    kept_middle: list[str] = []
    omitted_raw_lines: list[int] = []
    for rendered, s_lno, e_lno, _cnt in middle:
        if is_error_or_failure_line(rendered) or re.search(r"(?i)\bwarn(?:ing)?s?\b", rendered):
            kept_middle.append(rendered)
        else:
            omitted_raw_lines.extend(range(s_lno, e_lno + 1))

    out = [item[0] for item in head]
    if omitted_raw_lines:
        ranges = format_line_ranges(omitted_raw_lines)
        if kept_middle:
            out.append(
                f"... {len(omitted_raw_lines)} lines omitted ({ranges}; "
                f"kept {len(kept_middle)} error/warning lines) ..."
            )
            out.extend(kept_middle)
        else:
            out.append(f"... {len(omitted_raw_lines)} lines omitted ({ranges}) ...")
    elif kept_middle:
        out.extend(kept_middle)
    out.extend(item[0] for item in tail)
    return "\n".join(out)


def enforce_visibility_and_exact_hidden(
    raw_text: str,
    compressed_text: str,
    *,
    is_search: bool = False,
    skip_hidden_check: bool = False,
) -> str:
    """Enforce zero hidden critical lines and accurate hidden-line declaration.

    Called on compressed Bash output before attaching the archive pointer.
    Deterministic and fail-open.
    """
    if not raw_text or not compressed_text or compressed_text == raw_text:
        return compressed_text

    result = compressed_text

    # 1. Guarantee every critical line in raw_text is visible verbatim.
    missing_crit = find_missing_critical_lines(raw_text, result, is_search=is_search)
    if missing_crit:
        result = result + "\n" + "\n".join(ln for _, ln in missing_crit)

    if skip_hidden_check:
        return result

    # 2. Compute exact hidden raw lines (O(N+M) via line set).
    raw_lines = raw_text.splitlines()
    res_lines = result.splitlines()
    res_set = set(res_lines) | {ln.strip() for ln in res_lines}
    hidden_nos = [
        idx for idx, ln in enumerate(raw_lines, 1)
        if ln.strip() and ln not in res_set and ln.strip() not in res_set
    ]
    if not hidden_nos:
        return result

    exact_count = len(hidden_nos)
    exact_ranges = format_line_ranges(hidden_nos)

    # Fix up known generic/diff omission markers if their count drifted (e.g. after reinject).
    m_gen = _GENERIC_OMIT_LINE_RE.search(result)
    if m_gen:
        declared = int(m_gen.group(1))
        if declared != exact_count or "line" not in m_gen.group(0).split("omitted", 1)[-1]:
            replacement = f"... {exact_count} lines omitted ({exact_ranges}) ..."
            result = result[:m_gen.start()] + replacement + result[m_gen.end():]
        return result

    m_diff = _DIFF_MORE_LINE_RE.search(result)
    if m_diff:
        declared = int(m_diff.group(1))
        stats_tail = m_diff.group(2)
        if declared != exact_count or "lines " not in stats_tail:
            replacement = f"... ({exact_count} more lines, {exact_ranges}, {stats_tail})"
            result = result[:m_diff.start()] + replacement + result[m_diff.end():]
        return result

    # If no hidden-count declaration exists at all, append an exact one.
    if not _ANY_DECLARED_HIDE_RE.search(result):
        result = result + f"\n... {exact_count} lines omitted ({exact_ranges}) ..."

    return result


def enforce_baseline_safe(
    text: str,
    raw_output: str,
    archive_key: str | None,
    *,
    threshold_chars: int = 30_000,
    stub_chars: int = 2200,
    is_search: bool = False,
) -> str:
    """Line-boundary, visibility-preserving baseline-size invariant.

    - For raw outputs <= threshold_chars (30 KB), if compressed+pointer would
      exceed raw_output, return raw_output untouched rather than slicing the
      preview mid-sentence and dropping matches/critical lines/hidden counts.
    - For raw outputs > threshold_chars, if compressed+pointer exceeds stub_chars,
      trim cleanly on line boundaries while keeping every critical line and an
      exact hidden-line declaration + archive pointer; if even those exceed
      stub_chars, return raw_output untouched (fail-open).
    """
    cap = len(raw_output) if len(raw_output) <= threshold_chars else stub_chars
    if len(text) <= cap or text == raw_output:
        return text

    # Short output: compressed + pointer is larger than raw -> serve raw verbatim.
    if len(raw_output) <= threshold_chars:
        return raw_output

    raw_lines = raw_output.splitlines()
    crit_lines: list[tuple[int, str]] = []
    for idx, ln in enumerate(raw_lines, 1):
        if not ln.strip():
            continue
        if is_search and is_search_match_line(ln):
            if is_strict_critical_line(ln):
                crit_lines.append((idx, ln))
            continue
        if is_error_or_failure_line(ln):
            crit_lines.append((idx, ln))

    def _safe_head(s: str, limit: int) -> str:
        from bash_compress import _unicode_safe_head
        return _unicode_safe_head(s, limit)

    if not archive_key:
        if crit_lines:
            return raw_output
        return _safe_head(text, cap)

    try:
        from archive_result import build_archive_pointer
        pointer = build_archive_pointer("", len(raw_output), archive_key)
    except Exception:
        return raw_output

    if not pointer or len(pointer) >= cap:
        return raw_output

    # Strip existing pointer suffix before trimming body on line boundaries.
    body = text
    ptr_pos = body.find(_ARCHIVE_POINTER_START)
    if ptr_pos >= 0:
        body = body[:ptr_pos]

    budget = cap - len(pointer)
    # Single-line preview with no critical lines: cap cleanly on grapheme boundary
    # while keeping the archive pointer intact.
    if "\n" not in body and not crit_lines:
        return _safe_head(body, budget) + pointer

    # Reserve space for critical lines and an exact omission footer.
    crit_block = "\n".join(ln for _, ln in crit_lines)
    reserve_footer = 96
    head_budget = budget - (len(crit_block) + 1 if crit_block else 0) - reserve_footer
    if head_budget < 80:
        return raw_output

    kept_lines: list[str] = []
    used = 0
    for ln in body.splitlines():
        add_len = len(ln) + (1 if kept_lines else 0)
        if used + add_len > head_budget:
            break
        kept_lines.append(ln)
        used += add_len

    if not kept_lines and not crit_lines:
        return raw_output

    preview = "\n".join(kept_lines)
    if crit_lines:
        missing = [ln for _, ln in crit_lines if ln not in preview and ln.strip() not in preview]
        if missing:
            preview = (preview + "\n" if preview else "") + "\n".join(missing)

    prev_lines = preview.splitlines()
    prev_set = set(prev_lines) | {ln.strip() for ln in prev_lines}
    hidden_nos = [
        idx for idx, ln in enumerate(raw_lines, 1)
        if ln.strip() and ln not in prev_set and ln.strip() not in prev_set
    ]
    if hidden_nos:
        footer = f"... {len(hidden_nos)} lines omitted ({format_line_range(hidden_nos)}) ..."
        preview = (preview + "\n" if preview else "") + footer

    candidate = preview + pointer
    if len(candidate) > cap:
        return raw_output
    return candidate
