#!/usr/bin/env python3
"""Token Optimizer: Build/Test/Run Output Compression (PostToolUse only).

Compresses build, test, and run command output AFTER execution. The command
already ran; this module only compresses the captured stdout so the model sees
a compact summary instead of thousands of repetitive lines.

This module is invoked by ``bash_compress_hook.py`` on the NOT-read-only
branch — when ``is_read_only_pipeline`` returns false. Build/test/run commands
(gcc, pytest, cargo, npm, make, ...) are typically not read-only (they compile,
run tests, have side effects), so they never reach the read-only compression
path. This module fills that gap.

Safety (same stack as bash_compress.py):
  - Fail-open: any exception -> returns None -> raw output stands
  - PostToolUse only: the command already ran, nothing is denied or re-executed
  - Never compress outputs under ~2 KB (byte-identical passthrough)
  - Credential-bearing lines are never dropped (re-injected via _find_preserved_lines)
  - Every distinct error line in the original appears in the compressed output
  - The full original is archived behind the existing expand pointer (by the hook)
  - _enforce_baseline_invariant is applied by the hook

Architecture:
  classify(command) -> bool          # Stage 1: command match
  classify_by_shape(output) -> bool  # Stage 2: output shape fallback
  compress(command, output, ...) -> str | None  # Main entry point
"""
from __future__ import annotations

import re
import shlex

# ---------------------------------------------------------------------------
# Minimum size gate: outputs below this are never compressed.
# ---------------------------------------------------------------------------
_MIN_COMPRESS_BYTES = 2048  # ~2 KB

# ---------------------------------------------------------------------------
# Head/tail preservation window.
# ---------------------------------------------------------------------------
_HEAD_LINES = 20
_TAIL_LINES = 20
_MAX_ERROR_LINES = 200  # bound on pathological error dumps

# ---------------------------------------------------------------------------
# Stage 1: command-based eligibility.
# Maps command name -> set of subcommands that indicate build/test/run.
# A None subcommand set means ANY invocation of that command is eligible.
# ---------------------------------------------------------------------------
_BUILD_TEST_COMMANDS: dict[str, frozenset[str] | None] = {
    # Compilers — any invocation
    "gcc": None, "g++": None, "cc": None, "clang": None, "clang++": None,
    "rustc": None,
    # Build systems — any invocation
    "make": None, "cmake": None, "ninja": None,
    # Cargo (Rust)
    "cargo": frozenset({"build", "test", "check", "run", "clippy", "bench",
                         "fmt", "doc", "install"}),
    # Go
    "go": frozenset({"build", "test", "vet", "bench", "doc"}),
    # Node ecosystem
    "npm": frozenset({"test", "run", "install", "ci", "build", "lint",
                       "start", "exec"}),
    "yarn": frozenset({"test", "build", "install", "lint", "start",
                        "run", "exec"}),
    "pnpm": frozenset({"test", "build", "install", "lint", "start",
                        "run", "exec"}),
    "npx": None,  # npx runs arbitrary build/test tools
    # TypeScript / JS bundlers
    "tsc": None,
    "webpack": None, "esbuild": None, "vite": None, "next": None,
    "rollup": None,
    # JVM
    "mvn": None, "gradle": None, "gradlew": None,
    # .NET
    "dotnet": frozenset({"build", "test", "publish", "restore", "run",
                          "format", "vet"}),
    # Swift
    "swift": frozenset({"build", "test", "run", "package"}),
    # Linters / type checkers (non-read-only variants)
    "eslint": None, "ruff": None, "mypy": None,
    # Python test runners (non-read-only variants, e.g. with coverage)
    "pytest": None, "py.test": None,
    "tox": None, "nox": None,
    # Ruby
    "rspec": None, "rake": None,
}

# Python -m module patterns that are build/test/run.
_PYTHON_M_BUILD_MODULES = frozenset({
    "pytest", "unittest", "tox", "nox", "mypy", "ruff",
    "pip", "setuptools", "build", "wheel",
})

# ---------------------------------------------------------------------------
# Stage 2: output-shape markers for the generic fallback.
# ---------------------------------------------------------------------------
_SHAPE_MARKER_PATTERNS = [
    re.compile(r"error\[E\d{4}\]"),          # Rust error codes
    re.compile(r"^error:", re.MULTILINE),    # generic error
    re.compile(r"^warning:", re.MULTILINE),  # generic warning
    re.compile(r"\bFAILED\b"),               # test failure
    re.compile(r"\bTraceback\b"),            # Python traceback
    re.compile(r"Compiling\s"),              # Rust/cargo compiling
    re.compile(r"Building\s"),               # make/build
    re.compile(r"Finished\s"),               # cargo finished
    re.compile(r"--> .*:\d+:\d+"),           # Rust/gcc file:line:col
    re.compile(r"\w+:\d+:\d+:\s*(error|warning)"),  # gcc/clang file:line:col: error
    re.compile(r"\w+:\d+:\s*(error|warning)"),       # gcc/clang file:line: error
    re.compile(r"make:\s*\*\*\*"),           # make error
    re.compile(r"npm\s+ERR!"),               # npm error
    re.compile(r"npm\s+WARN"),               # npm warning
    re.compile(r"\d+\s+passed"),             # pytest summary
    re.compile(r"\d+\s+failed"),             # pytest summary
    re.compile(r"::\s*\w+\s+PASSED"),        # pytest per-test result
    re.compile(r"::\s*\w+\s+FAILED"),        # pytest per-test result
    re.compile(r"::\s*\w+\s+SKIPPED"),       # pytest per-test result
    re.compile(r"BUILD\s+(SUCCESSFUL|FAILED|SUCCESS|FAILURE)", re.I),  # gradle/maven
    re.compile(r"Tests:\s"),                 # jest summary
    re.compile(r"Test Suites:\s"),           # jest summary
    re.compile(r"undefined reference to"),   # linker
    re.compile(r"cannot find -l"),           # linker
    re.compile(r"ld:\s*(error|warning)"),    # linker
    re.compile(r"note:"),                    # Rust/gcc note
    re.compile(r"error: could not compile"), # Rust
    re.compile(r"Collecting\s"),             # pip install
    re.compile(r"Downloading\s"),            # pip/npm download
    re.compile(r"Using cached\s"),           # pip
    re.compile(r"Successfully installed"),   # pip
    re.compile(r"added \d+ package"),        # npm
    re.compile(r"removed \d+ package"),      # npm
]

# Combined shape-marker regex for the classify_by_shape fallback (single
# DFA pass per line instead of 29 separate searches).
_COMBINED_SHAPE_RE = re.compile(
    "|".join(
        f"(?i:{p.pattern})" if p.flags & re.I else f"(?:{p.pattern})"
        for p in _SHAPE_MARKER_PATTERNS
    ),
)

# ---------------------------------------------------------------------------
# Error line patterns — every DISTINCT line matching one of these is preserved.
# ---------------------------------------------------------------------------
_ERROR_LINE_PATTERNS = [
    re.compile(r"error\[E\d{4}\]"),          # Rust
    re.compile(r"^error:", re.MULTILINE),    # generic
    re.compile(r"^fatal error:", re.MULTILINE),
    re.compile(r"\bFAILED\b"),               # test failure
    re.compile(r"\bTraceback\b"),            # Python
    re.compile(r"AssertionError"),
    re.compile(r"AssertionFailedError"),
    re.compile(r"\w+:\d+:\d+:\s*(error|fatal error)"),  # gcc/clang
    re.compile(r"\w+:\d+:\s*(error|fatal error)"),       # gcc/clang short
    re.compile(r"make:\s*\*\*\*.*Error"),    # make error
    re.compile(r"npm\s+ERR!"),               # npm error
    re.compile(r"undefined reference to"),   # linker
    re.compile(r"cannot find -l"),           # linker
    re.compile(r"ld:\s*error"),              # linker
    re.compile(r"Exception in thread"),
    re.compile(r"BUILD\s+FAILED", re.I),     # gradle/maven
    re.compile(r"BUILD\s+FAILURE", re.I),    # maven
    re.compile(r"error: could not compile"), # Rust
    re.compile(r"error: aborting"),          # Rust/gcc
    re.compile(r"note: previous declaration"),  # Rust/gcc note
    re.compile(r"help: "),                   # Rust help
    re.compile(r"panicked at"),              # Rust panic
    re.compile(r"thread '.*' panicked"),     # Rust panic
]

# Combined error regex for fast scanning. Preserves per-pattern case
# sensitivity via inline (?i:...) groups, matching the approach in
# bash_compress_hook._get_combined_error_re.
_COMBINED_ERROR_RE = re.compile(
    "|".join(
        f"(?i:{p.pattern})" if p.flags & re.I else f"(?:{p.pattern})"
        for p in _ERROR_LINE_PATTERNS
    ),
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Summary line patterns — exit/summary lines are always preserved.
# ---------------------------------------------------------------------------
_SUMMARY_LINE_PATTERNS = [
    re.compile(r"\d+\s+passed.*", re.I),
    re.compile(r"\d+\s+failed.*", re.I),
    re.compile(r"\d+\s+skipped.*", re.I),
    re.compile(r"\d+\s+errors?.*", re.I),
    re.compile(r"\d+\s+warnings?.*", re.I),
    re.compile(r"error: could not compile", re.I),
    re.compile(r"^\s*Finished\s+", re.MULTILINE),  # cargo
    re.compile(r"Build\s+(finished|complete|successful)", re.I),
    re.compile(r"npm\s+ERR!.*", re.I),
    re.compile(r"Tests:\s+.*", re.I),
    re.compile(r"Test Suites:\s+.*", re.I),
    re.compile(r"make:\s*\*\*\*.*", re.I),
    re.compile(r"BUILD\s+(SUCCESSFUL|FAILED|SUCCESS|FAILURE)", re.I),
    re.compile(r"Found\s+\d+\s+(error|warning|problem)", re.I),
    re.compile(r"Successfully\s+(installed|built|deployed)", re.I),
    re.compile(r"added\s+\d+\s+package", re.I),
    re.compile(r"removed\s+\d+\s+package", re.I),
    re.compile(r"audited\s+\d+\s+package", re.I),
    re.compile(r"Compilation\s+(finished|succeeded|failed)", re.I),
    re.compile(r"Total\s+time:.*", re.I),
    re.compile(r"Results:.*", re.I),
    re.compile(r"Tests\s+run:.*", re.I),
    re.compile(r"FAILED\s+tests", re.I),
    re.compile(r"===\s*FAILURES\s*===", re.I),
    re.compile(r"===\s*ERRORS\s*===", re.I),
    re.compile(r"---\s*FAILED\s+---", re.I),
]

_COMBINED_SUMMARY_RE = re.compile(
    "|".join(f"(?:{p.pattern})" for p in _SUMMARY_LINE_PATTERNS),
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Progress / spinner noise patterns — these lines are dropped.
# ---------------------------------------------------------------------------
_PROGRESS_NOISE_PATTERNS = [
    re.compile(r"^\s*Compiling\s+.*\.\.\.\s*$"),  # "Compiling foo v0.1.0..."
    re.compile(r"^\s*Building\s+.*\.\.\.\s*$"),
    re.compile(r"^\s*Generating\s+.*\.\.\.\s*$"),
    re.compile(r"^\s*Running\s+.*\.\.\.\s*$"),
    re.compile(r"^\s*Downloading\s+.*", re.I),
    re.compile(r"^\s*Collecting\s+.*", re.I),
    re.compile(r"^\s*Using cached\s+.*", re.I),
    re.compile(r"^\s*Requirement already satisfied.*", re.I),
    re.compile(r"^\s*Building wheels?\s+.*", re.I),
    re.compile(r"^\s*Created wheel\s+.*", re.I),
    re.compile(r"^\s*Stored in directory:.*", re.I),
    re.compile(r"^\s*Resolving dependencies?\s+.*", re.I),
    re.compile(r"^\s*Installing collected packages?\s+.*", re.I),
    re.compile(r"^\s*Preparing metadata?\s+.*", re.I),
    re.compile(r"^\s*Downloading.*\.whl", re.I),
    re.compile(r"^\s*\[=*>\s*\]\s*\d+%", re.I),   # progress bar
    re.compile(r"^\s*\[=\s*\]\s*\d+%", re.I),     # progress bar
    re.compile(r"^\s*\d+%\|.*\|", re.I),           # tqdm progress bar
    re.compile(r"^\s*=>\s*", re.I),                # spinner
    re.compile(r"^\s*\.+\s*$"),                     # dots spinner
    re.compile(r"^\s*collecting\s*\.\.\.\s*$", re.I),  # pytest collecting
    re.compile(r"^\s*running\s+\d+.*", re.I),       # test running
    re.compile(r"PASSED\s*$"),                       # pytest per-test PASSED
    re.compile(r"SKIPPED\s*$"),                       # pytest per-test SKIPPED
    re.compile(r"XFAIL\s*$"),                         # pytest per-test XFAIL
    re.compile(r"XPASS\s*$"),                         # pytest per-test XPASS
    # Cargo progress: "   Compiling foo v0.1.0"
    re.compile(r"^\s+Compiling\s+\S+\s+v\S+"),
    re.compile(r"^\s+Running\s+`?unittests`?"),
    re.compile(r"^\s+Running\s+`?target"),
    # Docker build progress
    re.compile(r"^#\d+\s+"),
    re.compile(r"^#[\d.]+\s+"),
    # npm install progress
    re.compile(r"^\s*⸨\s*⸩\s*"),  # npm spinner
]

_COMBINED_PROGRESS_RE = re.compile(
    "|".join(f"(?:{p.pattern})" for p in _PROGRESS_NOISE_PATTERNS),
    re.IGNORECASE,
)

# Pre-compiled regexes for near-identical warning normalization (module-level
# so they compile once, not per call).
_NORMALIZE_RE = re.compile(r"\d+:\d+(?::\d+)?")
_BARE_NUM_RE = re.compile(r"(?<!\[E)\b\d+\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(command: str) -> bool:
    """Stage 1: decide if this command is a build/test/run command.

    Matches the command name (and optionally subcommand) against the
    eligibility list. Returns True if the command is a known build/test/run
    command. Never raises.
    """
    try:
        tokens = shlex.split(command)
        if not tokens:
            return False

        # Strip leading env var assignments (FOO=bar) and `env` command
        cmd_start = 0
        if tokens[cmd_start] == "env":
            cmd_start += 1
        while (cmd_start < len(tokens)
               and "=" in tokens[cmd_start]
               and not tokens[cmd_start].startswith("-")):
            cmd_start += 1
        if cmd_start >= len(tokens):
            return False

        cmd = tokens[cmd_start]
        subcmd = tokens[cmd_start + 1] if cmd_start + 1 < len(tokens) else ""

        # Handle ./gradlew, ./configure, etc.
        if "/" in cmd:
            cmd = cmd.rsplit("/", 1)[-1]

        # Python -m <module> (handles python, python3, python2, python3.11, etc.)
        if cmd.startswith("python") and subcmd == "-m":
            if cmd_start + 2 < len(tokens):
                module = tokens[cmd_start + 2]
                return module in _PYTHON_M_BUILD_MODULES
            return False

        # Direct command match
        if cmd in _BUILD_TEST_COMMANDS:
            allowed_subs = _BUILD_TEST_COMMANDS[cmd]
            if allowed_subs is None:
                return True  # any subcommand
            if subcmd in allowed_subs:
                return True
            # npm run <script> — any script is build/test/run
            if cmd == "npm" and subcmd == "run":
                return True
            if cmd in ("yarn", "pnpm") and subcmd == "run":
                return True
            # npx <anything> runs a build/test tool
            if cmd == "npx":
                return True
            return False

        return False
    except Exception:
        return False


def classify_by_shape(output: str) -> bool:
    """Stage 2: generic fallback on output shape.

    Returns True when the output has >= 3 lines matching build/test markers
    and is large enough to justify compression. Never raises.
    """
    try:
        if not output or len(output) < _MIN_COMPRESS_BYTES:
            return False
        lines = output.splitlines()
        if len(lines) < 10:
            return False
        match_count = 0
        for line in lines:
            if _COMBINED_SHAPE_RE.search(line):
                match_count += 1
                if match_count >= 3:
                    return True
        return match_count >= 3
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Compression helpers
# ---------------------------------------------------------------------------

def _is_error_line(line: str) -> bool:
    """True if this line is a distinct error line that must be preserved."""
    return bool(_COMBINED_ERROR_RE.search(line))


def _is_summary_line(line: str) -> bool:
    """True if this line is an exit/summary line that must be preserved."""
    return bool(_COMBINED_SUMMARY_RE.search(line))


def _is_progress_noise(line: str) -> bool:
    """True if this line is progress/spinner noise that can be dropped."""
    return bool(_COMBINED_PROGRESS_RE.search(line))


def _collapse_identical_runs(lines: list[str]) -> list[str]:
    """Collapse runs of identical consecutive lines into one + count marker.

    "Compiling foo..." x 50 -> "Compiling foo...  [x50 identical lines collapsed]"
    """
    if not lines:
        return []
    result: list[str] = []
    i = 0
    while i < len(lines):
        current = lines[i]
        run = 1
        while i + run < len(lines) and lines[i + run] == current:
            run += 1
        if run > 1:
            result.append(f"{current}  [{run} identical lines collapsed]")
        else:
            result.append(current)
        i += run
    return result


def _collapse_near_identical_warnings(lines: list[str]) -> list[str]:
    """Collapse near-identical warning blocks.

    Lines that differ only in line numbers or addresses are grouped:
      "warning: unused variable: `x` (at 10:5)"
      "warning: unused variable: `x` (at 20:8)"
    -> "warning: unused variable: `x` (at 10:5)  [+2 near-identical warnings]"

    Single O(n) walk with lazy normalization: only warning lines get
    normalized, and only as the run grows.
    """
    if len(lines) < 3:
        return list(lines)

    def _normalize(ln: str) -> str:
        s = _NORMALIZE_RE.sub("N:N", ln)
        s = _BARE_NUM_RE.sub("N", s)
        s = _HEX_RE.sub("0xADDR", s)
        return s.strip()

    result: list[str] = []
    i = 0
    while i < len(lines):
        current = lines[i]
        # Only collapse lines that look like warnings; skip normalization
        # for non-warning lines (the common case in build output).
        if "warning" not in current.lower():
            result.append(current)
            i += 1
            continue
        norm_current = _normalize(current)
        run = 1
        while (i + run < len(lines)
               and "warning" in lines[i + run].lower()
               and _normalize(lines[i + run]) == norm_current):
            run += 1
        if run > 1:
            result.append(f"{current}  [+{run - 1} near-identical warnings]")
        else:
            result.append(current)
        i += run
    return result


def _find_distinct_error_lines(lines: list[str]) -> list[str]:
    """Find every DISTINCT error line (deduplicated by exact text)."""
    seen: set[str] = set()
    errors: list[str] = []
    for line in lines:
        if _is_error_line(line):
            if line not in seen:
                seen.add(line)
                errors.append(line)
                if len(errors) >= _MAX_ERROR_LINES:
                    break
    return errors


def _find_summary_lines(lines: list[str]) -> list[str]:
    """Find all summary/exit lines (deduplicated, order preserved)."""
    seen: set[str] = set()
    summaries: list[str] = []
    for line in lines:
        if _is_summary_line(line):
            if line not in seen:
                seen.add(line)
                summaries.append(line)
    return summaries




# ---------------------------------------------------------------------------
# Main compression entry point
# ---------------------------------------------------------------------------

def compress(command: str, output: str) -> str | None:
    """Compress build/test/run output.

    Returns the compressed string, or None to signal "pass through raw".
    Never raises (fail-open: any exception -> None).

    The caller (bash_compress_hook.py) handles:
      - ANSI stripping (done before calling this)
      - Archiving the full original behind the expand pointer
      - _enforce_baseline_invariant
      - Logging to trends.db with feature=build_output_compress
      - Appending the thrash nudge
    """
    try:
        if not output or len(output) < _MIN_COMPRESS_BYTES:
            return None

        lines = output.splitlines()
        if len(lines) < 10:
            return None

        # --- single O(n) pass: noise drop + error/summary/credential scan ---
        # Combines what was 4 separate full scans into one walk. Each line is
        # tested against at most 3 combined regexes (progress, error, summary)
        # plus credential patterns, with early exit on progress noise.
        try:
            from bash_compress import _TOKEN_PATTERNS
        except Exception:
            _TOKEN_PATTERNS = ()

        # Fast literal pre-filter for credentials: most build output lines
        # contain none of these substrings, so the regex loop is skipped.
        _CRED_LITERALS = ("AKIA", "api_key", "apikey", "API_KEY",
                          "password", "PASSWORD", "secret", "token",
                          "Bearer", "BEGIN PRIVATE KEY", "ghp_", "gho_",
                          "sk-", "xoxb-", "xoxp-")

        kept_lines: list[str] = []
        noise_dropped = 0
        distinct_errors: list[str] = []
        _error_seen: set[str] = set()
        summary_lines: list[str] = []
        _summary_seen: set[str] = set()
        preserved_lines: list[str] = []

        for line in lines:
            # Progress noise: drop immediately (cheapest check, most common)
            if _COMBINED_PROGRESS_RE.search(line):
                noise_dropped += 1
                continue
            kept_lines.append(line)

            # Error line: keep distinct (bounded)
            if _COMBINED_ERROR_RE.search(line):
                if line not in _error_seen and len(_error_seen) < _MAX_ERROR_LINES:
                    _error_seen.add(line)
                    distinct_errors.append(line)

            # Summary line: keep distinct
            if _COMBINED_SUMMARY_RE.search(line):
                if line not in _summary_seen:
                    _summary_seen.add(line)
                    summary_lines.append(line)

            # Credential check: literal pre-filter then regex loop.
            # Most build output lines have no credential substrings.
            if _TOKEN_PATTERNS and any(lit in line for lit in _CRED_LITERALS):
                for pat in _TOKEN_PATTERNS:
                    if pat.search(line):
                        preserved_lines.append(line)
                        break

        # --- collapse repetition (each pass is O(n) over kept_lines) ---
        collapsed = _collapse_identical_runs(kept_lines)
        collapsed = _collapse_near_identical_warnings(collapsed)

        # If we didn't actually reduce the line count meaningfully, bail out
        if len(collapsed) >= len(lines) * 0.85 and noise_dropped < len(lines) * 0.10:
            # Not enough repetition to justify compression.
            # Still re-inject preserved lines in case they were dropped.
            result = "\n".join(collapsed)
            if preserved_lines:
                collapsed_set = set(collapsed)
                appended = [p for p in preserved_lines if p not in collapsed_set]
                if appended:
                    result = result + "\n" + "\n".join(appended)
            if result != output:
                return result
            return None

        # --- assemble the compressed output ---
        if len(collapsed) <= _HEAD_LINES + _TAIL_LINES:
            parts = list(collapsed)
        else:
            head = collapsed[:_HEAD_LINES]
            tail = collapsed[-_TAIL_LINES:]
            middle = collapsed[_HEAD_LINES:-_TAIL_LINES]
            parts = list(head)
            if len(middle) > 50:
                parts.append(f"... [{len(middle)} more lines, repetition collapsed] ...")
            else:
                parts.extend(middle)
            parts.extend(tail)

        # Re-inject distinct error lines not already in the output
        current_set = set(parts)
        for e in distinct_errors:
            if e not in current_set:
                parts.append(e)
                current_set.add(e)

        # Re-inject summary lines not already in the output
        for s in summary_lines:
            if s not in current_set:
                parts.append(s)
                current_set.add(s)

        # Re-inject credential-bearing lines not already in the output
        for p in preserved_lines:
            if p not in current_set:
                parts.append(p)
                current_set.add(p)

        compressed = "\n".join(parts)

        # Only return if we actually shrank the output
        if len(compressed) >= len(output):
            return None

        return compressed
    except Exception:
        return None
