#!/usr/bin/env python3
"""Final pre-emit safety verifier shared by all Token Optimizer compressors.

Stdlib-only. Called right before emitting any compressed replacement:
  (a) Confirms every critical line in raw output (traceback, non-zero exit,
      failing test, panic/fatal, error/exception) is visible verbatim.
  (b) Confirms every omitted line is covered by an accurate declared count
      and place.
  (c) Confirms a working runnable ``measure.py expand <key>`` pointer exists
      whenever any non-empty content is hidden (unless ``require_expand=False``
      for a pre-pointer inner compressor that is re-verified with
      ``require_expand=True`` after attaching the archive pointer).

Fail-closed to original: if any check fails or the verifier raises, callers
emit NOTHING and return the original output untouched.

Definitions of critical lines and undeclared hiding mirror
``scripts/replay_v2_metrics.py`` without importing from ``scripts/`` at runtime.
"""
from __future__ import annotations

import re

VISIBLE_OK = "VISIBLE_OK"
RECOVERABLE_DECLARED = "RECOVERABLE_DECLARED"
UNDECLARED_HIDDEN = "UNDECLARED_HIDDEN"
CRITICAL_HIDDEN = "CRITICAL_HIDDEN"
QUALITY_CLASSES = (VISIBLE_OK, RECOVERABLE_DECLARED, UNDECLARED_HIDDEN, CRITICAL_HIDDEN)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CD_PREFIX_RE = re.compile(r"^(?:cd\s+\S+\s*(?:&&|;)\s*)+")
_SEARCH_MATCH_LINE_RE = re.compile(r"^(?:[A-Za-z]:)?[\w./@+-]+\.[A-Za-z0-9]{1,10}:\d+[:\-]")
_PLAIN_PATH_LINE_RE = re.compile(r"^(?:[A-Za-z]:)?[\w./@+-]+/[\w./@+-]+\.[A-Za-z0-9]{1,10}$")
_SEARCH_TOOL_DIAG_RE = re.compile(r"^(?:grep|rg|egrep|fgrep|git|find|ag):\s+", re.I)
_EXPAND_CMD_RE = re.compile(
    r'measure\.py["\']?\s+(?:--runtime\s+\S+\s+)?expand\s+([a-zA-Z0-9_-]{8,})'
)

_CRIT_TRACEBACK_RE = re.compile(
    r'traceback \(most recent call last\)|^\s*file ".+", line \d+|'
    r"^\s*at\s+\S+\s+\([^)]+:\d+:\d+\)|^goroutine \d+ \[",
    re.I,
)
_CRIT_EXIT_RE = re.compile(
    r"\b(?:exit(?:ed with)?|return(?:ed)?)\s+(?:code|status)\s+[1-9]\d*\b|"
    r"^error:\s*exit code\s+[1-9]\d*\b",
    re.I,
)
_CRIT_FAIL_TEST_RE = re.compile(
    r"^FAILED\b|^\s*FAIL(?:\s+[\w./-]+|:)|^--- FAIL:|"
    r"\b[1-9]\d*\s+failed\b|\bAssertionError\b|[✗✕✖]\s+"
)
_CRIT_PANIC_RE = re.compile(
    r"\b(?:fatal|panic|panicked|segfault|segmentation fault|core dumped|out of memory)\b",
    re.I,
)
_CRIT_ERROR_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\b|\b(?:error|errors|exception|failed|failure|failures)\b)",
    re.I,
)
_CRIT_NON_ERROR_FAILURE_RE = re.compile(
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
_BENIGN_ZERO_ERR_RE = re.compile(
    r"\b(?:0|no|zero|without)\s+(?:errors?|failures?|failed)\b|"
    r"\b(?:error_count|errors|failures|failed)\s*[:=]\s*0\b",
    re.I,
)

_DECL_COUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(\d[\d,]*)\s+(?:more\s+)?(?:[\w/-]+\s+){0,3}"
        r"(?:matches?|lines?|files?|rows?|records?|items?|entries|results?|notes?|chars?)\s+"
        r"(?:omitted|hidden|skipped|elided|truncated|collapsed|archived)\b",
        re.I,
    ),
    re.compile(
        r"\b(\d[\d,]*)\s+more\s+(?:[\w/-]+\s+){0,2}"
        r"(?:matches?|lines?|files?|rows?|records?|items?|entries|results?|notes?|chars?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:omitted|hidden|skipped|elided|collapsed|archived)\s+(\d[\d,]*)\s+"
        r"(?:[\w/-]+\s+){0,2}"
        r"(?:matches?|lines?|files?|rows?|records?|items?|entries|results?|notes?|chars?)\b",
        re.I,
    ),
    re.compile(r"\bshowing\s+(\d[\d,]*)\s+of\s+(\d[\d,]*)\b", re.I),
    re.compile(r"\+(\d[\d,]*)\s+more\b", re.I),
    re.compile(
        r"\b(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+(?:lines?|matches?|rows?|records?|files?)\b",
        re.I,
    ),
    re.compile(
        r"\[lines\s+\d+-\d+\s+hidden:\s*(\d[\d,]*)\s+lines\s+matching\b",
        re.I,
    ),
)
_DECL_PLACE_RE = re.compile(
    r"\b(?:in\s+[\w./@+-]+|lines?\s+\d+(?:\s*[-–]\s*\d+)?|rows?\s+\d+(?:\s*[-–]\s*\d+)?|"
    r"records?\s+\d+(?:\s*[-–]\s*\d+)?|middle|tail|head|after\s+line|remaining|section|"
    r"more\s+files?\s+with\s+matches|omitted\s+from|non-file\s+lines?|folder\(s\)|"
    r"between\s+head|capped)\b",
    re.I,
)
_SINGLE_LINE_OMIT_RE = re.compile(
    r"\b(\d[\d,]*)\s+(?:more\s+lines?|(?:middle\s+)?lines?\s+(?:omitted|hidden|elided|collapsed|archived))\b",
    re.I,
)
_SEARCH_CMDS = frozenset({"grep", "rg", "egrep", "fgrep", "ag", "find", "ls", "fd"})


def has_valid_expand_command(text: str) -> bool:
    """Return True iff ``text`` contains a runnable ``measure.py expand <key>`` command."""
    return bool(_EXPAND_CMD_RE.search(text or ""))


def _is_search_context(feature: str, tool_name: str, command: str, comp_clean: str) -> bool:
    if tool_name in ("Grep", "Glob") or feature in (
        "builtin_grep",
        "builtin_glob",
        "bash_search_noise_fold",
        "cmd_git_ls",
    ):
        return True
    if comp_clean.startswith(("[Search results:", "[Grep ", "Grep files", "Glob files")):
        return True
    if tool_name == "Bash" and command:
        words = _CD_PREFIX_RE.sub("", command.strip()).split()
        if words and (
            words[0] in _SEARCH_CMDS
            or (words[0] == "git" and len(words) > 1 and words[1] in ("grep", "ls-files"))
        ):
            return True
    return False


_TOKEN_RE = re.compile(r"[^\s,;:'\"()\[\]{}]+")
_LINENO_TOKEN_RE = re.compile(r"(?:^|[:\s])(\d+):|:(\d+)\b")


def _build_comp_index(comp_clean: str, feature: str) -> dict:
    """Pre-index compressed text in O(M) so per-raw-line checks are O(1)."""
    lines_set: set[str] = set()
    prefix80_set: set[str] = set()
    tokens_set: set[str] = set()
    lineno_lines: dict[str, list[str]] = {}
    for raw_ln in comp_clean.splitlines():
        s = raw_ln.strip()
        if not s:
            continue
        lines_set.add(s)
        if len(s) > 80:
            prefix80_set.add(s[:80])
        for tok in _TOKEN_RE.findall(s):
            tokens_set.add(tok)
            if "/" in tok:
                tokens_set.add(tok.rsplit("/", 1)[-1])
            if ":" in tok:
                head = tok.split(":", 1)[0]
                if head:
                    tokens_set.add(head)
                    if "/" in head:
                        tokens_set.add(head.rsplit("/", 1)[-1])
        s_low = s.lower()
        for m in _LINENO_TOKEN_RE.finditer(s):
            lno = m.group(1) or m.group(2)
            if lno:
                lst = lineno_lines.get(lno)
                if lst is None:
                    lineno_lines[lno] = [s_low]
                elif len(lst) < 8:
                    lst.append(s_low)
    return {
        "lines": lines_set,
        "prefix80": prefix80_set,
        "tokens": tokens_set,
        "lineno_lines": lineno_lines,
        "feature": feature,
    }


def _line_is_visible(
    line: str,
    comp_clean: str,
    comp_low: str,
    *,
    is_search: bool = False,
    feature: str = "",
    comp_index: dict | None = None,
) -> bool:
    if comp_index is not None:
        if line in comp_index["lines"]:
            return True
        if len(line) > 80 and line[:80] in comp_index["prefix80"]:
            return True
        tokens = comp_index["tokens"]
        if _SEARCH_MATCH_LINE_RE.match(line):
            parts = line.split(":", 2)
            if len(parts) == 3:
                fname, lineno, body = parts[0], parts[1], parts[2].strip()
                base = fname.rsplit("/", 1)[-1]
                if (fname in tokens or base in tokens) and lineno in comp_index["lineno_lines"]:
                    if not body:
                        return True
                    body_key = body[:40].lower()
                    if any(body_key in cand for cand in comp_index["lineno_lines"][lineno]):
                        return True
        if (
            _PLAIN_PATH_LINE_RE.match(line)
            or (is_search and "/" in line and " " not in line)
            or feature == "cmd_git_ls"
        ) and line.rsplit("/", 1)[-1] in tokens:
            return True
        if feature == "cmd_git_diff_stat" and "|" in line:
            left, right = line.split("|", 1)
            fname = left.strip().rsplit("/", 1)[-1]
            nums = re.findall(r"\d+", right)
            if fname and fname in tokens and (not nums or nums[0] in tokens):
                return True
        return False

    if line in comp_clean or (len(line) > 80 and line[:80] in comp_clean):
        return True
    if _SEARCH_MATCH_LINE_RE.match(line):
        parts = line.split(":", 2)
        if len(parts) == 3:
            fname, lineno, body = parts[0], parts[1], parts[2].strip()
            base = fname.rsplit("/", 1)[-1]
            if (fname in comp_clean or base in comp_clean) and (
                f"{lineno}:" in comp_clean or f":{lineno}" in comp_clean
            ):
                if not body or body[:40].lower() in comp_low:
                    return True
    if (
        _PLAIN_PATH_LINE_RE.match(line) or (is_search and "/" in line and " " not in line)
    ) and line.rsplit("/", 1)[-1] in comp_clean:
        return True
    if feature == "cmd_git_diff_stat" and "|" in line:
        left, right = line.split("|", 1)
        fname = left.strip().rsplit("/", 1)[-1]
        nums = re.findall(r"\d+", right)
        if fname and fname in comp_clean and (not nums or nums[0] in comp_clean):
            return True
    return False


_CRIT_SEARCH_STRICT_RE = re.compile(
    r'traceback \(most recent call last\)|^\s*file ".+", line \d+|'
    r"^\s*at\s+\S+\s+\([^)]+:\d+:\d+\)|^goroutine \d+ \[|"
    r"\b(?:exit(?:ed with)?|return(?:ed)?)\s+(?:code|status)\s+[1-9]\d*\b|"
    r"^error:\s*exit code\s+[1-9]\d*\b|"
    r"\b(?:fatal|panic|panicked|segfault|segmentation fault|core dumped|out of memory)\b",
    re.I,
)


def _critical_reasons_for_line(
    line: str,
    is_search: bool,
    is_gh_list: bool,
    *,
    has_search_strict: bool = True,
) -> list[str]:
    if is_gh_list and "\t" in line and (
        "completed\tsuccess" in line.lower() or "\tsuccess\t" in line.lower()
    ):
        return []
    if is_search:
        if _PLAIN_PATH_LINE_RE.match(line):
            return []
        if _SEARCH_MATCH_LINE_RE.match(line):
            # Search hits for ordinary source words like "error" may compress,
            # but hits containing hard failure signals (FATAL/panic, Traceback,
            # non-zero exit code) must stay visible verbatim.
            if not has_search_strict or not _CRIT_SEARCH_STRICT_RE.search(line):
                return []
            strict_reasons: list[str] = []
            if _CRIT_TRACEBACK_RE.search(line):
                strict_reasons.append("hidden_traceback")
            if _CRIT_EXIT_RE.search(line):
                strict_reasons.append("hidden_exit_code")
            if _CRIT_PANIC_RE.search(line):
                strict_reasons.append("hidden_panic_fatal")
            return strict_reasons
        if not (
            _SEARCH_TOOL_DIAG_RE.match(line)
            or (has_search_strict and _CRIT_SEARCH_STRICT_RE.search(line))
            or _CRIT_NON_ERROR_FAILURE_RE.search(line)
        ):
            return []
    reasons: list[str] = []
    if _CRIT_TRACEBACK_RE.search(line):
        reasons.append("hidden_traceback")
    if _CRIT_EXIT_RE.search(line):
        reasons.append("hidden_exit_code")
    if _CRIT_FAIL_TEST_RE.search(line):
        reasons.append("hidden_failing_test")
    if _CRIT_PANIC_RE.search(line):
        reasons.append("hidden_panic_fatal")
    if not reasons and _CRIT_NON_ERROR_FAILURE_RE.search(line):
        reasons.append("hidden_error")
    if not reasons and _CRIT_ERROR_RE.search(line) and not _BENIGN_ZERO_ERR_RE.search(line):
        reasons.append("hidden_error")
    return reasons


def _check_declaration(
    comp_clean: str,
    hidden_nonempty: int,
    hidden_raw_lines: int,
    *,
    require_expand: bool = True,
) -> list[str]:
    reasons: list[str] = []
    if require_expand and not has_valid_expand_command(comp_clean):
        reasons.append("missing_expand_cmd")
    declared_counts: list[int] = []
    for rx in _DECL_COUNT_PATTERNS:
        for m in rx.finditer(comp_clean):
            if m.lastindex and m.lastindex >= 2:
                declared_counts.append(
                    max(0, int(m.group(2).replace(",", "")) - int(m.group(1).replace(",", "")))
                )
            else:
                declared_counts.append(int(m.group(1).replace(",", "")))
    if not declared_counts:
        reasons.append("missing_hidden_count")
    elif all(c == 0 for c in declared_counts) and hidden_nonempty > 0:
        reasons.append("inaccurate_hidden_count")
    else:
        single = _SINGLE_LINE_OMIT_RE.findall(comp_clean)
        has_breakdown = any(
            k in comp_clean.lower() for k in ("more matches", "more files", "folded noisy", "[git log:")
        )
        if len(single) == 1 and not has_breakdown and len(declared_counts) == 1:
            claimed = int(single[0].replace(",", ""))
            tol = max(3, int(0.15 * max(hidden_nonempty, hidden_raw_lines)))
            if abs(claimed - hidden_raw_lines) > tol and abs(claimed - hidden_nonempty) > tol:
                reasons.append("inaccurate_hidden_count")
    if not _DECL_PLACE_RE.search(comp_clean):
        reasons.append("missing_hidden_place")
    return reasons


def classify_output(
    original: str,
    compressed: str,
    feature: str = "",
    *,
    tool_name: str = "",
    command: str = "",
    exit_code: int | None = None,
    require_expand: bool = True,
    budget=None,
) -> tuple[str, list[str]]:
    """Classify ``compressed`` against ``original`` using replay v2 rules."""
    if not original or not compressed:
        return VISIBLE_OK, []
    if feature in ("rerun_dedupe", "crossturn_dedup") and any(
        s in compressed
        for s in (
            "identical to previous run",
            "identical to your previous",
            "same as previous run",
            "same as your previous",
        )
    ):
        return VISIBLE_OK, []
    if (
        feature == "bash_reread_unchanged"
        and "is unchanged since your earlier full read in this session" in compressed
    ):
        return VISIBLE_OK, []

    comp_clean = _ANSI_RE.sub("", compressed) if "\x1b" in compressed else compressed
    comp_low = comp_clean.lower()
    comp_index = _build_comp_index(comp_clean, feature)
    is_search = _is_search_context(feature, tool_name, command, comp_clean)
    is_gh_list = feature == "cmd_gh_list" or (
        tool_name == "Bash" and (command or "").strip().startswith("gh ")
    )
    has_ansi = "\x1b" in original
    has_search_strict = bool(_CRIT_SEARCH_STRICT_RE.search(original)) if is_search else True

    crit_reasons: set[str] = set()
    hidden_nonempty = 0
    orig_lines = original.splitlines()
    for _idx, raw_line in enumerate(orig_lines):
        if budget is not None:
            budget.check(_idx, stride=64)
        line = (_ANSI_RE.sub("", raw_line) if has_ansi else raw_line).strip()
        if not line or _line_is_visible(
            line, comp_clean, comp_low, is_search=is_search, feature=feature, comp_index=comp_index
        ):
            continue
        hidden_nonempty += 1
        creasons = _critical_reasons_for_line(
            line, is_search, is_gh_list, has_search_strict=has_search_strict
        )
        if creasons:
            # Substring fallback only for the rare critical line not matching whole-line index.
            if line in comp_clean or (len(line) > 80 and line[:80] in comp_clean):
                continue
            core = line.split(": ", 1)[-1][:60].strip()
            if not (core and len(core) >= 12 and core.lower() in comp_low):
                crit_reasons.update(creasons)

    if exit_code not in (None, 0) and hidden_nonempty > 0:
        if f"exit code {exit_code}" not in comp_low and f"exit status {exit_code}" not in comp_low:
            crit_reasons.add("hidden_exit_code")
    if crit_reasons:
        return CRITICAL_HIDDEN, sorted(crit_reasons)
    if hidden_nonempty == 0:
        return VISIBLE_OK, []
    hidden_raw = max(hidden_nonempty, len(orig_lines) - len(comp_clean.splitlines()))
    decl = _check_declaration(
        comp_clean, hidden_nonempty, hidden_raw, require_expand=require_expand
    )
    return (UNDECLARED_HIDDEN, sorted(set(decl))) if decl else (RECOVERABLE_DECLARED, [])


def verify_before_emit(
    original: str,
    compressed: str,
    *,
    feature: str = "",
    tool_name: str = "",
    command: str = "",
    exit_code: int | None = None,
    require_expand: bool = True,
) -> tuple[bool, list[str]]:
    """Return ``(ok, reasons)``. Fail-closed to ``(False, ["verifier_exception"])``."""
    try:
        if not isinstance(original, str) or not isinstance(compressed, str):
            return False, ["invalid_input_type"]
        if not original or not compressed or compressed == original:
            return True, []
        status, reasons = classify_output(
            original,
            compressed,
            feature,
            tool_name=tool_name,
            command=command,
            exit_code=exit_code,
            require_expand=require_expand,
        )
        return status in (VISIBLE_OK, RECOVERABLE_DECLARED), reasons
    except Exception:
        return False, ["verifier_exception"]


def gate_or_raw(
    original: str,
    compressed: str,
    *,
    feature: str = "",
    tool_name: str = "",
    command: str = "",
    exit_code: int | None = None,
    require_expand: bool = True,
) -> str:
    """Return ``compressed`` only if ``verify_before_emit`` passes; else ``original``."""
    try:
        if not compressed or compressed == original:
            return original
        ok, _ = verify_before_emit(
            original,
            compressed,
            feature=feature,
            tool_name=tool_name,
            command=command,
            exit_code=exit_code,
            require_expand=require_expand,
        )
        return compressed if ok else original
    except Exception:
        return original


def gate_or_none(
    original: str,
    compressed: str | None,
    *,
    feature: str = "",
    tool_name: str = "",
    command: str = "",
    exit_code: int | None = None,
    require_expand: bool = True,
) -> str | None:
    """Return ``compressed`` only if ``verify_before_emit`` passes; else ``None``."""
    try:
        if not compressed or compressed == original:
            return None
        ok, _ = verify_before_emit(
            original,
            compressed,
            feature=feature,
            tool_name=tool_name,
            command=command,
            exit_code=exit_code,
            require_expand=require_expand,
        )
        return compressed if ok else None
    except Exception:
        return None
