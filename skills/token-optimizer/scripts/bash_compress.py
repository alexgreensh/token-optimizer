#!/usr/bin/env python3
"""Token Optimizer v5: Bash Output Compression Wrapper.

Invoked by bash_hook.py via PreToolUse command rewriting:
  bash_compress.py git status
  bash_compress.py pytest tests/

Runs the command, captures output, applies pattern-matched compression.
On ANY error, returns raw output unchanged (fail-open).

Security:
- shell=True is NEVER used
- Token preservation scan runs on PRE-compression output
- Output buffered completely before writing to stdout
- Partial output on timeout is NEVER compressed
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Token/credential preservation patterns (scanned PRE-compression)
# ---------------------------------------------------------------------------

_TOKEN_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                     # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),                   # OpenAI / Anthropic
    re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}"),             # Anthropic specific
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                   # GitHub PAT (classic)
    re.compile(r"gho_[a-zA-Z0-9]{36}"),                   # GitHub OAuth
    re.compile(r"ghs_[a-zA-Z0-9]{36}"),                   # GitHub server-to-server
    re.compile(r"ghr_[a-zA-Z0-9]{36}"),                   # GitHub refresh
    re.compile(r"github_pat_[a-zA-Z0-9_]{80,}"),          # GitHub fine-grained PAT
    re.compile(r"npm_[a-zA-Z0-9]{36}"),                   # npm token
    re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+"),              # Slack bot token
    re.compile(r"xoxp-[0-9]+-[a-zA-Z0-9]+"),              # Slack user token
    re.compile(r"xoxa-[0-9]+-[a-zA-Z0-9]+"),              # Slack app token
    re.compile(r"sk_live_[a-zA-Z0-9]{24,}"),              # Stripe live
    re.compile(r"rk_live_[a-zA-Z0-9]{24,}"),              # Stripe restricted
    re.compile(r"hf_[a-zA-Z0-9]{34}"),                    # HuggingFace
    re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.I),  # Generic Bearer
    # v5.1 additions — raised by security review, common in bash compression
    # input families (docker, lint, build, logs).
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                # Google API key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),             # Google OAuth access token
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),    # PEM private key header
    re.compile(r"(?:postgres|postgresql|mysql|mongodb|mongodb\+srv|redis)://[^:\s/]+:[^@\s]+@", re.I),  # DB URI with password
    re.compile(r"https?://[^:\s/@]+:[^@\s]+@", re.I),      # HTTP(S) basic auth in URL (registry/proxy creds)
]

# ANSI escape code patterns.
#
# _ANSI_CSI_RE drops colour/style/cursor escape sequences.
#
# _ANSI_OSC8_RE matches a full OSC 8 hyperlink -- opener, label text, closer --
# and is used with a ``\1`` backreference so the visible label text survives
# (captured as group 1). The previous combined regex swallowed the label along
# with the opener/closer, which meant any credential embedded in the visible
# hyperlink text vanished before the preservation scan ran.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ANSI_OSC8_RE = re.compile(r"\x1b\]8;[^\x07]*\x07([^\x1b]*)\x1b\]8;;\x07")

# Stderr patterns that indicate failure even with exit code 0 (linters often
# emit errors on stderr but exit 0 because they only reported warnings).
# Localised error markers are included so non-English toolchains still trip
# the tee. The patterns are case-insensitive for Latin scripts and literal
# for CJK scripts (case folding is a no-op there).
_ERROR_STDERR_PATTERNS = [
    re.compile(r"\berror\s*:", re.I),
    re.compile(r"\bfatal\s*:", re.I),
    re.compile(r"\bpanic\s*:", re.I),
    re.compile(r"\bFAILED\b"),
    re.compile(r"\bTraceback\b"),
    # Localised variants:
    re.compile(r"\bfehler\s*:", re.I),       # German
    re.compile(r"\berreur\s*:", re.I),       # French
    re.compile(r"\berrore\s*:", re.I),       # Italian
    re.compile(r"\bошибка\s*:", re.I),       # Russian
    re.compile(r"错误\s*[:：]"),              # Chinese (simplified)
    re.compile(r"錯誤\s*[:：]"),              # Chinese (traditional)
    re.compile(r"エラー\s*[:：]"),            # Japanese
    re.compile(r"오류\s*[:：]"),              # Korean
]


def _strip_ansi(text):
    """Remove ANSI escape codes. Preserve visible text from OSC 8 hyperlinks.

    Two-step replace: first collapse OSC 8 hyperlinks to their visible label
    (via the captured group 1), then drop CSI escape sequences. This keeps
    any credential that happens to appear inside the visible label text
    available to the pre-compression preservation scan, instead of dropping
    it with the surrounding hyperlink structure.
    """
    text = _ANSI_OSC8_RE.sub(r"\1", text)
    return _ANSI_CSI_RE.sub("", text)


def _looks_like_failure(returncode, stderr):
    """Return True when the command should not have its output compressed.

    Triggers on non-zero exit, OR on exit code 0 with an error pattern on
    stderr (common for linters that print "error:" on stderr while exiting 0).
    Fail-open: on any surprise, treat as non-failure so the normal compression
    path still runs.
    """
    try:
        if returncode not in (0, None):
            return True
        if not stderr:
            return False
        for pat in _ERROR_STDERR_PATTERNS:
            if pat.search(stderr):
                return True
    except Exception:
        return False
    return False


# Error markers in scripts/languages the English-keyword handlers would
# otherwise drop. Any line matching one of these is re-injected through the
# same preservation path credentials use, so a foreign-language error
# surfaces even when the compression handler speaks only English.
#
# Latin-script entries require a trailing colon so short Latin tokens that
# also appear in English tech vocabulary do not false-positive. FOUT
# (Flash Of Unstyled Text) in web-perf output, FEL in Free Electron Laser
# toolchains, and Hata propagation model references are common enough
# that a bare word boundary would preserve unrelated lines and inflate
# the compressed output.
#
# CJK-script entries stay as literal character sequences since those
# characters do not collide with English tech tokens.
_FOREIGN_ERROR_PATTERNS = [
    re.compile(r"\bfout\s*[:：]", re.I),        # Dutch
    re.compile(r"\bfel\s*[:：]", re.I),         # Swedish
    re.compile(r"\bvirhe\s*[:：]", re.I),       # Finnish
    re.compile(r"\bhiba\s*[:：]", re.I),        # Hungarian
    re.compile(r"\bhata\s*[:：]", re.I),        # Turkish
    re.compile(r"\berro\s*[:：]", re.I),        # Portuguese / Spanish
    re.compile(r"\bblad\s*[:：]", re.I),        # Polish
    re.compile(r"l\u1ed7i\s*[:：]", re.I),      # Vietnamese
    re.compile(r"\u0e02\u0e49\u0e2d\u0e1c\u0e34\u0e14\u0e1e\u0e25\u0e32\u0e14"),  # Thai
    re.compile(r"\u05e9\u05d2\u05d9\u05d0\u05d4"),                                # Hebrew
    re.compile(r"\u062e\u0637\u0623"),                                            # Arabic
    re.compile(r"\u062e\u0637\u0627"),                                            # Persian
]


def _find_preserved_lines(text):
    """Find line indices that must survive compression (credentials + errors).

    Two scans run here:
      1. Token patterns (credentials) — never drop a line carrying a secret.
      2. Foreign-language error markers — the English-keyword handlers below
         will happily drop a Dutch "fout:" line because it does not contain
         "error"/"failed"/etc. We flag it here so the re-injection path
         brings it back after compression runs.
    """
    preserved = set()
    for i, line in enumerate(text.splitlines()):
        for pat in _TOKEN_PATTERNS:
            if pat.search(line):
                preserved.add(i)
                break
        else:
            # Only scan error patterns when the line did not already match
            # a credential pattern (small optimization).
            for pat in _ERROR_STDERR_PATTERNS:
                if pat.search(line):
                    preserved.add(i)
                    break
            else:
                for pat in _FOREIGN_ERROR_PATTERNS:
                    if pat.search(line):
                        preserved.add(i)
                        break
    return preserved


# ---------------------------------------------------------------------------
# Compression patterns (one per command family)
# ---------------------------------------------------------------------------

def _compress_git_status(output):
    """Compress git status to one-line summary."""
    lines = output.strip().splitlines()
    branch = "?"
    ahead_behind = ""

    staged_files = []
    unstaged_files = []
    untracked_files = []
    section = None
    for line in lines:
        if line.startswith("On branch "):
            branch = line.replace("On branch ", "").strip()
        elif "ahead" in line or "behind" in line:
            ahead_behind = line.strip().lstrip("(").rstrip(")")
        elif line.strip() in (
            "nothing to commit, working tree clean",
            "nothing to commit (working directory clean)",
            "nothing to commit, working directory clean",
        ):
            # Anchored equality check: the string "nothing to commit" can
            # legitimately appear inside a filename under "Untracked files:"
            # (e.g. `nothing to commit.txt`). A substring match there would
            # falsely report the tree clean and hide real untracked work.
            return f"branch: {branch}, clean{f' ({ahead_behind})' if ahead_behind else ''}"
        elif "Changes to be committed:" in line:
            section = "staged"
        elif "Changes not staged" in line:
            section = "unstaged"
        elif "Untracked files:" in line:
            section = "untracked"
        elif (line.startswith("\t") or line.startswith("        ")) and section:
            fname = line.strip()
            # Strip prefixes like "new file:", "modified:", "deleted:"
            for prefix in ("new file:", "modified:", "deleted:", "renamed:", "copied:"):
                if fname.startswith(prefix):
                    fname = fname[len(prefix):].strip()
                    break
            if section == "staged":
                staged_files.append(fname)
            elif section == "unstaged":
                unstaged_files.append(fname)
            elif section == "untracked":
                untracked_files.append(fname)

    parts = [f"branch: {branch}"]
    if ahead_behind:
        parts.append(ahead_behind)
    if staged_files:
        parts.append(f"{len(staged_files)} staged: {', '.join(staged_files)}")
    if unstaged_files:
        parts.append(f"{len(unstaged_files)} unstaged: {', '.join(unstaged_files)}")
    if untracked_files:
        parts.append(f"{len(untracked_files)} untracked: {', '.join(untracked_files)}")
    return "\n".join(parts) if len(parts) > 2 else ", ".join(parts)


def _compress_git_log(output):
    """Compress git log: keep hash + message, strip noise."""
    lines = output.strip().splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip GPG signature lines
        if stripped.startswith("gpg:") or stripped.startswith("Primary key"):
            continue
        # Skip merge noise
        if stripped.startswith("Merge:"):
            continue
        result.append(stripped)
    compressed = "\n".join(result)
    return compressed if compressed else output


def _compress_git_diff(output):
    """Compress git diff: keep file names and stats, truncate large diffs."""
    lines = output.strip().splitlines()
    if len(lines) <= 50:
        return output  # small diff, keep full

    # Extract summary stats
    additions = 0
    deletions = 0

    for line in lines:
        if line.startswith("diff --git"):
            pass
        elif line.startswith("+++"):
            pass
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    # Keep first 30 lines of actual diff content
    result_lines = lines[:30]
    if len(lines) > 30:
        result_lines.append(f"\n... ({len(lines) - 30} more lines, +{additions}/-{deletions} total)")

    return "\n".join(result_lines)


_PYTEST_SUMMARY_TAIL_LINES = 40


def _compress_pytest(output):
    """Compress pytest/cypress/playwright/mocha/karma/rspec test output.

    Extracts trailing summary lines (anything containing passed, passing,
    failed, failing, error, pending, or skipped) plus the explicit
    FAILURES/ERRORS section when pytest emits one. Cypress and playwright
    speak in "passing" / "failing" terms, so the reverse-scan accepts both
    vocabularies. The scan window is bounded by ``_PYTEST_SUMMARY_TAIL_LINES``
    to avoid walking the whole output on pathological inputs, but is wide
    enough to survive 15-20 trailing warning lines emitted after the summary.
    """
    lines = output.strip().splitlines()
    if not lines:
        return output

    summary_kw = ("passed", "passing", "failed", "failing", "error",
                  "pending", "skipped")

    # Reverse-scan the tail for a contiguous block of summary lines.
    # Accept up to 5 non-matching lines inside the block (trailing warnings)
    # before declaring the summary region over.
    summary_block = []
    non_match_run = 0
    MAX_NON_MATCH = 5
    for line in reversed(lines[-_PYTEST_SUMMARY_TAIL_LINES:]):
        stripped = line.strip().lstrip("=").strip()
        if not stripped:
            if summary_block:
                non_match_run += 1
                if non_match_run > MAX_NON_MATCH:
                    break
            continue
        if any(kw in stripped.lower() for kw in summary_kw):
            summary_block.append(stripped)
            non_match_run = 0
        elif summary_block:
            non_match_run += 1
            if non_match_run > MAX_NON_MATCH:
                break
    summary_block.reverse()
    summary_line = "\n".join(summary_block)

    # Find failure section (pytest-native)
    failure_lines = []
    in_failures = False
    for line in lines:
        if "FAILURES" in line or "ERRORS" in line:
            in_failures = True
            continue
        if in_failures:
            if line.startswith("=" * 10):
                break  # end of failures section
            failure_lines.append(line)

    if failure_lines:
        failure_text = "\n".join(failure_lines[:30])
        if len(failure_lines) > 30:
            failure_text += f"\n... ({len(failure_lines) - 30} more failure lines)"
        return f"{summary_line}\n\n{failure_text}"

    return summary_line if summary_line else output


def _compress_jest(output):
    """Compress jest/vitest: keep summary + failure details."""
    lines = output.strip().splitlines()

    summary_lines = []
    failure_lines = []

    for line in lines:
        if "Tests:" in line or "Test Suites:" in line or "Time:" in line:
            summary_lines.append(line.strip())
        elif "FAIL" in line and ("::" in line or ">" in line):
            failure_lines.append(line.strip())
        elif line.strip().startswith("Expected:") or line.strip().startswith("Received:"):
            failure_lines.append(line.strip())

    result = "\n".join(summary_lines)
    if failure_lines:
        result += "\n\nFailures:\n" + "\n".join(failure_lines[:20])
    return result if result.strip() else output


def _compress_npm_install(output):
    """Compress npm/pip install: summary only."""
    lines = output.strip().splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Keep: added/removed/audited summary, vulnerability count, warnings
        if any(kw in stripped.lower() for kw in [
            "added", "removed", "audited", "packages",
            "vulnerabilit", "up to date", "successfully installed",
            "warn", "error", "fatal",
        ]):
            result.append(stripped)
    return "\n".join(result) if result else output


def _compress_ls(output):
    """Compress directory listing: truncate at 50 entries."""
    lines = output.strip().splitlines()
    if len(lines) <= 50:
        return output

    result = lines[:50]
    result.append(f"... ({len(lines) - 50} more entries, {len(lines)} total)")
    return "\n".join(result)


# Lint rule-code patterns. Ordered by specificity so the first match wins.
# The CAPS+digits pattern catches ruff/flake8/pylint/shellcheck/pydocstyle.
# The trailing kebab pattern catches eslint rule names. The CamelCase/slash
# pattern catches rubocop cops. The parenthesised trailing word catches
# golangci-lint linter tags like `(typecheck)` or `(govet)`.
_LINT_CODE_PATTERNS = [
    re.compile(r"\b([A-Z]{1,4}\d{2,5})\b"),            # F401, E501, SC2086, W0611
    re.compile(r"\s([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\s*$"),  # eslint trailing kebab
    re.compile(r"\b([A-Z][A-Za-z]+/[A-Z][A-Za-z]+)\b"),     # rubocop Style/Foo
    re.compile(r"\(([a-z][a-z0-9]+)\)\s*$"),                # golangci (typecheck)
]

# Prefixes that look like lint rule codes but are not. A line like
# "vulnerable to CVE2024" or "HTTP404 on /api" would otherwise get grouped
# as if CVE2024 / HTTP404 were linter rules. The handler discards matches
# whose first two letters are any of these prefixes.
_LINT_CODE_BLOCKLIST_PREFIXES = (
    "CVE", "RFC", "ISO", "HTTP", "ISBN", "USD", "EUR", "GBP",
    "JPY", "SHA", "MD5", "PGP", "TCP", "UDP", "DNS",
)


def _compress_build(output):
    """Compress JS/TS/Go build output (tsc/webpack/esbuild/vite/next/go build).

    Keeps lines that look like errors, warnings, or final summary markers.
    Drops the bulk of bundler chatter (asset lists, file size tables,
    incremental compile stats). When there is no recognisable error/warning
    or summary signal at all, returns the raw output unchanged — the 10%
    ratio gate in ``compress()`` then throws the compression away on its own.
    The handler never fabricates a positive "build clean" claim.
    """
    lines = output.splitlines()
    if len(lines) < 20:
        return output

    error_kw = ("error", "warning", "failed", "fatal")
    summary_kw = (
        "compiled successfully", "built in", "build finished",
        "build completed", "done in", "errors", "warnings",
        "bundled ", "chunk ", "emitted ", "hash:", "version:",
    )
    # "Found 0 errors" / "0 warnings" / "no errors" are CLEAN signals that
    # happen to contain an error_kw substring. Treat them as summary,
    # not as error/warning, so a clean build is not reported with a
    # misleading "1 error/warning lines" header. The match is
    # left-anchored so a real error line like
    # `error TS2322: Expected 'no errors' but found 'undefined'` is not
    # mis-routed to the summary bucket, and so a line like
    # `10 errors in 5 files` cannot match the `0 errors` prefix.
    clean_summary_patterns = (
        "found 0 error", "0 errors", "0 warnings", "no errors",
        "no warnings", "0 problems",
    )
    # "Found N errors" (N > 0) is an aggregate summary, not an individual
    # error line. Counting it as an error inflates the handler's "N
    # error/warning lines" header. We detect it via a regex and route to
    # the summary bucket.
    found_n_errors_re = re.compile(r"^found\s+\d+\s+(error|warning|problem)s?", re.I)
    # "Errors  Files" style tsc trailing column headers are also summary.
    error_header_res = re.compile(r"^(errors?\s+files?|errors?\s+warnings?)$", re.I)

    errors = []
    summaries = []
    total_kept = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if any(low.startswith(pat) for pat in clean_summary_patterns):
            summaries.append(stripped)
            continue
        if found_n_errors_re.match(low) or error_header_res.match(low):
            summaries.append(stripped)
            continue
        if any(kw in low for kw in error_kw):
            errors.append(stripped)
            total_kept += 1
            if total_kept > 80:  # bound even on pathological error dumps
                break
        elif any(kw in low for kw in summary_kw):
            summaries.append(stripped)

    if not errors and not summaries:
        # No recognisable signal — hand the raw output back unchanged. The
        # 10% ratio gate in compress() will see zero compression and also
        # return raw. Do NOT fabricate a "no errors detected" marker: that
        # would be a positive claim about build state we cannot verify.
        return output

    parts = []
    if errors:
        parts.append(f"{len(errors)} error/warning lines:")
        parts.extend(errors[:40])
        if len(errors) > 40:
            parts.append(f"... ({len(errors) - 40} more errors/warnings)")
    if summaries:
        parts.append("")
        parts.extend(summaries[-5:])

    return "\n".join(parts)


def _compress_list(output):
    """Compress long listing commands (pip list / npm ls / docker ps / brew list).

    Shows the first ~10 entries verbatim, then a "... N more" placeholder,
    and preserves any trailing summary line. Header lines (first non-empty
    line plus common "Name Version" style headers) survive to keep the
    output interpretable. Short lists (<20 entries) pass through raw.
    """
    lines = output.splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    if len(non_empty) < 20:
        return output

    header_lines = []
    data_lines = []
    seen_data = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Detect header separators (all dashes, "Package Version", etc.)
        if not seen_data and (
            set(stripped) <= set("-= ")
            or any(stripped.lower().startswith(prefix) for prefix in (
                "package", "name ", "container", "image", "repository",
            ))
        ):
            header_lines.append(line)
            continue
        seen_data = True
        data_lines.append(line)

    if len(data_lines) < 20:
        return output

    keep_count = 10
    result = list(header_lines)
    result.extend(data_lines[:keep_count])
    result.append(f"... ({len(data_lines) - keep_count} more entries, {len(data_lines)} total)")
    return "\n".join(result)


def _compress_progress(output):
    """Compress build progress output (docker build, cargo build verbose, etc.).

    Drops noisy progress lines (layer staging, "Downloading", "Extracting",
    cache hit indicators) and keeps lines that signal status, warnings,
    errors, or final outcomes. Safe for pip install / cargo build / docker
    build style output. Unknown lines fall through as summary context when
    there are few enough of them to matter.
    """
    lines = output.splitlines()
    if len(lines) < 20:
        return output

    noisy_prefixes = (
        "Downloading ", "Collecting ", "Resolving ", "Building wheels ",
        "Created wheel", "Using cached ", "Requirement already ",
        "Extracting", "Transferring",
    )
    # docker-buildx progress markers: `#12 DONE 0.1s`, `#12 ...`, etc.
    docker_progress_re = re.compile(r"^#\d+\s+")
    # docker pull layer fetch progress (shows up as `abc123456789: Pulling...`)
    pull_progress_re = re.compile(
        r"^[a-f0-9]{12}:\s+(Pulling|Waiting|Already|Extracting|Download|Verifying)"
    )

    keep = []
    dropped = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        low = stripped.lower()

        # Always keep errors, warnings, failures, and final success markers.
        if any(kw in low for kw in ("error", "warning", "fail", "fatal",
                                    "successfully", "built ",
                                    "installed", "complete", "naming to",
                                    "exporting")):
            keep.append(stripped)
            continue

        # Drop noisy progress chatter.
        if stripped.startswith(noisy_prefixes):
            dropped += 1
            continue
        if docker_progress_re.match(stripped) and "DONE" not in stripped:
            dropped += 1
            continue
        if pull_progress_re.match(stripped):
            dropped += 1
            continue

        # Keep everything else as context.
        keep.append(stripped)

    if dropped < max(10, len(lines) // 4):
        return output  # not noisy enough to justify compression

    return "\n".join(keep)


def _tree_line_depth(line):
    """Return the logical depth of a `tree` output line.

    Walks the 4-character prefix chunks `tree` emits: vertical pipes ("│   "),
    blanks ("    "), and branch markers ("├── "/"└── "). Each pipe or blank
    chunk is one level; a branch marker contributes the final +1 for the leaf.
    Root-level names with no prefix return 0.
    """
    i = 0
    depth = 0
    while i + 4 <= len(line):
        chunk = line[i:i + 4]
        if chunk == "\u2502   " or chunk == "    ":  # "│   " or four spaces
            depth += 1
            i += 4
        elif chunk == "\u251c\u2500\u2500 " or chunk == "\u2514\u2500\u2500 ":  # ├── / └──
            return depth + 1
        else:
            return depth
    return depth


def _compress_tree(output):
    """Compress the `tree` command: keep entries up to depth 2 plus the summary.

    Depth is inferred from `tree`'s box-drawing prefix. Root (depth 0), direct
    children (depth 1), and grandchildren (depth 2) are kept; anything deeper
    is dropped and replaced with a single "(N entries at depth > 2 truncated)"
    marker. The trailing "N directories, M files" summary line survives
    because summary lines have depth 0. Credentials appearing in deep file
    names are preserved by the pre-compression token scan.
    """
    lines = output.splitlines()
    if len(lines) < 20:
        return output  # not enough to bother

    result = []
    truncated = 0
    for line in lines:
        if not line.strip():
            result.append(line)
            continue
        depth = _tree_line_depth(line)
        if depth <= 2:
            result.append(line)
        else:
            truncated += 1

    if truncated == 0:
        return output

    result.append(f"... ({truncated} entries at depth > 2 truncated)")
    return "\n".join(result)


def _compress_logs(output):
    """Compress log-like output (tail/journalctl).

    Detects runs of adjacent duplicate lines and collapses them to a single
    line plus a "(xN)" marker. Only activates when the duplicate rate is at
    least 30% of the input, so normal mixed logs pass through raw.

    Credential safety: the collapsed line is ``<original>  (xN)``, so the
    original line text is kept verbatim as a prefix. Any credential on the
    line is therefore preserved by the line itself — the pre-compression
    token scan still runs as a backstop, and its re-injection check (``if
    preserved_line not in compressed_text``) correctly finds the credential
    as a substring and skips adding a duplicate copy.
    """
    lines = output.splitlines()
    if len(lines) < 20:
        return output  # short logs: not worth the compression risk

    collapsed = []
    dup_removed = 0
    i = 0
    while i < len(lines):
        current = lines[i]
        run = 1
        while i + run < len(lines) and lines[i + run] == current:
            run += 1
        if run > 1:
            collapsed.append(f"{current}  (x{run})")
            dup_removed += run - 1
        else:
            collapsed.append(current)
        i += run

    # Require meaningful dup density before accepting the compression.
    if dup_removed < max(10, len(lines) // 3):
        return output

    return "\n".join(collapsed)


def _compress_lint(output):
    """Compress lint output (eslint/ruff/flake8/rubocop/shellcheck/biome/pylint/golangci-lint).

    Group findings by rule code, show top-5 codes with a single sample line,
    preserve trailing summary lines (e.g. "Found 6 errors") verbatim. Unknown
    lines fall through uncounted so the handler stays robust against format
    drift — credentials are preserved via the PRE-compression token scan.
    """
    lines = output.splitlines()
    if len(lines) < 10:
        return output

    counts = {}
    samples = {}
    summary_lines = []
    total_findings = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        matched_code = None
        for pat in _LINT_CODE_PATTERNS:
            # Walk every match of this pattern on the line so a blocked
            # candidate (e.g. CVE2024) does not shadow a real lint code
            # (e.g. E501) that appears later in the same line.
            for m in pat.finditer(stripped):
                candidate = m.group(1)
                if candidate.startswith(_LINT_CODE_BLOCKLIST_PREFIXES):
                    continue
                matched_code = candidate
                break
            if matched_code:
                break
        if matched_code:
            counts[matched_code] = counts.get(matched_code, 0) + 1
            if matched_code not in samples:
                sample = stripped
                if len(sample) > 140:
                    sample = sample[:137] + "..."
                samples[matched_code] = sample
            total_findings += 1
        else:
            # Trailing summary context: "Found 6 errors.", "✖ 12 problems", etc.
            low = stripped.lower()
            if any(tok in low for tok in ("found", "error", "warning", "problem",
                                          "clean", "passed", "failed", "checked")):
                summary_lines.append(stripped)

    if total_findings < 5:
        return output  # not worth the complexity for short lint runs

    parts = [f"{total_findings} findings across {len(counts)} rule codes"]

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for code, cnt in ranked[:5]:
        sample = samples.get(code, "")
        parts.append(f"  {code} x{cnt}: {sample}")

    if len(ranked) > 5:
        tail_count = sum(c for _, c in ranked[5:])
        parts.append(f"  ... {len(ranked) - 5} other codes ({tail_count} findings)")

    if summary_lines:
        parts.append("")
        for line in summary_lines[-3:]:
            parts.append(line)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pattern dispatch
# ---------------------------------------------------------------------------

def _detect_pattern(command_str):
    """Detect which compression pattern to use based on command."""
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        return None

    if not tokens:
        return None

    # Strip leading env vars
    cmd_start = 0
    while cmd_start < len(tokens) and "=" in tokens[cmd_start]:
        cmd_start += 1

    if cmd_start >= len(tokens):
        return None

    cmd = tokens[cmd_start]
    subcmd = tokens[cmd_start + 1] if cmd_start + 1 < len(tokens) else ""

    if cmd == "git":
        if subcmd in ("status",):
            # Only the default human-readable format is parseable. Any
            # flag that switches to machine-readable output (--porcelain
            # v1/v2, --short/-s, -z) routes to raw so the handler does not
            # swallow file lists it cannot recognise.
            git_args = tokens[cmd_start + 2:]
            for arg in git_args:
                if arg.startswith("--porcelain") or arg.startswith("--short"):
                    return None
                # Short-flag clusters (-s, -sb, -bs, -z, -sz): any
                # cluster containing s or z changes the output format.
                if arg.startswith("-") and not arg.startswith("--"):
                    if "s" in arg[1:] or "z" in arg[1:]:
                        return None
            return "git_status"
        elif subcmd in ("log",):
            return "git_log"
        elif subcmd in ("diff", "show"):
            return "git_diff"
        elif subcmd in ("branch",):
            # `git branch` lists branches — route through the list handler
            # so long branch lists truncate with a summary.
            return "list"
    elif cmd in ("pytest", "py.test") or (cmd in ("python", "python3") and subcmd == "-m" and
                                           cmd_start + 2 < len(tokens) and tokens[cmd_start + 2] == "pytest"):
        return "pytest"
    elif cmd in ("jest", "vitest") or (cmd == "npx" and subcmd in ("jest", "vitest")):
        return "jest"
    elif cmd == "rspec":
        return "pytest"  # similar enough format
    # v5.1 test-runner extensions (dispatch-only: cypress/playwright/mocha/
    # karma all report in "passing/failing" terminology; the extended pytest
    # handler now reads all five formats.)
    elif cmd == "mocha" or (cmd == "npx" and subcmd == "mocha"):
        return "pytest"
    elif cmd == "karma" or (cmd == "npx" and subcmd == "karma"):
        return "pytest"
    elif cmd == "cypress" and subcmd == "run":
        return "pytest"
    elif cmd == "npx" and subcmd == "cypress":
        return "pytest"
    elif cmd == "playwright" and subcmd == "test":
        return "pytest"
    elif cmd == "npx" and subcmd == "playwright":
        return "pytest"
    elif cmd in ("go", "cargo") and subcmd == "test":
        return "pytest"
    elif cmd == "npm" and subcmd in ("install", "ci"):
        return "npm_install"
    elif cmd == "npm" and subcmd == "test":
        # `npm test` dispatches to whatever the project wires up, usually
        # jest/vitest/mocha. Route through the pytest handler, which
        # understands all of them.
        return "pytest"
    elif cmd in ("pip", "pip3") and subcmd == "install":
        return "npm_install"
    elif cmd == "cargo" and subcmd == "build":
        return "npm_install"
    elif cmd in ("ls", "find"):
        return "ls"
    # v5.1 lint handlers
    elif cmd in ("eslint", "flake8", "pylint", "shellcheck", "rubocop"):
        return "lint"
    elif cmd == "ruff" and subcmd == "check":
        return "lint"
    elif cmd == "biome" and subcmd == "lint":
        return "lint"
    elif cmd == "golangci-lint" and subcmd == "run":
        return "lint"
    # v5.1 logs handler (read-only log inspection). `cat` is intentionally
    # NOT whitelisted — only tail and journalctl, which are log-specific.
    elif cmd in ("tail", "journalctl"):
        return "logs"
    # v5.1 tree handler
    elif cmd == "tree":
        return "tree"
    # v5.1 progress handler (docker build, docker pull)
    elif cmd == "docker" and subcmd in ("build", "pull"):
        return "progress"
    # v5.1 build handler
    elif cmd in ("tsc", "webpack", "esbuild"):
        return "build"
    elif cmd in ("vite", "next") and subcmd == "build":
        return "build"
    elif cmd == "go" and subcmd == "build":
        return "build"
    # v5.1 list handler
    elif cmd in ("pip", "pip3") and subcmd == "list":
        return "list"
    elif cmd == "npm" and subcmd == "ls":
        return "list"
    elif cmd == "pnpm" and subcmd == "list":
        return "list"
    elif cmd == "docker" and subcmd == "ps":
        return "list"
    elif cmd == "brew" and subcmd == "list":
        return "list"
    elif cmd == "sqlite3":
        return "sqlite3"
    elif cmd in ("wc", "du", "df"):
        return "disk_stats"
    elif cmd == "printenv":
        return "list"
    elif cmd == "docker" and subcmd in ("exec", "logs", "inspect"):
        if subcmd == "logs":
            return "logs"
        return "docker_output"
    elif cmd == "kubectl" and subcmd in ("get", "describe", "logs"):
        if subcmd == "logs":
            return "logs"
        return "list"

    return None


def _compress_sqlite3(output):
    """Compress sqlite3 query output: truncate large result sets."""
    lines = output.strip().splitlines()
    if len(lines) < 30:
        return output
    header = lines[:2]
    data = lines[2:22]
    result = header + data
    result.append(f"... ({len(lines) - 22} more rows, {len(lines)} total)")
    return "\n".join(result)


def _compress_disk_stats(output):
    """Compress du/df/wc output: keep header + totals."""
    lines = output.strip().splitlines()
    if len(lines) < 20:
        return output
    head = lines[:3]
    tail = lines[-5:]
    kept = len(head) + len(tail)
    result = head
    result.append(f"... ({len(lines) - kept} entries omitted)")
    result.extend(tail)
    return "\n".join(result)


def _compress_docker_output(output):
    """Compress docker exec/logs/inspect output."""
    stripped = output.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped[:200_000])
            if isinstance(data, list) and len(data) > 0:
                first_preview = json.dumps(data[0], indent=2)[:500]
                return f"[{len(data)} items, first:\n{first_preview}\n...]"
            elif isinstance(data, dict):
                keys = list(data.keys())[:10]
                return f"Object with {len(data)} keys: {', '.join(keys)}"
        except (json.JSONDecodeError, RecursionError, TypeError):
            pass
    return _compress_logs(output)


_PATTERN_HANDLERS = {
    "git_status": _compress_git_status,
    "git_log": _compress_git_log,
    "git_diff": _compress_git_diff,
    "pytest": _compress_pytest,
    "jest": _compress_jest,
    "npm_install": _compress_npm_install,
    "ls": _compress_ls,
    "lint": _compress_lint,
    "logs": _compress_logs,
    "tree": _compress_tree,
    "progress": _compress_progress,
    "list": _compress_list,
    "build": _compress_build,
    "sqlite3": _compress_sqlite3,
    "disk_stats": _compress_disk_stats,
    "docker_output": _compress_docker_output,
}

# Maps _detect_pattern() output to the feature name stored in compression_events.
_COMPRESS_FEATURE_MAP = {
    "git_status": "bash_compress_git",
    "git_log": "bash_compress_git",
    "git_diff": "bash_compress_git",
    "pytest": "bash_compress_pytest",
    "jest": "bash_compress_jest",
    "npm_install": "bash_compress_npm",
    "ls": "bash_compress_ls",
    "lint": "bash_compress_lint",
    "logs": "bash_compress_logs",
    "tree": "bash_compress_tree",
    "progress": "bash_compress_progress",
    "list": "bash_compress_list",
    "build": "bash_compress_build",
    "sqlite3": "bash_compress_build",
    "disk_stats": "bash_compress_list",
    "docker_output": "bash_compress_build",
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compress(command_str, raw_output, returncode=0, stderr=""):
    """Compress CLI output based on command pattern.

    Returns compressed output. On any issue, returns raw output.
    Token preservation scan runs FIRST on raw output.

    Tee-on-failure: if the command failed (non-zero exit) or printed error
    patterns on stderr even with exit 0, return raw output verbatim. Never
    compress failure output — the user needs the full signal to debug.
    """
    if _looks_like_failure(returncode, stderr):
        return raw_output  # fail-open tee: full output, unchanged

    if not raw_output or len(raw_output) < 100:
        return raw_output  # too small to bother

    # Strip ANSI codes (always safe)
    cleaned = _strip_ansi(raw_output)

    # PRE-compression token preservation scan
    preserved_lines = _find_preserved_lines(cleaned)

    # Detect pattern
    pattern = _detect_pattern(command_str)
    if pattern is None:
        return cleaned  # no pattern, return ANSI-stripped only

    handler = _PATTERN_HANDLERS.get(pattern)
    if handler is None:
        return cleaned

    try:
        compressed = handler(cleaned)
    except Exception:
        return cleaned  # fail open

    # Re-inject preserved lines that were stripped by compression.
    # Uses exact-line membership (not substring) so a short preserved
    # line that happens to be contained inside a longer compressed line
    # is still emitted on its own rather than silently folded into the
    # larger line.
    if preserved_lines:
        original_lines = cleaned.splitlines()
        compressed_line_set = set(compressed.splitlines())
        appended: list[str] = []
        for line_idx in preserved_lines:
            if line_idx < len(original_lines):
                preserved_line = original_lines[line_idx]
                if preserved_line not in compressed_line_set:
                    appended.append(preserved_line)
                    compressed_line_set.add(preserved_line)
        if appended:
            compressed = compressed + "\n" + "\n".join(appended)

    # Check if compression actually saved enough (10% minimum via bytes/4)
    # We use 10% rather than 30% because even modest truncation (e.g., ls with 60 entries)
    # is valuable context savings. The whitelist already limits risk.
    original_tokens = len(cleaned.encode("utf-8", errors="replace")) // 4
    compressed_tokens = len(compressed.encode("utf-8", errors="replace")) // 4
    if original_tokens > 0 and (1.0 - compressed_tokens / original_tokens) < 0.10:
        return cleaned  # not worth the risk

    return compressed


def main():
    """Run a command through compression wrapper."""
    if len(sys.argv) < 2:
        print("Usage: bash_compress.py <command...>", file=sys.stderr)
        sys.exit(1)

    command_args = sys.argv[1:]
    command_str = shlex.join(command_args)

    try:
        result = subprocess.run(
            command_args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        raw_output = stdout + stderr

        # Memory safety: commands like `find /`, `tree /`, or `journalctl`
        # under a large time window can emit hundreds of MB through
        # capture_output. Compression would duplicate that payload multiple
        # times across regex scans and rebuilt strings, so above 5MB we
        # emit raw and skip the compression path entirely.
        MAX_COMPRESS_BYTES = 5 * 1024 * 1024
        if len(raw_output.encode("utf-8", errors="replace")) > MAX_COMPRESS_BYTES:
            sys.stdout.write(raw_output)
            sys.stdout.flush()
            sys.exit(result.returncode)

        compressed = compress(
            command_str,
            raw_output,
            returncode=result.returncode,
            stderr=stderr,
        )

        # Record savings to trends.db if compression was meaningful
        try:
            orig_bytes = len(raw_output.encode("utf-8", errors="replace"))
            comp_bytes = len(compressed.encode("utf-8", errors="replace"))
            if comp_bytes < orig_bytes * 0.9:
                _pattern = _detect_pattern(command_str)
                _feature = _COMPRESS_FEATURE_MAP.get(_pattern or "")
                if _feature:
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from measure import _log_compression_event
                    _log_compression_event(
                        feature=_feature,
                        original_text=raw_output,
                        compressed_text=compressed,
                        session_id=os.environ.get("CLAUDE_SESSION_ID", ""),
                        command_pattern=command_str[:100],
                        quality_preserved=True,
                        verified=True,
                    )
        except Exception:
            pass

        # Buffer completely, then write
        sys.stdout.write(compressed)
        sys.stdout.flush()
        sys.exit(result.returncode)

    except subprocess.TimeoutExpired as e:
        # NEVER compress partial output on timeout
        partial = ""
        if e.stdout:
            partial += e.stdout if isinstance(e.stdout, str) else e.stdout.decode("utf-8", errors="replace")
        if e.stderr:
            partial += e.stderr if isinstance(e.stderr, str) else e.stderr.decode("utf-8", errors="replace")
        sys.stdout.write(partial)
        sys.stdout.write("\n[TIMEOUT after 60s - output may be incomplete]\n")
        sys.stdout.flush()
        sys.exit(124)

    except Exception as e:
        # Fail open: emit the error so Claude sees it instead of empty output
        sys.stderr.write(f"[bash_compress: wrapper error: {type(e).__name__}: {e}]\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
