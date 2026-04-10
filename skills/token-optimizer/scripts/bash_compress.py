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

import re
import shlex
import subprocess
import sys

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
    re.compile(r"npm_[a-zA-Z0-9]{36}"),                   # npm token
    re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+"),              # Slack bot token
    re.compile(r"xoxp-[0-9]+-[a-zA-Z0-9]+"),              # Slack user token
    re.compile(r"xoxa-[0-9]+-[a-zA-Z0-9]+"),              # Slack app token
    re.compile(r"sk_live_[a-zA-Z0-9]{24,}"),              # Stripe live
    re.compile(r"rk_live_[a-zA-Z0-9]{24,}"),              # Stripe restricted
    re.compile(r"hf_[a-zA-Z0-9]{34}"),                    # HuggingFace
    re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.I), # Generic Bearer
]

# ANSI escape code pattern
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\]8;[^\x07]*\x07[^\x1b]*\x1b\]8;;\x07")


def _strip_ansi(text):
    """Remove ANSI escape codes. Preserve URL text from OSC 8 hyperlinks."""
    return _ANSI_RE.sub("", text)


def _find_preserved_lines(text):
    """Find line indices containing credentials/tokens (PRE-compression scan)."""
    preserved = set()
    for i, line in enumerate(text.splitlines()):
        for pat in _TOKEN_PATTERNS:
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
        elif "nothing to commit" in line:
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


def _compress_pytest(output):
    """Compress pytest: keep summary + failure details only."""
    lines = output.strip().splitlines()

    # Find the summary line (e.g., "26 passed, 1 failed in 3.42s")
    summary_line = ""
    for line in reversed(lines):
        if "passed" in line or "failed" in line or "error" in line:
            stripped = line.strip().lstrip("=").strip()
            if stripped:
                summary_line = stripped
                break

    # Find failure section
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
        # Keep failure details but cap at 30 lines
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
            return "git_status"
        elif subcmd in ("log",):
            return "git_log"
        elif subcmd in ("diff", "show"):
            return "git_diff"
    elif cmd in ("pytest", "py.test") or (cmd in ("python", "python3") and subcmd == "-m" and
                                           cmd_start + 2 < len(tokens) and tokens[cmd_start + 2] == "pytest"):
        return "pytest"
    elif cmd in ("jest", "vitest") or (cmd == "npx" and subcmd in ("jest", "vitest")):
        return "jest"
    elif cmd == "rspec":
        return "pytest"  # similar enough format
    elif cmd in ("go", "cargo") and subcmd == "test":
        return "pytest"
    elif cmd == "npm" and subcmd in ("install", "ci"):
        return "npm_install"
    elif cmd in ("pip", "pip3") and subcmd == "install":
        return "npm_install"
    elif cmd == "cargo" and subcmd == "build":
        return "npm_install"
    elif cmd in ("ls", "find"):
        return "ls"

    return None


_PATTERN_HANDLERS = {
    "git_status": _compress_git_status,
    "git_log": _compress_git_log,
    "git_diff": _compress_git_diff,
    "pytest": _compress_pytest,
    "jest": _compress_jest,
    "npm_install": _compress_npm_install,
    "ls": _compress_ls,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compress(command_str, raw_output):
    """Compress CLI output based on command pattern.

    Returns compressed output. On any issue, returns raw output.
    Token preservation scan runs FIRST on raw output.
    """
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

    # Re-inject preserved lines that were stripped by compression
    if preserved_lines:
        original_lines = cleaned.splitlines()
        compressed_text = compressed
        for line_idx in preserved_lines:
            if line_idx < len(original_lines):
                preserved_line = original_lines[line_idx]
                if preserved_line not in compressed_text:
                    compressed_text += f"\n{preserved_line}"
        compressed = compressed_text

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
        raw_output = result.stdout
        if result.stderr:
            raw_output += result.stderr

        compressed = compress(command_str, raw_output)

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
