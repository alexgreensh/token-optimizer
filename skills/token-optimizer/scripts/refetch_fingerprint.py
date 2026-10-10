#!/usr/bin/env python3
"""Shared re-fetch fingerprint for Token Optimizer (self-healing).

SINGLE SOURCE OF TRUTH for the archive de-duplication fingerprint. Imported by
archive_result.py (writes the fingerprint into the archive manifest at
PostToolUse) and refetch_guard.py (recomputes it at PreToolUse to detect an
identical re-fetch). Keeping the writer and reader on one function prevents the
two from drifting apart — the class of gap that caused a regression.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# SINGLE SOURCE OF TRUTH for the manifest field name the writer stores and the
# guard matches on. Referencing this constant from both sides means a rename is a
# one-line change that can't silently break the match (the exact drift mode:
# a renamed field makes `entry.get("args_hash")` return None and the guard stops
# denying with zero error).
ARGS_HASH_KEY = "args_hash"


# Tools that OBSERVE live state (a browser page, a screen). Identical arguments
# do not mean an identical result: a screenshot or page read after a click is
# new data, so the re-fetch guard must never redirect these to an old archive.
# Matched with fnmatch against the full tool name; fnmatch is case-sensitive on
# POSIX, so names are lowercased first.
# Hosts spell the same server differently (mcp__claude-in-chrome__,
# mcp__Claude_in_Chrome__, mcp__plugin_claude-in-chrome_claude-in-chrome__), and
# some servers carry the keyword in the tool name instead (mcp__my-tools__
# browser_click), so match on either segment. Over-matching only means the
# guard never blocks that tool, which is the safe direction: the worst a broad
# substring match does is leave a tool un-denied, never deny a legit tool.
LIVE_STATE_TOOL_PATTERNS: tuple[str, ...] = (
    # Keyword in the SERVER segment: mcp__browser__click.
    "mcp__*chrome*__*",
    "mcp__*playwright*__*",
    "mcp__*puppeteer*__*",
    "mcp__*browser*__*",
    "mcp__*browse*__*",
    "mcp__*chromium*__*",
    "mcp__*devtools*__*",
    "mcp__*computer?use*__*",
    "mcp__*computeruse*__*",
    # Keyword in the TOOL segment (after the last __): a server that groups
    # many tools names its browser tools by prefix — mcp__my-tools__browser_click.
    "mcp__*__*chrome*",
    "mcp__*__*playwright*",
    "mcp__*__*puppeteer*",
    "mcp__*__*browser*",
    "mcp__*__*browse*",
    "mcp__*__*chromium*",
    "mcp__*__*devtools*",
    "mcp__*__*computer?use*",
    "mcp__*__*computeruse*",
    # Observer verbs that live in the tool segment on a generic server:
    # mcp__x__take_screenshot, mcp__x__page_snapshot, mcp__x__get_page_state.
    "mcp__*__*screenshot*",
    "mcp__*__*snapshot*",
    "mcp__*__*page_state*",
)

# Opt-in allowlist for servers whose live-state tools match no keyword:
# TOKEN_OPTIMIZER_LIVE_STATE_TOOLS="mcp__acme__screen*,mcp__acme__scrape_*".
# Comma-separated fnmatch patterns, matched against the full (lowercased)
# tool name. A match has THREE effects, all in the safe direction: the
# re-fetch guard never denies the tool, the archive writer stores
# args_hash=None (the result is never fingerprinted), and the archive footer
# tells the model to call the tool again for current state. A broad glob like
# mcp__* therefore disables the whole re-fetch loop — strictly stronger than
# TOKEN_OPTIMIZER_REFETCH_GUARD_WINDOW_SECONDS=0, which only lifts the deny.
_LIVE_STATE_TOOLS_ENV = "TOKEN_OPTIMIZER_LIVE_STATE_TOOLS"

# A pathological allowlist value must not multiply per-call match latency:
# at most this many comma-separated patterns are honoured.
_LIVE_STATE_TOOLS_MAX_PATTERNS = 64

# Parsed once per process: hooks spawn a fresh interpreter per call, but
# within one process (the archive writer, in-process tests) a repeated
# classification must not re-split the value or re-read settings.json.
# The sentinel marks "not yet read"; a failed read is NOT cached so a
# transient settings error does not stick.
_LIVE_STATE_PATTERNS_UNSET = object()
_LIVE_STATE_PATTERNS_CACHE: tuple[str, ...] | object = _LIVE_STATE_PATTERNS_UNSET

# The guard exists to break an immediate loop (the model re-issuing the call it
# just got a pointer for). Past this window an identical call is far more likely
# a deliberate re-check of live data (an inbox, a chat, a calendar), and serving
# the old archive would hand the model stale data as if it were current.
REFETCH_GUARD_WINDOW_SECONDS = 300


def _env_live_state_patterns() -> tuple[str, ...]:
    """Comma-separated fnmatch patterns from TOKEN_OPTIMIZER_LIVE_STATE_TOOLS.

    Read the same way the sibling TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS knob is
    read (archive_result.py): process environment first, then the env block of
    the user's global settings.json, so hosts that do not inject settings env
    into hook subprocesses still honour it.

    Parsed once per process and capped: hooks spawn a fresh interpreter per
    call, but within one process (the archive writer, an in-process test, the
    delegate seam) a repeated call must not re-split the value, and a
    pathological 20k-pattern value must not multiply match latency.

    Fail-open on bad input: a malformed value or a broken settings file yields
    no extra patterns rather than breaking classification. A failed read is
    NOT cached, so a transient settings read error does not stick.
    """
    global _LIVE_STATE_PATTERNS_CACHE
    if _LIVE_STATE_PATTERNS_CACHE is not _LIVE_STATE_PATTERNS_UNSET:
        return _LIVE_STATE_PATTERNS_CACHE
    try:
        raw = os.environ.get(_LIVE_STATE_TOOLS_ENV, "").strip()
        if not raw:
            # Lazy import: keeps this module dependency-free for readers that
            # only need tool_fingerprint, and any failure stays inside the
            # fail-open except.
            from runtime_env import settings_env_value

            raw = settings_env_value(_LIVE_STATE_TOOLS_ENV)
        parsed = tuple(
            p.strip().lower() for p in raw.split(",") if p.strip()
        )[:_LIVE_STATE_TOOLS_MAX_PATTERNS]
    except Exception:
        return ()
    _LIVE_STATE_PATTERNS_CACHE = parsed
    return parsed


def is_live_state_tool(tool_name: str) -> bool:
    """True for tools whose result depends on live external state. Never raises."""
    try:
        import fnmatch
        name = (tool_name or "").lower()
        return any(
            fnmatch.fnmatch(name, pat)
            for pat in LIVE_STATE_TOOL_PATTERNS + _env_live_state_patterns()
        )
    except Exception:
        return False


def tool_fingerprint(tool_name: str, tool_input) -> str:
    """Stable 16-hex fingerprint of an MCP tool call (name + normalized args).

    Two calls to the same tool with identical arguments produce the same
    fingerprint, so an exact re-fetch is detectable. Dict-key-order insensitive;
    never raises (falls back to repr for exotic, non-JSON-serializable inputs).
    """
    try:
        args = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args = repr(tool_input)
    raw = f"{tool_name}\x00{args}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def measure_py_path() -> str:
    """Absolute path to measure.py (this module ships in the same scripts dir).

    Shared by the archive footer and the guard's deny reason so the `expand`
    command they print can never diverge. Correct at any install layout because
    it resolves from this file's own location.
    """
    return str(Path(__file__).resolve().parent / "measure.py")


def expand_command(key: str) -> str:
    """The exact Bash command that retrieves an archived result — one source of
    truth for both the archive footer and the guard's deny reason."""
    return f"python3 {shell_path(measure_py_path())} expand {key}"


_SHELL_SAFE_PATH = re.compile(r"[A-Za-z0-9_./:@%+=,-]+\Z")


def shell_path(path: str) -> str:
    """A path the model can paste into Bash, Git Bash or PowerShell unchanged.

    On Windows backslashes become forward slashes: Python opens C:/x, and an
    unquoted backslash is an escape in Bash. Anything beyond plain path
    characters, a space above all, is single-quoted, which Bash and PowerShell
    both read literally. A path that needs no quoting is returned as is.
    """
    text = str(path)
    if os.name == "nt":
        text = text.replace("\\", "/")
    if _SHELL_SAFE_PATH.match(text):
        return text
    if "'" not in text:
        return f"'{text}'"
    # A single quote in the path: double quotes, with Bash's four specials escaped.
    for ch in ("\\", '"', "$", "`"):
        text = text.replace(ch, "\\" + ch)
    return f'"{text}"'
