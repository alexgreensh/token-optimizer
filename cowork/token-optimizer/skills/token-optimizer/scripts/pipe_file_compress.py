#!/usr/bin/env python3
"""Safe read-only pipeline compression (pipe_compress) and unchanged Bash file
re-read detection (bash_reread_unchanged).

Design invariants:
- Fail-open: any parse ambiguity, unexpected shape, or exception returns None
  so the caller serves the original output verbatim.
- Deterministic: identical inputs always produce byte-identical outputs so the
  prompt cache stays stable.
- Explicit model intent is respected: commands with explicit output limits
  (`head -n`, `tail -n`, `sed -n <range>`) are never shortened.
- Never compresses mixed output from multiple producers, loops, conditionals
  (`||`, `if`, `for`, `while`, `case`), command substitution (`$(...)`, `` ` ``),
  process substitution (`<()`, `>()`), subshells/braces, heredocs, file
  redirects, or any non-read-only stage.
- Unchanged Bash `cat` re-read detection never skeletonizes source code and
  only fires when `SessionStore.file_reads` + `compact_markers.py` prove the
  exact unchanged file was already fully shown in this session and is still in
  context.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat as stat_module
import time
from pathlib import Path
from typing import Any

FEATURE_PIPE_COMPRESS = "pipe_compress"
FEATURE_BASH_REREAD = "bash_reread_unchanged"

# Minimum output thresholds: short outputs pass through untouched.
MIN_LINES = 15
MIN_CHARS = 100
MIN_CAT_BYTES = 500
MIN_REDUCTION_RATIO = 0.10
MAX_REREAD_AGE_SECONDS = 3600

# Shell control-flow keywords that mean "not a simple read-only command".
_COMPOUND_KEYWORDS = frozenset({
    "for", "while", "until", "if", "then", "else", "elif", "fi",
    "do", "done", "case", "esac", "function", "select", "{", "}", "[[", "]]",
})

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SET_FLAGS_RE = re.compile(r"^[-+][euxo]+$")
_SET_OPTIONS = frozenset({"pipefail", "errexit", "nounset", "xtrace"})
_SED_RANGE_RE = re.compile(r"(?:^|;)\s*\d+\s*(?:,\s*(?:\d+|\$))?\s*[pq]\b")
_HEAD_TAIL_NUM_FLAG_RE = re.compile(r"^-[0-9]+$")

# Safe sink redirect targets (never file writes).
_SAFE_REDIRECT_TARGETS = frozenset({
    "/dev/null", "/dev/stderr", "/dev/stdout", "/dev/fd/1", "/dev/fd/2",
})

# Patterns whose output shape is preserved by line-reordering filters (sort/uniq).
_LINE_ORIENTED_PATTERNS = frozenset({
    "search_results", "find", "ls", "list", "disk_stats", "lint", "logs",
})

# Patterns whose output shape is preserved by bare (unflagged) head/tail.
_ALL_KNOWN_PIPE_PATTERNS = _LINE_ORIENTED_PATTERNS | frozenset({
    "git_diff", "git_log", "git_status", "tree", "k8s", "pytest", "jest",
    "build", "sqlite3", "csv",
})

# Trailing filters that do not destroy the producer's output shape.
_ORDER_FILTERS = frozenset({"sort", "uniq"})
_TRIM_FILTERS = frozenset({"head", "tail"})

# Glob / special path characters that disqualify single-file cat matching.
_GLOB_CHARS = frozenset({"*", "?", "["})


def _tokenize_shell(command_str: str) -> list[tuple[str, bool]] | None:
    """Quote-aware shell tokenizer.

    Returns a list of ``(token, was_quoted)`` tuples where unquoted shell
    operators (``|``, ``|&``, ``&&``, ``||``, ``;``, redirects) are split into
    standalone unquoted tokens, while quoted characters (e.g. ``'foo|bar'``,
    ``"a;b"``) remain inside their argument token with ``was_quoted=True``.

    Returns ``None`` on any dangerous or unparseable construct: newlines,
    command/process substitution, heredocs, subshells/braces, bare ``&``,
    unclosed quotes, or ``||``.
    """
    if not command_str or not isinstance(command_str, str):
        return None
    if "\n" in command_str or "\r" in command_str:
        return None

    tokens: list[tuple[str, bool]] = []
    cur: list[str] = []
    cur_quoted = False
    i, n = 0, len(command_str)
    quote = ""

    def flush() -> None:
        nonlocal cur, cur_quoted
        if cur or cur_quoted:
            tokens.append(("".join(cur), cur_quoted))
            cur = []
            cur_quoted = False

    while i < n:
        ch = command_str[i]
        if quote:
            if quote == '"':
                if ch == "\\" and i + 1 < n:
                    nxt = command_str[i + 1]
                    if nxt in ('"', "\\", "$", "`"):
                        cur.append(nxt)
                    else:
                        cur.append("\\" + nxt)
                    i += 2
                    continue
                if ch == "`" or command_str.startswith("$(", i):
                    return None
                if ch == '"':
                    quote = ""
                    i += 1
                    continue
                cur.append(ch)
                i += 1
                continue
            else:
                if ch == "'":
                    quote = ""
                    i += 1
                    continue
                cur.append(ch)
                i += 1
                continue

        # Outside quotes
        if ch in (" ", "\t"):
            flush()
            i += 1
            continue
        if ch == "#" and not cur and not cur_quoted:
            # Comment to end of single line
            break
        if ch == "\\":
            if i + 1 >= n:
                return None
            cur.append(command_str[i + 1])
            cur_quoted = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            cur_quoted = True
            i += 1
            continue
        if ch == "`" or command_str.startswith("$(", i):
            return None
        if command_str.startswith("<(", i) or command_str.startswith(">(", i):
            return None
        if command_str.startswith("<<", i):
            return None
        if ch in "(){}":
            return None
        if command_str.startswith("||", i):
            return None
        if command_str.startswith("&&", i):
            flush()
            tokens.append(("&&", False))
            i += 2
            continue
        if command_str.startswith("|&", i):
            flush()
            tokens.append(("|&", False))
            i += 2
            continue
        if ch in ("|", ";"):
            flush()
            tokens.append((ch, False))
            i += 1
            continue
        # Fd-prefixed redirections like 2>&1, 2>/dev/null, 1>&2
        if ch.isdigit() and not cur and not cur_quoted:
            j = i
            while j < n and command_str[j].isdigit():
                j += 1
            if j < n and command_str[j] in (">", "<"):
                op_end = j + 1
                if op_end < n and command_str[op_end] in (">", "&"):
                    op_end += 1
                    if command_str[op_end - 1] == "&":
                        while op_end < n and command_str[op_end].isdigit():
                            op_end += 1
                tokens.append((command_str[i:op_end], False))
                i = op_end
                continue
        if command_str.startswith("&>>", i):
            flush()
            tokens.append(("&>>", False))
            i += 3
            continue
        if command_str.startswith("&>", i) or command_str.startswith(">&", i):
            flush()
            op_end = i + 2
            while op_end < n and command_str[op_end].isdigit():
                op_end += 1
            tokens.append((command_str[i:op_end], False))
            i = op_end
            continue
        if command_str.startswith(">>", i) or command_str.startswith("<>", i):
            flush()
            tokens.append((command_str[i:i + 2], False))
            i += 2
            continue
        if ch in (">", "<"):
            flush()
            tokens.append((ch, False))
            i += 1
            continue
        if ch == "&":
            # Bare & (backgrounding) is rejected.
            return None
        cur.append(ch)
        i += 1

    if quote:
        return None
    flush()
    return tokens


def _is_redirect_op(tok: str, quoted: bool) -> bool:
    if quoted or not tok:
        return False
    if tok in (">", ">>", "<", "<>", "&>", "&>>", ">&"):
        return True
    i = 0
    while i < len(tok) and tok[i].isdigit():
        i += 1
    rest = tok[i:]
    if not rest:
        return False
    return rest.startswith((">", "<", "&>"))


def _redirect_needs_target(tok: str) -> bool:
    if tok in (">", ">>", "<", "<>", "&>", "&>>"):
        return True
    i = 0
    while i < len(tok) and tok[i].isdigit():
        i += 1
    rest = tok[i:]
    if rest.startswith(">&"):
        return False
    return rest.startswith((">", "<"))


def _strip_safe_redirects(stage: list[tuple[str, bool]]) -> list[str] | None:
    """Strip safe sink redirects (2>&1, >/dev/null). Reject any real file redirect."""
    out: list[str] = []
    i = 0
    while i < len(stage):
        tok, quoted = stage[i]
        if _is_redirect_op(tok, quoted):
            if _redirect_needs_target(tok):
                if i + 1 >= len(stage):
                    return None
                target, _tq = stage[i + 1]
                if target not in _SAFE_REDIRECT_TARGETS:
                    return None
                i += 2
                continue
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _is_harmless_prefix(tokens: list[str]) -> bool:
    if not tokens:
        return True
    head = tokens[0]
    if head == "cd":
        return len(tokens) <= 2 and (len(tokens) == 1 or tokens[1] != "-")
    if head == "export":
        return len(tokens) >= 2 and all(_ASSIGN_RE.match(t) for t in tokens[1:])
    if head == "set":
        rest = tokens[1:]
        if not rest:
            return False
        for j, t in enumerate(rest):
            if _SET_FLAGS_RE.match(t):
                continue
            if t in _SET_OPTIONS and j > 0 and rest[j - 1].endswith("o"):
                continue
            return False
        return True
    return all(_ASSIGN_RE.match(t) for t in tokens)


def _strip_stage_env_and_git_c(tokens: list[str]) -> list[str]:
    """Drop leading K=V assignments and normalize `git -C <dir> <sub>` -> `git <sub>`."""
    idx = 0
    while idx < len(tokens) and _ASSIGN_RE.match(tokens[idx]):
        idx += 1
    rest = tokens[idx:]
    if len(rest) >= 4 and rest[0] == "git" and rest[1] == "-C":
        j = 1
        while j + 1 < len(rest) and rest[j] == "-C":
            j += 2
        rest = ["git"] + rest[j:]
    return rest


def _stage_has_explicit_limit(tokens: list[str], *, strict_legacy_num: bool = False) -> bool:
    """True when a stage expresses explicit output-limiting intent.

    - `head -n ...`, `head --lines ...`, `head -c ...`, `head --bytes ...`
    - `tail -n ...`, `tail --lines ...`, `tail -c ...`, `tail --bytes ...`
    - `sed -n ...` (or bundled short flag with `n`, or range script like `10,40p`)
    When ``strict_legacy_num`` is True, also treats legacy ``head -50`` /
    ``tail -50`` as explicit limits.
    """
    clean = _strip_stage_env_and_git_c(tokens)
    if not clean:
        return False
    cmd = clean[0]
    args = clean[1:]
    if cmd in ("head", "tail"):
        for a in args:
            if a in ("-n", "-c") or a.startswith(("-n", "-c", "--lines", "--bytes")):
                return True
            if strict_legacy_num and _HEAD_TAIL_NUM_FLAG_RE.match(a):
                return True
        return False
    if cmd == "sed":
        for a in args:
            if a == "-n" or (a.startswith("-") and not a.startswith("--") and "n" in a[1:]):
                return True
            if _SED_RANGE_RE.search(a):
                return True
        return False
    return False


def has_explicit_output_limit(command: str, *, strict_legacy_num: bool = False) -> bool:
    """True if any stage in ``command`` explicitly limits output via head -n,
    tail -n, or sed -n / line-range."""
    if not command or not isinstance(command, str):
        return False
    toks = _tokenize_shell(command)
    if toks is None:
        # Fallback regex check on raw string when tokenization rejects (e.g.
        # multiline or complex command) so explicit limits still pass through.
        if re.search(r"\bsed\b[^\n|;&]*\s-[a-zA-Z]*n\b", command):
            return True
        if re.search(r"\b(?:head|tail)\b[^\n|;&]*\s(?:-[nc]\s*\d|--lines|--bytes)", command):
            return True
        return False
    stage: list[tuple[str, bool]] = []
    for tok, quoted in toks:
        if not quoted and tok in ("|", "|&", "&&", ";"):
            clean = _strip_safe_redirects(stage)
            if clean and _stage_has_explicit_limit(clean, strict_legacy_num=strict_legacy_num):
                return True
            stage = []
        else:
            stage.append((tok, quoted))
    if stage:
        clean = _strip_safe_redirects(stage)
        if clean and _stage_has_explicit_limit(clean, strict_legacy_num=strict_legacy_num):
            return True
    return False


def _is_git_show_file_content(tokens: list[str]) -> bool:
    if len(tokens) >= 2 and tokens[0] == "git" and tokens[1] == "show":
        return any(":" in t and not t.startswith("-") for t in tokens[2:])
    return False


def _detect_stage_pattern(tokens: list[str]) -> str | None:
    """Detect the known bash_compress pattern for a single cleaned stage."""
    if not tokens:
        return None
    if _is_git_show_file_content(tokens):
        return None
    if tokens[0] == "git" and len(tokens) >= 2 and tokens[1] == "grep":
        return "search_results"
    try:
        from bash_compress import _detect_pattern
        cmd_str = " ".join(shlex.quote(t) for t in tokens)
        return _detect_pattern(cmd_str)
    except Exception:
        return None


def classify_safe_pipeline(command: str) -> tuple[str, str] | None:
    """Return ``(producer_command, pattern_name)`` if ``command`` is a safe
    read-only pipeline or harmless-prefix chain whose last producer has a
    known compressible output shape. Otherwise return ``None``.
    """
    try:
        toks = _tokenize_shell(command)
        if not toks:
            return None

        # Reject compound shell keywords outside quotes.
        for tok, quoted in toks:
            if not quoted and tok in _COMPOUND_KEYWORDS:
                return None

        # Split into statements on unquoted `&&` and `;`.
        statements: list[list[tuple[str, bool]]] = []
        cur_stmt: list[tuple[str, bool]] = []
        for tok, quoted in toks:
            if not quoted and tok in ("&&", ";"):
                if cur_stmt:
                    statements.append(cur_stmt)
                    cur_stmt = []
            else:
                cur_stmt.append((tok, quoted))
        if cur_stmt:
            statements.append(cur_stmt)
        if not statements:
            return None

        # Strip harmless leading statements (cd, export, set, K=V).
        remaining_stmts: list[list[tuple[str, bool]]] = []
        for stmt in statements:
            # A harmless prefix never contains a pipe.
            has_pipe = any(not q and t in ("|", "|&") for t, q in stmt)
            if not has_pipe and not remaining_stmts:
                clean = _strip_safe_redirects(stmt)
                if clean is None:
                    return None
                if _is_harmless_prefix(clean):
                    continue
            remaining_stmts.append(stmt)

        # Must have exactly ONE producer statement (never mixed output).
        if len(remaining_stmts) != 1:
            return None

        # Split that statement on unquoted `|` / `|&`.
        raw_stages: list[list[tuple[str, bool]]] = []
        cur_stage: list[tuple[str, bool]] = []
        for tok, quoted in remaining_stmts[0]:
            if not quoted and tok in ("|", "|&"):
                if not cur_stage:
                    return None  # empty stage (e.g. leading/double pipe)
                raw_stages.append(cur_stage)
                cur_stage = []
            else:
                cur_stage.append((tok, quoted))
        if not cur_stage:
            return None
        raw_stages.append(cur_stage)

        # Validate every stage: no file redirects, no explicit output limits,
        # and every stage must be strictly read-only.
        from pipeline_analyzer import _is_stage_read_only

        clean_stages: list[list[str]] = []
        for st in raw_stages:
            clean = _strip_safe_redirects(st)
            if not clean:
                return None
            if _stage_has_explicit_limit(clean, strict_legacy_num=True):
                return None
            norm = _strip_stage_env_and_git_c(clean)
            if not norm:
                return None
            ok, _reason = _is_stage_read_only(norm)
            if not ok:
                return None
            clean_stages.append(norm)

        # Walk from the right across shape-preserving trailing filters to find
        # the last producer whose output shape is known.
        trailing_filters: list[str] = []
        producer_idx = len(clean_stages) - 1
        while producer_idx >= 0:
            head = clean_stages[producer_idx][0]
            if producer_idx > 0 and head in (_ORDER_FILTERS | _TRIM_FILTERS):
                trailing_filters.append(head)
                producer_idx -= 1
                continue
            break

        producer = clean_stages[producer_idx]
        pattern = _detect_stage_pattern(producer)
        if not pattern or pattern not in _ALL_KNOWN_PIPE_PATTERNS:
            return None

        # Verify every trailing filter preserves this producer's output shape.
        for filt in trailing_filters:
            if filt in _ORDER_FILTERS and pattern not in _LINE_ORIENTED_PATTERNS:
                return None

        producer_cmd = " ".join(shlex.quote(t) for t in producer)
        return producer_cmd, pattern
    except Exception:
        return None


def compress_pipe(
    command: str,
    stdout: str,
    returncode: int | None = 0,
    stderr: str = "",
) -> str | None:
    """Compress stdout of a safe read-only pipeline deterministically.

    Returns the compressed text if safe and >=10% smaller, else ``None``.
    Never raises (fail-open).
    """
    try:
        if not stdout or len(stdout) < MIN_CHARS:
            return None
        if len(stdout.splitlines()) < MIN_LINES:
            return None

        classified = classify_safe_pipeline(command)
        if classified is None:
            return None
        producer_cmd, _pattern = classified

        from bash_compress import compress as _bc_compress, _looks_like_failure, _strip_ansi
        if _looks_like_failure(returncode, stderr) or (stderr and stderr.strip()):
            return None

        cleaned = _strip_ansi(stdout)
        if len(cleaned) < MIN_CHARS or len(cleaned.splitlines()) < MIN_LINES:
            return None

        compressed = _bc_compress(producer_cmd, cleaned, returncode=returncode or 0, stderr=stderr)
        if not compressed or compressed == cleaned:
            return None

        from token_estimate import estimate_tokens as _est
        orig_tok = _est(cleaned)
        comp_tok = _est(compressed)
        if orig_tok <= 0 or (1.0 - comp_tok / orig_tok) < MIN_REDUCTION_RATIO:
            return None

        return compressed
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Step 3: Unchanged Bash `cat` file re-read detection (bash_reread_unchanged)
# ---------------------------------------------------------------------------

def parse_unranged_cat(command: str, cwd: str = "") -> str | None:
    """Return the resolved absolute file path for a bare unranged single-file
    ``cat <file>`` (or ``cd <dir> && cat <file>``), else ``None``.

    Rejects any flags (except ``--``), multiple files, globs, pipes, redirects,
    or compound constructs.
    """
    try:
        toks = _tokenize_shell(command)
        if not toks:
            return None
        for tok, quoted in toks:
            if not quoted and (tok in _COMPOUND_KEYWORDS or tok in ("|", "|&")):
                return None

        # Split on unquoted `&&` / `;`
        stmts: list[list[tuple[str, bool]]] = []
        cur: list[tuple[str, bool]] = []
        for tok, quoted in toks:
            if not quoted and tok in ("&&", ";"):
                if cur:
                    stmts.append(cur)
                    cur = []
            else:
                cur.append((tok, quoted))
        if cur:
            stmts.append(cur)
        if not stmts:
            return None

        base_dir = cwd
        remaining: list[list[str]] = []
        for stmt in stmts:
            clean = _strip_safe_redirects(stmt)
            if clean is None:
                return None
            if not remaining and _is_harmless_prefix(clean):
                if clean and clean[0] == "cd" and len(clean) == 2:
                    cd_target = os.path.expanduser(clean[1])
                    if os.path.isabs(cd_target):
                        base_dir = cd_target
                    elif base_dir:
                        base_dir = os.path.normpath(os.path.join(base_dir, cd_target))
                    else:
                        return None
                continue
            remaining.append(clean)

        if len(remaining) != 1:
            return None

        cat_toks = _strip_stage_env_and_git_c(remaining[0])
        if len(cat_toks) < 2 or cat_toks[0] != "cat":
            return None

        args = cat_toks[1:]
        if args[0] == "--":
            args = args[1:]
        if len(args) != 1:
            return None

        target = args[0]
        if not target or target == "-" or target.startswith("-"):
            return None
        if any(ch in target for ch in _GLOB_CHARS):
            return None
        if target.startswith("/dev/"):
            return None

        expanded = os.path.expanduser(target)
        if os.path.isabs(expanded):
            return os.path.normpath(expanded)
        if not base_dir:
            return None
        return os.path.normpath(os.path.join(base_dir, expanded))
    except Exception:
        return None


def _entry_proves_full_unchanged_in_context(
    entry: dict[str, Any] | None,
    st: os.stat_result,
    *,
    tool_use_id: str,
    now: float,
) -> bool:
    """Strict proof that ``entry`` represents a full, unchanged, un-edited read
    that is still in context."""
    if not entry or not isinstance(entry, dict):
        return False
    # Exact file identity check (mtime_ns + size_bytes).
    if int(entry.get("mtime_ns", 0) or 0) != st.st_mtime_ns:
        return False
    if int(entry.get("size_bytes", 0) or 0) != st.st_size:
        return False
    # Duplicate hook fire for the same tool_use_id is NOT a second read.
    if tool_use_id and entry.get("last_tool_use_id") == tool_use_id:
        return False
    # Must have a recorded full-file read `[0, 0]`. An Edit/Write clears
    # `ranges_seen` to `[]`, and a ranged Read records `[offset, limit]`.
    ranges_seen = entry.get("ranges_seen") or []
    has_full_read = any(
        isinstance(r, (list, tuple)) and len(r) == 2 and r[0] == 0 and r[1] == 0
        for r in ranges_seen
    )
    if not has_full_read:
        return False
    # If the prior Read was replaced by a skeleton/structure map rather than
    # showing the full file, do not claim the full file is in context.
    if entry.get("last_replacement_type"):
        return False
    # Recency check: only reference a read within the last hour.
    last_access = float(entry.get("last_access") or 0.0)
    if last_access <= 0 or (now - last_access) > MAX_REREAD_AGE_SECONDS:
        return False
    return True


def try_bash_reread_unchanged(
    command: str,
    stdout: str,
    *,
    session_id: str,
    store_id: str,
    transcript_path: str = "",
    cwd: str = "",
    tool_use_id: str = "",
    agent_id: str = "",
    exit_code: int | None = 0,
    stderr: str = "",
) -> str | None:
    """If ``command`` is an unranged ``cat <file>`` of an unchanged file proven
    to be fully shown in this session and still in context, return a short
    deterministic note. Otherwise return ``None`` (caller serves full output).

    Never skeletonizes source code. Fail-open on any doubt or error.
    """
    try:
        if not session_id or not store_id:
            return None
        if exit_code not in (None, 0) or (stderr and stderr.strip()):
            return None
        if not stdout or "\x00" in stdout:
            return None
        if len(stdout) < MIN_CAT_BYTES or len(stdout.splitlines()) < MIN_LINES:
            return None

        file_path = parse_unranged_cat(command, cwd=cwd)
        if not file_path:
            return None

        # Must have a real transcript path so compaction/microcompact
        # invalidation can be verified via compact_markers.py. Any doubt -> raw.
        if not transcript_path:
            return None
        try:
            t_st = os.stat(transcript_path)
            if not stat_module.S_ISREG(t_st.st_mode):
                return None
        except OSError:
            return None

        # Stat target file on disk (must be a regular file).
        try:
            st = os.stat(file_path)
            if not stat_module.S_ISREG(st.st_mode) or st.st_size < MIN_CAT_BYTES:
                return None
        except OSError:
            return None

        from session_store import SessionStore, mark_context_reset
        from read_cache import _scan_for_compaction

        store = SessionStore(store_id)
        try:
            # Respect compaction / microcompact / context_management invalidation.
            if _scan_for_compaction(store, transcript_path):
                if agent_id:
                    store.clear_file_entries()
                else:
                    store.reset_context_state()
                    mark_context_reset(session_id)
                return None

            # Look up by normalized path (and realpath if symlinked).
            real_path = os.path.realpath(file_path)
            entry = store.get_file_entry(file_path)
            if entry is None and real_path != file_path:
                entry = store.get_file_entry(real_path)

            now = time.time()
            if _entry_proves_full_unchanged_in_context(
                entry, st, tool_use_id=tool_use_id, now=now
            ):
                basename = Path(file_path).name
                line_count = len(stdout.splitlines())
                note = (
                    f"[Token Optimizer: {basename} ({line_count} lines, lines 1-{line_count}, "
                    f"{st.st_size} bytes) is unchanged since your earlier "
                    f"full read in this session.]"
                )
                return note

            # Record this unranged full `cat` in file_reads so a subsequent
            # unchanged `cat` in the same session (with no edits or compaction)
            # can be proven in context.
            from token_estimate import estimate_tokens_from_bytes
            new_entry = {
                "mtime_ns": st.st_mtime_ns,
                "size_bytes": st.st_size,
                "ranges_seen": [[0, 0]],
                "tokens_est": estimate_tokens_from_bytes(st.st_size),
                "read_count": int((entry or {}).get("read_count", 0) or 0) + 1,
                "last_access": now,
                "last_replacement_fingerprint": "",
                "last_replacement_type": "",
                "repeat_replacement_count": 0,
                "last_structure_reason": "",
                "last_structure_confidence": 0.0,
                "last_tool_use_id": tool_use_id or "",
            }
            store.upsert_file_entry(file_path, new_entry)
            return None
        finally:
            store.close()
    except Exception:
        return None


def archive_and_wrap(command: str, raw_stdout: str, compressed: str, session_id: str = "") -> str | None:
    """Archive raw stdout and attach an expand pointer (fail-closed to None if
    archiving fails for outputs > 500 bytes). Uses a deterministic archive key
    derived from session_id, command, and raw_stdout so identical inputs yield
    identical outputs (prompt-cache stability)."""
    try:
        from bash_compress import _enforce_baseline_invariant
        from archive_result import (
            archive_entry_exists,
            archive_original,
            build_archive_pointer,
        )
        sid = session_id or os.environ.get("CLAUDE_SESSION_ID", "")
        key = hashlib.sha256(
            f"{sid}|{command}|{raw_stdout}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        if archive_original(raw_stdout, sid, key, "Bash") is None:
            return None
        if not archive_entry_exists(sid, key):
            return None
        compressed = build_archive_pointer(compressed, len(raw_stdout), key)
        archive_key = key
        try:
            compressed = _enforce_baseline_invariant(compressed, raw_stdout, archive_key)
        except Exception:
            pass
        return compressed
    except Exception:
        return None


def hook_try_cat_reread(
    command: str,
    stdout: str,
    payload: dict[str, Any],
    store_id: str,
    exit_code: int | None,
    stderr: str,
) -> tuple[bool, str | None]:
    """Hook adapter for Step 3 (bash_reread_unchanged).

    Returns ``(is_bare_cat, replacement_stdout)``. When ``is_bare_cat`` is True
    and ``replacement_stdout`` is None, the caller must return raw stdout
    verbatim (never let generic fallback skeletonize or chop a file read).
    """
    cwd = str(payload.get("cwd", "") or "")
    if parse_unranged_cat(command, cwd=cwd) is None:
        return False, None
    sid = os.environ.get("CLAUDE_SESSION_ID", "")
    note = try_bash_reread_unchanged(
        command, stdout,
        session_id=sid,
        store_id=store_id,
        transcript_path=str(payload.get("transcript_path", "") or ""),
        cwd=cwd,
        tool_use_id=str(payload.get("tool_use_id", "") or ""),
        agent_id=str(payload.get("agent_id", "") or ""),
        exit_code=exit_code,
        stderr=stderr,
    )
    if note is None:
        return True, None
    wrapped = archive_and_wrap(command, stdout, note, session_id=sid)
    return True, wrapped


def hook_try_pipe_compress(
    command: str,
    stdout: str,
    exit_code: int | None,
    stderr: str,
) -> tuple[str, str, str] | None:
    """Hook adapter for Step 2 (pipe_compress).

    Returns ``(wrapped_output, cleaned_original, pattern)`` on success, else None.
    """
    cls = classify_safe_pipeline(command)
    if cls is None:
        return None
    comp = compress_pipe(command, stdout, returncode=exit_code, stderr=stderr)
    if comp is None:
        return None
    wrapped = archive_and_wrap(command, stdout, comp)
    if wrapped is None:
        return None
    from bash_compress import _strip_ansi
    return wrapped, _strip_ansi(stdout), cls[1]

