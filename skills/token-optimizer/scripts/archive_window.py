"""Bounded reads over an archived tool result.

``measure.py expand`` returns a whole archived response by default. These
helpers slice the stored text into a line range, a char range, or
match-context windows so a caller can pull back only the part it needs —
the same progressive-disclosure move the archive pointer already makes for
the preview. Shared by ``expand`` and reusable by other adapters that hold
archived text.

All offsets are 0-based char offsets into the stored response; line numbers
are 1-based. Nothing here writes or mutates — callers own I/O and accounting.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

# A bare ``--start``/``--chars`` read defaults to a ~6k-char window (the paper's
# doc-read default); match windows are hard-capped so one hot line cannot pull
# a whole minified file back into context.
DEFAULT_WINDOW_CHARS = 6000
MAX_MATCH_WINDOW_CHARS = 8000
DEFAULT_MATCH_CONTEXT = 3
MAX_MATCH_CONTEXT = 50
MAX_MATCH_WINDOWS = 10
_INT_MAX = 1 << 62

_VALUE_FLAGS = frozenset({"lines", "start", "chars", "match", "context", "skip"})


class SpecError(ValueError):
    """A bounded-read request that cannot be satisfied (bad flag, bad range)."""


@dataclass
class ReadSpec:
    """One bounded-read request, normalized from raw CLI flag values."""

    kind: str  # "chars" | "lines" | "match"
    start: int = 0
    chars: int = DEFAULT_WINDOW_CHARS
    lines: str = ""
    pattern: str = ""
    context: int = DEFAULT_MATCH_CONTEXT
    skip: int = 0


@dataclass
class Window:
    """A slice of the stored response plus the coordinates to cite it."""

    text: str
    start: int          # char offset, 0-based inclusive
    end: int            # char offset, exclusive
    line_start: int     # 1-based first line the slice touches
    line_end: int       # 1-based last line the slice touches
    total_chars: int
    total_lines: int
    has_prev: bool
    has_next: bool
    clipped: bool = False        # match window trimmed to MAX_MATCH_WINDOW_CHARS
    matched_lines: tuple = ()    # 1-based line numbers that matched (match mode)
    group_index: int = 0         # 1-based match group number (match mode)
    group_total: int = 0         # total match groups before --skip (match mode)


def _bounded_int(value, flag, lo, hi):
    try:
        n = int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise SpecError(f"--{flag} wants an integer, got {value!r}")
    if n < lo or n > hi:
        raise SpecError(f"--{flag} must be between {lo} and {hi}, got {n}")
    return n


def spec_from_flags(flags):
    """Build a ReadSpec from raw ``expand`` flag values, or None when empty.

    ``flags`` maps the bare flag name ("lines", "start", ...) to its string
    value. Conflicting or orphaned flags raise SpecError so the CLI can print
    a usage error instead of silently picking one.
    """
    if not flags:
        return None
    unknown = sorted(set(flags) - _VALUE_FLAGS)
    if unknown:
        raise SpecError(f"unknown expand option(s): {', '.join('--' + u for u in unknown)}")
    has_lines = "lines" in flags
    has_chars = "chars" in flags or "start" in flags
    has_match = "match" in flags
    if sum(1 for x in (has_lines, has_chars, has_match) if x) > 1:
        raise SpecError("--lines, --start/--chars and --match are mutually exclusive")
    for flag in ("context", "skip"):
        if flag in flags and not has_match:
            raise SpecError(f"--{flag} requires --match")
    if has_match and not str(flags["match"]).strip():
        raise SpecError("--match needs a non-empty pattern")
    if has_lines:
        return ReadSpec(kind="lines", lines=str(flags["lines"]))
    if has_match:
        return ReadSpec(
            kind="match",
            pattern=str(flags["match"]),
            context=_bounded_int(flags.get("context", DEFAULT_MATCH_CONTEXT),
                                 "context", 0, MAX_MATCH_CONTEXT),
            skip=_bounded_int(flags.get("skip", 0), "skip", 0, _INT_MAX),
        )
    return ReadSpec(
        kind="chars",
        start=_bounded_int(flags.get("start", 0), "start", 0, _INT_MAX),
        chars=_bounded_int(flags.get("chars", DEFAULT_WINDOW_CHARS), "chars", 1, _INT_MAX),
    )


def line_count(text):
    """Number of lines ``splitlines`` sees (0 for empty text)."""
    return len(text.splitlines())


def _split_lines(text):
    """(line parts with keepends, start offsets). offsets[i] starts line i;
    offsets[len(parts)] == len(text) is the end sentinel."""
    parts = text.splitlines(keepends=True)
    offsets = [0]
    for part in parts:
        offsets.append(offsets[-1] + len(part))
    return parts, offsets


def _line_of(offsets, n_lines, pos):
    """0-based index of the line containing char ``pos`` (clamped)."""
    i = bisect.bisect_right(offsets, pos) - 1
    return max(0, min(i, n_lines - 1))


def _window(text, offsets, n_lines, start, end, has_prev, has_next, **kw):
    return Window(
        text=text[start:end], start=start, end=end,
        line_start=_line_of(offsets, n_lines, start) + 1,
        line_end=_line_of(offsets, n_lines, max(start, end - 1)) + 1,
        total_chars=len(text), total_lines=n_lines,
        has_prev=has_prev, has_next=has_next, **kw)


def char_window(text, start, length):
    """``length`` chars beginning at 0-based ``start`` (clamped at EOF)."""
    total = len(text)
    if start < 0:
        raise SpecError("--start must be >= 0")
    if length < 1:
        raise SpecError("--chars must be >= 1")
    if start >= total:
        raise SpecError(f"--start {start} is past the end ({total} chars)")
    end = min(total, start + length)
    parts, offsets = _split_lines(text)
    return _window(text, offsets, len(parts), start, end,
                   has_prev=start > 0, has_next=end < total)


_LINE_SPEC = re.compile(r"(\d+)(?::(\d*)|-(\d*))?\Z")


def line_window(text, spec):
    """Lines ``spec`` of ``text``. Accepts ``A``, ``A:B``, ``A-B``; a bare
    separator (``A:`` / ``A-``) reads to EOF. Lines are 1-based."""
    m = _LINE_SPEC.fullmatch(str(spec).strip())
    if not m:
        raise SpecError("--lines wants A, A:B or A-B (1-based; trailing B optional = to EOF)")
    first = int(m.group(1))
    parts, offsets = _split_lines(text)
    n_lines = len(parts)
    if first < 1:
        raise SpecError("--lines are 1-based; the first line must be >= 1")
    if n_lines == 0 or first > n_lines:
        raise SpecError(f"--lines {first}: is past the end ({n_lines} lines)")
    if m.group(2) is None and m.group(3) is None:
        last = first  # bare "A" = that one line
    else:
        tail = m.group(2) if m.group(2) is not None else m.group(3)
        last = n_lines if tail == "" else int(tail)
        if last < first:
            raise SpecError(f"--lines end {last} is before start {first}")
        last = min(last, n_lines)
    start, end = offsets[first - 1], offsets[last]
    return _window(text, offsets, n_lines, start, end,
                   has_prev=first > 1, has_next=last < n_lines)


def _match_span(line, rx, needle):
    """(start, end) of the first match in ``line``, or None."""
    if rx is not None:
        m = rx.search(line)
        return (m.start(), m.end()) if m else None
    i = line.find(needle)
    return (i, i + len(needle)) if i >= 0 else None


def _match_last_end(line, rx, needle):
    """End offset of the LAST match in ``line``."""
    if rx is not None:
        end = 0
        for m in rx.finditer(line):
            end = m.end()
        return end or len(line)
    i = line.rfind(needle)
    return (i + len(needle)) if i >= 0 else len(line)


def _clip_to_matches(text, parts, offsets, group, rx, needle):
    """A <=MAX_MATCH_WINDOW_CHARS char range centered on the group's matches."""
    total = len(text)
    span_s = offsets[group[0]] + (_match_span(parts[group[0]], rx, needle) or (0, 0))[0]
    span_e = offsets[group[-1]] + _match_last_end(parts[group[-1]], rx, needle)
    span_len = max(1, span_e - span_s)
    if span_len >= MAX_MATCH_WINDOW_CHARS:
        ws, we = span_s, min(total, span_s + MAX_MATCH_WINDOW_CHARS)
    else:
        pad = (MAX_MATCH_WINDOW_CHARS - span_len) // 2
        ws = max(0, span_s - pad)
        we = min(total, ws + MAX_MATCH_WINDOW_CHARS)
        ws = max(0, we - MAX_MATCH_WINDOW_CHARS)
    return ws, we


def match_windows(text, pattern, context=DEFAULT_MATCH_CONTEXT, skip=0,
                  limit=MAX_MATCH_WINDOWS):
    """``limit`` context windows around regex/substring matches, after ``skip``
    groups. Returns ``(windows, group_total)``; ``group_total`` counts every
    match group so the caller can say how many remain. A pattern that does not
    compile as regex falls back to literal substring (grep -F semantics)."""
    parts, offsets = _split_lines(text)
    n_lines = len(parts)
    if n_lines == 0 or not str(pattern):
        return [], 0
    try:
        rx = re.compile(str(pattern))
        needle = None
    except re.error:
        rx, needle = None, str(pattern)
    matched = [i for i, line in enumerate(parts)
               if _match_span(line, rx, needle) is not None]
    if not matched:
        return [], 0
    # Cluster matched lines into groups: two matches share a window when their
    # ±context ranges touch, i.e. the gap is at most 2*context+1 lines.
    groups = [[matched[0]]]
    for idx in matched[1:]:
        if idx - groups[-1][-1] <= 2 * context + 1:
            groups[-1].append(idx)
        else:
            groups.append([idx])
    windows = []
    for gpos, group in enumerate(groups[skip:skip + limit]):
        lo = max(0, group[0] - context)
        hi = min(n_lines - 1, group[-1] + context)
        ws, we = offsets[lo], offsets[hi + 1]
        clipped = we - ws > MAX_MATCH_WINDOW_CHARS
        if clipped:
            ws, we = _clip_to_matches(text, parts, offsets, group, rx, needle)
        windows.append(_window(
            text, offsets, n_lines, ws, we,
            has_prev=False, has_next=False, clipped=clipped,
            matched_lines=tuple(i + 1 for i in group),
            group_index=skip + gpos + 1, group_total=len(groups)))
    return windows, len(groups)
