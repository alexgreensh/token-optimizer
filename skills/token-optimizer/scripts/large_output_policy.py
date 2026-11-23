#!/usr/bin/env python3
"""Narrow large Bash output policy (opt-in via TOKEN_OPTIMIZER_LARGE_OUTPUT=1, default OFF).

SPEC-large-output-narrow: replaces the broad 8 KB head-and-tail rule with a strict
repetitive-only rule that requires ALL conditions to hold, else passes raw:
  1. TOKEN_OPTIMIZER_LARGE_OUTPUT=1 (default/unset = OFF: one env check, nothing else).
  2. Output >= 16 KB (16384 bytes) after ANSI stripping AND >= 200 lines.
  3. Exit status 0, not interrupted, empty stderr.
  4. Command contains none of the excluded read/slice/query/inline-script forms
     per pipeline segment; unparseable command passes raw.
  5. Zero lines anywhere in output match the failure-taxonomy vectors (one match -> raw).
  6. Repetition test: >= 60% of lines fall in runs of >= 5 lines sharing a normalized shape.
  7. Replacement keeps first 40 + last 40 lines, every section/echo header, one sample
     line per hidden run, and exact declaration per hidden run:
       [lines A-B hidden: N lines matching "<shape>"; expand <key>]
     Net saving must be >= 30% and >= 500 tokens after the pointer, else pass raw.
  8. Archive first (failure -> raw), emit safety verifier, monotonic time box, logged
     as `large_output_policy`.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
from pathlib import Path
from typing import Any

FEATURE_NAME = "large_output_policy"
ENABLED_ENV = "TOKEN_OPTIMIZER_LARGE_OUTPUT"

MIN_BYTES = 16384
MIN_LINES = 200
HEAD_LINES = 40
TAIL_LINES = 40
MIN_RUN_LENGTH = 5
MIN_REPETITIVE_SHARE = 0.60
MIN_SAVING_RATIO = 0.30
MIN_SAVING_TOKENS = 500

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Condition 4: excluded commands/forms (matched per pipeline/chain segment)
_CMD_PREFIX = r"(?:^|[;&|\n]|\|\||&&)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
_EXCLUDED_CMD_RES: tuple[re.Pattern[str], ...] = (
    re.compile(_CMD_PREFIX + r"(?:cat|nl|bat|less|g?sed|g?head|g?tail|g?awk|mawk|nawk|g?cut|jq|yq|column|sqlite3|psql)\b"),
    re.compile(_CMD_PREFIX + r"(?:grep|rg)\b[^;&|\n]*\s-m\b"),
    re.compile(_CMD_PREFIX + r"git\s+log\b[^;&|\n]*\s(?:-n\b|-\d+\b|--max-count\b)"),
    re.compile(_CMD_PREFIX + r"git\s+show\b"),
    re.compile(r"<<-?\s*\S"),  # heredoc
    re.compile(_CMD_PREFIX + r"(?:python3?(?:\.\d+)?\b[^;&|\n]*\s-c\b|node\b[^;&|\n]*\s-e\b)"),
    re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE),
)
_GIT_DIFF_RE = re.compile(_CMD_PREFIX + r"git\s+diff\b([^;&|\n]*)")

# Condition 5: failure-taxonomy vectors (any single match -> pass whole output raw)
_FAILURE_VECTOR_RE = re.compile(
    r"(?:"
    r"command not found|No such file or directory|Permission denied|\bKilled\b|Segmentation fault|"
    r"^CONFLICT\s*\(|\s!\s+\[rejected\]|^fatal:|"
    r"^\s*Expected:|^\s*Received:|\bFAILED\b|^not ok\s+\d+|^---\s+FAIL:|panicked at|"
    r"error\[E\d+\]|\bTS\d{3,5}\b|^\s*-->\s*\S+:\d+|"
    r"OOMKilled|CrashLoopBackOff|exceeded its progress deadline|forces replacement|"
    r"\bFATAL\b|Traceback \(most recent call last\):|^\s*File \"[^\"]+\", line \d+|"
    r"exit code [1-9]\d*|exit status [1-9]\d*|\bAssertionError\b"
    r")",
    re.MULTILINE,
)

# Section / command-boundary header patterns
_SECTION_HEADER_RE = re.compile(
    r"(?:"
    r"={3,}|^-{3,}(?:\s|$|-)|^diff --git\s+|^\+\+\+\s+[ab]/|^---\s+[ab]/|"
    r"^##\[(?:group|endgroup)\]|^(?:Step|Stage|Phase)\s+\d+(?:/\d+)?\b|"
    r"^\[(?:Step|Stage|Phase)\s+\d+|^#{1,3}\s+\S"
    r")"
)
_ECHO_STMT_RE = re.compile(
    r"(?:^|[;&\n]|&&|\|\|)\s*(?:echo|printf)\s+(?:-e\s+|-n\s+)?(?:'([^'\n]{2,120})'|\"([^\"\n]{2,120})\"|([A-Za-z0-9_=.:/#-]{3,120}))"
)

# Normalization regexes for Condition 6 (shape masking: timestamps, hashes, paths, digits)
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{7,64}\b")
_PATH_RE = re.compile(r"(?:(?:/[\w.+-]+){2,}|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


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
    return bool(text) and (
        "[Full result archived" in text
        or "[bash_compress]" in text
        or "[large_output_policy:" in text
    )


def is_binary_like(text: str) -> bool:
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
    return (sample.count("\ufffd") / len(sample)) > 0.15


def is_condition4_excluded(command: str) -> bool:
    """True when command matches any Condition 4 exclusion or is unparseable."""
    if not command or not isinstance(command, str) or not command.strip():
        return True
    try:
        shlex.split(command)
    except ValueError:
        return True  # Unparseable command -> pass raw
    for pat in _EXCLUDED_CMD_RES:
        if pat.search(command):
            return True
    for m in _GIT_DIFF_RE.finditer(command):
        if "--stat" not in (m.group(1) or ""):
            return True
    try:
        from pipe_file_compress import has_explicit_output_limit
        if has_explicit_output_limit(command):
            return True
    except Exception:
        pass
    return False


is_explicit_narrow_read = is_condition4_excluded

# Deliberate content reads (bare cat / cat chains, jq, inline-script dumps) that
# bash_compress_hook must never chop. Deliberately narrower than Condition 4:
# `git log | head -20` or `du | sort | head` are NOT content reads and stay
# eligible for the ordinary compression pipeline.
_JQ_CMD_RE = re.compile(_CMD_PREFIX + r"(?:jq|yq)\b")
_INLINE_SCRIPT_READ_RE = re.compile(
    _CMD_PREFIX + r"(?:python3?(?:\.\d+)?\s+(?:[^;&|\n]*?-c\b|-\s*<<)|(?:node|ruby|perl)\s+[^;&|\n]*?-e\b)"
)
_CAT_SEG_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:cd|cat|nl|bat)\b")
_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")


def is_deliberate_content_read(command: str) -> bool:
    """True for deliberate content reads (bare cat, cat chains, jq, inline-script dumps)."""
    if not command or not isinstance(command, str):
        return False
    if _JQ_CMD_RE.search(command) or _INLINE_SCRIPT_READ_RE.search(command):
        return True
    segs = [s.strip() for s in _CHAIN_SPLIT_RE.split(command.strip()) if s.strip()]
    if segs and any(re.match(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:cat|nl|bat)\b", s) for s in segs):
        if all(_CAT_SEG_RE.match(s) and "|" not in s for s in segs):
            return True
    return False


def _extract_echo_markers(command: str) -> frozenset[str]:
    markers: set[str] = set()
    for m in _ECHO_STMT_RE.finditer(command or ""):
        val = (m.group(1) or m.group(2) or m.group(3) or "").replace("\\n", "").strip()
        if len(val) >= 2:
            markers.add(val)
    return frozenset(markers)


def is_section_header(line: str, echo_markers: frozenset[str] = frozenset()) -> bool:
    stripped = (line or "").strip()
    if not stripped:
        return False
    if echo_markers and stripped in echo_markers:
        return True
    return bool(_SECTION_HEADER_RE.search(stripped))


def normalize_shape(line: str) -> str:
    """Mask timestamps, hex hashes, paths, and digits to detect repetitive runs."""
    s = _TS_RE.sub("<TS>", line.strip())
    s = _HEX_RE.sub("<HEX>", s)
    s = _PATH_RE.sub("<PATH>", s)
    s = _NUM_RE.sub("<N>", s)
    return s[:80]


def _find_repetitive_runs(lines: list[str], budget=None) -> tuple[list[tuple[int, int, str]], float]:
    """Return (runs of length >= MIN_RUN_LENGTH as (start, end_inclusive, shape), repetitive_share)."""
    shapes: list[str] = []
    for idx, ln in enumerate(lines):
        if budget is not None:
            budget.check(idx, stride=64)
        shapes.append(normalize_shape(ln))
    runs: list[tuple[int, int, str]] = []
    rep_count = 0
    n = len(shapes)
    i = 0
    while i < n:
        j = i + 1
        while j < n and shapes[j] == shapes[i] and shapes[i]:
            j += 1
        length = j - i
        if length >= MIN_RUN_LENGTH:
            runs.append((i, j - 1, shapes[i]))
            rep_count += length
        i = j
    return runs, (rep_count / n) if n else 0.0


def _attach_pointer(preview_body: str, original_chars: int, archive_key: str) -> str:
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
    exit_code: int | None = 0,
    stderr: str = "",
    interrupted: bool = False,
    budget=None,
) -> dict[str, Any] | None:
    """Evaluate all 8 narrow-rule conditions. Pure & fail-open: returns None to pass raw."""
    try:
        if not isinstance(stdout, str) or not stdout:
            return None
        # Condition 3: exit 0, not interrupted, empty stderr
        if exit_code not in (None, 0) or interrupted or bool((stderr or "").strip()):
            return None
        if is_already_archived(stdout) or is_binary_like(stdout):
            return None
        # Condition 4: excluded read/slice/query/inline-script commands
        if is_condition4_excluded(command or ""):
            return None

        cleaned = _strip_ansi(stdout)
        # Condition 2: >= 16 KB after ANSI stripping and >= 200 lines
        cleaned_bytes = len(cleaned.encode("utf-8", errors="replace"))
        if cleaned_bytes < MIN_BYTES:
            return None
        lines = cleaned.splitlines()
        total_lines = len(lines)
        if total_lines < MIN_LINES:
            return None

        # Condition 5: zero failure-vector matches anywhere in output
        if _FAILURE_VECTOR_RE.search(cleaned):
            return None

        # Condition 6: >= 60% of lines in runs of >= 5 lines sharing a normalized shape
        runs, rep_share = _find_repetitive_runs(lines, budget=budget)
        if rep_share < MIN_REPETITIVE_SHARE or not runs:
            return None

        # Condition 7: keep first 40, last 40, section/echo headers, and 1 sample per hidden run
        echo_markers = _extract_echo_markers(command or "")
        kept: set[int] = set(range(min(HEAD_LINES, total_lines)))
        kept.update(range(max(0, total_lines - TAIL_LINES), total_lines))
        for idx, ln in enumerate(lines):
            if budget is not None:
                budget.check(idx, stride=64)
            if idx not in kept and is_section_header(ln, echo_markers):
                kept.add(idx)

        key = archive_key or ("0" * 16)
        # Map each repetitive run to hidden sub-spans (excluding kept lines)
        hide_spans: list[tuple[int, int, str]] = []
        for r_start, r_end, shape in runs:
            span_start = None
            for idx in range(r_start, r_end + 1):
                if idx not in kept:
                    if span_start is None:
                        span_start = idx
                else:
                    if span_start is not None:
                        if idx - span_start >= 2:
                            # Keep first line of the hidden run as sample
                            kept.add(span_start)
                            hide_spans.append((span_start + 1, idx - 1, shape))
                        span_start = None
            if span_start is not None and (r_end - span_start + 1) >= 2:
                kept.add(span_start)
                hide_spans.append((span_start + 1, r_end, shape))

        if not hide_spans:
            return None

        hide_by_start = {s: (e, sh) for s, e, sh in hide_spans}
        out_lines: list[str] = []
        hidden_total = 0
        i = 0
        while i < total_lines:
            if i in hide_by_start:
                end_0, shape = hide_by_start[i]
                cnt = end_0 - i + 1
                hidden_total += cnt
                lo, hi = i + 1, end_0 + 1
                safe_shape = shape.replace('"', "'")
                out_lines.append(
                    f'[lines {lo}-{hi} hidden: {cnt} lines matching "{safe_shape}"; expand {key}]'
                )
                i = end_0 + 1
            else:
                out_lines.append(lines[i])
                i += 1

        if hidden_total <= 0:
            return None

        preview_body = "\n".join(out_lines)
        preview_with_pointer = _attach_pointer(preview_body, len(stdout), key)

        tokens_before = _tokens(cleaned)
        tokens_after = _tokens(preview_with_pointer)
        saved_tokens = tokens_before - tokens_after
        if tokens_before <= 0 or saved_tokens < MIN_SAVING_TOKENS:
            return None
        if (saved_tokens / tokens_before) < MIN_SAVING_RATIO:
            return None

        return {
            "bytes": cleaned_bytes,
            "lines": total_lines,
            "hidden_lines": hidden_total,
            "repetitive_share": round(rep_share, 4),
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "saved_tokens": saved_tokens,
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
    exit_code: int | None = 0,
    stderr: str = "",
    interrupted: bool = False,
) -> tuple[str | None, str]:
    """PostToolUse entry point (default OFF; archive-first + emit safety gate + time box)."""
    try:
        if not is_enabled():
            return None, ""
        if not isinstance(stdout, str) or len(stdout.encode("utf-8", errors="replace")) < MIN_BYTES:
            return None, ""
        if exit_code not in (None, 0) or interrupted or bool((stderr or "").strip()):
            return None, ""

        budget = None
        try:
            from hook_fastpath import MonotonicBudget
            budget = MonotonicBudget()
        except Exception:
            pass

        archive_key = hashlib.sha256(
            f"{session_id}|{command}|{stdout}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        ev = evaluate(
            command, stdout, archive_key=archive_key,
            exit_code=exit_code, stderr=stderr, interrupted=interrupted, budget=budget,
        )
        if ev is None:
            return None, ""

        from archive_result import archive_entry_exists, archive_original
        if archive_original(stdout, session_id, archive_key, "Bash") is None:
            return None, ""
        if not archive_entry_exists(session_id, archive_key):
            return None, ""

        compressed = ev["preview_with_pointer"]
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
    """Benchmark adapter for benchmark.py external fixtures (no disk I/O)."""
    cmd = str(fixture.get("command", ""))
    raw = str(fixture.get("raw_output", ""))
    ev = evaluate(
        cmd, raw, archive_key="bench00000000001",
        exit_code=fixture.get("exit_code", 0),
        stderr=str(fixture.get("stderr", "")),
        interrupted=bool(fixture.get("interrupted", False)),
    )
    return raw if ev is None else ev["preview_with_pointer"]
