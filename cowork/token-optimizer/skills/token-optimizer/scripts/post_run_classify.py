#!/usr/bin/env python3
"""Classify a Bash command AFTER it ran, to pick a compressor.

``is_read_only_pipeline`` answers "is this command safe to rewrite before it
runs". After the command has run, the PostToolUse hook only needs to know which
compressor fits the output, and the read-only check rejects the shapes agents
write today: ``cd /repo && git diff``, ``export X=1; pytest``, ``git -C /repo
status``. This module strips the harmless leading statements and returns the
one command that produced the output, or ``None`` when that is not clear.

Harmless leading statements (joined by ``;``, ``&&`` or a newline):
  ``cd <path>``, ``export A=b ...``, ``A=b ...``, ``set -e`` style flag sets.
``git -C <path> <sub>`` maps to ``git <sub>``; leading ``A=b`` words before
the command are dropped (the compressor ignores them anyway).

Returns ``None`` for anything that is not exactly one remaining command or
pipeline: several commands, loops, heredocs, ``$(...)``, backticks, ``||``,
background ``&``, subshells and brace groups.

Modes (``TOKEN_OPTIMIZER_POST_RUN_CLASSIFY``):
  ``1`` / ``on`` (default) -- compress newly eligible results, log measured savings.
  ``shadow``           -- compute what would be removed, log it at the
                          opportunity tier, change nothing the model sees.
  ``0`` / ``off``      -- kill switch: the classifier is never consulted.

Only the pattern-specific compressors in ``ALLOWED_PATTERNS`` may act on a
newly eligible result. The generic fallback never does.
"""

from __future__ import annotations

import os
import re
import shlex

ENV_VAR = "TOKEN_OPTIMIZER_POST_RUN_CLASSIFY"
_DEFAULT_MODE = "on"

ALLOWED_PATTERNS = frozenset({
    "git_diff", "search_results", "disk_stats", "pytest", "sqlite3", "ls",
    "git_status",
})

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SET_FLAGS_RE = re.compile(r"^[-+][euxo]+$")
_SET_OPTIONS = frozenset({"pipefail", "errexit", "nounset", "xtrace"})
# Shell keywords that start compound commands; any of them means "not one
# simple command" and the classifier gives up.
_COMPOUND_WORDS = frozenset({
    "for", "while", "until", "if", "then", "else", "elif", "fi", "do", "done",
    "case", "esac", "function", "select", "{", "}", "[[", "]]",
})


def mode() -> str:
    """Return ``on``, ``shadow`` or ``off`` from the env (default on)."""
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return "off"
    if raw in ("1", "on", "true", "yes"):
        return "on"
    if raw == "shadow":
        return "shadow"
    return _DEFAULT_MODE


def _split_statements(command: str) -> list[str] | None:
    """Split on unquoted ``;``, ``&&`` and newlines. ``None`` = give up."""
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(command)
    quote = ""
    while i < n:
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                cur.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
            elif quote == '"' and (ch == "`" or command.startswith("$(", i)):
                return None
            cur.append(ch)
            i += 1
            continue
        if ch == "\\":
            if i + 1 < n and command[i + 1] == "\n":
                i += 2  # line continuation
                continue
            cur.append(command[i:i + 2])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "#" and (not cur or cur[-1][-1:].isspace()):
            # Comment to end of line.
            while i < n and command[i] != "\n":
                i += 1
            continue
        if ch == "`" or command.startswith("$(", i) or command.startswith("<<", i):
            return None
        if ch in "(){}":
            return None
        if command.startswith("||", i):
            return None
        if command.startswith("&&", i):
            out.append("".join(cur))
            cur = []
            i += 2
            continue
        if ch == "&":
            # `&>` / `>&` / `2>&1` are redirections; a lone `&` backgrounds.
            prev = cur[-1][-1:] if cur else ""
            nxt = command[i + 1:i + 2]
            if prev in (">", "<") or nxt == ">":
                cur.append(ch)
                i += 1
                continue
            return None
        if ch in (";", "\n"):
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if quote:
        return None
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _is_harmless(tokens: list[str]) -> bool:
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
            return False  # bare `set` prints every variable
        ok = True
        for j, t in enumerate(rest):
            if _SET_FLAGS_RE.match(t):
                continue
            if t in _SET_OPTIONS and j > 0 and rest[j - 1].endswith("o"):
                continue
            ok = False
        return ok
    return all(_ASSIGN_RE.match(t) for t in tokens)


_WORD = r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|[^\s'";&|<>])+"""
_LEAD_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=" + r"(?:" + _WORD + r")?" + r"\s+)+")
_GIT_C_RE = re.compile(r"^git((?:\s+-C\s+" + _WORD + r")+)(?=\s)")


def _rewrite_effective(stmt: str) -> str | None:
    """Drop leading ``A=b`` words and map ``git -C <p>`` to ``git``.

    Works on the original text so redirections and quoting reach the
    read-only check exactly as written.
    """
    text = _LEAD_ASSIGN_RE.sub("", stmt.strip(), count=1)
    if text.startswith("git") and re.match(r"^git\s+-C\b", text):
        m = _GIT_C_RE.match(text)
        if not m:
            return None
        text = "git" + text[m.end():]
    return text.strip() or None


def _literal_echo_boundary(statements):
    """A single literal echo may label one producer's output."""
    echoes = [s for s in statements if re.fullmatch(r"echo\s+[A-Za-z0-9_][A-Za-z0-9_.-]*", s)]
    return echoes[0].split()[1] if len(echoes) == 1 else None


def echo_boundary_preserved(command, raw, compressed):
    statements = _split_statements(command)
    if not statements:
        return True
    boundary = _literal_echo_boundary(statements)
    if boundary is None:
        return True
    return boundary in raw.splitlines() and boundary in compressed.splitlines()


def unsafe_compound_output(command: str) -> bool:
    """Avoid using single-output renderers on mixed command output."""
    statements = _split_statements(command)
    if statements is None:
        return True
    producers = []
    for statement in statements:
        try:
            tokens = shlex.split(statement)
        except ValueError:
            return True
        if not _is_harmless(tokens):
            producers.append(statement)
    if len(producers) < 2:
        return False
    if len(producers) == 2 and _literal_echo_boundary(producers) is not None:
        return False
    from bash_compress import _detect_pattern
    from build_output_compress import classify as build_command, _command_eligible_for_shape_fallback
    return any(_detect_pattern(stmt) is not None or build_command(stmt)
               or _command_eligible_for_shape_fallback(stmt) for stmt in producers)


def classify(command: str) -> str | None:
    """Return the single effective command, or ``None``.

    ``None`` means: several commands remain, the shape is compound, or the
    command cannot be parsed. The result is a normalised command string meant
    only for picking a compressor; it is never executed.
    """
    if not command or not isinstance(command, str):
        return None
    statements = _split_statements(command)
    if not statements:
        return None
    remaining: list[str] = []
    for stmt in statements:
        try:
            lexer = shlex.shlex(stmt, posix=True, punctuation_chars="|")
            lexer.whitespace_split = True
            toks = list(lexer)
        except ValueError:
            return None
        if any(t in _COMPOUND_WORDS for t in toks):
            return None
        if _is_harmless(toks):
            continue
        remaining.append(stmt)
    if len(remaining) != 1:
        return None
    return _rewrite_effective(remaining[0])


def _is_file_content_print(effective: str) -> bool:
    """`git show <rev>:<path>` prints file content; never newly eligible."""
    try:
        toks = shlex.split(effective)
    except ValueError:
        return True
    if len(toks) >= 2 and toks[0] == "git" and toks[1] == "show":
        return any(":" in t and not t.startswith("-") for t in toks[2:])
    return False


# Command words that can map to an ALLOWED_PATTERNS compressor (see
# bash_compress._detect_pattern). Checked before importing bash_compress, whose
# import costs tens of ms, so `cd x && sed -n ...` and other rejects pay ~0.1 ms.
# Over-inclusive on purpose: _detect_pattern still makes the real decision.
_CANDIDATE_HEADS = frozenset({
    "git", "grep", "rg", "ag", "ack", "wc", "du", "df", "ls", "sqlite3",
    "pytest", "py.test", "python", "python3", "rspec", "mocha", "karma",
    "cypress", "playwright", "npx", "go", "cargo", "tox", "nox",
    "nox-sessions", "gradle", "gradlew", "./gradlew", "mvn", "deno", "bun",
    "ava", "npm", "jest", "vitest",
})


def _has_candidate_head(effective: str) -> bool:
    for stage in re.split(r"\|&?", effective):
        words = [w for w in stage.split() if not _ASSIGN_RE.match(w)]
        if words and words[0].strip("'\"") in _CANDIDATE_HEADS:
            return True
    return False


def eligible_effective(command: str) -> str | None:
    """Effective command when a pattern-specific compressor may act, else None.

    The effective command must itself pass the same read-only pipeline check
    a bare command passes, and its detected pattern must be in
    ``ALLOWED_PATTERNS``. Fail closed on any error.
    """
    try:
        eff = classify(command)
        if eff is None or eff == command.strip():
            return None
        if _is_file_content_print(eff) or not _has_candidate_head(eff):
            return None
        from pipeline_analyzer import is_read_only_pipeline
        ok, _ = is_read_only_pipeline(eff)
        if not ok:
            return None
        from bash_compress import _detect_pattern
        if _detect_pattern(eff) not in ALLOWED_PATTERNS:
            return None
        return eff
    except Exception:
        return None


SHADOW_FEATURE = "post_run_classify_shadow"


def shadow_measure(command: str, effective: str, stdout: str, stderr: str,
                   exit_code, has_error_patterns) -> int:
    """Shadow mode: log what the pattern compressor WOULD remove. Never raises.

    Runs the same failure and error-word gates as the live path, then the
    compressor on the effective command. Writes one opportunity-tier row when
    the compressor would remove at least 10%, and nothing else: no archive,
    no dedup store, no change to the output. Returns tokens it would remove.
    """
    try:
        from bash_compress import compress, _looks_like_failure, _strip_ansi
        if _looks_like_failure(exit_code, stderr):
            return 0
        cleaned = _strip_ansi(stdout)
        if has_error_patterns(cleaned):
            return 0
        compressed = compress(effective, cleaned, returncode=exit_code, stderr=stderr)
        from token_estimate import estimate_tokens as _est
        orig, comp = _est(cleaned), _est(compressed or "")
        if not compressed or compressed == cleaned or orig == 0 or (1.0 - comp / orig) < 0.10:
            return 0
        from compression_log import log_compression_event
        from credential_patterns import redact_credentials as _redact
        log_compression_event(
            feature=SHADOW_FEATURE,
            session_id=os.environ.get("CLAUDE_SESSION_ID", ""),
            command_pattern=_redact(command)[:100],
            tier="opportunity",
            verified=False,
            detail="shadow",
            original_tokens=orig,
            compressed_tokens=comp,
        )
        return orig - comp
    except Exception:
        return 0
